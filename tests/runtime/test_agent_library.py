"""Tests for reading roles and skills from the shared agent library's resolve output."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runtime import agent_library
from omnigent.runtime.agent_library import parse_resolution, resolve_library
from omnigent.runtime.role_instructions import apply_session_role, load_role_overlay
from omnigent.spec.types import AgentSpec, ExecutorSpec, SkillSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_library"
LIBRARY_BODY = (
    "You are this project's Architect.\n\nIn this project you also own the CLI's resolve format."
)

_FAKE_CLI = """\
import json, pathlib, sys, time
here = pathlib.Path(sys.argv[1])
with open(here / "calls.log", "a") as log:
    log.write(" ".join(sys.argv[2:]) + "\\n")
settings = json.loads((here / "settings.json").read_text())
time.sleep(settings.get("sleep", 0))
sys.stdout.write((here / "resolve.json").read_text())
sys.exit(settings.get("exit", 0))
"""


@pytest.fixture(autouse=True)
def _fresh_library_state() -> Iterator[None]:
    agent_library.clear_cache()
    yield
    agent_library.clear_cache()


class Library:
    """The design fixture copied to a temp dir, served by a fake resolve command."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = tmp_path / "library"
        self.project = tmp_path / "project"
        shutil.copytree(FIXTURE / "library", self.root)
        shutil.copytree(FIXTURE / "project", self.project)
        (self.project / ".git").mkdir()
        self.cli_dir = tmp_path / "cli"
        self.cli_dir.mkdir()
        (self.cli_dir / "fake_cli.py").write_text(_FAKE_CLI)
        text = (FIXTURE / "expected-resolve.json").read_text()
        text = text.replace("{library}", str(self.root)).replace("{project}", str(self.project))
        self.document = json.loads(text)
        self.write(self.document)
        self.settings()
        self.command = [sys.executable, str(self.cli_dir / "fake_cli.py"), str(self.cli_dir)]
        config_home = tmp_path / "config"
        config_home.mkdir()
        (config_home / "config.yaml").write_text(
            json.dumps({"agent_library": {"command": self.command}})
        )
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    def write(self, document: dict[str, object]) -> None:
        (self.cli_dir / "resolve.json").write_text(json.dumps(document))

    def settings(self, **values: object) -> None:
        (self.cli_dir / "settings.json").write_text(json.dumps(values))

    def calls(self) -> list[str]:
        log = self.cli_dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    return Library(tmp_path, monkeypatch)


def _write_role(root: Path, name: str, text: str) -> Path:
    path = root / ".omnigent" / "roles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _spec(skills: list[SkillSpec] | None = None) -> AgentSpec:
    return AgentSpec(spec_version=1, name="a", executor=ExecutorSpec(), skills=skills or [])


def _architect(library: Library) -> str:
    overlay = load_role_overlay("Architect", str(library.project), home=library.home)
    assert overlay is not None and overlay.instructions is not None
    return overlay.instructions


def test_without_a_project_file_or_a_command_roles_and_skills_are_unchanged(
    library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_role(library.home, "Architect.md", "home text")
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    overlay = load_role_overlay("Architect", str(plain), home=library.home)
    assert overlay is not None and overlay.instructions is not None
    assert overlay.instructions.endswith("home text")
    assert apply_session_role(_spec(), {}, str(plain)).skills == []
    assert library.calls() == []

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "no-config"))
    assert _architect(library).endswith("home text")
    assert apply_session_role(_spec(), {}, str(library.project)).skills == []
    assert library.calls() == []


def test_parses_the_design_fixture_and_ignores_unknown_keys(library: Library) -> None:
    document = dict(library.document, future_key={"x": 1})
    document["assets"]["roles"][0]["future_field"] = "ignored"  # type: ignore[index]
    resolution = parse_resolution(document)
    assert resolution is not None
    assert resolution.library_root == library.root
    assert resolution.project_root == library.project
    role = resolution.role("architect")
    assert role is not None and role.body.strip() == LIBRARY_BODY
    assert [(s.name, s.layer) for s in resolution.skills] == [
        ("coding-style", "collection:python"),
        ("data-loading", "collection:research"),
        ("python-core", "collection:python"),
        ("release-notes", "project"),
    ]
    assert (
        parse_resolution(dict(document, scope={**document["scope"], "project_file": None})) is None
    )  # type: ignore[dict-item]


def test_a_workspace_inside_the_project_resolves_the_project_with_its_role(
    library: Library,
) -> None:
    subdir = library.project / "src"
    subdir.mkdir()
    assert resolve_library(subdir, "Architect") is not None
    assert library.calls() == [f"resolve --project {library.project} --json --role Architect"]


def test_role_precedence_is_workspace_then_library_then_home(library: Library) -> None:
    _write_role(library.home, "Architect.md", "home text")
    text = _architect(library)
    assert LIBRARY_BODY in text and "home text" not in text

    _write_role(library.project, "Architect.md", "workspace text")
    text = _architect(library)
    assert text.index(LIBRARY_BODY) < text.index("workspace text")
    assert "home text" not in text

    library.document["assets"]["roles"] = []  # type: ignore[index]
    library.write(library.document)
    os.utime(library.project / ".agents" / "library.yaml", ns=(1, 1))
    text = _architect(library)
    assert "home text" in text and LIBRARY_BODY not in text


