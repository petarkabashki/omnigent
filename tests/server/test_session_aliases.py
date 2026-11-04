"""Alias resolution reports duplicates as ambiguous instead of picking one."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_aliases import (
    check_alias_for_new_session,
    resolve_session_address,
    session_handle,
)
from omnigent.session_alias_client import resolve_address
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY, PROJECT_LABEL_KEY
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

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
    assert "namespace 'proj'" in missing.value.message
    store = _store(_session(SESSION_A, "Bench"), _session(SESSION_B, "bench"))
    with pytest.raises(OmnigentError) as ambiguous:
        _resolve(store, " proj /Bench")
    assert "project 'proj'" in ambiguous.value.message


# ── namespace tree resolution ─────────────────────────────────────────────

NS, SUB, OTHER_SUB, OTHER = ("1" * 32, "2" * 32, "3" * 32, "4" * 32)
LEGACY = "5" * 32


@pytest.fixture()
def tree(db_uri: str) -> SqlAlchemyProjectStore:
    """``ns`` > ``sub`` and ``other`` > ``sub``: two projects sharing a leaf name."""
    projects = SqlAlchemyProjectStore(db_uri)
    projects.create(NS, "ns", "u")
    projects.create(SUB, "sub", "u", parent_id=NS)
    projects.create(OTHER, "other", "u")
    projects.create(OTHER_SUB, "sub", "u", parent_id=OTHER)
    return projects


def _filed(session_id: str, alias: str, project_id: str | None = None, label: str | None = None):
    labels = {ALIAS_LABEL_KEY: alias}
    if label is not None:
        labels[PROJECT_LABEL_KEY] = label
    return SimpleNamespace(
        id=session_id, labels=labels, archived=False, title="t", project_id=project_id
    )


def _resolve_in(projects, sessions, address, source=None, label_projects=()):
    store = _store(*sessions)
    store.list_projects.return_value = list(label_projects)
    return resolve_session_address(store, projects, address=address, user_id="u", source=source)


def test_tree_path_resolves_only_the_exact_node(tree) -> None:
    mine = _filed(SESSION_A, "Product", SUB)
    elsewhere = _filed(SESSION_B, "Product", OTHER_SUB)
    assert _resolve_in(tree, [mine, elsewhere], "ns/sub/Product") == SESSION_A
    assert _resolve_in(tree, [mine, elsewhere], "OTHER/Sub/product") == SESSION_B
    with pytest.raises(OmnigentError) as caught:
        _resolve_in(tree, [mine, elsewhere], "sub/Product")
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_depth_one_tree_project_still_unions_label_members(tree) -> None:
    filed = _filed(SESSION_A, "Product", NS)
    labelled = _filed(SESSION_B, "Bench", label="ns")
    assert _resolve_in(tree, [filed, labelled], "ns/Product") == SESSION_A
    assert _resolve_in(tree, [filed, labelled], "ns/Bench") == SESSION_B
    # Labels hold the stored name, whatever case the address used.
    assert _resolve_in(tree, [filed, labelled], "NS/Bench") == SESSION_B


def test_legacy_root_with_slash_unions_its_label_members(tree, db_uri: str) -> None:
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(SqlProject(id=LEGACY, name="x/y", user_id="u", created_at=1))
        session.commit()
    source = _filed(SESSION_A, "Product", LEGACY)
    labelled = _filed(SESSION_B, "Bench", label="x/y")
    assert _resolve_in(tree, [source, labelled], "Bench", source=source) == SESSION_B


def test_nested_project_does_not_union_a_label_spelling_its_path(tree) -> None:
    source = _filed(SESSION_A, "Product", SUB)
    labelled = _filed(SESSION_B, "Bench", label="ns/sub")
    with pytest.raises(OmnigentError):
        _resolve_in(tree, [source, labelled], "Bench", source=source)


def test_bare_role_uses_the_source_namespace(tree) -> None:
    source = _filed(SESSION_B, "Architect", SUB)
    sibling = _filed(SESSION_A, "Product", SUB)
    elsewhere = _filed("6" * 32, "Product", OTHER_SUB)
    assert _resolve_in(tree, [sibling, elsewhere], "Product", source=source) == SESSION_A
    assert session_handle(sibling, tree, "u") == "ns/sub/Product"


def test_flat_legacy_project_resolves_by_exact_name(tree, db_uri: str) -> None:
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(SqlProject(id=LEGACY, name="x/y", user_id="u", created_at=1))
        session.commit()
    legacy = _filed(SESSION_A, "Product", LEGACY)
    assert _resolve_in(tree, [legacy, _filed(SESSION_B, "Product", SUB)], "x/y/Product") == (
        SESSION_A
    )
    assert session_handle(legacy, tree, "u") == "x/y/Product"


def test_label_project_resolves_and_excludes_filed_namesakes(tree) -> None:
    label = _filed(SESSION_A, "Product", label="sub")
    nested = _filed(SESSION_B, "Product", SUB)
    assert _resolve_in(tree, [label, nested], "sub/Product") == SESSION_A


def test_tree_and_flat_legacy_project_conflict(tree, db_uri: str) -> None:
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(SqlProject(id=LEGACY, name="ns/sub", user_id="u", created_at=1))
        session.commit()
    with pytest.raises(OmnigentError) as caught:
        _resolve_in(tree, [_filed(SESSION_A, "Product", SUB)], "ns/sub/Product")
    assert caught.value.code == ErrorCode.CONFLICT
    assert SUB in caught.value.message and LEGACY in caught.value.message
    assert "migrate-slashes --dry-run" in caught.value.message


def test_tree_and_label_project_conflict(tree) -> None:
    with pytest.raises(OmnigentError) as caught:
        _resolve_in(
            tree, [_filed(SESSION_A, "Product", SUB)], "ns/sub/Product", label_projects=["ns/sub"]
        )
    assert caught.value.code == ErrorCode.CONFLICT
    assert "label-project" in caught.value.message


def test_case_only_sibling_projects_conflict(tree) -> None:
    tree.create("7" * 32, "SUB", "u", parent_id=NS)
    with pytest.raises(OmnigentError) as caught:
        _resolve_in(tree, [_filed(SESSION_A, "Product", SUB)], "ns/sub/Product")
    assert caught.value.code == ErrorCode.CONFLICT
    assert "differ only by case" in caught.value.message


def test_alias_uniqueness_is_scoped_to_the_tree_node(tree) -> None:
    store = _store(_filed(SESSION_A, "Product", OTHER_SUB))
    store.list_projects.return_value = []
    assert (
        check_alias_for_new_session(store, tree, alias="Product", project_id=SUB, user_id="u")
        == "Product"
    )
    store = _store(_filed(SESSION_A, "Product", SUB))
    store.list_projects.return_value = []
    with pytest.raises(OmnigentError) as caught:
        check_alias_for_new_session(store, tree, alias="product", project_id=SUB, user_id="u")
    assert caught.value.code == ErrorCode.ALREADY_EXISTS


# ── CLI source namespace ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "sends_workspace"),
    [("Bench", True), ("/Bench", False), ("ns/Bench", False)],
)
def test_cli_sends_its_working_directory_only_for_a_bare_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str, sends_workspace: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    seen: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"session_id": SESSION_A})

    client = httpx.Client(transport=httpx.MockTransport(respond), base_url="http://server")

    assert resolve_address(client, address) == {"session_id": SESSION_A}

    assert seen["address"] == address
    assert seen.get("workspace") == (str(tmp_path.resolve()) if sends_workspace else None)
