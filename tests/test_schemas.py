"""Unit tests for graph/schemas.py — Pydantic boundary model validation.

Covers:
  - StageResult.model_validate() with a minimal dict (just outcome)
  - StageResult with full implementation fields populated
  - StageResult with error outcome and error message
  - RunRecord construction (both direct and nested inside StageResult)

No I/O, no subprocesses, no LangGraph or GitHub calls.
Run with: python -m pytest tests/test_schemas.py -v
"""

import os
import sys

os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
os.environ.setdefault("PROJECT_OWNER", "test-owner")
os.environ.setdefault("PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pydantic import ValidationError

from graph.schemas import StageResult, RunRecord, QualityCheckResult


# ── StageResult minimal (just outcome) ───────────────────────────────────────

class TestStageResultMinimal:
    def test_just_outcome_done(self):
        result = StageResult.model_validate({"outcome": "done"})
        assert result.outcome == "done"

    def test_just_outcome_error(self):
        result = StageResult.model_validate({"outcome": "error"})
        assert result.outcome == "error"

    def test_just_outcome_needs_human(self):
        result = StageResult.model_validate({"outcome": "needs_human"})
        assert result.outcome == "needs_human"

    def test_just_outcome_timeout(self):
        result = StageResult.model_validate({"outcome": "timeout"})
        assert result.outcome == "timeout"

    def test_just_outcome_crash(self):
        result = StageResult.model_validate({"outcome": "crash"})
        assert result.outcome == "crash"

    def test_just_outcome_retry(self):
        result = StageResult.model_validate({"outcome": "retry"})
        assert result.outcome == "retry"

    def test_default_outcome_is_done(self):
        """Empty dict should default outcome to 'done'."""
        result = StageResult.model_validate({})
        assert result.outcome == "done"

    def test_missing_optional_fields_get_defaults(self):
        result = StageResult.model_validate({"outcome": "done"})
        assert result.error == ""
        assert result.plan_content == ""
        assert result.branch == ""
        assert result.files_changed == []
        assert result.commit_shas == []
        assert result.checks == []
        assert result.modules == []
        assert result.pr_number == 0
        assert result.pr_url == ""
        assert result.research_doc_url == ""
        assert result.followup_ticket_numbers == []
        assert result.responded_thread_ids == []
        assert result.self_review_passed is False

    def test_record_default_stage_is_unknown(self):
        result = StageResult.model_validate({"outcome": "done"})
        assert result.record.stage == "unknown"


# ── StageResult with full implementation fields ───────────────────────────────

class TestStageResultFullImpl:
    def _full_impl_dict(self):
        return {
            "outcome": "done",
            "branch": "ANDROID-99-fix-login",
            "files_changed": [
                "app/src/main/Login.kt",
                "app/src/test/LoginTest.kt",
            ],
            "impl_summary": "Fixed null pointer exception in Login screen.",
            "commit_shas": ["abc123", "def456"],
            "record": {
                "stage": "ai_implementation",
                "run_id": "run-42",
                "started_at": 1700000000.0,
                "finished_at": 1700000180.0,
                "outcome": "done",
                "result_path": "/tmp/results/99/ai_implementation_0.json",
            },
        }

    def test_branch_populated(self):
        result = StageResult.model_validate(self._full_impl_dict())
        assert result.branch == "ANDROID-99-fix-login"

    def test_files_changed_populated(self):
        result = StageResult.model_validate(self._full_impl_dict())
        assert len(result.files_changed) == 2
        assert "app/src/main/Login.kt" in result.files_changed

    def test_commit_shas_populated(self):
        result = StageResult.model_validate(self._full_impl_dict())
        assert result.commit_shas == ["abc123", "def456"]

    def test_impl_summary_populated(self):
        result = StageResult.model_validate(self._full_impl_dict())
        assert "null pointer" in result.impl_summary

    def test_nested_record_parsed(self):
        result = StageResult.model_validate(self._full_impl_dict())
        assert result.record.stage == "ai_implementation"
        assert result.record.run_id == "run-42"
        assert result.record.outcome == "done"
        assert result.record.result_path.endswith(".json")

    def test_quality_checks_populated(self):
        data = {
            "outcome": "done",
            "checks": [
                {"check": "detekt", "passed": True, "fixes_applied": False, "error": ""},
                {"check": "lint",   "passed": True, "fixes_applied": True,  "error": ""},
                {"check": "unit_tests", "passed": False, "fixes_applied": False, "error": "2 failures"},
            ],
            "modules": [":app", ":feature-login"],
        }
        result = StageResult.model_validate(data)
        assert len(result.checks) == 3
        assert result.checks[0].check == "detekt"
        assert result.checks[1].fixes_applied is True
        assert result.checks[2].passed is False
        assert result.modules == [":app", ":feature-login"]

    def test_ship_fields_populated(self):
        data = {
            "outcome": "done",
            "pr_number": 1234,
            "pr_url": "https://github.com/Grindr/android/pull/1234",
        }
        result = StageResult.model_validate(data)
        assert result.pr_number == 1234
        assert result.pr_url == "https://github.com/Grindr/android/pull/1234"

    def test_quality_mode_field(self):
        for mode in ("local", "runner"):
            data = {
                "outcome": "done",
                "quality_mode": mode,
                "checks": [{"check": "detekt", "passed": True}],
                "modules": [":core"],
            }
            result = StageResult.model_validate(data)
            assert result.quality_mode == mode

    def test_plan_fields_populated(self):
        data = {
            "outcome": "done",
            "plan_content": "## Plan\n1. Step one\n2. Step two",
            "plan_comment_url": "https://github.com/org/repo/issues/99#comment-111",
            "plan_revision": 2,
        }
        result = StageResult.model_validate(data)
        assert result.plan_content.startswith("## Plan")
        assert result.plan_revision == 2

    def test_spike_fields_populated(self):
        data = {
            "outcome": "done",
            "research_doc_url": "https://github.com/org/repo/issues/5#comment-999",
        }
        result = StageResult.model_validate(data)
        assert "github.com" in result.research_doc_url

    def test_followup_ticket_numbers_populated(self):
        data = {
            "outcome": "done",
            "followup_ticket_numbers": [201, 202, 203],
        }
        result = StageResult.model_validate(data)
        assert result.followup_ticket_numbers == [201, 202, 203]

    def test_responded_thread_ids_populated(self):
        data = {
            "outcome": "done",
            "responded_thread_ids": ["thread-a", "thread-b"],
        }
        result = StageResult.model_validate(data)
        assert result.responded_thread_ids == ["thread-a", "thread-b"]

    def test_self_review_passed_true(self):
        data = {"outcome": "done", "self_review_passed": True}
        result = StageResult.model_validate(data)
        assert result.self_review_passed is True


# ── StageResult with error outcome ────────────────────────────────────────────

class TestStageResultError:
    def test_error_outcome_with_message(self):
        data = {"outcome": "error", "error": "Skill timed out after 3600s"}
        result = StageResult.model_validate(data)
        assert result.outcome == "error"
        assert "timed out" in result.error

    def test_error_outcome_empty_message(self):
        data = {"outcome": "error"}
        result = StageResult.model_validate(data)
        assert result.outcome == "error"
        assert result.error == ""

    def test_crash_sentinel(self):
        data = {"outcome": "crash", "error": "Process exited with code 1"}
        result = StageResult.model_validate(data)
        assert result.outcome == "crash"

    def test_needs_human_outcome(self):
        data = {"outcome": "needs_human", "error": "Quality checks exhausted after 3 rounds"}
        result = StageResult.model_validate(data)
        assert result.outcome == "needs_human"
        assert "exhausted" in result.error

    def test_error_with_partial_record(self):
        """Even on error, a partial record should parse cleanly."""
        data = {
            "outcome": "error",
            "error": "Detekt found 12 issues",
            "record": {
                "stage": "quality",
                "outcome": "error",
                "result_path": "/tmp/results/7/quality_0.json",
            },
        }
        result = StageResult.model_validate(data)
        assert result.outcome == "error"
        assert result.record.stage == "quality"
        assert result.record.outcome == "error"

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValidationError):
            StageResult.model_validate({"outcome": "not_a_valid_outcome"})


