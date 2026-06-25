"""Unit tests for graph/nodes/recover.py — recovery routing logic.

Covers:
  - Fresh runs (is_recovery=False) → pass-through to route_entry
  - Recovery with explicit target → goto that node + clear flags
  - Recovery with empty target → scan history → goto failed stage
  - Recovery with no history and no target → fallback to route_entry
  - Unknown target → fallback to route_entry
  - All RunRecord stage strings are mapped in _STAGE_TO_NODE
  - All resume nodes have a board status mapping

Run with: python -m pytest tests/test_recover.py -v
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
os.environ.setdefault("PROJECT_OWNER", "test-owner")
os.environ.setdefault("PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from graph.nodes.recover import (
    _NODE_TO_BOARD_STATUS,
    _STAGE_TO_NODE,
    _scan_history_for_target,
    node_recover,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _state(**identity_overrides) -> dict:
    """Build a minimal TicketState for tests."""
    return {
        "identity": {
            "ticket_number": 42,
            "item_id":       "PVTI_test",
            "issue_node_id": "I_test",
            "repo":          "android",
            "repo_full":     "owner/android",
            "is_spike":      False,
            "entry_point":   "plan",
            **identity_overrides,
        },
        "errors": [],
        "run_history": [],
    }


def _run_command(coro):
    """Run an async node and return its Command result."""
    return asyncio.run(coro)


# ── Fresh-run pass-through ────────────────────────────────────────────────────


def test_fresh_run_passes_through_to_route_entry():
    state = _state()  # is_recovery default False
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "route_entry"
    assert cmd.update in (None, {}, ...)  # no state mutation on fast path


def test_fresh_run_with_is_recovery_explicit_false():
    state = _state(is_recovery=False)
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "route_entry"


# ── Recovery with explicit target ─────────────────────────────────────────────


@patch("graph.nodes.recover.move_status")
def test_recovery_with_target_routes_and_clears_flags(mock_move):
    mock_move.side_effect = AsyncMock(return_value=None)
    state = _state(is_recovery=True, recovery_target="quality")
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "quality"
    assert cmd.update["identity"]["is_recovery"] is False
    assert cmd.update["identity"]["recovery_target"] == ""
    assert cmd.update["control"]["current_stage"] == "quality"
    assert any("recovery" in e.lower() for e in cmd.update["errors"])


@patch("graph.nodes.recover.move_status")
def test_recovery_moves_board_status(mock_move):
    mock_move.side_effect = AsyncMock(return_value=None)
    state = _state(is_recovery=True, recovery_target="ship")
    _run_command(node_recover(state))
    mock_move.assert_awaited_once()
    args, _ = mock_move.call_args
    # move_status(client, item_id, status_name)
    assert args[1] == "PVTI_test"
    assert args[2] == "Ready To Ship - AI"


# ── Recovery falls back to history scan ───────────────────────────────────────


@patch("graph.nodes.recover.move_status")
def test_recovery_without_target_scans_history(mock_move):
    mock_move.side_effect = AsyncMock(return_value=None)
    state = _state(is_recovery=True)  # no recovery_target
    state["run_history"] = [
        {"stage": "AI Planning",       "outcome": "done"},
        {"stage": "AI Implementation", "outcome": "done"},
        {"stage": "AI Quality Check",  "outcome": "error"},
    ]
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "quality"


@patch("graph.nodes.recover.move_status")
def test_recovery_picks_latest_failure(mock_move):
    mock_move.side_effect = AsyncMock(return_value=None)
    state = _state(is_recovery=True)
    state["run_history"] = [
        {"stage": "AI Quality Check", "outcome": "error"},   # earlier failure (skipped)
        {"stage": "AI Quality Check", "outcome": "done"},    # then succeeded
        {"stage": "Self Review",      "outcome": "timeout"}, # latest failure (winner)
    ]
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "self_review"


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_recovery_with_empty_history_falls_back_to_route_entry():
    state = _state(is_recovery=True)  # empty run_history, no target
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "route_entry"
    assert cmd.update["identity"]["is_recovery"] is False
    assert cmd.update["identity"]["recovery_target"] == ""


def test_recovery_with_unknown_target_falls_back_to_route_entry():
    state = _state(is_recovery=True, recovery_target="nonexistent_node")
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "route_entry"
    assert cmd.update["identity"]["is_recovery"] is False


def test_recovery_with_all_done_history_returns_none():
    state = _state(is_recovery=True)
    state["run_history"] = [
        {"stage": "AI Planning",       "outcome": "done"},
        {"stage": "AI Implementation", "outcome": "done"},
    ]
    assert _scan_history_for_target(state) is None
    cmd = _run_command(node_recover(state))
    assert cmd.goto == "route_entry"


# ── Mapping completeness ──────────────────────────────────────────────────────


def test_all_stages_have_node_mappings():
    """Every RunRecord.stage emitted by a node must have a _STAGE_TO_NODE entry."""
    # Stage strings used in runner.py STALE_THRESHOLD and node calls.
    expected_stages = {
        "AI Planning", "AI Implementation", "AI Quality Check",
        "Self Review", "Ready To Ship - AI", "Fix CI",
        "Respond To Review", "Spike Followups",
    }
    assert expected_stages.issubset(_STAGE_TO_NODE.keys()), (
        f"missing stages in _STAGE_TO_NODE: {expected_stages - _STAGE_TO_NODE.keys()}"
    )


def test_all_target_nodes_have_board_status():
    """Every resume node must have a board status mapping for the dashboard."""
    for stage, node in _STAGE_TO_NODE.items():
        assert node in _NODE_TO_BOARD_STATUS, (
            f"target node '{node}' (from stage '{stage}') lacks _NODE_TO_BOARD_STATUS entry"
        )


# ── Various outcome values trigger recovery ───────────────────────────────────


@pytest.mark.parametrize("outcome", ["error", "timeout", "crash", "needs_human"])
def test_recovery_treats_non_done_outcomes_as_failures(outcome):
    state = _state(is_recovery=True)
    state["run_history"] = [{"stage": "AI Quality Check", "outcome": outcome}]
    assert _scan_history_for_target(state) == "quality"
