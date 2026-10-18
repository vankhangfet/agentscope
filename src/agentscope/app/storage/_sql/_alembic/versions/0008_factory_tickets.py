# -*- coding: utf-8 -*-
"""Factory tickets table.

Revision ID: 0008_factory_tickets
Revises: 0007_discussions
Create Date: 2026-10-18 00:00:00.000000

The table is new — factory tickets have never been persisted — so
there is nothing to backfill. ``status`` copies its field out of the
payload's ``data`` for status-filtered listing and the boot sweep's
active query.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008_factory_tickets"
down_revision: Union[str, None] = "0007_discussions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``factory_tickets`` table."""
    op.create_table(
        "factory_tickets",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("factory_tickets", schema=None) as batch_op:
        for column in (
            "created_at",
            "updated_at",
            "user_id",
            "status",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_factory_tickets_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    """Drop the ``factory_tickets`` table."""
    with op.batch_alter_table("factory_tickets", schema=None) as batch_op:
        for column in (
            "status",
            "user_id",
            "updated_at",
            "created_at",
        ):
            batch_op.drop_index(batch_op.f(f"ix_factory_tickets_{column}"))
    op.drop_table("factory_tickets")
