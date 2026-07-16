"""LangGraph StateGraph definition for the new native agentic pipeline."""

from langgraph.graph import StateGraph, END
from graph.state import TicketState
from graph.nodes import (
    node_recover,
    node_route_entry,
    node_plan, gate_plan_approval,
    node_implement,
    node_quality, node_self_review,
    gate_impl_approval,
    node_ship,
    node_monitor_pr,
    node_fix_ci, node_respond,
    node_done, node_needs_human, node_escalate_error,
)


def build_workflow(checkpointer, store=None):
    builder = StateGraph(TicketState)

    # Register nodes
    builder.add_node("recover",            node_recover)
    builder.add_node("route_entry",        node_route_entry)
    builder.add_node("plan",               node_plan)
    builder.add_node("gate_plan_approval", gate_plan_approval)
    builder.add_node("implement",          node_implement)
    builder.add_node("quality",            node_quality)
    builder.add_node("self_review",        node_self_review)
    builder.add_node("gate_impl_approval", gate_impl_approval)
    builder.add_node("ship",               node_ship)
    builder.add_node("monitor_pr",         node_monitor_pr)
    builder.add_node("fix_ci",             node_fix_ci)
    builder.add_node("respond",            node_respond)
    builder.add_node("done",               node_done)
    builder.add_node("needs_human",        node_needs_human)
    builder.add_node("escalate_error",     node_escalate_error)

    # `recover` is the new entry point — it pass-throughs to `route_entry` for
    # normal runs and routes directly to the failed stage when restarting an
    # errored ticket (see graph/nodes/recover.py).
    builder.set_entry_point("recover")

    # All machine nodes return Command(goto=...) which LangGraph uses for routing.
    # We only declare edges to END for the three terminal nodes.
    builder.add_edge("done",           END)
    builder.add_edge("needs_human",    END)
    builder.add_edge("escalate_error", END)

    kwargs = {"checkpointer": checkpointer}
    if store is not None:
        kwargs["store"] = store
    return builder.compile(**kwargs)


def visualize_workflow() -> str:
    from langgraph.checkpoint.memory import MemorySaver
    graph = build_workflow(MemorySaver())
    try:
        return graph.get_graph().draw_mermaid()
    except Exception:
        return str(graph.get_graph())
