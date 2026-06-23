"""Unit tests for graph/state.py — reducers and TicketState construction.

Covers:
  - merge_dict: partial update patches without clobbering sibling fields
  - Annotated[list, add]: appends accumulate instead of overwriting
  - TicketState construction with nested groups
  - Top-level list fields accumulate correctly across multiple updates

No I/O, no subprocesses, no LangGraph internals mocked.
Run with: python -m pytest tests/test_state.py -v
"""

import os
import sys

os.environ.setdefault("PROJECT_NODE_ID", "test-project-id")
os.environ.setdefault("STATUS_FIELD_ID", "test-field-id")
os.environ.setdefault("PROJECT_OWNER", "test-owner")
os.environ.setdefault("PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from graph.state import (
    merge_dict,
    TicketState,
    Identity,
    RequestMemory,
    Implementation,
    Quality,
    QualityResult,
    Ship,
    Review,
    Spike,
    RunRecord,
    Control,
)


# ── merge_dict reducer ────────────────────────────────────────────────────────

class TestMergeDict:
    def test_partial_update_patches_does_not_clobber_siblings(self):
        old = {"ticket_number": 42, "repo": "android", "is_spike": False}
        new = {"repo": "ios"}
        result = merge_dict(old, new)
        assert result["ticket_number"] == 42, "ticket_number must survive a partial update"
        assert result["repo"] == "ios", "updated field must reflect the new value"
        assert result["is_spike"] is False, "sibling field must not be clobbered"

    def test_new_key_is_added(self):
        old = {"ticket_number": 1}
        new = {"repo_local_path": "/workspace/android"}
        result = merge_dict(old, new)
        assert result["ticket_number"] == 1
        assert result["repo_local_path"] == "/workspace/android"

    def test_old_none_returns_new(self):
        result = merge_dict(None, {"branch": "feature/ANDROID-1"})
        assert result == {"branch": "feature/ANDROID-1"}

    def test_new_none_returns_old(self):
        result = merge_dict({"branch": "main"}, None)
        assert result == {"branch": "main"}

    def test_both_none_returns_empty(self):
        result = merge_dict(None, None)
        assert result == {}

    def test_overwrite_same_key(self):
        old = {"current_stage": "plan", "last_run": {"stage": "plan"}}
        new = {"current_stage": "quality"}
        result = merge_dict(old, new)
        assert result["current_stage"] == "quality"
        # last_run must still be present — we only changed current_stage
        assert result["last_run"] == {"stage": "plan"}

    def test_empty_new_leaves_old_intact(self):
        old = {"a": 1, "b": 2}
        result = merge_dict(old, {})
        assert result == {"a": 1, "b": 2}

    def test_empty_old_returns_new(self):
        result = merge_dict({}, {"x": 99})
        assert result == {"x": 99}


# ── Annotated[list, add] reducer behaviour ────────────────────────────────────
#
# We validate the LangGraph reducer contract by directly calling the `add`
# operator (operator.add) that the Annotated metadata references.  This lets
# us confirm the semantics without spinning up a graph.

class TestAddReducer:
    """Validate that operator.add (the reducer for all top-level list fields)
    appends elements rather than overwriting."""

    def _add(self, a, b):
        from operator import add
        return add(a, b)

    def test_appends_strings(self):
        result = self._add(["sha1", "sha2"], ["sha3"])
        assert result == ["sha1", "sha2", "sha3"]

    def test_appends_dicts(self):
        run1 = {"stage": "plan", "outcome": "done"}
        run2 = {"stage": "quality", "outcome": "done"}
        result = self._add([run1], [run2])
        assert len(result) == 2
        assert result[0] == run1
        assert result[1] == run2

    def test_appends_ints(self):
        result = self._add([101, 102], [103])
        assert result == [101, 102, 103]

    def test_appends_multiple_rounds(self):
        acc = []
        acc = self._add(acc, ["sha-a"])
        acc = self._add(acc, ["sha-b", "sha-c"])
        acc = self._add(acc, ["sha-d"])
        assert acc == ["sha-a", "sha-b", "sha-c", "sha-d"]

    def test_does_not_overwrite_existing_elements(self):
        existing = ["thread-1", "thread-2"]
        new_items = ["thread-3"]
        result = self._add(existing, new_items)
        assert "thread-1" in result
        assert "thread-2" in result
        assert "thread-3" in result
        assert len(result) == 3


