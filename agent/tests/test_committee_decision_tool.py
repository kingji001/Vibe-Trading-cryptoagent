"""Tests for SubmitDecisionTool.execute (src/tools/committee_decision_tool.py).

Direct tool-level coverage: the schema-level rules already live in
test_committee_schemas.py and the preset-wiring rules in
test_crypto_committee_preset.py, but nothing previously drove
``SubmitDecisionTool.execute`` itself -- the validation-error JSON shape
workers actually retry against, the run_dir persistence side effect, and
the unknown-schema / non-dict-payload guard clauses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.committee.schemas import render_markdown
from src.tools.committee_decision_tool import SubmitDecisionTool

LONG = "x" * 120

VALID_PAYLOADS = {
    "research_plan": {
        "recommendation": "Buy",
        "rationale": LONG,
        "strategic_actions": ["scale in on dips", "invalidate below 60000"],
    },
    "trader_proposal": {
        "action": "Buy",
        "reasoning": LONG,
        "entry_price": 65000,
        "stop_loss": 61200,
        "take_profit": 70000,
        "position_sizing": "half size",
    },
    "portfolio_decision": {
        "rating": "Hold",
        "executive_summary": LONG,
        "investment_thesis": LONG * 2,
        "time_horizon": "72h swing",
        "price_target": 68000,
    },
    "sentiment_report": {
        "sentiment": "bullish",
        "score_0_10": 7.5,
        "confidence": "medium",
        "narrative": LONG,
    },
}


@pytest.fixture()
def tool() -> SubmitDecisionTool:
    return SubmitDecisionTool()


# ---------------------------------------------------------------------------
# Valid payloads: one per schema
# ---------------------------------------------------------------------------


class TestValidPayloads:
    @pytest.mark.parametrize("schema_name", sorted(VALID_PAYLOADS))
    def test_valid_payload_ok_and_persisted(
        self, tool: SubmitDecisionTool, tmp_path: Path, schema_name: str
    ) -> None:
        result = json.loads(
            tool.execute(
                schema=schema_name,
                payload=VALID_PAYLOADS[schema_name],
                run_dir=str(tmp_path),
            )
        )

        assert result["status"] == "ok"
        assert result["schema"] == schema_name

        expected_path = tmp_path / f"decision.{schema_name}.json"
        assert result["saved_to"] == str(expected_path)
        assert expected_path.exists()

        persisted = json.loads(expected_path.read_text(encoding="utf-8"))
        assert persisted == result["decision"]

        from src.committee.schemas import SCHEMAS

        model = SCHEMAS[schema_name].model_validate(VALID_PAYLOADS[schema_name])
        assert result["rendered_markdown"] == render_markdown(schema_name, model)
        assert "next_step" in result


# ---------------------------------------------------------------------------
# Invalid payloads: field issue shape
# ---------------------------------------------------------------------------


class TestInvalidPayloads:
    def test_missing_required_field_reports_field_and_problem(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        payload = {
            "action": "Buy",
            # reasoning omitted
        }
        result = json.loads(
            tool.execute(schema="trader_proposal", payload=payload, run_dir=str(tmp_path))
        )

        assert result["status"] == "error"
        assert result["issues"], result
        for issue in result["issues"]:
            assert set(issue) == {"field", "problem"}
        fields = {issue["field"] for issue in result["issues"]}
        assert "reasoning" in fields
        # nothing persisted on validation failure
        assert not list(tmp_path.iterdir())

    def test_nested_list_item_error_uses_dotted_index_path(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        """Real-world worker-retry case: a bad element inside
        strategic_actions must surface as 'strategic_actions.0', not a bare
        'strategic_actions' or an opaque pydantic loc tuple."""
        payload = {
            "recommendation": "Buy",
            "rationale": LONG,
            "strategic_actions": [123],
        }
        result = json.loads(
            tool.execute(schema="research_plan", payload=payload, run_dir=str(tmp_path))
        )

        assert result["status"] == "error"
        issue = next(i for i in result["issues"] if i["field"] == "strategic_actions.0")
        assert "string" in issue["problem"].lower()
        assert not list(tmp_path.iterdir())

    def test_short_rationale_reports_min_length_issue(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        payload = {
            "recommendation": "Buy",
            "rationale": "too short",
            "strategic_actions": ["a"],
        }
        result = json.loads(
            tool.execute(schema="research_plan", payload=payload, run_dir=str(tmp_path))
        )
        assert result["status"] == "error"
        fields = {issue["field"] for issue in result["issues"]}
        assert "rationale" in fields

    def test_extra_field_forbidden_surfaces_as_issue(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        payload = dict(VALID_PAYLOADS["portfolio_decision"])
        payload["surprise_field"] = "not allowed"
        result = json.loads(
            tool.execute(schema="portfolio_decision", payload=payload, run_dir=str(tmp_path))
        )

        assert result["status"] == "error"
        issue = next(i for i in result["issues"] if i["field"] == "surprise_field")
        assert "extra" in issue["problem"].lower()
        assert not list(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# Unknown schema / non-dict payload guard clauses
# ---------------------------------------------------------------------------


class TestGuardClauses:
    def test_unknown_schema_name_errors_and_persists_nothing(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        result = json.loads(
            tool.execute(schema="not_a_real_schema", payload={}, run_dir=str(tmp_path))
        )
        assert result["status"] == "error"
        assert "not_a_real_schema" in result["error"]
        assert "issues" not in result
        assert not list(tmp_path.iterdir())

    def test_missing_schema_kwarg_treated_as_unknown(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        result = json.loads(tool.execute(payload={}, run_dir=str(tmp_path)))
        assert result["status"] == "error"
        assert not list(tmp_path.iterdir())

    @pytest.mark.parametrize("bad_payload", [None, "a string", ["a", "list"], 42])
    def test_non_dict_payload_errors_and_persists_nothing(
        self, tool: SubmitDecisionTool, tmp_path: Path, bad_payload: object
    ) -> None:
        result = json.loads(
            tool.execute(
                schema="trader_proposal", payload=bad_payload, run_dir=str(tmp_path)
            )
        )
        assert result["status"] == "error"
        assert "payload" in result["error"]
        assert not list(tmp_path.iterdir())

    def test_missing_payload_kwarg_errors(self, tool: SubmitDecisionTool, tmp_path: Path) -> None:
        result = json.loads(
            tool.execute(schema="trader_proposal", run_dir=str(tmp_path))
        )
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Persistence details
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_no_run_dir_still_succeeds_with_no_saved_to(self, tool: SubmitDecisionTool) -> None:
        result = json.loads(
            tool.execute(
                schema="sentiment_report", payload=VALID_PAYLOADS["sentiment_report"]
            )
        )
        assert result["status"] == "ok"
        assert result["saved_to"] is None

    def test_run_dir_created_when_missing(self, tool: SubmitDecisionTool, tmp_path: Path) -> None:
        """Current behavior: a non-existent run_dir (including nested missing
        parents) is created via mkdir(parents=True, exist_ok=True) rather
        than treated as an error."""
        missing_dir = tmp_path / "nested" / "run123"
        assert not missing_dir.exists()

        result = json.loads(
            tool.execute(
                schema="sentiment_report",
                payload=VALID_PAYLOADS["sentiment_report"],
                run_dir=str(missing_dir),
            )
        )

        assert result["status"] == "ok"
        expected_path = missing_dir / "decision.sentiment_report.json"
        assert result["saved_to"] == str(expected_path)
        assert expected_path.exists()

    def test_persistence_failure_is_swallowed_and_status_stays_ok(
        self, tool: SubmitDecisionTool, tmp_path: Path
    ) -> None:
        """Current behavior: persistence is best-effort. If run_dir collides
        with an existing file (mkdir cannot create a directory there), the
        exception is caught, saved_to comes back None, but the decision is
        still reported 'ok' -- the validated payload is not lost, only the
        on-disk copy."""
        blocked_run_dir = tmp_path / "blocked"
        blocked_run_dir.write_text("not a directory", encoding="utf-8")

        result = json.loads(
            tool.execute(
                schema="sentiment_report",
                payload=VALID_PAYLOADS["sentiment_report"],
                run_dir=str(blocked_run_dir),
            )
        )

        assert result["status"] == "ok"
        assert result["saved_to"] is None

    def test_file_naming_matches_schema(self, tool: SubmitDecisionTool, tmp_path: Path) -> None:
        result = json.loads(
            tool.execute(
                schema="research_plan",
                payload=VALID_PAYLOADS["research_plan"],
                run_dir=str(tmp_path),
            )
        )
        assert result["status"] == "ok"
        files = list(tmp_path.iterdir())
        assert [f.name for f in files] == ["decision.research_plan.json"]

    def test_second_submit_overwrites_first(self, tool: SubmitDecisionTool, tmp_path: Path) -> None:
        """Current behavior: submitting the same schema twice to the same
        run_dir overwrites the prior JSON file in place -- there is no
        versioning or append; the last successful submit wins."""
        first = dict(VALID_PAYLOADS["trader_proposal"])
        second = dict(VALID_PAYLOADS["trader_proposal"])
        second["reasoning"] = "y" * 120
        second["action"] = "Sell"

        r1 = json.loads(
            tool.execute(schema="trader_proposal", payload=first, run_dir=str(tmp_path))
        )
        r2 = json.loads(
            tool.execute(schema="trader_proposal", payload=second, run_dir=str(tmp_path))
        )

        assert r1["status"] == "ok" and r2["status"] == "ok"
        files = list(tmp_path.iterdir())
        assert len(files) == 1  # overwritten, not a second file

        persisted = json.loads(files[0].read_text(encoding="utf-8"))
        assert persisted["action"] == "Sell"
        assert persisted != r1["decision"]
        assert persisted == r2["decision"]
