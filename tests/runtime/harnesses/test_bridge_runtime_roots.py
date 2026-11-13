"""Native bridge roots live under the per-user runtime parent, not the system temp dir."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from omnigent._platform import stable_user_id
from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.inner import claude_sdk_executor
from omnigent.runtime.harnesses.paths import (
    HARNESS_TMP_PARENT_ENV_VAR,
    RUNTIME_PARENT_MAX,
    RuntimeParentError,
    ensure_runtime_parent,
)

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
def test_bridge_dirs_land_under_the_configured_parent(harness: str, short_tmp_dir: Path) -> None:
    system_tmp = short_tmp_dir / "sys"
    system_tmp.mkdir()
    parent = short_tmp_dir / "rp"
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
    short_tmp_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Children resolve the runner's root even when they inherit another parent."""
    runner_parent = short_tmp_dir / "runner"
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT_PARENT", runner_parent)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", runner_parent / "claude-native")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", short_tmp_dir)
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
    short_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = short_tmp_dir / "elsewhere"
    elsewhere.mkdir()
    parent = short_tmp_dir / "rp"
    parent.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv(HARNESS_TMP_PARENT_ENV_VAR, str(parent))

    with pytest.raises(RuntimeError):
        claude_bridge._ensure_secure_dir(parent / "claude-native" / "sess")
    assert list(elsewhere.iterdir()) == []


def test_claude_sdk_cli_tmpdir_follows_the_parent(
    short_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HARNESS_TMP_PARENT_ENV_VAR, str(short_tmp_dir / "rp"))
    assert claude_sdk_executor._claude_cli_tmpdir() == short_tmp_dir / "rp"
    assert (short_tmp_dir / "rp").is_dir()

    monkeypatch.delenv(HARNESS_TMP_PARENT_ENV_VAR)
    assert claude_sdk_executor._claude_cli_tmpdir() == Path(tempfile.gettempdir())
    # The CLI names its dir claude-<uid>; the grant must match that uid.
    assert stable_user_id() == str(os.getuid())


def _serve_mcp_entry(harness: str, bridge_dir: Path) -> dict:
    """The serve-mcp server entry each harness writes into its MCP config."""
    module = importlib.import_module(f"omnigent.harnesses.{harness}_native.bridge")
    if harness in ("claude", "cursor"):
        (entry,) = module.build_mcp_config(bridge_dir)["mcpServers"].values()
        return entry
    if harness == "devin":
        return module.build_devin_mcp_server(bridge_dir)
    if harness == "kiro":
        (entry,) = module.build_kiro_mcp_config(bridge_dir)["mcpServers"].values()
        return entry
    if harness == "qwen":
        return module.build_mcp_server_entry(bridge_dir)
    hermes_home = module.write_policy_hook_config(bridge_dir, "http://server.example", "sess")
    return json.loads((hermes_home / "config.yaml").read_text())["mcp_servers"]["omnigent"]


@pytest.mark.parametrize("harness", ["claude", "cursor", "devin", "hermes", "kiro", "qwen"])
def test_serve_mcp_children_pin_the_runner_parent(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # hermes copies ~/.hermes auth files
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    module = importlib.import_module(f"omnigent.harnesses.{harness}_native.bridge")
    entry = _serve_mcp_entry(harness, bridge_dir)
    assert "serve-mcp" in entry["args"]
    assert entry["env"][HARNESS_TMP_PARENT_ENV_VAR] == str(module._BRIDGE_ROOT.parent)


@pytest.mark.parametrize("harness", ["goose", "kimi"])
def test_harnesses_without_a_serve_mcp_child_need_no_pin(harness: str) -> None:
    # Their hook children get the concrete bridge dir, so there is no root to agree on.
    module = importlib.import_module(f"omnigent.harnesses.{harness}_native.bridge")
    assert "serve-mcp" not in Path(module.__file__).read_text()


def test_host_refuses_a_bad_parent_as_a_configuration_error(tmp_path: Path) -> None:
    long_parent = tmp_path / ("p" * max(1, RUNTIME_PARENT_MAX + 1 - len(os.fsencode(tmp_path))))
    env = _child_env(
        **{
            HARNESS_TMP_PARENT_ENV_VAR: str(long_parent),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent", "host", "--non-interactive", "--no-open"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert f"Error: runtime parent {long_parent} is" in proc.stderr
    assert f"Shorten {HARNESS_TMP_PARENT_ENV_VAR}" in proc.stderr
    output = proc.stdout + proc.stderr
    for absent in ("ran into an issue", "Traceback", "omnigent stop", "Connected"):
        assert absent not in output, output[-2000:]
    assert not long_parent.exists()


def test_parent_errors_are_configuration_errors(short_tmp_dir: Path) -> None:
    shared = short_tmp_dir / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # other-writable without the sticky bit
    with pytest.raises(RuntimeParentError, match="sticky"):
        ensure_runtime_parent({HARNESS_TMP_PARENT_ENV_VAR: str(shared / "rp")})