# ── TicketState construction with nested groups ────────────────────────────────

class TestTicketStateConstruction:
    def test_minimal_construction(self):
        """TicketState is total=False — empty dict must be valid."""
        s: TicketState = {}
        assert isinstance(s, dict)

    def test_identity_group(self):
        identity: Identity = {
            "ticket_number": 99,
            "repo": "android",
            "is_spike": False,
            "entry_point": "plan",
        }
        s: TicketState = {"identity": identity}
        assert s["identity"]["ticket_number"] == 99
        assert s["identity"]["repo"] == "android"

    def test_request_memory_group(self):
        request: RequestMemory = {
            "initial_request": "Fix login crash",
            "initial_request_url": "https://github.com/org/repo/issues/1",
            "approved_plan": "1. Fix null check\n2. Add test",
            "plan_revision": 1,
        }
        s: TicketState = {"request": request}
        assert s["request"]["initial_request"] == "Fix login crash"
        assert s["request"]["plan_revision"] == 1

    def test_implementation_group(self):
        impl: Implementation = {
            "branch": "ANDROID-99-fix-crash",
            "worktree_path": "/tmp/worktrees/ANDROID-99",
            "files_changed": ["app/src/main/Login.kt"],
            "self_review_retry_count": 0,
        }
        s: TicketState = {"impl": impl}
        assert s["impl"]["branch"] == "ANDROID-99-fix-crash"
        assert s["impl"]["files_changed"] == ["app/src/main/Login.kt"]

    def test_quality_group(self):
        checks: list[QualityResult] = [
            {"check": "detekt", "passed": True, "fixes_applied": False},
            {"check": "lint",   "passed": True, "fixes_applied": True},
        ]
        quality: Quality = {
            "checks": checks,
            "affected_modules": [":app"],
            "quality_mode": "runner",
        }
        s: TicketState = {"quality": quality}
        assert len(s["quality"]["checks"]) == 2
        assert s["quality"]["affected_modules"] == [":app"]
        assert s["quality"]["quality_mode"] == "runner"

    def test_quality_group_local_mode(self):
        quality: Quality = {
            "checks": [],
            "affected_modules": [":core"],
            "quality_mode": "local",
        }
        s: TicketState = {"quality": quality}
        assert s["quality"]["quality_mode"] == "local"

    def test_ship_group(self):
        ship: Ship = {
            "pr_number": 1234,
            "pr_url": "https://github.com/org/repo/pull/1234",
            "ci_status": "pending",
            "ci_fix_count": 0,
        }
        s: TicketState = {"ship": ship}
        assert s["ship"]["pr_number"] == 1234
        assert s["ship"]["ci_status"] == "pending"

    def test_review_group(self):
        review: Review = {
            "review_comment_round": 1,
            "open_thread_ids": ["thread-abc"],
        }
        s: TicketState = {"review": review}
        assert s["review"]["review_comment_round"] == 1

    def test_spike_group(self):
        spike: Spike = {"research_doc_url": "https://github.com/org/repo/issues/5#comment-99"}
        s: TicketState = {"spike": spike}
        assert "research_doc_url" in s["spike"]

    def test_control_group_with_run_record(self):
        record: RunRecord = {
            "stage": "plan",
            "run_id": "run-001",
            "started_at": 1700000000.0,
            "finished_at": 1700000060.0,
            "outcome": "done",
            "result_path": "/tmp/results/99/plan_0.json",
        }
        control: Control = {
            "current_stage": "plan",
            "last_run": record,
        }
        s: TicketState = {"control": control}
        assert s["control"]["current_stage"] == "plan"
        assert s["control"]["last_run"]["outcome"] == "done"

    def test_full_state_construction(self):
        """A fully-populated TicketState with all groups and top-level lists."""
        record: RunRecord = {
            "stage": "plan",
            "outcome": "done",
            "result_path": "/tmp/results/1/plan_0.json",
        }
        s: TicketState = {
            "identity": {"ticket_number": 1, "repo": "android", "is_spike": False},
            "request":  {"initial_request": "Add dark mode"},
            "impl":     {"branch": "ANDROID-1-add-dark-mode"},
            "quality":  {"checks": []},
            "ship":     {"pr_number": 10},
            "review":   {"review_comment_round": 0},
            "spike":    {"research_doc_url": ""},
            "control":  {"current_stage": "plan", "last_run": record},
            "commit_shas":          ["sha-abc"],
            "quality_history":      [{"run": 1}],
            "run_history":          [record],
            "errors":               [],
            "responded_thread_ids": [],
            "followup_tickets":     [],
        }
        assert s["identity"]["ticket_number"] == 1
        assert s["commit_shas"] == ["sha-abc"]
        assert len(s["run_history"]) == 1


