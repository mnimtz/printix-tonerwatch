"""Business fields for customers, printers, and orders.

customers: address + our own internal customer_number (billing/order
paperwork facts about the customer — distinct from
customer_suppliers.customer_number, which is this customer's account
number AT a given supplier).

printer_info: delivery_address (falls back to the customer's address
when empty — only needed when a device ships to a different site) +
contact_name (complements the existing contact_email with an actual
name for delivery/order paperwork).

toner_orders: updated_by_user_id + updated_at, so "who created this"
(ordered_by_user_id, no longer overwritten by later transitions) and
"who last moved it" are two separate, both-visible facts.

Revision ID: 0008_business_fields
Revises: 0007_supplier_address
Create Date: 2026-07-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_business_fields"
down_revision: Union[str, None] = "0007_supplier_address"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column(
            "address", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column(
            "customer_number", sa.Text(), nullable=False, server_default=""))

    with op.batch_alter_table("printer_info") as batch:
        batch.add_column(sa.Column(
            "delivery_address", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column(
            "contact_name", sa.Text(), nullable=False, server_default=""))

    with op.batch_alter_table("toner_orders") as batch:
        # v0.24.32 fix: SQLite's batch recreate raises "Constraint must
        # have a name" for an unnamed FK added via add_column — this
        # broke the migration outright (site down, "Application
        # Error") until the constraint got an explicit name. Confirmed
        # locally against a reconstructed pre-0008 schema, including
        # that the partial unique index (uq_active_toner_order)
        # survives the table recreate correctly either way.
        batch.add_column(sa.Column(
            "updated_by_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL",
                          name="fk_toner_orders_updated_by_user_id"),
            nullable=True))
        batch.add_column(sa.Column(
            "updated_at", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("toner_orders") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("updated_by_user_id")

    with op.batch_alter_table("printer_info") as batch:
        batch.drop_column("contact_name")
        batch.drop_column("delivery_address")

    with op.batch_alter_table("customers") as batch:
        batch.drop_column("customer_number")
        batch.drop_column("address")
