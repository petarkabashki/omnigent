"""Saved views: declared params, ``$name`` binding and the YAML exchange format.

A saved view is a spec v1 plus named inputs. A spec uses a param only as a whole
source-param value, e.g. ``{"host": "$host"}``; there is no string interpolation and
rules never see params. Binding replaces each placeholder with a typed value and
validates the result like any spec, so a stored view always binds to a valid spec.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Literal

import yaml
from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from omnigent.views.spec import ViewSpec, _Strict, parse_spec

SAVED_VIEW_FORMAT_VERSION = 1
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
MAX_PARAMS = 12
MAX_ENUM_VALUES = 50
MAX_PARAM_VALUE_CHARS = 500
PARAM_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,39}$"
_PLACEHOLDER = re.compile(r"^\$([A-Za-z][A-Za-z0-9_]{0,39})$")

ParamValue = str | bool | int
ParamType = Literal["string", "bool", "int"]
_PYTHON_TYPES: dict[str, type] = {"string": str, "bool": bool, "int": int}
# Values used to check a spec when a param has neither a default nor an enum.
_SAMPLES: dict[str, ParamValue] = {"string": "x", "bool": False, "int": 1}
_INT_QUERY = re.compile(r"-?[0-9]{1,18}")


class ParamDecl(_Strict):
    """One declared input of a saved view."""

    type: ParamType
    default: ParamValue | None = None
    enum: list[ParamValue] | None = Field(default=None, min_length=1, max_length=MAX_ENUM_VALUES)
    description: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _typed(self) -> ParamDecl:
        for value in [self.default, *(self.enum or [])]:
            if value is not None and not _is_type(value, self.type):
                raise ValueError(f"{value!r} is not a {self.type}")
            if isinstance(value, str) and len(value) > MAX_PARAM_VALUE_CHARS:
                raise ValueError(f"param values are at most {MAX_PARAM_VALUE_CHARS} characters")
        if self.enum is not None and self.default is not None and self.default not in self.enum:
            raise ValueError(f"default {self.default!r} is not one of the enum values")
        return self


def _is_type(value: Any, param_type: str) -> bool:
    # bool is an int subclass in Python, so int params refuse it explicitly.
    if param_type == "int" and isinstance(value, bool):
        return False
    return isinstance(value, _PYTHON_TYPES[param_type])


_PARAMS_ADAPTER: TypeAdapter[dict[str, ParamDecl]] = TypeAdapter(dict[str, ParamDecl])


def parse_params(raw: Any) -> dict[str, ParamDecl]:
    """Validate a ``params`` declaration; every failure is a ``ValueError``."""
    if raw is None:
        return {}
    params = _PARAMS_ADAPTER.validate_python(raw)
    if len(params) > MAX_PARAMS:
        raise ValueError(f"a view declares at most {MAX_PARAMS} params")
    for name in params:
        if not re.fullmatch(PARAM_NAME_PATTERN, name):
            raise ValueError(f"param name {name!r} must match {PARAM_NAME_PATTERN}")
    return params


def _source_params(spec: dict[str, Any]) -> dict[str, Any]:
    view = spec.get("view")
    source = view.get("source") if isinstance(view, dict) else None
    params = source.get("params") if isinstance(source, dict) else None
    return params if isinstance(params, dict) else {}


def placeholders(spec: dict[str, Any]) -> dict[str, str]:
    """Source-param field to param name, for every ``$name`` value in the spec."""
    found: dict[str, str] = {}
    for field, value in _source_params(spec).items():
        match = _PLACEHOLDER.fullmatch(value) if isinstance(value, str) else None
        if match:
            found[field] = match.group(1)
    return found


def bind(
    spec: dict[str, Any], params: dict[str, ParamDecl], values: dict[str, ParamValue]
) -> ViewSpec:
    """Replace each placeholder with its value and validate the bound spec.

    :param spec: Stored spec, e.g. ``{"spec": 1, "view": {..., "source": {"name":
        "sessions", "params": {"host": "$host"}}}}``.
    :param params: The view's declared params.
    :param values: Typed values by param name; a missing one falls back to its default.
    :returns: The validated, bound spec.
    :raises ValueError: A placeholder names an undeclared param, a param has no value
        and no default, or the bound spec is invalid.
    """
    bound = copy.deepcopy(spec)
    source_params = _source_params(bound)
    for field, name in placeholders(spec).items():
        if name not in params:
            raise ValueError(f"source param {field!r} uses undeclared param ${name}")
        value = values.get(name, params[name].default)
        if value is None:
            raise ValueError(f"param {name!r} needs a value")
        source_params[field] = value
    return parse_spec(bound)


def check(spec: Any, params: dict[str, ParamDecl]) -> dict[str, Any]:
    """Validate a spec for storage by binding sample values; returns the spec as a dict.

    The stored spec keeps its placeholders. Each param binds its default, else its first
    enum value, else a sample of its type, so a param whose type the field refuses fails.
    """
    if isinstance(spec, str):
        spec = parse_spec_object(spec)
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    samples: dict[str, ParamValue] = {}
    sampled: list[str] = []
    for name, decl in params.items():
        if decl.default is not None:
            samples[name] = decl.default
        elif decl.enum:
            samples[name] = decl.enum[0]
        else:
            samples[name] = _SAMPLES[decl.type]
            sampled.append(name)
    try:
        bind(spec, params, samples)
    except RecursionError as error:
        raise ValueError("spec is nested too deeply") from error
    except ValueError as error:
        if not sampled:
            raise
        names = ", ".join(sampled)
        raise ValueError(
            f"{_reasons(error)} (checked with sample values for {names}; "
            "give those params a default)"
        ) from error
    return spec


def _reasons(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:5]
        )
    return str(error)


def parse_spec_object(raw: str) -> Any:
    """A spec given as a JSON string, decoded without validation."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("spec is not valid JSON") from error


