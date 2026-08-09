"""Add plan/approval columns to tasks for the P0-D approval gate.

A task carries an optional structured plan artifact (JSONB), whether that
plan touches files or destructive steps (and so needs human approval), and
the approval decision. Execution refuses to start while ``plan_needs_approval``
is set and ``plan_approved`` is still undecided.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the plan and approval columns to ``tasks``."""
    op.add_column("tasks", sa.Column("plan", JSONB(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("plan_needs_approval", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("tasks", sa.Column("plan_approved", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop the plan and approval columns from ``tasks``."""
    op.drop_column("tasks", "plan_approved")
    op.drop_column("tasks", "plan_needs_approval")
    op.drop_column("tasks", "plan")
