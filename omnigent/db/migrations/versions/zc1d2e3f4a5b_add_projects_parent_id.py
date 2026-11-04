"""Add ``projects.parent_id`` so projects form a namespace tree.

Revision ID: zc1d2e3f4a5b
Revises: zb1c2d3e4f5a

Adds a nullable ``parent_id`` (no foreign key, like the other id columns) and
the child-lookup index. Additive; existing rows stay roots and keep their names.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zc1d2e3f4a5b"
down_revision: str | None = "zb1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_projects_parent"


def upgrade() -> None:
    """Add ``parent_id`` and the child-lookup index."""
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    with op.batch_alter_table("projects") as batch:
        if "parent_id" not in existing:
            batch.add_column(sa.Column("parent_id", Uuid16(), nullable=True))
        batch.create_index(_INDEX, ["workspace_id", "user_id", "parent_id", "name"])


def downgrade() -> None:
    """Drop the index and ``parent_id`` (children flatten into roots).

    Older code needs one project per name per owner, so the downgrade refuses
    when flattening would create duplicate names.
    """
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT name, COUNT(*) FROM projects GROUP BY workspace_id, user_id, name "
            "HAVING COUNT(*) > 1 ORDER BY name"
        )
    ).all()
    if duplicates:
        listed = ", ".join(f"{name!r} x{count}" for name, count in duplicates)
        raise RuntimeError(
            "Cannot downgrade: flattening nested projects would leave several projects "
            f"with the same name for one owner ({listed}). Rename or delete them first."
        )
    with op.batch_alter_table("projects") as batch:
        batch.drop_index(_INDEX)
        batch.drop_column("parent_id")
