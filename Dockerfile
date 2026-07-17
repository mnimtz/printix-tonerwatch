# syntax=docker/dockerfile:1.7
# ============================================================================
# Printix TonerWatch — multi-tenant toner monitoring for MSP partners
# ============================================================================
# Multi-stage build:
#   1. builder  — compile Python wheels (pyodbc, pymssql need dev headers)
#   2. runtime  — slim Debian-based image with two SEPARATE SQL Server
#                 drivers, for two separate features:
#                   * FreeTDS, used by pymssql (bi_client.py) to read a
#                     tenant's own Printix BI database — pymssql talks
#                     TDS directly, no unixODBC driver registration
#                     needed.
#                   * Microsoft's ODBC Driver 18, used by pyodbc
#                     (db.py's build_azure_sql_url) when TonerWatch's
#                     OWN backend storage is switched to Azure SQL —
#                     genuinely needs a registered unixODBC driver
#                     named "ODBC Driver 18 for SQL Server", which
#                     FreeTDS does not provide.
#
# Target platforms: linux/amd64, linux/arm64 (built via buildx in CI)
# ============================================================================

ARG PYTHON_VERSION=3.13

# ----------------------------------------------------------------------------
# Stage 1: builder
# ----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        unixodbc-dev \
        freetds-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-warn-script-location -r requirements.txt

# ----------------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/app/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        unixodbc \
        libsybdb5 \
        freetds-bin \
        tini \
        ca-certificates \
        curl \
        gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

# Microsoft's ODBC Driver 18 for SQL Server — separate apt repo, not in
# Debian's own. db.py's build_azure_sql_url() has always assumed this
# was present ("available in the container image"), but it never
# actually was: FreeTDS above covers pymssql (bi_client.py), not
# pyodbc. Left unnoticed until the Azure-SQL-as-TonerWatch's-own-
# backend feature (v0.23.0) got its first real test.
RUN curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list \
        | sed 's|deb |deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] |' \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

USER app
WORKDIR /app

COPY --from=builder --chown=app:app /root/.local /home/app/.local
COPY --chown=app:app src/          /app/src/
COPY --chown=app:app alembic/      /app/alembic/
COPY --chown=app:app alembic.ini   /app/alembic.ini
COPY --chown=app:app VERSION       /app/VERSION
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh

# Data volume: SQLite database + Fernet encryption key
VOLUME ["/data"]

EXPOSE 8080

# Liveness probe — the /healthz endpoint returns {"status":"ok"} once
# the FastAPI app is fully initialised (schema migrated, translations
# checked). Azure App Service uses this to gate traffic during rollouts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${WEB_PORT:-8080}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
