# -*- coding: utf-8 -*-
"""Workflows table.

Revision ID: 0005_workflows
Revises: 0004_sops
Create Date: 2026-10-08 00:00:00.000000

The table is new — workflows have only ever been stored on Redis — so
there is nothing to backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_workflows"
down_revision: Union[str, None] = "0004_sops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``workflows`` table."""
    op.create_table(
        "workflows",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        for column in ("created_at", "updated_at", "user_id"):
            batch_op.create_index(
                batch_op.f(f"ix_workflows_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    """Drop the ``workflows`` table."""
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        for column in ("user_id", "updated_at", "created_at"):
            batch_op.drop_index(batch_op.f(f"ix_workflows_{column}"))
    op.drop_table("workflows")