# ── RunRecord construction ─────────────────────────────────────────────────────

class TestRunRecordConstruction:
    def test_minimal_run_record(self):
        record = RunRecord(stage="plan")
        assert record.stage == "plan"
        assert record.outcome == "done"
        assert record.run_id == ""
        assert record.result_path == ""

    def test_run_record_with_all_fields(self):
        record = RunRecord(
            stage="quality",
            run_id="run-007",
            started_at=1700000000.0,
            finished_at=1700000300.0,
            outcome="done",
            result_path="/tmp/results/42/quality_0.json",
        )
        assert record.stage == "quality"
        assert record.run_id == "run-007"
        assert record.finished_at == 1700000300.0
        assert record.result_path.endswith(".json")

    def test_run_record_all_outcome_literals(self):
        for outcome in ("done", "error", "needs_human", "timeout", "crash"):
            record = RunRecord(stage="test", outcome=outcome)
            assert record.outcome == outcome

    def test_run_record_invalid_outcome_raises(self):
        with pytest.raises(ValidationError):
            RunRecord(stage="plan", outcome="invalid_outcome")

    def test_run_record_started_at_defaults_to_non_zero(self):
        record = RunRecord(stage="ship")
        assert record.started_at > 0, "started_at must default to current time, not 0"

    def test_run_record_model_validate_from_dict(self):
        data = {
            "stage": "implement",
            "run_id": "abc",
            "outcome": "done",
            "result_path": "/tmp/r.json",
        }
        record = RunRecord.model_validate(data)
        assert record.stage == "implement"
        assert record.result_path == "/tmp/r.json"

    def test_quality_check_result_defaults(self):
        qr = QualityCheckResult()
        assert qr.check == ""
        assert qr.passed is True
        assert qr.fixes_applied is False
        assert qr.error == ""

    def test_quality_check_result_from_dict(self):
        data = {"check": "detekt", "passed": False, "fixes_applied": True, "error": "5 issues"}
        qr = QualityCheckResult.model_validate(data)
        assert qr.check == "detekt"
        assert qr.passed is False
        assert qr.fixes_applied is True
        assert qr.error == "5 issues"
