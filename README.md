# Printix TonerWatch

**Multi-tenant toner monitoring, alerting and ordering console for MSP
partners** — built on top of the Tungsten Printix BI database.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fmnimtz%2Fprintix-tonerwatch%2Fmain%2Fdeploy%2Fazure%2Fazuredeploy.json)
[![Container image](https://ghcr-badge.egpl.dev/mnimtz/printix-tonerwatch/latest_tag?trim=major&label=ghcr.io)](https://github.com/mnimtz/printix-tonerwatch/pkgs/container/tonerwatch)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

One MSP technician oversees toner status across dozens of Printix customer
tenants from a single console — per-customer thresholds, a supply catalog
that maps printer models to OEM cartridges, an order kanban that closes the
loop between *low toner detected* and *cartridge replaced*, and integrations
with Entra ID, Microsoft 365 Copilot and any major LLM provider.

---

## Features

### Fleet visibility

- **One instance, many tenants.** Each Printix customer is registered once
  with its BI-database credentials, billing address and internal customer
  number; the shared runtime queries them all on a schedule and rolls the
  data up into cross-customer views. Customers, orders, and the supply
  library all have their own live freetext search box.
- **Active vs. registered user counts.** The same BI-DB connection used
  for toner data also carries Printix's own `dbo.users` and `dbo.jobs`
  tables — a background tick keeps a 10-minute cache of both, per
  customer: **active users** are those who genuinely printed something
  in the last 30 days (the number that actually correlates with billing/
  licensing, confirmed against a real tenant's Printix Partner Portal
  "Active users" graph), while **registered users** is the older,
  much larger account-exists-and-isn't-disabled count. Both are
  surfaced as dashboard tiles (summed across every visible customer)
  and columns on the customer list. No extra credentials, no Partner
  API dependency — pure read-only discovery of what the existing
  connection already exposes.
- **Two views on the same data.** Card-based grid for quick triage or a
  sortable list view for spreadsheet-style scanning. Toggle persists per
  user in session.
- **Live client-side search + filters.** Chip filters for severity, hide
  devices with no toner data at all (Printix Anywhere queues and anything
  else the BI feed never reported supplies for) with a live count, group
  chips per printer group. Client-side freetext search (name, location,
  model, serial, vendor, asset tag, notes, customer, group) narrows the
  visible cards/rows as you type — no server round-trip.
- **Human-readable activity feed on the dashboard.** Replaces the old raw
  audit log with `👤 Marcus signed in · 3 min ago` and 30+ localized
  action templates. Icons per family (⚙ settings, 👤 user, 🏢 customer,
  🖨 toner, 📦 order, 🛒 supply). Height-capped with its own scrollbar so
  a full page of recent events doesn't push everything else down.
- **Priority-first dashboard.** A personalized one-line status summary
  ("3 critical, 7 warning — Acme GmbH and Beta AG need a look first"),
  a fleet-health donut, and customer cards sorted worst-first (critical →
  warn → healthy → no data yet) instead of insertion order, so the thing
  that needs attention is the first thing you see, not something you have
  to scan for.
- **AI-phrased greeting (optional).** With an LLM configured, the summary
  line above is written by the model instead — built from the exact same
  numbers plus recent toner-level anomalies. When there's actually a
  problem it writes two sentences and names the specific customers with
  real critical/warn counts ("Acme has 3 critical and 1 warn, Beta has 2
  warn"), not just a total; on a clean day it's one warm sentence. Cached
  ~hourly per operator **and per language** (switching the UI language
  gets a fresh sentence in that language immediately, not whatever got
  cached first) so it isn't an LLM call on every page load; any failure
  (not configured, error, slow) falls straight back to the static
  sentence — it's decoration, never a dependency.
- **Human-friendly status badges.** Bare SNMP codes (`NO_PAPER`,
  `MARKER_SUPPLY_LOW`, `DOOR_OPEN`, …) rendered as colored pill badges
  with icons and translated captions; unknown codes fall back to the raw
  code, so a new firmware state still shows up sensibly.
- **Cache-only fast path.** `/toner` reads exclusively from the
  background cache that the scheduler warms — pages render in under
  100 ms regardless of BI DB latency. Cold-cache banners with an inline
  ↻ Refresh button when a customer hasn't been warmed yet.

### Alerting

- **Threshold-based per customer.** Warn + critical percentage configurable
  per customer.
- **Digest mode + quiet hours.** Group per-customer transitions into one
  mail; hold notifications during a configurable window.
- **Three mail providers.** Resend (HTTPS, Azure-friendly), SMTP fallback,
  and **Microsoft Graph** (`/users/{upn}/sendMail`, reuses the Entra SSO
  app registration). Graph provider auto-lists tenant mailboxes for the
  sender picker + inline auth-probe button that pinpoints AADSTS misconfigs.
- **One-click Mail.Send permission grant.** The Graph provider needs
  Mail.Send + User.Read.All as *Application* permissions (not the
  delegated ones sign-in uses) with admin consent — Settings → Entra ID →
  Reconfigure → **📧 Enable Mail.Send** does this automatically for tenant
  Global Administrators, updating the app manifest and admin-consenting
  both roles via Graph. Falls back to a 3-step manual Azure Portal guide
  (with one-click deep links) for admins who registered the app without
  Global Admin rights.
- **Rich HTML mail with one-click ordering.** Every critical/warn row
  carries the SKU + description + a green **🛒 Order now** button + a blue
  **✓ Mark as ordered** magic link (14-day-signed, single action, POST
  confirm to survive link-preview bots). Autonomous mode adds a
  🤖 *Auto-ordered — no action needed* badge on rows the runner sent itself.
- **Alert runner** on APScheduler; default 15 min, configurable via
  `ALERT_INTERVAL_MINUTES`.
- **Anomaly detection on toner-level jumps.** Every poll tick compares the
  new level against the previous one — an increase that lands short of a
  full reset (not a real cartridge replacement) or a very steep one-tick
  drop gets flagged on the customer detail page, no historical time series
  needed. Pure threshold logic on data already collected each tick, not an
  LLM call.
- **Toner-level history.** Every time a printer's level actually changes,
  the reading is appended to a time series (`toner_readings`) — not one
  row per poll, only on real transitions, so the table stays proportional
  to real-world change events. Raw readings age out after an
  admin-configurable retention window (Settings → Alert-Runner, default
  90 days / one quarter) into `toner_readings_daily`, a daily
  avg/min/max/sample-count rollup, so long-run trend analysis stays cheap
  even after months of data. Foundation for a real level-over-time chart
  in Reports once enough history accumulates.

### Order flow (kanban)

- **Five-state state machine** — draft → ordered → delivered → installed
  (or cancelled from any active state).
- **Auto-drafts on alert.** The runner creates one draft per
  (customer, printer, colour) slot; idempotent, so a stuck printer doesn't
  spawn a new draft every tick.
- **AI SKU completion.** If the matching supply template has no SKU AND
  an LLM provider is configured, the runner asks it for the OEM cartridge
  ID + description + yield and merges the result into the fresh draft.
- **Autonomous mode.** Per-customer opt-in (`auto_order_mode = autonomous`)
  — the runner transitions freshly-created drafts straight to `ordered`
  and emails the supplier. A per-customer `auto_order_daily_cap` (default
  10) protects against runaway alerts turning into P.O. spam; excess
  drafts stay as drafts.
- **Three-column kanban** on `/orders` — Draft / Ordered / Delivered plus a
  collapsible "recently closed" section, customer filter dropdown, a live
  freetext search box, and a green **🛒 Order now** button per card.
- **Magic-link handlers.** `/orders/action/{token}` lets an email
  recipient act on an order without logging in — landing page requires a
  POST confirm so mail-preview crawlers can't accidentally change state.
- **AI supplier-mail draft.** A **✉️ Mail text** button on any draft/ordered
  card asks the LLM to write a ready-to-copy purchase-order email (subject
  + body) from the order's own SKU/quantity/printer — TonerWatch never
  sends it, the operator copies it into their own mail client. When the
  SKU is linked to a supplier (see below), the modal also shows a
  pre-filled **To:** address, the customer's account number with that
  supplier, and the resolved delivery address (the specific printer's
  own override, else the customer's own address on file) — all
  resolved automatically, none of it typed by hand.
- **Who created it, who last moved it.** Every card shows who created
  the draft and — if different — who most recently transitioned it
  (marked ordered, delivered, installed, …), so "who ordered this" and
  "who confirmed delivery" are both a glance away.

### Reports

- **Flexible builder** (`/reports`) — any date range (presets for
  7/30/90 days, this quarter, this year, or a custom from/to), a single
  customer or every customer this operator can see, and five
  combinable categories: **orders** (volume, status breakdown, average
  fulfillment time), **consumption** (toner actually shipped —
  delivered/installed orders — by color, printer and customer, with
  spend), **device health** (toner-level anomalies flagged in the
  window, plus printers with unusually many orders as an order-based
  proxy for "might need a look"), **supplier performance**
  (orders, spend and average fulfillment time per supplier), and
  **active users** (opt-in — a live BI-DB snapshot rather than a
  date-windowed aggregate like the others: a per-customer summary of
  both genuinely active users — printed in the last 30 days — and
  registered users, across every customer in scope, or the full
  name/email/department list of active users once exactly one
  customer is selected — a multi-customer report deliberately never
  dumps every visible customer's user directory into one table).
  "Consumption" is measured the honest way — cartridges that actually
  shipped — rather than a level curve; a level-over-time chart becomes
  possible once enough toner-level history (see Alerting) has
  accumulated.
- **Quick-launch templates** on the hub — full 30-day overview,
  consumption trend, supplier performance, and a link straight into
  the existing per-customer savings report.
- **AI executive summary** — button-triggered (never automatic), asks
  the configured LLM to phrase the already-computed numbers into a
  short paragraph suitable for a quarterly business review. Same rule
  as every other AI feature here: the model only ever narrates numbers
  that were already computed in Python, never a source of a number
  itself.
- **CSV export** and a print-friendly layout (`window.print()` → save
  as PDF) for sharing outside the app.
- Same tenant fence as every other page — a technician only ever sees
  data for customers they've been explicitly granted access to.

### Supply library

- **Model templates.** One entry per (printer_model, colour) with SKU,
  description, manufacturer, supplier, order URL, default quantity, unit
  price, page yield, notes. Feeds every printer of that model.
- **Per-printer overrides.** For a specific device that needs a different
  SKU (rebranded cartridge, framework contract, XL variant), override on
  `/toner/{customer}/{printer}/supply`. Empty override fields fall back to
  the template.
- **AI suggest button.** With an LLM provider configured, a **🤖 AI
  suggest** button on the supply form fills empty SKU / description /
  manufacturer / yield_pages from the printer model + colour. Never
  clobbers fields the operator already typed.
- **Seed set** — 13 common OEM entries (HP 26A, HP 415A CMYK,
  Brother TN-421 CMYK, Kyocera TK-5220 CMYK) so a fresh install has
  something to work with.

### Suppliers

- **Global vendor list** (`/suppliers`, admin-only) — name, default order
  mailbox, contact person, phone, postal address, website, notes. The
  same distributor usually serves multiple customers, so suppliers are
  defined once and linked from templates and overrides via a dropdown
  (kept in sync with the legacy free-text `supplier` field for
  display/backward compatibility).
- **Per-customer account details** (`/customers/{id}/suppliers`) — this
  customer's account/customer number with each supplier, plus optional
  overrides for the order-email, contact person, and phone — for the
  rare case a customer orders through a different mailbox or has a
  different account contact than the supplier's own default.
- **Feeds the order-mail draft automatically.** Once a supply template or
  override is linked to a supplier and the customer has an account number
  on file, the **✉️ Mail text** feature resolves the target address,
  account number, and — if a contact person is on file — personalizes
  the greeting instead of a generic salutation. The phone number is
  shown alongside the draft for a quick call on an urgent shortage,
  never part of the mail body itself.

### Printix Partner API (Printix Mandanten)

- **Off by default, one settings switch.** Settings → Printix Partner —
  partner ID, client ID, client secret (Fernet-encrypted), Production
  vs. Test environment. Nothing calls out to Printix until an admin
  both saves credentials *and* flips the switch on.
- **"Printix Mandanten" nav item** appears for admins once enabled, and
  for any individual technician an admin explicitly grants access to
  from that user's edit page — independent of the customer-access
  grants used everywhere else, since this operates across the whole
  partner account rather than one customer.
- **List / create / view tenants + billing**, straight from Printix's
  own partner API (`https://printix.bitbucket.io`) — tenant name,
  domain, optional initial user (with an admin flag) on create; current
  and previous billing period (license count, printing users) on the
  detail page. No update/delete/cancel endpoint is publicly documented,
  so this doesn't offer one either.
- **"Add as TonerWatch customer"** on a tenant's detail page pre-fills
  the name and URL into `/customers/new` — a convenience bridge, not an
  auto-merge. A Printix tenant and a TonerWatch customer (with its own
  BI-DB credentials) stay two separate concepts.
- **Readable errors**, not raw API dumps — bad credentials, an
  already-taken domain, a stale token, all get a specific message
  pointing at what to fix.

### Per-printer metadata overrides

- **Enrich BI with your own info.** `/toner/{customer}/{printer}/info`
  form for location override, serial-number override, asset tag, group
  name (for filter/grouping), delivery address (falls back to the
  customer's own address — only needed when a device ships elsewhere),
  contact name + e-mail, purchase date, warranty date, freeform notes.
- **Group filter + search.** Case-insensitive substring match on printer
  name / location / model / serial / asset tag / notes. Group picker
  with autocomplete from existing groups.

### Saved views

- **Persistent filter presets.** Save the current customer + severity +
  group + search combination as a named preset — one click later, the
  same view is back.
- **Private or shared.** Admins can tick "share with everyone" so a whole
  tenant sees the same well-crafted view.

### Authentication

- **Local password + optional Entra ID SSO.** Bcrypt-hashed passwords
  with per-(IP, email) login throttling; user list has a role chip
  filter (Admin / Technician / All) plus a client-side live search.
- **Entra Auto-Setup via device-code flow.** One click, one sign-in with
  a Global Admin. TonerWatch mints the App Registration + Client Secret
  + tenant-wide admin consent for the OIDC scopes via Microsoft Graph —
  no manual portal work needed.
- **Secret rotation without a new app.** Reconfigure detects the
  existing app and offers 🔑 *Rotate secret only* — mints a fresh
  `client_secret` on the same app registration; existing permissions
  and consent carry over. Also fixes AADSTS7000215 (stored secret
  invalid) without going to Azure Portal.
- **Diagnose page** at `/settings/entra/diagnose` — snapshots the SSO
  config, the effective redirect URI (with a warning if the reverse
  proxy sends `http://`), all local users the SSO flow could match,
  and the last 30 SSO audit events. Callback errors now redirect to
  `/login?error=…` so the message survives.
- **Sign in with Microsoft.** OAuth2 authorization-code flow via MSAL.
  Auto-provisioning with optional email-domain restriction, default role
  for new users.
- **Role-based access.** Admins see every customer; technicians only see
  the customers they were explicitly granted access to (M:N table).
- **Invite users by email.** Users → *Invite user* creates the account
  with no password (login-blocked until completed) and emails a signed,
  7-day link to set one — no password to generate or hand over
  yourself. If no mail provider is configured, or the send fails, the
  admin gets the raw link to forward manually instead of a dead end.
  Pending invites show a badge on the user list with a one-click resend.

### Backup

- **One-shot ZIP download.** SQLite database (via SQLite's online backup
  API — safe under concurrent writes) + manifest.json + optional Fernet
  key. On MSSQL backends the ZIP is manifest-only with a pointer at
  Azure SQL native backup.
- **Scheduled Azure Blob upload.** Connection string Fernet-encrypted at
  rest; container auto-created; interval configurable in hours;
  last-upload timestamp + blob name (or error) shown in Settings.

### Database setup

- **SQLite by default, Azure SQL as an alternative.** `/settings/database`
  shows which backend is active + lets an admin non-destructively test an
  Azure SQL config (server + database + credentials → `SELECT 1` in a
  throwaway engine, never touches the running one) and copy a
  ready-to-paste `DATABASE_URL` for Azure App Service → Application
  Settings. The container image bundles Microsoft's ODBC Driver 18 for
  SQL Server, so `mssql+pyodbc://` connection strings work out of the box.
- **One-click automated cutover.** On Azure App Service, the site carries a
  System-Assigned Managed Identity (Bicep `identity: SystemAssigned` +
  a self-scoped "Website Contributor" role assignment) and can switch its
  own `DATABASE_URL` and restart itself via the ARM REST API — no manual
  Portal copy-paste. It authenticates to Azure via App Service's own
  local Managed Identity token endpoint (`IDENTITY_ENDPOINT` /
  `IDENTITY_HEADER`, not the VM-only Instance Metadata Service), so no
  credential is ever stored. The switch always fetches the *full*
  current app-settings collection and merges in the new `DATABASE_URL`
  before writing back — never a blind overwrite — so `FERNET_KEY` and
  every other secret survive untouched. Deployments that predate this
  feature need a one-time `az cli` bootstrap (shown inline on
  `/settings/database` — assigns the identity + role once).

### AI / LLM integration

- **Five providers**, one primitive. Chat() call is provider-agnostic;
  supports OpenAI, Azure OpenAI, Google Gemini, Anthropic Claude and
  Ollama (self-hosted). Adding a new provider is a ~30-line function.
- **Model discovery.** After picking a provider and entering the API
  key, the ↻ *list* button queries the provider's own `/v1/models` (or
  equivalent) and populates an autocomplete datalist — no more typing a
  model ID from memory.
- **Fernet-encrypted API keys.** Never plaintext in the DB.
- **Test-connection button** fires a one-word chat and reports the
  provider on success. Every provider failure lands as a readable error
  message on the settings page (no more raw 500s from a wrong model
  name).
- **Savings potential report** (`/customers/{id}/savings`). Every number
  — total spend, price coverage, same-SKU price variance across a
  customer's printers, customer overrides priced above the shared supply
  library — is computed straight from order history and stored pricing,
  never from the LLM. The LLM only phrases those exact numbers into a
  short paragraph for a sales conversation; if it's not configured the
  page still shows the full numbers, just without the narrative.
- **"Not configured" vs. "configured but failed" are shown separately.**
  Every AI-narrative surface (Reports, savings report) tells the two
  apart instead of using one generic message for both — if the provider
  is set up but the actual call errors out (bad key, rate limit,
  timeout, model rejected the prompt), the page shows that real error
  text inline rather than the misleading "AI isn't configured".

### Microsoft 365 Copilot Connector

- **Push printer fleet into the Semantic Index.** Copilot for M365 can
  answer questions like *"which printers at Acme are critical?"* or
  *"what's the serial of the marketing MFP?"* without opening TonerWatch.
- **Graph external items** — nine indexed fields (printer name, customer,
  model, location, serial, group, worst severity, asset tag, vendor) plus
  a natural-language content blurb for full-text search.
- **Scheduler + manual sync** — daily by default (configurable), or one
  click *Sync now*.

### Internationalization

- **Six languages.** English, French, Italian, German, Spanish and Dutch —
  browser language auto-detected, user-switchable at any time.
- **Boot-time completeness gate.** 898+ keys × 6 languages, verified at
  startup — a missing key fails the boot loudly instead of silently
  falling back to English.

### UI polish

- **Brand-compliant.** Red Hat Display, Tungsten Printix palette, Print &
  Workplace pillar gradient, rhombus signature. Same tokens across every
  page.
- **Matrix-rain loading overlay.** Splash on login/setup, transparent
  overlay between page navigations — session-storage hand-off means no
  white flash between pages.
- **Left sidebar navigation.** Grouped by function; collapsed on mobile.

### Data model

- **Dialect-neutral SQLAlchemy Core.** Same codebase runs against SQLite
  (default, everything in `/data/tonerwatch.sqlite`) or MSSQL / Azure SQL
  Database — switch via `DATABASE_URL`.
- **Alembic migrations.** Every schema change is one revision on top of
  the last; upgrades run automatically on boot.
- **Fernet-encrypted secrets.** BI-DB passwords, mail credentials, Azure
  Blob connection string, Entra client secret, LLM API keys — all
  encrypted at rest with a per-instance key.

---

## Deployment

### Azure App Service (recommended)

Click the **Deploy to Azure** button above. The Bicep template provisions:

- an App Service Plan (Linux, B1 by default)
- a Web App running the multi-arch container from GHCR
- an Azure File share mounted at `/data` for the SQLite database + Fernet key
- application settings pre-wired for the container

Alternatively, deploy manually from a shell:

```bash
az deployment group create \
  --resource-group my-tonerwatch-rg \
  --template-file deploy/azure/main.bicep \
  --parameters appName=my-tonerwatch
```

See [`deploy/azure/README.md`](deploy/azure/README.md) for parameter details.

### Docker (self-hosted)

```bash
docker run -d --name tonerwatch \
  -p 8080:8080 \
  -v tonerwatch-data:/data \
  ghcr.io/mnimtz/printix-tonerwatch:latest
```

Open <http://localhost:8080>, complete the first-run setup wizard, and add
your first Printix customer.

### Docker Compose (local development)

```bash
docker compose up -d
```

## Local development (Python)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

mkdir -p ./data
export DB_PATH=./data/tonerwatch.sqlite
export FERNET_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export SESSION_HTTPS_ONLY=false   # only for plain-HTTP localhost

uvicorn src.server:app --reload --port 8080
```

## Configuration

All configuration is via environment variables — no configuration files. Any
integration secret (mail, backup, SSO, LLM, Copilot) is entered once in the
Settings UI and stored Fernet-encrypted in the DB.

| Variable | Default | Purpose |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Bind address |
| `WEB_PORT` | `8080` | Bind port |
| `DATABASE_URL` | *(unset — falls back to `sqlite:///${DB_PATH}`)* | SQLAlchemy URL. Set to `mssql+pymssql://user:pass@server:1433/db` for Azure SQL Database. |
| `DB_PATH` | `/data/tonerwatch.sqlite` | SQLite database path used when `DATABASE_URL` is unset. |
| `FERNET_KEY` | auto-generated on first start | Encryption key for all secrets at rest. |
| `FERNET_KEY_FILE` | *(unset)* | Path to the Fernet key file if you'd rather mount it than pass it as an env var. |
| `SESSION_SECRET` | derived from `FERNET_KEY` | Signing key for session cookies. |
| `SESSION_HTTPS_ONLY` | `true` | Set to `false` for local plain-HTTP development. |
| `DEFAULT_LANG` | `en` | Fallback UI language when the browser preference cannot be resolved. |
| `ALERT_INTERVAL_MINUTES` | `15` | Cadence of the toner alert evaluation runner. |
| `REFRESH_INTERVAL_MINUTES` | `5` | Cadence of the background BI-cache warmer. |
| `PUBLIC_BASE_URL` | *(unset)* | Absolute URL of your TonerWatch instance (e.g. `https://tonerwatch.example.com`). Used to build clickable magic links in alert mails and Copilot Connector items. |
| `SKIP_ALERT_RUNNER` | `0` | Set to `1` to disable the scheduler entirely (useful for local dev). |

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data model,
request lifecycle and deployment topology.

## Release process

1. Bump `VERSION` (semver — patch for fixes, minor for features, major for
   breaking changes)
2. Commit + tag: `git tag v0.14.1 && git push --follow-tags`
3. GitHub Actions builds and publishes the multi-arch container to GHCR
4. In Azure, restart the App Service to pick up the new `:latest` image
   (or configure webhook-based auto-pull)

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

Printix TonerWatch is an independent tool for the Tungsten Printix
ecosystem and is not an official product of Tungsten Automation
Corporation. Tungsten Automation®, Tungsten Printix™ and the Tungsten
Automation logo are trademarks of Tungsten Automation Corporation.
