"""The saved views migration follows the live status change and round-trips."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from tests.db.test_migration_roles import _migrate, uri  # noqa: F401  (fixture)

PREVIOUS = "zg5b6c7d8e9f"
REVISION = "zh6c7d8e9f0a"


def test_follows_the_previous_head_under_a_single_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))

    (head,) = script.get_heads()
    assert REVISION in {revision.revision for revision in script.iterate_revisions(head, "base")}
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_and_downgrade_round_trip(uri: str) -> None:  # noqa: F811
    engine = sa.create_engine(uri)
    _migrate(uri, engine, command.upgrade, "head")

    inspector = sa.inspect(engine)
    assert "saved_views" in inspector.get_table_names()
    unique = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("saved_views")
        if index["unique"]
    }
    assert unique == {"uq_saved_views_owner_name": ["workspace_id", "owner_key", "name"]}

    _migrate(uri, engine, command.downgrade, PREVIOUS)
    assert "saved_views" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
