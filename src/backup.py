"""On-demand backup + Azure Blob upload.

What gets backed up
-------------------

The primary artifact is a ZIP containing:

* ``database.sqlite``   — a fresh copy of the SQLite file
* ``manifest.json``     — version, timestamp, table row counts, DB
                          backend type
* ``fernet.key``        — OPTIONAL, only included when the caller
                          asks for it via ``include_fernet=True``.
                          Without this key the backup is useless
                          for restoring encrypted secrets (BI-DB
                          passwords, mail credentials).

For MSSQL backends we return a manifest-only ZIP with a note
pointing at Azure SQL native backup — dumping large SQL Server DBs
via SQLAlchemy is a bad idea and Azure already does it better.

Azure Blob upload
-----------------

Config lives in the `settings` table under the ``backup`` key
(connection string is Fernet-encrypted). A ``ScheduleUpload`` job
in the APScheduler runner picks the config up at start-up and
runs the upload every N hours (default 24).

Every upload writes a new blob with a datestamped name:
``tonerwatch-YYYYMMDD-HHMMSS.zip``. Old blobs are left untouched —
Azure lifecycle management is a better fit than in-app pruning.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "backup"


# ---------------------------------------------------------------------------
# Config persistence (Azure Blob)
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Return the backup config, or a disabled stub. Secret fields
    are decrypted; caller MUST not persist the decrypted copy back."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    if raw.get("azure_conn_str_enc"):
        try:
            raw["azure_conn_str"] = crypto.decrypt(raw["azure_conn_str_enc"])
        except crypto.CryptoError:
            raw["azure_conn_str"] = ""
    return {
        "azure_enabled":       bool(raw.get("azure_enabled")),
        "azure_container":     raw.get("azure_container", "tonerwatch-backups"),
        "azure_conn_str":      raw.get("azure_conn_str", ""),
        "azure_interval_hours": int(raw.get("azure_interval_hours") or 24),
        "azure_include_fernet": bool(raw.get("azure_include_fernet", True)),
        # Present-flag so the UI can show "connection string stored"
        "azure_conn_str_present": bool(raw.get("azure_conn_str")),
        "last_upload_at":       raw.get("last_upload_at", ""),
        "last_upload_blob":     raw.get("last_upload_blob", ""),
        "last_upload_error":    raw.get("last_upload_error", ""),
    }


def save_config(cfg: dict[str, Any]) -> None:
    """Persist backup config. Encrypts the connection string; empty
    submission means "keep the currently stored one"."""
    payload: dict[str, Any] = {
        "azure_enabled":       bool(cfg.get("azure_enabled")),
        "azure_container":     (cfg.get("azure_container") or "tonerwatch-backups").strip(),
        "azure_interval_hours": max(1, int(cfg.get("azure_interval_hours") or 24)),
        "azure_include_fernet": bool(cfg.get("azure_include_fernet", True)),
    }
    conn_str = cfg.get("azure_conn_str") or ""
    if conn_str:
        payload["azure_conn_str_enc"] = crypto.encrypt(conn_str)
    else:
        existing = load_config()
        if existing.get("azure_conn_str"):
            payload["azure_conn_str_enc"] = crypto.encrypt(existing["azure_conn_str"])
    # Preserve last-upload log entries — they're informational and
    # shouldn't get wiped on a settings save.
    existing = load_config()
    for k in ("last_upload_at", "last_upload_blob", "last_upload_error"):
        if existing.get(k):
            payload[k] = existing[k]
    _write_settings(payload)


def _record_upload(*, blob: str = "", error: str = "") -> None:
    """Log the outcome of a run to the backup settings so the UI
    can show 'last upload: 2026-07-15 10:23 → tonerwatch-….zip'."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    raw["last_upload_at"] = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")
    raw["last_upload_blob"] = blob or ""
    raw["last_upload_error"] = error or ""
    _write_settings(raw)


def _write_settings(payload: dict[str, Any]) -> None:
    value_json = json.dumps(payload, ensure_ascii=False)
    with db.get_conn() as conn:
        row = conn.execute(
            db.settings.select().where(db.settings.c.key == SETTINGS_KEY)
        ).first()
        if row is None:
            conn.execute(insert(db.settings).values(
                key=SETTINGS_KEY, value_json=value_json))
        else:
            conn.execute(update(db.settings)
                         .where(db.settings.c.key == SETTINGS_KEY)
                         .values(value_json=value_json,
                                 updated_at=func.current_timestamp()))


# ---------------------------------------------------------------------------
# Build a backup ZIP in memory
# ---------------------------------------------------------------------------

def is_sqlite_backend() -> bool:
    """SQLite = we can just copy the file. Anything else (MSSQL,
    Postgres) uses its own native backup — we won't try to reinvent
    that."""
    url = str(db.get_engine().url).lower()
    return url.startswith("sqlite")


def _sqlite_path() -> Path | None:
    """Absolute path to the SQLite file, or None if not on SQLite."""
    if not is_sqlite_backend():
        return None
    url = db.get_engine().url
    if url.database:
        return Path(url.database).resolve()
    return None


def _table_row_counts() -> dict[str, int]:
    """Row counts per table for the manifest. Best-effort — an
    error on any single table shouldn't fail the whole backup."""
    counts: dict[str, int] = {}
    with db.get_conn() as conn:
        for tbl in db.metadata.sorted_tables:
            try:
                n = conn.execute(select(func.count()).select_from(tbl)).scalar()
                counts[tbl.name] = int(n or 0)
            except Exception:
                counts[tbl.name] = -1
    return counts


