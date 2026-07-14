"""Captured reasoning must keep ONE type across every chunk of a stream.

Regression: ChatOpenAIWithReasoning._capture wrote additional_kwargs
["reasoning_content"] from either `reasoning_content`/`reasoning` (a str) or
MiniMax M3's `reasoning_details` (a typed list). When one stream emitted both
shapes, langchain_core.utils._merge.merge_dicts refused to merge the chunks:

    TypeError: additional_kwargs["reasoning_content"] already exists in this
               message, but with a different type.

That killed the turn inside LangChain's own merge_chat_generation_chunks --
before ChatLLM ever saw the chunks -- and took out ~48% of crypto_committee
swarm runs, 4 committee cycles, and the 72h evidence run's verdict.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk

from src.providers.llm import ChatOpenAIWithReasoning

# One stream, two provider shapes for the same concept.
_STR_DELTA = {"reasoning_content": "Let me weigh the bull case. "}
_LIST_DELTA = {"reasoning_details": [{"type": "reasoning.text", "text": "Now the bear case."}]}


@pytest.fixture
def llm():
    """A MiniMax-configured client; no network, we only drive _capture."""
    return ChatOpenAIWithReasoning(
        model="MiniMax-M3", api_key="test-key", vibe_provider="minimax"
    )


def _chunk(llm, delta: dict) -> AIMessageChunk:
    """Build a chunk exactly as _convert_chunk_to_generation_chunk does."""
    msg = AIMessageChunk(content="")
    llm._capture(delta, msg)
    return msg


def test_str_and_list_reasoning_deltas_merge(llm):
    """The two shapes must merge instead of raising TypeError.

    This is the bug in three lines: a stream carrying both a reasoning_content
    delta and a reasoning_details delta.
    """
    merged = _chunk(llm, _STR_DELTA) + _chunk(llm, _LIST_DELTA)

    reasoning = merged.additional_kwargs["reasoning_content"]
    assert isinstance(reasoning, str)
    # Both deltas' text must survive -- a fix that merely drops one shape would
    # stop the crash while silently losing half the reasoning.
    assert "Let me weigh the bull case." in reasoning
    assert "Now the bear case." in reasoning


def test_capture_always_yields_str(llm):
    """Every provider shape collapses to one canonical type."""
    for delta in (_STR_DELTA, _LIST_DELTA, {"reasoning": "openrouter relays it here"}):
        msg = _chunk(llm, delta)
        assert isinstance(msg.additional_kwargs["reasoning_content"], str), delta


def test_capture_survives_unexpected_shape(llm):
    """An unknown shape must not reintroduce a merge-time type collision.

    The provider contract is not ours to control; anything that is not a str
    must still normalize rather than crash a 72h run months from now.
    """
    msg = _chunk(llm, {"reasoning_details": {"type": "reasoning.text", "text": "dict, not list"}})
    assert isinstance(msg.additional_kwargs["reasoning_content"], str)

    merged = msg + _chunk(llm, _STR_DELTA)
    assert isinstance(merged.additional_kwargs["reasoning_content"], str)


def test_no_reasoning_leaves_key_absent(llm):
    """A turn with no reasoning must omit the key entirely.

    MiniMax rejects an empty/plain reasoning_details with 400 "Mismatch type
    []*open_platform_oai.ReasoningDetail" (see llm.py), so the key must be
    absent rather than "" on turns that carry no reasoning.
    """
    msg = _chunk(llm, {"content": "just an answer"})
    assert "reasoning_content" not in msg.additional_kwargs


def test_long_stream_of_mixed_deltas_merges(llm):
    """The real failure mode: many deltas alternating between both shapes."""
    chunks = [_chunk(llm, _STR_DELTA if i % 2 else _LIST_DELTA) for i in range(10)]

    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c

    assert isinstance(merged.additional_kwargs["reasoning_content"], str)
