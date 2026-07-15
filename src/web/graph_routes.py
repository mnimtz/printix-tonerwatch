"""Admin routes for the Microsoft 365 Copilot Connector."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, graph_connector


router = APIRouter()


@router.post("/settings/graph", include_in_schema=False)
async def graph_config_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    cfg = {
        "enabled":         bool(form.get("enabled")),
        "tenant_id":       form.get("tenant_id") or "",
        "client_id":       form.get("client_id") or "",
        "client_secret":   form.get("client_secret") or "",
        "connection_id":   form.get("connection_id") or "",
        "connection_name": form.get("connection_name") or "",
        "connection_desc": form.get("connection_desc") or "",
        "interval_hours":  form.get("interval_hours") or 24,
    }
    graph_connector.save_config(cfg)
    db.audit(admin["id"], "settings.graph_updated",
             target_type="settings", target_id="graph_connector",
             meta_json=json.dumps({"enabled": cfg["enabled"]}))
    return RedirectResponse("/settings?info=graph_saved#graph",
                            status_code=303)


@router.post("/settings/graph/sync_now", include_in_schema=False)
async def graph_sync_now(request: Request):
    admin = auth.require_admin(request)
    public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    pushed, err = graph_connector.sync_all_printers(public_base_url=public_base)
    db.audit(admin["id"], "graph.manual_sync",
             target_type="settings", target_id="graph_connector",
             meta_json=json.dumps({"pushed": pushed, "error": err[:200]}))
    if err:
        return RedirectResponse(
            f"/settings?error=graph_sync_{err[:120].replace('&','')}#graph",
            status_code=303)
    return RedirectResponse(
        f"/settings?info=graph_sync_ok_{pushed}#graph", status_code=303)
