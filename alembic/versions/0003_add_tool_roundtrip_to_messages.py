"""Add tool_call_id and tool_calls to messages.

Tool interactions are persisted so the transcript can be faithfully rebuilt
for an LLM context window.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the tool round-trip columns to ``messages``."""
    op.add_column(
        "messages",
        sa.Column("tool_call_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the tool round-trip columns from ``messages``."""
    op.drop_column("messages", "tool_calls")
    op.drop_column("messages", "tool_call_id")