def coerce(decl: ParamDecl, raw: str, name: str) -> ParamValue:
    """A query-string value converted to the param's type, e.g. ``"true"`` to ``True``."""
    value: ParamValue
    if decl.type == "bool":
        if raw not in ("true", "false"):
            raise ValueError(f"param {name!r} must be true or false")
        value = raw == "true"
    elif decl.type == "int":
        # int() alone accepts " 7", "+7", "0_7" and non-ASCII digits.
        if not _INT_QUERY.fullmatch(raw):
            raise ValueError(f"param {name!r} must be an integer")
        value = int(raw)
    else:
        value = raw
    if decl.enum is not None and value not in decl.enum:
        raise ValueError(f"param {name!r} must be one of {decl.enum}")
    return value


class SavedViewDocument(_Strict):
    """The YAML exchange format, ``{version: 1, name, description, params, spec}``."""

    version: Literal[1]
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    params: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any]

    @field_validator("version", mode="before")
    @classmethod
    def _not_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("version must be the integer 1")
        return value


def to_yaml(name: str, description: str, params: dict[str, Any], spec: dict[str, Any]) -> str:
    """Serialize a saved view to the YAML exchange format."""
    document = {
        "version": SAVED_VIEW_FORMAT_VERSION,
        "name": name,
        "description": description,
        "params": params,
        "spec": spec,
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def from_yaml(text: str) -> SavedViewDocument:
    """Parse the YAML exchange format; every failure is a ``ValueError``."""
    try:
        # Aliases could expand a small document into a huge spec; a view never needs them.
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text, yaml.SafeLoader)):
            raise ValueError("YAML anchors and aliases are not allowed")
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"not valid YAML: {error}") from error
    except RecursionError as error:
        raise ValueError("YAML is nested too deeply") from error
    if not isinstance(raw, dict):
        raise ValueError("a saved view document must be a mapping")
    try:
        return SavedViewDocument.model_validate(raw)
    except RecursionError as error:
        raise ValueError("YAML is nested too deeply") from error
