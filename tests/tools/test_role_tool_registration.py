"""``sys_role_take`` / ``sys_role_release`` are registered wherever ``sys_role_list`` is."""

from __future__ import annotations

import pytest

from omnigent.harnesses.antigravity_native import bridge as antigravity_bridge
from omnigent.harnesses.cursor_native import bridge as cursor_bridge
from omnigent.runner import tool_dispatch
from omnigent.tools.builtins import SysRoleListTool, SysRoleReleaseTool, SysRoleTakeTool

ROLE_TOOLS = (SysRoleTakeTool.name(), SysRoleReleaseTool.name())


@pytest.mark.parametrize(
    "registered",
    [
        pytest.param(tool_dispatch._SESSION_QUERY_TOOLS, id="runner-dispatch"),
        pytest.param(cursor_bridge._CURSOR_AUTO_APPROVE_TOOLS, id="cursor-bridge"),
        pytest.param(antigravity_bridge._AGY_ENABLED_TOOLS, id="antigravity-bridge"),
    ],
)
def test_role_change_tools_sit_beside_role_list(registered) -> None:
    assert SysRoleListTool.name() in registered
    assert set(ROLE_TOOLS) <= set(registered)


def test_role_take_schema_requires_only_a_role_never_a_session_id() -> None:
    parameters = SysRoleTakeTool().get_schema()["function"]["parameters"]
    assert parameters["required"] == ["role"] and set(parameters["properties"]) == {"role"}
    assert SysRoleReleaseTool().get_schema()["function"]["parameters"]["properties"] == {}