def test_a_library_edit_reaches_the_next_session_and_unchanged_files_reuse_the_result(
    library: Library,
) -> None:
    assert LIBRARY_BODY in _architect(library)
    assert LIBRARY_BODY in _architect(library)
    assert len(library.calls()) == 1

    library.document["assets"]["roles"][0]["body"] = "edited body\n"  # type: ignore[index]
    library.write(library.document)
    role_file = library.root / "core" / "roles" / "Architect.md"
    os.utime(role_file, ns=(role_file.stat().st_mtime_ns + 10**9,) * 2)
    assert "edited body" in _architect(library)
    assert len(library.calls()) == 2


def test_library_skills_join_the_session_once_each(library: Library) -> None:
    own = SkillSpec(name="coding-style", description="the agent's own", content="x")
    spec = apply_session_role(_spec([own]), {"omnigent.alias": "Architect"}, str(library.project))
    by_name = {skill.name: skill for skill in spec.skills}
    assert sorted(by_name) == ["coding-style", "data-loading", "python-core", "release-notes"]
    assert by_name["coding-style"] is own
    assert (
        by_name["data-loading"].skill_dir
        == library.root / "collections/research/skills/data-loading"
    )
    assert library.calls()[-1].endswith("--role Architect")


def test_a_project_copy_outranks_a_collection_skill_but_a_link_to_it_does_not(
    library: Library,
) -> None:
    copy = library.project / ".claude" / "skills" / "python-core"
    shutil.copytree(library.root / "collections/python/skills/python-core", copy)
    linked = library.project / ".agents" / "skills" / "data-loading"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(library.root / "collections/research/skills/data-loading")

    names = [skill.name for skill in apply_session_role(_spec(), {}, str(library.project)).skills]
    assert names == ["coding-style", "data-loading", "release-notes"]


def test_exit_one_uses_the_partial_result_and_names_what_was_left_out(
    library: Library, caplog: pytest.LogCaptureFixture
) -> None:
    library.document["assets"]["skills"] = library.document["assets"]["skills"][:1]  # type: ignore[index]
    library.document["diagnostics"] = [
        {
            "level": "error",
            "code": "bad-front-matter",
            "type": "skills",
            "name": "python-core",
            "message": "unreadable",
        }
    ]
    library.write(library.document)
    library.settings(exit=1)
    with caplog.at_level(logging.WARNING, logger=agent_library.__name__):
        names = [s.name for s in apply_session_role(_spec(), {}, str(library.project)).skills]
    assert names == ["coding-style"]
    assert "error bad-front-matter skills/python-core" in caplog.text
    assert "unreadable" in caplog.text


def test_an_unknown_role_warning_keeps_the_assets_and_logs_once_per_cache_entry(
    library: Library, caplog: pytest.LogCaptureFixture
) -> None:
    library.document["scope"]["role"] = "Coordinator"  # type: ignore[index]
    library.document["diagnostics"] = [
        {
            "level": "warning",
            "code": "unknown-role",
            "type": "roles",
            "name": "Coordinator",
            "message": "no role named Coordinator",
        }
    ]
    library.write(library.document)
    labels = {"omnigent.alias": "Coordinator"}
    with caplog.at_level(logging.WARNING, logger=agent_library.__name__):
        for _ in range(2):
            spec = apply_session_role(_spec(), labels, str(library.project))
            assert len(spec.skills) == 4
    assert len(library.calls()) == 1
    logged = [r.getMessage() for r in caplog.records if r.name == agent_library.__name__]
    assert len(logged) == 1
    assert "warning unknown-role roles/Coordinator" in logged[0]
    assert "no role named Coordinator" in logged[0]


@pytest.mark.parametrize(
    "case",
    ["exit-2", "missing-command", "timeout", "format-version-2", "not-json"],
)
def test_failures_fall_back_to_today_with_one_warning(
    library: Library,
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_role(library.home, "Architect.md", "home text")
    command = library.command
    if case == "exit-2":
        library.settings(exit=2)
    elif case == "missing-command":
        command = [str(tmp_path / "no-such-agent-library")]
    elif case == "timeout":
        library.settings(sleep=5)
        monkeypatch.setattr(agent_library, "RESOLVE_TIMEOUT_SECONDS", 0.2)
    elif case == "format-version-2":
        library.write(dict(library.document, format_version=2))
    else:
        (library.cli_dir / "resolve.json").write_text("Traceback: boom")
    monkeypatch.setattr(agent_library, "configured_command", lambda: tuple(command))

    with caplog.at_level(logging.WARNING, logger=agent_library.__name__):
        for _ in range(2):
            assert _architect(library).endswith("home text")
            assert apply_session_role(_spec(), {}, str(library.project)).skills == []
    assert len([r for r in caplog.records if r.name == agent_library.__name__]) == 1


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, None),
        ({"agent_library": {"command": "uv run agent-library"}}, ("uv", "run", "agent-library")),
        (
            {"agent_library": {"command": ["agent-library", "--library", "/x"]}},
            ("agent-library", "--library", "/x"),
        ),
        ({"agent_library": {"command": 5}}, None),
    ],
)
def test_the_command_comes_from_one_config_setting(
    config: dict[str, object], expected: tuple[str, ...] | None
) -> None:
    assert agent_library.configured_command(config) == expected
