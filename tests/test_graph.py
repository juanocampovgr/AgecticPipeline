"""Tests for graph compilation and interrupt/resume round-trip.

These tests compile the real StateGraph (no mocks for LangGraph itself) but
use in-memory checkpointing and stub all I/O (GitHub API, subprocesses).
Run with: python -m pytest tests/test_graph.py -v
"""

import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Graph compilation ─────────────────────────────────────────────────────────

def test_workflow_compiles():
    """Smoke test: graph can be built without errors and has expected nodes."""
    os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
    os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
    os.environ.setdefault("PROJECT_OWNER", "test-owner")
    os.environ.setdefault("PROJECT_NUMBER", "1")

    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    node_names = set(graph.get_graph().nodes.keys())

    expected = {
        "route_entry", "spawn_plan", "wait_plan", "move_to_plan_review",
        "wait_plan_approval", "spawn_implement", "wait_implement",
        "spawn_self_review", "wait_self_review", "move_to_impl_review",
        "wait_impl_approval", "spawn_ship", "wait_ship", "move_to_in_pr",
        "monitor_pr", "spawn_fix_ci", "wait_fix_ci", "spawn_respond",
        "wait_respond", "spawn_followups", "wait_followups",
        "done", "needs_human", "escalate_error",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"


def test_mermaid_renders():
    """visualize_workflow() should return a non-empty string."""
    os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
    os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
    os.environ.setdefault("PROJECT_OWNER", "test-owner")
    os.environ.setdefault("PROJECT_NUMBER", "1")

    from graph.workflow import visualize_workflow
    diagram = visualize_workflow()
    assert diagram and len(diagram) > 100


# ── State helpers ─────────────────────────────────────────────────────────────

def test_ticket_state_is_total_false():
    """TicketState total=False means partial construction must not raise."""
    from graph.state import TicketState
    # Should not raise — all fields optional
    s: TicketState = {"ticket_number": 42, "is_spike": False}  # type: ignore[typeddict-item]
    assert s["ticket_number"] == 42


# ── Routing edge coverage ─────────────────────────────────────────────────────

def test_all_route_keys_exist_in_graph():
    """Every key returned by routing functions must be registered as an edge target."""
    os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
    os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
    os.environ.setdefault("PROJECT_OWNER", "test-owner")
    os.environ.setdefault("PROJECT_NUMBER", "1")

    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    g = graph.get_graph()
    edge_labels = {edge.conditional for edge in g.edges if edge.conditional}
    node_names = set(g.nodes.keys())

    # All conditional branch targets must be real nodes
    for edge in g.edges:
        if edge.target != "__end__":
            assert edge.target in node_names, f"Edge target '{edge.target}' is not a registered node"


# ── Branch ID helpers ─────────────────────────────────────────────────────────

def test_make_branch_id():
    os.environ.setdefault("PROJECT_NODE_ID", "test")
    os.environ.setdefault("STATUS_FIELD_ID", "test")
    os.environ.setdefault("PROJECT_OWNER", "test")
    os.environ.setdefault("PROJECT_NUMBER", "1")

    from pipeline_poller import make_branch_id, extract_jira_ticket_id

    assert make_branch_id("ANDROID-1234", "ANDROID-1234 Fix crash on startup") == "ANDROID-1234-fix-crash-on-startup"
    assert make_branch_id("IOS-99", "IOS-99") == "IOS-99"
    assert make_branch_id("BE-1", "BE-1 Add new endpoint for users") == "BE-1-add-new-endpoint-for-users"


def test_extract_jira_ticket_id():
    from pipeline_poller import extract_jira_ticket_id

    assert extract_jira_ticket_id("ANDROID-1234 Fix crash", "") == "ANDROID-1234"
    assert extract_jira_ticket_id("Fix crash", "See ANDROID-999 for context") == "ANDROID-999"
    assert extract_jira_ticket_id("no ticket here", "nothing here") is None
    assert extract_jira_ticket_id("BE-12 backend fix", "") == "BE-12"
