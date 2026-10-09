# -*- coding: utf-8 -*-
"""Workflow runs table.

Revision ID: 0006_workflow_runs
Revises: 0005_workflows
Create Date: 2026-10-09 00:00:00.000000

The table is new — runs have only ever been kept in memory — so there
is nothing to backfill. ``workflow_id`` and ``status`` copy their
fields out of the payload's ``data`` for per-workflow listing and the
startup recovery sweep.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_workflow_runs"
down_revision: Union[str, None] = "0005_workflows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``workflow_runs`` table."""
    op.create_table(
        "workflow_runs",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        for column in (
            "created_at",
            "updated_at",
            "user_id",
            "workflow_id",
            "status",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_workflow_runs_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    """Drop the ``workflow_runs`` table."""
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        for column in (
            "status",
            "workflow_id",
            "user_id",
            "updated_at",
            "created_at",
        ):
            batch_op.drop_index(batch_op.f(f"ix_workflow_runs_{column}"))
    op.drop_table("workflow_runs")
