"""Decision journal: the committee's learning loop.

Ports TradingAgents' reflection mechanism (decision -> realized outcome ->
written lesson -> injected into the next Portfolio Manager prompt), adapted
for a 24/7 crypto market:

- multi-horizon resolution at 24h / 72h / 7d instead of a fixed 5 trading days;
- alpha measured against a crypto benchmark (default BTC-USDT, crypto's SPY);
  for the benchmark asset itself alpha is definitionally 0, so directional
  correctness is judged on raw return;
- storage is append-only JSONL with atomic rewrites (temp file + os.replace),
  mirroring the Hypothesis Registry's persistence style — pure code, no LLM
  and no live-trading dependencies.

Price math is deterministic and lookahead-safe: the reference price is the
OPEN of the first bar at/after the decision timestamp; horizon prices are the
CLOSE of the last bar at/before the horizon deadline. Bar data is supplied by
an injected ``fetch_bars`` callable so tests never touch the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# horizon key -> hours
HORIZONS: dict[str, int] = {"24h": 24, "72h": 72, "7d": 168}

DEFAULT_BENCHMARK = "BTC-USDT"
JOURNAL_PATH_ENV = "VIBE_TRADING_COMMITTEE_JOURNAL"

# A bar must land within this window of the target timestamp to count.
_MAX_BAR_GAP = timedelta(hours=3)

# |move| at the primary horizon below which a Hold call counts as correct.
HOLD_BAND = 0.02

_POSITIVE = {"Buy", "Overweight"}
_NEGATIVE = {"Sell", "Underweight"}

# fetch_bars(symbol, start_dt, end_dt) -> [{"ts": datetime, "open": float,
# "close": float}, ...] sorted ascending, UTC.
FetchBars = Callable[[str, datetime, datetime], list[dict[str, Any]]]


def journal_path(path: str | Path | None = None) -> Path:
    """Resolve the journal file location (arg > env > default)."""
    if path is not None:
        return Path(path)
    env = os.getenv(JOURNAL_PATH_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".vibe-trading" / "committee" / "journal.jsonl"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_entries(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load all journal entries (oldest first). Missing file -> []."""
    p = journal_path(path)
    if not p.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _write_entries(entries: list[dict[str, Any]], path: str | Path | None) -> None:
    """Atomically rewrite the journal (temp file + replace)."""
    p = journal_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)


def primary_horizon(time_horizon: str) -> str:
    """Map a stated horizon like '72h swing' / '2-4 week position' to a key."""
    text = (time_horizon or "").lower()
    if re.search(r"\b(24\s*h|1\s*day|intraday|overnight)\b", text):
        return "24h"
    if re.search(r"\b(7\s*d|1\s*week|week|month|position)\b", text):
        return "7d"
    return "72h"


