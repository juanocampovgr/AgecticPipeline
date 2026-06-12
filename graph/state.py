"""LangGraph TicketState TypedDict — source of truth for all graph threads."""

from typing import TypedDict


class TicketState(TypedDict, total=False):
    # ── Identity (always set at thread start) ─────────────────────────────────
    ticket_number: int
    item_id: str            # ProjectV2Item node ID
    issue_node_id: str      # Issue node ID (for label mutations)
    repo: str               # short name, e.g. "grindr-android"
    repo_full: str          # "owner/repo"
    is_spike: bool
    labels: list        # GitHub issue labels at thread-start time
    jira_ticket_id: str     # Jira ID parsed from title/body, e.g. "ANDROID-1234"; empty if none
    entry_point: str        # "plan" (default) | "implement" — alternate graph entry for recovery

    # ── Plan stage ────────────────────────────────────────────────────────────
    plan_content: str

    # ── Implementation stage ──────────────────────────────────────────────────
    worktree_path: str
    repo_local_path: str

    # ── PR stage ──────────────────────────────────────────────────────────────
    pr_url: str
    pr_number: int

    # ── CI / review tracking ──────────────────────────────────────────────────
    ci_status: str          # "pass" | "fail" | "pending" | "unknown"
    ci_fix_count: int       # how many times fix_ci has run
    review_comment_round: int  # how many respond_to_review rounds completed

    # ── Self-review ───────────────────────────────────────────────────────────
    self_review_passed: bool
    self_review_retry_count: int  # how many self-review → re-implement loops

    # ── Spike follow-ups ──────────────────────────────────────────────────────
    followup_tickets: list  # list of created issue numbers

    # ── Error accumulation ────────────────────────────────────────────────────
    errors: list

    # ── Spawn tracking (for staleness detection / marker polling) ─────────────
    last_fired_at: float
    last_run_id: str

    # ── Routing values set by wait nodes ──────────────────────────────────────
    # These are written by interrupt-wait nodes and consumed by conditional edges.
    pr_outcome: str              # "done" | "fix_ci" | "respond" | "needs_human"
    impl_approval_type: str      # "impl-approved" | "followup-approved"

    _plan_marker_outcome: str    # "done" | "error"
    _impl_marker_outcome: str    # "done" | "error"
    _self_review_outcome: str    # "done" | "error"
    _ship_marker_outcome: str    # "done" | "error"
    _fix_ci_outcome: str         # "done" | "needs_human"
    _respond_outcome: str        # "done" | "needs_human"
    _followups_outcome: str      # "done" | "error"
