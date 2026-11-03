"""Tests for the context-reset migration: item type 12 and ``context_epoch``."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_PREVIOUS = "d0a1f879a01d"
_REVISION = "zb1c2d3e4f5a"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


def _metadata_columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("omnigent_conversation_metadata")}


def test_upgrade_accepts_context_reset_and_downgrade_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'context-reset.db'}"
    engine = sa.create_engine(uri)
    _run(uri, engine, "upgrade", _PREVIOUS)
    assert "context_epoch" not in _metadata_columns(engine)

    _run(uri, engine, "upgrade", _REVISION)
    assert "context_epoch" in _metadata_columns(engine)
    checks = sa.inspect(engine).get_check_constraints("conversation_items")
    assert any("12" in str(check["sqltext"]) for check in checks)

    _run(uri, engine, "downgrade", _PREVIOUS)
    assert "context_epoch" not in _metadata_columns(engine)
    engine.dispose()
    clear_engine_cache()


def test_downgrade_refuses_while_reset_items_exist(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'context-reset-rows.db'}"
    engine = sa.create_engine(uri)
    _run(uri, engine, "upgrade", _REVISION)
    store = SqlAlchemyConversationStore(uri)
    conv = store.create_conversation()
    store.append_context_reset(conv.id, response_id="reset_1")

    with pytest.raises(RuntimeError, match="context_reset"):
        _run(uri, engine, "downgrade", _PREVIOUS)
    engine.dispose()
    clear_engine_cache()
