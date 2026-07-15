# Azure App Service deployment

This directory holds the Azure deployment assets for Tungsten Toner
Radar. Two equivalent templates are shipped: a Bicep source
(`main.bicep`) for hand-edited deployments and the compiled ARM JSON
(`azuredeploy.json`) that the "Deploy to Azure" button in the root
README uses.

## One-click deploy

Click the **Deploy to Azure** button in the root
[`README.md`](../../README.md). Azure Portal will prompt you for:

| Parameter | What to enter | Default |
|---|---|---|
| `appName` | Globally-unique DNS label (letters, digits and hyphens) | *required* |
| `location` | Azure region | Resource group's region |
| `sku` | App Service Plan SKU | `B1` |
| `containerImage` | Container image to pull | `ghcr.io/mnimtz/printix-toner-radar:latest` |
| `tz` | IANA timezone | `Europe/Berlin` |
| `defaultLang` | Fallback UI language (EFIGS) | `en` |

The template creates:

- an **App Service Plan** (Linux, SKU per parameter)
- a **Web App** running the container from GitHub Container Registry
- a **Storage Account** with an **Azure Files** share mounted at `/data`
  on the container — the share holds `toner_radar.sqlite` (the SQLite
  database) and `fernet.key` (the encryption key for BI-DB credentials)

Deployment takes about five minutes. Once it finishes, open the app URL
shown under **Outputs → appUrl**, complete the first-run setup wizard
to create the administrator account, and add your first Printix
customer under **Customers**.

## Manual deployment (CLI)

Prefer the CLI? From this directory:

```bash
# 1. Create the resource group (once)
az group create --name my-radar-rg --location westeurope

# 2. Deploy the template
az deployment group create \
  --resource-group my-radar-rg \
  --template-file main.bicep \
  --parameters appName=my-toner-radar sku=B1
```

Or use `azuredeploy.json` with `--template-file` if you don't have the
Bicep CLI installed. Both templates produce identical resources.

## SKU guidance

| SKU | Monthly (~EUR) | Always-on | Notes |
|---|---|---|---|
| `F1` | 0 | ❌ (sleeps after 20 min idle) | Free tier — fine for evaluation, not for production alerting because the runner sleeps too. |
| `B1` | ~10 | ✅ | **Recommended default.** 1.75 GB RAM, dedicated instance. Handles ~20 customers with room to spare. |
| `B2` / `B3` | 20-40 | ✅ | Step up for 50+ customers or high polling frequency. |
| `S1` | ~50 | ✅ | Enables custom domain + staging slots for blue-green deploys. |
| `P1V3` | ~100 | ✅ | Production workloads with SLAs — VNet integration, higher throughput. |

## Post-deployment configuration

1. **Set a stable session secret.** By default the session cookie
   signing key is derived from the auto-generated `FERNET_KEY`. To
   rotate the two independently, add an app setting:
   ```bash
   az webapp config appsettings set -g my-radar-rg -n my-toner-radar \
     --settings SESSION_SECRET="$(openssl rand -base64 48)"
   ```
2. **Configure central mail settings** inside the app, under
   **Settings → Notifications**, before adding customers with alert
   recipients configured.
3. **Register the customers** you want to monitor under
   **Customers → Add customer**, entering the Printix BI-database
   credentials (server / database / username / password). Use the
   *Test connection* button before saving.

## Backups

Everything the tool needs lives in the mounted Azure Files share.
Snapshot the storage account or copy the share content on a schedule
that matches your recovery objective. **Losing the Fernet key means
losing the ability to decrypt customer BI credentials** — always back
up `fernet.key` alongside `toner_radar.sqlite`.

## Updating

Push a new `v*.*.*` tag on the GitHub repository; the CI publishes a
fresh multi-arch image to GHCR. To pick it up, restart the App
Service:

```bash
az webapp restart -g my-radar-rg -n my-toner-radar
```

Or configure webhook-based auto-pulls under
**Deployment Center → Continuous deployment**.

## Rollback

Every published tag stays in GHCR indefinitely. To roll back, change
the `containerImage` app setting to a previous version tag and restart:

```bash
az webapp config container set -g my-radar-rg -n my-toner-radar \
  --docker-custom-image-name ghcr.io/mnimtz/printix-toner-radar:0.1.0
az webapp restart -g my-radar-rg -n my-toner-radar
```

The SQLite schema is additive — a rollback never loses data, though
newer columns will simply be unused by the older code.