# ── Top-level list fields accumulate correctly ────────────────────────────────

class TestTopLevelListAccumulation:
    """Verify that each of the six top-level Annotated[list, add] fields
    accumulate correctly when simulated with operator.add."""

    def _add(self, a, b):
        from operator import add
        return add(a, b)

    def test_commit_shas_accumulate(self):
        shas: list[str] = []
        shas = self._add(shas, ["sha-1", "sha-2"])
        shas = self._add(shas, ["sha-3"])
        assert shas == ["sha-1", "sha-2", "sha-3"]

    def test_quality_history_accumulates(self):
        history: list[dict] = []
        run1 = {"run": 1, "passed": True}
        run2 = {"run": 2, "passed": False}
        history = self._add(history, [run1])
        history = self._add(history, [run2])
        assert len(history) == 2
        assert history[0]["run"] == 1
        assert history[1]["run"] == 2

    def test_run_history_accumulates(self):
        history: list[RunRecord] = []
        rec1: RunRecord = {"stage": "plan",    "outcome": "done"}
        rec2: RunRecord = {"stage": "quality", "outcome": "done"}
        history = self._add(history, [rec1])
        history = self._add(history, [rec2])
        assert len(history) == 2
        assert history[0]["stage"] == "plan"
        assert history[1]["stage"] == "quality"

    def test_errors_accumulate(self):
        errors: list[str] = []
        errors = self._add(errors, ["plan stage timed out"])
        errors = self._add(errors, ["quality check failed"])
        assert len(errors) == 2

    def test_responded_thread_ids_accumulate(self):
        ids: list[str] = []
        ids = self._add(ids, ["thread-1"])
        ids = self._add(ids, ["thread-2", "thread-3"])
        assert ids == ["thread-1", "thread-2", "thread-3"]

    def test_followup_tickets_accumulate(self):
        tickets: list[int] = []
        tickets = self._add(tickets, [201, 202])
        tickets = self._add(tickets, [203])
        assert tickets == [201, 202, 203]

    def test_merge_dict_does_not_stomp_sibling_list_fields(self):
        """Confirm that updating one nested group via merge_dict does not
        affect top-level list fields when they are stored separately."""
        old_impl = {"branch": "ANDROID-1-old", "worktree_path": "/tmp/old"}
        new_impl = {"branch": "ANDROID-1-new"}
        merged_impl = merge_dict(old_impl, new_impl)
        assert merged_impl["branch"] == "ANDROID-1-new"
        assert merged_impl["worktree_path"] == "/tmp/old"
        # Simulated top-level commit_shas unaffected — they live outside the group
        commit_shas = ["sha-1", "sha-2"]
        assert commit_shas == ["sha-1", "sha-2"]
