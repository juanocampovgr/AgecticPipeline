"""LangGraph TicketState TypedDict — source of truth for all graph threads."""

from operator import add
from typing import Annotated, Literal, TypedDict


def merge_dict(old: dict | None, new: dict | None) -> dict:
    """Shallow patch-merge for nested overwrite groups."""
    return {**(old or {}), **(new or {})}


# ── Overwrite-only groups ─────────────────────────────────────────────────────

class Identity(TypedDict, total=False):
    ticket_number: int
    item_id: str
    issue_node_id: str
    repo: str
    repo_full: str
    repo_local_path: str
    is_spike: bool
    labels: list[str]
    jira_ticket_id: str
    entry_point: Literal["plan", "implement"]
    # Recovery: set by the poller when restarting an errored ticket so
    # `node_recover` can route to the failed stage instead of starting fresh.
    is_recovery: bool
    recovery_target: str


class RequestMemory(TypedDict, total=False):
    initial_request: str
    initial_request_url: str
    approved_plan: str
    approved_plan_comment_url: str
    plan_revision: int


class QualityResult(TypedDict, total=False):
    check: Literal["detekt", "lint", "unit_tests"]
    passed: bool
    fixes_applied: bool
    error: str


class Implementation(TypedDict, total=False):
    branch: str
    worktree_path: str
    files_changed: list[str]
    impl_summary: str
    self_review_passed: bool
    self_review_retry_count: int


class Quality(TypedDict, total=False):
    checks: list[QualityResult]
    affected_modules: list[str]
    quality_mode: Literal["local", "runner"]


class Ship(TypedDict, total=False):
    pr_number: int
    pr_url: str
    ci_status: Literal["pass", "fail", "pending", "done", "unknown"]
    ci_fix_count: int


class Review(TypedDict, total=False):
    review_comment_round: int
    open_thread_ids: list[str]


class Spike(TypedDict, total=False):
    research_doc_url: str


class RunRecord(TypedDict, total=False):
    stage: str
    run_id: str
    started_at: float
    finished_at: float
    outcome: Literal["done", "error", "needs_human", "timeout", "crash"]
    result_path: str


class Control(TypedDict, total=False):
    current_stage: str
    last_run: RunRecord


# ── Top-level TicketState ─────────────────────────────────────────────────────

class TicketState(TypedDict, total=False):
    # Nested overwrite groups (merge_dict reducer)
    identity: Annotated[Identity, merge_dict]
    request:  Annotated[RequestMemory, merge_dict]
    impl:     Annotated[Implementation, merge_dict]
    quality:  Annotated[Quality, merge_dict]
    ship:     Annotated[Ship, merge_dict]
    review:   Annotated[Review, merge_dict]
    spike:    Annotated[Spike, merge_dict]
    control:  Annotated[Control, merge_dict]

    # Append-only lists at TOP LEVEL — reducers fire correctly here
    commit_shas:          Annotated[list[str], add]
    quality_history:      Annotated[list[dict], add]
    run_history:          Annotated[list[RunRecord], add]
    errors:               Annotated[list[str], add]
    responded_thread_ids: Annotated[list[str], add]
    followup_tickets:     Annotated[list[int], add]
