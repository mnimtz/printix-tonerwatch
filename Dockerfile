# syntax=docker/dockerfile:1.7
# ============================================================================
# TonerWatch — multi-tenant toner monitoring for MSP partners
# ============================================================================
# Multi-stage build:
#   1. builder  — compile Python wheels (pyodbc, pymssql need dev headers)
#   2. runtime  — slim Debian-based image with the FreeTDS runtime for
#                 Microsoft SQL Server connectivity (Printix BI database)
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
        libodbc1 \
        freetds-bin \
        libfreetds-dev \
        tini \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

USER app
WORKDIR /app

COPY --from=builder --chown=app:app /root/.local /home/app/.local
COPY --chown=app:app src/          /app/src/
COPY --chown=app:app VERSION       /app/VERSION
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh

# Data volume: SQLite database + Fernet encryption key
VOLUME ["/data"]

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
