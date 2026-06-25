"""Tests for the new LangGraph-native pipeline graph (graph/workflow.py).

Covers:
  - build_workflow() compiles without error using MemorySaver
  - All new node names are present after compilation
  - visualize_workflow() returns a non-empty string
  - Every edge target in the compiled graph is a valid registered node

No mocking of LangGraph internals; no real GitHub or subprocess calls.
Run with: python -m pytest tests/test_new_graph.py -v
"""

import os
import sys

os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
os.environ.setdefault("PROJECT_OWNER", "test-owner")
os.environ.setdefault("PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _get_compiled_graph():
    """Return a compiled graph instance backed by MemorySaver."""
    from langgraph.checkpoint.memory import MemorySaver
    from graph.workflow import build_workflow
    return build_workflow(MemorySaver())


# Expected node names for the new LangGraph-native topology (§1b of the plan).
# `recover` is the entry node that pass-throughs to route_entry on fresh runs
# and routes to the failed stage when restarting an errored ticket.
EXPECTED_NODES = {
    "recover",
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

# Nodes that should NOT be present in the new graph (they were part of the old
# spawn_* / wait_* pattern that the refactor removes)
REMOVED_NODES = {
    "spawn_plan", "wait_plan", "move_to_plan_review", "wait_plan_approval",
    "spawn_implement", "wait_implement",
    "spawn_self_review", "wait_self_review", "move_to_impl_review", "wait_impl_approval",
    "spawn_ship", "wait_ship", "move_to_in_pr",
    "spawn_fix_ci", "wait_fix_ci",
    "spawn_respond", "wait_respond",
    "spawn_followups", "wait_followups",
}


# ── Compilation tests ─────────────────────────────────────────────────────────

class TestBuildWorkflow:
    def test_compiles_without_error(self):
        """build_workflow(MemorySaver()) must not raise."""
        graph = _get_compiled_graph()
        assert graph is not None

    def test_all_new_node_names_present(self):
        graph = _get_compiled_graph()
        node_names = set(graph.get_graph().nodes.keys())
        missing = EXPECTED_NODES - node_names
        assert not missing, (
            f"New topology is missing expected nodes: {sorted(missing)}\n"
            f"Actual nodes: {sorted(node_names)}"
        )

    def test_old_spawn_wait_nodes_are_absent(self):
        """The old spawn_*/wait_* nodes should not exist in the new graph."""
        graph = _get_compiled_graph()
        node_names = set(graph.get_graph().nodes.keys())
        leftover = REMOVED_NODES & node_names
        assert not leftover, (
            f"Old spawn/wait nodes still present — refactor incomplete: {sorted(leftover)}"
        )

    def test_recover_is_entry_point(self):
        """The graph's entry node must be `recover` (which pass-throughs to
        route_entry for fresh runs and routes to the failed stage on recovery).
        """
        graph = _get_compiled_graph()
        g = graph.get_graph()
        start_edges = [e for e in g.edges if e.source == "__start__"]
        targets = {e.target for e in start_edges}
        assert "recover" in targets, (
            f"recover is not the entry point; __start__ edges: {targets}"
        )

    def test_terminal_nodes_are_present(self):
        graph = _get_compiled_graph()
        node_names = set(graph.get_graph().nodes.keys())
        for terminal in ("done", "needs_human", "escalate_error"):
            assert terminal in node_names, f"Terminal node '{terminal}' is missing"

    def test_gate_nodes_are_present(self):
        graph = _get_compiled_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "gate_plan_approval"  in node_names, "gate_plan_approval node is missing"
        assert "gate_impl_approval"  in node_names, "gate_impl_approval node is missing"

    def test_node_count_matches_expected(self):
        """Graph should have exactly the nodes in EXPECTED_NODES plus __start__/__end__."""
        graph = _get_compiled_graph()
        node_names = set(graph.get_graph().nodes.keys())
        # Strip LangGraph-internal virtual nodes
        real_nodes = {n for n in node_names if not n.startswith("__")}
        assert real_nodes == EXPECTED_NODES, (
            f"Node set mismatch.\n"
            f"  Extra nodes (not expected): {sorted(real_nodes - EXPECTED_NODES)}\n"
            f"  Missing nodes (expected but absent): {sorted(EXPECTED_NODES - real_nodes)}"
        )


# ── visualize_workflow tests ──────────────────────────────────────────────────

class TestVisualizeWorkflow:
    def test_returns_non_empty_string(self):
        from graph.workflow import visualize_workflow
        diagram = visualize_workflow()
        assert isinstance(diagram, str), "visualize_workflow() must return a str"
        assert len(diagram) > 100, (
            f"Diagram string is suspiciously short ({len(diagram)} chars) — "
            "likely an empty or degenerate render"
        )

    def test_contains_node_names(self):
        """The mermaid/string output should reference key node names."""
        from graph.workflow import visualize_workflow
        diagram = visualize_workflow()
        for node in ("route_entry", "plan", "ship", "done"):
            assert node in diagram, (
                f"Node '{node}' not found in visualize_workflow() output"
            )

    def test_no_exception_on_repeated_calls(self):
        """Calling visualize_workflow() twice must not raise."""
        from graph.workflow import visualize_workflow
        d1 = visualize_workflow()
        d2 = visualize_workflow()
        assert d1 == d2, "Repeated calls to visualize_workflow() should be deterministic"


# ── Edge validity tests ───────────────────────────────────────────────────────

class TestEdgeValidity:
    def test_all_edge_targets_are_valid_nodes(self):
        """Every edge target (excluding __end__) must be a registered node."""
        graph = _get_compiled_graph()
        g = graph.get_graph()
        node_names = set(g.nodes.keys())

        invalid_targets = []
        for edge in g.edges:
            if edge.target not in ("__end__",) and edge.target not in node_names:
                invalid_targets.append(
                    f"  source={edge.source!r} → target={edge.target!r}"
                )

        assert not invalid_targets, (
            "The following edges point to non-existent nodes:\n"
            + "\n".join(invalid_targets)
        )

    def test_all_edge_sources_are_valid_nodes(self):
        """Every edge source (excluding __start__) must be a registered node."""
        graph = _get_compiled_graph()
        g = graph.get_graph()
        node_names = set(g.nodes.keys())

        invalid_sources = []
        for edge in g.edges:
            if edge.source not in ("__start__",) and edge.source not in node_names:
                invalid_sources.append(
                    f"  source={edge.source!r} → target={edge.target!r}"
                )

        assert not invalid_sources, (
            "The following edges originate from non-existent nodes:\n"
            + "\n".join(invalid_sources)
        )

    def test_terminal_nodes_reach_end(self):
        """done, needs_human, and escalate_error must each have an edge to __end__."""
        graph = _get_compiled_graph()
        g = graph.get_graph()
        end_sources = {e.source for e in g.edges if e.target == "__end__"}
        for terminal in ("done", "needs_human", "escalate_error"):
            assert terminal in end_sources, (
                f"Terminal node '{terminal}' does not have an edge to __end__"
            )

    def test_quality_can_route_to_needs_human(self):
        """quality node must have an edge to needs_human (for infrastructure failures)."""
        graph = _get_compiled_graph()
        g = graph.get_graph()
        quality_targets = {e.target for e in g.edges if e.source == "quality"}
        assert "needs_human" in quality_targets, (
            f"quality node missing needs_human edge — infra-failure path broken. "
            f"Actual targets: {quality_targets}"
        )

    def test_no_self_loops_on_non_retryable_nodes(self):
        """Only ship and implement may loop back to themselves (retry paths)."""
        graph = _get_compiled_graph()
        g = graph.get_graph()
        retryable = {"ship", "implement", "__start__", "__end__"}
        self_loops = [
            e for e in g.edges
            if e.source == e.target and e.source not in retryable
        ]
        assert not self_loops, (
            f"Unexpected self-loops found: {[(e.source, e.target) for e in self_loops]}"
        )
