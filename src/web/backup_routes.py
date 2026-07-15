"""Backup routes — download ZIP, save Azure Blob config, trigger upload.

All admin-only. Delegates the actual work to :mod:`src.backup`.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from .. import auth, backup, db


router = APIRouter()


# ---------------------------------------------------------------------------
# Download ZIP
# ---------------------------------------------------------------------------

@router.get("/settings/backup/download", include_in_schema=False)
async def backup_download(request: Request):
    admin = auth.require_admin(request)
    include_fernet = (request.query_params.get("include_fernet") or "").lower() in ("1", "true", "yes", "on")
    zip_bytes = backup.create_backup_zip(include_fernet=include_fernet)
    filename = backup.default_backup_filename()

    db.audit(admin["id"], "backup.downloaded",
             target_type="settings", target_id="backup",
             meta_json=json.dumps({"bytes": len(zip_bytes),
                                    "include_fernet": include_fernet}))

    def _stream():
        yield zip_bytes

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Azure Blob config (save + manual upload)
# ---------------------------------------------------------------------------

@router.post("/settings/backup", include_in_schema=False)
async def backup_config_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    cfg = {
        "azure_enabled":       bool(form.get("azure_enabled")),
        "azure_container":     (form.get("azure_container") or "").strip(),
        "azure_conn_str":      form.get("azure_conn_str") or "",
        "azure_interval_hours": form.get("azure_interval_hours") or 24,
        "azure_include_fernet": bool(form.get("azure_include_fernet")),
    }
    backup.save_config(cfg)
    db.audit(admin["id"], "backup.config_saved",
             target_type="settings", target_id="backup",
             meta_json=json.dumps({"azure_enabled": cfg["azure_enabled"]}))
    return RedirectResponse("/settings?info=backup_saved#backup",
                            status_code=303)


@router.post("/settings/backup/upload_now", include_in_schema=False)
async def backup_upload_now(request: Request):
    admin = auth.require_admin(request)
    ok, msg = backup.run_scheduled_upload()
    db.audit(admin["id"], "backup.manual_upload",
             target_type="settings", target_id="backup",
             meta_json=json.dumps({"ok": ok, "message": msg}))
    if ok:
        return RedirectResponse(
            f"/settings?info=backup_uploaded_{msg}#backup",
            status_code=303)
    return RedirectResponse(
        f"/settings?error={msg[:120].replace('&','')}#backup",
        status_code=303)
