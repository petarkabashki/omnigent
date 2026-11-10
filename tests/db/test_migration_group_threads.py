"""The group threads migration chains from the roles tables, is the head and round-trips."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from tests.db.test_migration_roles import _migrate, uri  # noqa: F401  (fixture)

PREVIOUS = "zd2e3f4a5b6c"
REVISION = "ze3f4a5b6c7d"
TABLES = {"group_threads", "group_thread_members", "group_thread_posts"}


def test_is_the_single_head_after_the_roles_tables() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_and_downgrade_round_trip(uri: str) -> None:  # noqa: F811
    engine = sa.create_engine(uri)
    _migrate(uri, engine, command.upgrade, "head")

    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= TABLES
    unique = {
        index["name"]: index["column_names"]
        for table in ("group_thread_members", "group_thread_posts")
        for index in inspector.get_indexes(table)
        if index["unique"]
    }
    assert unique == {
        "uq_group_thread_members_ref": ["workspace_id", "thread_id", "kind", "ref"],
        "uq_group_thread_posts_seq": ["workspace_id", "thread_id", "seq"],
    }

    _migrate(uri, engine, command.downgrade, PREVIOUS)
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    engine.dispose()
    clear_engine_cache()
