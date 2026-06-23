"""Tests for graph compilation and node registration.

These tests compile the real StateGraph (no mocks for LangGraph itself) but
use in-memory checkpointing.  All 15 nodes use Command(goto=...) for routing;
there are no conditional-edge routing functions to test separately.
Run with: python -m pytest tests/test_graph.py -v
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
os.environ.setdefault("PROJECT_OWNER", "test-owner")
os.environ.setdefault("PROJECT_NUMBER", "1")


# ── Graph compilation ─────────────────────────────────────────────────────────

def test_workflow_compiles():
    """Smoke test: graph can be built without errors and has the expected 15 nodes."""
    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    node_names = set(graph.get_graph().nodes.keys())

    expected = {
        "route_entry",
        "plan",
        "gate_plan_approval",
        "implement",
        "quality",
        "self_review",
        "gate_impl_approval",
        "ship",
        "monitor_pr",
        "fix_ci",
        "respond",
        "followups",
        "done",
        "needs_human",
        "escalate_error",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"


def test_workflow_node_count():
    """Graph must have exactly 17 nodes (15 domain + __start__ + __end__)."""
    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    nodes = graph.get_graph().nodes
    assert len(nodes) == 17, f"Expected 17 nodes, got {len(nodes)}: {set(nodes.keys())}"


def test_mermaid_renders():
    """visualize_workflow() must return a non-empty string."""
    from graph.workflow import visualize_workflow
    diagram = visualize_workflow()
    assert diagram and len(diagram) > 100


# ── Edge coverage ─────────────────────────────────────────────────────────────

def test_all_edge_targets_are_registered_nodes():
    """Every edge target (except __end__) must be a registered node."""
    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    g = graph.get_graph()
    node_names = set(g.nodes.keys())

    for edge in g.edges:
        if edge.target != "__end__":
            assert edge.target in node_names, (
                f"Edge target '{edge.target}' is not a registered node"
            )


def test_terminal_nodes_have_end_edges():
    """done, needs_human, and escalate_error must all have an edge to END."""
    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow

    graph = build_workflow(MemorySaver())
    g = graph.get_graph()
    end_sources = {e.source for e in g.edges if e.target == "__end__"}

    for terminal in ("done", "needs_human", "escalate_error"):
        assert terminal in end_sources, f"Terminal node '{terminal}' has no edge to END"


# ── Node imports ──────────────────────────────────────────────────────────────

def test_all_node_callables_importable():
    """Every node listed in graph/nodes/__init__.py must be importable."""
    from graph.nodes import (
        node_route_entry,
        node_plan,
        gate_plan_approval,
        node_implement,
        node_quality,
        node_self_review,
        gate_impl_approval,
        node_ship,
        node_monitor_pr,
        node_fix_ci,
        node_respond,
        node_followups,
        node_done,
        node_needs_human,
        node_escalate_error,
    )
    callables = [
        node_route_entry, node_plan, gate_plan_approval, node_implement,
        node_quality, node_self_review, gate_impl_approval, node_ship,
        node_monitor_pr, node_fix_ci, node_respond, node_followups,
        node_done, node_needs_human, node_escalate_error,
    ]
    for fn in callables:
        assert callable(fn), f"{fn!r} is not callable"


# ── State construction ────────────────────────────────────────────────────────

def test_ticket_state_nested_identity():
    """TicketState is total=False — nested identity group must round-trip cleanly."""
    from graph.state import TicketState, Identity
    identity: Identity = {"ticket_number": 42, "repo": "android", "is_spike": False}
    s: TicketState = {"identity": identity}
    assert s["identity"]["ticket_number"] == 42
    assert s["identity"]["repo"] == "android"


# ── Pipeline-poller helpers ───────────────────────────────────────────────────

def test_make_branch_id():
    from pipeline_poller import make_branch_id
    assert make_branch_id("ANDROID-1234", "ANDROID-1234 Fix crash on startup") == "ANDROID-1234-fix-crash-on-startup"
    assert make_branch_id("IOS-99", "IOS-99") == "IOS-99"
    assert make_branch_id("BE-1", "BE-1 Add new endpoint for users") == "BE-1-add-new-endpoint-for-users"


def test_extract_jira_ticket_id():
    from pipeline_poller import extract_jira_ticket_id
    assert extract_jira_ticket_id("ANDROID-1234 Fix crash", "") == "ANDROID-1234"
    assert extract_jira_ticket_id("Fix crash", "See ANDROID-999 for context") == "ANDROID-999"
    assert extract_jira_ticket_id("no ticket here", "nothing here") is None
    assert extract_jira_ticket_id("BE-12 backend fix", "") == "BE-12"
