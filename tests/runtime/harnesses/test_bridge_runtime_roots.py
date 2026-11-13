"""Native bridge roots live under the per-user runtime parent, not the system temp dir."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent._platform import stable_user_id
from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.inner import claude_sdk_executor
from omnigent.runtime.harnesses.paths import HARNESS_TMP_PARENT_ENV_VAR, RUNTIME_PARENT_MAX

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX runtime parent")

# Each bridge's module and the call that creates one session's dir through it.
_CREATE_SESSION_DIR = {
    "claude": "b._ensure_secure_dir(b._BRIDGE_ROOT / 'sess')",
    "cursor": "b._ensure_secure_bridge_dir(b.bridge_dir_for_session_id('sess'))",
    "devin": "b.prepare_bridge_dir('sess')",
    "goose": "b._ensure_dir(b.bridge_dir_for_session_id('sess'))",
    "hermes": "b._ensure_dir(b.bridge_dir_for_session_id('sess'))",
    "kimi": "b._ensure_dir(b.bridge_dir_for_session_id('sess'))",
    "kiro": "b.prepare_bridge_dir('sess')",
    "qwen": "b._ensure_secure_bridge_dir(b.bridge_dir_for_session_id('sess'))",
}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A dir short enough that a parent below it fits the socket limit."""
    path = Path(tempfile.mkdtemp(prefix="rp-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _child_env(**overrides: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("OMNIGENT_")}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run_python(script: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("harness", sorted(_CREATE_SESSION_DIR))
def test_bridge_dirs_land_under_the_configured_parent(harness: str, short_dir: Path) -> None:
    system_tmp = short_dir / "sys"
    system_tmp.mkdir()
    parent = short_dir / "rp"
    script = (
        f"from omnigent.harnesses.{harness}_native import bridge as b\n"
        f"{_CREATE_SESSION_DIR[harness]}\n"
        "print(b._BRIDGE_ROOT)"
    )
    env = _child_env(TMPDIR=str(system_tmp), **{HARNESS_TMP_PARENT_ENV_VAR: str(parent)})

    root = Path(_run_python(script, env))

    assert root == parent / f"{harness}-native"
    assert (root / "sess").is_dir() or any(root.iterdir())
    assert list(system_tmp.iterdir()) == []


def test_bridge_roots_default_to_the_per_user_tmp_dir() -> None:
    script = (
        "import importlib, json\n"
        f"names = {sorted(_CREATE_SESSION_DIR)!r}\n"
        "print(json.dumps({n: str(importlib.import_module("
        "f'omnigent.harnesses.{n}_native.bridge')._BRIDGE_ROOT) for n in names}))"
    )
    roots = json.loads(_run_python(script, _child_env(**{HARNESS_TMP_PARENT_ENV_VAR: None})))

    default = Path(f"/tmp/omnigent-{os.getuid()}")
    assert roots == {name: str(default / f"{name}-native") for name in _CREATE_SESSION_DIR}


def test_claude_hooks_and_serve_mcp_agree_with_the_runner_root(
    short_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Children resolve the runner's root even when they inherit another parent."""
    runner_parent = short_dir / "runner"
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT_PARENT", runner_parent)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", runner_parent / "claude-native")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", short_dir)
    bridge_dir = runner_parent / "claude-native" / "sess"
    bridge_dir.mkdir(parents=True)
    args = claude_bridge.augment_claude_args(
        (),
        bridge_dir=bridge_dir,
        python_executable=sys.executable,
        ap_server_url="http://127.0.0.1:8787",
    )
    mcp_env = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["omnigent"]["env"]
    inherited = _child_env(**{HARNESS_TMP_PARENT_ENV_VAR: str(tmp_path / "shell")})
    print_root = "from omnigent.harnesses.claude_native import bridge as b; print(b._BRIDGE_ROOT)"

    serve_mcp_root = _run_python(print_root, {**inherited, **mcp_env})
    hook_command = claude_bridge._python_hook_command([sys.executable, "-c", print_root])
    hook = subprocess.run(
        hook_command, shell=True, env=inherited, capture_output=True, text=True, timeout=120
    )

    expected = str(runner_parent / "claude-native")
    assert serve_mcp_root == expected
    assert hook.returncode == 0, hook.stderr[-2000:]
    assert hook.stdout.strip().splitlines()[-1] == expected
    assert shlex.quote(str(runner_parent)) in hook_command


def test_claude_bridge_refuses_a_parent_too_long_for_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_parent = tmp_path / ("p" * max(1, RUNTIME_PARENT_MAX + 1 - len(os.fsencode(tmp_path))))
    monkeypatch.setenv(HARNESS_TMP_PARENT_ENV_VAR, str(long_parent))

    with pytest.raises(RuntimeError, match=f"the limit is {RUNTIME_PARENT_MAX} bytes"):
        claude_bridge._ensure_secure_dir(long_parent / "claude-native" / "sess")
    assert not long_parent.exists()


def test_claude_bridge_refuses_a_symlinked_parent(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = short_dir / "elsewhere"
    elsewhere.mkdir()
    parent = short_dir / "rp"
    parent.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv(HARNESS_TMP_PARENT_ENV_VAR, str(parent))

    with pytest.raises(RuntimeError):
        claude_bridge._ensure_secure_dir(parent / "claude-native" / "sess")
    assert list(elsewhere.iterdir()) == []


def test_claude_sdk_cli_tmpdir_follows_the_parent(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HARNESS_TMP_PARENT_ENV_VAR, str(short_dir / "rp"))
    assert claude_sdk_executor._claude_cli_tmpdir() == short_dir / "rp"
    assert (short_dir / "rp").is_dir()

    monkeypatch.delenv(HARNESS_TMP_PARENT_ENV_VAR)
    assert claude_sdk_executor._claude_cli_tmpdir() == Path(tempfile.gettempdir())
    # The CLI names its dir claude-<uid>; the grant must match that uid.
    assert stable_user_id() == str(os.getuid())
