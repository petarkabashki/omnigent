"""``omnigent projects migrate-slashes`` prints a plan and never writes."""

import contextlib

from click.testing import CliRunner

from omnigent import cli


class _Client:
    """Records every request; only GET /v1/projects is expected."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.requests: list[str] = []

    def get(self, url: str, **_: object):
        self.requests.append(f"GET {url}")
        rows = self.rows

        class _Response:
            is_error = False
            content = b"x"

            def json(self) -> dict:
                return {"data": rows}

        return _Response()


def _run(monkeypatch, rows: list[dict], args: list[str]):
    client = _Client(rows)
    monkeypatch.setattr(cli, "_session_api_client", lambda _server: contextlib.nullcontext(client))
    return client, CliRunner().invoke(cli.cli, ["projects", "migrate-slashes", *args])


def test_dry_run_prints_the_plan_and_only_reads(monkeypatch) -> None:
    rows = [
        {"id": "1" * 32, "name": "a", "parent_id": None},
        {"id": "2" * 32, "name": "a/x", "parent_id": None},
        {"id": "3" * 32, "name": "q/r", "parent_id": None},
    ]
    client, result = _run(monkeypatch, rows, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "create namespace q" in result.output
    assert "rename to 'x' and move under a" in result.output
    assert "Dry run only" in result.output
    assert client.requests == ["GET /v1/projects"]


def test_without_dry_run_flag_it_refuses(monkeypatch) -> None:
    client, result = _run(monkeypatch, [], [])
    assert result.exit_code != 0
    assert client.requests == []
