"""The ``projects.parent_id`` migration round-trips, and refuses a lossy downgrade.

Runs on SQLite always and on ``OMNIGENT_TEST_DB_URI`` (Postgres/MySQL) when it is set.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_PREVIOUS_HEAD = "zb1c2d3e4f5a"
_BACKENDS = [
    "sqlite",
    pytest.param(
        "external",
        marks=pytest.mark.skipif(
            not os.environ.get("OMNIGENT_TEST_DB_URI"), reason="needs OMNIGENT_TEST_DB_URI"
        ),
    ),
]


@pytest.fixture
def uri(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[str]:
    """A fresh database URI: a SQLite file, or a throwaway database on the configured server."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'tree.db'}"
        return
    admin = sa.create_engine(os.environ["OMNIGENT_TEST_DB_URI"], isolation_level="AUTOCOMMIT")
    name = f"projects_tree_{uuid4().hex}"
    quoted = admin.dialect.identifier_preparer.quote(name)
    cascade = " CASCADE" if admin.dialect.name == "cockroachdb" else ""
    with admin.connect() as conn:
        conn.execute(sa.text(f"CREATE DATABASE {quoted}"))
    try:
        yield admin.url.set(database=name).render_as_string(hide_password=False)
    finally:
        clear_engine_cache()
        with admin.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE {quoted}{cascade}"))
        admin.dispose()


def _downgrade(engine: Engine, uri: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, _PREVIOUS_HEAD)


@pytest.mark.parametrize("uri", _BACKENDS, indirect=True)
def test_parent_id_migration_round_trip(uri: str) -> None:
    engine = get_or_create_engine(uri)
    store = SqlAlchemyProjectStore(uri)
    root = store.create("a" * 32, "root", None)
    child = store.create("b" * 32, "child", None, parent_id=root.id)

    _downgrade(engine, uri)
    inspector = sa.inspect(engine)
    assert "parent_id" not in {c["name"] for c in inspector.get_columns("projects")}
    assert "ix_projects_parent" not in {i["name"] for i in inspector.get_indexes("projects")}
    with engine.connect() as connection:
        names = connection.execute(sa.text("SELECT name FROM projects ORDER BY name")).scalars()
        assert list(names) == ["child", "root"]

    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    inspector = sa.inspect(engine)
    assert "parent_id" in {c["name"] for c in inspector.get_columns("projects")}
    assert "ix_projects_parent" in {i["name"] for i in inspector.get_indexes("projects")}
    # Downgrade flattened the tree: every row comes back as a root.
    assert store.get(child.id, user_id=None).parent_id is None
    store.update(child.id, user_id=None, parent_id=root.id)
    assert store.get_path(child.id, user_id=None) == "root/child"


@pytest.mark.parametrize("uri", _BACKENDS, indirect=True)
def test_downgrade_refuses_when_flattening_would_duplicate_names(uri: str) -> None:
    engine = get_or_create_engine(uri)
    store = SqlAlchemyProjectStore(uri)
    first = store.create("a" * 32, "a", None)
    second = store.create("b" * 32, "b", None)
    store.create("c" * 32, "sub", None, parent_id=first.id)
    store.create("d" * 32, "sub", None, parent_id=second.id)

    with pytest.raises(RuntimeError, match=r"'sub' x2"):
        _downgrade(engine, uri)
    # The refusal leaves the schema and the tree untouched.
    assert "parent_id" in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    assert store.get_path("c" * 32, user_id=None) == "a/sub"
