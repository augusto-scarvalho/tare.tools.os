"""Chat engines + command gating for the harness operator REPL (SPEC-111).

Extracted from chat_operator.py (QA.3 self-lib-file-budget). Owns: the
read-only allowlist and classify/gate ladder, the single harness-command
execution path, and the three agent engines (Claude headless, Codex
best-effort, OpenAI-compatible tool loop). Exercised by chat_operator's
__main__ self-check and testing/scenarios/ux_repl_onboarding.py.
"""
from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

try:
    from . import context_diet, model_routing, prompt_kit, stream_json
    from .common import HarnessError, truncate_text
except ImportError:  # run directly / flat sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import context_diet, model_routing, prompt_kit, stream_json
    from harness_lib.common import HarnessError, truncate_text

PROMPT_REL = ".harness/prompts/harness-operator.md"
# SPEC-144 front-desk: the router role converses through the porteiro contract,
# not the operator one. Any role missing here keeps the operator prompt.
# SPEC-146 chat rooms: the two overseer ROOMS share one room prompt (converse +
# implement + commit, complexity-tiered write authorization).
PROMPT_BY_ROLE = {"router": ".harness/prompts/front-desk.md",
                  "route-overseer": ".harness/prompts/room-overseer.md",
                  "ui-overseer": ".harness/prompts/room-overseer.md"}
# SPEC-118 v5: the chat SURFACE's own tool floor -- whatever the role's diet says,
# the REPL needs exec (harness commands) and todo (the plan HUD) to function.
CHAT_TOOL_FLOOR = ("exec", "todo")
OUTPUT_CAP = 8000  # chars of command output fed back into the model context
MAX_TOOL_ROUNDS = 20  # openai engine: safety valve against tool-call loops
# One agent turn can legitimately run several harness commands; tune via env.
TURN_TIMEOUT = float(os.environ.get("HARNESS_CHAT_TURN_TIMEOUT", "1800"))

INTERRUPT_NOTE = "[turn interrupted by user]"

# SPEC-137 orphaned-gate vigil (incident 2026-07-17): the same cmdline signature
# tools/hooks/subagent_gate_wait.py matches (that hook stays import-free by
# design, so the 4-line signature is duplicated, not shared).
GATE_PROC_RE = re.compile(r"spec_test_gate\.py|harness\.py[^\r\n]*\bvalidate\b|harness-test\.py",
                          re.IGNORECASE)


def gate_processes() -> list[str]:
    """Cmdlines of live harness gate/validate processes. Raises on listing
    trouble — the caller owns fail-open vs keep-trying (the stop hook's silent
    fail-open under gate load is what hid the incident)."""
    if sys.platform == "win32":
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        lines = (proc.stdout or "").splitlines()
    else:
        proc = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        lines = (proc.stdout or "").splitlines()
    return [ln.strip() for ln in lines if GATE_PROC_RE.search(ln)]


def _interrupt_event(engine: Any) -> threading.Event:
    """The engine's interrupt Event — created on demand because scenarios build
    engines via __new__ (no __init__, see _argv docstring)."""
    ev = getattr(engine, "_interrupted", None)
    if ev is None:
        ev = engine._interrupted = threading.Event()
    return ev


def _kill_turn_tree(proc: Any) -> None:
    """Kill the WHOLE turn tree, not just the direct child. On Windows the
    engine spawns claude.CMD -> cmd.exe -> node: proc.kill() terminates only
    the cmd.exe shim, the node agent survives holding the inherited stdout
    pipe, and the turn visibly KEEPS running tools and streaming after ⏹
    (owner incident 2026-07-17). signal_process_tree = taskkill /T /F there;
    proc.kill() stays as the final belt-and-suspenders pass."""
    try:
        from .processes import signal_process_tree
    except ImportError:
        from harness_lib.processes import signal_process_tree
    import signal as _signal
    with contextlib.suppress(Exception):
        signal_process_tree(proc.pid, getattr(_signal, "SIGKILL", _signal.SIGTERM))
    with contextlib.suppress(OSError):
        proc.kill()


def _watch_interrupt(proc: Any, interrupted: threading.Event) -> threading.Event:
    """Turn-stop watcher (GUI ⏹ / CLI Esc-Esc): kills the turn's child TREE when
    engine.interrupt() fires or the panel touches HARNESS_CHAT_STOP_FILE (the
    out-of-band channel — the REPL is blocked reading the engine stream, so a
    piped '/stop' line would only be seen after the turn). Returns the Event
    that ends the watcher; the flag file is consumed (unlinked) on trigger."""
    done = threading.Event()
    flag = os.environ.get("HARNESS_CHAT_STOP_FILE")
    if flag:  # a stale flag (⏹ clicked between turns) must not kill THIS turn
        with contextlib.suppress(OSError):
            os.unlink(flag)

    def _watch() -> None:
        while not done.wait(0.25):
            if not interrupted.is_set():
                if not (flag and os.path.exists(flag)):
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(flag)
                interrupted.set()
            _kill_turn_tree(proc)
            return

    threading.Thread(target=_watch, daemon=True).start()
    return done

# Exact-only: extra args can turn these mutating (status --html writes a page,
# escalations --resolve resolves one).
READ_ONLY_EXACT = [("status",), ("escalations",)]
# Prefix: read-only regardless of trailing args.
READ_ONLY_PREFIXES = [
    ("graph-status",), ("metrics",), ("executor", "list"), ("targets", "list"),
    ("records", "search"), ("records", "recent"),
    ("protected-files", "list"), ("protected-files", "check"),
    ("classify",), ("doc-find",), ("executor", "validate"),
    ("workflow", "suggest"), ("workflow", "status"), ("workflow", "state"),
    ("workflow", "events"), ("workflow", "token-audit"),
    ("agents", "audit"),
    ("models", "list"), ("models", "catalog"), ("models", "show"),  # SPEC-115 read-only
    ("routing", "show"),
    # SPEC-144 front-desk dispatch: single-demand classify+brief only. Deliberately
    # ("route", "--task") and not ("route",): the bare prefix would put
    # `Bash(... route *)` on the Claude allowlist and auto-approve `route --loop`
    # (live worker spawns). --loop is re-gated in classify_command besides.
    ("route", "--task"),
]
# Never runnable by the agent, not even with a y/n confirm: the human must type
# the command via the ! escape (humanInTheLoop, .harness/project.json).
# --approve-writes: the SPEC-144 route-loop token is owner-typed, never the agent's.
HUMAN_ONLY_FLAGS = {"--approval-token", "--send", "--approve-writes"}

# Interaction modes (SPEC-111 R23), Shift+Tab cycle order / /mode names.
MODES = ("manual", "plan", "auto", "accept-edits")
MODE_TAGS = {
    "plan": "[mode: plan — propose a plan; do not run mutating harness commands; "
            "only read-only commands will execute]\n\n",
    "accept-edits": "[mode: accept-edits — mutating harness commands are pre-approved "
                    "and will run; --approval-token/--send commands remain human-only]\n\n",
}


