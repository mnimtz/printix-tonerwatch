# TonerWatch

**Multi-tenant toner monitoring, alerting and ordering for Managed Service Providers**
built on top of the Tungsten Printix BI database.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fmnimtz%2Ftonerwatch%2Fmain%2Fdeploy%2Fazure%2Fazuredeploy.json)
[![Container image](https://ghcr-badge.egpl.dev/mnimtz/tonerwatch/latest_tag?trim=major&label=ghcr.io)](https://github.com/mnimtz/tonerwatch/pkgs/container/tonerwatch)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

TonerWatch lets one MSP technician oversee toner status across
dozens of Printix customer tenants from a single console — with per-customer
thresholds, supply catalog entries per printer model, and end-to-end order
tracking that closes the loop between "low toner detected" and "cartridge
replaced".

---

## Highlights

- **One instance, many customers.** Each Printix customer is registered once
  with its BI-database credentials; the shared runtime queries them all on a
  schedule and rolls the data up into cross-customer dashboards.
- **Role-based access.** Administrators see every customer; technicians only
  see the customers explicitly assigned to them.
- **Supply library.** Configure the correct SKU, supplier and order quantity
  per printer model once — every printer of that model inherits the entry,
  and per-device overrides remain available for edge cases.
- **Actionable alerts.** Low-toner emails carry the exact SKU and a
  one-click *"mark as ordered"* magic link, so field techs never have to
  copy-paste part numbers from a spreadsheet.
- **Optional auto-drafts.** Per customer, alerts can pre-create a draft
  order in the Kanban board that a technician only needs to confirm.
- **Saved views.** Persistent filter presets — "critical, all customers",
  "site X, colour only", "orders overdue" — for the way each technician works.
- **Brand-aligned UI.** Fully compliant with the Tungsten Automation
  Brand Book: Red Hat Display, brand palette, rhombus frames, single
  Print &amp; Workplace product gradient.
- **EFIGS from day one.** English, French, Italian, German and Spanish —
  browser language auto-detected, user-switchable at any time.

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
  --resource-group my-radar-rg \
  --template-file deploy/azure/main.bicep \
  --parameters appName=my-radar
```

See [`deploy/azure/README.md`](deploy/azure/README.md) for parameter details.

### Docker (self-hosted)

```bash
docker run -d --name toner-radar \
  -p 8080:8080 \
  -v tonerwatch-data:/data \
  ghcr.io/mnimtz/tonerwatch:latest
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

uvicorn src.server:app --reload --port 8080
```

## Configuration

All configuration is via environment variables — no configuration files.

| Variable | Default | Purpose |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Bind address |
| `WEB_PORT` | `8080` | Bind port |
| `DB_PATH` | `/data/tonerwatch.sqlite` | SQLite database path |
| `FERNET_KEY` | auto-generated on first start | Encryption key for BI credentials |
| `SESSION_SECRET` | derived from `FERNET_KEY` | Signing key for session cookies |
| `DEFAULT_LANG` | `en` | Fallback UI language when browser preference cannot be resolved to a supported one |

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data model,
request lifecycle and deployment topology.

## Release process

1. Bump `VERSION` (semver — patch for fixes, minor for features, major for
   breaking changes)
2. Update `CHANGELOG.md`
3. Commit and tag: `git tag v0.1.1 && git push --follow-tags`
4. GitHub Actions builds and publishes the multi-arch container to GHCR
5. In Azure, restart the App Service to pick up the new image
   (or configure webhook-based auto-pull)

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

TonerWatch is an independent tool for the Tungsten Printix
ecosystem and is not an official product of Tungsten Automation
Corporation. Tungsten Automation®, Tungsten Printix™ and the Tungsten
Automation logo are trademarks of Tungsten Automation Corporation.
