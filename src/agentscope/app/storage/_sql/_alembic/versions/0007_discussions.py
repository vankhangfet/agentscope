# -*- coding: utf-8 -*-
"""Discussions table.

Revision ID: 0007_discussions
Revises: 0006_workflow_runs
Create Date: 2026-10-16 00:00:00.000000

The table is new — discussions have never been persisted — so there
is nothing to backfill. ``status`` copies its field out of the
payload's ``data`` for status-filtered listing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_discussions"
down_revision: Union[str, None] = "0006_workflow_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``discussions`` table."""
    op.create_table(
        "discussions",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("discussions", schema=None) as batch_op:
        for column in (
            "created_at",
            "updated_at",
            "user_id",
            "status",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_discussions_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    """Drop the ``discussions`` table."""
    with op.batch_alter_table("discussions", schema=None) as batch_op:
        for column in (
            "status",
            "user_id",
            "updated_at",
            "created_at",
        ):
            batch_op.drop_index(batch_op.f(f"ix_discussions_{column}"))
    op.drop_table("discussions")
