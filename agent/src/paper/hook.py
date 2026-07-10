"""Post-journal-append execution hook (Task 5).

Called from ``committee_journal_tool``'s ``action="append"`` success path,
right after a decision is durably journaled. Translates the freshly journaled
entry into a paper trade via ``execute_decision`` (Task 4) against a default
``PaperBroker`` (Task 2/3).

Failure isolation is the entire point of this module: the journal append has
already succeeded by the time this is called, and nothing that happens here —
a disabled kill switch, a broker/store construction error, a price fetch
failure, an unexpected exception anywhere in the translator — may ever change
that outcome. ``maybe_execute_paper`` therefore NEVER raises.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _paper_enabled() -> bool:
    """``VIBE_PAPER_ENABLED`` truthiness: unset -> enabled; "0"/"false"/"" -> disabled.

    Mirrors ``src.paper.translator._paper_enabled`` exactly (kept as a small
    local copy rather than importing that module eagerly, so the "return None
    fast" contract below never has to import the paper package at all when
    disabled).
    """
    val = os.environ.get("VIBE_PAPER_ENABLED")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "")


def maybe_execute_paper(entry: dict) -> dict | None:
    """Execute a freshly journaled decision as a paper trade, or skip fast.

    Returns ``None`` immediately (no imports, no account touched) when
    ``VIBE_PAPER_ENABLED`` is falsy — the caller must treat ``None`` as "omit
    the paper_execution key entirely", not as an empty result.

    When enabled, constructs a default ``PaperBroker`` (default price_fn,
    ``PaperStore`` rooted at ``paper_root()``) and calls ``execute_decision``.
    ANY exception raised during broker construction or execution is caught
    here and returned as ``{"error": str(exc)}`` — this function must never
    raise, so a paper-execution crash can never fail (or even be visible to)
    the journal append that triggered it.
    """
    if not _paper_enabled():
        return None
    try:
        from src.paper.broker import PaperBroker
        from src.paper.store import PaperStore, paper_root
        from src.paper.translator import execute_decision

        store = PaperStore(paper_root())
        broker = PaperBroker(store)
        return execute_decision(entry, broker)
    except Exception as exc:  # never raise — the journal append must not fail
        logger.exception(
            "paper execution hook failed for decision %s", entry.get("id")
        )
        return {"error": str(exc)}
