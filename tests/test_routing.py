"""Routing logic tests for the LangGraph-native pipeline.

In the new architecture, routing is embedded inside each node function via
Command(goto=...) — there are no standalone routing functions.  The routing
paths are exercised implicitly through integration tests that run the full
graph with an in-memory checkpointer.

This file verifies that the runner's compute_result_path produces the correct
deterministic paths, since that is the primary idempotency guard.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PROJECT_NODE_ID", "test")
os.environ.setdefault("STATUS_FIELD_ID", "test")
os.environ.setdefault("PROJECT_OWNER", "test")
os.environ.setdefault("PROJECT_NUMBER", "1")
os.environ.setdefault("PIPELINE_RESULTS_DIR", "/tmp/pipeline_test_results")

from graph.runner import compute_result_path


def _state(ticket: int, *, retry: int = 0, ci_fix: int = 0, review_round: int = 0) -> dict:
    return {
        "identity": {"ticket_number": ticket},
        "impl": {"self_review_retry_count": retry},
        "ship": {"ci_fix_count": ci_fix},
        "review": {"review_comment_round": review_round},
        "run_history": [],
    }


# ── compute_result_path ───────────────────────────────────────────────────────

def test_plan_result_path():
    path = compute_result_path(_state(42), "AI Planning")
    assert str(path).endswith("42/ai_planning_0.json")


def test_implement_result_path():
    path = compute_result_path(_state(42), "AI Implementation")
    assert str(path).endswith("42/ai_implementation_0.json")


def test_quality_result_path():
    path = compute_result_path(_state(42), "AI Quality Check")
    assert str(path).endswith("42/ai_quality_check_0.json")


def test_self_review_result_path_with_retry():
    path = compute_result_path(_state(42, retry=2), "Self Review")
    assert str(path).endswith("42/self_review_2.json")


def test_fix_ci_result_path_with_count():
    path = compute_result_path(_state(42, ci_fix=1), "Fix CI")
    assert str(path).endswith("42/fix_ci_1.json")


def test_respond_result_path_with_round():
    path = compute_result_path(_state(42, review_round=2), "Respond to Review")
    assert str(path).endswith("42/respond_to_review_2.json")


def test_ship_result_path():
    path = compute_result_path(_state(42), "Ship")
    assert str(path).endswith("42/ship_0.json")


def test_result_path_uses_ticket_number_as_directory():
    path = compute_result_path(_state(99), "AI Planning")
    parts = path.parts
    assert "99" in parts, f"Expected ticket directory '99' in path parts: {parts}"


def test_result_path_is_deterministic():
    """Same state + same stage must always produce the same path."""
    state = _state(7, retry=1)
    p1 = compute_result_path(state, "AI Self Review")
    p2 = compute_result_path(state, "AI Self Review")
    assert p1 == p2
