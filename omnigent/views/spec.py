"""Agent-built view spec, version 1.

A spec is data, never code: a view node names a hub source (resolved per user by
``/v1/views/resolve``), binds row fields by ``$.path`` and decides marks and badges with a
small JSONLogic subset. ``spec_v1.schema.json`` is generated from these models.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SPEC_VERSION = 1
MAX_SPEC_BYTES = 64 * 1024
MAX_ROWS = 500
SCHEMA_PATH = Path(__file__).with_name("spec_v1.schema.json")

# A row field path such as "$.title" or "$.hub_ref"; any other string is literal text.
# Field names start with a letter, so "__proto__" and the like can never be bound.
PATH_PATTERN = r"^\$(\.[A-Za-z][A-Za-z0-9_]*)+$"
PATH_FIELD_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$"
RULE_OPERATORS = ("var", "==", "!=", "<", "<=", ">", ">=", "and", "or", "!", "in")
Tone = Literal["neutral", "muted", "info", "success", "warning", "danger"]


def _check_rule(rule: Any, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("rule is nested too deeply")
    if isinstance(rule, list):
        for part in rule:
            _check_rule(part, depth + 1)
        return
    if not isinstance(rule, dict):
        return
    if len(rule) != 1:
        raise ValueError("a rule object has exactly one operator")
    [(operator, argument)] = rule.items()
    if operator not in RULE_OPERATORS:
        raise ValueError(f"unknown rule operator {operator!r}; use one of {RULE_OPERATORS}")
    if operator == "var":
        if not isinstance(argument, str) or not re.fullmatch(PATH_FIELD_PATTERN, argument):
            raise ValueError("'var' takes a row field name, e.g. {'var': 'stopped'}")
        return
    _check_rule(argument, depth + 1)


Rule = Annotated[
    dict[str, Any],
    Field(
        description=(
            "JSONLogic subset: one operator per object, e.g. {'==': [{'var': 'kind'}, 'group']}."
        ),
        json_schema_extra={
            "minProperties": 1,
            "maxProperties": 1,
            "propertyNames": {"enum": list(RULE_OPERATORS)},
        },
    ),
]
Path_ = Annotated[str, Field(pattern=PATH_PATTERN, description="Row field path, e.g. '$.title'.")]
Text = Annotated[
    str,
    Field(max_length=500, description="A '$.path' binds a row field; other text is shown as is."),
]


class _Strict(BaseModel):
    # strict: a spec is stored and returned as given, so it must match the published schema.
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionsParams(_Strict):
    project: str | None = None
    kind: Literal["default", "sub_agent", "any"] = "default"
    search: str | None = None
    archived: bool = False
    host: str | None = None
    role: str | None = None


class ThreadsParams(_Strict):
    kind: Literal["group", "role_thread"] | None = None
    member: str | None = None
    state: str | None = None


class RolesParams(_Strict):
    handle: str | None = None
    held: bool | None = None


class SubagentsParams(_Strict):
    of: str = Field(min_length=1, description="Hub session id whose sub-agent tree to list.")
    depth: int = Field(default=3, ge=1, le=5)


def _order_field(*fields: str) -> Any:
    """``order`` is a sortable row field, ``-`` prefixed for descending."""
    choices = [f"{sign}{name}" for name in fields for sign in ("", "-")]
    return Literal[tuple(choices)]  # type: ignore[valid-type]


SESSION_ORDER_FIELDS = (
    "id", "title", "kind", "harness", "spawn_kind", "project", "host", "stopped",
    "stop_reason", "last_event_ts",
)  # fmt: skip
THREAD_ORDER_FIELDS = ("id", "kind", "title", "state", "messages", "last_event_ts")
ROLE_ORDER_FIELDS = ("id", "handle", "permanent", "last_event_ts")


class _Source(_Strict):
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)


class SessionsSource(_Source):
    name: Literal["sessions"]
    order: _order_field(*SESSION_ORDER_FIELDS) = "-last_event_ts"  # type: ignore[valid-type]
    params: SessionsParams = SessionsParams()


class ThreadsSource(_Source):
    name: Literal["threads"]
    order: _order_field(*THREAD_ORDER_FIELDS) = "-last_event_ts"  # type: ignore[valid-type]
    params: ThreadsParams = ThreadsParams()


class RolesSource(_Source):
    name: Literal["roles"]
    order: _order_field(*ROLE_ORDER_FIELDS) = "-last_event_ts"  # type: ignore[valid-type]
    params: RolesParams = RolesParams()


class SubagentsSource(_Source):
    name: Literal["subagents"]
    order: _order_field(*SESSION_ORDER_FIELDS, "depth") = "depth"  # type: ignore[valid-type]
    params: SubagentsParams


Source = Annotated[
    SessionsSource | ThreadsSource | RolesSource | SubagentsSource, Field(discriminator="name")
]


class _Ruled(_Strict):
    @field_validator("when", check_fields=False)
    @classmethod
    def _valid_rule(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _check_rule(value)
        return value


class Badge(_Ruled):
    text: Text
    tone: Tone = "neutral"
    when: Rule | None = None


class Mark(_Ruled):
    when: Rule
    tone: Tone = "warning"
    label: str | None = Field(default=None, max_length=80)


class Item(_Strict):
    title: Text
    subtitle: Text | None = None
    link: Path_ | None = Field(default=None, description="Path to a hub_ref, e.g. '$.hub_ref'.")
    badges: list[Badge] = Field(default_factory=list, max_length=8)


class Column(_Strict):
    header: str = Field(max_length=80)
    value: Text
    link: Path_ | None = None
    badges: list[Badge] = Field(default_factory=list, max_length=8)


class _View(_Strict):
    source: Source
    marks: list[Mark] = Field(default_factory=list, max_length=8)
    empty: str | None = Field(default=None, max_length=200, description="Text when no rows match.")


class ListView(_View):
    type: Literal["List"]
    item: Item


class TableView(_View):
    type: Literal["Table"]
    columns: list[Column] = Field(min_length=1, max_length=12)


class TreeView(_View):
    type: Literal["Tree"]
    # spawned_by links sessions, so a tree reads only session rows.
    source: Annotated[SessionsSource | SubagentsSource, Field(discriminator="name")]
    edge: Literal["spawned_by"] = "spawned_by"
    item: Item


View = Annotated[ListView | TableView | TreeView, Field(discriminator="type")]


class ViewSpec(_Strict):
    """One agent-built view."""

    model_config = ConfigDict(extra="forbid", title="Omnigent view spec v1")
    spec: Literal[1]
    title: str | None = Field(default=None, max_length=120)
    view: View

    @field_validator("spec", mode="before")
    @classmethod
    def _not_bool(cls, value: Any) -> Any:
        # Literal[1] compares by equality, so True would otherwise pass.
        if isinstance(value, bool):
            raise ValueError("spec must be the integer 1")
        return value


SPEC_ADAPTER = TypeAdapter(ViewSpec)


def parse_spec(raw: Any) -> ViewSpec:
    """Validate a spec given as a JSON string or an already decoded object."""
    if isinstance(raw, str):
        if len(raw.encode()) > MAX_SPEC_BYTES:
            raise ValueError(f"spec is larger than {MAX_SPEC_BYTES} bytes")
        raw = json.loads(raw)
    elif len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_SPEC_BYTES:
        raise ValueError(f"spec is larger than {MAX_SPEC_BYTES} bytes")
    return SPEC_ADAPTER.validate_python(raw)


def _rule_defs() -> dict[str, Any]:
    """Recursive rule grammar; nesting depth (16) is enforced only by ``parse_spec``."""
    operand = {"$ref": "#/$defs/RuleOperand"}
    operators: dict[str, Any] = dict.fromkeys(RULE_OPERATORS, operand)
    operators["var"] = {"type": "string", "pattern": PATH_FIELD_PATTERN}
    return {
        "Rule": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "properties": operators,
            "additionalProperties": False,
        },
        "RuleOperand": {
            "anyOf": [
                {"type": ["string", "number", "boolean", "null"]},
                {"type": "array", "items": operand},
                {"$ref": "#/$defs/Rule"},
            ]
        },
    }


def _link_rules(node: Any) -> Any:
    """Point every inline rule field at the recursive ``Rule`` definition."""
    if isinstance(node, dict):
        if node.get("propertyNames") == {"enum": list(RULE_OPERATORS)}:
            return {"$ref": "#/$defs/Rule", "description": node.get("description", "")}
        return {key: _link_rules(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_link_rules(value) for value in node]
    return node


def json_schema() -> dict[str, Any]:
    """The published JSON Schema for spec v1."""
    schema = _link_rules(SPEC_ADAPTER.json_schema())
    schema.setdefault("$defs", {}).update(_rule_defs())
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://omnigent.invalid/views/spec_v1.schema.json"
    return schema


def write_schema() -> None:
    """Regenerate ``spec_v1.schema.json`` from the models."""
    SCHEMA_PATH.write_text(json.dumps(json_schema(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    write_schema()
