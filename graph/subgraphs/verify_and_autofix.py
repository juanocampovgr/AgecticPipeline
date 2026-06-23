"""verify_and_autofix — reusable Functional API subgraph.

Used by node_quality, node_ship, and node_fix_ci to implement:
  run_checks → pass → success
  run_checks → fail, rounds < max → re-run → loop
  run_checks → fail, rounds = max → needs_human
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Literal

try:
    from langgraph.func import entrypoint, task
except ImportError:
    # Fallback if Functional API not available
    entrypoint = None
    task = None


@dataclass
class AutofixInput:
    """Input to the verify_and_autofix subgraph."""
    run_check: Callable  # async () -> dict with outcome key
    max_rounds: int = 3
    stage: str = "check"


@dataclass
class AutofixResult:
    outcome: Literal["success", "needs_human"]
    rounds: int
    last_result: dict


async def run_verify_and_autofix(
    run_check_fn,
    max_rounds: int = 3,
    stage: str = "check",
) -> AutofixResult:
    """
    Run a check function, retrying up to max_rounds if it fails.

    run_check_fn: async callable that returns a dict with 'outcome' key.
                  outcome == "done" means success; anything else means failure.

    Returns AutofixResult with outcome "success" or "needs_human".
    """
    rounds = 0
    last_result: dict = {}

    while rounds <= max_rounds:
        last_result = await run_check_fn()
        outcome = last_result.get("outcome", "error")

        if outcome == "done":
            return AutofixResult(outcome="success", rounds=rounds, last_result=last_result)

        rounds += 1
        if rounds > max_rounds:
            return AutofixResult(outcome="needs_human", rounds=rounds, last_result=last_result)

        # Brief pause before retry
        await asyncio.sleep(2)

    return AutofixResult(outcome="needs_human", rounds=rounds, last_result=last_result)
