"""Add context resets: the context_reset item type and a per-session epoch.

Revision ID: zb1c2d3e4f5a
Revises: d0a1f879a01d

Adds ``context_epoch`` (NOT NULL, default 0) to
``omnigent_conversation_metadata`` and widens the ``conversation_items.type``
check to admit ``context_reset`` (code 12). Additive; no backfill. Downgrade
refuses while any ``context_reset`` item exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "zb1c2d3e4f5a"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTEXT_RESET = 12
_OLD_TYPE_CHECK = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)"
_NEW_TYPE_CHECK = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)"


def upgrade() -> None:
    """Add ``context_epoch`` and accept the ``context_reset`` item type."""
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "context_epoch" not in existing:
        op.add_column(
            "omnigent_conversation_metadata",
            sa.Column("context_epoch", sa.Integer(), nullable=False, server_default="0"),
        )
    with op.batch_alter_table("conversation_items") as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_conversation_items_type", type_="check")
        batch.create_check_constraint("ck_conversation_items_item_type", _NEW_TYPE_CHECK)


def downgrade() -> None:
    """Restore the narrower type check and drop ``context_epoch``."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check item types")
    items = sa.table("conversation_items", sa.column("type"))
    resets = sa.select(items.c.type).where(items.c.type == _CONTEXT_RESET).limit(1)
    if op.get_bind().execute(resets).first():
        raise RuntimeError("Delete context_reset items before downgrading")
    with op.batch_alter_table("conversation_items") as batch:
        batch.drop_constraint("ck_conversation_items_item_type", type_="check")
        batch.create_check_constraint("ck_conversation_items_type", _OLD_TYPE_CHECK)
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("context_epoch")
