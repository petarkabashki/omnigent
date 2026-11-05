"""Tests for per-role overlays: loading, substitution, layering, trust and merge."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from omnigent.runtime.role_instructions import (
    MAX_ROLE_FILE_BYTES,
    apply_role_overlay,
    apply_session_role,
    load_role_instructions,
    load_role_overlay,
    load_session_role_overlay,
    role_params_from_labels,
    substitute_placeholders,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, MCPServerConfig, SkillSpec


def _write_role(root: Path, name: str, text: str) -> Path:
    path = root / ".omnigent" / "roles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _write_skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\nDo it.\n"
    )


def test_workspace_role_layers_over_home(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    home_source = _write_role(home, "Product.md", "home text")
    ws_source = _write_role(workspace, "Product.md", "workspace text\n")
    text = load_role_instructions("Product", str(workspace), home=home)
    assert text == (
        f"## Role: Product\n\nRole instructions from {home_source}:\n\nhome text\n\n"
        f"Role instructions from {ws_source}:\n\nworkspace text"
    )


def test_home_fallback_and_case_insensitive_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_role(home, "product.md", "home text")
    text = load_role_instructions("PRODUCT", str(tmp_path / "missing-ws"), home=home)
    assert text is not None and text.endswith("home text")


@pytest.mark.parametrize("alias", [None, "", "../etc", "9bad", "a/b"])
def test_invalid_or_missing_alias_loads_nothing(tmp_path: Path, alias: str | None) -> None:
    _write_role(tmp_path, "etc.md", "never")
    assert load_role_instructions(alias, str(tmp_path), home=tmp_path) is None


def test_empty_or_absent_role_file(tmp_path: Path) -> None:
    assert load_role_instructions("Bench", str(tmp_path), home=tmp_path / "h") is None
    _write_role(tmp_path, "Bench.md", "  \n")
    assert load_role_instructions("Bench", str(tmp_path), home=tmp_path / "h") is None


def test_role_file_is_truncated(tmp_path: Path) -> None:
    _write_role(tmp_path, "Big.md", "x" * (MAX_ROLE_FILE_BYTES + 100))
    text = load_role_instructions("Big", str(tmp_path), home=tmp_path / "h")
    assert text is not None and text.count("x") == MAX_ROLE_FILE_BYTES


def test_front_matter_description_heads_the_role(tmp_path: Path) -> None:
    _write_role(
        tmp_path, "Bench.md", "---\ndescription: Evals and verification\n---\nRun evals.\n"
    )
    text = load_role_instructions("Bench", str(tmp_path), home=tmp_path / "h")
    assert text is not None
    assert text.startswith("## Role: Bench (Evals and verification)\n")
    assert text.endswith("Run evals.") and "description:" not in text


def test_unreadable_front_matter_keeps_the_body(tmp_path: Path) -> None:
    _write_role(tmp_path, "Bench.md", "---\n: [unclosed\n---\nRun evals.\n")
    text = load_role_instructions("Bench", str(tmp_path), home=tmp_path / "h")
    assert text is not None and text.startswith("## Role: Bench\n") and text.endswith("Run evals.")


def test_substitution_leaves_unknown_and_env_placeholders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    values = {"params.repo": "omnigent", "alias": "Bench"}
    with caplog.at_level(logging.WARNING):
        text = substitute_placeholders("${alias} ${params.repo} ${params.nope} ${HOME}", values)
    assert text == "Bench omnigent ${params.nope} ${HOME}"
    assert "params.nope" in caplog.text and "HOME" not in caplog.text


def test_folder_role_with_params_skills_and_mcp(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws" / "proj"
    role_md = _write_role(
        home,
        "Bench/ROLE.md",
        "---\nparams: {repo: omnigent, depth: 2}\nskills: [skills/evals, ../../escape]\n"
        "mcp_servers:\n  notes:\n    command: notes-mcp\n"
        '    args: ["--root", "${workspace}", "${params.repo}"]\n'
        "---\nBench ${project} at depth ${params.depth} as ${alias}.\n",
    )
    _write_skill(role_md.parent / "skills" / "evals", "evals")
    overlay = load_role_overlay("Bench", str(workspace), session_params={"depth": "5"}, home=home)
    assert overlay is not None
    assert overlay.params == {"repo": "omnigent", "depth": "5"}
    assert overlay.instructions is not None
    assert overlay.instructions.endswith("Bench proj at depth 5 as Bench.")
    assert [skill.name for skill in overlay.skills] == ["evals"]
    [server] = overlay.mcp_servers
    assert (server.name, server.command) == ("notes", "notes-mcp")
    assert server.args == ["--root", str(workspace), "omnigent"]


def test_workspace_role_cannot_add_skills_or_mcp_unless_trusted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write_role(home, "Bench.md", "---\nparams: {repo: home}\n---\nHome.\n")
    ws_role = _write_role(
        workspace,
        "Bench/ROLE.md",
        "---\nparams: {repo: ws}\nskills: [skills/evals]\n"
        "mcp_servers: {notes: {command: notes-mcp}}\n---\nWorkspace.\n",
    )
    _write_skill(ws_role.parent / "skills" / "evals", "evals")
    with caplog.at_level(logging.WARNING):
        untrusted = load_role_overlay("Bench", str(workspace), home=home)
    assert untrusted is not None
    assert untrusted.params == {"repo": "ws"}
    assert untrusted.instructions is not None and untrusted.instructions.endswith("Workspace.")
    assert (untrusted.skills, untrusted.mcp_servers) == ([], [])
    assert "untrusted workspace role" in caplog.text

    trust_list = home / ".omnigent" / "trusted_role_workspaces"
    trust_list.write_text(f"# comment\n{workspace}\n")
    trusted = load_role_overlay("Bench", str(workspace), home=home)
    assert trusted is not None
    assert [skill.name for skill in trusted.skills] == ["evals"]
    assert [server.name for server in trusted.mcp_servers] == ["notes"]


def _spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="agent",
        instructions="Author text.",
        executor=ExecutorSpec(),
        params={"repo": "spec", "keep": "1"},
        skills=[SkillSpec(name="evals", description="old", content="old")],
        mcp_servers=[MCPServerConfig(name="notes", transport="stdio", command="old")],
    )


def test_apply_role_overlay_is_additive_and_cannot_widen(tmp_path: Path) -> None:
    home = tmp_path / "home"
    role_md = _write_role(
        home,
        "Bench/ROLE.md",
        "---\nparams: {repo: role}\nskills: [skills/evals]\n"
        "mcp_servers: {notes: {command: new}, extra: {command: extra}}\n"
        "executor: {type: other}\nos_env: {type: host}\nguardrails: {policies: []}\n"
        "---\nRole text.\n",
    )
    _write_skill(role_md.parent / "skills" / "evals", "evals")
    overlay = load_role_overlay("Bench", None, home=home)
    assert overlay is not None
    spec = _spec()
    merged = apply_role_overlay(spec, overlay)

    assert merged.params == {"repo": "role", "keep": "1"}
    assert [(skill.name, skill.description) for skill in merged.skills] == [
        ("evals", "evals skill")
    ]
    assert [(server.name, server.command) for server in merged.mcp_servers] == [
        ("notes", "new"),
        ("extra", "extra"),
    ]
    assert merged.instructions == f"Author text.\n\n{overlay.instructions}"
    # Front-matter keys outside the overlay never reach the spec.
    for field in ("executor", "os_env", "guardrails", "tools", "llm", "name"):
        assert getattr(merged, field) == getattr(spec, field)
    # The input spec is not mutated, and re-applying does not duplicate text.
    assert spec.instructions == "Author text." and spec.params["repo"] == "spec"
    assert apply_role_overlay(merged, overlay).instructions == merged.instructions


def test_session_role_reads_alias_and_role_param_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write_role(home, "Bench.md", "---\nparams: {repo: file}\n---\nRepo ${params.repo}.\n")
    labels = {"omnigent.alias": "Bench", "omnigent.role_param.repo": "label", "other": "x"}
    assert role_params_from_labels(labels) == {"repo": "label"}
    merged = apply_session_role(_spec(), labels, None)
    assert merged.instructions is not None and merged.instructions.endswith("Repo label.")
    unaliased = _spec()
    assert apply_session_role(unaliased, {"other": "x"}, None) is unaliased


@pytest.mark.parametrize("trusted", [False, True])
def test_home_role_named_skill_ignores_untrusted_workspace_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    # Host skill discovery always includes the real home's ~/.claude/skills.
    monkeypatch.setenv("HOME", str(home))
    _write_role(home, "Bench.md", "---\nskills: [review, notes]\n---\nBody.\n")
    _write_skill(workspace / ".claude" / "skills" / "review", "review")
    _write_skill(home / ".claude" / "skills" / "notes", "notes")
    if trusted:
        listing = home / ".omnigent" / "trusted_role_workspaces"
        listing.write_text(f"{workspace.resolve()}\n")
    overlay = load_role_overlay("Bench", str(workspace), home=home)
    assert overlay is not None
    names = {skill.name for skill in overlay.skills}
    # The repo's skill loads only once the workspace is trusted; home skills always do.
    assert names == ({"review", "notes"} if trusted else {"notes"})


def test_session_overlay_label_wins_and_the_alias_stays_the_sessions_own(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write_role(home, "Bench.md", "You are ${alias}.")
    _write_role(home, "reviewer-7.md", "never loaded")
    labels = {"omnigent.alias": "reviewer-7", "omnigent.role_overlay": "Bench"}

    overlay = load_session_role_overlay(labels, str(workspace), home=home)

    assert overlay is not None and overlay.instructions is not None
    assert overlay.instructions.startswith("## Role: Bench\n")
    assert overlay.instructions.endswith("You are reviewer-7.")


def test_session_overlay_falls_back_to_the_alias(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_role(home, "Bench.md", "bench text")

    overlay = load_session_role_overlay({"omnigent.alias": "Bench"}, None, home=home)

    assert overlay is not None and overlay.alias == "Bench"
    assert load_session_role_overlay({}, None, home=home) is None
    assert load_session_role_overlay(None, None, home=home) is None


@pytest.mark.parametrize("overlay", ["../etc", "a/b", "9bad"])
def test_a_malformed_overlay_label_loads_nothing_not_the_alias(
    tmp_path: Path, overlay: str
) -> None:
    home = tmp_path / "home"
    _write_role(home, "Bench.md", "bench text")
    (home / ".omnigent" / "etc.md").write_text("never")
    labels = {"omnigent.alias": "Bench", "omnigent.role_overlay": overlay}

    assert load_session_role_overlay(labels, None, home=home) is None


def test_session_params_reach_the_overlay(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_role(home, "Bench.md", "Review ${params.repo}.")
    labels = {
        "omnigent.alias": "reviewer-7",
        "omnigent.role_overlay": "Bench",
        "omnigent.role_param.repo": "demo",
    }

    overlay = load_session_role_overlay(labels, None, home=home)

    assert overlay is not None and overlay.instructions is not None
    assert overlay.instructions.endswith("Review demo.")


def test_apply_session_role_uses_the_overlay_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_role(tmp_path, "Bench.md", "bench text")
    spec = AgentSpec(spec_version=1, name="a", instructions="base", executor=ExecutorSpec())

    applied = apply_session_role(
        spec, {"omnigent.alias": "reviewer-7", "omnigent.role_overlay": "Bench"}, None
    )

    assert applied.instructions is not None and applied.instructions.endswith("bench text")
