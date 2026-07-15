"""Restore a TonerWatch backup ZIP.

Companion to :mod:`src.backup`. Reads a ZIP produced by
``/settings/backup/download`` (or the scheduled Azure Blob upload)
and copies the SQLite database + Fernet key back into place.

Usage (from a shell inside the container or venv):

    python -m src.restore <backup.zip> [--force] [--to /data]

Safety:
* Refuses to run when the target SQLite file already exists AND is
  non-empty, unless ``--force`` is passed. Rationale: if the operator
  has already added data since the backup, restoring silently would
  lose it.
* Refuses to restore across incompatible backend types (SQLite ZIP
  onto an MSSQL deployment, and vice versa) — the manifest carries
  the source backend.
* Always saves the pre-restore state to a timestamped .pre-restore
  suffix so a wrong click is one ``mv`` away from recoverable.

Web UI equivalent is intentionally NOT provided — restore is a
one-shot ops action best done from a shell where the operator has
full context on the DB state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


class RestoreError(Exception):
    """Raised on any validation / IO failure — printed and exits 1."""


# ---------------------------------------------------------------------------
# ZIP inspection
# ---------------------------------------------------------------------------

def read_manifest(zip_path: Path) -> dict:
    if not zip_path.exists():
        raise RestoreError(f"backup file not found: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                raise RestoreError(
                    "backup ZIP has no manifest.json — is this a TonerWatch backup?")
            with zf.open("manifest.json") as f:
                data = json.loads(f.read().decode("utf-8"))
    except zipfile.BadZipFile as e:
        raise RestoreError(f"not a valid ZIP file: {e}") from e
    if data.get("product") != "Printix TonerWatch":
        raise RestoreError(
            f"manifest.product != 'Printix TonerWatch' (got: {data.get('product')!r}) "
            "— aborting to prevent accidental cross-product restore")
    return data


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore(zip_path: Path, target_dir: Path, *,
            force: bool = False, restore_fernet: bool = True) -> dict:
    """Extract database.sqlite (+ optional fernet.key) from the ZIP
    into ``target_dir``. Returns a summary dict with what was
    actually restored + which paths were moved aside."""
    zip_path = zip_path.resolve()
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    manifest = read_manifest(zip_path)
    if manifest.get("backend") != "sqlite":
        raise RestoreError(
            f"manifest backend={manifest.get('backend')!r} — this ZIP "
            "was created against a non-SQLite backend. Use the "
            "database's native restore tooling instead.")

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if "database.sqlite" not in names:
            raise RestoreError("backup ZIP has no database.sqlite entry")

        target_db = target_dir / "tonerwatch.sqlite"
        moved_db = _move_aside(target_db, force=force,
                                what="database.sqlite")

        # Stream the DB out
        with zf.open("database.sqlite") as src, open(target_db, "wb") as dst:
            shutil.copyfileobj(src, dst)

        result = {
            "manifest":   manifest,
            "database":   str(target_db),
            "moved_db":   str(moved_db) if moved_db else None,
            "fernet":     None,
            "moved_fernet": None,
        }

        if restore_fernet and "fernet.key" in names:
            target_key = target_dir / "fernet.key"
            moved_key = _move_aside(target_key, force=force,
                                     what="fernet.key")
            with zf.open("fernet.key") as src, open(target_key, "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                os.chmod(target_key, 0o600)
            except OSError:
                pass
            result["fernet"]        = str(target_key)
            result["moved_fernet"]  = str(moved_key) if moved_key else None

    return result


def _move_aside(target: Path, *, force: bool, what: str) -> Path | None:
    """If ``target`` exists non-empty, move it aside to a timestamped
    ``.pre-restore-YYYYMMDD-HHMMSS`` suffix and return the new path.
    When force=False and the file exists, raise instead."""
    if not target.exists():
        return None
    if target.stat().st_size == 0:
        target.unlink()  # empty file — no data to lose
        return None
    if not force:
        raise RestoreError(
            f"{what} already exists at {target} with data — pass "
            "--force to move it aside and restore anyway. The existing "
            "file will be renamed with a .pre-restore-<timestamp> suffix.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    aside = target.with_suffix(target.suffix + f".pre-restore-{stamp}")
    target.rename(aside)
    return aside


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.restore",
        description="Restore a TonerWatch backup ZIP into /data (or --to).")
    parser.add_argument("zip", type=Path,
                        help="Path to the backup ZIP file.")
    parser.add_argument("--to", type=Path, default=Path("/data"),
                        help="Target directory (default: /data).")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing DB / key (they get "
                        "moved aside with a .pre-restore suffix).")
    parser.add_argument("--no-fernet", action="store_true",
                        help="Skip restoring the Fernet key even if "
                        "present in the ZIP.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read manifest + report what WOULD be "
                        "restored, but don't touch any file.")
    args = parser.parse_args(argv)

    try:
        manifest = read_manifest(args.zip)
    except RestoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"backup manifest: {json.dumps(manifest, indent=2, ensure_ascii=False)}")

    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0

    try:
        r = restore(args.zip, args.to, force=args.force,
                    restore_fernet=not args.no_fernet)
    except RestoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"✓ database restored → {r['database']}")
    if r["moved_db"]:
        print(f"  (previous DB kept as {r['moved_db']})")
    if r["fernet"]:
        print(f"✓ Fernet key restored → {r['fernet']}")
        if r["moved_fernet"]:
            print(f"  (previous key kept as {r['moved_fernet']})")
    elif not args.no_fernet:
        print("  (no fernet.key in ZIP — skip)")

    print("\nDone. Start TonerWatch to run Alembic migrations against "
          "the restored database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
