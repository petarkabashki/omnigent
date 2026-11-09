"""Heuristic for assistant messages that end by asking the user for input."""

from __future__ import annotations

import pytest

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.server.awaiting_input import detect_awaiting_input


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tests pass.\n\nShould I push this?", "Should I push this?"),
        ("Two options.\n\nWhich option do you want", "Which option do you want"),
        ("Ready to land. Approve to land on main?", "Approve to land on main?"),
        ("All done. Let me know if you want changes.", "Let me know if you want changes."),
        ("Done.\n\nPlease confirm the branch name.", "Please confirm the branch name."),
        ("**Do you want me to continue?**", "Do you want me to continue?"),
        ("Which of these do you prefer\n\n- A\n- B\n- C", "Which of these do you prefer"),
        ("Which of these do you want?\n\n- A\n- B\n- C", "Which of these do you want?"),
    ],
)
def test_detects_requests_to_the_user(text: str, expected: str | None) -> None:
    assert detect_awaiting_input(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Implemented the fix and all tests pass.",
        "Why does this fail? Because the cache is stale. I fixed it.",
        "Done.\n\n```\nShould I push this?\n```",
        "As you wrote:\n\n> Should I push this?\n\nThat is now done.",
        "Is the cache stale? Yes, so I cleared it.",
        "I approved the change last week.",
    ],
)
def test_ignores_statements_code_and_quotes(text: str) -> None:
    assert detect_awaiting_input(text) is None


def test_long_request_is_truncated_to_label_length() -> None:
    reason = detect_awaiting_input("Should I " + "really " * 100 + "do it?")
    assert reason is not None
    assert len(reason) == LABEL_VALUE_MAX_LEN
    assert reason.endswith("do it?")
