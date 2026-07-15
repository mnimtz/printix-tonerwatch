"""Add customers.sql_port (default 1433) so operators can point at
Azure SQL failover endpoints or on-prem SQL Server on non-standard
ports without patching the code.

Revision ID: 0002_customer_sql_port
Revises: 0001_initial
Create Date: 2026-07-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002_customer_sql_port"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column(
            "sql_port", sa.Integer(), nullable=False, server_default="1433",
        ))


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("sql_port")
