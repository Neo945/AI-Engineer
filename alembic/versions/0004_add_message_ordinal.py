"""Add ordinal to messages for deterministic transcript ordering.

Messages persisted by a single task run share the same ``created_at``
(one flush per run), so transcript order must be tracked explicitly.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the ``ordinal`` column to ``messages``."""
    op.add_column(
        "messages",
        sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_messages_ordinal", "messages", ["ordinal"])


def downgrade() -> None:
    """Drop the ``ordinal`` column from ``messages``."""
    op.drop_index("ix_messages_ordinal", table_name="messages")
    op.drop_column("messages", "ordinal")
