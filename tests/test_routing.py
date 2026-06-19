"""Unit tests for all LangGraph routing functions in graph/nodes.py.

No I/O, no subprocesses, no GitHub API calls — pure state → route-key assertions.
Run with: python -m pytest tests/test_routing.py -v
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from graph.nodes import (
    route_entry,
    route_plan_marker,
    route_impl_marker,
    route_self_review,
    route_impl_approval,
    route_ship_marker,
    route_monitor_pr,
    route_fix_ci,
    route_respond,
)


# ── route_entry ───────────────────────────────────────────────────────────────

def test_route_entry_normal():
    assert route_entry({}) == "normal"
    assert route_entry({"is_spike": False}) == "normal"
    assert route_entry({"entry_point": "plan"}) == "normal"


def test_route_entry_spike():
    assert route_entry({"is_spike": True}) == "spike"


def test_route_entry_implement():
    assert route_entry({"entry_point": "implement"}) == "implement"


def test_route_entry_spike_beats_implement():
    # spike flag takes priority in route_entry
    assert route_entry({"is_spike": True, "entry_point": "implement"}) == "spike"


# ── route_plan_marker ─────────────────────────────────────────────────────────

def test_route_plan_marker_done():
    assert route_plan_marker({"_plan_marker_outcome": "done"}) == "done"


def test_route_plan_marker_error():
    assert route_plan_marker({"_plan_marker_outcome": "error"}) == "error"


def test_route_plan_marker_default():
    assert route_plan_marker({}) == "done"


# ── route_impl_marker ─────────────────────────────────────────────────────────

def test_route_impl_marker_non_spike_success():
    assert route_impl_marker({"_impl_marker_outcome": "done", "is_spike": False}) == "self_review"


def test_route_impl_marker_spike_done():
    assert route_impl_marker({"_impl_marker_outcome": "done", "is_spike": True}) == "spike_done"


def test_route_impl_marker_error():
    assert route_impl_marker({"_impl_marker_outcome": "error"}) == "error"
    assert route_impl_marker({"_impl_marker_outcome": "error", "is_spike": True}) == "error"


def test_route_impl_marker_default_non_spike():
    assert route_impl_marker({}) == "self_review"


# ── route_self_review ─────────────────────────────────────────────────────────

def test_route_self_review_passed():
    assert route_self_review({"_self_review_outcome": "done"}) == "proceed"
    assert route_self_review({}) == "proceed"


def test_route_self_review_retry_first():
    assert route_self_review({"_self_review_outcome": "error", "self_review_retry_count": 1}) == "retry"


def test_route_self_review_escalate_at_max():
    assert route_self_review({"_self_review_outcome": "error", "self_review_retry_count": 2}) == "escalate"
    assert route_self_review({"_self_review_outcome": "error", "self_review_retry_count": 5}) == "escalate"


# ── route_impl_approval ───────────────────────────────────────────────────────

def test_route_impl_approval_ship():
    assert route_impl_approval({"impl_approval_type": "impl-approved", "is_spike": False}) == "ship"
    assert route_impl_approval({}) == "ship"


def test_route_impl_approval_spike_done():
    assert route_impl_approval({"impl_approval_type": "impl-approved", "is_spike": True}) == "spike_done"


def test_route_impl_approval_followups():
    assert route_impl_approval({"impl_approval_type": "followup-approved", "is_spike": True}) == "followups"


def test_route_impl_approval_followup_non_spike_goes_to_spike_done():
    # followup-approved only makes sense for spike; non-spike falls through to spike_done
    assert route_impl_approval({"impl_approval_type": "followup-approved", "is_spike": False}) == "ship"


# ── route_ship_marker ─────────────────────────────────────────────────────────

def test_route_ship_marker():
    assert route_ship_marker({"_ship_marker_outcome": "done"}) == "done"
    assert route_ship_marker({"_ship_marker_outcome": "error"}) == "error"
    assert route_ship_marker({"_ship_marker_outcome": "retry"}) == "retry"
    assert route_ship_marker({}) == "done"


# ── route_monitor_pr ──────────────────────────────────────────────────────────

def test_route_monitor_pr():
    assert route_monitor_pr({"pr_outcome": "done"}) == "done"
    assert route_monitor_pr({"pr_outcome": "fix_ci"}) == "fix_ci"
    assert route_monitor_pr({"pr_outcome": "respond"}) == "respond"
    assert route_monitor_pr({"pr_outcome": "needs_human"}) == "needs_human"
    assert route_monitor_pr({}) == "done"


# ── route_fix_ci ──────────────────────────────────────────────────────────────

def test_route_fix_ci_continue():
    assert route_fix_ci({"_fix_ci_outcome": "done", "ci_fix_count": 1}) == "monitor_pr"


def test_route_fix_ci_needs_human_direct():
    assert route_fix_ci({"_fix_ci_outcome": "needs_human"}) == "needs_human"


def test_route_fix_ci_needs_human_at_limit():
    assert route_fix_ci({"_fix_ci_outcome": "done", "ci_fix_count": 3}) == "needs_human"
    assert route_fix_ci({"_fix_ci_outcome": "done", "ci_fix_count": 4}) == "needs_human"


# ── route_respond ─────────────────────────────────────────────────────────────

def test_route_respond_continue():
    assert route_respond({"_respond_outcome": "done", "review_comment_round": 1}) == "monitor_pr"


def test_route_respond_needs_human_direct():
    assert route_respond({"_respond_outcome": "needs_human"}) == "needs_human"


def test_route_respond_needs_human_at_limit():
    assert route_respond({"_respond_outcome": "done", "review_comment_round": 2}) == "needs_human"
    assert route_respond({"_respond_outcome": "done", "review_comment_round": 5}) == "needs_human"
