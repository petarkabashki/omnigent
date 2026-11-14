"""Host recovery sweeps visit only sessions with startup work and back off when idle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server import startup_inputs
from omnigent.server.startup_inputs import STARTUP_LABEL, ManagedStartupInputs


class _PagedHostStore:
    def __init__(self, session_ids: list[str], candidates: set[str]) -> None:
        self.session_ids = sorted(session_ids)
        self.candidates = candidates
        self.pages: list[str | None] = []

    def list_host_conversation_ids(
        self, host_id: str, *, after: str | None = None, limit: int = 32
    ) -> list[str]:
        self.pages.append(after)
        remaining = [sid for sid in self.session_ids if after is None or sid > after]
        return remaining[:limit]

    def select_startup_candidates(self, ids: list[str], *, label_key: str) -> set[str]:
        assert label_key == STARTUP_LABEL
        return self.candidates & set(ids)


def _service(store: _PagedHostStore, connection: object) -> ManagedStartupInputs:
    hosts = SimpleNamespace(get=lambda _host_id: connection)
    return ManagedStartupInputs(store, hosts)  # type: ignore[arg-type]


async def test_a_sweep_visits_only_candidates_across_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_ids = [f"conv_{index:04d}" for index in range(300)]
    store = _PagedHostStore(session_ids, {"conv_0003", "conv_0290"})
    connection = object()
    service = _service(store, connection)
    visited: list[str] = []

    async def recover(_app: Any, session_id: str, _connection: object) -> bool:
        visited.append(session_id)
        return session_id == "conv_0290"

    monkeypatch.setattr(service, "_recover_session", recover)

    busy = await service._sweep_host(None, "host", connection)  # type: ignore[arg-type]

    assert busy is True
    assert visited == ["conv_0003", "conv_0290"]
    assert store.pages == [None, session_ids[startup_inputs._SWEEP_PAGE_SIZE - 1]]


async def test_idle_sweeps_back_off_on_a_host_with_many_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_ids = [f"conv_{index:04d}" for index in range(600)]
    connection: object | None = object()
    store = _PagedHostStore(session_ids, set())
    service = _service(store, connection)
    service.hosts = SimpleNamespace(get=lambda _host_id: connection)  # type: ignore[assignment]
    delays: list[float] = []
    done = asyncio.Event()

    async def record_wait(_wake: asyncio.Event, delay: float) -> None:
        nonlocal connection
        delays.append(delay)
        if len(delays) == 6:
            connection = None
            done.set()

    monkeypatch.setattr(startup_inputs, "_wait_for_wake", record_wait)
    service.host_changed(None, "host")
    async with asyncio.timeout(5):
        await done.wait()

    assert delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]
