"""Toner-level history — delta-based readings + daily rollup.

toner_readings gets one row per (customer, printer, color) only when
the level actually changes (written from toner_alerts._upsert_state,
which already knows the previous value) — not one row per poll tick.
toner_readings_daily holds the compacted avg/min/max once a raw row
ages past the admin-configurable retention window (runner_config's
toner_history_raw_retention_days, default 90) — see toner_history.py.

Revision ID: 0009_toner_history
Revises: 0008_business_fields
Create Date: 2026-07-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_toner_history"
down_revision: Union[str, None] = "0008_business_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "toner_readings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("printer_id", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.Text(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(("customer_id",), ("customers.id",),
                                ondelete="CASCADE"),
    )
    op.create_index(
        "idx_toner_readings_slot_time", "toner_readings",
        ["customer_id", "printer_id", "color", "recorded_at"],
    )

    op.create_table(
        "toner_readings_daily",
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("printer_id", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column("avg_level", sa.Integer(), nullable=False),
        sa.Column("min_level", sa.Integer(), nullable=False),
        sa.Column("max_level", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("customer_id", "printer_id", "color", "date",
                                name="pk_toner_readings_daily"),
        sa.ForeignKeyConstraint(("customer_id",), ("customers.id",),
                                ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("toner_readings_daily")
    op.drop_index("idx_toner_readings_slot_time", table_name="toner_readings")
    op.drop_table("toner_readings")
