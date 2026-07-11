"""Deterministic, LLM-free event trigger (two-tier-cadence Task 3).

Executed inside ``run_tick`` on every tick (both 1D and 1H cadences) — no
extra scheduled job. For each watched symbol it flags a material market move
so the tick job can fire an ad-hoc committee run:

  - a price move: ``|price - reference| / |reference| >= price_move_pct%``,
  - a funding spike: ``|funding_rate| >= funding_abs``.

Reference price resolution order (binding, design spec 2.3):
  1. the last committee decision's execution price for the symbol
     (``journal_ref_fn`` — its ledger fill's ``fill_price``, else the journal
     entry's ``ref_price``),
  2. else the previous tick's stored ``last_price`` for the symbol,
  3. else no price trigger this tick — the observed price is stored so the
     NEXT tick can compare against it.

A per-symbol cooldown (``cooldown_h``, persisted in ``tick_state.json`` under
``last_event_trigger_ts``) means a sustained move triggers exactly once: a
symbol still inside its cooldown window is not re-flagged, regardless of
whether the agent later acted on the earlier trigger.

Reference-price semantics worth knowing before tuning cooldown/thresholds:
  - a symbol in cooldown is skipped entirely, so its ``last_price`` is NOT
    refreshed during the cooldown window — intended: once the cooldown
    elapses, the next move is measured from the price at the moment the
    cooldown-starting trigger fired, not from wherever the price drifted to
    while skipped;
  - a symbol that leaves the watched set and later rejoins with no journaled
    committee decision in the meantime falls back to whatever ``last_price``
    was last stored for it, which may be stale — the first tick after it
    rejoins can fire one spurious, cooldown-bounded trigger against that old
    price.

``check_events`` is PURE: every market/journal read is an injected callable
(``price_fn`` / ``funding_fn`` / ``journal_ref_fn``), so the function itself
performs no I/O and never invents a price. A fetch failure for a symbol raises
out of the injected callable; ``check_events`` catches it, emits no trigger for
that metric, and does NOT store a price (never invent). The *recording* of that
failure in the tick result is ``run_tick``'s job — it wraps the fetchers so a
failure lands in the tick's ``errors`` list (design spec: "error recorded in
the tick result").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from src.paper.broker import _fmt_ts, _parse_iso

# price_fn(symbol) -> last price (float); raises on fetch failure.
PriceFn = Callable[[str], float]
# funding_fn(symbol) -> current funding rate (float); raises on fetch failure.
FundingFn = Callable[[str], float]
# journal_ref_fn(symbol) -> reference (decision-time) price or None.
JournalRefFn = Callable[[str], "float | None"]

# Trigger metric labels (stable — surfaced in the tick result and the tick job
# prompt so a human/agent can see WHY a committee run was fired).
METRIC_PRICE = "price_move_pct"
METRIC_FUNDING = "funding_abs"


def _env_float(name: str, default: str) -> float:
    """Parse a float env var, treating an empty value as the default.

    ``"0"`` reads as ``0.0`` (disables the threshold); an unset OR empty value
    falls back to ``default`` — never crashing ``float("")``.

    Kept for any external caller relying on the simple contract; it still
    raises on a genuinely unparseable value (e.g. ``"5%"``) — ``from_env``
    below does NOT call this directly for that reason, see
    ``_env_float_or_warn``.
    """
    raw = os.environ.get(name, default)
    raw = raw.strip() or default
    return float(raw)


def _env_float_or_warn(name: str, default: str, warnings: list[str]) -> float:
    """Parse a float env var, falling back to ``default`` on a bad value.

    An unparseable value (e.g. ``VIBE_EVENT_PRICE_MOVE_PCT=5%``) must NOT
    raise: that would abort ``EventConfig.from_env`` and, transitively,
    ``run_tick`` BEFORE stop/TP evaluation — a typo in an event-tuning env var
    would freeze risk management entirely. Instead the default for that var is
    used and a human-readable warning is appended to ``warnings`` (surfaced by
    the caller — ``run_tick`` records it in the tick's ``errors`` list).
    """
    raw = os.environ.get(name, default)
    raw = raw.strip() or default
    try:
        return float(raw)
    except ValueError:
        warnings.append(
            f"invalid {name}={raw!r} (not a number) — using default {default}"
        )
        return float(default)


@dataclass(frozen=True)
class EventConfig:
    """Event-trigger thresholds (all additive; unset = spec defaults).

    - ``price_move_pct`` (``VIBE_EVENT_PRICE_MOVE_PCT``, default 5; 0 = off)
    - ``funding_abs`` (``VIBE_EVENT_FUNDING_ABS``, default 0.001 = 0.1%/8h; 0 = off)
    - ``cooldown_h`` (``VIBE_EVENT_COOLDOWN_H``, default 12)
    - ``warnings``: human-readable messages for any env var that failed to
      parse and fell back to its default (see ``_env_float_or_warn``); empty
      when every configured value parsed cleanly. ``from_env`` never raises.
    """

    price_move_pct: float = 5.0
    funding_abs: float = 0.001
    cooldown_h: float = 12.0
    warnings: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        """True when at least one threshold is active (> 0)."""
        return self.price_move_pct > 0 or self.funding_abs > 0

    @classmethod
    def from_env(cls) -> "EventConfig":
        warnings: list[str] = []
        return cls(
            price_move_pct=_env_float_or_warn("VIBE_EVENT_PRICE_MOVE_PCT", "5", warnings),
            funding_abs=_env_float_or_warn("VIBE_EVENT_FUNDING_ABS", "0.001", warnings),
            cooldown_h=_env_float_or_warn("VIBE_EVENT_COOLDOWN_H", "12", warnings),
            warnings=warnings,
        )


def _in_cooldown(last_ts: Any, now: datetime, cooldown_h: float) -> bool:
    """Is the symbol still inside its cooldown window?"""
    if not last_ts or cooldown_h <= 0:
        return False
    parsed = _parse_iso(last_ts)
    if parsed is None:
        return False
    return (now - parsed) < timedelta(hours=cooldown_h)


def check_events(
    symbols: list[str],
    state: dict,
    *,
    price_fn: PriceFn,
    funding_fn: FundingFn,
    journal_ref_fn: JournalRefFn,
    now: datetime,
    config: EventConfig,
) -> tuple[list[dict], dict]:
    """Pure event check for ``symbols`` against ``state``.

    Returns ``(triggers, new_state)``. ``new_state`` is a fresh copy of
    ``state`` with ``last_price`` refreshed for each symbol whose price fetch
    succeeded and ``last_event_trigger_ts`` armed for each symbol that fired
    (the input ``state`` is never mutated). Each trigger is
    ``{"symbol", "reason", "metric", "value", "threshold"}``.

    At most ONE trigger is emitted per symbol per call (price takes precedence
    over funding); a symbol in cooldown is skipped entirely (no fetch, no
    trigger, no price refresh). A fetch failure is swallowed here (no trigger,
    no invented price) — ``run_tick`` records it in the tick result.
    """
    # Copy the WHOLE input dict (not just the three known keys) and overlay
    # fresh copies of those three, so a foreign top-level key present in
    # ``state`` (e.g. one added by a future schema version) survives the
    # round-trip through ``check_events`` -> ``store.save_tick_state`` instead
    # of being silently dropped on the next save.
    new_state = dict(state)
    new_state["last_bar_ts"] = dict(state.get("last_bar_ts", {}))
    new_state["last_event_trigger_ts"] = dict(state.get("last_event_trigger_ts", {}))
    new_state["last_price"] = dict(state.get("last_price", {}))
    triggers: list[dict] = []
    if not config.enabled:
        return triggers, new_state

    move_threshold = config.price_move_pct
    funding_threshold = config.funding_abs

    for symbol in symbols:
        if _in_cooldown(
            new_state["last_event_trigger_ts"].get(symbol), now, config.cooldown_h
        ):
            continue

        trigger: dict | None = None

        # --- price move (checked first; takes precedence) ------------------ #
        if move_threshold > 0:
            price = _safe_call(price_fn, symbol)
            if price is not None:
                reference = _resolve_reference(
                    symbol, journal_ref_fn, new_state["last_price"]
                )
                # Store the observed price for the NEXT tick's fallback
                # reference, whether or not it triggers this tick.
                new_state["last_price"][symbol] = price
                if reference is not None and reference != 0:
                    move_pct = abs(price - reference) / abs(reference) * 100.0
                    if move_pct >= move_threshold:
                        trigger = {
                            "symbol": symbol,
                            "reason": (
                                f"price {price} moved {move_pct:.2f}% from reference "
                                f"{reference} (>= {move_threshold}% threshold)"
                            ),
                            "metric": METRIC_PRICE,
                            "value": round(move_pct, 4),
                            "threshold": move_threshold,
                        }

        # --- funding spike (only when price did not already fire) ---------- #
        if trigger is None and funding_threshold > 0:
            funding = _safe_call(funding_fn, symbol)
            if funding is not None and abs(funding) >= funding_threshold:
                trigger = {
                    "symbol": symbol,
                    "reason": (
                        f"funding rate {funding} breached |{funding_threshold}| "
                        "threshold"
                    ),
                    "metric": METRIC_FUNDING,
                    "value": funding,
                    "threshold": funding_threshold,
                }

        if trigger is not None:
            triggers.append(trigger)
            new_state["last_event_trigger_ts"][symbol] = _fmt_ts(now)

    return triggers, new_state


def _resolve_reference(
    symbol: str, journal_ref_fn: JournalRefFn, last_price: dict
) -> float | None:
    """Reference price for the move check: decision price, else prev-tick price."""
    ref = journal_ref_fn(symbol)
    if ref is not None:
        return float(ref)
    prev = last_price.get(symbol)
    return float(prev) if prev is not None else None


def _safe_call(fn: Callable[[str], float], symbol: str) -> float | None:
    """Call an injected fetcher, returning ``None`` on any failure.

    Never invents a value: a raising fetcher yields ``None`` so the caller
    emits no trigger and stores no price. ``run_tick`` wraps the real fetchers
    to record the failure in the tick result.
    """
    try:
        return fn(symbol)
    except Exception:
        return None
