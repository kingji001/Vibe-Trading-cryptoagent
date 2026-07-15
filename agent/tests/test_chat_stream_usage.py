"""Streamed LLM calls must report token usage.

Regression: AgentLoop runs every turn through ``ChatLLM.stream_chat``, but the
stream was opened without ``stream_usage``, so the provider sent no usage block
and ``_record_llm_usage`` (src/agent/loop.py) counted the call as zero. Every
llm_usage.json read "calls": 0 / "total_tokens": 0 while real tokens burned --
token spend on an unattended 72h run was silently unauditable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

from src.providers.chat import ChatLLM

_USAGE = {"input_tokens": 185, "output_tokens": 81, "total_tokens": 266}


class _RecordingLLM:
    """Stub standing in for ChatOpenAIWithReasoning."""

    def __init__(self) -> None:
        self.stream_kwargs: dict = {}

    def stream(self, messages, **kwargs):
        self.stream_kwargs = kwargs
        yield AIMessageChunk(content="PONG", usage_metadata=_USAGE)

    def bind_tools(self, tools):
        return self


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(ChatLLM, "__init__", lambda self: None)
    chat = ChatLLM()
    chat._llm = _RecordingLLM()
    chat.model_name = "MiniMax-M3"
    return chat


def test_stream_chat_requests_usage_from_provider(llm):
    """The stream must be opened with stream_usage so usage is sent back."""
    llm.stream_chat([{"role": "user", "content": "ping"}])

    assert llm._llm.stream_kwargs.get("stream_usage") is True


def test_stream_chat_propagates_usage_metadata(llm):
    """Usage reported on the stream must survive onto the LLMResponse.

    This is what _record_llm_usage reads; None here means llm_usage.json
    stays at zero and the run looks free.
    """
    response = llm.stream_chat([{"role": "user", "content": "ping"}])

    assert response.usage_metadata is not None, "usage lost between stream and LLMResponse"
    assert response.usage_metadata["total_tokens"] == 266
