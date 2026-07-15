# Architecture

TonerWatch is a single-process, single-binary web application
that manages many Printix customers from one runtime. This document
describes the data model, request lifecycle and deployment topology.

## Guiding principles

- **One instance, many customers.** A single deployment holds many
  Printix customer tenants. All of them are polled from one runtime
  and rolled up into shared dashboards.
- **All configuration via environment variables.** No config files.
- **All state in one place.** SQLite database + Fernet key inside the
  `/data` volume. Backup one directory, back up the whole tool.
- **Encrypt at rest what must be.** BI-database credentials, mail
  credentials and any future OAuth tokens are Fernet-encrypted with a
  key that never leaves `/data`.
- **Brand compliance is not optional.** The Tungsten Brand Book is
  encoded in `base.html` — CSS variables, typography, allowed
  gradients, rhombus frame shapes. Never inline off-brand colours.

## Deployment topology

```
                                       ┌──────────────────────────┐
                                       │   Printix BI-DB (Azure   │
                                       │   SQL) — customer tenant │
                                       └────────────▲─────────────┘
                                                    │ read-only SELECTs
                                                    │ (pymssql / pyodbc)
    ┌────────────┐    HTTPS    ┌────────────────────┴────────────┐
    │ MSP tech   ├─────────────▶  Azure App Service (Linux)      │
    │ (browser)  │             │  ─ TonerWatch          │
    └────────────┘             │    (FastAPI + uvicorn)          │
                               │  ─ APScheduler (alert runner)   │
                               └─────────────────┬───────────────┘
                                                 │
                              ┌──────────────────┴─────────────────┐
                              │ Azure File share mounted at /data  │
                              │   ├── tonerwatch.sqlite           │
                              │   └── fernet.key                   │
                              └────────────────────────────────────┘
                                                 │
                                                 │ SMTP / Resend API
                                                 ▼
                                     ┌──────────────────────────┐
                                     │  Alert recipients (email)│
                                     └──────────────────────────┘
```

Every customer's BI credentials are stored encrypted in the SQLite
database. The runtime decrypts them on demand to run supply-level
queries; the decrypted values never leave process memory.

## Data model (target — all phases)

```
users                     MSP staff (technicians and admins)
  id, email, password_hash, name, role, entra_oid?, created_at, last_login_at

customers                 Printix customer tenants under management
  id, name, tenant_url, notes,
  sql_server, sql_database, sql_username, sql_password_enc,
  alert_recipients_csv, alert_min_level,
  order_recipients_csv,
  warn_pct, critical_pct,
  timezone, quiet_hours_start, quiet_hours_end,
  auto_order_mode,  -- 'off' | 'draft'
  active, created_at, created_by_user_id

customer_access           M:N — technicians assigned to specific customers
  user_id, customer_id, access_level   -- 'read' | 'admin'
  (empty for admins → admins see every customer)

supply_templates          Reusable per-model catalog entries
  id, printer_model, color, sku, description, manufacturer,
  supplier, supplier_url, default_quantity, unit_price_cents, notes,
  is_shared, updated_at, updated_by_user_id

printer_supplies          Per-device override on top of the template
  customer_id, printer_id, color,
  sku, description, manufacturer, supplier, supplier_url,
  default_quantity, unit_price_cents, notes,
  updated_at, updated_by_user_id

toner_state               Latest reading & alert bookkeeping per marker
  customer_id, printer_id, color,
  level, severity, last_notified_at, last_seen_at

toner_events              Append-only alert / order log
  id, customer_id, kind, printer_id, color, level,
  created_at, meta_json

toner_orders              Cross-customer order pipeline
  id, customer_id, printer_id, printer_name, color, quantity,
  status,                 -- 'draft'|'ordered'|'delivered'|'installed'|'cancelled'
  ordered_at, closed_at, closed_reason,
  ordered_by_user_id, notes

saved_views               Persistent per-user filter presets
  id, user_id, name, scope, filters_json, is_shared, created_at

audit_log                 Every state-changing operation
  id, user_id, action, target_type, target_id, meta_json, created_at

settings                  Instance-level configuration (JSON blob per key)
  key, value_json, updated_at
```

## Request lifecycle

1. **Language resolution.** `LanguageMiddleware` picks the UI language
   in this order: (a) explicit `?lang=xx` query, which is persisted to
   the session; (b) session cookie; (c) `Accept-Language` header,
   matched against the EFIGS set; (d) `DEFAULT_LANG`.
2. **Session.** `SessionMiddleware` (Starlette) reads a signed cookie
   into `request.session`. `require_user()` in `auth.py` resolves the
   session's `user_id` to a fresh `users` row on every request.
3. **Authorisation.** Route handlers call `require_admin()` or
   `require_customer_access(customer_id)` where relevant. The latter
   consults `customer_access` when the user is a technician.
4. **Database.** Every handler that touches SQLite opens a short-lived
   connection via `db.get_conn()`. WAL journaling is on, so readers
   never block the alert runner.
5. **Rendering.** Jinja2 renders one of the templates in
   `src/web/templates/`. Every template extends `base.html`, which owns
   all brand-level chrome, navigation and the language switcher.

## Alert runner (P3)

APScheduler runs `evaluate_and_notify(customer)` for every active
customer on a configurable cadence (default: every 15 minutes). The
runner is *cooperative* with the HTTP handlers — same SQLite database,
short transactions, no long-held locks.

Per-marker state (`toner_state`) tracks the last notification timestamp
and hysteresis, so a threshold crossing produces exactly one email even
if the level oscillates around it. Digest mode collects transitions
into a single per-customer email when the customer is configured that
way.

## Order flow (P4b)

When an alert fires:

- If the customer has `auto_order_mode = 'off'`, the alert email lists
  the SKU (if the supply library has an entry) and includes a signed
  *"mark as ordered"* magic link. Clicking it creates a `toner_orders`
  row in state `ordered` and closes the alert.
- If `auto_order_mode = 'draft'`, the runner creates a `toner_orders`
  row in state `draft` first, then sends an email that says *"a draft
  order was created — please review"*. The row lives in the Kanban
  board until a technician confirms it.

The Kanban board is cross-customer by default; per-customer filters
are available via the saved-views system (P5).

## Brand compliance

`src/web/templates/base.html` is the single source of truth for the
visual language. All CSS custom properties map back to the Tungsten
Brand Book:

- Colour palette (primary, accents, greys, domain backdrops)
- Typography (Red Hat Display, five weights, hierarchy)
- Radius scale, spacing scale (4 px grid)
- Signature rhombus 65° angle — reused via `clip-path` on hero
  frames and text banners
- Print &amp; Workplace product gradient (`#00EB86 → #00A0FB`) used
  sparingly on the header and key call-to-action buttons

## Internationalisation

Five languages: English, French, Italian, German, Spanish (EFIGS).

- Every user-facing string routes through `_('key')` in Jinja
  (Python: `t('key', lang)`).
- Adding a new key requires values for **all five** languages in the
  same commit. `check_translations()` in `src/web/i18n.py` enforces
  the invariant at boot and refuses to start with gaps.
- The language switcher lives in `base.html`; its selection is stored
  in the user's session, overriding the browser preference for the
  session's lifetime.
