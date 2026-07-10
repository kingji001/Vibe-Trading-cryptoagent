"""Phase 2 §4 concurrency-governance tests.

Four independent contracts, all socket-free (no real provider, no real time):

1. Global LLM gate — N threads through ``ChatLLM.stream_chat`` with a fake
   provider never exceed ``VIBE_LLM_MAX_CONCURRENT`` simultaneous in-flight
   requests (instrumented with a live counter, not sockets).
2. Layer-deadline scaling — ``compute_layer_deadline`` sizes the 5-tasks /
   3-workers case for two waves, not one.
3. Backoff schedule — ``run_with_stream_retry`` sleeps on the capped
   exponential-with-jitter schedule, honoring ``Retry-After`` when present;
   ``time.sleep`` is patched and the jitter source injected so the sequence is
   deterministic.
4. Regression — ``VIBE_LLM_MAX_CONCURRENT=0`` (unset) never acquires the gate:
   the same N threads all run concurrently (zero-overhead path unchanged).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

import src.providers.backoff as backoff_mod
from src.providers.backoff import (
    compute_backoff_delay,
    next_delay,
    retry_after_seconds,
    run_with_stream_retry,
)
from src.providers.chat import ChatLLM, LLMResponse, ProviderStreamError, get_llm_gate, reset_llm_gate
from src.swarm.runtime import compute_layer_deadline


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _GateProbe:
    """Thread-safe live/peak in-flight counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)

    def exit(self) -> None:
        with self._lock:
            self.live -= 1


def _fake_chunk() -> SimpleNamespace:
    """Return a one-shot object quacking like an AIMessageChunk."""
    return SimpleNamespace(
        content="ok",
        tool_calls=[],
        additional_kwargs={},
        response_metadata={"finish_reason": "stop"},
        usage_metadata=None,
    )


class _FakeStreamingLLM:
    """Stand-in for the bound LangChain client used inside ``ChatLLM``.

    ``stream`` marks itself in-flight for the duration of the (fake) request so
    the gate probe can observe how many requests overlap. A barrier makes every
    thread arrive at the gate together, so an ungated run demonstrably reaches
    N-way concurrency while a gated run is pinned to the cap.
    """

    def __init__(self, probe: _GateProbe, barrier: threading.Barrier, hold: float = 0.05) -> None:
        self._probe = probe
        self._barrier = barrier
        self._hold = hold

    def stream(self, messages: Any, config: Any = None):
        self._probe.enter()
        try:
            # Sleep while "in flight" so overlapping requests are observable.
            threading.Event().wait(self._hold)
            yield _fake_chunk()
        finally:
            self._probe.exit()


def _make_chat_llm(probe: _GateProbe, barrier: threading.Barrier) -> ChatLLM:
    """Build a ChatLLM bound to the fake client without touching ``build_llm``."""
    llm = ChatLLM.__new__(ChatLLM)
    llm.model_name = "fake-model"
    llm._llm = _FakeStreamingLLM(probe, barrier)
    return llm


def _drive_threads(llm: ChatLLM, barrier: threading.Barrier, n: int) -> list[Exception]:
    """Run ``n`` concurrent stream_chat calls; return any exceptions raised."""
    errors: list[Exception] = []
    err_lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            llm.stream_chat([{"role": "user", "content": "hi"}])
        except Exception as exc:  # pragma: no cover - defensive
            with err_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return errors


# --------------------------------------------------------------------------- #
# 1. Global LLM gate — cap is enforced across threads
# --------------------------------------------------------------------------- #


def test_gate_caps_concurrent_in_flight_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """With cap=3, 9 threads never exceed 3 simultaneous in-flight requests."""
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "3")
    reset_llm_gate()
    try:
        n = 9
        probe = _GateProbe()
        barrier = threading.Barrier(n)
        llm = _make_chat_llm(probe, barrier)

        errors = _drive_threads(llm, barrier, n)

        assert errors == []
        assert probe.peak <= 3, f"gate breached: peaked at {probe.peak} in-flight"
        # Prove the gate actually allowed concurrency (did not serialize to 1).
        assert probe.peak >= 2
    finally:
        reset_llm_gate()


