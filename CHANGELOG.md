# Changelog

All notable changes to Printix Toner Radar are documented here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

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
