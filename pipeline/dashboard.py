"""Per-ticket dashboard TUI.

Run as `python -m pipeline.dashboard --ticket {N}`. Reads the ticket's event
log (`~/.pipeline/events/{ticket}.jsonl`) and renders a live checklist of
pipeline stages, the currently active stage with elapsed timer and a tail of
its Claude log, plus a poller heartbeat in the footer.

The dashboard is read-only — it never writes back to the pipeline. Exit on
`ticket_done` or Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from graph.events import events_path

# ── Stage ordering (display only) ─────────────────────────────────────────────

NORMAL_STAGES: list[tuple[str, str]] = [
    ("AI Planning", "Planning"),
    ("Plan Approval", "Plan approval gate"),
    ("AI Implementation", "Implementation"),
    ("AI Quality Check", "Quality check"),
    ("Self Review", "Self-review"),
    ("Impl Approval", "Implementation approval gate"),
    ("Ready To Ship - AI", "Ship (open PR)"),
    ("Monitor PR", "Monitor PR"),
    ("Fix CI", "Fix CI"),
    ("Respond To Review", "Respond to review"),
]

SPIKE_STAGES: list[tuple[str, str]] = [
    ("AI Implementation", "Research"),
    ("Impl Approval", "Approval gate"),
    ("Spike Followups", "Follow-up tickets"),
]

GATE_STAGES = {"Plan Approval", "Impl Approval"}


# ── State machine ─────────────────────────────────────────────────────────────


@dataclass
class StageState:
    status: str = "pending"  # pending | running | done | failed | waiting | skipped | retry
    started_at: float | None = None
    finished_at: float | None = None
    attempt: int = 0
    detail: str = ""
    log_path: str | None = None
    error: str | None = None


@dataclass
class Snapshot:
    ticket: int = 0
    title: str = ""
    repo: str = ""
    jira_id: str = ""
    branch: str = ""
    pr_url: str = ""
    status: str = ""
    is_spike: bool = False
    stages: dict[str, StageState] = field(default_factory=dict)
    current_stage: str | None = None
    last_event_ts: float = 0.0
    last_heartbeat: float = 0.0
    terminal: bool = False
    terminal_outcome: str = ""


# ── Event → state reducer ─────────────────────────────────────────────────────


def _ensure_stage(snap: Snapshot, stage: str) -> StageState:
    st = snap.stages.get(stage)
    if st is None:
        st = StageState()
        snap.stages[stage] = st
    return st


def apply_event(snap: Snapshot, ev: dict) -> None:
    snap.last_event_ts = float(ev.get("ts") or 0)
    kind = ev.get("kind", "")
    stage = ev.get("stage") or ""
    payload = ev.get("payload") or {}

    if kind == "ticket_grabbed":
        snap.ticket = int(payload.get("ticket") or snap.ticket)
        snap.title = payload.get("title", snap.title)
        snap.repo = payload.get("repo", snap.repo)
        snap.jira_id = payload.get("jira_id", snap.jira_id)
        snap.is_spike = bool(payload.get("is_spike", snap.is_spike))
        snap.status = payload.get("status", snap.status)
        return

    if kind == "heartbeat":
        snap.last_heartbeat = snap.last_event_ts
        return

    if kind == "status_changed":
        snap.status = payload.get("status", snap.status)
        return

    if kind == "stage_started":
        st = _ensure_stage(snap, stage)
        st.status = "running"
        st.started_at = snap.last_event_ts
        st.finished_at = None
        st.error = None
        st.log_path = payload.get("log_path") or st.log_path
        st.attempt = int(payload.get("attempt", st.attempt))
        snap.current_stage = stage
        return

    if kind == "stage_progress":
        st = _ensure_stage(snap, stage)
        if st.status == "pending":
            st.status = "running"
            st.started_at = snap.last_event_ts
        if "log_path" in payload:
            st.log_path = payload["log_path"]
        return

    if kind == "stage_completed":
        st = _ensure_stage(snap, stage)
        st.status = "done"
        st.finished_at = snap.last_event_ts
        st.detail = payload.get("detail", "")
        outcome = payload.get("outcome")
        if outcome and outcome != "done":
            st.detail = f"{outcome}: {st.detail}".strip(": ")
        if snap.current_stage == stage:
            snap.current_stage = None
        # Useful state side-effects for the header
        if "branch" in payload:
            snap.branch = payload["branch"]
        if "pr_url" in payload:
            snap.pr_url = payload["pr_url"]
        return

    if kind == "stage_failed":
        st = _ensure_stage(snap, stage)
        st.status = "failed"
        st.finished_at = snap.last_event_ts
        st.error = payload.get("error", "")
        st.detail = payload.get("outcome", "")
        if snap.current_stage == stage:
            snap.current_stage = None
        return

    if kind == "stage_retry":
        st = _ensure_stage(snap, stage)
        st.status = "retry"
        st.attempt = int(payload.get("attempt", st.attempt + 1))
        st.detail = payload.get("reason", "")
        return

    if kind == "gate_waiting":
        st = _ensure_stage(snap, stage)
        st.status = "waiting"
        st.started_at = st.started_at or snap.last_event_ts
        st.detail = payload.get("label", "awaiting label")
        snap.current_stage = stage
        return

    if kind == "gate_resumed":
        st = _ensure_stage(snap, stage)
        st.status = "done"
        st.finished_at = snap.last_event_ts
        st.detail = payload.get("label", "approved")
        if snap.current_stage == stage:
            snap.current_stage = None
        return

    if kind == "ticket_done":
        snap.terminal = True
        snap.terminal_outcome = payload.get("outcome", "done")
        snap.current_stage = None
        return


# ── Renderers ─────────────────────────────────────────────────────────────────

_SPINNER = Spinner("dots", style="cyan")


def _fmt_elapsed(start: float | None, end: float | None) -> str:
    if start is None:
        return ""
    finish = end if end is not None else time.time()
    secs = max(0, int(finish - start))
    m, s = divmod(secs, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _status_cell(st: StageState) -> Text:
    if st.status == "running":
        # rich.spinner.Spinner renders the current frame when assembled into Text
        return Text.from_markup("[cyan]●[/] running")
    if st.status == "waiting":
        return Text.from_markup("[yellow]⏸[/] waiting")
    if st.status == "done":
        return Text.from_markup("[green]✓[/] done")
    if st.status == "failed":
        return Text.from_markup("[red]✗[/] failed")
    if st.status == "retry":
        return Text.from_markup(f"[yellow]↻[/] retry #{st.attempt + 1}")
    if st.status == "skipped":
        return Text.from_markup("[dim]–[/] skipped")
    return Text.from_markup("[dim]·[/] pending")


def render_header(snap: Snapshot) -> Panel:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()

    title_line = Text()
    title_line.append(f"#{snap.ticket}  ", style="bold bright_white")
    title_line.append(snap.title or "(loading title…)", style="bright_white")

    table.add_row("Ticket", title_line)
    if snap.repo:
        table.add_row("Repo", Text(snap.repo))
    if snap.jira_id:
        table.add_row("Jira", Text(snap.jira_id))
    if snap.branch:
        table.add_row("Branch", Text(snap.branch))
    if snap.pr_url:
        table.add_row("PR", Text(snap.pr_url, style="blue underline"))
    if snap.status:
        table.add_row("Status", Text(snap.status, style="magenta"))

    flavor = "spike" if snap.is_spike else "normal"
    subtitle = f"flow: {flavor}"
    return Panel(table, title="Pipeline Dashboard", subtitle=subtitle, border_style="cyan")


def render_checklist(snap: Snapshot) -> Panel:
    stages = SPIKE_STAGES if snap.is_spike else NORMAL_STAGES

    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(width=2)
    table.add_column(min_width=14, max_width=18)
    table.add_column(ratio=1)
    table.add_column(min_width=8, justify="right")

    seen_current = False
    for idx, (key, label) in enumerate(stages, start=1):
        st = snap.stages.get(key, StageState())

        # Conditional stages (Fix CI, Respond) and the post-current stages
        # remain "pending" until we see a real event for them. The header arrow
        # marks the next stage.
        marker = " "
        if snap.current_stage == key and not seen_current:
            marker = "▸"
            seen_current = True

        if st.status == "running":
            indicator: object = _SPINNER
        else:
            indicator = Text(_indicator_char(st.status), style=_indicator_style(st.status))

        detail = Text()
        detail.append(label, style="white")
        if st.detail and st.status not in ("pending",):
            detail.append(f"  — {st.detail[:80]}", style="dim")
        if st.error and st.status == "failed":
            detail.append(f"\n  {st.error[:120]}", style="red")

        elapsed = _fmt_elapsed(st.started_at, st.finished_at)
        table.add_row(
            Text(marker, style="bold cyan"),
            indicator if not isinstance(indicator, Text) else indicator,
            detail,
            Text(elapsed, style="dim"),
        )

    return Panel(table, title="Stages", border_style="white")


def _indicator_char(status: str) -> str:
    return {
        "done": "✓",
        "failed": "✗",
        "waiting": "⏸",
        "retry": "↻",
        "skipped": "–",
        "pending": "·",
    }.get(status, "·")


def _indicator_style(status: str) -> str:
    return {
        "done": "green",
        "failed": "red",
        "waiting": "yellow",
        "retry": "yellow",
        "skipped": "dim",
        "pending": "dim",
    }.get(status, "white")


def _tail_log(log_path: str | None, lines: int = 8) -> list[str]:
    if not log_path:
        return []
    try:
        p = Path(log_path)
        if not p.exists():
            return []
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 8192)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace")
        out = [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
        return out[-lines:]
    except Exception:
        return []


def render_current(snap: Snapshot) -> Panel:
    stage = snap.current_stage
    if stage is None:
        if snap.terminal:
            body = Align.center(
                Text(
                    f"workflow complete — {snap.terminal_outcome}",
                    style="bold green" if snap.terminal_outcome == "done" else "bold red",
                ),
                vertical="middle",
            )
            return Panel(body, title="Result", border_style="green" if snap.terminal_outcome == "done" else "red")
        return Panel(
            Align.center(Text("idle — awaiting next stage", style="dim"), vertical="middle"),
            title="Current",
            border_style="dim",
        )

    st = snap.stages.get(stage, StageState())
    header = Table.grid(padding=(0, 1))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("Stage", Text(stage, style="cyan"))
    header.add_row("Status", _status_cell(st))
    if st.started_at:
        header.add_row("Elapsed", Text(_fmt_elapsed(st.started_at, st.finished_at)))
    if st.log_path:
        header.add_row("Log", Text(st.log_path, style="dim"))

    log_lines = _tail_log(st.log_path)
    if log_lines:
        log_block = Text("\n".join(log_lines), style="white")
    elif stage in GATE_STAGES:
        log_block = Text("waiting for human approval label on the issue…", style="yellow")
    else:
        log_block = Text("(waiting for output…)", style="dim")

    return Panel(
        Group(header, Text(""), log_block),
        title="Current stage",
        border_style="cyan",
    )


def render_footer(snap: Snapshot) -> Panel:
    now = time.time()
    hb_age = (now - snap.last_heartbeat) if snap.last_heartbeat else None
    if hb_age is None:
        hb = Text("poller: ?", style="dim")
    elif hb_age < 180:
        hb = Text(f"poller: alive ({int(hb_age)}s ago)", style="green")
    elif hb_age < 600:
        hb = Text(f"poller: stale ({int(hb_age)}s ago)", style="yellow")
    else:
        hb = Text(f"poller: missing ({int(hb_age)}s ago)", style="red")

    last_ev = (
        Text(f"last event: {int(now - snap.last_event_ts)}s ago")
        if snap.last_event_ts else Text("last event: —", style="dim")
    )
    hint = Text("Ctrl-C to close (pipeline keeps running)", style="dim")

    bar = Table.grid(padding=(0, 2), expand=True)
    bar.add_column(ratio=1)
    bar.add_column(ratio=1)
    bar.add_column(ratio=1, justify="right")
    bar.add_row(hb, last_ev, hint)
    return Panel(bar, border_style="dim")


def render(snap: Snapshot) -> Group:
    return Group(
        render_header(snap),
        render_checklist(snap),
        render_current(snap),
        render_footer(snap),
    )


# ── Event tailer ──────────────────────────────────────────────────────────────


class JsonlTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._buf = b""

    def read_new(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # log truncated/rotated; restart
            self._offset = 0
            self._buf = b""
        if size == self._offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except OSError:
            return []
        self._offset = size
        data = self._buf + chunk
        *lines, self._buf = data.split(b"\n")
        out: list[dict] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except Exception:
                continue
        return out


# ── Main loop ─────────────────────────────────────────────────────────────────


def run(ticket: int, refresh_hz: float = 4.0) -> int:
    console = Console()
    snap = Snapshot(ticket=ticket)
    tail = JsonlTail(events_path(ticket))

    period = 1.0 / max(1.0, refresh_hz)
    grace_after_terminal_s = 5.0
    terminal_at: float | None = None

    with Live(render(snap), refresh_per_second=refresh_hz, console=console, screen=False) as live:
        try:
            while True:
                for ev in tail.read_new():
                    apply_event(snap, ev)
                if snap.terminal and terminal_at is None:
                    terminal_at = time.time()
                live.update(render(snap))
                if terminal_at and (time.time() - terminal_at) >= grace_after_terminal_s:
                    break
                time.sleep(period)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.dashboard")
    parser.add_argument("--ticket", type=int, required=True)
    parser.add_argument("--refresh-hz", type=float, default=4.0)
    args = parser.parse_args(argv)
    return run(args.ticket, refresh_hz=args.refresh_hz)


if __name__ == "__main__":
    sys.exit(main())
