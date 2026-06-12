"""LangGraph StateGraph definition for the AgenticPipeline ticket workflow.

Graph topology:
  Non-spike: route_entry → spawn_plan → wait_plan → move_to_plan_review
             → wait_plan_approval → spawn_implement → wait_implement
             → spawn_self_review → wait_self_review
             → move_to_impl_review → wait_impl_approval
             → spawn_ship → wait_ship → move_to_in_pr
             → monitor_pr → [done | fix_ci | respond | needs_human]

  Spike:     route_entry → spawn_implement → wait_implement → move_to_impl_review
             → wait_impl_approval → [impl-approved → done | followup-approved → followups → done]
"""

from langgraph.graph import StateGraph, END

from graph.state import TicketState
from graph.nodes import (
    node_route_entry,
    node_spawn_plan, node_wait_plan_marker, node_move_to_plan_review, node_wait_plan_approval,
    node_spawn_implement, node_wait_impl_marker,
    node_spawn_self_review, node_wait_self_review_marker,
    node_move_to_impl_review, node_wait_impl_approval,
    node_spawn_ship, node_wait_ship_marker, node_move_to_in_pr,
    node_monitor_pr,
    node_spawn_fix_ci, node_wait_fix_ci_marker,
    node_spawn_respond_to_review, node_wait_respond_marker,
    node_spawn_followups, node_wait_followups_marker,
    node_done, node_needs_human, node_escalate_error,
    route_entry, route_plan_marker, route_impl_marker, route_self_review,
    route_impl_approval, route_ship_marker, route_monitor_pr, route_fix_ci, route_respond,
)


def build_workflow(checkpointer):
    """Build and compile the ticket workflow graph with the given checkpointer."""
    builder = StateGraph(TicketState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("route_entry",           node_route_entry)
    builder.add_node("spawn_plan",            node_spawn_plan)
    builder.add_node("wait_plan",             node_wait_plan_marker)
    builder.add_node("move_to_plan_review",   node_move_to_plan_review)
    builder.add_node("wait_plan_approval",    node_wait_plan_approval)
    builder.add_node("spawn_implement",       node_spawn_implement)
    builder.add_node("wait_implement",        node_wait_impl_marker)
    builder.add_node("spawn_self_review",     node_spawn_self_review)
    builder.add_node("wait_self_review",      node_wait_self_review_marker)
    builder.add_node("move_to_impl_review",   node_move_to_impl_review)
    builder.add_node("wait_impl_approval",    node_wait_impl_approval)
    builder.add_node("spawn_ship",            node_spawn_ship)
    builder.add_node("wait_ship",             node_wait_ship_marker)
    builder.add_node("move_to_in_pr",         node_move_to_in_pr)
    builder.add_node("monitor_pr",            node_monitor_pr)
    builder.add_node("spawn_fix_ci",          node_spawn_fix_ci)
    builder.add_node("wait_fix_ci",           node_wait_fix_ci_marker)
    builder.add_node("spawn_respond",         node_spawn_respond_to_review)
    builder.add_node("wait_respond",          node_wait_respond_marker)
    builder.add_node("spawn_followups",       node_spawn_followups)
    builder.add_node("wait_followups",        node_wait_followups_marker)
    builder.add_node("done",                  node_done)
    builder.add_node("needs_human",           node_needs_human)
    builder.add_node("escalate_error",        node_escalate_error)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.set_entry_point("route_entry")

    builder.add_conditional_edges("route_entry", route_entry, {
        "normal":    "spawn_plan",
        "spike":     "spawn_implement",
        "implement": "spawn_implement",
    })

    # ── Plan path (non-spike) ─────────────────────────────────────────────────
    builder.add_edge("spawn_plan", "wait_plan")
    builder.add_conditional_edges("wait_plan", route_plan_marker, {
        "done":  "move_to_plan_review",
        "error": "escalate_error",
    })
    builder.add_edge("move_to_plan_review", "wait_plan_approval")
    builder.add_edge("wait_plan_approval",  "spawn_implement")

    # ── Implement (shared: spike goes directly here) ──────────────────────────
    builder.add_edge("spawn_implement", "wait_implement")
    builder.add_conditional_edges("wait_implement", route_impl_marker, {
        "self_review": "spawn_self_review",   # non-spike success
        "spike_done":  "move_to_impl_review", # spike success (skip self-review)
        "error":       "escalate_error",
    })

    # ── Self-review (non-spike only) ──────────────────────────────────────────
    builder.add_edge("spawn_self_review", "wait_self_review")
    builder.add_conditional_edges("wait_self_review", route_self_review, {
        "proceed": "move_to_impl_review",
        "retry":   "spawn_implement",
    })

    # ── Impl review gate (shared) ─────────────────────────────────────────────
    builder.add_edge("move_to_impl_review",  "wait_impl_approval")
    builder.add_conditional_edges("wait_impl_approval", route_impl_approval, {
        "ship":       "spawn_ship",
        "spike_done": "done",
        "followups":  "spawn_followups",
    })

    # ── Ship path (non-spike) ─────────────────────────────────────────────────
    builder.add_edge("spawn_ship",     "wait_ship")
    builder.add_conditional_edges("wait_ship", route_ship_marker, {
        "done":  "move_to_in_pr",
        "error": "escalate_error",
        "retry": "spawn_ship",
    })
    builder.add_edge("move_to_in_pr",  "monitor_pr")
    builder.add_conditional_edges("monitor_pr", route_monitor_pr, {
        "done":        "done",
        "fix_ci":      "spawn_fix_ci",
        "respond":     "spawn_respond",
        "needs_human": "needs_human",
    })

    # ── CI fix loop ───────────────────────────────────────────────────────────
    builder.add_edge("spawn_fix_ci", "wait_fix_ci")
    builder.add_conditional_edges("wait_fix_ci", route_fix_ci, {
        "monitor_pr":  "monitor_pr",
        "needs_human": "needs_human",
    })

    # ── Review comment loop ───────────────────────────────────────────────────
    builder.add_edge("spawn_respond", "wait_respond")
    builder.add_conditional_edges("wait_respond", route_respond, {
        "monitor_pr":  "monitor_pr",
        "needs_human": "needs_human",
    })

    # ── Spike follow-up path ──────────────────────────────────────────────────
    builder.add_edge("spawn_followups",   "wait_followups")
    builder.add_edge("wait_followups",    "done")

    # ── Terminal edges ────────────────────────────────────────────────────────
    builder.add_edge("done",            END)
    builder.add_edge("needs_human",     END)
    builder.add_edge("escalate_error",  END)

    return builder.compile(checkpointer=checkpointer)


def visualize_workflow() -> str:
    """Return an ASCII/Mermaid representation of the workflow graph."""
    from langgraph.checkpoint.memory import MemorySaver
    graph = build_workflow(MemorySaver())
    try:
        return graph.get_graph().draw_mermaid()
    except Exception:
        return str(graph.get_graph())
