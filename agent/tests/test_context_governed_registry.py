"""ContextBuilder must work against a governed registry, not just a raw one.

Regression: SessionService wraps every registry in GovernedToolRegistry
(src/session/service.py), but ContextBuilder reached into the raw
ToolRegistry's private ``_tools`` dict. The wrapper only preserves the
public surface, so every session-runtime agent turn (scheduled jobs, API
sessions, channels) died with AttributeError while building the system
prompt -- before the first LLM call, and silently, because _run_attempt
swallowed the exception.
"""

from __future__ import annotations

import pytest

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import BaseTool, ToolRegistry
from src.governance.manifest import ToolSurface
from src.governance.runtime import govern_registry


class _EchoTool(BaseTool):
    name = "echo"
    description = "Echo a message back."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string", "description": "text to echo"}},
        "required": ["message"],
    }

    def execute(self, **kwargs: object) -> str:
        return str(kwargs.get("message", ""))


def _registry_with_echo() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


def _builder(registry) -> ContextBuilder:
    return ContextBuilder(registry=registry, memory=WorkspaceMemory())


@pytest.fixture
def governed():
    """A registry wrapped exactly as SessionService._run_with_agent wraps it."""
    return govern_registry(_registry_with_echo(), surface=ToolSurface.LOCAL_API)


def test_build_system_prompt_works_with_governed_registry(governed):
    """The system prompt must build when the registry is policy-wrapped."""
    prompt = _builder(governed).build_system_prompt("hello")

    # The tool must actually be described, not merely counted -- a fix that
    # silently yielded zero tools would strip the agent of its tools instead
    # of crashing, which is worse.
    assert "echo" in prompt
    assert "Echo a message back." in prompt


def test_tool_descriptions_match_between_raw_and_governed():
    """Governing a registry must not change what the agent is told it has."""
    raw = _registry_with_echo()
    wrapped = govern_registry(_registry_with_echo(), surface=ToolSurface.LOCAL_API)

    assert _builder(wrapped)._format_tool_descriptions() == _builder(raw)._format_tool_descriptions()


def test_tool_count_matches_between_raw_and_governed():
    """The advertised tool count must survive the governance wrapper."""
    raw = _registry_with_echo()
    wrapped = govern_registry(_registry_with_echo(), surface=ToolSurface.LOCAL_API)

    assert "1 tools" in _builder(wrapped).build_system_prompt()
    assert "1 tools" in _builder(raw).build_system_prompt()


def test_context_builder_does_not_touch_private_registry_internals(governed):
    """ContextBuilder must go through the public surface only.

    Guards the actual root cause: any reach into ``_tools`` (or another
    private attribute the wrapper does not forward) reintroduces the bug for
    every governed surface.
    """
    assert not hasattr(governed, "_tools")  # the wrapper genuinely lacks it

    builder = _builder(governed)
    builder.build_system_prompt("hello")
    builder.build_messages("hello")
