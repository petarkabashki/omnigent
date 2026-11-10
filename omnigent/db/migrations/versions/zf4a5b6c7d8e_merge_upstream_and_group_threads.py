"""Merge the upstream scheduled-task branch with the role and group-thread branch.

Revision ID: zf4a5b6c7d8e
Revises: 1ad1db53f99d, ze3f4a5b6c7d

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "zf4a5b6c7d8e"
down_revision: tuple[str, ...] = ("1ad1db53f99d", "ze3f4a5b6c7d")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
