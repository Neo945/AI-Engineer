"""Enable pgvector and pg_trgm extensions.

Revision ID: 0001
Revises:
Create Date: 2026-08-07

The retrieval and memory modules depend on these extensions. pgvector
provides the ``vector`` type for embeddings; pg_trgm gives trigram text
search over identifiers and comments.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enable the extensions required by the retrieval module."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    """Disable the extensions."""
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
