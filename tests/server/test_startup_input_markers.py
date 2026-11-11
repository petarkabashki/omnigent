"""Delivery markers keep a peer message's header on its first line."""

from __future__ import annotations

import pytest

from omnigent.server.role_threads import thread_of_header
from omnigent.server.startup_inputs import RESENT_NOTICE, add_delivery_markers

_HEADER = "[Message from omnigent/Product (session s1, message msg_1)] [thread omnigent/Product]"


def _blocks(text: str) -> list[dict[str, str]]:
    return [{"type": "input_text", "text": text}]


def test_resent_role_message_keeps_its_header_and_thread_tag() -> None:
    [block] = add_delivery_markers(_blocks(f"{_HEADER}\nreview C13"), [RESENT_NOTICE])

    assert block["text"] == f"{_HEADER}\n{RESENT_NOTICE}\nreview C13"
    assert thread_of_header(block["text"]) == "omnigent/Product"


def test_header_only_message_gets_the_marker_after_it() -> None:
    [block] = add_delivery_markers(_blocks(_HEADER), [RESENT_NOTICE])

    assert block["text"] == f"{_HEADER}\n{RESENT_NOTICE}"


def test_message_without_header_keeps_the_marker_first() -> None:
    [block] = add_delivery_markers(_blocks("hello"), [RESENT_NOTICE])

    assert block["text"] == f"{RESENT_NOTICE}\nhello"


def test_marker_appears_once_when_added_again() -> None:
    for text in (f"{_HEADER}\nreview C13", "hello"):
        once = add_delivery_markers(_blocks(text), [RESENT_NOTICE])
        twice = add_delivery_markers(once, [RESENT_NOTICE])

        assert twice == once
        assert twice[0]["text"].count(RESENT_NOTICE) == 1


def test_content_without_text_gets_a_marker_block_and_is_not_mutated() -> None:
    image = {"type": "input_image", "image_url": "https://example.com/a.png"}
    content = [image]

    marked = add_delivery_markers(content, [RESENT_NOTICE])

    assert marked == [{"type": "input_text", "text": RESENT_NOTICE}, image]
    assert content == [image]


def test_unregistered_marker_is_rejected_on_both_paths() -> None:
    image = {"type": "input_image", "image_url": "https://example.com/a.png"}
    for content in (_blocks("hello"), [image]):
        with pytest.raises(ValueError, match="unregistered delivery marker"):
            add_delivery_markers(content, ["[Not a marker]"])
