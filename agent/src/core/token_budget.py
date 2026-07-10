"""Per-run token-burn observability (Phase 2 §4).

The MiniMax Token Plan enforces its 5-hour / weekly quota windows server-side;
the client's only job is to make burn *visible* so an operator can see a run
approaching the window before the server throttles. This module centralises
the cumulative-token summary line and the optional soft-budget warning shared
by the swarm runtime and the main ReAct loop.

Observability only — there is deliberately **no** client-side hard cutoff. A
mid-pipeline abort is worse than a throttled wait.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


def token_budget_warn_threshold() -> int:
    """Return the soft per-run token-budget warning threshold.

    Returns:
        ``VIBE_RUN_TOKEN_BUDGET_WARN`` as a positive int, or ``0`` when unset,
        zero, or unparseable (``0`` disables the warning entirely).
    """
    try:
        value = int(os.getenv("VIBE_RUN_TOKEN_BUDGET_WARN", "0") or "0")
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def format_token_burn(
    *,
    scope: str,
    run_id: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: Optional[int] = None,
    calls: Optional[int] = None,
    gate_wait_seconds: Optional[float] = None,
) -> str:
    """Build the one-line cumulative token summary for a finished run.

    Args:
        scope: Short label for the run kind (e.g. ``"swarm"`` / ``"agent"``).
        run_id: Run identifier.
        input_tokens: Cumulative prompt tokens.
        output_tokens: Cumulative completion tokens.
        total_tokens: Cumulative total; derived from in+out when omitted.
        calls: Number of LLM calls, when known.
        gate_wait_seconds: Total time queued on the LLM gate, when known.

    Returns:
        A single log-friendly line.
    """
    total = total_tokens if total_tokens is not None else (input_tokens or 0) + (output_tokens or 0)
    parts = [
        f"token-burn scope={scope}",
        f"run={run_id}",
        f"input={input_tokens}",
        f"output={output_tokens}",
        f"total={total}",
    ]
    if calls is not None:
        parts.append(f"calls={calls}")
    if gate_wait_seconds:
        parts.append(f"gate_wait={gate_wait_seconds:.1f}s")
    return " ".join(parts)


def log_token_burn(
    logger: logging.Logger,
    *,
    scope: str,
    run_id: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: Optional[int] = None,
    calls: Optional[int] = None,
    gate_wait_seconds: Optional[float] = None,
) -> str:
    """Log the cumulative token summary and warn if the soft budget is exceeded.

    Args:
        logger: Logger to emit on.
        scope: Short run-kind label.
        run_id: Run identifier.
        input_tokens: Cumulative prompt tokens.
        output_tokens: Cumulative completion tokens.
        total_tokens: Cumulative total; derived from in+out when omitted.
        calls: Number of LLM calls, when known.
        gate_wait_seconds: Total time queued on the LLM gate, when known.

    Returns:
        The summary line that was logged (also convenient for tests / events).
    """
    total = total_tokens if total_tokens is not None else (input_tokens or 0) + (output_tokens or 0)
    line = format_token_burn(
        scope=scope,
        run_id=run_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        calls=calls,
        gate_wait_seconds=gate_wait_seconds,
    )
    logger.info(line)

    threshold = token_budget_warn_threshold()
    if threshold and total > threshold:
        logger.warning(
            "token-budget warning: scope=%s run=%s used %d tokens > "
            "VIBE_RUN_TOKEN_BUDGET_WARN=%d (observability only — run not aborted)",
            scope,
            run_id,
            total,
            threshold,
        )
    return line
