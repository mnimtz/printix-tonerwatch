"""Add printer_info table for per-device metadata overrides.

Lets operators enrich (or correct) what Printix BI knows about each
device: better location string, own serial number if BI didn't
populate one, grouping for filter/list views, asset-tag,
warranty dates, per-device contact e-mail, freeform notes.

Revision ID: 0003_printer_info
Revises: 0002_customer_sql_port
Create Date: 2026-07-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_printer_info"
down_revision: Union[str, None] = "0002_customer_sql_port"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "printer_info",
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("printer_id", sa.Text(), nullable=False),
        sa.Column("location_override", sa.Text(), nullable=False, server_default=""),
        sa.Column("serial_override",   sa.Text(), nullable=False, server_default=""),
        sa.Column("asset_tag",         sa.Text(), nullable=False, server_default=""),
        sa.Column("group_name",        sa.Text(), nullable=False, server_default=""),
        sa.Column("contact_email",     sa.Text(), nullable=False, server_default=""),
        sa.Column("purchased_at",      sa.Text(), nullable=False, server_default=""),
        sa.Column("warranty_until",    sa.Text(), nullable=False, server_default=""),
        sa.Column("notes",             sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at",        sa.Text(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("customer_id", "printer_id",
                                name="pk_printer_info"),
        sa.ForeignKeyConstraint(("customer_id",), ("customers.id",),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(("updated_by_user_id",), ("users.id",),
                                ondelete="SET NULL"),
    )
    op.create_index(
        "idx_printer_info_group",
        "printer_info",
        ["customer_id", "group_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_printer_info_group", table_name="printer_info")
    op.drop_table("printer_info")
