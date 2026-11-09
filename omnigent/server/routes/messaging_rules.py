"""Owner routes for the messaging-rules file: ``/v1/messaging-rules``.

Admin-only, and outside the delegated-token path allowlist in
:mod:`omnigent.server.auth`, so a runner or session token can never reach them.
"""

from __future__ import annotations

import asyncio
import os

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.builtins.messaging import (
    POLICY_REGISTRY,
    MessageRoute,
    parse_rules,
    write_text_atomically,
)
from omnigent.runtime import get_caps
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.default_policies import _require_admin
from omnigent.spec.types import FunctionPolicySpec
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.policy_store import PolicyStore

_HANDLER = str(POLICY_REGISTRY[0]["handler"])


def active_rules_policy(store: PolicyStore) -> tuple[str, str] | None:
    """Return ``(policy_name, rules_path)`` of the enabled messaging_rules policy, or ``None``."""
    for policy in store.list_defaults():
        path = (policy.factory_params or {}).get("path")
        if policy.enabled and policy.handler == _HANDLER and isinstance(path, str):
            return policy.name, os.path.expanduser(path)
    for spec in get_caps().default_policies:
        if (
            isinstance(spec, FunctionPolicySpec)
            and spec.function
            and spec.function.path == _HANDLER
        ):
            path = (spec.function.arguments or {}).get("path")
            if isinstance(path, str):
                return spec.name, os.path.expanduser(path)
    return None


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def _validate(text: str):
    try:
        return parse_rules(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise OmnigentError(
            f"Invalid messaging rules: {exc}", code=ErrorCode.INVALID_INPUT
        ) from exc


class _PutBody(BaseModel):
    text: str


class _TestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(alias="from")
    target: str = Field(alias="to")
    text: str | None = None  # unsaved editor text to test instead of the file


def create_messaging_rules_router(
    store: PolicyStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the router; every route needs admin rights in multi-user mode."""
    router = APIRouter()

    async def _path(request: Request) -> tuple[str, str]:
        await _require_admin(request, auth_provider, permission_store)
        found = await asyncio.to_thread(active_rules_policy, store)
        if found is None:
            raise OmnigentError("No enabled messaging_rules policy", code=ErrorCode.NOT_FOUND)
        return found

    @router.get("/messaging-rules")
    async def get_rules(request: Request) -> dict[str, object]:
        """Return the rules file path, its text and whether it parses."""
        policy_name, path = await _path(request)
        text = await asyncio.to_thread(_read_text, path)
        error = None
        try:
            parse_rules(text)
        except (ValueError, yaml.YAMLError) as exc:
            error = str(exc)
        return {
            "policy": policy_name,
            "path": path,
            "text": text,
            "valid": error is None,
            "error": error,
        }

    @router.put("/messaging-rules")
    async def put_rules(request: Request, body: _PutBody) -> dict[str, object]:
        """Replace the rules file after validating it; invalid text is a 400."""
        _, path = await _path(request)
        rules = _validate(body.text)
        await asyncio.to_thread(write_text_atomically, path, body.text)
        return {"path": path, "valid": True, "rule_count": len(rules.rules)}

    @router.post("/messaging-rules/test")
    async def test_pair(request: Request, body: _TestBody) -> dict[str, object]:
        """Return the action and matching rule for one sender -> target pair."""
        _, path = await _path(request)
        text = body.text if body.text is not None else await asyncio.to_thread(_read_text, path)
        decision = _validate(text).match(MessageRoute(body.sender, body.target))
        return {
            "action": decision.action,
            "rule_index": decision.rule_index,
            "reason": decision.reason,
        }

    return router