# --------------------------------------------------------------------------- #
# 4. Regression — cap=0 (unset) is the zero-overhead, ungated path
# --------------------------------------------------------------------------- #


def test_gate_disabled_by_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset and explicit-zero both disable the gate (returns None)."""
    monkeypatch.delenv("VIBE_LLM_MAX_CONCURRENT", raising=False)
    reset_llm_gate()
    assert get_llm_gate() is None
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "0")
    assert get_llm_gate() is None


def test_gate_zero_does_not_limit_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """VIBE_LLM_MAX_CONCURRENT=0 leaves all N requests running concurrently."""
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "0")
    reset_llm_gate()
    try:
        n = 6
        probe = _GateProbe()
        barrier = threading.Barrier(n)
        llm = _make_chat_llm(probe, barrier)

        errors = _drive_threads(llm, barrier, n)

        assert errors == []
        # No gate → all threads overlap, exceeding what any cap<=3 would allow.
        assert probe.peak == n
    finally:
        reset_llm_gate()


def test_gate_wait_seconds_zero_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single ungated call reports zero gate-wait (no telemetry overhead)."""
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "0")
    reset_llm_gate()
    probe = _GateProbe()
    barrier = threading.Barrier(1)
    llm = _make_chat_llm(probe, barrier)
    barrier.wait()
    response = llm.stream_chat([{"role": "user", "content": "hi"}])
    assert response.gate_wait_seconds == 0.0


# --------------------------------------------------------------------------- #
# 2. Layer-deadline scaling — 5 tasks / 3 workers = two waves
# --------------------------------------------------------------------------- #


def test_layer_deadline_scales_for_five_tasks_three_workers() -> None:
    """5 tasks at cap 3 → ceil(5/3)=2 waves → budget doubled + buffer."""
    # per-task budget 360s (e.g. 120s × 3 attempts).
    deadline = compute_layer_deadline(layer_budget=360, runnable_tasks=5, max_workers=3, buffer_s=60)
    assert deadline == 360 * 2 + 60  # 780


def test_layer_deadline_single_wave_when_workers_cover_layer() -> None:
    """3 tasks at cap 3 → one wave → unchanged from the legacy formula."""
    deadline = compute_layer_deadline(layer_budget=360, runnable_tasks=3, max_workers=3, buffer_s=60)
    assert deadline == 360 + 60


def test_layer_deadline_six_tasks_three_workers_two_waves() -> None:
    """6 tasks at cap 3 → exactly two full waves."""
    assert compute_layer_deadline(layer_budget=100, runnable_tasks=6, max_workers=3) == 100 * 2 + 60


def test_layer_deadline_none_when_no_budget() -> None:
    """No runnable budget → no imposed deadline."""
    assert compute_layer_deadline(layer_budget=0, runnable_tasks=5, max_workers=3) is None


def test_layer_deadline_handles_zero_workers() -> None:
    """A degenerate 0-worker cap is treated as 1 worker (no ZeroDivision)."""
    assert compute_layer_deadline(layer_budget=50, runnable_tasks=4, max_workers=0) == 50 * 4 + 60


def test_layer_deadline_wave_divisor_bounded_by_gate_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate cap=3 with max_workers=4 → divisor 3: 4 tasks run in 2 waves, not 1.

    With the global gate enabled, a run's 4 pool threads can only make 3-way
    simultaneous LLM progress, so a deadline sized for a single 4-wide wave
    would falsely time out the gated 4th task.
    """
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "3")
    deadline = compute_layer_deadline(layer_budget=100, runnable_tasks=4, max_workers=4, buffer_s=60)
    assert deadline == 100 * 2 + 60  # ceil(4/3) = 2 waves


def test_layer_deadline_gate_disabled_keeps_max_workers_divisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate cap=0 (and unset) → divisor stays max_workers (upstream behavior)."""
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "0")
    assert compute_layer_deadline(layer_budget=100, runnable_tasks=4, max_workers=4, buffer_s=60) == 100 + 60
    monkeypatch.delenv("VIBE_LLM_MAX_CONCURRENT", raising=False)
    assert compute_layer_deadline(layer_budget=100, runnable_tasks=4, max_workers=4, buffer_s=60) == 100 + 60