def append_decision(
    *,
    symbol: str,
    rating: str,
    time_horizon: str,
    price_target: float | None = None,
    run_id: str | None = None,
    decided_at: datetime | str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append a pending decision. Same (run_id, symbol) is idempotent."""
    entries = load_entries(path)
    if run_id:
        for e in entries:
            if e.get("run_id") == run_id and e.get("symbol") == symbol:
                return e  # already journaled for this run

    if decided_at is None:
        decided = _utcnow()
    elif isinstance(decided_at, str):
        decided = _parse_ts(decided_at)
    else:
        decided = decided_at

    entry: dict[str, Any] = {
        "id": "dec_"
        + hashlib.sha256(
            f"{symbol}|{decided.isoformat()}|{rating}".encode()
        ).hexdigest()[:12],
        "decided_at": decided.isoformat(),
        "symbol": symbol,
        "rating": rating,
        "time_horizon": time_horizon,
        "primary_horizon": primary_horizon(time_horizon),
        "price_target": price_target,
        "run_id": run_id,
        "status": "pending",
        "ref_price": None,
        "horizons": {},
        "reflection": None,
        "reflected_at": None,
    }
    entries.append(entry)
    _write_entries(entries, path)
    return entry


def _price_at(bars: list[dict[str, Any]], target: datetime, *, kind: str) -> float | None:
    """Deterministic bar lookup.

    kind='ref'  -> OPEN of the first bar at/after target (entry price).
    kind='mark' -> CLOSE of the last bar at/before target (horizon price).
    Returns None when no bar lands within _MAX_BAR_GAP of the target.
    """
    if kind == "ref":
        for bar in bars:
            if bar["ts"] >= target:
                return float(bar["open"]) if bar["ts"] - target <= _MAX_BAR_GAP else None
        return None
    best = None
    for bar in bars:
        if bar["ts"] <= target:
            best = bar
        else:
            break
    if best is None or target - best["ts"] > _MAX_BAR_GAP:
        return None
    return float(best["close"])


def _direction_correct(rating: str, score_move: float, horizon_move: float) -> bool | None:
    """Was the call directionally right? Hold judged by the band on |move|."""
    if rating in _POSITIVE:
        return score_move > 0
    if rating in _NEGATIVE:
        return score_move < 0
    if rating == "Hold":
        return abs(horizon_move) <= HOLD_BAND
    return None


def resolve_due(
    fetch_bars: FetchBars,
    *,
    now: datetime | None = None,
    benchmark: str = DEFAULT_BENCHMARK,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve every due, unresolved horizon of every pending entry.

    Returns {"resolved": [(entry_id, horizon), ...], "reflection_due": [entry, ...],
    "errors": [str, ...]}. An entry lands in reflection_due when its primary
    horizon has data and no reflection has been written yet.
    """
    now = now or _utcnow()
    entries = load_entries(path)
    resolved: list[tuple[str, str]] = []
    errors: list[str] = []
    bars_cache: dict[str, list[dict[str, Any]]] = {}

    def get_bars(symbol: str, start: datetime) -> list[dict[str, Any]]:
        if symbol not in bars_cache:
            bars = fetch_bars(symbol, start - timedelta(hours=6), now)
            bars_cache[symbol] = sorted(bars, key=lambda b: b["ts"])
        return bars_cache[symbol]

    changed = False
    for entry in entries:
        if entry.get("status") != "pending":
            continue
        decided = _parse_ts(entry["decided_at"])
        symbol = entry["symbol"]

        due = [
            (key, hours)
            for key, hours in HORIZONS.items()
            if key not in entry["horizons"] and decided + timedelta(hours=hours) <= now
        ]
        if not due:
            continue

        try:
            sym_bars = get_bars(symbol, decided)
            bench_bars = sym_bars if symbol == benchmark else get_bars(benchmark, decided)
        except Exception as exc:  # loader/network failure -> retry next run
            errors.append(f"{entry['id']}: bar fetch failed: {exc}")
            continue

        ref = entry.get("ref_price") or _price_at(sym_bars, decided, kind="ref")
        bench_ref = _price_at(bench_bars, decided, kind="ref")
        if ref is None or bench_ref is None:
            errors.append(f"{entry['id']}: no reference bar near {entry['decided_at']}")
            continue
        entry["ref_price"] = ref

        for key, hours in due:
            deadline = decided + timedelta(hours=hours)
            mark = _price_at(sym_bars, deadline, kind="mark")
            bench_mark = _price_at(bench_bars, deadline, kind="mark")
            if mark is None or bench_mark is None:
                errors.append(f"{entry['id']}: no bar near {key} deadline")
                continue
            raw = mark / ref - 1.0
            bench_ret = bench_mark / bench_ref - 1.0
            alpha = raw - bench_ret
            score_move = raw if symbol == benchmark else alpha
            entry["horizons"][key] = {
                "raw_return": round(raw, 6),
                "benchmark_return": round(bench_ret, 6),
                "alpha": round(alpha, 6),
                "mark_price": mark,
                "direction_correct": _direction_correct(entry["rating"], score_move, raw),
                "resolved_at": now.isoformat(),
            }
            resolved.append((entry["id"], key))
            changed = True

        if len(entry["horizons"]) == len(HORIZONS):
            entry["status"] = "resolved"
            changed = True

    if changed:
        _write_entries(entries, path)

    reflection_due = [
        e
        for e in entries
        if e.get("reflection") is None and e["primary_horizon"] in e["horizons"]
    ]
    return {"resolved": resolved, "reflection_due": reflection_due, "errors": errors}


def write_reflection(
    entry_id: str, reflection: str, *, path: str | Path | None = None
) -> dict[str, Any]:
    """Attach a written lesson to an entry."""
    entries = load_entries(path)
    for entry in entries:
        if entry["id"] == entry_id:
            entry["reflection"] = reflection.strip()
            entry["reflected_at"] = _utcnow().isoformat()
            _write_entries(entries, path)
            return entry
    raise KeyError(f"No journal entry with id {entry_id!r}")


def _tag_line(entry: dict[str, Any]) -> str:
    """Render the TradingAgents-style tag: [date | symbol | rating | outcome]."""
    date = entry["decided_at"][:10]
    ph = entry["primary_horizon"]
    outcome = entry["horizons"].get(ph)
    if outcome:
        result = (
            f"{outcome['raw_return']:+.2%} raw | {outcome['alpha']:+.2%} alpha | {ph}"
        )
    else:
        result = "pending"
    return f"[{date} | {entry['symbol']} | {entry['rating']} | {result}]"


def lessons_block(
    symbol: str,
    *,
    n_same: int = 5,
    n_cross: int = 3,
    path: str | Path | None = None,
) -> str:
    """Markdown block for prompt injection: same-symbol history + cross-symbol lessons."""
    entries = load_entries(path)
    if not entries:
        return "No prior committee decisions recorded."

    same = [e for e in entries if e["symbol"] == symbol][-n_same:]
    cross = [
        e for e in entries if e["symbol"] != symbol and e.get("reflection")
    ][-n_cross:]

    lines: list[str] = []
    if same:
        lines.append(f"### Past decisions on {symbol}")
        for e in reversed(same):
            lines.append(f"- {_tag_line(e)}")
            if e.get("reflection"):
                lines.append(f"  Lesson: {e['reflection']}")
    if cross:
        lines.append("### Recent lessons from other assets")
        for e in reversed(cross):
            lines.append(f"- {_tag_line(e)} Lesson: {e['reflection']}")
    return "\n".join(lines) if lines else "No prior committee decisions recorded."
