"""Add attempt/max_attempts to tasks for retries and durable checkpoints.

Retries need a durable attempt counter on the task so a failed run can be
re-run within a bound, and the status/transcript combination acts as the
checkpoint a retry resumes from.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the retry metadata columns to ``tasks``."""
    op.add_column(
        "tasks",
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )


def downgrade() -> None:
    """Drop the retry metadata columns from ``tasks``."""
    op.drop_column("tasks", "max_attempts")
    op.drop_column("tasks", "attempt")
