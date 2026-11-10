"""Content-aware loop guard shared by role threads and group threads.

Only short or repeated messages count toward the loop cap; every message counts
toward a larger burst backstop within the same quiet-reset window.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MAX_HOPS = 6
HOP_RESET_SECONDS = 600  # a quiet thread starts counting again
SHORT_MESSAGE_CHARS = 280
REPEAT_WINDOW = 3  # a message repeating one of the sender's last N is a loop hop
BURST_HOPS = 20  # every message counts here, regardless of content

Tripped = Literal["loop", "burst"]
_HEADER_LINE = re.compile(r"^\[Message from [^\n]*\](?: \[thread [^\]\n]+\])?$")


def limit_reached(
    tripped: Tripped,
    *,
    max_hops: int = MAX_HOPS,
    burst_hops: int = BURST_HOPS,
    window_seconds: int = HOP_RESET_SECONDS,
) -> str:
    """The limit a refused message hit, worded for the sender."""
    if tripped == "loop":
        return f"the loop limit ({max_hops} short or repeated messages without a pause)"
    return f"the message backstop ({burst_hops} messages in {window_seconds // 60} minutes)"


def message_digest(text: str) -> str:
    """Short hash of a message body: headers dropped, lowercased, whitespace collapsed."""
    lines = [line for line in text.splitlines() if not _HEADER_LINE.match(line.strip())]
    normalized = " ".join(" ".join(lines).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def counts_as_hop(text: str, recent_digests: Sequence[str]) -> bool:
    """Whether a message is short or repeats one of the sender's recent messages."""
    return (
        len(text.strip()) < SHORT_MESSAGE_CHARS
        or message_digest(text) in recent_digests[-REPEAT_WINDOW:]
    )


@dataclass(frozen=True)
class GuardVerdict:
    """Counters after a message, and which limit it tripped, if any."""

    hops: int
    stamps: list[int]
    recent: list[str]
    tripped: Tripped | None


def evaluate(
    text: str,
    *,
    prev_hops: int,
    last_at: int,
    stamps: Sequence[int],
    recent: Sequence[str],
    now: int,
    max_hops: int = MAX_HOPS,
    burst_hops: int = BURST_HOPS,
) -> GuardVerdict:
    """Apply the guard to one message; ``tripped`` is set when it must be held."""
    fresh = now - last_at <= HOP_RESET_SECONDS
    window = [stamp for stamp in stamps if now - stamp <= HOP_RESET_SECONDS]
    hops = prev_hops if fresh else 0
    if counts_as_hop(text, recent):
        hops += 1
    tripped: Tripped | None = None
    if hops > max_hops:
        tripped = "loop"
    elif len(window) >= burst_hops:
        tripped = "burst"
    digests = [*recent, message_digest(text)][-REPEAT_WINDOW:]
    return GuardVerdict(hops, [*window, now][-burst_hops:], digests, tripped)
