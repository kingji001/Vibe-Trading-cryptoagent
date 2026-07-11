"""Paper-trading persistence layer — atomic file store.

Owns four state files under the paper root (``VIBE_PAPER_ROOT`` override, else
``~/.vibe-trading/paper``):

- ``account.json``   — cash, created_at, config snapshot (fees/slippage at creation)
- ``positions.json`` — open positions (see broker.py for the binding dict shape)
- ``ledger.jsonl``   — append-only fills
- ``equity.jsonl``   — daily mark-to-market snapshots

This module is persistence ONLY: no trading logic, no fill math, no mandates.
Every write goes through ``_atomic_write_text`` (tmp + ``os.replace``), matching
the swarm store's tmp+rename pattern (``src/swarm/task_store.py``); an
interrupted rename leaves the previous file untouched and cleans up the tmp file.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def paper_root() -> Path:
    """Resolve the paper state directory.

    Honors ``VIBE_PAPER_ROOT`` (used by tests) else ``~/.vibe-trading/paper``.
    """
    override = os.environ.get("VIBE_PAPER_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "paper"


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class PaperStore:
    """File-based persistence for a single paper account. All writes atomic."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._account_path = self.root / "account.json"
        self._positions_path = self.root / "positions.json"
        self._ledger_path = self.root / "ledger.jsonl"
        self._equity_path = self.root / "equity.jsonl"
        self._tick_state_path = self.root / "tick_state.json"
        self._lock = threading.Lock()

    # -- atomic primitive --------------------------------------------------- #
    def _atomic_write_text(self, path: Path, text: str) -> None:
        """Write ``text`` to ``path`` atomically via tmp + os.replace.

        If the rename is interrupted the original file is untouched and the
        tmp file is removed, so callers never observe a half-written file.
        """
        tmp_path = path.with_name(path.name + ".tmp")
        with self._lock:
            tmp_path.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise

    # -- account ------------------------------------------------------------ #
    def load_account(self) -> dict | None:
        if not self._account_path.exists():
            return None
        return json.loads(self._account_path.read_text(encoding="utf-8"))

    def create_account(self, start_cash: float, config: dict) -> dict:
        """Create (or recreate) the account with ``start_cash`` and a config snapshot."""
        account = {
            "cash": float(start_cash),
            "created_at": _utc_now_iso(),
            "config": dict(config),
        }
        self._atomic_write_text(self._account_path, json.dumps(account, indent=2))
        return account

    def save_account(self, account: dict) -> None:
        self._atomic_write_text(self._account_path, json.dumps(account, indent=2))

    # -- positions ---------------------------------------------------------- #
    def load_positions(self) -> list[dict]:
        if not self._positions_path.exists():
            return []
        return json.loads(self._positions_path.read_text(encoding="utf-8"))

    def save_positions(self, positions: list[dict]) -> None:
        self._atomic_write_text(self._positions_path, json.dumps(positions, indent=2))

    # -- ledger (append-only) ----------------------------------------------- #
    def append_ledger(self, entry: dict) -> None:
        self._append_jsonl(self._ledger_path, entry)

    def iter_ledger(self) -> Iterator[dict]:
        yield from self._iter_jsonl(self._ledger_path)

    # -- equity (append-only) ----------------------------------------------- #
    def append_equity(self, entry: dict) -> None:
        self._append_jsonl(self._equity_path, entry)

    def iter_equity(self) -> Iterator[dict]:
        yield from self._iter_jsonl(self._equity_path)

    # -- tick state (intraday watermark + event bookkeeping) ---------------- #
    def load_tick_state(self) -> dict:
        """Return the intraday tick state, always with the full schema.

        Shape (Task 1 creates the whole schema; ``last_event_trigger_ts`` and
        the event use of ``last_price`` are populated by Task 3)::

            {"last_bar_ts": {symbol: iso}, "last_event_trigger_ts": {symbol: iso},
             "last_price": {symbol: float}}

        When the file is absent an empty-but-complete schema is returned WITHOUT
        creating it — 1D mode never touches ``tick_state.json`` (byte-identical
        to pre-intraday behavior). A partial on-disk file has any missing
        top-level keys backfilled so callers can index them unconditionally.
        """
        empty = {"last_bar_ts": {}, "last_event_trigger_ts": {}, "last_price": {}}
        if not self._tick_state_path.exists():
            return empty
        data = json.loads(self._tick_state_path.read_text(encoding="utf-8"))
        for key, default in empty.items():
            data.setdefault(key, default)
        return data

    def save_tick_state(self, state: dict) -> None:
        """Persist the intraday tick state atomically (tmp + os.replace)."""
        self._atomic_write_text(self._tick_state_path, json.dumps(state, indent=2))

    # -- jsonl helpers ------------------------------------------------------ #
    def _append_jsonl(self, path: Path, entry: dict) -> None:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        line = json.dumps(entry, ensure_ascii=False)
        self._atomic_write_text(path, existing + line + "\n")

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterator[dict]:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)

    # -- reset -------------------------------------------------------------- #
    def archive_and_reset(self) -> Path:
        """Move current state into a timestamped archive subdir; return its path.

        The live account/positions/ledger/equity files are removed so the next
        operation starts from a clean slate (the broker recreates the account).
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = self.root / f"archive-{stamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for path in (
                self._account_path,
                self._positions_path,
                self._ledger_path,
                self._equity_path,
                self._tick_state_path,
            ):
                if path.exists():
                    path.replace(archive_dir / path.name)
        return archive_dir
