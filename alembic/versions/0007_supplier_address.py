"""Supplier postal address.

Global field on the supplier record — a delivery/billing address is
a property of the supplier itself, not something that varies per
managed customer (unlike order_email/contact_person/phone, which do
have a legitimate per-customer override).

Revision ID: 0007_supplier_address
Revises: 0006_supplier_contact
Create Date: 2026-07-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_supplier_address"
down_revision: Union[str, None] = "0006_supplier_contact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("suppliers") as batch:
        batch.add_column(sa.Column(
            "address", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("suppliers") as batch:
        batch.drop_column("address")
