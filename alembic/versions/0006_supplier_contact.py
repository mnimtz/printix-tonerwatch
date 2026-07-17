"""Supplier contact person + phone (global + per-customer override).

Lets an MSP record who to address the order email to ("Sehr geehrter
Herr Müller" instead of "Sehr geehrte Damen und Herren") and a phone
number to call for an urgent shortage — both optional, both blank
means the mail-draft feature falls back to the generic salutation and
no phone is shown anywhere. Mirrors the existing order_email /
order_email_override pattern: a global default on suppliers, an
optional per-customer override on customer_suppliers for the rare
case one customer has a different account contact at the same
distributor.

Revision ID: 0006_supplier_contact
Revises: 0005_suppliers
Create Date: 2026-07-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_supplier_contact"
down_revision: Union[str, None] = "0005_suppliers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("suppliers") as batch:
        batch.add_column(sa.Column(
            "contact_person", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column(
            "phone", sa.Text(), nullable=False, server_default=""))

    with op.batch_alter_table("customer_suppliers") as batch:
        batch.add_column(sa.Column(
            "contact_person_override", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column(
            "phone_override", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("customer_suppliers") as batch:
        batch.drop_column("phone_override")
        batch.drop_column("contact_person_override")

    with op.batch_alter_table("suppliers") as batch:
        batch.drop_column("phone")
        batch.drop_column("contact_person")
