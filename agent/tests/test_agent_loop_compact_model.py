"""Phase 3: optional utility tier for `_auto_compact` via VIBE_COMPACT_MODEL.

When VIBE_COMPACT_MODEL is unset, `_auto_compact` must keep using the loop's
main `self.llm` (upstream behavior, unchanged). When set, it must route the
summarization call through a same-provider `ChatLLM(model_name=...)` instance
instead of the main model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.agent.loop as loop_mod
from src.agent.loop import AgentLoop
from src.agent.trace import TraceWriter


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubMainLLM:
    """Stands in for AgentLoop's main self.llm."""

    def __init__(self) -> None:
        self.model_name = "main-model"
        self.chat_calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> _Response:
        self.chat_calls.append(messages)
        return _Response("main-model summary")


class _StubCompactLLM:
    """Stands in for a `ChatLLM(model_name=...)` built for the compact tier."""

    instances: list["_StubCompactLLM"] = []

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name
        self.chat_calls: list[list[dict[str, Any]]] = []
        _StubCompactLLM.instances.append(self)

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> _Response:
        self.chat_calls.append(messages)
        return _Response("compact-model summary")


class _ExplodingChatLLM:
    """Fails the test loudly if ChatLLM is constructed when VIBE_COMPACT_MODEL is unset."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"ChatLLM should not be constructed when VIBE_COMPACT_MODEL is unset "
            f"(args={args}, kwargs={kwargs})"
        )


def _build_agent(llm: Any, tmp_path: Path) -> AgentLoop:
    from src.tools import build_registry
    from src.memory.persistent import PersistentMemory

    pm = PersistentMemory()
    agent = AgentLoop(
        registry=build_registry(persistent_memory=pm, include_shell_tools=False),
        llm=llm,
        event_callback=None,
        max_iterations=1,
        persistent_memory=pm,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent


def _run_compact(agent: AgentLoop, tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace")
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "earlier context"},
        {"role": "user", "content": "large recent context " + ("x" * 100_000)},
    ]
    try:
        agent._auto_compact(messages, tmp_path / "run", trace, iteration=1)
    finally:
        trace.close()


def test_compact_unset_uses_main_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBE_COMPACT_MODEL", raising=False)
    monkeypatch.setattr(loop_mod, "ChatLLM", _ExplodingChatLLM)

    main_llm = _StubMainLLM()
    agent = _build_agent(main_llm, tmp_path)

    _run_compact(agent, tmp_path)

    assert len(main_llm.chat_calls) == 1


def test_compact_set_routes_through_compact_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBE_COMPACT_MODEL", "quick-compact-model")
    _StubCompactLLM.instances = []
    monkeypatch.setattr(loop_mod, "ChatLLM", _StubCompactLLM)

    main_llm = _StubMainLLM()
    agent = _build_agent(main_llm, tmp_path)

    _run_compact(agent, tmp_path)

    assert len(main_llm.chat_calls) == 0, "main model must not be used when VIBE_COMPACT_MODEL is set"
    assert len(_StubCompactLLM.instances) == 1
    compact_instance = _StubCompactLLM.instances[0]
    assert compact_instance.model_name == "quick-compact-model"
    assert len(compact_instance.chat_calls) == 1
