"""Tests for per-role instruction loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.role_instructions import MAX_ROLE_FILE_BYTES, load_role_instructions


def _write_role(root: Path, name: str, text: str) -> Path:
    path = root / ".omnigent" / "roles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_workspace_role_file_wins_over_home(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write_role(home, "Product.md", "home text")
    source = _write_role(workspace, "Product.md", "workspace text\n")
    text = load_role_instructions("Product", str(workspace), home=home)
    assert text == f"## Role: Product\n\nRole instructions from {source}:\n\nworkspace text"


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