def gate_for(category: str, chat_mode: str) -> str:
    """Combine classify_command's category with the interaction mode (R23).

    Returns 'run' | 'confirm' | 'blocked-plan' | 'human-only'.
    """
    if category == "human-only":
        return "human-only"
    if category == "auto":
        return "run"
    return {"plan": "blocked-plan", "auto": "confirm",
            "accept-edits": "run", "manual": "confirm"}[chat_mode]


def classify_command(args: list[str]) -> str:
    """Classify a harness.py argv tail as 'auto' | 'confirm' | 'human-only'."""
    if any(a in HUMAN_ONLY_FLAGS for a in args):
        return "human-only"
    # SPEC-144 front-desk: single-demand `route` is read-only classify+brief (the
    # dispatch lane), but the --loop driver spawns workers/commits -> keep gated.
    if args and args[0] == "route" and "--loop" in args:
        return "confirm"
    key = tuple(args)
    if key in READ_ONLY_EXACT:
        return "auto"
    if any(key[: len(p)] == p for p in READ_ONLY_PREFIXES):
        return "auto"
    return "confirm"


def _project_python(root: Path) -> str:
    """Repo-relative venv python string, as documented in the operator prompt."""
    rel = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    if (root / rel).exists():
        return f"./{rel}"
    return sys.executable  # harness already runs under a working interpreter


def run_harness_command(root: Path, args: list[str], human: bool = False) -> str:
    cmd = [sys.executable, "scripts/harness.py", *args]
    # human=True marks the ! escape / manual-mode path: the HITL deny hook
    # (tools/hooks/deny_hitl_flags.py) must not fire on human-typed commands.
    # TE.5: only the agent path (human=False) gets compact output — the human
    # reads the ! escape's output in the terminal, so it stays pretty JSON.
    env = ({**os.environ, "HARNESS_CHAT_HITL_OK": "1"} if human
           else {**os.environ, "HARNESS_AGENT_OUTPUT": "compact"})
    try:
        res = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return f"error: `harness.py {' '.join(args)}` timed out after 600s"
    out = (res.stdout or "") + (("\n[stderr]\n" + res.stderr) if res.stderr else "")
    out, _ = truncate_text(out.strip(), OUTPUT_CAP)
    return f"exit code {res.returncode}\n{out}" if res.returncode else (out or "(no output)")


def _allowed_tool_patterns(root: Path) -> list[str]:
    py = _project_python(root)
    pats = [" ".join(p) for p in READ_ONLY_EXACT]
    pats += [" ".join(p) + " *" for p in READ_ONLY_PREFIXES]
    allowed = [f"Bash({py} scripts/harness.py {p})" for p in pats]
    # Owner decision 2026-07-13 #5 (PERMIT): TodoWrite in headless chat spawns —
    # plan-annotation only, no project-disk writes; feeds the chat-plan-hud.
    allowed.append("TodoWrite")
    return allowed


# SPEC-146 chat rooms: the front-end surfaces the ui-overseer room is structurally
# confined to (path-scoped Edit()/Write() allowlist; Step-1 probe proved the build
# honors it). Mirrors ui-delivery.fileGlobs in task-profiles.json — keep the two in sync.
UI_SURFACES = ["scripts/harness_ui.py", "scripts/harness_ui_page.py",
               "scripts/harness_ui_page_md.py", "scripts/harness_lib/ui_*.py",
               "scripts/harness_lib/status_html.py", "testing/ui/**"]
# The overseer ROOM roles the porteiro hands off to (both share room-overseer.md).
ROOM_ROLES = ("route-overseer", "ui-overseer")


def session_role_env(role: str, model: str | None = None,
                     effort: str | None = None) -> dict[str, str]:
    """Env telling the vendor CLI child WHICH role's playbook chain the
    SessionStart reinjection hook must compose (reload_context_after_compact
    reads HARNESS_SESSION_ROLE, default overseer). Without this every chat
    child — the GUI porteiro included — hydrated with the OVERSEER contract
    (owner 2026-07-30: GUI opens on router; only a bare vendor CLI, which
    never passes through here, keeps the overseer default). Empty role ->
    {} so role-less engine builds stay byte-identical.

    seat-telemetry (2026-07-31): the same splice now carries the rest of the
    SEAT — (role, model, effort) — because that triplet is what a delegation or
    a defect is attributed to, and a child that cannot name its own seat forces
    the operator to retype it (and to get it wrong). MODEL/EFFORT are added only
    when known: a partial seat must not look complete downstream."""
    if not role:
        return {}
    # An UNKNOWN model/effort is cleared, never left to inherit. The splice lands in
    # `{**os.environ, ...}`, so a child given a new ROLE but no model would keep the
    # PARENT's HARNESS_SESSION_MODEL and present a seat that is complete and WRONG —
    # `router|<the overseer's model>|high` (audit probe, kimi, 2026-07-31). Every
    # guard downstream tests completeness; none can test truthfulness. Empty string
    # is the clear: seat_identity rejects it, so the seat degrades to unknown.
    # `configured-by-executor` (model_routing.CONFIGURED) is ALSO unknown: it is the
    # role-metamodel placeholder meaning "the executor picks the model/effort", so
    # carrying it verbatim would present another fake-complete seat.
    def _known(v: str | None) -> str:
        s = str(v or "")
        return "" if s == model_routing.CONFIGURED else s
    return {"HARNESS_SESSION_ROLE": role,
            "HARNESS_SESSION_MODEL": _known(model),
            "HARNESS_SESSION_EFFORT": _known(effort)}


def room_tool_patterns(root: Path, role: str) -> list[str]:
    """Write-capable allowlist for an overseer ROOM (SPEC-146). `route-overseer`
    gets bare Edit/Write; `ui-overseer` is structurally confined to UI_SURFACES
    (path-scoped Edit(<glob>)/Write(<glob>) — the harness only permits edits there).
    Both add harness/pytest/scenario/self-check Bash + git staging (NO git push).
    Any other role -> [] so roomless chat stays byte-identical.
    # ponytail: Bash staging is not path-confined (git add/commit reach anything) —
    # the Edit/Write scoping + the room prompt + SPEC-137's structural gate + the
    # watching owner are the confinement layers, not the Bash allowlist."""
    if role not in ROOM_ROLES:
        return []
    py = _project_python(root)
    if role == "ui-overseer":
        edits = [f"Edit({g})" for g in UI_SURFACES] + [f"Write({g})" for g in UI_SURFACES]
    else:  # route-overseer: full write capability, prompt+SPEC-137 confine it
        edits = ["Edit", "Write"]
    bash = [f"Bash({py} scripts/harness.py *)", f"Bash({py} -m pytest *)",
            f"Bash({py} testing/scenarios/*)", f"Bash({py} scripts/harness_lib/*)",
            "Bash(git status)", "Bash(git diff *)", "Bash(git log *)",
            "Bash(git show *)", "Bash(git add *)", "Bash(git commit *)"]
    return edits + bash


