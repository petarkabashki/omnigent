"""The roles migration chains from the namespace tree and round-trips.

Runs on a throwaway SQLite file and, with ``OMNIGENT_TEST_DB_URI`` set, on a
throwaway database on that server. ``alembic upgrade head`` here is the call that
raises if the migration left two heads.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import exc as sa_exc

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

PREVIOUS = "zc1d2e3f4a5b"
REVISION = "zd2e3f4a5b6c"


@pytest.fixture(params=["sqlite", "server"])
def uri(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'roles.db'}"
        return
    base = os.environ.get("OMNIGENT_TEST_DB_URI", "")
    if not base:
        pytest.skip("OMNIGENT_TEST_DB_URI is not set")
    name = f"roles_migration_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        yield re.sub(r"/[^/]*(\?.*)?$", f"/{name}", base)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _migrate(uri: str, engine: sa.Engine, step, revision: str) -> None:
    # No outer transaction: some older revisions need an autocommit block on a server.
    config = _build_alembic_config(uri)
    with engine.connect() as conn:
        config.attributes["connection"] = conn
        step(config, revision)
        conn.commit()


def test_chains_from_the_namespace_tree_with_one_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_and_downgrade_round_trip(uri: str) -> None:
    engine = sa.create_engine(uri)
    _migrate(uri, engine, command.upgrade, "head")

    inspector = sa.inspect(engine)
    assert {"roles", "role_events"} <= set(inspector.get_table_names())
    assert inspector.get_pk_constraint("roles")["constrained_columns"] == ["workspace_id", "id"]
    unique = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("roles")
        if index["unique"]
    }
    assert unique == {
        "uq_roles_name": ["workspace_id", "owner_key", "project_key", "name_key"],
        "uq_roles_holder": ["workspace_id", "holder_session_id"],
    }
    assert not any(
        index.get("dialect_options", {}).get("postgresql_where")
        for index in inspector.get_indexes("roles")
    )
    assert [i["name"] for i in inspector.get_indexes("role_events")] == ["ix_role_events_role"]

    _migrate(uri, engine, command.downgrade, PREVIOUS)
    assert not {"roles", "role_events"} & set(sa.inspect(engine).get_table_names())
    _migrate(uri, engine, command.upgrade, "head")
    assert "roles" in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()


def test_event_codes_are_checked_by_the_database(uri: str) -> None:
    engine = sa.create_engine(uri)
    _migrate(uri, engine, command.upgrade, "head")

    with pytest.raises(sa_exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO role_events (id, role_id, generation, kind, actor, at, role_name, "
                "role_path) VALUES (:id, :id, 1, 99, 1, 0, 'x', '')"
            ),
            {"id": uuid.uuid4().bytes},
        )

    engine.dispose()
    clear_engine_cache()