def _fernet_key_bytes() -> bytes | None:
    """Read the Fernet key from the same place the runtime does.
    Returns None when the key is only in FERNET_KEY env (not on disk)."""
    key_path_env = os.environ.get("FERNET_KEY_FILE", "").strip()
    candidates = []
    if key_path_env:
        candidates.append(Path(key_path_env))
    # Standard locations that entrypoint.sh writes to.
    candidates.extend([Path("/data/fernet.key"), Path("data/fernet.key")])
    for p in candidates:
        if p.exists() and p.is_file():
            try:
                return p.read_bytes()
            except OSError:
                pass
    # Fall back to the env var if we can't read the file — restoring
    # from this backup will need the same env var, which the operator
    # already has in their infrastructure.
    env_key = os.environ.get("FERNET_KEY", "").strip().encode()
    return env_key or None


def create_backup_zip(*, include_fernet: bool = False) -> bytes:
    """Return the ZIP bytes for a fresh backup. Always returns non-
    empty bytes; on MSSQL the ZIP contains only the manifest."""
    buf = io.BytesIO()
    manifest: dict[str, Any] = {
        "product":          "Printix TonerWatch",
        "created_at_utc":   _dt.datetime.now(_dt.timezone.utc).strftime(
                                "%Y-%m-%d %H:%M:%S"),
        "app_version":      _read_version(),
        "backend":          "sqlite" if is_sqlite_backend() else "mssql-or-other",
        "table_row_counts": _table_row_counts(),
        "includes_fernet":  False,
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if is_sqlite_backend():
            sqlite = _sqlite_path()
            if sqlite and sqlite.exists():
                # Use SQLite's own backup API for a consistent copy —
                # a plain file copy can race with in-flight writes.
                with tempfile.NamedTemporaryFile(
                        prefix="tw-backup-", suffix=".sqlite",
                        delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    _sqlite_online_backup(sqlite, tmp_path)
                    zf.write(tmp_path, arcname="database.sqlite")
                    manifest["database_bytes"] = tmp_path.stat().st_size
                finally:
                    tmp_path.unlink(missing_ok=True)
            else:
                manifest["warning"] = "sqlite file not found on disk"
        else:
            manifest["note"] = (
                "MSSQL/Azure SQL backend: use Azure SQL native backup "
                "or `sqlpackage` for full backups. This ZIP contains "
                "only the manifest.")

        if include_fernet:
            key = _fernet_key_bytes()
            if key:
                zf.writestr("fernet.key", key)
                manifest["includes_fernet"] = True
            else:
                manifest["warning_fernet"] = "Fernet key not found — restore of encrypted secrets will fail"

        zf.writestr("manifest.json",
                    json.dumps(manifest, indent=2, ensure_ascii=False))

    return buf.getvalue()


def _sqlite_online_backup(src: Path, dst: Path) -> None:
    """Use sqlite3.Connection.backup() — safe under concurrent writes."""
    import sqlite3
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _read_version() -> str:
    for p in (Path("VERSION"), Path("/app/VERSION")):
        if p.exists():
            try:
                return p.read_text().strip()
            except OSError:
                pass
    return "unknown"


def default_backup_filename() -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"tonerwatch-{stamp}.zip"


# ---------------------------------------------------------------------------
# Azure Blob upload
# ---------------------------------------------------------------------------

class BackupUploadError(Exception):
    """Raised on any Azure-side failure — caller logs + records."""


def upload_to_azure_blob(zip_bytes: bytes, cfg: dict[str, Any]) -> str:
    """Upload the ZIP to Azure Blob. Returns the blob name on success.
    Raises :class:`BackupUploadError` on any failure."""
    if not cfg.get("azure_conn_str"):
        raise BackupUploadError("no connection string configured")
    if not cfg.get("azure_container"):
        raise BackupUploadError("no container name configured")

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as e:
        raise BackupUploadError(
            f"azure-storage-blob not installed: {e}") from e

    blob_name = default_backup_filename()
    try:
        client = BlobServiceClient.from_connection_string(cfg["azure_conn_str"])
        container = client.get_container_client(cfg["azure_container"])
        # Create the container on the fly if it doesn't exist yet —
        # first-run experience should just work.
        try:
            container.create_container()
        except Exception:
            pass  # already exists — expected on 2nd+ runs
        container.upload_blob(name=blob_name, data=zip_bytes, overwrite=False)
    except Exception as e:  # noqa: BLE001
        raise BackupUploadError(str(e)[:300]) from e

    return blob_name


def run_scheduled_upload() -> tuple[bool, str]:
    """Called by APScheduler. Returns (ok, message-or-blob-name)."""
    cfg = load_config()
    if not cfg.get("azure_enabled"):
        return False, "disabled"
    if not cfg.get("azure_conn_str"):
        return False, "no_conn_str"
    try:
        zip_bytes = create_backup_zip(
            include_fernet=cfg.get("azure_include_fernet", True))
    except Exception as e:  # noqa: BLE001
        _record_upload(error=f"backup build failed: {str(e)[:200]}")
        logger.exception("backup build failed")
        return False, "build_failed"
    try:
        blob = upload_to_azure_blob(zip_bytes, cfg)
    except BackupUploadError as e:
        _record_upload(error=str(e)[:300])
        logger.warning("backup upload failed: %s", e)
        return False, str(e)[:200]
    _record_upload(blob=blob)
    logger.info("backup uploaded: %s", blob)
    return True, blob
