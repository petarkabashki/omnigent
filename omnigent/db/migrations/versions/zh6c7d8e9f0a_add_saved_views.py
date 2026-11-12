"""Add ``saved_views``.

Revision ID: zh6c7d8e9f0a
Revises: zg5b6c7d8e9f

Creates the table empty. Additive; the downgrade drops every saved view.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zh6c7d8e9f0a"
down_revision: str | None = "zg5b6c7d8e9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``saved_views`` and its owner-name unique index."""
    op.create_table(
        "saved_views",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("owner_key", sa.String(128), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("spec", sa.Text(), nullable=False),
        sa.Column("params", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("project_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_saved_views_owner_name",
        "saved_views",
        ["workspace_id", "owner_key", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Drop ``saved_views``."""
    op.drop_index("uq_saved_views_owner_name", table_name="saved_views")
    op.drop_table("saved_views")