def test_layer_deadline_gate_cap_above_workers_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate cap larger than max_workers never widens the divisor."""
    monkeypatch.setenv("VIBE_LLM_MAX_CONCURRENT", "8")
    assert compute_layer_deadline(layer_budget=100, runnable_tasks=4, max_workers=4, buffer_s=60) == 100 + 60


# --------------------------------------------------------------------------- #
# 3. Backoff schedule — deterministic sleep sequence
# --------------------------------------------------------------------------- #


def _retryable_error(status: int = 429, retry_after: str | None = None) -> ProviderStreamError:
    """Build a retryable ProviderStreamError, optionally with a Retry-After."""
    original: Any = SimpleNamespace()
    original.status_code = status
    if retry_after is not None:
        original.response = SimpleNamespace(headers={"retry-after": retry_after})
    return ProviderStreamError(provider="minimax", model="MiniMax-M3", original=original)


def _non_retryable_error() -> ProviderStreamError:
    """Build a deterministic 400 ProviderStreamError (never retried)."""
    original: Any = SimpleNamespace()
    original.status_code = 400
    return ProviderStreamError(provider="minimax", model="MiniMax-M3", original=original)


class _ScriptedStream:
    """Callable raising queued errors then returning a final response."""

    def __init__(self, errors: list[Exception], final: LLMResponse | None = None) -> None:
        self._errors = list(errors)
        self._final = final if final is not None else LLMResponse(content="done")
        self.calls = 0

    def __call__(self) -> LLMResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._final


def test_backoff_equal_jitter_floor_sequence() -> None:
    """rand→0.0 gives the exponential floor: base 2 → [1, 2, 4, 8] over 5 attempts."""
    stream = _ScriptedStream([_retryable_error() for _ in range(5)])
    sleeps: list[float] = []

    with pytest.raises(ProviderStreamError):
        run_with_stream_retry(
            stream,
            max_attempts=5,
            base_s=2.0,
            sleep=sleeps.append,
            rand=lambda: 0.0,
        )

    assert stream.calls == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_backoff_equal_jitter_ceiling_sequence() -> None:
    """rand→1.0 gives the full ceiling: base 2 → [2, 4, 8, 16] over 5 attempts."""
    stream = _ScriptedStream([_retryable_error() for _ in range(5)])
    sleeps: list[float] = []

    with pytest.raises(ProviderStreamError):
        run_with_stream_retry(stream, max_attempts=5, base_s=2.0, sleep=sleeps.append, rand=lambda: 1.0)

    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped() -> None:
    """The exponential ceiling never exceeds the 90s cap."""
    assert compute_backoff_delay(10, base_s=2.0, cap_s=90.0, rand=lambda: 1.0) == 90.0
    assert compute_backoff_delay(10, base_s=2.0, cap_s=90.0, rand=lambda: 0.0) == 45.0


def test_backoff_succeeds_after_retries() -> None:
    """One retryable failure then success → one sleep, response returned."""
    stream = _ScriptedStream([_retryable_error()], LLMResponse(content="recovered"))
    sleeps: list[float] = []

    result = run_with_stream_retry(stream, max_attempts=5, base_s=2.0, sleep=sleeps.append, rand=lambda: 0.0)

    assert result.content == "recovered"
    assert stream.calls == 2
    assert sleeps == [1.0]


def test_backoff_honors_retry_after_header() -> None:
    """A 429 with Retry-After sleeps for that value, not the exponential floor."""
    stream = _ScriptedStream([_retryable_error(retry_after="30") for _ in range(3)])
    sleeps: list[float] = []

    with pytest.raises(ProviderStreamError):
        run_with_stream_retry(stream, max_attempts=3, base_s=2.0, sleep=sleeps.append, rand=lambda: 0.0)

    assert sleeps == [30.0, 30.0]


def test_retry_after_is_capped() -> None:
    """A hostile Retry-After is clamped to the cap."""
    exc = _retryable_error(retry_after="500")
    assert next_delay(exc, 1, base_s=2.0, cap_s=90.0, rand=lambda: 0.0) == 90.0


def test_retry_after_absent_is_none() -> None:
    """No Retry-After header → falls through to exponential backoff."""
    assert retry_after_seconds(_retryable_error()) is None
    assert retry_after_seconds(_retryable_error(retry_after="12")) == 12.0


def test_non_retryable_error_raises_without_sleeping() -> None:
    """A deterministic 4xx is never retried: one call, no sleeps."""
    stream = _ScriptedStream([_non_retryable_error()])
    sleeps: list[float] = []

    with pytest.raises(ProviderStreamError):
        run_with_stream_retry(stream, max_attempts=5, base_s=2.0, sleep=sleeps.append, rand=lambda: 0.0)

    assert stream.calls == 1
    assert sleeps == []


def test_env_defaults_are_the_mandated_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset envs yield the §4 defaults: max 5 attempts, base 2s."""
    monkeypatch.delenv("VT_STREAM_RETRY_MAX", raising=False)
    monkeypatch.delenv("VT_STREAM_RETRY_BASE_S", raising=False)
    assert backoff_mod.stream_retry_max() == 5
    assert backoff_mod.stream_retry_base_s() == 2.0


