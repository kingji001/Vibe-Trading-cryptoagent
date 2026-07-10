"""Phase 1 regression coverage for MiniMax M3 provider hardening.

Drives the MiniMax first-class-provider work: capability flags, Path A
(OpenAI-compatible reasoning_split capture/replay), temperature/top_p
defaulting, Path B (Anthropic-compatible native adapter) selection, and the
``provider doctor`` graceful-degradation contract when no key is configured.

All tests are offline: they patch the underlying client classes or exercise
pure request/response transforms, so no socket is opened.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.context import ContextBuilder
from src.providers.capabilities import get_provider_capabilities
from src.providers.chat import ChatLLM, LLMResponse, ToolCallRequest
from src.providers.llm import (
    ChatOpenAIWithReasoning,
    build_llm,
    minimax_provider_checks,
)


# --------------------------------------------------------------------------- caps
def test_minimax_capabilities_are_path_a_and_path_b_ready() -> None:
    """MiniMax must capture + replay reasoning and expose the M3 split flag."""
    caps = get_provider_capabilities("minimax", "MiniMax-M3")

    assert caps.name == "minimax"
    assert caps.capture_reasoning is True
    assert caps.send_reasoning_content is True
    assert caps.reasoning_split_extra_body is True
    assert caps.native_adapter_package == "langchain-anthropic"


def test_non_minimax_providers_do_not_get_reasoning_split_flag() -> None:
    """The M3-only split flag must not leak onto other providers (byte-identical)."""
    for provider, model in (
        ("deepseek", "deepseek-v4-pro"),
        ("moonshot", "kimi-k2.6"),
        ("openai", "gpt-5.5"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
    ):
        caps = get_provider_capabilities(provider, model)
        assert caps.reasoning_split_extra_body is False


# --------------------------------------------------------------------------- temp
def _capture_build(env: dict[str, str]) -> dict:
    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    captured: dict = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    with patch.dict(os.environ, env, clear=True):
        with patch.object(llm_mod, "ChatOpenAIWithReasoning", _FakeChatOpenAI):
            build_llm()
    return captured


def test_minimax_defaults_temperature_1_and_top_p_when_left_at_zero() -> None:
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "minimax",
            "MINIMAX_API_KEY": "mm-key",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
            "LANGCHAIN_TEMPERATURE": "0.0",
        }
    )

    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95


def test_minimax_explicit_temperature_preserved_and_top_p_not_forced() -> None:
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "minimax",
            "MINIMAX_API_KEY": "mm-key",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
            "LANGCHAIN_TEMPERATURE": "0.7",
        }
    )

    assert captured["temperature"] == 0.7
    assert "top_p" not in captured


def test_minimax_path_a_sends_reasoning_split_extra_body() -> None:
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "minimax",
            "MINIMAX_API_KEY": "mm-key",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
        }
    )

    assert captured["extra_body"] == {"reasoning_split": True}


def test_minimax_thinking_disabled_flows_into_extra_body() -> None:
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "minimax",
            "MINIMAX_API_KEY": "mm-key",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
            "MINIMAX_THINKING": "disabled",
        }
    )

    assert captured["extra_body"]["reasoning_split"] is True
    assert captured["extra_body"]["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- regression
def test_non_minimax_build_is_unchanged() -> None:
    """OpenAI build must not gain top_p / reasoning_split from the MiniMax work."""
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "LANGCHAIN_MODEL_NAME": "gpt-5.5",
        }
    )

    assert "top_p" not in captured
    assert captured["extra_body"] is None


# --------------------------------------------------------------------------- reasoning replay (Path A)
def test_minimax_reasoning_captured_from_reasoning_details() -> None:
    """M3's OpenAI surface returns ``reasoning_details``; capture normalizes it."""
    llm = ChatOpenAIWithReasoning(
        model="MiniMax-M3", api_key="mm-key", vibe_provider="minimax"
    )
    msg = SimpleNamespace(additional_kwargs={})
    llm._capture({"reasoning_details": "STEP-1 then STEP-2"}, msg)

    assert msg.additional_kwargs["reasoning_content"] == "STEP-1 then STEP-2"


def test_minimax_reasoning_replayed_on_turn_2_as_reasoning_details() -> None:
    """Turn-1 reasoning must be re-attached to the assistant turn on turn 2.

    Simulates the ReAct loop: a streamed turn-1 assistant message carries
    reasoning (captured into ``reasoning_content``); the loop replays it via
    ``format_assistant_tool_calls``; the next request payload must re-emit it
    under M3's ``reasoning_details`` field so the model does not re-derive its
    chain of thought from scratch.
    """
    llm = ChatOpenAIWithReasoning(
        model="MiniMax-M3", api_key="mm-key", vibe_provider="minimax"
    )

    # Turn 1: a mocked stream delta from M3 carrying reasoning_details.
    turn1_chunk = SimpleNamespace(additional_kwargs={})
    llm._capture({"reasoning_details": "chain-of-thought-A"}, turn1_chunk)
    reasoning = turn1_chunk.additional_kwargs["reasoning_content"]

    # Loop replays the assistant turn (with tool calls + reasoning) into history.
    tool_call = ToolCallRequest(id="call_1", name="get_time", arguments={})
    assistant_message = ContextBuilder.format_assistant_tool_calls(
        [tool_call], content="", reasoning_content=reasoning
    )
    history = [
        {"role": "user", "content": "what time is it?"},
        assistant_message,
        {"role": "tool", "tool_call_id": "call_1", "name": "get_time", "content": "12:00Z"},
    ]

    payload = llm._get_request_payload(history)
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")

    assert assistant["reasoning_details"] == "chain-of-thought-A"
    assert "reasoning_content" not in assistant


