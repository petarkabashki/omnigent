"""Add ``group_threads``, ``group_thread_members`` and ``group_thread_posts``.

Revision ID: ze3f4a5b6c7d
Revises: zd2e3f4a5b6c

Creates the three tables empty. Additive; the downgrade drops every group
thread with its transcript.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ze3f4a5b6c7d"
down_revision: str | None = "zd2e3f4a5b6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the group thread tables and their indexes."""
    op.create_table(
        "group_threads",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("owner_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("hops", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hops_at", sa.Integer(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint("state IN (1, 2)", name="ck_group_threads_state"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_group_threads_owner", "group_threads", ["workspace_id", "owner_key", "created_at"]
    )
    op.create_table(
        "group_thread_members",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("thread_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("ref", sa.String(200), nullable=False),
        sa.Column("joined_at", sa.Integer(), nullable=False),
        sa.Column("left_at", sa.Integer(), nullable=True),
        sa.Column("delivered_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_to", Uuid16(), nullable=True),
        sa.CheckConstraint("kind IN (1, 2)", name="ck_group_thread_members_kind"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_group_thread_members_ref",
        "group_thread_members",
        ["workspace_id", "thread_id", "kind", "ref"],
        unique=True,
    )
    op.create_table(
        "group_thread_posts",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("thread_id", Uuid16(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("author_kind", sa.SmallInteger(), nullable=False),
        sa.Column("author_ref", sa.String(200), nullable=False),
        # Free text stored compressed (CompressedText → LargeBinary).
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("mentions", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("author_kind IN (1, 2, 3)", name="ck_group_thread_posts_author_kind"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_group_thread_posts_seq",
        "group_thread_posts",
        ["workspace_id", "thread_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the three tables."""
    op.drop_index("uq_group_thread_posts_seq", table_name="group_thread_posts")
    op.drop_table("group_thread_posts")
    op.drop_index("uq_group_thread_members_ref", table_name="group_thread_members")
    op.drop_table("group_thread_members")
    op.drop_index("ix_group_threads_owner", table_name="group_threads")
    op.drop_table("group_threads")
