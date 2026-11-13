"""Add ``projects.private``, the owner-set private-namespace flag.

Revision ID: zi7d8e9f0a1b
Revises: zh6c7d8e9f0a

Additive: a non-null boolean that defaults to false, so existing projects stay
non-private.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zi7d8e9f0a1b"
down_revision: str | None = "zh6c7d8e9f0a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``private`` with a false server default."""
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "private" in existing:
        return
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Drop ``private``."""
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("private")
