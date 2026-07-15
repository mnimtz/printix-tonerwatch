"""Baseline schema — matches src/db.py :: metadata at v0.1.0.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from src import db as _db_module


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the entire schema in one shot from the metadata object.

    We deliberately don't hand-craft `op.create_table(...)` calls here —
    the single source of truth for the schema is `src/db.py :: metadata`.
    That way any dialect-specific quirks (partial indices, check
    constraints etc.) are compiled once by SQLAlchemy and generated
    correctly for both SQLite and MSSQL.
    """
    _db_module.metadata.create_all(op.get_bind())


def downgrade() -> None:
    _db_module.metadata.drop_all(op.get_bind())
