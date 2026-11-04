"""Add ``roles`` and ``role_events``.

Revision ID: zd2e3f4a5b6c
Revises: zc1d2e3f4a5b

Creates both tables empty: session labels can live on another database, so a
migration cannot backfill roles from them. Additive; the downgrade drops the
role history along with the rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zd2e3f4a5b6c"
down_revision: str | None = "zc1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``roles`` and ``role_events`` with their indexes."""
    op.create_table(
        "roles",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("project_id", Uuid16(), nullable=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("name_key", sa.String(32), nullable=False),
        # ``user_id or ''`` and ``project_id or ''``: NULLs would escape the name index.
        sa.Column("owner_key", sa.String(128), nullable=False),
        sa.Column("project_key", sa.String(32), nullable=False),
        sa.Column("overlay_ref", sa.String(64), nullable=True),
        # Opaque JSON stored compressed (CompressedText → LargeBinary).
        sa.Column("defaults", sa.LargeBinary(), nullable=True),
        sa.Column("permanent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("holder_session_id", Uuid16(), nullable=True),
        sa.Column("previous_session_id", Uuid16(), nullable=True),
        sa.Column("held_since", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_roles_name",
        "roles",
        ["workspace_id", "owner_key", "project_key", "name_key"],
        unique=True,
    )
    op.create_index("uq_roles_holder", "roles", ["workspace_id", "holder_session_id"], unique=True)

    op.create_table(
        "role_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("role_id", Uuid16(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        # Stable int codes (see omnigent.db.enum_codecs ROLE_EVENT_KIND/ACTOR).
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("prev_session_id", Uuid16(), nullable=True),
        sa.Column("actor", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.Column("role_name", sa.String(32), nullable=False),
        sa.Column("role_path", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN (1, 2, 3, 4, 5, 6)", name="ck_role_events_kind"),
        sa.CheckConstraint("actor IN (1, 2, 3, 4)", name="ck_role_events_actor"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_role_events_role", "role_events", ["workspace_id", "role_id", "generation"]
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("ix_role_events_role", table_name="role_events")
    op.drop_table("role_events")
    op.drop_index("uq_roles_holder", table_name="roles")
    op.drop_index("uq_roles_name", table_name="roles")
    op.drop_table("roles")
