# Changelog

All notable changes to Printix TonerWatch are documented here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

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
