"""Add ``role_lanes``, the named lanes of a role.

Revision ID: zj8e9f0a1b2c
Revises: zi7d8e9f0a1b

Creates the table empty with its two unique indexes. Additive: no existing
table changes, and the downgrade drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zj8e9f0a1b2c"
down_revision: str | None = "zi7d8e9f0a1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``role_lanes`` with its name and session indexes."""
    if "role_lanes" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "role_lanes",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("role_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=True),
        sa.Column("item", sa.String(32), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_role_lanes_name", "role_lanes", ["workspace_id", "role_id", "name"], unique=True
    )
    op.create_index(
        "uq_role_lanes_session", "role_lanes", ["workspace_id", "session_id"], unique=True
    )


def downgrade() -> None:
    """Drop ``role_lanes``."""
    op.drop_index("uq_role_lanes_session", table_name="role_lanes")
    op.drop_index("uq_role_lanes_name", table_name="role_lanes")
    op.drop_table("role_lanes")
