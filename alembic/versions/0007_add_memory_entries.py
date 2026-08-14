"""Create memory_entries table for the memory subsystem.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14

The memory module (Phase 10) persists durable session/project/decision
memory per workspace. Each row is a typed entry (fact/decision/preference/
conversation) with optional pgvector embedding for semantic recall.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the memory_kind enum and the memory_entries table.

    The ``sa.Enum`` column below creates the ``memory_kind`` type as part of
    the table DDL, so no separate ``CREATE TYPE`` is issued.
    """
    memory_kind = sa.Enum(
        "fact",
        "decision",
        "preference",
        "conversation",
        name="memory_kind",
    )

    op.create_table(
        "memory_entries",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", memory_kind, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_memory_entries_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_entries"),
    )
    op.create_index(
        "ix_memory_entries_workspace_id",
        "memory_entries",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the memory_entries table and the memory_kind enum."""
    op.drop_index("ix_memory_entries_workspace_id", table_name="memory_entries")
    op.drop_table("memory_entries")
    op.execute("DROP TYPE IF EXISTS memory_kind")
