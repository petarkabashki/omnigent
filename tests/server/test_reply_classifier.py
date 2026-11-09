"""LLM reply classification and how its verdict lands in session and thread labels."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest

from omnigent.entities import MessageData
from omnigent.process_logging import DATA_DIR_ENV_VAR
from omnigent.server import awaiting_input, reply_classifier, role_threads
from omnigent.server.reply_classifier import ReplyClass, classify


def _completion(content: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == reply_classifier.DEFAULT_MODEL
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # Keeps a real ~/.omnigent/openrouter.key out of every test.
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    return tmp_path


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(reply_classifier.API_KEY_ENV, "test-key")
    monkeypatch.delenv(reply_classifier.DISABLE_ENV, raising=False)
    monkeypatch.delenv(reply_classifier.MODEL_ENV, raising=False)


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"kind": "input", "reason": "Land it?"}', ReplyClass("input", "Land it?")),
        ('{"kind": "ack", "reason": "Review  branch\\n x"}', ReplyClass("ack", "Review branch x")),
        ('{"kind": "info", "reason": "ignored"}', ReplyClass("info", "")),
        ('```json\n{"kind": "info", "reason": ""}\n```', ReplyClass("info", "")),
        ('{"kind": "maybe", "reason": ""}', None),
        ("not json", None),
    ],
)
def test_parses_the_verdict(content: str, expected: ReplyClass | None) -> None:
    assert classify("Done. Land it?", transport=_completion(content)) == expected


@pytest.mark.usefixtures("with_key")
def test_http_failure_returns_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    assert classify("Land it?", transport=transport) is None


def test_without_a_key_nothing_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(reply_classifier.API_KEY_ENV, raising=False)
    transport = httpx.MockTransport(lambda request: pytest.fail("no call expected"))
    assert classify("Land it?", transport=transport) is None


@pytest.mark.parametrize(
    ("env_key", "file_text", "expected_key"),
    [
        (None, "test-key\n", "test-key"),
        ("test-key", "file-key\n", "test-key"),
        (None, "  \n", None),
    ],
)
def test_key_falls_back_to_the_data_dir_file(
    monkeypatch: pytest.MonkeyPatch,
    isolated_data_dir: Path,
    env_key: str | None,
    file_text: str,
    expected_key: str | None,
) -> None:
    monkeypatch.delenv(reply_classifier.DISABLE_ENV, raising=False)
    monkeypatch.delenv(reply_classifier.MODEL_ENV, raising=False)
    if env_key is None:
        monkeypatch.delenv(reply_classifier.API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(reply_classifier.API_KEY_ENV, env_key)
    (isolated_data_dir / reply_classifier.API_KEY_FILE_NAME).write_text(file_text)

    assert reply_classifier.enabled() is (expected_key is not None)
    if expected_key is None:
        transport = httpx.MockTransport(lambda request: pytest.fail("no call expected"))
        assert classify("Land it?", transport=transport) is None
    else:
        verdict = '{"kind": "input", "reason": "Land it?"}'
        assert classify("Land it?", transport=_completion(verdict)) == ReplyClass(
            "input", "Land it?"
        )


class _Store:
    """Just the store surface the label writers use: labels and newest-first messages."""

    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self.labels = dict(labels or {})
        self.items: list[Any] = []

    def say(self, role: Literal["user", "assistant"], text: str) -> None:
        block = "output_text" if role == "assistant" else "input_text"
        data = MessageData(role=role, agent="a", content=[{"type": block, "text": text}])
        self.items.insert(0, SimpleNamespace(data=data))

    def get_conversation(self, session_id: str) -> Any:
        return SimpleNamespace(id=session_id, labels=dict(self.labels))

    def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
        self.labels.update(updates)

    def list_items(self, *args: object, **kwargs: object) -> Any:
        return SimpleNamespace(data=self.items)


def _turn(text: str) -> _Store:
    store = _Store()
    store.say("user", "Go")
    store.say("assistant", text)
    return store


@pytest.mark.parametrize(
    ("result", "awaiting", "ack"),
    [
        (ReplyClass("input", "Land it?"), "Land it?", ""),
        (ReplyClass("ack", "Review the branch"), "", "Review the branch"),
        (ReplyClass("info", ""), "", ""),
        # The classifier failed: the heuristic decides, with no acknowledgement.
        (None, "Should I land it?", ""),
    ],
)
def test_classification_sets_the_session_labels(
    result: ReplyClass | None, awaiting: str, ack: str
) -> None:
    store = _turn("Pushed. Should I land it?")
    awaiting_input.apply_classification(store, "s", "Pushed. Should I land it?", result)  # type: ignore[arg-type]
    assert store.labels.get(awaiting_input.AWAITING_INPUT_LABEL_KEY, "") == awaiting
    assert store.labels.get(awaiting_input.AWAITING_ACK_LABEL_KEY, "") == ack


def test_a_stale_classification_is_dropped() -> None:
    store = _turn("Should I land it?")
    store.say("user", "yes")
    awaiting_input.apply_classification(
        store,  # type: ignore[arg-type]
        "s",
        "Should I land it?",
        ReplyClass("input", "Land it?"),
    )
    assert awaiting_input.AWAITING_INPUT_LABEL_KEY not in store.labels


def test_turn_end_waits_for_the_classifier(
    with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(reply_classifier, "submit", lambda text, *a, **k: submitted.append(text))
    store = _turn("Done. Should I land it?")
    store.labels[awaiting_input.AWAITING_ACK_LABEL_KEY] = "old"
    awaiting_input.refresh_after_turn(store, "s")  # type: ignore[arg-type]
    assert submitted == ["Done. Should I land it?"]
    assert store.labels.get(awaiting_input.AWAITING_INPUT_LABEL_KEY, "") == ""
    assert store.labels[awaiting_input.AWAITING_ACK_LABEL_KEY] == ""


@pytest.mark.parametrize(
    ("kind", "last", "state", "expected"),
    [
        ("info", "msg_1", "waiting_on_us", "open"),
        ("ack", "msg_1", "waiting_on_us", "waiting_on_us"),
        ("info", "msg_2", "waiting_on_us", "waiting_on_us"),
        ("info", "msg_1", "blocked", "blocked"),
    ],
)
def test_informational_thread_message_leaves_the_thread_open(
    kind: reply_classifier.ReplyKind, last: str, state: str, expected: str
) -> None:
    counterpart = "/Architect"
    store = _Store(
        {
            role_threads._label(counterpart, "last"): last,
            role_threads._label(counterpart, "state"): state,
        }
    )
    role_threads.settle_inbound_kind(store, "s", counterpart, "msg_1", kind)  # type: ignore[arg-type]
    assert store.labels[role_threads._label(counterpart, "state")] == expected