# --------------------------------------------------------------------------- #
# Token-burn observability (Phase 2 §4)
# --------------------------------------------------------------------------- #

import logging

from src.core.token_budget import format_token_burn, log_token_burn, token_budget_warn_threshold


def test_token_budget_threshold_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset / zero / garbage all disable the warning (threshold 0)."""
    monkeypatch.delenv("VIBE_RUN_TOKEN_BUDGET_WARN", raising=False)
    assert token_budget_warn_threshold() == 0
    monkeypatch.setenv("VIBE_RUN_TOKEN_BUDGET_WARN", "0")
    assert token_budget_warn_threshold() == 0
    monkeypatch.setenv("VIBE_RUN_TOKEN_BUDGET_WARN", "not-a-number")
    assert token_budget_warn_threshold() == 0
    monkeypatch.setenv("VIBE_RUN_TOKEN_BUDGET_WARN", "50000")
    assert token_budget_warn_threshold() == 50000


def test_token_burn_summary_line_shape() -> None:
    """The cumulative summary derives total and includes optional fields."""
    line = format_token_burn(
        scope="swarm", run_id="r-1", input_tokens=1000, output_tokens=200, calls=7, gate_wait_seconds=3.5
    )
    assert "scope=swarm" in line
    assert "run=r-1" in line
    assert "input=1000" in line and "output=200" in line and "total=1200" in line
    assert "calls=7" in line and "gate_wait=3.5s" in line


def test_token_burn_warns_over_budget(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """A run exceeding the soft budget logs a warning (no abort — observability)."""
    monkeypatch.setenv("VIBE_RUN_TOKEN_BUDGET_WARN", "1000")
    logger = logging.getLogger("test.tokenburn")
    with caplog.at_level(logging.WARNING, logger="test.tokenburn"):
        log_token_burn(logger, scope="agent", run_id="r-2", input_tokens=900, output_tokens=300)
    assert any("token-budget warning" in rec.message for rec in caplog.records)


def test_token_burn_no_warning_when_under_or_unset(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """No warning when the budget is unset or not exceeded."""
    monkeypatch.delenv("VIBE_RUN_TOKEN_BUDGET_WARN", raising=False)
    logger = logging.getLogger("test.tokenburn2")
    with caplog.at_level(logging.WARNING, logger="test.tokenburn2"):
        log_token_burn(logger, scope="agent", run_id="r-3", input_tokens=10_000, output_tokens=10_000)
    assert not any("token-budget warning" in rec.message for rec in caplog.records)