class ClaudeEngine:
    """Headless Claude Code, one `claude -p --resume` run per turn."""

    name = "claude"

    def __init__(self, root: Path, model: str | None, effort: str | None,
                 prompt_rel: str = PROMPT_REL, diet: dict[str, Any] | None = None) -> None:
        self.binary = shutil.which("claude")
        if not self.binary:
            raise HarnessError("claude CLI not found on PATH\nfix: install Claude Code or pick another engine (--engine openai)")
        self.root = root
        self.model = model
        self.effort = effort
        self.prompt_rel = prompt_rel
        # SPEC-118 v5 session diet: layer flags ride the argv; denied tools merge
        # into the ONE --disallowedTools below (a repeated flag risks a silent
        # last-one-wins override of the plan-mode lockout).
        self.diet_layer = context_diet.layer_flags("claude", diet)
        self.diet_denied = context_diet.denied_tools("claude", diet, CHAT_TOOL_FLOOR)
        self.diet_env = context_diet.env_for(diet)
        self.session_id: str | None = None
        self.chat_mode = "auto"
        self.last_usage: dict[str, Any] | None = None
        self._prev_cost = 0.0  # total_cost_usd may be cumulative under --resume
        self._interrupted = threading.Event()

    def interrupt(self) -> None:
        """Stop the CURRENT turn (kills the per-turn child; partial text kept)."""
        _interrupt_event(self).set()

    def _argv(self, allowed: list[str]) -> list[str]:
        """Pure argv builder (everything up to --resume), split out so the REPL
        mode governs the REAL vendor session, not just the prompt tag (SPEC-114
        R19). Excludes the binary so the self-check can build via __new__."""
        # stream-json + --verbose (SPEC-114 v13): the SAME adapter the worker path
        # uses (executors.json), so per-tool activity surfaces MID-turn instead of a
        # single buffered blob. claude requires --verbose with -p stream-json.
        cmd = ["-p", "--output-format", "stream-json", "--verbose",
               # getattr: scenarios build engines via __new__ without __init__
               "--append-system-prompt-file", getattr(self, "prompt_rel", PROMPT_REL),
               # SPEC-118 v5 session diet (empty without a role diet)
               *getattr(self, "diet_layer", []),
               "--allowedTools", *allowed,
               # R19: mode has one source of truth (the REPL/human); the agent
               # can't self-enter plan mode — headless --resume can't approve its exit.
               # Diet-denied tools merge here: ONE --disallowedTools flag total.
               "--disallowedTools", "EnterPlanMode", "ExitPlanMode",
               *[t for t in getattr(self, "diet_denied", [])
                 if t not in ("EnterPlanMode", "ExitPlanMode")]]
        # Always explicit: user-scope settings can set defaultMode (e.g. plan on
        # this machine), and a headless session born in plan mode can't leave it
        # (incident 2026-07-10: badge said auto, session answered in plan mode).
        cmd += ["--permission-mode",
                {"plan": "plan", "accept-edits": "acceptEdits"}.get(self.chat_mode, "auto")]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def send(self, msg: str) -> str:
        allowed = _allowed_tool_patterns(self.root)
        # SPEC-146 chat rooms: an overseer ROOM engine carries write-capable
        # patterns (Edit/Write + git staging); roomless chat has room_tools == [],
        # so the allowlist is byte-identical to today off-room.
        allowed += getattr(self, "room_tools", [])
        if self.chat_mode == "accept-edits":
            # mutations pre-approved; the deny_hitl_flags PreToolUse hook is
            # the structural backstop for --approval-token/--send (R23)
            allowed.append(f"Bash({_project_python(self.root)} scripts/harness.py *)")
        cmd = [self.binary, *self._argv(allowed)]
        # verbose chat (SPEC-114 v13): fired per tool_use so run_chat can emit a
        # `tool` event mid-turn. getattr-guarded → only ClaudeEngine consumes it;
        # codex/openai/manual set nothing and are unaffected.
        on_tool = getattr(self, "on_activity", None)
        # chat-tool-chips increment 2: tool RESULTS feed the chip inspector.
        on_tool_result = getattr(self, "on_tool_result", None)
        # chat-plan-hud: TodoWrite updates feed the plan HUD/card mid-turn.
        on_plan = getattr(self, "on_plan", None)
        # room-chat fix: mid-turn assistant text surfaces as it streams.
        on_text = getattr(self, "on_text", None)
        # chat-status-line: per-message usage feeds the live token HUD.
        on_usage = getattr(self, "on_usage", None)
        # message via stdin (claude.CMD goes through cmd.exe on Windows, so the user's
        # free text must never be an argv element); stream-json stdout is parsed
        # line-by-line as claude runs. stderr is drained on a thread so a chatty child
        # can't deadlock the stdout read; a watchdog timer enforces TURN_TIMEOUT.
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                # TE.5: the session's Bash-run harness.py commands inherit compact output;
                # diet_env (SPEC-118 v5) skips the canonical-pack reinjection for a dieted role.
                # HARNESS_CHAT_TURN marks an unattended engine turn: the Stop wiring of
                # subagent_gate_wait.py guards ONLY these (stall class, intake 549bd180a1df).
                # PYTHONIOENCODING/PYTHONUTF8: python descendants of the engine's Bash
                # calls write cp1252 into claude's utf-8 pipe otherwise → U+FFFD in the
                # tool-result digests the chip inspector shows.
                env={**os.environ, "HARNESS_AGENT_OUTPUT": "compact",
                     "HARNESS_CHAT_TURN": "1", "PYTHONIOENCODING": "utf-8",
                     "PYTHONUTF8": "1", **getattr(self, "diet_env", {}),
                     **session_role_env(getattr(self, "room_role", ""),
                                        getattr(self, "model", None),
                                        getattr(self, "effort", None))})
        except OSError as exc:
            return f"[claude spawn failed] {exc}"
        err: list[str] = []
        err_t = threading.Thread(target=lambda: err.append(proc.stderr.read() or ""), daemon=True)
        err_t.start()
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            _kill_turn_tree(proc)  # same orphan-node bug as ⏹: shim-only kill left the agent running
        timer = threading.Timer(TURN_TIMEOUT, _on_timeout)
        timer.start()
        interrupted = _interrupt_event(self)
        interrupted.clear()
        watch_done = _watch_interrupt(proc, interrupted)
        try:
            try:
                proc.stdin.write(msg)
                proc.stdin.close()
            except OSError:
                pass
            parsed = stream_json.parse_chat_stream(proc.stdout, on_tool=on_tool,
                                                   on_tool_result=on_tool_result,
                                                   on_plan=on_plan, on_text=on_text,
                                                   on_usage=on_usage)
            proc.wait()
        finally:
            timer.cancel()
            watch_done.set()
            err_t.join(timeout=1)
        if interrupted.is_set():
            # keep the session id (init event streams early) so --resume still works
            self.session_id = parsed.get("session_id") or self.session_id
            partial = (parsed["text"] or "").strip()
            return f"{partial}\n\n{INTERRUPT_NOTE}" if partial else INTERRUPT_NOTE
        if timed_out.is_set():
            return f"[claude turn timed out after {TURN_TIMEOUT:.0f}s — raise HARNESS_CHAT_TURN_TIMEOUT if legitimate]"
        stderr_text = "".join(err).strip()
        if proc.returncode:
            return f"[claude exited {proc.returncode}] {(stderr_text or parsed['text'] or '').strip()}"
        self.session_id = parsed.get("session_id") or self.session_id
        if parsed.get("result"):
            self.last_usage = self._telemetry(parsed["result"], parsed.get("last_ctx_tokens"))
        return parsed["text"] or stderr_text

    def _telemetry(self, data: dict[str, Any],
                   last_ctx: int | None = None) -> dict[str, Any] | None:
        usage = data.get("usage") or {}
        if not usage:
            return None
        in_tok = sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        cost = data.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            # total_cost_usd is cumulative across --resume turns (E2E 2026-07-10:
            # turn1 $0.033 -> turn2 $0.057 with cheaper cache-read tokens); report the delta
            delta = float(cost) - self._prev_cost
            self._prev_cost = float(cost)
            cost = delta if delta >= 0 else float(cost)
        else:
            cost = None
        models = data.get("modelUsage") or {}
        model_id, main = (max(models.items(), key=lambda kv: kv[1].get("inputTokens") or 0)
                          if models else (None, {}))
        duration = data.get("duration_ms")
        return {"in_tokens": in_tok or None,
                "out_tokens": usage.get("output_tokens"),
                # TE.6: split out so the ledger can measure cache efficiency —
                # cached reads bill ~10% of input; the share proves (or refutes)
                # the stable-prefix packet discipline.
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cost_usd": cost,
                "duration_s": duration / 1000 if isinstance(duration, (int, float)) else None,
                # ctx = the LAST API round's input (stream-parsed); the result
                # usage sums every round, which read >100% on tool-heavy turns.
                "ctx_used": last_ctx or in_tok or None,
                "ctx_window": main.get("contextWindow"),
                "model": model_id}


