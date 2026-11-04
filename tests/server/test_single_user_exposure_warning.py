"""Unit tests for the single-user exposure warning helpers.

``warn_if_single_user_exposed`` is the rule every spawn path shares — the
CLI prints it to stderr, the container entrypoints log it. It fires only
where the exposure is real: a reachable bind, a truthy single-user
marker, and a resolved source of ``header`` (the one mode where the
unauthenticated ``"local"`` fallback is live).
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from omnigent.server import auth as auth_module
from omnigent.server.auth import (
    bind_host_is_loopback,
    forwarded_request_signal,
    warn_if_single_user_exposed,
    warn_once_if_single_user_forwarded,
)

_AUTH_ENVS = (
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_LOCAL_SINGLE_USER",
    "OMNIGENT_OIDC_ISSUER",
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear auth env vars so each case starts from a known posture."""
    for _var in _AUTH_ENVS:
        monkeypatch.delenv(_var, raising=False)


# ── bind_host_is_loopback ───────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_hosts_recognized(host: str) -> None:
    """Literals and the wider 127.0.0.0/8 range count as loopback."""
    assert bind_host_is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5"])
def test_non_loopback_hosts_recognized(host: str) -> None:
    """Wildcards are NOT loopback — they accept traffic from every interface."""
    assert bind_host_is_loopback(host) is False


def test_unresolvable_host_treated_as_reachable() -> None:
    """An unparseable host errs toward "reachable" so the warning still fires."""
    assert bind_host_is_loopback("some-k8s-service.internal") is False


# ── warn_if_single_user_exposed ─────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
@pytest.mark.parametrize("marker", ["1", "true", "yes", "TRUE"])
def test_warns_on_reachable_bind_with_truthy_marker(
    host: str, marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable bind + truthy marker + header mode → warning names the exposure."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", marker)

    msg = warn_if_single_user_exposed(host)

    assert msg is not None
    assert host in msg
    assert "unauthenticated" in msg.lower()
    assert "OMNIGENT_LOCAL_SINGLE_USER" in msg


def test_silent_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """On loopback the marker is the normal, safe local posture."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")

    assert warn_if_single_user_exposed("127.0.0.1") is None


def test_silent_without_marker() -> None:
    """No marker means no ``"local"`` fallback, so nothing is exposed."""
    assert warn_if_single_user_exposed("0.0.0.0") is None


def test_silent_with_falsy_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``=0`` is an explicit opt-out, not a declaration."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")

    assert warn_if_single_user_exposed("0.0.0.0") is None


@pytest.mark.parametrize("provider", ["accounts", "oidc"])
def test_silent_under_explicit_login_provider(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit login provider routes identity through the cookie path.

    Login is required there, so claiming exposure would be false.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", provider)
    if provider == "oidc":
        monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://idp.example.com")

    assert warn_if_single_user_exposed("0.0.0.0") is None


def test_warns_under_explicit_header_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning header mode deliberately is still exposure, so still warn."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")

    assert warn_if_single_user_exposed("0.0.0.0") is not None


def test_warns_under_auth_enabled_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AUTH_ENABLED=0`` is set-but-falsy, resolving to header mode.

    The Docker kill-switch posture, which also sets the marker.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")

    assert warn_if_single_user_exposed("0.0.0.0") is not None


def test_silent_under_auth_enabled_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truthy enable switch resolves to accounts — login required."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")

    assert warn_if_single_user_exposed("0.0.0.0") is None


# ── forwarded-request detection ─────────────────────────────────────


def _request(headers: dict[str, str]) -> Request:
    """Build a loopback-peer request carrying *headers*."""
    return Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Host": "localhost:8000"},
        {"Host": "127.0.0.1:8000"},
        {"Host": "[::1]:8000"},
        {"Host": "app.localhost:8000"},
        {"Host": "localhost:8000", "User-Agent": "Mozilla/5.0", "Origin": "http://localhost:8000"},
    ],
)
def test_normal_local_use_has_no_forward_signal(headers: dict[str, str]) -> None:
    """Plain local browser, CLI and plain SSH-tunnel requests look unforwarded."""
    assert forwarded_request_signal(_request(headers)) is None


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "203.0.113.9"},
        {"Forwarded": "for=203.0.113.9"},
        {"Via": "1.1 proxy"},
        {"Tailscale-User-Login": "a@example.com"},
        {"Cf-Ray": "abc"},
        {"Host": "box.tail1234.ts.net"},
        {"Host": "abc.ngrok-free.app:443"},
    ],
)
def test_forward_signals_detected(headers: dict[str, str]) -> None:
    """Relay headers or a non-loopback Host mark a forwarded request."""
    assert forwarded_request_signal(_request(headers)) is not None


def test_forward_warning_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Repeated forwarded requests log one warning; local ones log none."""
    monkeypatch.setattr(auth_module, "_forward_warning_emitted", False)
    with caplog.at_level("WARNING", logger=auth_module.logger.name):
        warn_once_if_single_user_forwarded(_request({"Host": "localhost:8000"}))
        assert not caplog.records
        for _ in range(3):
            warn_once_if_single_user_forwarded(_request({"X-Forwarded-For": "1.2.3.4"}))
    assert len(caplog.records) == 1
    assert "SSH tunnels" in caplog.records[0].getMessage()
