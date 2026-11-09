"""Classify an agent's message by what it needs from its reader, using an LLM.

Kinds: ``input`` (a question or decision only the reader can make), ``ack`` (a
handoff or result the reader should look at, nothing to decide) and ``info``.
Runs on its own small thread pool because the call takes seconds; any failure
returns ``None`` so callers keep their heuristic.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import httpx

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN

_logger = logging.getLogger(__name__)

ReplyKind = Literal["input", "ack", "info"]
_KINDS: tuple[ReplyKind, ...] = ("input", "ack", "info")

API_KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "OMNIGENT_REPLY_CLASSIFIER_MODEL"
DISABLE_ENV = "OMNIGENT_REPLY_CLASSIFIER_OFF"
DEFAULT_MODEL = "typesafe/jev-router"
_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT_SECONDS = 20.0
# Requests are decided by how a message ends; keep the tail.
_MAX_INPUT_CHARS = 6000

_AUDIENCE = {
    "user": "an AI coding agent's message to its human user",
    "agent": "a message one AI agent sent to another agent",
}

_INSTRUCTIONS = """You classify {audience} by what it needs from the reader.
- "input": the sender waits for the reader to answer a question, choose between options, \
or approve something before it can continue. Only a reply moves it forward.
- "ack": the sender hands over a result, reports a change, or asks the reader to do or \
check something, with nothing to decide (e.g. "done, please check", "landed; restart the \
hub", "heads-up: I changed X"). Seeing it is enough.
- "info": a status, an answer or a report that needs nothing. Optional offers such as \
"let me know if you want more" are info.
Reply with JSON: "kind" and "reason", where reason is at most 120 characters: the \
question for input, what to acknowledge for ack, empty for info."""

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "reply_kind",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_KINDS)},
                "reason": {"type": "string"},
            },
            "required": ["kind", "reason"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ReplyClass:
    """A classified message: its kind and a short reason (empty for ``info``)."""

    kind: ReplyKind
    reason: str


_executor: ThreadPoolExecutor | None = None


def enabled() -> bool:
    """Whether a key is configured and the classifier isn't switched off."""
    return bool(os.environ.get(API_KEY_ENV)) and not os.environ.get(DISABLE_ENV)


def _parse(body: dict[str, object]) -> ReplyClass | None:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    try:
        verdict = json.loads(content.strip().removeprefix("```json").strip("`\n "))
    except ValueError:
        return None
    kind = verdict.get("kind") if isinstance(verdict, dict) else None
    if kind not in _KINDS:
        return None
    reason = verdict.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) and kind != "info" else ""
    return ReplyClass(kind=kind, reason=reason[:LABEL_VALUE_MAX_LEN])


def classify(
    text: str,
    *,
    audience: Literal["user", "agent"] = "user",
    transport: httpx.BaseTransport | None = None,
) -> ReplyClass | None:
    """Classify ``text``; ``None`` when disabled, empty, or the call fails."""
    text = text.strip()
    if not text or not enabled():
        return None
    payload = {
        "model": os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": _INSTRUCTIONS.format(audience=_AUDIENCE[audience])},
            {"role": "user", "content": text[-_MAX_INPUT_CHARS:]},
        ],
        "response_format": _SCHEMA,
        "max_tokens": 200,
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {os.environ[API_KEY_ENV]}"}
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(_URL, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("reply classifier call failed: %s", type(exc).__name__)
        return None
    result = _parse(body) if isinstance(body, dict) else None
    usage = body.get("usage") if isinstance(body, dict) else None
    _logger.info(
        "reply classifier: kind=%s model=%s cost=%s",
        result.kind if result else None,
        body.get("model") if isinstance(body, dict) else None,
        usage.get("cost") if isinstance(usage, dict) else None,
    )
    return result


def submit(
    text: str,
    on_result: Callable[[ReplyClass], None],
    *,
    audience: Literal["user", "agent"] = "user",
    on_failure: Callable[[], None] | None = None,
) -> None:
    """Classify off-thread, then call ``on_result`` (or ``on_failure`` without a result)."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="reply-classifier")

    def run() -> None:
        try:
            result = classify(text, audience=audience)
            if result is not None:
                on_result(result)
            elif on_failure is not None:
                on_failure()
        except Exception:
            _logger.exception("reply classifier callback failed")

    _executor.submit(run)
