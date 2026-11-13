"""The projects.private migration follows the saved views head and round-trips."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from tests.db.test_migration_roles import _migrate, uri  # noqa: F401  (fixture)

PREVIOUS = "zh6c7d8e9f0a"
REVISION = "zi7d8e9f0a1b"


def test_follows_the_previous_head_under_a_single_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))

    (head,) = script.get_heads()
    assert REVISION in {revision.revision for revision in script.iterate_revisions(head, "base")}
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_defaults_existing_projects_to_not_private(uri: str) -> None:  # noqa: F811
    engine = sa.create_engine(uri)
    _migrate(uri, engine, command.upgrade, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO projects (workspace_id, id, name, user_id, created_at) "
                "VALUES ('default', X'00000000000000000000000000000001', 'p', 'u', 1)"
            )
        )

    _migrate(uri, engine, command.upgrade, REVISION)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT private FROM projects")).scalar_one() == 0

    _migrate(uri, engine, command.downgrade, PREVIOUS)
    columns = {column["name"] for column in sa.inspect(engine).get_columns("projects")}
    assert "private" not in columns

    engine.dispose()
    clear_engine_cache()