def find_codex() -> str | None:
    """Codex ships off PATH on Windows (installer drops it under LOCALAPPDATA),
    so which() alone gives a false "not found" — probe the install path too.
    Single probe shared by the engine and the setup wizard (SPEC-114 R20)."""
    fallback = (Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
    return shutil.which("codex") or (str(fallback) if fallback.exists() else None)


class CodexEngine:
    """Headless Codex CLI, one `codex exec [resume]` run per turn (SPEC-114 R20).

    Session continuity is by explicit thread_id (captured from the JSONL
    `thread.started` event), which dodges the wrong-session race that
    `resume --last` hits when a human drives codex in parallel.
    """

    name = "codex"

    def __init__(self, root: Path, model: str | None, effort: str | None = None,
                 prompt_rel: str = PROMPT_REL, diet: dict[str, Any] | None = None) -> None:
        self.binary = find_codex()
        if not self.binary:
            raise HarnessError("codex CLI not found on PATH or under "
                               "%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\n"
                               "fix: install Codex or pick another engine (--engine claude|openai)")
        self.root = root
        self.model = model
        self.effort = effort
        self.prompt_rel = prompt_rel
        # SPEC-118 v5 diet: codex has only the project-doc layer knob (flags_for
        # is keepTools-inert for codex); env skip is vendor-neutral.
        self.diet_flags = context_diet.flags_for("codex", diet)
        self.diet_env = context_diet.env_for(diet)
        self.thread_id: str | None = None
        self.chat_mode = "auto"
        self.last_usage: dict[str, Any] | None = None
        self._interrupted = threading.Event()

    def interrupt(self) -> None:
        """Stop the CURRENT turn (kills the per-turn child; partial text kept)."""
        _interrupt_event(self).set()

    def _argv(self) -> list[str]:
        """Pure argv builder (excludes the binary; prompt read from stdin as '-')
        so the REPL mode governs codex's real sandbox, not just the prompt tag
        (R20 parity with ClaudeEngine._argv / R19). Fresh exec takes `-s`; resume
        has no `-s`, so the same mode maps to a `-c sandbox_mode=...` config
        override — validated live 2026-07-10 to re-enforce mid-session. Modes
        other than plan/accept-edits omit the flag (CLI default preserved)."""
        sandbox = {"plan": "read-only", "accept-edits": "workspace-write"}.get(self.chat_mode)
        # SPEC-146 rooms on codex: write capability maps to the DIRECTORY sandbox
        # (claude confines by --allowedTools path globs; codex has no per-path
        # grants). ui-overseer's path-scoped contract is not expressible here →
        # read-only (owner decision 2026-07-17: analyse/propose, never edit).
        # ponytail: capability-level mapping; revisit when the harness-native
        # sandbox lands (intake: harness-sandbox-room-confinement). Plan mode wins.
        if getattr(self, "room_tools", []) and self.chat_mode != "plan":
            sandbox = ("read-only" if getattr(self, "room_role", "") == "ui-overseer"
                       else "workspace-write")
        if self.thread_id:
            cmd = ["exec", "resume", self.thread_id, "--json"]
            if sandbox:
                cmd += ["-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd = ["exec", "--json"]
            if sandbox:
                cmd += ["-s", sandbox]
        # Codex has no --effort flag but honors -c model_reasoning_effort on both
        # fresh exec and resume — validated live 2026-07-10 (banner: reasoning
        # effort: low). So the card default reaches the real session (SPEC-114 R21).
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        if self.model:
            cmd += ["-m", self.model]
        cmd += getattr(self, "diet_flags", [])  # SPEC-118 v5 (empty without a role diet)
        cmd += ["-"]  # read prompt from stdin (quoting safety, same as claude)
        return cmd

    # M1 diff derivation (2026-07-17): the exec --json adapter DROPS the patch
    # content upstream (event_processor_with_jsonl_output.rs maps
    # PatchChangeKind::Update{..} to {path, kind} only), so the engine derives it:
    # snapshot the listed paths when a file_change item STARTS, difflib old→new
    # when it COMPLETES. Exact even over the owner's local dirt; a lost race
    # (apply beat the snapshot → old == new) falls back to `git diff HEAD`.
    # ponytail: per-file cap keeps a giant patch from ballooning a turn.
    _SNAP_CAP = 512_000  # chars per snapshotted file

    def _read_capped(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")[: self._SNAP_CAP]
        except OSError:
            return ""  # absent/unreadable = empty side of the diff

    def _git_diff_fallback(self, path: str) -> str:
        """Worktree diff vs HEAD for one path — may include pre-existing local
        edits (labeled tradeoff); empty on any trouble."""
        try:
            res = subprocess.run(["git", "diff", "--no-color", "HEAD", "--", path],
                                 cwd=self.root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15)
            return (res.stdout or "").strip() if res.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _file_change_diff(self, snap: dict[str, str],
                          changes: list[dict[str, str]]) -> tuple[str, str, int, int]:
        """Diffs for a completed file_change: (unified, pretty, added, removed).
        Unified stays the M1 back-compat payload; pretty is the chip-diff-v2
        numbered form (per-path header, context-collapsed) — claude-CLI parity
        for codex chips. Delete shows the removal; a raceless-empty update tries
        git (unified only — no snapshot to number against)."""
        parts: list[str] = []
        pretty_parts: list[str] = []
        added_total = removed_total = 0
        for c in changes:
            path, kind = c.get("path") or "", c.get("kind") or ""
            if not path:
                continue
            old = snap.get(path, "")
            new = "" if kind == "delete" else self._read_capped(path)
            if old == new:
                if kind != "delete":
                    fb = self._git_diff_fallback(path)
                    if fb:
                        parts.append(fb)
                continue
            rel = os.path.relpath(path, self.root) if os.path.isabs(path) else path
            parts.append("\n".join(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")))
            pretty, added, removed = stream_json.numbered_diff(
                old.splitlines(), new.splitlines(), context=3)
            if pretty:
                head = rel if len(changes) > 1 else ""  # single-file chips name the file in arg already
                pretty_parts.append((head + "\n" if head else "") + pretty)
                added_total += added
                removed_total += removed
        return ("\n".join(p for p in parts if p),
                "\n\n".join(pretty_parts), added_total, removed_total)

    def send(self, msg: str) -> str:
        prompt = msg
        if not self.thread_id:  # first turn: prepend the role's system prompt
            prompt = (self.root / getattr(self, "prompt_rel", PROMPT_REL)).read_text(encoding="utf-8") + "\n\n" + msg
        cmd = [self.binary, *self._argv()]
        # Vendor-neutral chat surface (codex parity, 2026-07-17): the same
        # getattr-guarded callbacks ClaudeEngine reads — parse_codex_stream maps
        # the codex JSONL onto them, so chips/plan/mid-turn text work unchanged.
        on_tool = getattr(self, "on_activity", None)
        on_tool_result = getattr(self, "on_tool_result", None)
        # M1: wrap the callbacks with the file_change snapshot/diff derivation —
        # the descriptor's internal `changes` list is the wiring (see stream_json).
        snaps: dict[str, tuple[dict[str, str], list[dict[str, str]]]] = {}

        def _snap_tool(desc: dict[str, Any], _inner: Any = on_tool) -> None:
            changes = desc.get("changes") or []
            if changes:
                snaps[desc["id"]] = ({c["path"]: self._read_capped(c["path"])
                                      for c in changes if c.get("path")
                                      and c.get("kind") != "add"}, changes)
            if _inner:
                _inner(desc)

        def _diff_result(res: dict[str, Any], _inner: Any = on_tool_result) -> None:
            snap = snaps.pop(res.get("id") or "", None)
            if snap is not None:
                diff, pretty, added, removed = self._file_change_diff(snap[0], snap[1])
                if diff:
                    res["diff"] = stream_json._digest(diff)
                    # SPEC-147 inv 5: uncapped unified for the workspace pane (multi-file
                    # codex change => one unified text); bridge-stripped before SSE.
                    res["diffFull"] = {"unified": diff}
                if pretty:  # chip-diff-v2: numbered claude-CLI-style body for codex chips
                    res["diffPretty"] = stream_json._digest(pretty, stream_json.DIFF_CAP)
                    res["diffAdded"], res["diffRemoved"] = added, removed
            if _inner:
                _inner(res)

        on_tool, on_tool_result = _snap_tool, _diff_result
        on_plan = getattr(self, "on_plan", None)
        on_text = getattr(self, "on_text", None)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                # TE.5: codex's Bash-run harness.py commands inherit compact output;
                # PYTHONIOENCODING/PYTHONUTF8: python descendants write UTF-8 into
                # codex's pipes (same mojibake class as the claude engine).
                env={**os.environ, "HARNESS_AGENT_OUTPUT": "compact",
                     "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                     **getattr(self, "diet_env", {}),
                     **session_role_env(getattr(self, "room_role", ""),
                                        getattr(self, "model", None),
                                        getattr(self, "effort", None))})
        except OSError as exc:
            return f"[codex spawn failed] {exc}"
        err: list[str] = []
        err_t = threading.Thread(target=lambda: err.append(proc.stderr.read() or ""), daemon=True)
        err_t.start()
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            _kill_turn_tree(proc)  # same orphan-node bug as ⏹: shim-only kill left the agent running
        timer = threading.Timer(TURN_TIMEOUT, _on_timeout)
        timer.start()
        interrupted = _interrupt_event(self)
        interrupted.clear()
        watch_done = _watch_interrupt(proc, interrupted)
        try:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:
                pass
            parsed = stream_json.parse_codex_stream(proc.stdout, on_tool=on_tool,
                                                    on_tool_result=on_tool_result,
                                                    on_plan=on_plan, on_text=on_text)
            proc.wait()
        finally:
            timer.cancel()
            watch_done.set()
            err_t.join(timeout=1)
        if interrupted.is_set():
            self.thread_id = parsed.get("session_id") or self.thread_id
            partial = (parsed["text"] or "").strip()
            return f"{partial}\n\n{INTERRUPT_NOTE}" if partial else INTERRUPT_NOTE
        if timed_out.is_set():
            return f"[codex turn timed out after {TURN_TIMEOUT:.0f}s — raise HARNESS_CHAT_TURN_TIMEOUT if legitimate]"
        stderr_text = "".join(err).strip()
        if proc.returncode:
            return f"[codex exited {proc.returncode}] {(stderr_text or parsed['text'] or '').strip()}"
        self.thread_id = parsed.get("session_id") or self.thread_id
        if parsed.get("result"):
            usage = parsed["result"].get("usage") or {}
            # honest-n/a: raw engine numbers only — no summing reasoning tokens
            # into out_tokens, no fabricated cost; events carry no model id.
            self.last_usage = {"in_tokens": usage.get("input_tokens"),
                               "out_tokens": usage.get("output_tokens"),
                               # TE.6 parity: cached reads feed the cache-efficiency ledger
                               "cache_read_tokens": usage.get("cached_input_tokens"),
                               "model": self.model,
                               "cost_usd": None,
                               "duration_s": time.monotonic() - started,
                               "ctx_used": usage.get("input_tokens"),
                               "ctx_window": None}
        reply = parsed["text"]
        if reply and parsed.get("errors"):
            reply += "\n" + "\n".join(parsed["errors"])
        return reply or stderr_text


RUN_HARNESS_TOOL = {
    "type": "function",
    "function": {
        "name": "run_harness",
        "description": "Run a scripts/harness.py subcommand in the repo and return its output.",
        "parameters": {
            "type": "object",
            "properties": {"args": {
                "type": "string",
                "description": "Subcommand and arguments only, e.g. 'status' or 'workflow status <id>'. No 'python scripts/harness.py' prefix.",
            }},
            "required": ["args"],
        },
    },
}


class OpenAIEngine:
    """Tool-calling loop against any OpenAI-compatible /chat/completions."""

    def __init__(self, root: Path, model: str | None, endpoint: str | None, api_key_env: str,
                 prompt_rel: str = PROMPT_REL) -> None:
        self.endpoint = (endpoint or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
        if not self.endpoint:
            raise HarnessError("no endpoint for --engine openai\nfix: pass --endpoint <base-url> or set OPENAI_BASE_URL (e.g. http://localhost:11434/v1)")
        if not model:
            raise HarnessError("no model for --engine openai\nfix: pass --model <name> (the model id your endpoint serves)")
        self.root = root
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")  # optional for local servers
        self.name = f"openai:{self.endpoint}"
        self.chat_mode = "auto"
        self.last_usage: dict[str, Any] | None = None
        self.say: Any = print  # HUD-aware print, injected by run_chat
        self.pause_cm: Any = contextlib.nullcontext  # HUD pause for nested prompts
        try:
            from . import chat_setup
        except ImportError:
            from harness_lib import chat_setup
        self.ctx_window = next((c.get("contextWindow") for c in chat_setup._model_cards(root, "openai")
                                if c.get("id") == model), None)
        system = (root / prompt_rel).read_text(encoding="utf-8")
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def _chat(self) -> dict[str, Any]:
        body = json.dumps({"model": self.model, "messages": self.messages,
                           "tools": [RUN_HARNESS_TOOL]}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.endpoint}/chat/completions", data=body, headers=headers)
        timeout = float(os.environ.get("GRAPHIFY_API_TIMEOUT", "120"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _run_tool_call(self, raw_args: str) -> str:
        try:
            argv = shlex.split(json.loads(raw_args).get("args", ""))
        except (json.JSONDecodeError, ValueError) as exc:
            return f"error: malformed tool arguments ({exc})"
        if not argv:
            return "error: empty command"
        category = classify_command(argv)
        gate = gate_for(category, self.chat_mode)
        shown = f"{_project_python(self.root)} scripts/harness.py {' '.join(argv)}"
        if gate == "human-only":
            return ("blocked: this command carries a human-in-the-loop flag "
                    "(--approval-token/--send). Ask the human to run it themselves "
                    f"with: !{' '.join(argv)}")
        if gate == "blocked-plan":
            return "blocked: plan mode — propose this command instead of running it"
        if gate == "confirm":
            with self.pause_cm():
                self.say(f"\n  agent wants to run: {shown}")
                try:
                    allowed = prompt_kit.confirm("  run it?", default=False)
                except KeyboardInterrupt:
                    allowed = False
            if not allowed:
                return "user declined to run this command"
        elif category == "confirm":  # gate == "run" via accept-edits
            self.say(f"  [auto-approved] harness.py {' '.join(argv)}")
        self.say(f"  [run] harness.py {' '.join(argv)}")
        return run_harness_command(self.root, argv)

    def send(self, msg: str) -> str:
        self.messages.append({"role": "user", "content": msg})
        started = time.monotonic()
        in_sum = out_sum = 0
        ctx_used = None
        for _ in range(MAX_TOOL_ROUNDS):
            data = self._chat()
            usage = data.get("usage") or {}
            in_sum += int(usage.get("prompt_tokens") or 0)
            out_sum += int(usage.get("completion_tokens") or 0)
            ctx_used = usage.get("prompt_tokens") or ctx_used  # last round ≈ context size
            reply = data["choices"][0]["message"]
            calls = reply.get("tool_calls") or []
            self.messages.append({"role": "assistant",
                                  "content": reply.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
            if not calls:
                self.last_usage = {"in_tokens": in_sum or None, "out_tokens": out_sum or None,
                                   "cost_usd": None, "duration_s": time.monotonic() - started,
                                   "ctx_used": ctx_used, "ctx_window": self.ctx_window,
                                   "model": self.model}
                return str(reply.get("content") or "").strip()
            for call in calls:
                out = self._run_tool_call(call.get("function", {}).get("arguments", "{}"))
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                      "content": out})
        return "(stopped: too many consecutive tool calls)"


def _construct(root: Path, engine: str, model: str | None, effort: str | None,
               endpoint: str | None, api_key_env: str,
               prompt_rel: str = PROMPT_REL, diet: dict[str, Any] | None = None) -> Any:
    if engine == "manual":
        return None
    if engine == "claude":
        return ClaudeEngine(root, model, effort, prompt_rel=prompt_rel, diet=diet)
    if engine == "codex":
        # M3 transport (2026-07-17): HARNESS_CODEX_TRANSPORT=app-server opts into
        # the JSON-RPC app-server engine (native diffs, turn diff, output deltas,
        # ctx window); any construction failure falls back to exec --json HERE so
        # the SPEC-115 hop chain is untouched. Default stays exec.
        if os.environ.get("HARNESS_CODEX_TRANSPORT") == "app-server":
            try:
                try:
                    from . import codex_appserver
                except ImportError:
                    from harness_lib import codex_appserver
                return codex_appserver.CodexAppServerEngine(
                    root, model, effort, prompt_rel=prompt_rel, diet=diet)
            except HarnessError as exc:
                print(f"[codex] app-server transport unavailable ({exc}); using exec --json",
                      file=sys.stderr)
        return CodexEngine(root, model, effort, prompt_rel=prompt_rel, diet=diet)
    # OpenAIEngine is already minimal (our prompt + ONE run_harness tool): the
    # diet is structurally a no-op there, so it takes no diet param.
    return OpenAIEngine(root, model, endpoint, api_key_env, prompt_rel=prompt_rel)


# SPEC-170 v5 (rule 19): where a compose-marked role's spawn prompt file is
# rendered. Runtime artifact, regenerated per build_engine call, gitignored.
SPAWN_PROMPT_DIR = ".harness/runs/spawn-prompts"


def compose_spawn_prompt(root: Path, role: str, prompt_rel: str) -> str:
    """The spawn prompt path for `role`: for a compose-marked role (registry
    `inject: "compose"`, SPEC-170 rule 18) the role prompt + the rendered chain
    (chainHash-stamped) written under SPAWN_PROMPT_DIR; every other case —
    list mode, unregistered role, absent/broken registry — returns `prompt_rel`
    unchanged (rule 7 additive landing). A compose failure must never block
    chat construction. Snapshot semantics: content is fixed at build time
    (rule 20); the stamped chainHash is the staleness audit trail.
    # ponytail: ONE file per role, shared by concurrent sessions. Claude re-reads
    # the flag each turn, so a second session booting the same role refreshes a
    # live session's chain mid-flight — benign (the rewrite is deterministic from
    # the same on-disk chain, and newer beats staler). Go per-session only if an
    # incident ever needs a frozen chain."""
    try:
        from . import playbook_registry
    except ImportError:
        from harness_lib import playbook_registry
    try:
        registry = playbook_registry.load(root)
        if playbook_registry.inject_mode(role, registry) != "compose":
            return prompt_rel
        payload = ((root / prompt_rel).read_text(encoding="utf-8").rstrip()
                   + "\n\n" + playbook_registry.spawn_compose(role, registry, root) + "\n")
        out = root / SPAWN_PROMPT_DIR / f"{role}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8", newline="\n")
        return f"{SPAWN_PROMPT_DIR}/{role}.md"
    except Exception as exc:
        # SPEC-173 rule 16: the fail-open stays (chat construction must never
        # block), but a budget REFUSAL cannot be swallowed silently — a diagnostic
        # nobody sees is the failure mode the rule exists to prevent.
        if "spawn refused" in str(exc):
            print(f"!! {str(exc).splitlines()[0]}", file=sys.stderr)
        return prompt_rel


def build_engine(root: Path, engine: str, model: str | None, effort: str | None,
                 endpoint: str | None, api_key_env: str, notify: Any = None,
                 target: str | None = None, role: str | None = None) -> Any:
    """Build the chat engine. On a CONSTRUCTION failure (binary/key missing,
    HarnessError) walk the `chat` role's fallback chain (SPEC-115), emitting one
    notice per hop; raise only when the chain is exhausted. The chain is empty
    under canonical, so this is a single attempt = pre-SPEC-115 behavior.
    `target` (SPEC-114 R36) resolves the overseer chain from a perTarget binding.
    `role` (SPEC-144) picks the fallback chain AND the system prompt: the
    front-desk `router` role converses through front-desk.md, others keep the
    operator contract. The role's `contextDiet` (SPEC-118 v5) rides into every
    hop; each engine translates it via context_diet (no-op vendors ignore it)."""
    notify = notify or (lambda m: print(m, file=sys.stderr))
    prompt_rel = PROMPT_BY_ROLE.get(role or "", PROMPT_REL)
    if role:  # SPEC-170 v5: compose-marked roles get the rendered chain inline
        prompt_rel = compose_spawn_prompt(root, role, prompt_rel)
    diet: dict[str, Any] | None = None
    if role:
        try:
            diet = model_routing.resolve_role(root, role, target=target).get("contextDiet")
        except Exception:
            diet = None  # a broken routing file must never block chat construction
    hops = [(engine, model, effort), *model_routing.chat_fallbacks(root, target, role=role)]
    last: HarnessError | None = None
    for i, (eng, mdl, eff) in enumerate(hops):
        try:
            built = _construct(root, eng, mdl, eff, endpoint, api_key_env,
                               prompt_rel=prompt_rel, diet=diet)
            if built is not None:  # SPEC-146: room write-capability ([] off-room /
                # for non-room roles). ClaudeEngine splices the patterns into its
                # allowlist; CodexEngine maps their PRESENCE to the directory
                # sandbox (role-aware: ui-overseer → read-only); openai ignores both.
                built.room_tools = room_tool_patterns(root, role or "")
                built.room_role = role or ""
            return built
        except HarnessError as exc:
            last = exc
            nxt = hops[i + 1] if i + 1 < len(hops) else None
            if nxt:
                notify(f"[routing] {eng} unavailable ({str(exc).splitlines()[0]}); "
                       f"falling back to {nxt[0]}·{nxt[1]}")
    raise last if last else HarnessError(f"could not build engine {engine!r}")


# -- SPEC-166: mid-run failover — the chain completes the loop at TURN time ----

EXECUTORS_REL = ".harness/routing/executors.json"
# ponytail: fixed digest window; make it configurable only if a real transcript
# usefully overflows it (SPEC-166 ceiling).
DIGEST_TURNS = 6
DIGEST_CHARS = 4000
_FAILURE_CLASSES = (("rate-limit", "rateLimitPatterns"), ("auth", "authFailurePatterns"),
                    ("quota", "quotaFailurePatterns"))
# SPEC-166 R1 shape guard: only engine-error-shaped replies are classifiable — a
# healthy assistant reply that merely DISCUSSES rate limits must never trigger the
# walk. Timeouts match the shape but no runtimeLimits pattern, so they stay inert.
_ERROR_REPLY = re.compile(r"^\[(?:claude|codex) (?:exited \d+|spawn failed|turn timed out)")


def failure_text(reply: str) -> str | None:
    """The classifiable portion of a turn reply: engine-error-shaped only."""
    r = (reply or "").lstrip()
    return r if _ERROR_REPLY.match(r) else None


def _engine_failure_patterns(root: Path, engine: str) -> dict[str, list[str]]:
    """runtimeLimits patterns for the executors this chat engine may be running on —
    ONE shared table (SPEC-166 R1; SPEC-165 classification precedent). claude/codex
    map to their own executor; the generic `openai` chat engine can be pointed at any
    http executor, so it unions the http family's patterns."""
    try:
        executors = (json.loads((Path(root) / EXECUTORS_REL).read_text(encoding="utf-8"))
                     .get("executors") or {})
    except (OSError, json.JSONDecodeError):
        return {}
    picked = ([executors[engine]] if engine in executors else
              [e for e in executors.values() if isinstance(e, dict) and e.get("type") == "http"])
    out: dict[str, list[str]] = {}
    for cls, key in _FAILURE_CLASSES:
        pats = [str(p).lower() for e in picked
                for p in (e.get("runtimeLimits") or {}).get(key) or []]
        if pats:
            out[cls] = pats
    return out


def classify_turn_failure(root: Path, engine: str, text: str) -> str | None:
    """'rate-limit' | 'auth' | 'quota' | None — lowercase substring match of the
    engine's executor runtimeLimits patterns against the turn's failure text
    (an engine-error-shaped reply or an exception string). Unclassified failures
    keep today's error surface (R1)."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return None
    for cls, needles in _engine_failure_patterns(root, engine).items():
        if any(n in low for n in needles):
            return cls
    return None


def _chat_engine_for(executor: str) -> str:
    """Chain hops name EXECUTORS; the chat can only construct claude/codex/openai —
    every http executor rides the OpenAI-wire engine (build_engine parity: the
    construction walk already resolves hops this way via _construct's else-branch)."""
    return executor if executor in ("claude", "codex") else "openai"


def midrun_hops(root: Path, role: str | None, target: str | None,
                engine: str, model: str | None) -> list[tuple[str, str, str | None]]:
    """(executor, card, effort) hops strictly AFTER the current (engine, card) in the
    role's chain. Canonical, or current hop absent from the chain, -> [] (R2, R9)."""
    try:
        res = model_routing.resolve_role(root, role or model_routing.OVERSEER, target=target)
    except Exception:
        return []  # a broken routing file must never break a live conversation
    if res.get("profile") == model_routing.CANONICAL:
        return []
    chain = [(c.get("executor") or model_routing.DEFAULT_EXECUTOR, c["card"], c.get("effort"))
             for c in [res.get("primary"), *(res.get("fallbacks") or [])]
             if isinstance(c, dict) and c.get("card")]
    for i, (hop_exec, card, _eff) in enumerate(chain):
        if _chat_engine_for(hop_exec) == engine and card == model:
            return chain[i + 1:]
    return []


def turn_digest(recent) -> str:
    """Bounded plain preamble from [(user, reply)] pairs for a cross-vendor rebuild
    (R4). Message content only — never a new contract surface."""
    if not recent:
        return ""
    parts: list[str] = []
    for user, reply in list(recent)[-DIGEST_TURNS:]:
        parts.append(f"owner: {user}".strip())
        parts.append(f"you: {reply}".strip())
    body = "\n".join(parts)[-DIGEST_CHARS:]
    return ("[mid-run engine failover — bounded digest of the recent conversation; "
            "continue it seamlessly]\n" + body + "\n[end digest]\n\n")


def midrun_failover(root: Path, engine: str, model: str | None, effort: str | None,
                    endpoint: str | None, api_key_env: str, *, role: str | None = None,
                    target: str | None = None, current_agent: Any = None,
                    failed_msg: str = "", digest: str = "", notify: Any = None,
                    construct: Any = None):
    """SPEC-166 R2-R7: walk the hops after the current one. Per hop: rebuild (a
    same-vendor hop copies the native session/thread id — R3 lossless; a
    cross-vendor hop prepends the bounded digest to the retried message — R4),
    re-splice room capability + diet (R7), retry the failed turn ONCE (R5; a
    classified failure or transport error on the retry advances the walk).
    Returns (agent, (executor, card, effort), reply) on survival, None on
    exhaustion (R6 — the caller keeps today's error surface, engine unchanged)."""
    notify = notify or (lambda m: print(m, file=sys.stderr))
    construct = construct or _construct
    prompt_rel = PROMPT_BY_ROLE.get(role or "", PROMPT_REL)
    diet: dict[str, Any] | None = None
    if role:
        try:
            diet = model_routing.resolve_role(root, role, target=target).get("contextDiet")
        except Exception:
            diet = None
    for hop_exec, card, eff in midrun_hops(root, role, target, engine, model):
        hop_engine = _chat_engine_for(hop_exec)
        try:
            built = construct(root, hop_engine, card, eff, endpoint, api_key_env,
                              prompt_rel=prompt_rel, diet=diet)
        except HarnessError as exc:
            notify(f"[routing] hop {hop_exec}·{card} unavailable "
                   f"({str(exc).splitlines()[0]}); walking on")
            continue
        if built is None:
            continue
        built.room_tools = getattr(current_agent, "room_tools", []) or []
        built.room_role = getattr(current_agent, "room_role", "") or ""
        same_vendor = hop_engine == engine
        if same_vendor:  # R3: the vendor store survives a model change within the vendor
            for attr in ("session_id", "thread_id"):
                val = getattr(current_agent, attr, None)
                if val and hasattr(built, attr):
                    setattr(built, attr, val)
        msg = failed_msg if same_vendor else (digest + failed_msg)
        notify(f"[routing] {engine}·{model} failed mid-run; continuing on {hop_exec}·{card}")
        try:
            reply = built.send(msg)  # R5: exactly one retry per hop
        except (urllib.error.URLError, OSError) as exc:
            notify(f"[routing] hop {hop_exec}·{card} errored on the retry ({exc}); walking on")
            continue
        failed = failure_text(reply)
        if failed and classify_turn_failure(root, hop_engine, failed):
            notify(f"[routing] hop {hop_exec}·{card} also limited; walking on")
            continue
        return built, (hop_exec, card, eff), reply
    return None


def _self_check() -> int:
    # session_role_env: a role-bearing child hydrates with ITS chain; a role-less
    # build adds nothing (the bare vendor CLI keeps the hook's overseer default).
    assert session_role_env("router") == {"HARNESS_SESSION_ROLE": "router",
                                          "HARNESS_SESSION_MODEL": "",
                                          "HARNESS_SESSION_EFFORT": ""}
    assert session_role_env("route-overseer", "opus", "high") == {
        "HARNESS_SESSION_ROLE": "route-overseer", "HARNESS_SESSION_MODEL": "opus",
        "HARNESS_SESSION_EFFORT": "high"}
    # the seat must never be INHERITED: a child handed a role but no model CLEARS the
    # parent's, so the downstream seat degrades to unknown instead of lying complete
    assert session_role_env("router")["HARNESS_SESSION_MODEL"] == ""
    assert session_role_env("") == {}
    # the `configured-by-executor` placeholder is unknown, not a real seat value:
    # it must clear like None/"" so the seat degrades to unknown, never fake-complete
    _cbe = session_role_env("router", model_routing.CONFIGURED, model_routing.CONFIGURED)
    assert _cbe["HARNESS_SESSION_MODEL"] == "" and _cbe["HARNESS_SESSION_EFFORT"] == "", _cbe
    # ...and the claude spawn seam actually splices it: intercept Popen, capture
    # the env send() builds, let the OSError path return (no vendor CLI spawned).
    captured: dict[str, str] = {}
    captured_cmd: list[str] = []

    def _fake_popen(cmd, **kw):
        captured_cmd[:] = list(cmd)
        captured.update(kw.get("env") or {})
        raise OSError("self-check intercept")

    eng = ClaudeEngine.__new__(ClaudeEngine)
    eng.root = Path(__file__).resolve().parents[2]
    eng.binary, eng.model, eng.effort = "claude", None, None
    eng.session_id, eng.chat_mode, eng.room_role = None, "auto", "router"
    orig_popen = subprocess.Popen
    subprocess.Popen = _fake_popen  # type: ignore[assignment]
    try:
        reply = eng.send("hi")
        # accept-edits (and ONLY accept-edits) pre-approves the harness verb.
        auto_cmd = list(captured_cmd)
        eng.chat_mode = "accept-edits"
        eng.send("hi")
    finally:
        subprocess.Popen = orig_popen  # type: ignore[assignment]
    assert reply.startswith("[claude spawn failed]"), reply
    assert captured.get("HARNESS_SESSION_ROLE") == "router", captured
    pre_approved = f"Bash({_project_python(eng.root)} scripts/harness.py *)"
    assert pre_approved not in auto_cmd, "auto mode must not pre-approve the harness verb"
    assert pre_approved in captured_cmd, "accept-edits must pre-approve the harness verb"
    print("chat_engines self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
