"""Persistent bottom HUD for the chat REPL (SPEC-111 R22).

Fixed blocks, bottom of the screen, drawn on stderr inside an ANSI scroll
region (DECSTBM) so the conversation scrolls above them:

    ─────────────────────────────────────────── separator
    ⠋ 23s ctx ▮▮▮▯▯ 34% of 1000k · last … · session …   engine telemetry
    W-01 running unit-042 implement 3m12s ~8.4k tok      agents panel (0–4 rows)
    repo·branch │ engine │ mode: auto                     status row
    harness> <input buffer>                               input row

Fail-open by design: any exception anywhere disables the HUD permanently and
the REPL continues exactly as the plain tui. Kill switch: HARNESS_CHAT_NO_HUD=1.
Harness worker tokens are chars/4 estimates and always render with `~` (R24).
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .common import gate_holding as _gate_holding, parse_iso_datetime, read_json
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import gate_holding as _gate_holding, parse_iso_datetime, read_json

MAX_AGENT_ROWS = 4
STALE_PLANNED_S = 86400  # pending worker of a planned WF older than this → stale badge
MAX_PLAN_ROWS = 5   # chat-plan-hud: summary + current + next steps, capped
MIN_WIDTH_FOR_AGENTS = 50
MIN_HEIGHT = 10
WORKERS_CACHE_S = 2.0


def _glyphs() -> dict[str, Any]:
    enc = getattr(sys.stderr, "encoding", None) or "ascii"
    try:
        "⠋⠙⠹⠸⠼⠴⠦⠧▮▯│·─…⚠".encode(enc)
        return {"spin": "⠋⠙⠹⠸⠼⠴⠦⠧", "on": "▮", "off": "▯", "bar_sep": "│",
                "sep": " · ", "line": "─", "ell": "…", "dash": "—", "warn": "⚠"}
    except (UnicodeEncodeError, LookupError):
        return {"spin": "|/-\\", "on": "#", "off": ".", "bar_sep": "|",
                "sep": " | ", "line": "-", "ell": "...", "dash": "-", "warn": "[!]"}


def format_plan_rows(steps: list[dict[str, Any]], cols: int,
                     g: dict[str, Any]) -> list[str]:
    """chat-plan-hud rows for the bottom HUD: one summary row + the current
    step + upcoming steps, capped at MAX_PLAN_ROWS. Pure formatter (testable);
    [] when there is no active plan."""
    if not steps:
        return []
    done = sum(1 for s in steps if s.get("status") == "completed")
    marks = {"completed": "v", "in_progress": ">", "pending": "o"}
    rows = [_clip(f" plan {done}/{len(steps)} {g['line'] * 3}", cols - 1, g["ell"])]
    current = [s for s in steps if s.get("status") == "in_progress"]
    upcoming = [s for s in steps if s.get("status") == "pending"]
    for step in (current + upcoming)[: MAX_PLAN_ROWS - 1]:
        mark = marks.get(str(step.get("status")), "o")
        rows.append(_clip(f"  {mark} {step.get('text', '')}", cols - 1, g["ell"]))
    return rows


def _fmt_tokens(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return "n/a"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def _fmt_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def _clip(text: str, width: int, ell: str) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - len(ell), 0)] + ell


_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_BRANCH_TTL_S = 30.0  # ponytail: branch rarely changes mid-session; a TTL cache kills the
# per-poll git subprocess (the panel's biggest WARM /api/state cost) while a checkout still
# shows within 30s. Per-process, bounded by the handful of roots polled.


def _git_branch(root: Path) -> str:
    key = str(root)
    hit = _BRANCH_CACHE.get(key)
    mono = time.monotonic()
    if hit is not None and mono - hit[0] < _BRANCH_TTL_S:
        return hit[1]
    try:
        res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=5)
        branch = res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""
    _BRANCH_CACHE[key] = (mono, branch)
    return branch


def read_workers(root: Path, now_utc: Any = None,
                 include_done: bool = False) -> list[dict[str, Any]]:
    """Active harness workers from workflow state files; [] on any trouble.

    include_done=True also returns finished workers (SPEC-114 v2 panel), each
    carrying a `finishedAt` (run.finishedAt or updatedAt). HUD callers omit it
    and get the active-only rows unchanged.
    """
    active = {"pending", "queued", "running"}
    rows: list[dict[str, Any]] = []
    try:
        for wf_file in sorted((root / ".harness" / "workflows" / "active").glob("WF-*/workflow.json")):
            wf = read_json(wf_file, {}) or {}
            workers = wf.get("workers") or []
            async_started: dict[str, str] = {}
            # async/tasks startedAt feeds ONLY a running worker's `elapsed` (below); skip the
            # per-WF task glob+read entirely when nothing is running — the common case at scale
            # (finalized WFs), whose startedAt is never read. Cold O(n) disk win, shape-identical.
            if any(str(w.get("status") or "") == "running" for w in workers):
                tasks_dir = wf_file.parent / "async" / "tasks"
                if tasks_dir.is_dir():
                    for task_file in tasks_dir.glob("*.json"):
                        task = read_json(task_file, {}) or {}
                        if task.get("workerId") and task.get("startedAt"):
                            async_started[str(task["workerId"])] = str(task["startedAt"])
            for worker in workers:
                status = str(worker.get("status") or "")
                if status not in active and not include_done:
                    continue
                started = (async_started.get(str(worker.get("workerId")))
                           or (worker.get("run") or {}).get("startedAt")
                           or worker.get("updatedAt"))
                elapsed = None
                if status == "running" and started:
                    started_dt = parse_iso_datetime(str(started))
                    if started_dt is not None and now_utc is not None:
                        elapsed = (now_utc - started_dt).total_seconds()
                tokens = ((worker.get("estimatedPromptTokens") or 0)
                          + (worker.get("estimatedResultTokens") or 0))
                # SPEC-114 v7: last activity = stdout run-log mtime (1 stat/worker); a
                # running worker with no recent output shows "travou?" without a click.
                try:
                    mtime = (wf_file.parent / "run-logs" / f"{worker.get('workerId')}.stdout.log").stat().st_mtime
                    last_output = dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat()
                except OSError:
                    last_output = None
                row = {"workerId": str(worker.get("workerId") or "?"),
                       "status": status,
                       "unit": str(worker.get("unitId") or ""),
                       "profile": str(worker.get("taskProfile") or ""),
                       "workflow": wf_file.parent.name,  # SPEC-118: addresses the drill-in
                       "elapsed": elapsed,
                       "lastOutputAt": last_output,  # SPEC-114 v7; None when no log yet
                       "tokens": tokens or None}
                if status not in active:
                    finished = (worker.get("run") or {}).get("finishedAt") or worker.get("updatedAt")
                    if finished:
                        row["finishedAt"] = str(finished)
                # stale-planned badge (owner incident 2026-07-16): a pending worker of a
                # never-started (planned) WF older than a day sits on the board forever —
                # cancel is a no-op for it; the drawer's Discard is the exit. Badge it.
                if (status == "pending" and str(wf.get("status") or "") == "planned"
                        and now_utc is not None):
                    born = parse_iso_datetime(str(worker.get("updatedAt")
                                                  or wf.get("createdAt") or ""))
                    if born is not None and (now_utc - born).total_seconds() > STALE_PLANNED_S:
                        row["stale"] = True
                rows.append(row)
    except Exception:
        return []
    order = {"running": 0, "queued": 1, "pending": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 3), r["workerId"]))
    return rows


def format_worker_rows(rows: list[dict[str, Any]], width: int,
                       glyphs: dict[str, Any] | None = None) -> list[str]:
    """Pure agents-panel rows (R24: tokens always ~estimates)."""
    g = glyphs or _glyphs()
    if not rows or width < MIN_WIDTH_FOR_AGENTS:
        return []
    shown = rows if len(rows) <= MAX_AGENT_ROWS else rows[: MAX_AGENT_ROWS - 1]
    out = []
    for r in shown:
        tok = f"~{_fmt_tokens(r['tokens'])} tok" if r.get("tokens") else ""
        work = _clip(f"{r['unit']} {r['profile']}".strip(), 28, g["ell"])
        line = f"{r['workerId'][:8]:<8} {r['status']:<8} {work:<28} {_fmt_elapsed(r.get('elapsed')):>7}  {tok}"
        out.append(_clip(line.rstrip(), width - 1, g["ell"]))
    if len(rows) > MAX_AGENT_ROWS:
        out.append(_clip(f"{g['ell']}and {len(rows) - (MAX_AGENT_ROWS - 1)} more workers",
                         width - 1, g["ell"]))
    return out


def format_status_row(repo: str, branch: str, engine_label: str, chat_mode: str,
                      width: int, glyphs: dict[str, Any] | None = None,
                      alerts: int = 0) -> str:
    """Status row; drops segments right-to-left but mode (and alerts) always stay."""
    g = glyphs or _glyphs()
    sep = f" {g['bar_sep']} "
    mode_seg = f"mode: {chat_mode}" + (f" {g['warn']}{alerts}" if alerts else "")
    segments = [f"{repo}{'·' if branch else ''}{branch}", engine_label, mode_seg]
    while len(segments) > 1 and len(sep.join(segments)) > width - 1:
        segments.pop(0)
    return _clip(sep.join(segments), width - 1, g["ell"])


def read_pending_critical(root: Path) -> int:
    """High/critical self-review findings pending (SPEC-111 R28); never raises."""
    try:
        state = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
        return sum(1 for rec in (state.get("raised") or {}).values()
                   if isinstance(rec, dict) and rec.get("criticality") in ("high", "critical"))
    except Exception:
        return 0


def read_approved_plan(root: Path) -> dict[str, Any]:
    """The active plan-grant (`.harness/runtime/approved-plan.json`) for the
    takeover card; {} when absent/corrupt. Never raises."""
    try:
        return read_json(root / ".harness" / "runtime" / "approved-plan.json", {}) or {}
    except Exception:
        return {}


def format_takeover_row(plan_grant: dict[str, Any], workers: list[dict[str, Any]],
                        plan_steps: list[dict[str, Any]], alerts: int,
                        gate_holding: bool, cols: int, g: dict[str, Any],
                        expired: bool = False) -> list[str]:
    """chat-takeover-card level-0: one compact line projecting {objective, owner,
    next effect, recovery} so a human taking over reads the situation at a glance.
    Segments drop right-to-left under width pressure (objective survives); the
    whole row is droppable (the HUD sheds it before the never-dropped status row).
    [] when there is nothing to hand over. Pure + fail-open — never raises."""
    grant = plan_grant or {}
    footprint = grant.get("footprint") or []
    plan_path = str(grant.get("planPath") or "")
    segs: list[str] = []
    if footprint or plan_path:
        name = (Path(plan_path).stem or "plan") if plan_path else "plan"
        obj = f"obj {name} ({len(footprint)}f)" if footprint else f"obj {name}"
        segs.append(obj + (" expired" if expired else ""))
    running = next((w for w in (workers or []) if w.get("status") == "running"), None)
    if running:
        segs.append(f"owner {str(running.get('workerId') or '?')}")
    elif footprint or plan_path or (plan_steps or []):
        segs.append("owner operator")
    step = next((s.get("text") for s in (plan_steps or [])
                 if s.get("status") == "in_progress"), None)
    if step:
        segs.append(f"next {step}")
    rec = ([f"{alerts} crit"] if alerts else []) + (["gate holding"] if gate_holding else [])
    if rec:
        segs.append("recovery " + ", ".join(rec))
    if not segs:
        return []
    sep = g["sep"]
    parts = list(segs)
    while len(parts) > 1 and len(f" takeover {sep.join(parts)}") > cols - 1:
        parts.pop()
    return [_clip(f" takeover {sep.join(parts)}", cols - 1, g["ell"])]


def format_telemetry_row(usage: dict[str, Any] | None, session: dict[str, Any],
                         turn_elapsed: float | None, spin_frame: str, width: int,
                         glyphs: dict[str, Any] | None = None) -> str:
    """Telemetry row; drops session, then last, keeping the context bar."""
    g = glyphs or _glyphs()
    u = usage or {}
    prefix = f"{spin_frame} {_fmt_elapsed(turn_elapsed)} " if turn_elapsed is not None else ""
    used, window = u.get("ctx_used"), u.get("ctx_window")
    if isinstance(used, (int, float)) and isinstance(window, (int, float)) and window:
        filled = min(int(used * 10 / window) + (1 if used else 0), 10)
        bar = f"ctx {g['on'] * filled}{g['off'] * (10 - filled)} {int(used * 100 / window)}% of {_fmt_tokens(window)}"
    else:
        bar = "ctx n/a"
    last = (f"last {_fmt_tokens(u.get('in_tokens'))}→{_fmt_tokens(u.get('out_tokens'))}"
            f" · ${u['cost_usd']:.3f}" if isinstance(u.get("cost_usd"), (int, float))
            else f"last {_fmt_tokens(u.get('in_tokens'))}→{_fmt_tokens(u.get('out_tokens'))}")
    if isinstance(u.get("duration_s"), (int, float)):
        last += f" · {u['duration_s']:.1f}s"
    sess_cost = session.get("cost_usd")
    sess = (f"session ${sess_cost:.2f}/{session.get('turns', 0)} turns"
            if isinstance(sess_cost, (int, float)) else f"session {session.get('turns', 0)} turns")
    parts = [bar, last, sess]
    while len(parts) > 1 and len(prefix + g["sep"].join(parts)) > width - 1:
        parts.pop()
    return _clip(prefix + g["sep"].join(parts), width - 1, g["ell"])


class Hud:
    """Owns the scroll region, the ticker thread, and every terminal write."""

    def __init__(self, root: Path, engine_label: str, chat_mode: str) -> None:
        self.root = root
        self.engine_label = engine_label
        self.chat_mode = chat_mode
        self.repo = root.name
        self.branch = _git_branch(root)
        self.g = _glyphs()
        self.lock = threading.RLock()
        self.live = False
        self.usage: dict[str, Any] | None = None
        self.session: dict[str, Any] = {"turns": 0, "cost_usd": 0.0}
        self.input_buf = ""
        self.input_pos = 0
        self.prompt = "harness> "
        self._turn_started: float | None = None
        self._spin_i = 0
        self._workers: list[dict[str, Any]] = []
        self._workers_at = 0.0
        self._alerts = 0
        self._plan: list[dict[str, Any]] = []  # chat-plan-hud: live TodoWrite steps
        self._plan_grant: dict[str, Any] = {}   # chat-takeover-card: approved plan-grant
        self._plan_expired = False
        self._gate_holding = False
        self._paused = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._geometry: tuple[int, int, int] = (0, 0, 0)  # cols, rows, hud height

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if os.environ.get("HARNESS_CHAT_NO_HUD") == "1":
            return False
        try:
            cols, rows = shutil.get_terminal_size()
            if rows < MIN_HEIGHT or not sys.stderr.isatty():
                return False
            self.live = True
            self._redraw()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._emergency_off()
            return False

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self.live:
            with self.lock:
                try:
                    _, rows, _ = self._geometry
                    sys.stderr.write("\x1b[r")  # full scroll region back
                    if rows:
                        sys.stderr.write(f"\x1b[{rows};1H\x1b[2K")
                    sys.stderr.write("\x1b[?25h\n")
                    sys.stderr.flush()
                except Exception:
                    pass
            self.live = False

    def _emergency_off(self) -> None:
        self.live = False
        try:
            sys.stderr.write("\x1b[r\x1b[?25h")
            sys.stderr.flush()
        except Exception:
            pass

    # -- conversation + state ----------------------------------------------
    def say(self, *parts: Any) -> None:
        """Print conversation text into the scrolling area above the HUD."""
        text = " ".join(str(p) for p in parts)
        if not self.live:
            print(text)
            return
        with self.lock:
            try:
                _, rows, hud_h = self._geometry
                top = rows - hud_h
                sys.stdout.flush()
                sys.stderr.write(f"\x1b[{top};1H")
                sys.stderr.flush()
                for line in (text.split("\n") or [""]):
                    sys.stdout.write("\n" + line)
                sys.stdout.flush()
                self._redraw()
            except Exception:
                self._emergency_off()
                print(text)

    def set_mode(self, chat_mode: str) -> None:
        self.chat_mode = chat_mode
        self._safe_redraw()

    def render_input(self, buf: str, pos: int) -> None:
        self.input_buf, self.input_pos = buf, pos
        self._safe_redraw()

    def set_plan(self, steps: list[dict[str, Any]] | None) -> None:
        """chat-plan-hud: update the bottom plan rows; a FINISHED plan flushes
        once as a checklist into the scrollback (history) and drops the rows."""
        steps = steps or []
        if steps and all(s.get("status") == "completed" for s in steps):
            marks = {"completed": "v"}
            self.say("\n".join([f"  plan complete ({len(steps)}/{len(steps)}):"]
                               + [f"    {marks.get(str(s.get('status')), 'v')} {s.get('text', '')}"
                                  for s in steps]))
            self._plan = []
        else:
            self._plan = steps
        self._safe_redraw()

    def begin_turn(self) -> None:
        self._turn_started = time.monotonic()
        self._safe_redraw()

    def end_turn(self, usage: dict[str, Any] | None, session: dict[str, Any]) -> None:
        self._turn_started = None
        if usage:
            self.usage = usage
        self.session = dict(session)
        self._safe_redraw()

    def paused(self):
        """Suspend drawing + release the region for nested prompts (menus, y/N)."""
        hud = self

        class _Paused:
            def __enter__(self):
                hud.lock.acquire()
                hud._paused += 1
                if hud.live:
                    try:
                        _, rows, hud_h = hud._geometry
                        sys.stderr.write(f"\x1b[r\x1b[{rows - hud_h};1H\n")
                        sys.stderr.flush()
                    except Exception:
                        hud._emergency_off()
                return self

            def __exit__(self, *exc):
                hud._paused -= 1
                if hud.live and hud._paused == 0:
                    hud._geometry = (0, 0, 0)  # force region re-setup
                    try:
                        hud._redraw()
                    except Exception:
                        hud._emergency_off()
                hud.lock.release()
                return False

        return _Paused()

    # -- drawing -----------------------------------------------------------
    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            if not self.live or self._paused:
                continue
            self._spin_i += 1
            self._safe_redraw()

    def _safe_redraw(self) -> None:
        if not self.live or self._paused:
            return
        with self.lock:
            try:
                self._redraw()
            except Exception:
                self._emergency_off()

    def _workers_snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now - self._workers_at > WORKERS_CACHE_S:
            import datetime as dt
            now_utc = dt.datetime.now(dt.timezone.utc)
            self._workers = read_workers(self.root, now_utc)
            self._alerts = read_pending_critical(self.root)  # same cadence, no new polling
            self._plan_grant = read_approved_plan(self.root)  # chat-takeover-card
            exp = parse_iso_datetime(str(self._plan_grant.get("expiresAt") or ""))
            self._plan_expired = bool(exp is not None and exp < now_utc)
            self._gate_holding = _gate_holding(self.root)
            self._workers_at = now
        return self._workers

    def _redraw(self) -> None:
        cols, rows = shutil.get_terminal_size()
        if rows < MIN_HEIGHT:
            return
        workers = self._workers_snapshot()
        agent_rows = format_worker_rows(workers, cols, self.g)
        plan_rows = format_plan_rows(self._plan, cols, self.g)
        takeover_rows = format_takeover_row(self._plan_grant, workers, self._plan,
                                            self._alerts, self._gate_holding, cols, self.g,
                                            expired=self._plan_expired)
        base_h = 4 + len(agent_rows) + len(plan_rows)
        if base_h + len(takeover_rows) > rows - 1:
            takeover_rows = []  # narrow terminal: shed the takeover card (status row stays)
        hud_h = base_h + len(takeover_rows)
        if (cols, rows, hud_h) != self._geometry:
            sys.stderr.write(f"\x1b[1;{rows - hud_h}r")
            if self._geometry == (0, 0, 0):
                sys.stderr.write(f"\x1b[{rows - hud_h};1H")  # park content cursor once
            self._geometry = (cols, rows, hud_h)
        top = rows - hud_h + 1
        spin = self.g["spin"][self._spin_i % len(self.g["spin"])]
        turn_elapsed = (time.monotonic() - self._turn_started) if self._turn_started else None
        lines = [self.g["line"] * (cols - 1),
                 format_telemetry_row(self.usage, self.session, turn_elapsed, spin, cols, self.g),
                 *takeover_rows,
                 *plan_rows,
                 *agent_rows,
                 format_status_row(self.repo, self.branch, self.engine_label,
                                   self.chat_mode, cols, self.g, alerts=self._alerts)]
        out = ["\x1b7"]  # DECSC
        for i, line in enumerate(lines):
            out.append(f"\x1b[{top + i};1H\x1b[2K{line}")
        prompt_line = _clip(self.prompt + self.input_buf, cols - 1, self.g["ell"])
        out.append(f"\x1b[{rows};1H\x1b[2K{prompt_line}")
        out.append("\x1b8")  # DECRC
        # park the real cursor at the input cell so typing looks native
        cursor_col = min(len(self.prompt) + self.input_pos + 1, cols - 1)
        out.append(f"\x1b[{rows};{cursor_col}H")
        sys.stderr.write("".join(out))
        sys.stderr.flush()


def _self_check() -> None:
    import datetime as dt
    import json as _json
    import tempfile

    g = {"spin": "|", "on": "#", "off": ".", "bar_sep": "|", "sep": " | ",
         "line": "-", "ell": "...", "dash": "-", "warn": "[!]"}

    # formatting helpers
    assert _fmt_elapsed(None) == "-" and _fmt_elapsed(45) == "45s"
    assert _fmt_elapsed(192) == "3m12s" and _fmt_elapsed(3700) == "1h01m"
    assert _clip("abcdef", 6, "...") == "abcdef" and _clip("abcdefg", 6, "...") == "abc..."

    # status row segment dropping: mode always survives
    full = format_status_row("repo", "main", "claude·sonnet·high", "auto", 120, g)
    assert "repo·main" in full and "mode: auto" in full
    narrow = format_status_row("repo", "main", "claude·sonnet·high", "auto", 30, g)
    assert "mode: auto" in narrow and "repo" not in narrow
    # R28: alert badge rides the mode segment (never dropped)
    warn_row = format_status_row("repo", "main", "claude", "auto", 120, g, alerts=2)
    assert "mode: auto [!]2" in warn_row
    assert "[!]" not in format_status_row("repo", "main", "claude", "auto", 120, g, alerts=0)

    # telemetry row: bar kept under pressure; honest n/a
    u = {"in_tokens": 12300, "out_tokens": 400, "cost_usd": 0.031, "duration_s": 12.4,
         "ctx_used": 340000, "ctx_window": 1000000}
    s = {"turns": 3, "cost_usd": 0.19}
    wide = format_telemetry_row(u, s, None, "|", 120, g)
    assert "34% of 1000.0k" in wide and "session $0.19/3 turns" in wide and "####" in wide
    tight = format_telemetry_row(u, s, None, "|", 40, g)
    assert "ctx" in tight and "session" not in tight
    spin = format_telemetry_row(u, s, 23.0, "|", 120, g)
    assert spin.startswith("| 23s ")
    assert "ctx n/a" in format_telemetry_row(None, {"turns": 0}, None, "|", 80, g)

    # workers reader + rows (R24: ~estimates)
    tmp = Path(tempfile.mkdtemp())
    wf_dir = tmp / ".harness" / "workflows" / "active" / "WF-1"
    wf_dir.mkdir(parents=True)
    now = dt.datetime.now(dt.timezone.utc)
    started = (now - dt.timedelta(seconds=192)).isoformat()
    (wf_dir / "workflow.json").write_text(_json.dumps({"workers": [
        {"workerId": "W-01", "unitId": "unit-042", "taskProfile": "implementation",
         "status": "running", "updatedAt": started, "estimatedPromptTokens": 8000,
         "estimatedResultTokens": 400},
        {"workerId": "W-02", "unitId": "unit-043", "taskProfile": "review",
         "status": "queued", "estimatedPromptTokens": 2100},
        {"workerId": "W-03", "status": "succeeded", "updatedAt": started}]}), encoding="utf-8")
    rows = read_workers(tmp, now)
    assert [r["workerId"] for r in rows] == ["W-01", "W-02"]  # done filtered, running first
    assert 191 <= rows[0]["elapsed"] <= 193 and rows[1]["elapsed"] is None
    lines = format_worker_rows(rows, 100, g)
    assert len(lines) == 2 and "~8.4k tok" in lines[0] and "3m12s" in lines[0]
    assert format_worker_rows(rows, 45, g) == []  # narrow hides the panel
    many = rows * 4
    lines = format_worker_rows(many, 100, g)
    assert len(lines) == MAX_AGENT_ROWS and "more workers" in lines[-1]
    # SPEC-114 v2: include_done also returns finished workers with a finishedAt
    allrows = read_workers(tmp, now, include_done=True)
    assert [r["workerId"] for r in allrows] == ["W-01", "W-02", "W-03"]  # done sorts last
    assert allrows[-1]["finishedAt"] == started and "finishedAt" not in allrows[0]
    (wf_dir / "workflow.json").write_text("{broken", encoding="utf-8")
    assert read_workers(tmp, now) == []  # corrupt state never crashes the ticker

    # R28: pending-critical reader off escalations state; corrupt -> 0
    state_dir = tmp / ".harness" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "escalations.json").write_text(_json.dumps({"raised": {
        "a": {"criticality": "critical"}, "b": {"criticality": "watch"},
        "c": {"criticality": "high"}, "d": {}}}), encoding="utf-8")
    assert read_pending_critical(tmp) == 2
    (state_dir / "escalations.json").write_text("{broken", encoding="utf-8")
    assert read_pending_critical(tmp) == 0

    # chat-takeover-card: level-0 projection {objective, owner, next, recovery}
    grant = {"footprint": ["a.py", "b.py"], "planPath": "/x/approved-plan.md"}
    wk = [{"workerId": "W-07", "status": "running"}]
    steps = [{"text": "wire the thing", "status": "in_progress"}]
    row = format_takeover_row(grant, wk, steps, 2, True, 120, g)
    assert len(row) == 1
    assert "obj approved-plan (2f)" in row[0] and "owner W-07" in row[0]
    assert "next wire the thing" in row[0] and "recovery 2 crit, gate holding" in row[0]
    # no worker -> the operator owns it; expiry marked; drops right-to-left when narrow
    exp_row = format_takeover_row(grant, [], steps, 0, False, 120, g, expired=True)
    assert "owner operator" in exp_row[0] and "obj approved-plan (2f) expired" in exp_row[0]
    narrow = format_takeover_row(grant, wk, steps, 2, True, 50, g)
    assert len(narrow) == 1 and "obj approved-plan (2f)" in narrow[0]
    assert "owner W-07" in narrow[0] and "recovery" not in narrow[0] and "next" not in narrow[0]
    # nothing to hand over -> no row; corrupt/absent grant JSON never crashes
    assert format_takeover_row({}, [], [], 0, False, 120, g) == []
    assert read_approved_plan(tmp) == {}  # absent file -> {}
    runtime_dir = tmp / ".harness" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "approved-plan.json").write_text("{broken", encoding="utf-8")
    assert read_approved_plan(tmp) == {}  # corrupt -> {}
    assert _gate_holding(tmp) is False  # missing gate-hold dir -> False

    print("chat_hud self-check ok")


if __name__ == "__main__":
    _self_check()
