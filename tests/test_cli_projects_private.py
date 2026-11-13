"""``omnigent projects private`` resolves a project path and PATCHes the flag."""

import contextlib

from click.testing import CliRunner

from omnigent import cli


class _Response:
    is_error = False
    content = b"x"

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


class _Client:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, str, object]] = []

    def get(self, url: str, **_: object) -> _Response:
        self.requests.append(("GET", url, None))
        return _Response({"data": self.rows})

    def patch(self, url: str, json: dict, **_: object) -> _Response:
        self.requests.append(("PATCH", url, json))
        return _Response({**self.rows[-1], "private": json["private"]})


def _run(monkeypatch, rows: list[dict], args: list[str]):
    client = _Client(rows)
    monkeypatch.setattr(cli, "_session_api_client", lambda _server: contextlib.nullcontext(client))
    return client, CliRunner().invoke(cli.cli, ["projects", "private", *args])


def test_on_patches_the_project_found_by_path(monkeypatch) -> None:
    rows = [
        {"id": "1" * 32, "name": "acme", "path": "acme"},
        {"id": "2" * 32, "name": "research", "path": "acme/research"},
    ]
    client, result = _run(monkeypatch, rows, ["ACME/research", "on"])
    assert result.exit_code == 0, result.output
    assert client.requests[-1] == ("PATCH", f"/v1/projects/{'2' * 32}", {"private": True})
    assert "acme/research is now private." in result.output


def test_unknown_project_fails_without_writing(monkeypatch) -> None:
    client, result = _run(monkeypatch, [{"id": "1" * 32, "name": "a", "path": "a"}], ["b", "off"])
    assert result.exit_code != 0
    assert "No project at 'b'" in result.output
    assert [method for method, _, _ in client.requests] == ["GET"]
