"""Create core schema: users, workspaces, sessions, tasks, messages, code_chunks.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the domain tables and native enum types."""
    session_status = sa.Enum(
        "idle",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
        name="session_status",
    )
    task_status = sa.Enum(
        "pending",
        "planning",
        "running",
        "reviewing",
        "testing",
        "completed",
        "failed",
        "cancelled",
        name="task_status",
    )
    message_role = sa.Enum(
        "system",
        "user",
        "assistant",
        "tool",
        name="message_role",
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "is_superuser",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "workspaces",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("repo_url", sa.String(length=2000), nullable=True),
        sa.Column("repo_path", sa.String(length=500), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
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
            ["owner_id"],
            ["users.id"],
            name="fk_workspaces_owner_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
    )
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", session_status, server_default="idle", nullable=False),
        sa.Column(
            "meta",
            sa.dialects.postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            name="fk_sessions_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_workspace_id", "sessions", ["workspace_id"])
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "tasks",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_type", sa.String(length=50), nullable=False),
        sa.Column("status", task_status, server_default="pending", nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            ["parent_task_id"],
            ["tasks.id"],
            name="fk_tasks_parent_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_tasks_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
    op.create_index("ix_tasks_session_id", "tasks", ["session_id"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_messages_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_messages_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    op.create_index("ix_messages_task_id", "messages", ["task_id"])

    op.create_table(
        "code_chunks",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=2000), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "meta",
            sa.dialects.postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_code_chunks_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_code_chunks"),
    )
    op.create_index("ix_code_chunks_workspace_id", "code_chunks", ["workspace_id"])
    op.create_index(
        "ix_code_chunks_workspace_file",
        "code_chunks",
        ["workspace_id", "file_path"],
    )


def downgrade() -> None:
    """Drop the domain tables and native enum types."""
    op.drop_table("code_chunks")
    op.drop_table("messages")
    op.drop_table("tasks")
    op.drop_table("sessions")
    op.drop_table("workspaces")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS message_role")
    op.execute("DROP TYPE IF EXISTS task_status")
    op.execute("DROP TYPE IF EXISTS session_status")
