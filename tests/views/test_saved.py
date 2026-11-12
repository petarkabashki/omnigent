"""Saved view params, binding and the YAML exchange format."""

from __future__ import annotations

import pytest

from omnigent.views import saved

SPEC = {
    "spec": 1,
    "view": {
        "type": "List",
        "source": {"name": "sessions", "params": {"search": "$text", "archived": "$closed"}},
        "item": {"title": "$.title"},
    },
}
PARAMS = {"text": {"type": "string"}, "closed": {"type": "bool", "default": False}}


def test_bind_substitutes_values_and_falls_back_to_defaults() -> None:
    bound = saved.bind(SPEC, saved.parse_params(PARAMS), {"text": "deploy"})

    assert bound.view.source.params.search == "deploy"
    assert bound.view.source.params.archived is False
    assert SPEC["view"]["source"]["params"]["search"] == "$text"


@pytest.mark.parametrize(
    ("params", "values", "message"),
    [
        (PARAMS, {}, "'text' needs a value"),
        ({"closed": {"type": "bool"}}, {"closed": True}, r"undeclared param \$text"),
        ({**PARAMS, "text": {"type": "int"}}, {"text": 3}, "search"),
    ],
)
def test_bind_refuses_missing_undeclared_and_mistyped_values(
    params: dict, values: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        saved.bind(SPEC, saved.parse_params(params), values)


def test_check_binds_samples_so_a_wrong_param_type_fails_at_save() -> None:
    assert saved.check(SPEC, saved.parse_params(PARAMS)) == SPEC
    with pytest.raises(ValueError, match="archived"):
        saved.check(SPEC, saved.parse_params({**PARAMS, "closed": {"type": "string"}}))


@pytest.mark.parametrize(
    "params",
    [
        {"1bad": {"type": "string"}},
        {"name\n": {"type": "string"}},
        {"n": {"type": "int", "default": True}},
        {"n": {"type": "string", "enum": ["a"], "default": "b"}},
        {"n": {"type": "string", "sql": "1"}},
        {f"p{index}": {"type": "string"} for index in range(saved.MAX_PARAMS + 1)},
    ],
)
def test_parse_params_refuses_bad_declarations(params: dict) -> None:
    with pytest.raises(ValueError):
        saved.parse_params(params)


def test_coerce_types_query_values_and_checks_the_enum() -> None:
    assert saved.coerce(saved.ParamDecl(type="bool"), "true", "b") is True
    assert saved.coerce(saved.ParamDecl(type="int"), "7", "n") == 7
    assert saved.coerce(saved.ParamDecl(type="int"), "-7", "n") == -7
    for decl, raw in [
        (saved.ParamDecl(type="bool"), "1"),
        (saved.ParamDecl(type="int"), "x"),
        *((saved.ParamDecl(type="int"), raw) for raw in (" 7", "+7", "0_7", "\u0667", "1.0", "")),
        (saved.ParamDecl(type="string", enum=["a"]), "b"),
    ]:
        with pytest.raises(ValueError):
            saved.coerce(decl, raw, "p")


def test_yaml_round_trips_and_refuses_aliases_and_bool_versions() -> None:
    text = saved.to_yaml("Stuck", "été", PARAMS, SPEC)
    document = saved.from_yaml(text)

    assert (document.name, document.description, document.params, document.spec) == (
        "Stuck",
        "été",
        PARAMS,
        SPEC,
    )
    with pytest.raises(ValueError, match="aliases"):
        saved.from_yaml("version: 1\nname: &n x\ndescription: *n\nspec: {}\n")
    with pytest.raises(ValueError):
        saved.from_yaml(text.replace("version: 1", "version: true"))
    with pytest.raises(ValueError, match="mapping"):
        saved.from_yaml("- 1\n")
