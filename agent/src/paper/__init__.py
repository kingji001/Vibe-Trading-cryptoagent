"""Paper-trading portfolio engine (deterministic, LLM-free).

``store`` owns persistence (account / positions / ledger / equity under
``VIBE_PAPER_ROOT`` or ``~/.vibe-trading/paper``); ``broker`` owns trading
logic (market fills, mandates, mark-to-market). See the design spec
``docs/superpowers/specs/2026-07-11-paper-trading-loop-design.md`` §3.1.
"""
