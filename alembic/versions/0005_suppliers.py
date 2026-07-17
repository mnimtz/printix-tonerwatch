"""Suppliers — global vendor list + per-customer account details.

Lets an MSP model the distributors it actually orders from (Group24,
Bechtle, ...) once, then record the per-customer relationship — the
customer's own account/customer number with that supplier, and an
order-mailbox override for the rare case it differs from the
supplier's own default. supply_templates / printer_supplies get a
supplier_id so the order-mail draft feature can resolve the right
recipient + customer number automatically instead of the operator
typing them in every time.

Revision ID: 0005_suppliers
Revises: 0004_autonomous_orders
Create Date: 2026-07-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_suppliers"
down_revision: Union[str, None] = "0004_autonomous_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("order_email", sa.Text(), nullable=False, server_default=""),
        sa.Column("website_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Text(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(("updated_by_user_id",), ("users.id",),
                                ondelete="SET NULL"),
        sa.UniqueConstraint("name", name="uq_suppliers_name"),
    )

    op.create_table(
        "customer_suppliers",
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("customer_number", sa.Text(), nullable=False, server_default=""),
        sa.Column("order_email_override", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("customer_id", "supplier_id",
                                name="pk_customer_suppliers"),
        sa.ForeignKeyConstraint(("customer_id",), ("customers.id",),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(("supplier_id",), ("suppliers.id",),
                                ondelete="CASCADE"),
    )

    # SQLite batch mode needs every constraint it adds to be named (it
    # may have to recreate the table around it later), unlike a plain
    # metadata.create_all() which happily leaves inline FKs anonymous —
    # so these two get an explicit name unlike the rest of the schema.
    # Downgrade only needs drop_column() (batch mode rebuilds the whole
    # table without that column, taking the FK with it), so the name
    # never has to be referenced again.
    with op.batch_alter_table("supply_templates") as batch:
        batch.add_column(sa.Column(
            "supplier_id", sa.Integer(),
            sa.ForeignKey("suppliers.id", ondelete="SET NULL",
                          name="fk_supply_templates_supplier"),
            nullable=True))

    with op.batch_alter_table("printer_supplies") as batch:
        batch.add_column(sa.Column(
            "supplier_id", sa.Integer(),
            sa.ForeignKey("suppliers.id", ondelete="SET NULL",
                          name="fk_printer_supplies_supplier"),
            nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("printer_supplies") as batch:
        batch.drop_column("supplier_id")

    with op.batch_alter_table("supply_templates") as batch:
        batch.drop_column("supplier_id")

    op.drop_table("customer_suppliers")
    op.drop_table("suppliers")
