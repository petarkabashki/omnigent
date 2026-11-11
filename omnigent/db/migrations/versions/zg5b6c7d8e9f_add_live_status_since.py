"""Record when a session's stored live status last changed.

Revision ID: zg5b6c7d8e9f
Revises: zf4a5b6c7d8e

Adds the nullable ``live_status_since`` column (epoch seconds) to
``omnigent_conversation_metadata``, beside ``live_status``, so every replica and
a restarted hub can tell how long a session has been running or waiting.

Additive with no default or backfill: existing rows read NULL until their next
status change, which is how they already read after a restart. Older servers
ignore the column. Downgrade drops it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zg5b6c7d8e9f"
down_revision: str | None = "zf4a5b6c7d8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"


def upgrade() -> None:
    """Add ``live_status_since``."""
    op.add_column(_TABLE, sa.Column("live_status_since", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove ``live_status_since``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("live_status_since")
