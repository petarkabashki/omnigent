"""Saved view entity — persisted in the ``saved_views`` table.

A :class:`SavedView` is one owner's named view spec with declared params. Data
always resolves as the viewer, so a view holds a query, never rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SavedView:
    """
    A saved view.

    :param id: UUID primary key (bare 32-char hex).
    :param user_id: Owner, or ``None`` in single-user mode.
    :param name: Unique per owner, e.g. ``"Stuck sessions"``.
    :param description: Free text shown in the views list.
    :param spec: Spec v1 object; source params may hold ``"$name"`` placeholders.
    :param params: Declared params, e.g. ``{"host": {"type": "string"}}``.
    :param project_id: Project the view is filed under, or ``None``.
    :param created_at: Unix epoch seconds.
    :param updated_at: Unix epoch seconds of the last update, or ``None``.
    """

    id: str
    user_id: str | None
    name: str
    description: str
    spec: dict[str, Any]
    params: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    created_at: int = 0
    updated_at: int | None = None
