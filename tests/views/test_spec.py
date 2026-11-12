"""Spec v1: the published schema matches the models, and bad specs fail with a reason."""

from __future__ import annotations

import json

import jsonschema
import pytest
from pydantic import ValidationError

from omnigent.views.spec import MAX_SPEC_BYTES, SCHEMA_PATH, json_schema, parse_spec


def _list_view(**view: object) -> dict[str, object]:
    base = {
        "type": "List",
        "source": {"name": "sessions", "params": {"role": "Product"}},
        "item": {"title": "$.title", "link": "$.hub_ref", "badges": [{"text": "$.harness"}]},
        "marks": [{"when": {"==": [{"var": "stopped"}, True]}, "tone": "muted"}],
    }
    return {"spec": 1, "view": {**base, **view}}


def test_the_published_schema_matches_the_models() -> None:
    published = json.loads(SCHEMA_PATH.read_text())
    assert published == json_schema(), "run: python -m omnigent.views.spec"
    jsonschema.Draft202012Validator.check_schema(published)


@pytest.mark.parametrize(
    "spec",
    [
        _list_view(),
        {
            "spec": 1,
            "view": {
                "type": "Table",
                "source": {"name": "threads", "params": {"kind": "group"}, "limit": 200},
                "columns": [{"header": "Thread", "value": "$.title", "link": "$.hub_ref"}],
            },
        },
        {
            "spec": 1,
            "view": {
                "type": "Tree",
                "source": {"name": "subagents", "params": {"of": "abc", "depth": 3}},
                "item": {"title": "$.title"},
            },
        },
    ],
)
def test_valid_specs_pass_both_the_models_and_the_schema(spec: dict[str, object]) -> None:
    parse_spec(spec)
    jsonschema.validate(spec, json.loads(SCHEMA_PATH.read_text()))


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (_list_view(type="Chart"), "type"),
        (_list_view(marks=[{"when": {"eval": "1"}}]), "unknown rule operator"),
        (_list_view(item={"title": "$.title", "link": "https://example.com"}), "link"),
        (_list_view(item={"title": "$.title", "html": "<b>x</b>"}), "html"),
        (_list_view(source={"name": "sessions", "params": {"sql": "1"}}), "sql"),
        (_list_view(source={"name": "subagents", "params": {}}), "of"),
        ({"spec": 2, "view": _list_view()["view"]}, "spec"),
    ],
)
def test_invalid_specs_fail_with_a_reason(spec: dict[str, object], reason: str) -> None:
    with pytest.raises(ValidationError) as error:
        parse_spec(spec)
    assert reason in str(error.value)


def test_an_oversized_spec_is_refused_before_parsing() -> None:
    with pytest.raises(ValueError, match="larger than"):
        parse_spec("x" * (MAX_SPEC_BYTES + 1))
