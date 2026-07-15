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
  with its BI-database credentials; the shared runtime queries them all on
  a schedule and rolls the data up into cross-customer views.
- **Two views on the same data.** Card-based grid for quick triage or a
  sortable list view for spreadsheet-style scanning. Toggle persists per
  user in session.
- **Human-friendly status badges.** Bare SNMP codes (`NO_PAPER`,
  `MARKER_SUPPLY_LOW`, `DOOR_OPEN`, …) rendered as colored pill badges
  with icons and translated captions; unknown codes fall back to the raw
  code, so a new firmware state still shows up sensibly.
- **Live-look-alike, cache-fast.** Every render reads from a background
  cache that a scheduler warms every 5 minutes — dashboards load in under
  100 ms even when the BI DB is asleep.

### Alerting

- **Threshold-based per customer.** Warn + critical percentage configurable
  per customer.
- **Digest mode + quiet hours.** Group per-customer transitions into one
  mail; hold notifications during a configurable window.
- **Rich HTML mail with one-click ordering.** Every critical/warn row
  carries the SKU + description + a green **🛒 Order now** button + a blue
  **✓ Mark as ordered** magic link (14-day-signed, single action, POST
  confirm to survive link-preview bots).
- **Alert runner** on APScheduler; default 15 min, configurable via
  `ALERT_INTERVAL_MINUTES`.

### Order flow (kanban)

- **Five-state state machine** — draft → ordered → delivered → installed
  (or cancelled from any active state).
- **Auto-drafts on alert.** The runner creates one draft per
  (customer, printer, colour) slot; idempotent, so a stuck printer doesn't
  spawn a new draft every tick.
- **Three-column kanban** on `/orders` — Draft / Ordered / Delivered plus a
  collapsible "recently closed" section. Every card has a green **🛒 Order
  now** button (opens the supplier URL) and buttons for the next state
  transition.
- **Magic-link handlers.** `/orders/action/{token}` lets an email
  recipient act on an order without logging in — landing page requires a
  POST confirm so mail-preview crawlers can't accidentally change state.

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

### Per-printer metadata overrides

- **Enrich BI with your own info.** `/toner/{customer}/{printer}/info`
  form for location override, serial-number override, asset tag, group
  name (for filter/grouping), contact e-mail, purchase date, warranty
  date, freeform notes.
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
  with per-(IP, email) login throttling.
- **Sign in with Microsoft.** OAuth2 authorization-code flow via MSAL.
  Auto-provisioning with optional email-domain restriction, default role
  for new users.
- **Role-based access.** Admins see every customer; technicians only see
  the customers they were explicitly granted access to (M:N table).

### Backup

- **One-shot ZIP download.** SQLite database (via SQLite's online backup
  API — safe under concurrent writes) + manifest.json + optional Fernet
  key. On MSSQL backends the ZIP is manifest-only with a pointer at
  Azure SQL native backup.
- **Scheduled Azure Blob upload.** Connection string Fernet-encrypted at
  rest; container auto-created; interval configurable in hours;
  last-upload timestamp + blob name (or error) shown in Settings.

### AI / LLM integration

- **Five providers**, one primitive. Chat() call is provider-agnostic;
  supports OpenAI, Azure OpenAI, Google Gemini, Anthropic Claude and
  Ollama (self-hosted). Adding a new provider is a ~30-line function.
- **Fernet-encrypted API keys.** Never plaintext in the DB.
- **Test-connection button** fires a one-word chat and reports the
  provider on success.

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

- **EFIGS from day one.** English, French, Italian, German and Spanish —
  browser language auto-detected, user-switchable at any time.
- **Boot-time completeness gate.** 448+ keys × 5 languages, verified at
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
