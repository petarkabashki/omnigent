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
        ({"spec": True, "view": _list_view()["view"]}, "spec"),
        (_list_view(source={"name": "sessions", "limit": "5"}), "limit"),
        (_list_view(source={"name": "sessions", "params": {"archived": "true"}}), "archived"),
        (_list_view(source={"name": "sessions", "order": "-hub_ref"}), "order"),
        (_list_view(source={"name": "subagents", "params": {"of": ""}}), "of"),
        (_list_view(marks=[{"when": {"and": [{"eval": 1}]}}]), "unknown rule operator"),
        (_list_view(marks=[{"when": {"!": {"var": ["stopped"]}}}]), "var"),
        (_list_view(marks=[{"when": {"var": "__proto__"}}]), "var"),
        (_list_view(item={"title": "$.title", "link": "$.__proto__"}), "link"),
        (_list_view(type="Tree", source={"name": "threads"}), "source"),
    ],
)
def test_invalid_specs_fail_both_the_models_and_the_schema(
    spec: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValidationError) as error:
        parse_spec(spec)
    assert reason in str(error.value)
    assert not jsonschema.Draft202012Validator(json_schema()).is_valid(spec)


def test_an_oversized_spec_is_refused_before_parsing() -> None:
    with pytest.raises(ValueError, match="larger than"):
        parse_spec("x" * (MAX_SPEC_BYTES + 1))


@pytest.mark.parametrize(
    "raw",
    [
        "[" * 60000,
        '{"a":' * 12000,
        '{"spec": 1, "view": {"type": "List", "source": {"name": "roles"}, "item": {"title": "x"},'
        ' "marks": [{"when": {"<": [{"var": "id"}, NaN]}}]}}',
    ],
    ids=["deep-array", "deep-object", "nan"],
)
def test_malformed_json_is_a_value_error(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_spec(raw)


def test_a_decoded_spec_with_nan_is_refused() -> None:
    spec = _list_view(marks=[{"when": {"<": [{"var": "id"}, float("nan")]}}])
    with pytest.raises(ValueError):
        parse_spec(spec)
