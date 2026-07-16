"""v0.20.0 — autonomous ordering mode + daily cap.

Extends the customer table so an admin can opt in to fully
autonomous ordering (draft → ordered → supplier email) with a
per-tenant daily cap that limits the blast radius if the toner
alert runner ever spirals.

Revision ID: 0004_autonomous_orders
Revises: 0003_printer_info
Create Date: 2026-07-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_autonomous_orders"
down_revision: Union[str, None] = "0003_printer_info"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(
            sa.Column("auto_order_daily_cap", sa.Integer(),
                      nullable=False, server_default="10"))
        # The existing CHECK constraint on auto_order_mode allowed
        # only 'off'/'draft'. Extend to include 'autonomous'.
        # SQLite recreates the whole table via batch_alter_table,
        # so we just drop + recreate the check constraint.
        batch.drop_constraint("ck_customers_auto_order_mode",
                              type_="check")
        batch.create_check_constraint(
            "ck_customers_auto_order_mode",
            "auto_order_mode IN ('off','draft','autonomous')")


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_constraint("ck_customers_auto_order_mode",
                              type_="check")
        batch.create_check_constraint(
            "ck_customers_auto_order_mode",
            "auto_order_mode IN ('off','draft')")
        batch.drop_column("auto_order_daily_cap")
