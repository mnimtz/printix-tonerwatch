"""Per-user grant flag for the Printix Mandanten nav section.

Idempotent add_column — no FK/constraint here (learned from the
0008 migration incident: only add_column calls that carry an inline
constraint need that treatment), but the existence guard costs
nothing and keeps this safe to re-run.

Revision ID: 0010_printix_tenants_access
Revises: 0009_toner_history
Create Date: 2026-07-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_printix_tenants_access"
down_revision: Union[str, None] = "0009_toner_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "printix_tenants_access" not in cols:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "printix_tenants_access", sa.Integer(),
                nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("printix_tenants_access")