def test_chat_llm_parse_response_preserves_reasoning_for_replay() -> None:
    """ChatLLM._parse_response must surface reasoning so the loop can replay it."""
    ai_message = SimpleNamespace(
        content="answer",
        tool_calls=[],
        additional_kwargs={"reasoning_content": "kept-reasoning"},
        response_metadata={"finish_reason": "stop"},
        usage_metadata=None,
    )
    parsed = ChatLLM._parse_response(ai_message)

    assert isinstance(parsed, LLMResponse)
    assert parsed.reasoning_content == "kept-reasoning"


# --------------------------------------------------------------------------- Path B
def test_minimax_path_b_selected_by_anthropic_base_url() -> None:
    """A ``/anthropic`` base URL selects the native langchain-anthropic adapter."""
    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    captured: dict = {}

    class _FakeChatAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_module = SimpleNamespace(ChatAnthropic=_FakeChatAnthropic)
    env = {
        "LANGCHAIN_PROVIDER": "minimax",
        "MINIMAX_API_KEY": "mm-sub-key",
        "MINIMAX_BASE_URL": "https://api.minimax.io/anthropic",
        "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch.dict(sys.modules, {"langchain_anthropic": fake_module}):
            llm = build_llm()

    assert isinstance(llm, _FakeChatAnthropic)
    assert captured["model"] == "MiniMax-M3"
    assert captured["api_key"] == "mm-sub-key"
    assert captured["base_url"] == "https://api.minimax.io/anthropic"
    # Path B still gets MiniMax temperature/top_p defaulting.
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95


def test_minimax_path_b_builds_real_langchain_anthropic_adapter() -> None:
    """With langchain-anthropic installed, Path B builds a real ChatAnthropic."""
    pytest.importorskip("langchain_anthropic")
    from langchain_anthropic import ChatAnthropic

    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    env = {
        "LANGCHAIN_PROVIDER": "minimax",
        "MINIMAX_API_KEY": "mm-sub-key",
        "MINIMAX_BASE_URL": "https://api.minimax.io/anthropic",
        "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
    }
    with patch.dict(os.environ, env, clear=True):
        llm = build_llm()

    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "MiniMax-M3"


def test_minimax_path_b_missing_package_raises_install_hint() -> None:
    """Path B without langchain-anthropic must raise a clear install hint."""
    import builtins
    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object):
        if name == "langchain_anthropic":
            raise ModuleNotFoundError("No module named 'langchain_anthropic'")
        return real_import(name, *args, **kwargs)

    env = {
        "LANGCHAIN_PROVIDER": "minimax",
        "MINIMAX_API_KEY": "mm-sub-key",
        "MINIMAX_BASE_URL": "https://api.minimax.io/anthropic",
        "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch.dict(sys.modules, {"langchain_anthropic": None}):
            with patch.object(builtins, "__import__", _fake_import):
                with pytest.raises(RuntimeError, match="langchain-anthropic"):
                    build_llm()


def test_minimax_path_a_selected_by_default_v1_base_url() -> None:
    """The default /v1 base URL keeps the OpenAI-compatible Path A."""
    captured = _capture_build(
        {
            "LANGCHAIN_PROVIDER": "minimax",
            "MINIMAX_API_KEY": "mm-key",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
        }
    )
    # Reaches the ChatOpenAI (Path A) constructor, not the anthropic adapter.
    assert captured["model"] == "MiniMax-M3"
    assert captured["extra_body"] == {"reasoning_split": True}


# --------------------------------------------------------------------------- doctor
def test_minimax_doctor_degrades_gracefully_without_key() -> None:
    """provider doctor must report 'no key' as degraded, never a hard failure."""
    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    env = {
        "LANGCHAIN_PROVIDER": "minimax",
        "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
        "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
    }
    with patch.dict(os.environ, env, clear=True):
        checks = minimax_provider_checks()

    assert checks["api_key"]["MINIMAX_API_KEY"] == "unset"
    assert checks["status"] == "degraded"
    assert "no" in checks["note"].lower()
    assert "skipped" in checks["endpoint_reachable"]
    assert "skipped" in checks["reasoning_round_trip"]
    assert checks["path"].startswith("A")


def test_minimax_doctor_reports_path_b_for_anthropic_endpoint() -> None:
    import src.providers.llm as llm_mod

    llm_mod._dotenv_loaded = True
    env = {
        "LANGCHAIN_PROVIDER": "minimax",
        "MINIMAX_BASE_URL": "https://api.minimax.io/anthropic",
        "LANGCHAIN_MODEL_NAME": "MiniMax-M3",
    }
    with patch.dict(os.environ, env, clear=True):
        checks = minimax_provider_checks()

    assert checks["path"].startswith("B")
