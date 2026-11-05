"""``omnigent session reset`` prints the server's words and the new epoch."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.errors import OmnigentError


@contextmanager
def _client(_server: str | None = None) -> Iterator[object]:
    yield object()


def test_session_reset_prints_the_epoch(monkeypatch: Any) -> None:
    calls: dict[str, object] = {}

    def resolve(_client: object, address: str) -> dict[str, str]:
        calls["address"] = address
        return {"session_id": "conv_abc", "handle": "omnigent/Architect"}

    def reset(
        _client: object,
        session_id: str,
        *,
        stop_running: bool = False,
        drop_pending: bool = False,
    ) -> dict[str, object]:
        calls["session_id"] = session_id
        calls["stop_running"] = stop_running
        calls["drop_pending"] = drop_pending
        return {"session_id": session_id, "epoch": 2, "boundary_item_id": "item_1"}

    monkeypatch.setattr("omnigent.cli._session_api_client", _client)
    monkeypatch.setattr("omnigent.session_alias_client.resolve_address", resolve)
    monkeypatch.setattr("omnigent.session_alias_client.reset_session", reset)

    result = CliRunner().invoke(cli, ["session", "reset", "omnigent/Architect", "--stop"])

    assert result.exit_code == 0
    assert result.output == "Reset omnigent/Architect in place. Session conv_abc is at epoch 2.\n"
    assert calls == {
        "address": "omnigent/Architect",
        "session_id": "conv_abc",
        "stop_running": True,
        "drop_pending": False,
    }


def test_session_reset_prints_the_server_refusal(monkeypatch: Any) -> None:
    def resolve(_client: object, _address: str) -> dict[str, str]:
        return {"session_id": "conv_abc", "handle": "omnigent/Architect"}

    def reset(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OmnigentError("The session has queued input; retry with pending='drop'")

    monkeypatch.setattr("omnigent.cli._session_api_client", _client)
    monkeypatch.setattr("omnigent.session_alias_client.resolve_address", resolve)
    monkeypatch.setattr("omnigent.session_alias_client.reset_session", reset)

    result = CliRunner().invoke(cli, ["session", "reset", "conv_abc"])

    assert result.exit_code != 0
    assert result.output.strip() == "The session has queued input; retry with pending='drop'"
