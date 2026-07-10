"""Shared throttle-aware backoff for retryable provider stream errors.

Single home for the capped exponential-backoff-with-jitter policy used by
both the swarm worker (:mod:`src.swarm.worker`) and the main ReAct loop
(:mod:`src.agent.loop`). Keeping the schedule in one place means the two
call sites cannot drift apart, and the jitter source is injectable so the
sleep sequence is deterministically testable without real time.

Policy (from the Phase 2 §4 design):

* base ``2s``, factor ``2``, cap ``90s``, max ``5`` attempts;
* only *retryable* :class:`~src.providers.chat.ProviderStreamError`s are
  retried (the 408/429/5xx/transport classification lives on the error);
* the provider's ``Retry-After`` header is honored when present (capped at
  ``cap_s`` so a hostile value cannot stall a run indefinitely).

The MiniMax Token Plan throttles "typically reset within ~1 minute", so the
90s cap comfortably covers the throttle window. ``VT_STREAM_RETRY_MAX`` and
``VT_STREAM_RETRY_BASE_S`` keep the schedule tunable per deployment.
"""

from __future__ import annotations

import datetime as _dt
import os
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

from src.providers.chat import LLMResponse, ProviderStreamError

DEFAULT_STREAM_RETRY_MAX = 5
DEFAULT_STREAM_RETRY_BASE_S = 2.0
STREAM_RETRY_CAP_S = 90.0


