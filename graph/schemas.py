"""Pydantic boundary models for skill result-file validation."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field


class QualityCheckResult(BaseModel):
    check: str = ""
    passed: bool = True
    fixes_applied: bool = False
    error: str = ""


class RunRecord(BaseModel):
    stage: str
    run_id: str = ""
    started_at: float = Field(default_factory=time.time)
    finished_at: float = Field(default_factory=time.time)
    outcome: Literal["done", "error", "needs_human", "timeout", "crash"] = "done"
    result_path: str = ""


class StageResult(BaseModel):
    """Validated output from a skill result file.

    Every skill writes one of these as JSON to $PIPELINE_RESULT_PATH.
    Fields absent from a particular skill are left at defaults.
    """

    outcome: Literal["done", "error", "needs_human", "timeout", "crash", "retry"] = "done"
    error: str = ""
    record: RunRecord = Field(default_factory=lambda: RunRecord(stage="unknown"))

    # Plan results
    plan_content: str = ""
    plan_comment_url: str = ""
    plan_revision: int = 0

    # Implementation results
    branch: str = ""
    files_changed: list[str] = Field(default_factory=list)
    impl_summary: str = ""
    commit_shas: list[str] = Field(default_factory=list)

    # Quality results
    checks: list[QualityCheckResult] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    quality_mode: str = ""  # "local" or "runner"

    # Self-review results
    self_review_passed: bool = False

    # Ship results
    pr_number: int = 0
    pr_url: str = ""

    # Spike results
    research_doc_url: str = ""

    # Followup results
    followup_ticket_numbers: list[int] = Field(default_factory=list)

    # Review results
    responded_thread_ids: list[str] = Field(default_factory=list)
