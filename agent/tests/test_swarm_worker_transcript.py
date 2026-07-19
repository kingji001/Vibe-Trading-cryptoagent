"""Tests for opt-in worker transcript persistence on normal completion.

By default the worker persists a full message transcript ONLY on the
iteration-limit / timeout failure paths, so a normal (successful) run leaves
no prompt-level evidence of what the worker actually saw. Setting
``VIBE_SWARM_PERSIST_TRANSCRIPTS`` truthy opts a run into persisting the full
transcript on normal completion too, so an auditor can prove the identity
anchor / grounding block sat inside a live prompt and grep the tool-call
messages. Default OFF because 12 committee runs/day x 13 workers of full
transcripts is real disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import src.providers.backoff as backoff_mod
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.models import SwarmAgentSpec, SwarmTask, WorkerResult
import src.swarm.worker as worker_mod
from src.swarm.worker import run_worker

FINAL_TEXT = (
    "# BTC-USDT — Short-Term View\n\n"
    "Spot 81,704.6 (2026-05-05). 7d range 77,750-82,842.\n\n"
    "**Recommendation: accumulate on dips to 79k; invalidation below 77.5k.**"
)

GROUNDING_BLOCK = (
    "## Ground Truth (verified recent data — prefer over training data)\n\n"
    "IDENTITY-ANCHOR: BTC-USDT is spot Bitcoin priced in USDT on OKX.\n"
    "Last close 81,704.6 as of 2026-05-05."
)


class _EmptyRegistry:
    def get_definitions(self) -> list[dict]:
        return []

    def execute(self, name: str, args: dict) -> str:
        return "ok"

    def get(self, name: str):
        return None


class _ScriptedChatLLM:
    """Scripted ChatLLM that returns queued responses in order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *args, **kwargs) -> "_ScriptedChatLLM":
        return self

    def stream_chat(
        self, messages, tools=None, on_text_chunk=None, timeout=None
    ) -> LLMResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content=FINAL_TEXT)


def _run(
    monkeypatch,
    tmp_path: Path,
    llm: _ScriptedChatLLM,
    max_iterations: int = 5,
) -> WorkerResult:
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    agent = SwarmAgentSpec(
        id="analyst",
        role="Synthesis analyst",
        system_prompt="You synthesize upstream findings.",
        tools=[],
        skills=[],
        max_iterations=max_iterations,
        timeout_seconds=60,
    )
    task = SwarmTask(id="t1", agent_id="analyst", prompt_template="Summarize.")
    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _EmptyRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", llm),
    ):
        return run_worker(
            agent_spec=agent,
            task=task,
            upstream_summaries={},
            user_vars={},
            run_dir=tmp_path,
            grounding_block=GROUNDING_BLOCK,
        )


def _tool_then_final() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="searching...",
            tool_calls=[
                ToolCallRequest(id="tc0", name="web_search", arguments={"q": "btc"})
            ],
        ),
        LLMResponse(content=FINAL_TEXT),
    ]


def _transcript_path(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "analyst" / "messages.json"


def test_flag_off_no_transcript_on_success(monkeypatch, tmp_path):
    """Default (flag unset): a successful run persists NO transcript file."""
    monkeypatch.delenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", raising=False)
    llm = _ScriptedChatLLM(_tool_then_final())

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "completed"
    assert not _transcript_path(tmp_path).exists()


def test_flag_off_explicit_falsy_no_transcript(monkeypatch, tmp_path):
    """An explicit falsy value ("0") also suppresses transcript persistence."""
    monkeypatch.setenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", "0")
    llm = _ScriptedChatLLM(_tool_then_final())

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "completed"
    assert not _transcript_path(tmp_path).exists()


def test_flag_on_persists_transcript_on_success(monkeypatch, tmp_path):
    """Flag on: a successful run persists the full transcript with the system
    prompt (grounding / identity anchor), the tool-call messages, roles, and
    the final assistant text."""
    monkeypatch.setenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", "1")
    llm = _ScriptedChatLLM(_tool_then_final())

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "completed"
    path = _transcript_path(tmp_path)
    assert path.exists()

    transcript = json.loads(path.read_text(encoding="utf-8"))
    roles = [m.get("role") for m in transcript]
    # system + user prompt present, per-message roles preserved
    assert transcript[0]["role"] == "system"
    assert transcript[1]["role"] == "user"
    assert "tool" in roles
    assert "assistant" in roles

    # Auditor can grep the identity anchor line inside a live prompt.
    system_content = transcript[0]["content"]
    assert "IDENTITY-ANCHOR" in system_content
    assert "Ground Truth" in system_content

    # Tool-call evidence and the final assistant text are both present.
    blob = path.read_text(encoding="utf-8")
    assert "web_search" in blob
    assert "Recommendation: accumulate" in blob


def test_flag_on_truthy_variants(monkeypatch, tmp_path):
    """"yes"/"true"/"on" are all honored as truthy (opt-in idiom)."""
    for idx, val in enumerate(("yes", "true", "on", "TRUE")):
        sub = tmp_path / f"run{idx}"
        sub.mkdir()
        monkeypatch.setenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", val)
        llm = _ScriptedChatLLM(_tool_then_final())
        agent = SwarmAgentSpec(
            id="analyst",
            role="Synthesis analyst",
            system_prompt="You synthesize upstream findings.",
            tools=[],
            skills=[],
            max_iterations=5,
            timeout_seconds=60,
        )
        task = SwarmTask(id="t1", agent_id="analyst", prompt_template="Summarize.")
        with (
            patch.object(
                worker_mod, "build_swarm_registry", lambda *a, **k: _EmptyRegistry()
            ),
            patch.object(worker_mod, "ChatLLM", llm),
        ):
            run_worker(
                agent_spec=agent,
                task=task,
                upstream_summaries={},
                user_vars={},
                run_dir=sub,
                grounding_block=GROUNDING_BLOCK,
            )
        assert (sub / "artifacts" / "analyst" / "messages.json").exists(), val


def test_failure_path_persists_regardless_of_flag(monkeypatch, tmp_path):
    """Pin current behavior: hitting the iteration limit persists the
    transcript even when the opt-in flag is OFF."""
    monkeypatch.delenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", raising=False)
    # Always return a tool call → never terminates → iteration limit reached.
    responses = [
        LLMResponse(
            content="searching...",
            tool_calls=[
                ToolCallRequest(id=f"tc{i}", name="web_search", arguments={"q": "x"})
            ],
        )
        for i in range(10)
    ]
    llm = _ScriptedChatLLM(responses)

    _run(monkeypatch, tmp_path, llm, max_iterations=3)

    assert _transcript_path(tmp_path).exists()
