# Changelog

All notable changes to Printix TonerWatch are documented here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

### Added
- **P1 — Customer management + M:N user↔customer access**. New
  `/customers` menu (list, create, edit, detail, deactivate) with
  Fernet-encrypted BI-database password and an inline
  "Test connection" button that actually opens a MSSQL socket to the
  supplied credentials before saving. New `/users` menu (admin only)
  for MSP technicians and administrators, and a dedicated
  `/users/{id}/access` matrix that governs which customers each
  technician can see. Route-level `require_admin` and
  `require_customer_access` guards enforce the model at the HTTP
  layer, and every state change (`customer.created`,
  `customer.updated`, `customer.deactivated`, `user.created`,
  `user.updated`, `user.access_updated`) is captured in the audit log
  with the acting user id and a JSON payload.
- 88 new i18n keys × 5 languages for the P1 UI (customer, user,
  access namespaces). The boot-time translation gate confirms every
  key is present in every language.
- **SQLAlchemy Core + Alembic** — the whole database layer is now
  dialect-neutral. `DATABASE_URL` picks the backend at startup:
  `sqlite:///data/tonerwatch.sqlite` (default) or
  `mssql+pymssql://user:pass@server:1433/db` (Azure SQL Database, for
  operators who prefer external managed storage over the mounted
  Azure File share). On fresh install the metadata is materialised
  and the Alembic head is stamped; on every subsequent boot
  `alembic upgrade head` applies pending migrations transactionally.
- Post-boot the container will crash-loop with a clear error if the
  Alembic env cannot import the metadata module, so schema drift
  surfaces immediately rather than silently.
- Security headers middleware — `Content-Security-Policy`,
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy` on every response.
- Very small in-process login throttle — per (client-ip, email) tuple,
  exponential back-off after 8 fails in a rolling 5 min window,
  capped at 30 s. Stops the trivial brute-force case without adding
  a Redis dependency.
- Docker `HEALTHCHECK` against `/healthz` — App Service liveness
  probes now gate traffic properly.

### Changed
- **Product name settled on "Printix TonerWatch — Print Supply
  Intelligence"** (short logo mark stays "TonerWatch"). Repo renamed on
  GitHub: `mnimtz/tonerwatch` → `mnimtz/printix-tonerwatch` (redirect
  from old URL in place). Container image now published to
  `ghcr.io/mnimtz/printix-tonerwatch`.
- Docker runtime stage no longer references the non-existent
  Debian-bookworm package `libfreetds-dev` — replaced with `libsybdb5`
  (actual FreeTDS runtime shared library used by pymssql). Also drops
  the redundant `libodbc1` (pulled in transitively by `unixodbc`).
  This fixes the CI build that broke immediately after v0.1.0 was
  tagged.
- **Earlier this session**: rebranded from "Printix Toner Radar" to
  "TonerWatch — Print Supply Intelligence". Repo renamed on GitHub (redirect from the old URL is
  in place). Container image now published to
  `ghcr.io/mnimtz/printix-tonerwatch`. Default SQLite database file is now
  `/data/tonerwatch.sqlite` (previously `/data/toner_radar.sqlite`) —
  operators upgrading from v0.1.0 must rename the file inside their
  `/data` volume before the container next starts.
- Login and first-run setup screens now use the full product logo
  (icon + wordmark + "Print Supply Intelligence" tagline) instead of
  the icon-plus-CSS-wordmark combination.
- Sidebar wordmark reads "TonerWatch / Print Supply Intelligence".

### Added
- Product icon and full logo shipped as PNG assets under
  `src/web/assets/` (256×256 icon with transparent background,
  800×400 logo, 32×32 favicon).

## [0.1.0] — 2026-07-15

Initial skeleton release.

### Added
- Multi-tenant customer management (schema stub, UI pending in P1)
- Session-based authentication with bcrypt password hashing
- First-run setup wizard for the initial admin account
- Fernet-encrypted storage for customer BI-database credentials
- EFIGS internationalization (English, French, Italian, German, Spanish)
- Browser language auto-detection with per-session override
- Tungsten Automation brand-aligned UI (Red Hat Display, brand palette,
  rhombus frames, blue-domain gradient, Print & Workplace product gradient)
- SQLite backend with automatic schema migrations
- Docker container (multi-arch: linux/amd64, linux/arm64)
- Azure App Service one-click deployment via Bicep template
- GitHub Actions workflow: multi-arch container publish to GHCR on `v*.*.*` tag
