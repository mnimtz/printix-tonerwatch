"""Printix Mandanten — partner-API tenant list/create/detail.

Gated by ``auth.require_printix_tenants_access``: admins always see
this when the feature is globally enabled in Settings; technicians
only if an admin explicitly granted them access (users/edit.html).
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, db, printix_partner
from . import i18n


router = APIRouter()

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


@router.get("/printix-tenants", response_class=HTMLResponse,
            include_in_schema=False)
async def printix_tenants_list(request: Request):
    user = auth.require_printix_tenants_access(request)
    templates = request.app.state.templates
    cfg = printix_partner.load_config()
    tenants: list[dict] = []
    error = None
    if printix_partner.is_configured():
        try:
            tenants = printix_partner.list_tenants(cfg)
        except printix_partner.PrintixPartnerError as e:
            error = str(e)
    return templates.TemplateResponse(
        "printix_tenants/list.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "tenants": tenants,
            "configured": printix_partner.is_configured(),
            "error": error,
        },
    )


@router.get("/printix-tenants/new", response_class=HTMLResponse,
            include_in_schema=False)
async def printix_tenant_new_form(request: Request):
    user = auth.require_printix_tenants_access(request)
    if not printix_partner.is_configured():
        return RedirectResponse("/printix-tenants", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "printix_tenants/new.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "form": {"tenant_name": "", "tenant_domain": "",
                     "initial_user_email": "", "initial_user_name": "",
                     "initial_user_admin": False},
            "error": None,
        },
    )


@router.post("/printix-tenants/new", include_in_schema=False)
async def printix_tenant_new_submit(
        request: Request,
        tenant_name: str = Form(""),
        tenant_domain: str = Form(""),
        initial_user_email: str = Form(""),
        initial_user_name: str = Form(""),
        initial_user_admin: str = Form("")):
    user = auth.require_printix_tenants_access(request)
    templates = request.app.state.templates
    lang = request.state.lang

    tenant_name = tenant_name.strip()
    tenant_domain = tenant_domain.strip().lower()
    initial_user_email = initial_user_email.strip()
    initial_user_name = initial_user_name.strip()

    form = {"tenant_name": tenant_name, "tenant_domain": tenant_domain,
            "initial_user_email": initial_user_email,
            "initial_user_name": initial_user_name,
            "initial_user_admin": bool(initial_user_admin)}

    err = None
    if not tenant_name:
        err = i18n.t("printix_tenants.error.name_required", lang)
    elif not tenant_domain or not _DOMAIN_RE.match(tenant_domain):
        err = i18n.t("printix_tenants.error.domain_invalid", lang)
    elif initial_user_email and "@" not in initial_user_email:
        err = i18n.t("printix_tenants.error.initial_user_email_invalid", lang)

    if not err:
        initial_user = None
        if initial_user_email:
            initial_user = {"email": initial_user_email,
                            "name": initial_user_name,
                            "create_as_admin": bool(initial_user_admin)}
        try:
            tenant = printix_partner.create_tenant(
                tenant_name, tenant_domain, initial_user=initial_user)
        except printix_partner.PrintixPartnerError as e:
            err = str(e)
        else:
            db.audit(user["id"], "printix_tenant.created",
                     target_type="printix_tenant",
                     target_id=tenant.get("tenant_id", ""),
                     meta_json=json.dumps({"tenant_name": tenant_name,
                                           "tenant_domain": tenant_domain}))
            return RedirectResponse(
                f"/printix-tenants/{tenant['tenant_id']}", status_code=303)

    return templates.TemplateResponse(
        "printix_tenants/new.html",
        {"request": request, "lang": lang, "user": user,
         "form": form, "error": err},
        status_code=400,
    )


@router.get("/printix-tenants/{tenant_id}", response_class=HTMLResponse,
            include_in_schema=False)
async def printix_tenant_detail(tenant_id: str, request: Request):
    user = auth.require_printix_tenants_access(request)
    templates = request.app.state.templates
    try:
        tenant = printix_partner.get_tenant(tenant_id)
    except printix_partner.PrintixPartnerError as e:
        return templates.TemplateResponse(
            "printix_tenants/detail.html",
            {"request": request, "lang": request.state.lang, "user": user,
             "tenant": None, "error": str(e)},
            status_code=502,
        )
    return templates.TemplateResponse(
        "printix_tenants/detail.html",
        {"request": request, "lang": request.state.lang, "user": user,
         "tenant": tenant, "error": None},
    )