def stream_retry_max() -> int:
    """Resolve the max attempt count, robust to garbage env values.

    Returns:
        ``VT_STREAM_RETRY_MAX`` as an int (default 5). A non-integer or
        sub-1 value falls back to the default rather than crashing import.
    """
    try:
        value = int(os.getenv("VT_STREAM_RETRY_MAX", str(DEFAULT_STREAM_RETRY_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_STREAM_RETRY_MAX
    return value if value >= 1 else DEFAULT_STREAM_RETRY_MAX


def stream_retry_base_s() -> float:
    """Resolve the backoff base delay in seconds, robust to garbage values.

    Returns:
        ``VT_STREAM_RETRY_BASE_S`` as a float (default 2.0). A non-numeric
        or non-positive value falls back to the default.
    """
    try:
        value = float(os.getenv("VT_STREAM_RETRY_BASE_S", str(DEFAULT_STREAM_RETRY_BASE_S)))
    except (TypeError, ValueError):
        return DEFAULT_STREAM_RETRY_BASE_S
    return value if value > 0 else DEFAULT_STREAM_RETRY_BASE_S


def compute_backoff_delay(
    attempt: int,
    *,
    base_s: float,
    cap_s: float = STREAM_RETRY_CAP_S,
    rand: Callable[[], float] = random.random,
) -> float:
    """Return the equal-jitter backoff delay for a 1-based failure number.

    Uses "equal jitter": half the exponential ceiling is fixed and half is
    randomised, so the delay always grows with ``attempt`` (a guaranteed
    floor) while still spreading load. With ``rand`` pinned to ``0.0`` the
    schedule is exactly the exponential floor (``base_s/1``, ``base_s``,
    ``2·base_s`` …); pinned to ``1.0`` it is the full ceiling.

    Args:
        attempt: 1-based failure count (1 = first failure).
        base_s: Base delay in seconds.
        cap_s: Maximum ceiling before jitter.
        rand: Zero-arg callable returning a float in ``[0, 1)`` — injected in
            tests to make the sleep sequence deterministic.

    Returns:
        Delay in seconds.
    """
    ceiling = min(cap_s, base_s * (2 ** (attempt - 1)))
    half = ceiling / 2.0
    return half + rand() * half


def retry_after_seconds(exc: Any) -> Optional[float]:
    """Extract a ``Retry-After`` hint (seconds) from a provider error, if any.

    Honors both the numeric-seconds and HTTP-date forms of the header and
    looks on the wrapped original exception's ``response.headers`` (the shape
    the OpenAI / httpx SDKs expose on rate-limit errors).

    Args:
        exc: A :class:`ProviderStreamError` (or any object that may carry a
            ``response.headers`` mapping).

    Returns:
        Non-negative seconds to wait, or ``None`` when no usable header is
        present.
    """
    for candidate in (getattr(exc, "original", None), exc):
        if candidate is None:
            continue
        value = _header_retry_after(candidate)
        if value is not None:
            return value
    return None


def _header_retry_after(obj: Any) -> Optional[float]:
    """Return the parsed ``Retry-After`` from an object's headers, or None."""
    headers = getattr(getattr(obj, "response", None), "headers", None)
    if headers is None:
        headers = getattr(obj, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    return _parse_retry_after(raw)


def _parse_retry_after(raw: Any) -> Optional[float]:
    """Parse the numeric-seconds or HTTP-date form of ``Retry-After``."""
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (parsed - now).total_seconds())


def next_delay(
    exc: ProviderStreamError,
    attempt: int,
    *,
    base_s: float,
    cap_s: float = STREAM_RETRY_CAP_S,
    rand: Callable[[], float] = random.random,
) -> float:
    """Return the wait before the next retry, honoring ``Retry-After`` first.

    Args:
        exc: The retryable error just raised.
        attempt: 1-based failure count.
        base_s: Base delay in seconds.
        cap_s: Maximum delay (also caps a server-sent ``Retry-After``).
        rand: Injectable jitter source.

    Returns:
        Delay in seconds.
    """
    retry_after = retry_after_seconds(exc)
    if retry_after is not None:
        return min(retry_after, cap_s)
    return compute_backoff_delay(attempt, base_s=base_s, cap_s=cap_s, rand=rand)


def run_with_stream_retry(
    stream_fn: Callable[[], LLMResponse],
    *,
    max_attempts: Optional[int] = None,
    base_s: Optional[float] = None,
    cap_s: float = STREAM_RETRY_CAP_S,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Optional[Callable[[int, float, ProviderStreamError], None]] = None,
) -> LLMResponse:
    """Run ``stream_fn`` with capped exponential backoff on retryable errors.

    Calls ``stream_fn`` up to ``max_attempts`` times. A non-retryable
    :class:`ProviderStreamError` (deterministic 4xx) or the final attempt
    re-raises immediately. Between attempts it sleeps for the backoff /
    ``Retry-After`` delay and, when provided, invokes ``on_retry`` so the
    caller can log, emit an event, and reset any partial-stream state.

    Args:
        stream_fn: Zero-arg callable performing one streaming/chat attempt;
            recompute per-attempt timeout inside it if needed.
        max_attempts: Total attempts (default from ``VT_STREAM_RETRY_MAX``).
        base_s: Backoff base (default from ``VT_STREAM_RETRY_BASE_S``).
        cap_s: Maximum backoff delay.
        sleep: Sleep function (injected in tests to avoid real waits).
        rand: Jitter source (injected in tests for deterministic schedules).
        on_retry: Optional ``(attempt, delay, exc) -> None`` hook fired just
            before sleeping; ``attempt`` is the 1-based number that failed.

    Returns:
        The :class:`LLMResponse` from the first successful attempt.

    Raises:
        ProviderStreamError: On a non-retryable error or after exhausting all
            attempts.
    """
    attempts = max_attempts if max_attempts is not None else stream_retry_max()
    base = base_s if base_s is not None else stream_retry_base_s()

    for attempt in range(1, attempts + 1):
        try:
            return stream_fn()
        except ProviderStreamError as exc:
            if not exc.retryable or attempt >= attempts:
                raise
            delay = next_delay(exc, attempt, base_s=base, cap_s=cap_s, rand=rand)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)

    # Unreachable: the loop either returns a response or raises.
    raise AssertionError("run_with_stream_retry exhausted without returning")
