"""Alias resolution reports duplicates as ambiguous instead of picking one."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_aliases import resolve_session_address
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY

SESSION_A = "a" * 32
SESSION_B = "b" * 32


def _session(session_id: str, alias: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id, labels={ALIAS_LABEL_KEY: alias}, archived=False, title="t"
    )


def _store(*sessions: SimpleNamespace) -> MagicMock:
    store = MagicMock()
    store.list_conversations.return_value = SimpleNamespace(data=list(sessions), has_more=False)
    return store


def _resolve(store: MagicMock, address: str) -> str:
    return resolve_session_address(store, None, address=address, user_id="u", source=None)


def test_single_match_resolves() -> None:
    assert _resolve(_store(_session(SESSION_A, "Product")), "product") == SESSION_A


def test_duplicate_alias_is_ambiguous_and_lists_candidates() -> None:
    store = _store(_session(SESSION_A, "Product"), _session(SESSION_B, "product"))
    with pytest.raises(OmnigentError) as caught:
        _resolve(store, "Product")
    assert caught.value.code == ErrorCode.CONFLICT
    assert "ambiguous" in caught.value.message
    assert SESSION_A in caught.value.message and SESSION_B in caught.value.message


def test_no_match_is_not_found() -> None:
    with pytest.raises(OmnigentError) as caught:
        _resolve(_store(), "Nobody")
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_errors_show_the_stripped_project_name() -> None:
    with pytest.raises(OmnigentError) as missing:
        _resolve(_store(), " proj /Nobody")
    assert "project 'proj'" in missing.value.message
    store = _store(_session(SESSION_A, "Bench"), _session(SESSION_B, "bench"))
    with pytest.raises(OmnigentError) as ambiguous:
        _resolve(store, " proj /Bench")
    assert "project 'proj'" in ambiguous.value.message
