"""Conversational harness-operator REPL (`harness.py chat`).

The user delegates harness control to an agent engine — Claude Code, Codex, or
any OpenAI-compatible endpoint (local models included). SUPERVISION_UI_IDEATION
§1 premise: the controller acts ONLY through scripts/harness.py subcommands.

Fragmented per QA.3 (self-lib-file-budget): engines + command gating live in
chat_engines.py, onboarding/prefs/model-cards/picker in chat_setup.py, the HUD
in chat_hud.py. This module owns the REPL loop and rich input (paste, @file
mentions, fuzzy finder). Key names are re-exported so callers and the
__main__ self-check keep a single import surface.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
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
    from . import chat_engines, chat_hud, cost_metrics, intake_triage, md_ansi, prompt_kit
    from . import targets as targets_lib
    from .chat_engines import (MODES, MODE_TAGS, ROOM_ROLES, TURN_TIMEOUT, ClaudeEngine,
                               CodexEngine, OpenAIEngine, build_engine, classify_command,
                               gate_for, gate_processes, run_harness_command)
    from .chat_setup import (CARDS_REL, CUSTOM, ENGINE_DEFAULT, ENGINES, PREFS_REL,
                             _checklist_rows, _engine_options, _load_prefs, _model_cards,
                             _model_options, _pick_project, _post_plan_mode, _project_candidates,
                             _project_checklist, _register_target, _resolve_config,
                             _save_prefs, _select_effort, _select_model, _wizard,
                             _wizard_openai)
    from .common import HarnessError, read_json, truncate_text
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import (chat_engines, chat_hud, cost_metrics, intake_triage,
                             md_ansi, prompt_kit)
    from harness_lib import targets as targets_lib
    from harness_lib.chat_engines import (MODES, MODE_TAGS, ROOM_ROLES, TURN_TIMEOUT,
                                          ClaudeEngine, CodexEngine, OpenAIEngine, build_engine,
                                          classify_command, gate_for, gate_processes,
                                          run_harness_command)
    from harness_lib.chat_setup import (CARDS_REL, CUSTOM, ENGINE_DEFAULT, ENGINES, PREFS_REL,
                                        _checklist_rows, _engine_options, _load_prefs,
                                        _model_cards, _model_options, _pick_project,
                                        _post_plan_mode, _project_candidates, _project_checklist,
                                        _register_target, _resolve_config, _save_prefs,
                                        _select_effort, _select_model, _wizard, _wizard_openai)
    from harness_lib.common import HarnessError, read_json, truncate_text

# SPEC-146 chat rooms: the porteiro classifies a change demand with `route --task`
# and the chat HANDS OFF to a write-capable overseer ROOM in the SAME window.
# ROOM_BY_PROFILE maps the routed workflow profile -> room role; anything routed
# (non-inline, non-escalated) that is not mapped enters the DEFAULT_ROOM (the fable
# route-overseer). ui-delivery is the one profile with a cheap Opus-xhigh UI room.
ROOM_BY_PROFILE = {"ui-delivery": "ui-overseer"}
DEFAULT_ROOM = "route-overseer"


def route_handoff(name: str, arg: str, digest: str) -> dict[str, Any] | None:
    """Pure: did a porteiro `route --task` (NOT `--dispatch`) tool call classify a
    change demand into a real route? -> {room, brief}; an ESCALATED route ->
    {withheld: True} (the chat announces the withhold — porteiro incident
    2026-07-17: silent suppression let the model claim the route went through);
    else None. `cmd_route` prints the decision JSON first, so the
    (2000-char-capped) tool-result digest raw_decodes to the decision. Inline /
    no-route -> None (the porteiro relays those itself). --dispatch means the
    owner asked for the detached AFK path, not a room; a non-shell tool is never
    a route call (Bash, or PowerShell on Windows claude sessions)."""
    # porteiro incident #3 2026-07-17: on Windows the claude engine's shell tool is
    # PowerShell, not Bash — the Bash-only check silently dropped every handoff.
    if name not in ("Bash", "PowerShell") or "route --task" not in arg or "--dispatch" in arg:
        return None
    # codex parity: aggregated_output may carry shell-wrapper noise before the
    # decision JSON — scan to the first '{' (claude digests start there already).
    payload = digest[digest.find("{"):] if "{" in digest else digest
    try:
        decision, _ = json.JSONDecoder().raw_decode(payload.strip())
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decision, dict):
        return None
    if decision.get("escalate"):
        return {"withheld": True}
    if decision.get("route") in (None, "inline"):
        return None
    room = ROOM_BY_PROFILE.get(decision.get("workflowProfile"), DEFAULT_ROOM)
    return {"room": room, "brief": digest}


def _room_state(agent: Any) -> str:
    """SPEC-146 chat-session-scoped typed lifecycle state for a stashed room engine.
    Per-turn engines (claude / codex-exec / openai) respawn their child each
    send(), so a stashed object is always usable -> 'live'. An engine that keeps a
    PERSISTENT child (codex app-server, `self.proc`) reads 'stale' once that child
    dies. ponytail: only live/stale are honestly detectable in-process — /back
    stashes a room identically to a switch, leaving no marker, so a distinct
    'left' state is not distinguishable today without new per-room persistence."""
    proc = getattr(agent, "proc", None)
    if proc is None:
        return "live"
    try:
        from .processes import pid_alive
    except ImportError:  # run directly for the __main__ self-check
        from harness_lib.processes import pid_alive
    try:
        return "live" if pid_alive(getattr(proc, "pid", None)) else "stale"
    except Exception:
        return "stale"


def _room_label(rooms: dict[str, Any], name: str) -> str:
    """`ui-overseer(live)` / `sec-triage(stale)` — a room name tagged with its
    detected lifecycle state for the human listing and the engine-facing tag."""
    entry = rooms.get(name)
    agent = entry[0] if isinstance(entry, tuple) else entry
    return f"{name}({_room_state(agent)})"


HELP_TEXT = """commands:
  /help    — this list
  /config  — re-run the setup wizard (engine, model, endpoint) and the local-setup checklist
  /engine  — switch engine only (model/effort/endpoint reset to defaults)
  /mode    — show or set the interaction mode: manual / plan / auto / accept-edits (Shift+Tab cycles)
  /approve — approve the current plan: switch to your postPlanMode and send the approval message (default "approved, proceed")
  /room    — list live rooms; /room <name> enters an overseer room (route-overseer / ui-overseer)
  /back    — leave the current overseer room and return to the porteiro (front desk)
  /repo    — fuzzy-pick the project to work on (registered targets or browse+register); /repo <name> sets it, /repo . clears back to this repo
  /paste   — paste a large block; finish with a line containing only /end
  /tool    — list this turn's tool calls; /tool <n> inspects one (input+output)
  /exit    — quit (also /quit)
  !<args>  — run `harness.py <args>` yourself; the output is shared with the agent
  @<file>  — mention a file in a message; its content is attached (capped)
  @?       — open the fuzzy file finder; unknown @tokens open it seeded with the token
  Esc Esc  — while the agent is thinking: stop the current turn (partial reply kept)
  a line ending in \\ continues on the next line; multi-line pastes are captured as one block;
  dropping a file on the terminal attaches it (the absolute path becomes an @mention)"""


ATTACH_CAP = 64_000  # chars per @file attachment / pasted block (SPEC-111 R19/R20)
# Leading `.` admits panel-upload paths (@.harness/runtime/chat-uploads/x.png);
# `:` admits absolute Windows paths (@C:\dir\file.txt). The (?<=\s) guard keeps
# emails (a@b.com) out, and rstrip below still trims trailing punctuation.
MENTION_RE = re.compile(r"(?:^|(?<=\s))@([\w~.][\w:./\\-]*)")
QUOTED_MENTION_RE = re.compile(r'(?:^|(?<=\s))@"([^"\n]+)"')  # @"path with spaces"


def _stdin_pending() -> bool:
    """More input already buffered on a TTY stdin (i.e., a multi-line paste)?

    Piped stdin never drains (SPEC-111 R15) — draining would eat the script.
    """
    try:
        if not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import msvcrt
            return bool(msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


ESC_WINDOW_S = 2.0  # second Esc must land within this window to stop the turn


# SPEC-137 orphaned-gate vigil (incident 2026-07-17): an engine turn that ends
# while a gate/validate run is alive leaves the gate's completion with no live
# owner — the pass/fail sat silent until the owner's next message. If gates
# survive a turn, one daemon thread polls until they exit, reports the newest
# validation-results.jsonl record in-chat, and queues a note the REPL drains
# into the engine's next turn.
_VIGIL_NOTES: list[str] = []
_VIGIL_LIVE = threading.Event()  # one vigil at a time; re-arms after it settles
_VIGIL_POLL_S = 15
_VIGIL_MAX_POLLS = 240  # 60 min cap — double the worst gate seen


def _gate_verdict(results: Path, before_size: int) -> str:
    """Human line for the newest validation record appended after before_size."""
    try:
        size = results.stat().st_size
    except OSError:
        size = 0  # never written = no record
    if size <= before_size:
        return "gate exited with NO new validation record — check the gate output/log"
    try:
        row = json.loads(results.read_text(encoding="utf-8", errors="replace")
                         .strip().splitlines()[-1])
        counts = row.get("counts") or {}
        return (f"{row.get('gate')}: {row.get('status')} "
                f"(pass {counts.get('pass')}, fail {counts.get('fail')}, skip {counts.get('skip')})")
    except Exception as exc:
        return f"gate settled but the validation record is unreadable ({type(exc).__name__})"


def _watch_orphan_gate(root: Path, say: Any) -> None:
    """Arm the vigil when a gate/validate process outlives the engine turn."""
    if _VIGIL_LIVE.is_set():
        return
    try:
        gates = gate_processes()
    except Exception:
        return  # listing trouble now; the next turn end re-checks
    if not gates:
        return
    results = root / ".harness" / "runs" / "validation-results.jsonl"
    try:
        before = results.stat().st_size
    except OSError:
        before = 0
    say(f"(gate vigil: {len(gates)} gate/validate process(es) outlive this turn — "
        "I'll report here when it settles)")
    _VIGIL_LIVE.set()

    def _vigil() -> None:
        try:
            for _ in range(_VIGIL_MAX_POLLS):
                time.sleep(_VIGIL_POLL_S)
                try:
                    if not gate_processes():
                        break
                except Exception:
                    continue  # hiccup under load: keep the vigil, never die silent
            else:
                msg = (f"gate vigil GAVE UP after {_VIGIL_MAX_POLLS * _VIGIL_POLL_S // 60} min — "
                       "gate processes still alive; check .harness/runs/validation-results.jsonl")
                say("\n" + msg)
                _VIGIL_NOTES.append(f"[{msg}]")
                return
            msg = "gate settled — " + _gate_verdict(results, before)
            say("\n(gate vigil) " + msg)
            _VIGIL_NOTES.append(f"[gate vigil: {msg}]")
        finally:
            _VIGIL_LIVE.clear()

    threading.Thread(target=_vigil, daemon=True).start()


def _esc_watcher(agent: Any) -> threading.Event | None:
    """Esc-Esc during a turn interrupts the agent, Claude-Code style (TTY only —
    the panel bridge stops via HARNESS_CHAT_STOP_FILE instead). Returns the
    Event that ends the watcher, or None when unsupported."""
    if not hasattr(agent, "interrupt"):
        return None
    try:
        if not sys.stdin.isatty() or os.name != "nt":
            # ponytail: Windows-first (msvcrt); a POSIX raw-mode watcher when
            # someone chats from a POSIX TTY and asks for it.
            return None
        import msvcrt
    except Exception:
        return None
    done = threading.Event()

    def _watch() -> None:
        last_esc = 0.0
        while not done.wait(0.05):
            if not msvcrt.kbhit():
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow/function keys arrive as a pair
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if ch != "\x1b":
                continue
            now_t = time.monotonic()
            if now_t - last_esc <= ESC_WINDOW_S:
                print("\n[Esc Esc — stopping the turn]", file=sys.stderr)
                agent.interrupt()
                return
            last_esc = now_t
            print("(Esc again to stop the turn)", file=sys.stderr)

    threading.Thread(target=_watch, daemon=True).start()
    return done


def _drain_paste(first: str) -> str:
    lines = [first]
    while _stdin_pending():
        try:
            lines.append(input())
        except EOFError:
            break
    return "\n".join(lines)


def _read_paste_block() -> str:
    # P5: the panel bridge drains stderr into its transcript, so suppress the hint
    # there (HARNESS_CHAT_EVENTS=1 is the panel signal); TTY + piped CLI unchanged.
    if os.environ.get("HARNESS_CHAT_EVENTS") != "1":
        print("paste mode — finish with a line containing only /end (or Ctrl+Z / Ctrl+D)",
              file=sys.stderr)
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "/end":
            break
        lines.append(line)
    return "\n".join(lines)


def _finish_block(block: str, mode: str) -> str:
    """Echo the capture and offer an optional note to send with the block."""
    block = block.rstrip("\n")
    if not block.strip():
        print("(empty paste discarded)", file=sys.stderr)
        return ""
    if os.environ.get("HARNESS_CHAT_EVENTS") != "1":  # P5: suppressed under the panel bridge
        print(f"[captured {block.count(chr(10)) + 1} pasted lines]", file=sys.stderr)
    if mode != "no-input":
        note = prompt_kit.text("add a note to send with it (Enter = send as-is)", mode=mode)
        if note:
            return f"{note}\n\n{block}"
    return block


def _read_message(mode: str, editor: Any = None, pause: Any = None) -> tuple[str, str]:
    """One REPL entry: ("line", text) for commands/plain lines, ("block", text)
    for pasted or continued multi-line content (SPEC-111 R19).

    editor: HUD-backed raw line reader returning (kind, text); pause: HUD
    pause contextmanager factory for the nested paste/note prompts."""
    pause = pause or contextlib.nullcontext
    if editor is not None:
        kind, first = editor()
        if kind == "block":
            with pause():
                return "block", _finish_block(first, mode)
        stripped = first.strip()
        if stripped == "/paste":
            with pause():
                return "block", _finish_block(_read_paste_block(), mode)
        return "line", stripped
    first = input("\nharness> ")
    stripped = first.strip()
    if stripped == "/paste":
        return "block", _finish_block(_read_paste_block(), mode)
    if stripped.startswith(("/", "!")):
        return "line", stripped  # commands are never drained or expanded
    if _stdin_pending():
        return "block", _finish_block(_drain_paste(first), mode)
    if stripped.endswith("\\"):
        lines = [stripped[:-1].rstrip()]
        while True:
            sys.stderr.write("... ")
            sys.stderr.flush()
            try:
                nxt = input()
            except EOFError:
                break
            if nxt.rstrip().endswith("\\"):
                lines.append(nxt.rstrip()[:-1].rstrip())
                continue
            lines.append(nxt)
            break
        return "block", "\n".join(lines).strip()
    return "line", stripped


def _git_files(repo: Path) -> list[str]:
    try:
        res = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=30)
        if res.returncode == 0:
            return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


def _list_repo_files(root: Path, target_root: Path | None = None) -> list[str]:
    """Candidate paths for the @ fuzzy finder: git ls-files, else a bounded walk.

    With a session target, the target repo's files are appended as
    harness-root-relative paths (../repo/src/x.py) so MENTION_RE and the
    mention resolver work unchanged (SPEC-111 R25)."""
    files = _git_files(root)
    if files and target_root is not None:
        try:
            prefix = Path(os.path.relpath(target_root, root)).as_posix()
            files += [f"{prefix}/{f}" for f in _git_files(target_root)]
        except ValueError:  # different drive: relpath impossible
            print(f"[target files not listed: {target_root} is on another drive]", file=sys.stderr)
    if files:
        return files
    skip = {".git", ".venv", "__pycache__", "node_modules", "graphify-out"}
    out: list[str] = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        out.extend(str((Path(base) / n).relative_to(root)).replace("\\", "/") for n in names)
        if len(out) > 20000:  # ponytail: bound the walk; huge trees use git anyway
            break
    return out


def _mention_tokens(text: str) -> list[str]:
    """Ordered, deduped mention tokens: @token plus @"path with spaces"."""
    out: list[str] = []
    for token in MENTION_RE.findall(text) + QUOTED_MENTION_RE.findall(text):
        token = token.rstrip(".,;:!?")
        if token and token not in out:
            out.append(token)
    return out


def _absorb_dropped_paths(text: str) -> str:
    """Terminal drag-and-drop pastes a (possibly quoted) absolute path with no @.
    Rewrite quoted segments / bare path-like tokens that name an existing file
    into @"…" mentions so the normal attachment flow picks them up. Only
    ABSOLUTE (or ~) paths qualify — a typed relative path like docs/x.md keeps
    meaning prose, never a surprise attachment."""
    candidates: list[tuple[str, str]] = []  # (literal to replace, path)
    for m in re.finditer(r'"([^"\n]+)"|\'([^\'\n]+)\'', text):
        candidates.append((m.group(0), m.group(1) or m.group(2)))
    candidates += [(t, t) for t in text.split()]
    for literal, cand in candidates:
        if not literal or literal.startswith("@") or f"@{literal}" in text:
            continue
        path = Path(os.path.expanduser(cand))
        if path.is_absolute() and path.is_file():
            text = text.replace(literal, f'@"{cand}"', 1)
    return text


def _resolve_mentions(root: Path, text: str, mode: str,
                      target_root: Path | None = None) -> str:
    """Fuzzy-resolve `@?` and unresolved @tokens before sending (SPEC-111 R21)."""
    if mode == "no-input":
        return text
    pending: list[str] = []
    if re.search(r"(?:^|\s)@\?", text):
        pending.append("?")
    for token in MENTION_RE.findall(text):
        clean = token.rstrip(".,;:!?")
        if not clean or clean in pending:
            continue
        path = Path(os.path.expanduser(clean))
        if not (path if path.is_absolute() else root / clean).is_file():
            pending.append(clean)
    if not pending:
        return text
    files = _list_repo_files(root, target_root)
    for token in pending:
        query = "" if token == "?" else token
        pick = prompt_kit.fuzzy_select(f"file for @{'…' if token == '?' else token}",
                                       files, query=query, mode=mode)
        if pick:
            text = text.replace("@?" if token == "?" else f"@{token}", f"@{pick}", 1)
    return text


def _expand_mentions(root: Path, text: str) -> str:
    """Attach @file mentions as [file: ...] blocks (SPEC-111 R20).

    Unresolved tokens stay plain text; text content is capped at ATTACH_CAP per
    file. A binary file (NUL byte) is attached as a path note — the engine reads
    it with its own file tools (images, PDFs, …) instead of inlining garbage.
    """
    attachments: list[str] = []
    for token in _mention_tokens(text):
        path = Path(os.path.expanduser(token))
        if not path.is_absolute():
            path = root / token
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            print(f"[@{token} skipped: {exc}]", file=sys.stderr)
            continue
        if b"\x00" in raw[:8192]:
            attachments.append(f"[file attached (binary, {len(raw)} bytes): {path.resolve()} "
                               "— read it with your file tools]")
            continue
        content, _ = truncate_text(raw.decode("utf-8", errors="replace"), ATTACH_CAP)
        attachments.append(f"[file: {token}]\n{content}\n[end of {token}]")
    if not attachments:
        return text
    return text + "\n\n" + "\n\n".join(attachments)


def _fmt_tokens(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return "n/a"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def _turn_identity(cfg: dict[str, Any], role: str | None,
                   usage: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = str(cfg["engine"][0] or "")
    measured = (usage or {}).get("model")
    if engine == "claude" and isinstance(measured, str) and measured.strip():
        model, source = measured, "measured this turn"
    else:
        model, source = cfg["model"]
    return {"engine": engine, "model": model, "effort": cfg["effort"][0],
            "modelSource": source, "role": role}


def _midrun_rescue(root: Path, cfg: dict[str, Any], agent: Any, role: str | None,
                   target: str | None, api_key_env: str, say: Any, hud: Any,
                   recent_turns, failed_text: str, retry_msg: str):
    """SPEC-166: a classified mid-run failure walks the role's chain. On survival
    the session goes STICKY on the surviving hop (R8): cfg + HUD label updated
    here, the caller rebinds/wires the agent. Returns (agent, reply) or None
    (unclassified, or chain exhausted — R6 keeps today's error surface)."""
    cls = chat_engines.classify_turn_failure(root, cfg["engine"][0], failed_text)
    if not cls:
        return None
    fo = chat_engines.midrun_failover(
        root, cfg["engine"][0], cfg["model"][0], cfg["effort"][0],
        cfg["endpoint"][0], api_key_env, role=role, target=target,
        current_agent=agent, failed_msg=retry_msg,
        digest=chat_engines.turn_digest(recent_turns), notify=say)
    if not fo:
        say(f"[routing] chain exhausted mid-run ({cls}) — /model or /config to recover")
        return None
    new_agent, (hop_exec, card, eff), reply = fo
    eng = chat_engines._chat_engine_for(hop_exec)
    cfg["engine"], cfg["model"], cfg["effort"] = ((eng, "failover"), (card, "failover"),
                                                  (eff, "failover"))
    if hud:
        hud.engine_label = "·".join(str(x) for x in (eng, card, eff) if x)
    return new_agent, reply


def format_turn_summary(usage: dict[str, Any] | None, session: dict[str, Any]) -> str:
    """One honest stderr line per turn (SPEC-111 R24): engine numbers as-is, n/a never guessed."""
    u = usage or {}
    tok = f"{_fmt_tokens(u.get('in_tokens'))}→{_fmt_tokens(u.get('out_tokens'))} tok"
    cost = f"${u['cost_usd']:.3f}" if isinstance(u.get("cost_usd"), (int, float)) else "$n/a"
    dur = f"{u['duration_s']:.1f}s" if isinstance(u.get("duration_s"), (int, float)) else "n/a"
    if isinstance(u.get("ctx_used"), (int, float)) and isinstance(u.get("ctx_window"), (int, float)) and u["ctx_window"]:
        ctx = f"ctx {int(u['ctx_used'] * 100 / u['ctx_window'])}% of {_fmt_tokens(u['ctx_window'])}"
    else:
        ctx = "ctx n/a"
    sess = f"${session['cost_usd']:.2f}" if isinstance(session.get("cost_usd"), (int, float)) else "$n/a"
    return f"[turn: {tok} · {cost} · {dur} · {ctx} · session {sess}/{session.get('turns', 0)} turns]"


def _pending_critical(root: Path, subject: str | None = None) -> int:
    """High/critical self-review findings awaiting a human (SPEC-111 R28).
    One cheap state read — no polling. esc-scoped-hitl-view: with `subject`,
    counts only that world's records (legacy records read as "self"), so a
    session on target A is not nagged about target B's findings."""
    try:
        state = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
        count = 0
        for rec in (state.get("raised") or {}).values():
            if not (isinstance(rec, dict) and rec.get("criticality") in ("high", "critical")):
                continue
            if subject is not None and (rec.get("subject") or "self") != subject:
                continue
            count += 1
        return count
    except Exception:
        return 0


def _banner(agent: Any, cfg: dict[str, tuple[Any, str]], chat_mode: str) -> None:
    if agent is None:
        print("manual mode — every line runs `harness.py <line>` directly; /help for commands")
        return
    parts = [f"engine: {cfg['engine'][0]} ({cfg['engine'][1]})"]
    for field in ("model", "effort", "endpoint"):
        value, source = cfg[field]
        if value:
            parts.append(f"{field}: {value} ({source})")
    parts.append(f"mode: {chat_mode}")
    print(", ".join(parts))
    print("  /help for commands; !<args> runs a command yourself (this is how you approve gated commands)")


def _reresolve_needed(cfg: dict[str, tuple[Any, str]],
                      new_cfg: dict[str, tuple[Any, str]]) -> bool:
    """Fix A (SPEC-114 R37): after a mid-session target switch, rebuild the engine
    only when the re-resolved engine/model/effort actually differs AND the current
    model/effort were not explicitly pinned. `flag`/`session` sources are deliberate
    pins (CLI --model, /config, /engine); a perTarget routing binding must not
    silently override them."""
    pinned = cfg["model"][1] in ("flag", "session") or cfg["effort"][1] in ("flag", "session")
    changed = any(new_cfg[k][0] != cfg[k][0] for k in ("engine", "model", "effort"))
    return changed and not pinned


TOOL_ICONS = {"Read": "📖", "Edit": "✏️", "Write": "✏️", "NotebookEdit": "✏️",
              "Bash": "🖥️", "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐",
              "WebSearch": "🌐", "TodoWrite": "📋"}


def format_tool_line(t: dict[str, Any]) -> str:
    """One collapsed CLI activity line per tool call (chat-tool-chips inc. 3).
    Icon degrades to [tool] when the console encoding rejects emoji (the
    chat_hud._glyphs encode-probe pattern)."""
    icon = TOOL_ICONS.get(str(t.get("name")), "🔧")
    try:
        icon.encode(sys.stdout.encoding or "utf-8")
    except Exception:
        icon = "[tool]"
    arg = f" {t['arg']}" if t.get("arg") else ""
    return f"  {icon} {t.get('name')}{arg}"


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def format_tool_diff(t: dict[str, Any]) -> str:
    """chip-diff-v2 (owner feedback 2026-07-17): the claude-CLI-style body under
    an Edit/Write activity line — `⎿ Added N lines, removed M line` + the
    numbered diff, ANSI red/green on a TTY (plain when piped)."""
    added = int(t.get("diffAdded") or 0)
    removed = int(t.get("diffRemoved") or 0)
    head = f"  ⎿ Added {added} line{_plural(added)}, removed {removed} line{_plural(removed)}"
    tty = sys.stdout.isatty()
    body = []
    for line in str(t.get("diffPretty") or "").splitlines():
        colored = line
        if tty:
            if re.match(r"^\s*\d+ - ", line):
                colored = f"\x1b[31m{line}\x1b[0m"
            elif re.match(r"^\s*\d+ \+ ", line):
                colored = f"\x1b[32m{line}\x1b[0m"
        body.append("    " + colored)
    return head + "\n" + "\n".join(body)


def format_tool_entry(index: int, entry: dict[str, Any]) -> str:
    """Full `/tool <n>` inspector text for one buffered call — capped digests
    only (the caps live at the stream_json producer), never raw stream."""
    fail = "[FAILED] " if entry.get("isError") else ""  # chip-error-color CLI parity
    lines = [f"[{index}] {fail}{entry.get('name')} {entry.get('arg') or ''}".rstrip()]
    if entry.get("input"):
        lines.append("input: " + str(entry["input"]))
    lines.append("output: " + (str(entry["output"]) if entry.get("output")
                               else "(none captured)"))
    return "\n".join(lines)


def _extract_questions(inp: Any) -> list[dict[str, Any]]:
    """Pull {q, options} pairs out of claude's AskUserQuestion tool input
    (shape: {"questions":[{"question":…,"options":[{"label":…},…]}]}) for the
    `question` event (P8). Tolerant: every missing key degrades to [] worst case."""
    out: list[dict[str, Any]] = []
    if not isinstance(inp, dict):
        return out
    for q in inp.get("questions") or []:
        if not isinstance(q, dict):
            continue
        options = [str(o.get("label")) for o in (q.get("options") or [])
                   if isinstance(o, dict) and o.get("label")]
        out.append({"q": str(q.get("question") or ""), "options": options})
    return out


def run_chat(root: Path, engine: str | None = None, model: str | None = None,
             effort: str | None = None, endpoint: str | None = None,
             api_key_env: str = "OPENAI_API_KEY", no_input: bool = False,
             reconfigure: bool = False, reset_prefs: bool = False,
             target: str | None = None, role: str = "router") -> int:
    # SPEC-144 front-desk: chat (CLI and GUI, which pipes to this) opens on the
    # `router` role's model (sonnet/high) as the tier-1 porteiro; --role overseer
    # restores the fable operator (owner 2026-07-21: Opus removed from the
    # overseer role). The role picks model+prompt, never the engine.
    print("harness operator chat")
    mode = prompt_kit.detect_mode(no_input)
    if reset_prefs:
        (root / PREFS_REL).unlink(missing_ok=True)
        print("saved chat preferences removed", file=sys.stderr)
    prefs = _load_prefs(root)
    # SPEC-114 R36: session target hint (flag > saved pref). Hoisted above cfg because
    # the routing rung in _resolve_config and build_engine's fallback walk both resolve
    # a perTarget binding from it — cfg is resolved BEFORE the target block below.
    target_hint = target or (prefs.get("target") if isinstance(prefs.get("target"), str) else None)
    cfg = _resolve_config(root, prefs, engine, model, effort, endpoint, target=target_hint,
                          role=role)
    # session working target (SPEC-111 R25): flag > saved prefs > first-run picker
    target_name: str | None = None
    target_root_path: Path | None = None
    if target:
        target_name = target
        target_root_path = targets_lib.target_root(targets_lib.resolve(target))  # fail-closed
    elif isinstance(prefs.get("target"), str) and prefs.get("target"):
        try:
            entry = targets_lib.resolve(str(prefs["target"]))
            target_name, target_root_path = str(prefs["target"]), targets_lib.target_root(entry)
        except HarnessError as exc:
            print(f"[saved target unavailable: {str(exc).splitlines()[0]} — continuing untargeted]",
                  file=sys.stderr)
    try:
        if mode != "no-input" and (reconfigure or (not prefs and engine is None)):
            # onboarding journey 1: first interactive run (or --reconfigure)
            e, m, ef, ep = _wizard(root, mode, api_key_env)
            cfg = {"engine": (e, "wizard"), "model": (m, "wizard"),
                   "effort": (ef, "wizard"), "endpoint": (ep, "wizard")}
            if target_name is None:
                picked = _pick_project(root, mode)
                if picked:
                    target_name = picked
                    target_root_path = targets_lib.target_root(targets_lib.resolve(picked))
            if prompt_kit.confirm("save these as your defaults?", default=True, mode=mode):
                _save_prefs(root, e, m, ef, ep, target_name)
            _project_checklist(root, mode)
        elif (cfg["engine"][0] == "openai" and mode != "no-input"
              and not (cfg["model"][0] and cfg["endpoint"][0])):
            m, ep = _wizard_openai(root, mode, api_key_env)
            cfg["model"], cfg["endpoint"] = (m, "wizard"), (ep, "wizard")
        elif (mode == "no-input" and cfg["engine"][0] == "openai"
              and not (cfg["model"][0] and cfg["endpoint"][0])):
            # SPEC-111 R2: --no-input can't run _wizard_openai, so fail closed
            # HERE, before build_engine — else the SPEC-115 cross-engine fallback
            # masks the explicitly-requested openai engine's missing value (rc 0).
            missing = "--model" if not cfg["model"][0] else "--endpoint"
            raise HarnessError(f"--engine openai in --no-input needs {missing}")
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    agent = build_engine(root, cfg["engine"][0], cfg["model"][0], cfg["effort"][0],
                         cfg["endpoint"][0], api_key_env, target=target_hint, role=role)
    chat_mode = "manual" if agent is None else "auto"
    if agent is not None:
        agent.chat_mode = chat_mode
    # SPEC-146 chat rooms: `current_role` is the live room (starts on the porteiro);
    # `rooms` stashes (engine, cfg) per role so /back and /room resume WITH context;
    # `pending_handoff` is filled by route_handoff during a porteiro turn; `queued`
    # is the room's auto-fired first turn (the handoff brief). Rooms don't survive a
    # process restart — ceiling noted in the spec.
    rooms: dict[str, tuple[Any, dict[str, tuple[Any, str]]]] = {}
    current_role = role
    pending_handoff: list[dict[str, Any]] = []
    queued: str | None = None
    # Pipe-event protocol (SPEC-114 v2): the browser panel sets HARNESS_CHAT_EVENTS=1
    # and reads \x1e-prefixed JSON lines off stdout to drive busy/telemetry/mode state.
    if os.environ.get("HARNESS_CHAT_EVENTS") == "1":
        # belt-and-braces: force this REPL child's own streams to UTF-8 even if a
        # future launcher forgets PYTHONIOENCODING/PYTHONUTF8 — else "fable·high" etc.
        # write cp1252 and decode to U+FFFD in the bridge transcript.
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except AttributeError:
                pass
    emit = ((lambda d: print("\x1e" + json.dumps(d), flush=True))
            if os.environ.get("HARNESS_CHAT_EVENTS") == "1" else (lambda d: None))
    # verbose chat (SPEC-114 v13): ClaudeEngine fires this per tool_use mid-turn;
    # emit a compact `tool` event so the panel renders a live activity line. Only
    # ClaudeEngine reads on_activity (getattr-guarded) — codex/openai/manual ignore it.
    # chat-tool-chips: per-turn tool buffer feeding the CLI `/tool [n]`
    # inspector — in-memory only, cleared at each turn start, never persisted.
    tool_log: list[dict[str, Any]] = []

    def on_tool(t: dict[str, Any]) -> None:
        tool_log.append({"name": t["name"], "arg": t["arg"], "id": t.get("id") or "",
                         "input": t.get("input_digest") or "", "output": ""})
        evt = {"event": "tool", "name": t["name"], "arg": t["arg"],
               "id": t.get("id") or "", "inputDigest": t.get("input_digest") or ""}
        # chat-chip-render: pass through the derived render fields when present; a tool
        # with none keeps the event byte-identical to before (absent keys omitted).
        for k in ("diff", "code", "language", "diffPretty"):
            if t.get(k):
                evt[k] = t[k]
        for k in ("diffAdded", "diffRemoved"):  # counts: 0 is meaningful, keep it
            if t.get(k) is not None:
                evt[k] = t[k]
        if t.get("diffFull"):  # SPEC-147: panel-only, bridge strips it into the diff ring
            evt["diffFull"] = t["diffFull"]
        emit(evt)
        # CLI visibility (chips increment 3): a collapsed dim activity line in the
        # terminal; the panel bridge path (HARNESS_CHAT_EVENTS=1) renders chips.
        if os.environ.get("HARNESS_CHAT_EVENTS") != "1":
            say(format_tool_line(t))
            if t.get("diffPretty"):  # chip-diff-v2: claude-CLI-style body inline
                say(format_tool_diff(t))
        # P8: an AskUserQuestion tool_use ALSO derives a structured `question`
        # event (the descriptor carries the raw input for this one tool).
        if t.get("name") == "AskUserQuestion":
            emit({"event": "question", "tool": "AskUserQuestion",
                  "questions": _extract_questions(t.get("input")), "source": "tool_use"})

    def on_tool_result(r: dict[str, Any]) -> None:
        evt = {"event": "tool-result", "id": r.get("id") or "",
               "digest": r.get("digest") or ""}
        if r.get("isError"):  # chip-error-color: the panel paints this chip red
            evt["isError"] = True
        if r.get("diff"):  # M1: codex file_change results carry a derived diff
            evt["diff"] = r["diff"]
        if r.get("diffFull"):  # SPEC-147: panel-only, bridge strips it into the diff ring
            evt["diffFull"] = r["diffFull"]
        if r.get("diffPretty"):  # chip-diff-v2: numbered twin + counts (codex parity)
            evt["diffPretty"] = r["diffPretty"]
            evt["diffAdded"] = r.get("diffAdded")
            evt["diffRemoved"] = r.get("diffRemoved")
            if os.environ.get("HARNESS_CHAT_EVENTS") != "1":
                say(format_tool_diff(r))
        emit(evt)
        for entry in reversed(tool_log):  # match the newest call with that id
            if entry["id"] and entry["id"] == r.get("id"):
                entry["output"] = r.get("digest") or ""
                if r.get("isError"):  # chip-error-color: /tool marks it [FAILED]
                    entry["isError"] = True
                # SPEC-146 chat rooms: only a porteiro turn hands off; the completed
                # turn (below) pops pending_handoff, enters the room, queues the brief.
                if current_role == "router":
                    h = route_handoff(entry["name"], entry["arg"], r.get("digest") or "")
                    if h:
                        pending_handoff.append(h)
                break

    # room-chat fix (2026-07-16): mid-turn assistant text used to vanish until the
    # turn's FINAL reply — a room turn narrates for minutes between tool calls and
    # the GUI showed only tool/plan events. Surface each text block as it streams;
    # `turn_texts` lets the end-of-turn say() skip a duplicate of what already showed.
    turn_texts: list[str] = []

    def on_text(text: str) -> None:
        turn_texts.append(text)
        emit({"event": "text", "text": text})
        if os.environ.get("HARNESS_CHAT_EVENTS") != "1":
            say(md_ansi.render(text))

    # M3 app-server extras (additive; only the codex app-server transport fires
    # them today): live command-output chunks into the chip, and the aggregated
    # turn diff. Panel-only — the plain CLI already prints command output inline.
    def on_tool_delta(d: dict[str, Any]) -> None:
        emit({"event": "tool-delta", "id": d.get("id") or "", "chunk": d.get("chunk") or ""})

    def on_turn_diff(diff: str) -> None:
        emit({"event": "turn-diff", "diff": diff})

    # chat-status-line: per-message usage → one `usage` frame; the panel
    # accumulates the turn's tokens for the live plan-HUD/spinner suffix.
    # Panel-only (a plain terminal has the end-of-turn stats line already).
    def on_usage(u: dict[str, Any]) -> None:
        emit({"event": "usage", **u})

    # chat-plan-hud: TodoWrite updates → one `plan-steps` frame (last wins) +
    # the HUD's bottom rows; a plain non-HUD terminal prints only step
    # TRANSITIONS (never the whole list per rewrite).
    plan_state: dict[str, Any] = {"current": None}

    def on_plan(steps: list[dict[str, Any]]) -> None:
        emit({"event": "plan-steps", "steps": steps})
        if hud:
            hud.set_plan(steps)
            return
        if os.environ.get("HARNESS_CHAT_EVENTS") == "1":
            return  # the panel renders the card from the frame
        done = sum(1 for s in steps if s.get("status") == "completed")
        current = next((s.get("text") for s in steps
                        if s.get("status") == "in_progress"), None)
        if current and current != plan_state["current"]:
            plan_state["current"] = current
            say(f"  > plan ({done}/{len(steps)}): {current}")
        elif not current and done == len(steps) and plan_state["current"]:
            plan_state["current"] = None
            say(f"  > plan complete ({done}/{len(steps)})")
    _banner(agent, cfg, chat_mode)
    # svc-autostart-owners: declared autoStart:["chat"] services follow this
    # REPL's lifetime — ensured here, stopped at interpreter exit (atexit) and
    # reaped by the next `services status` on a hard kill.
    try:
        import atexit
        from harness_lib import services as services_lib
        for row in services_lib.ensure_autostart(root, "chat"):
            print(f"service {row.get('name')}: {row.get('status')}", file=sys.stderr)
        atexit.register(services_lib.stop_owned, root)
    except Exception:
        pass  # a dead dependency must not block the chat
    # esc-scoped-hitl-view: the nag counts only THIS session's subject.
    pending_crit = _pending_critical(root, target_name or "self")
    if pending_crit:
        print(f"[!] self-review: {pending_crit} high/critical finding(s) pending "
              f"[{target_name or 'self'}] - !escalations to triage",
              file=sys.stderr)
    session = {"turns": 0, "cost_usd": 0.0}
    # SPEC-166 R4: the harness-side turn window a cross-vendor mid-run failover
    # digests from (the vendor stores aren't portable across vendors).
    recent_turns: deque = deque(maxlen=chat_engines.DIGEST_TURNS)
    hud = None
    if mode == "tui":
        try:
            label = "·".join(str(x) for x in (cfg["engine"][0], cfg["model"][0],
                                              cfg["effort"][0]) if x)
            hud = chat_hud.Hud(root, label, chat_mode)
            if target_name:
                hud.repo = f"{root.name}→{target_name}"
            if not hud.start():
                hud = None
        except Exception:
            hud = None
    say = hud.say if hud else print
    pause = hud.paused if hud else contextlib.nullcontext

    def _wire(a: Any) -> None:
        """Attach the REPL callbacks + current mode to a (re)built engine. One
        definition for boot, the /repo and /config rebuilds, and SPEC-146 room
        switches; harmless on codex/openai (the callbacks are getattr-guarded)."""
        a.chat_mode = chat_mode
        a.say, a.pause_cm = say, pause
        a.on_activity = on_tool
        a.on_tool_result = on_tool_result
        a.on_plan = on_plan
        a.on_text = on_text
        a.on_tool_delta = on_tool_delta
        a.on_turn_diff = on_turn_diff
        a.on_usage = on_usage

    def _switch_room(new_role: str) -> None:
        """Enter room `new_role` in the SAME window (SPEC-146): stash the live
        engine, resume it from `rooms` or build it (empty prefs so the role rung
        always wins — the room is never pinned by a saved model/effort), re-wire the
        callbacks, retag cfg/HUD, and announce. The room inherits the SESSION's
        engine (codex parity 2026-07-17): routing resolves the role's card for that
        executor; claude confines writes by path globs, codex by directory sandbox
        (ui-overseer on codex degrades to read-only — owner decision)."""
        nonlocal agent, cfg, current_role
        if agent is not None:
            rooms[current_role] = (agent, cfg)
        if new_role in rooms:
            agent, cfg = rooms[new_role]
        else:
            cfg = _resolve_config(root, {}, cfg["engine"][0] or "claude", None, None, None,
                                  target=target_hint, role=new_role)
            agent = build_engine(root, cfg["engine"][0], cfg["model"][0], cfg["effort"][0],
                                 cfg["endpoint"][0], api_key_env, notify=say,
                                 target=target_hint, role=new_role)
        _wire(agent)
        current_role = new_role
        if hud:
            hud.engine_label = "·".join(str(x) for x in (
                cfg["engine"][0], cfg["model"][0], cfg["effort"][0]) if x)
        label = f"{cfg['model'][0] or 'vendor default'}·{cfg['effort'][0] or 'vendor default'}"
        if new_role == "router":
            # porteiro incident 2026-07-17: the old hint promised "/room re-enters"
            # but bare /room only listed — say only what bare /room will actually do.
            hint = ("/room re-enters your live room"
                    if len([r for r in rooms if r in ROOM_ROLES]) == 1
                    else "/room <name> re-enters a live room")
            say(f"back at the front desk (porteiro {label}) — {hint}")
        else:
            say(f"entered room {new_role} ({label}) — /back returns to the porteiro")

    if agent is not None:
        _wire(agent)

    def _cycle_mode() -> None:
        nonlocal chat_mode
        if agent is None:
            return  # manual-only until an engine exists (/config)
        chat_mode = MODES[(MODES.index(chat_mode) + 1) % len(MODES)]
        agent.chat_mode = chat_mode
        if hud:
            hud.set_mode(chat_mode)

    editor = None
    if hud:
        editor = lambda: prompt_kit.read_line(  # noqa: E731
            "harness> ", render=hud.render_input, on_shift_tab=_cycle_mode)
    notes: list[str] = []  # human ! runs, fed into the agent's next turn
    try:
        while True:
            # idle marker after every path (turn, command, /mode, error) — clears browser busy
            emit({"event": "ready", "mode": chat_mode, **_turn_identity(cfg, current_role),
                  "endpoint": cfg["endpoint"][0], "target": target_name})
            if queued:  # SPEC-146: a room's auto-fired first turn (the handoff brief)
                kind, line = "line", queued
                queued = None
            else:
                try:
                    kind, line = _read_message(mode, editor=editor, pause=pause)
                except (EOFError, KeyboardInterrupt):
                    say("")
                    return 0
                except Exception:  # editor trouble: fall back to plain input() for good
                    if hud:
                        hud.close()
                        hud, editor = None, None
                        say, pause = print, contextlib.nullcontext
                        if agent is not None:
                            agent.say, agent.pause_cm = say, pause
                        continue
                    raise
            if not line:
                continue
            if kind == "block" and (agent is None or chat_mode == "manual"):
                say("no agent receives blocks in manual mode — /mode auto to talk to the agent")
                continue
            if kind == "line" and (line == "/repo" or line.startswith("/repo ")):
                arg = line[5:].strip()
                if mode == "no-input" and not arg:
                    say("project prompts are disabled (--no-input)")  # only the argless picker needs a prompt
                    continue
                try:
                    with pause():
                        # `/repo .` clears the target (piped-safe "back to this repo"); a name resolves.
                        picked = None if arg == "." else (arg or _pick_project(root, mode))
                    if picked:
                        entry = targets_lib.resolve(picked)
                        target_name, target_root_path = picked, targets_lib.target_root(entry)
                        say(f"target: {target_name} ({target_root_path})")
                        notes.append(f"[the human switched the working target to '{target_name}'; "
                                     f"pass --target {target_name} to target-aware commands]")
                    else:
                        target_name, target_root_path = None, None
                        say(f"target: (this repo) {root.name}")
                    _save_prefs(root, str(cfg["engine"][0]), cfg["model"][0],
                                cfg["effort"][0], cfg["endpoint"][0], target_name)
                    if hud:
                        hud.repo = f"{root.name}→{target_name}" if target_name else root.name
                    # Fix A (SPEC-114 R37): the engine was resolved ONCE at boot; a
                    # mid-session target switch must re-resolve the routing rung so a
                    # perTarget overseer binding takes effect now, not next session.
                    if agent is not None:
                        new_cfg = _resolve_config(root, _load_prefs(root), engine, model,
                                                  effort, endpoint, target=target_name, role=role)
                        if _reresolve_needed(cfg, new_cfg):
                            agent = build_engine(root, new_cfg["engine"][0], new_cfg["model"][0],
                                                 new_cfg["effort"][0], new_cfg["endpoint"][0],
                                                 api_key_env, notify=say, target=target_name,
                                                 role=role)
                            _wire(agent)  # SPEC-146: one callback-rewire path
                            cfg = new_cfg
                            if hud:
                                hud.engine_label = "·".join(str(x) for x in (
                                    cfg["engine"][0], cfg["model"][0], cfg["effort"][0]) if x)
                            notes.append("[the harness re-resolved the engine model for the new "
                                         "target; this is a FRESH vendor session with no memory of "
                                         "the conversation so far]")
                            say(f"engine now: {cfg['engine'][0]} · "
                                f"{cfg['model'][0] or 'vendor default'} · "
                                f"{cfg['effort'][0] or 'vendor default'} ({cfg['model'][1]})")
                except KeyboardInterrupt:
                    say("\n(kept the current target)")
                except HarnessError as exc:
                    say(f"harness error: {exc}")
                continue
            if kind == "line" and (line == "/mode" or line.startswith("/mode ")):
                arg = line[5:].strip()
                if not arg:
                    say(f"mode: {chat_mode} (available: {', '.join(MODES)}; /mode <name> or Shift+Tab)")
                elif arg not in MODES:
                    say(f"unknown mode '{arg}'; available: {', '.join(MODES)}")
                elif arg != "manual" and agent is None:
                    say("no engine configured — /config to pick one first")
                else:
                    chat_mode = arg
                    if agent is not None:
                        agent.chat_mode = chat_mode
                    if hud:
                        hud.set_mode(chat_mode)
                    say(f"mode: {chat_mode}")
                continue
            if kind == "line" and (line == "/approve" or line.startswith("/approve ")):
                # P8: flip to the postPlanMode pref (reuses /mode's machinery) then
                # let the approval message fall through as a normal user turn.
                if agent is None:
                    say("no engine configured — /config to pick one first")
                    continue
                chat_mode = _post_plan_mode(_load_prefs(root))
                agent.chat_mode = chat_mode
                if hud:
                    hud.set_mode(chat_mode)
                say(f"mode: {chat_mode} (approved)")
                line = line[len("/approve"):].strip() or "approved, proceed"
                # no `continue`: `line` is now the approval turn, sent below
            if kind == "line" and line in {"/exit", "/quit"}:
                return 0
            if kind == "line" and line == "/help":
                say(HELP_TEXT)
                continue
            if kind == "line" and line == "/back":  # SPEC-146: leave a room -> porteiro
                if agent is None:
                    say("no engine configured — /config to pick one first")
                elif current_role == "router":
                    say("already at the front desk (porteiro)")
                else:
                    _switch_room("router")
                continue
            if kind == "line" and (line == "/room" or line.startswith("/room ")):
                arg = line[5:].strip()
                if agent is None:
                    say("no engine configured — /config to pick one first")
                elif not arg:
                    live = sorted(r for r in rooms if r in ROOM_ROLES)
                    # porteiro incident 2026-07-17: bare /room at the front desk with
                    # exactly one live room re-enters it (what the /back hint promises);
                    # zero or several rooms -> the listing, as before.
                    if current_role == "router" and len(live) == 1:
                        _switch_room(live[0])
                    else:
                        labeled = [_room_label(rooms, r) for r in live]  # U1: typed state per room
                        say(f"you are in: {current_role}. live rooms: {', '.join(labeled) or 'none yet'}. "
                            f"/room <name> enters ({'/'.join(ROOM_ROLES)}); /back to the porteiro")
                elif arg not in ROOM_ROLES:
                    say(f"unknown room '{arg}'; rooms: {', '.join(ROOM_ROLES)}")
                elif arg == current_role:
                    say(f"already in room {arg}")
                else:
                    _switch_room(arg)
                continue
            if kind == "line" and (line == "/tool" or line.startswith("/tool ")):
                # chat-tool-chips CLI parity: expand-to-inspect in a line REPL is
                # a command — list this turn's calls, or print one call's
                # input+output from the in-memory buffer (nothing persisted).
                arg = line[5:].strip()
                if not tool_log:
                    say("no tool calls captured this turn yet")
                    continue
                if arg.isdigit() and 1 <= int(arg) <= len(tool_log):
                    say(format_tool_entry(int(arg), tool_log[int(arg) - 1]))
                else:
                    for i, entry in enumerate(tool_log, 1):
                        say(f"[{i}] {entry['name']} {entry['arg'] or ''}".rstrip())
                    say("/tool <n> shows a call's input+output")
                continue
            if kind == "line" and line in {"/config", "/engine"}:
                if mode == "no-input":
                    say("configuration prompts are disabled (--no-input)")
                    continue
                try:
                    with pause():
                        if line == "/config":
                            e, m, ef, ep = _wizard(root, mode, api_key_env)
                        else:
                            e = prompt_kit.select("Who drives the harness?", _engine_options(), mode=mode)
                            m = ef = ep = None
                        new_cfg = {"engine": (e, "session"), "model": (m, "session"),
                                   "effort": (ef, "session"), "endpoint": (ep, "session")}
                        new_agent = build_engine(root, e, m, ef, ep, api_key_env, target=target_name,
                                                 role=role)
                        if prompt_kit.confirm("save these as your defaults?", default=False, mode=mode):
                            _save_prefs(root, e, m, ef, ep, target_name)
                        if line == "/config":
                            _project_checklist(root, mode)
                    cfg, agent, notes = new_cfg, new_agent, []
                    # SPEC-146: /config rebuilds the boot-role engine — abandon any
                    # room (stale engines) and land back at the porteiro.
                    rooms.clear()
                    pending_handoff.clear()
                    current_role = role
                    chat_mode = "manual" if agent is None else ("auto" if chat_mode == "manual" else chat_mode)
                    if agent is not None:
                        _wire(agent)
                    if hud:
                        hud.engine_label = "·".join(str(x) for x in (e, m, ef) if x)
                        hud.set_mode(chat_mode)
                    _banner(agent, cfg, chat_mode)
                except KeyboardInterrupt:
                    say("\n(kept the current engine)")
                except HarnessError as exc:
                    say(f"harness error: {exc}")
                continue
            if kind == "line" and line.startswith("/"):
                say("unknown command; /help lists commands")
                continue
            if kind == "line" and (agent is None or chat_mode == "manual" or line.startswith("!")):
                try:
                    argv = shlex.split(line.lstrip("!"))
                except ValueError as exc:
                    say(f"parse error: {exc}")
                    continue
                # esc-scoped-hitl-view: inside a /repo session, a bare
                # `!escalations` defaults to that subject's world (explicit
                # flags win — a typed --target passes through untouched).
                if argv[:1] == ["escalations"] and target_name and "--target" not in argv:
                    argv += ["--target", str(target_name)]
                    say(f"(scoped to --target {target_name}; run with --target self for the harness view)")
                out = run_harness_command(root, argv, human=True)
                say(out)
                if agent is not None:
                    notes.append(f"[the human ran `harness.py {' '.join(argv)}` themselves; output:]\n{out}")
                continue
            if kind == "line":
                # typed lines get the fuzzy finder for unknown @tokens; pasted
                # blocks don't (an @decorator in pasted code is not a mention)
                line = _absorb_dropped_paths(line)  # terminal drag-and-drop → @"path"
                try:
                    with pause():
                        line = _resolve_mentions(root, line, mode, target_root_path)
                except KeyboardInterrupt:
                    say("\n(mention search cancelled — sending as typed)")
            if _VIGIL_NOTES:  # orphaned-gate vigil: hand the settle note to the engine
                notes.extend(_VIGIL_NOTES)
                del _VIGIL_NOTES[:]
            msg = "\n\n".join([*notes, line]) if notes else line
            notes = []
            msg = MODE_TAGS.get(chat_mode, "") + msg  # per-turn mode tag (R23)
            if target_name:  # per-turn target tag (R25)
                msg = (f"[target: {target_name} — pass --target {target_name} to discover, "
                       f"doc-find, graph-build-code-ast, workflow plan]\n\n") + msg
            live_rooms = sorted(r for r in rooms if r in ROOM_ROLES)
            if current_role == "router" and live_rooms:
                # porteiro incident 2026-07-17: rooms live only in this process, so the
                # engine cannot see them and confabulated "no room is open" — tell it.
                # U1: each room carries its typed state (live/stale) so a hung/dead
                # room engine is a named state, not a silent absence.
                labeled = [_room_label(rooms, r) for r in live_rooms]
                msg = (f"[live rooms: {', '.join(labeled)} — the owner re-enters with "
                       "/room <name>; you cannot enter or inspect rooms yourself]\n\n") + msg
            if hud:
                hud.begin_turn()
            tool_log.clear()  # chat-tool-chips: /tool inspects the CURRENT turn
            turn_texts.clear()  # room-chat fix: dedupe window is the CURRENT turn
            emit({"event": "turn-start"})
            if cfg["engine"][0] == "openai":  # SPEC-117 inv.4: openai has no hook system;
                # claude/codex get the triage via their own UserPromptSubmit hook (double injection is a defect)
                _packet = intake_triage.triage_packet(root, line)
                if _packet:
                    msg = _packet + "\n\n" + msg
            esc_done = _esc_watcher(agent)
            expanded = _expand_mentions(root, msg)
            try:
                agent.last_usage = None
                reply = agent.send(expanded)
            except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
                # SPEC-166: a classified transport failure (429/quota/auth — the
                # openai-engine path raises) walks the chain; anything else keeps
                # today's error surface.
                rescue = _midrun_rescue(root, cfg, agent, current_role, target_name,
                                        api_key_env, say, hud, recent_turns,
                                        str(exc), expanded)
                if rescue is None:
                    if hud:
                        hud.end_turn(None, session)
                    emit({"event": "turn-end", "usage": None, "session": dict(session),
                          **_turn_identity(cfg, current_role)})
                    say(f"engine error: {exc}")
                    continue
                agent, reply = rescue
                _wire(agent)
            finally:
                if esc_done is not None:
                    esc_done.set()
            # SPEC-166: claude/codex surface failures as engine-error-shaped REPLIES
            # (never raises) — classify only that shape, walk on a match (R1).
            failed = chat_engines.failure_text(reply)
            if failed:
                rescue = _midrun_rescue(root, cfg, agent, current_role, target_name,
                                        api_key_env, say, hud, recent_turns,
                                        failed, expanded)
                if rescue is not None:
                    agent, reply = rescue
                    _wire(agent)
            recent_turns.append((line, (reply or "")[:1500]))  # SPEC-166 R4 window
            session["turns"] += 1
            usage = getattr(agent, "last_usage", None)
            if usage and isinstance(usage.get("cost_usd"), (int, float)):
                session["cost_usd"] += usage["cost_usd"]
            emit({"event": "turn-end", "usage": usage, "session": dict(session),
                  **_turn_identity(cfg, current_role, usage)})
            # P8: in plan mode the reply IS the plan — derive a stable `plan` event
            # right after turn-end so a future UI can render an approval strip.
            if chat_mode == "plan" and reply:
                emit({"event": "plan", "text": reply, "mode": "plan", "source": "derived"})
            cost_metrics.record_turn(root, engine=str(cfg["engine"][0]), model=cfg["model"][0],
                                     effort=cfg["effort"][0], chat_mode=chat_mode,
                                     target=target_name, usage=usage, reply_chars=len(reply))
            if hud:
                hud.end_turn(usage, session)
            else:
                print(format_turn_summary(usage, session), file=sys.stderr)
            # room-chat fix: the reply may repeat what already streamed via on_text —
            # claude's result echoes the last text block; codex's reply is the
            # "\n\n"-join of every block, sometimes with engine notices appended
            # (live all-codex run 2026-07-17 duplicated every text without this).
            # Print only what was NOT streamed: nothing, the unstreamed tail, or all.
            r = reply.strip()
            seen = ({t.strip() for t in turn_texts}
                    | {"\n".join(turn_texts).strip(), "\n\n".join(turn_texts).strip()})
            streamed_prefix = next((j for j in ("\n\n".join(turn_texts).strip(),
                                                "\n".join(turn_texts).strip())
                                    if j and r.startswith(j)), None)
            if r and r not in seen:
                tail = r[len(streamed_prefix):].strip() if streamed_prefix else reply
                if tail:
                    say(md_ansi.render(tail))  # P6: markdown -> ANSI on a TTY; identity when piped
            _watch_orphan_gate(root, say)  # SPEC-137: a gate outliving the turn gets a vigil
            # SPEC-146 chat rooms: a completed porteiro turn whose `route --task`
            # classified a change demand (route_handoff filled pending_handoff during
            # on_tool_result) enters the overseer's room and queues the brief as that
            # room's auto-fired first turn.
            if current_role == "router" and pending_handoff:
                h = pending_handoff.pop(0)
                pending_handoff.clear()  # one handoff per turn; drop any extras
                if h.get("withheld"):
                    # escalated route: mechanical ground truth for owner AND model —
                    # no room, and nobody gets to narrate the handoff into existing.
                    say("route WITHHELD (risk flag) — no room opened; "
                        "owner decision inbox: harness.py decide")
                else:
                    _switch_room(h["room"])
                    queued = (f"[handoff from porteiro — room {h['room']}]\n{h['brief']}\n\n"
                              "The owner is present in this chat. Follow your complexity ladder.")
    finally:
        if hud:
            hud.close()


if __name__ == "__main__":  # ponytail: minimal self-check for the approval gate
    assert classify_command(["status"]) == "auto"
    assert classify_command(["status", "--html"]) == "confirm"
    assert classify_command(["escalations"]) == "auto"
    assert classify_command(["escalations", "--resolve", "E1"]) == "confirm"
    assert classify_command(["workflow", "status", "wf-1"]) == "auto"
    assert classify_command(["workflow", "execute", "wf-1"]) == "confirm"
    assert classify_command(["workflow", "plan", "--approval-token", "X"]) == "human-only"
    assert classify_command(["workflow", "trace-push", "--send"]) == "human-only"
    assert classify_command(["doc-find", "gates"]) == "auto"

    # porteiro incident #3 2026-07-17: route_handoff must fire for the Windows
    # PowerShell shell tool, not only Bash (a dropped handoff = no room, and the
    # porteiro narrates the handoff into existing).
    _dec = json.dumps({"route": "pre-defined-profile", "delegate": True, "escalate": False,
                       "workflowProfile": "plan"})
    _cmd = "./.venv/Scripts/python.exe scripts/harness.py route --task x"
    assert route_handoff("PowerShell", _cmd, _dec)["room"] == "route-overseer"
    assert route_handoff("Bash", _cmd, _dec)["room"] == "route-overseer"
    assert route_handoff("Read", "notes/route --task dump.md", _dec) is None
    assert route_handoff("Bash", _cmd + " --dispatch", _dec) is None
    assert route_handoff("PowerShell", _cmd, json.dumps(
        {"route": "pre-defined-profile", "delegate": True, "escalate": True})) == {"withheld": True}
    assert route_handoff("PowerShell", _cmd, json.dumps(
        {"route": "pre-defined-profile", "delegate": True, "escalate": False,
         "workflowProfile": "ui-delivery"}))["room"] == "ui-overseer"

    # R24 telemetry formatting: honest n/a, never a guess
    s = {"turns": 3, "cost_usd": 0.19}
    line_full = format_turn_summary({"in_tokens": 12300, "out_tokens": 400, "cost_usd": 0.031,
                                     "duration_s": 12.4, "ctx_used": 30000, "ctx_window": 1000000}, s)
    assert "12.3k→400 tok" in line_full and "$0.031" in line_full and "ctx 3% of 1000.0k" in line_full \
        and "session $0.19/3 turns" in line_full, line_full
    line_empty = format_turn_summary(None, {"turns": 1, "cost_usd": None})
    assert "n/a→n/a tok" in line_empty and "$n/a" in line_empty and "ctx n/a" in line_empty, line_empty
    # claude JSON -> telemetry mapping
    fake = {"usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                      "cache_creation_input_tokens": 25, "output_tokens": 7},
            "total_cost_usd": 0.02, "duration_ms": 1500,
            "modelUsage": {"m": {"inputTokens": 100, "contextWindow": 200000}}}
    tel = ClaudeEngine._telemetry(type("S", (), {"_prev_cost": 0.0})(), fake)
    assert tel == {"in_tokens": 175, "out_tokens": 7, "cache_read_tokens": 50,
                   "cost_usd": 0.02, "duration_s": 1.5,
                   "ctx_used": 175, "ctx_window": 200000, "model": "m"}, tel

    # chip-diff-v2: CLI body under an Edit line — counts header + indented
    # numbered rows (piped run here -> plain, no ANSI).
    ftd = format_tool_diff({"diffPretty": "11 - old\n11 + new\n12   ctx",
                            "diffAdded": 1, "diffRemoved": 1})
    assert ftd.splitlines()[0] == "  ⎿ Added 1 line, removed 1 line", ftd
    assert ftd.splitlines()[1:] == ["    11 - old", "    11 + new", "    12   ctx"], ftd

    # SPEC-137 orphaned-gate vigil: signature matching + settle verdict
    from harness_lib.chat_engines import GATE_PROC_RE
    assert GATE_PROC_RE.search("python scripts/harness.py validate --staged")
    assert GATE_PROC_RE.search(r"C:\x\.venv\Scripts\python.exe scripts\spec_test_gate.py spec-pack")
    assert GATE_PROC_RE.search("python scripts/harness-test.py scenarios")
    assert not GATE_PROC_RE.search("python scripts/harness.py status")
    assert not GATE_PROC_RE.search("python scripts/harness.py chat")
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _vt:
        _res = Path(_vt) / "validation-results.jsonl"
        assert "NO new validation record" in _gate_verdict(_res, 0)  # file never appeared
        _res.write_text(json.dumps({"gate": "spec-pack", "status": "pass",
                                    "counts": {"pass": 640, "fail": 0, "skip": 19}}) + "\n",
                        encoding="utf-8")
        v = _gate_verdict(_res, 0)
        assert v == "spec-pack: pass (pass 640, fail 0, skip 19)", v
        assert "NO new validation record" in _gate_verdict(_res, _res.stat().st_size)

    # R23 gate_for truth table: category x chat_mode
    for m in MODES:
        assert gate_for("human-only", m) == "human-only"
        assert gate_for("auto", m) == "run"
    assert gate_for("confirm", "plan") == "blocked-plan"
    assert gate_for("confirm", "auto") == "confirm"
    assert gate_for("confirm", "accept-edits") == "run"
    assert gate_for("confirm", "manual") == "confirm"

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    # R10: corrupt or alien prefs are treated as absent
    (tmp / PREFS_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp / PREFS_REL).write_text("{not json", encoding="utf-8")
    assert _load_prefs(tmp) == {}
    (tmp / PREFS_REL).write_text('{"schemaVersion": 99}', encoding="utf-8")
    assert _load_prefs(tmp) == {}
    _save_prefs(tmp, "claude", "fable", "high", None)
    prefs = _load_prefs(tmp)
    assert prefs["model"] == "fable"
    # R9 precedence: flag > env > saved prefs > default (no cards file in tmp yet,
    # so the R21 card fall-through is inert here — exercised by m5_ui_panel.py)
    cfg = _resolve_config(tmp, prefs, None, None, None, None)
    assert cfg["engine"] == ("claude", "saved default")
    assert cfg["model"] == ("fable", "saved default")
    assert _resolve_config(tmp, prefs, None, "opus", None, None)["model"] == ("opus", "flag")
    os.environ["OPENAI_BASE_URL"] = "http://x/v1"
    assert _resolve_config(tmp, prefs, None, None, None, None)["endpoint"] == ("http://x/v1", "env")
    assert _resolve_config(tmp, prefs, None, None, None, "http://y/v1")["endpoint"] == ("http://y/v1", "flag")
    del os.environ["OPENAI_BASE_URL"]
    # switching engines by flag drops the saved engine's model/effort/endpoint
    assert _resolve_config(tmp, prefs, "openai", None, None, None)["model"][0] is None
    assert _resolve_config(tmp, {}, None, None, None, None)["engine"] == ("claude", "default")
    # SPEC-144 front-desk: saved prefs still outrank the role rung; a role on a bare
    # root (no routing/cards files) degrades to the vendor default without crashing
    # (the real sonnet/high resolution is rt-9 in rt_route_dispatcher.py)
    assert _resolve_config(tmp, prefs, None, None, None, None, role="router")["model"] == ("fable", "saved default")
    assert _resolve_config(tmp, {}, None, None, None, None, role="router")["model"][0] is None
    # R12: checklist rows in a bare temp root flag the missing venv
    rows = _checklist_rows(tmp)
    assert rows and rows[0][0].startswith("project venv") and rows[0][1] is False
    # R25: prefs round-trip with target; picker candidates shape
    _save_prefs(tmp, "claude", "fable", "high", None, target="my-app")
    assert _load_prefs(tmp)["target"] == "my-app"
    _save_prefs(tmp, "claude", None, None, None)
    assert _load_prefs(tmp)["target"] is None
    cands = _project_candidates(tmp)
    assert cands[0].startswith("(this repo)") and cands[-1].startswith("browse")
    (tmp / ".git").mkdir(exist_ok=True)  # register() requires a git repo (governed target)
    _register_target(tmp, "app1", tmp)  # registers; load_targets is ROOT-bound so just check the file
    reg = json.loads((tmp / ".harness" / "targets" / "app1" / "target.json").read_text(encoding="utf-8"))
    assert reg == {"name": "app1", "path": str(tmp)}
    # R18 model cards: loading, default preselection, live merge, corrupt fallback
    cards_path = tmp / CARDS_REL
    cards_path.parent.mkdir(parents=True, exist_ok=True)
    cards_path.write_text(json.dumps({"schemaVersion": 1, "engines": {"claude": {"models": [
        {"id": "fable", "name": "Fable 5", "provider": "Anthropic",
         "reasoning": ["low", "high"], "default": True},
        {"id": "opus"},
        {"name": "no-id-dropped"}]}}}), encoding="utf-8")
    cards = _model_cards(tmp, "claude")
    assert [c["id"] for c in cards] == ["fable", "opus"]
    opts, di = _model_options(cards, live=("opus", "extra"))
    assert opts[0].value == ENGINE_DEFAULT and opts[-1].value == CUSTOM
    assert [o.value for o in opts[1:-1]] == ["fable", "opus", "extra"]  # dedupe by id
    assert opts[di].value == "fable"  # card default preselected (R17)
    opts2, di2 = _model_options(cards, include_engine_default=False)
    assert opts2[0].value == "fable" and opts2[di2].value == "fable"
    assert _model_cards(tmp, "openai") == []  # engine without cards
    cards_path.write_text("{broken", encoding="utf-8")
    assert _model_cards(tmp, "claude") == []  # corrupt registry degrades (R18)

    # R20 @file mentions
    (tmp / "notes.txt").write_text("hello attachment", encoding="utf-8")
    (tmp / "big.txt").write_text("x" * (ATTACH_CAP + 100), encoding="utf-8")
    (tmp / "blob.bin").write_bytes(b"\x00\x01\x02")
    out = _expand_mentions(tmp, "see @notes.txt please")
    assert "[file: notes.txt]" in out and "hello attachment" in out and out.startswith("see @notes.txt")
    assert "truncated by harness" in _expand_mentions(tmp, "see @big.txt")
    out = _expand_mentions(tmp, "see @blob.bin")  # binary → path note, not inlined
    assert "[file attached (binary, 3 bytes):" in out and str((tmp / "blob.bin").resolve()) in out
    assert _expand_mentions(tmp, "mail a@b.com") == "mail a@b.com"  # emails are not mentions
    assert _expand_mentions(tmp, "no @missing.txt here") == "no @missing.txt here"
    assert "[file: notes.txt]" in _expand_mentions(tmp, "trailing @notes.txt.")  # punctuation trimmed

    # attachments v2: quoted mentions, absolute Windows paths, dropped paths
    (tmp / "com espaço.txt").write_text("spaced", encoding="utf-8")
    out = _expand_mentions(tmp, 'see @"com espaço.txt"')
    assert "[file: com espaço.txt]" in out and "spaced" in out
    abs_notes = str((tmp / "notes.txt").resolve())  # e.g. C:\...\notes.txt — needs the `:`
    assert "hello attachment" in _expand_mentions(tmp, f"see @{abs_notes}")
    dropped = _absorb_dropped_paths(f'olha isso "{abs_notes}"')
    assert dropped == f'olha isso @"{abs_notes}"', dropped
    assert _absorb_dropped_paths(abs_notes) == f'@"{abs_notes}"'  # whole line = bare drop
    assert "hello attachment" in _expand_mentions(tmp, _absorb_dropped_paths(abs_notes))
    # turn-stop plumbing: the flag file (panel ⏹) and interrupt() (Esc Esc) both
    # kill the turn's child within the watcher poll; the flag is consumed.
    import harness_lib.chat_engines as _ce

    class _FakeProc:
        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    def _wait_killed(fp: Any) -> None:
        deadline = time.monotonic() + 3
        while not fp.killed and time.monotonic() < deadline:
            time.sleep(0.05)

    flag = tmp / "stop.flag"
    os.environ["HARNESS_CHAT_STOP_FILE"] = str(flag)
    try:
        # stale flag (⏹ clicked between turns): consumed at turn start, no kill
        flag.write_text("stale", encoding="utf-8")
        ev0, fp0 = threading.Event(), _FakeProc()
        done0 = _ce._watch_interrupt(fp0, ev0)
        assert not flag.exists(), "stale flag must be consumed at watcher start"
        time.sleep(0.6)
        assert not fp0.killed and not ev0.is_set(), "stale flag must not kill the turn"
        done0.set()
        # live flag (⏹ mid-turn): kills the child and marks interrupted
        ev, fp = threading.Event(), _FakeProc()
        done = _ce._watch_interrupt(fp, ev)
        flag.write_text("stop", encoding="utf-8")
        _wait_killed(fp)
        assert fp.killed and ev.is_set() and not flag.exists(), "flag path"
        done.set()
    finally:
        del os.environ["HARNESS_CHAT_STOP_FILE"]
    ev2, fp2 = threading.Event(), _FakeProc()
    done2 = _ce._watch_interrupt(fp2, ev2)
    ev2.set()  # the interrupt() path
    _wait_killed(fp2)
    assert fp2.killed, "interrupt path"
    done2.set()
    assert _esc_watcher(object()) is None  # no interrupt() → unsupported → None

    (tmp / ".up").mkdir()
    (tmp / ".up" / "img.png").write_bytes(b"\x89PNG\x00fake")
    out = _expand_mentions(tmp, "veja @.up/img.png")  # GUI upload shape: leading dot + binary
    assert "[file attached (binary" in out and "img.png" in out, out
    assert _absorb_dropped_paths("veja docs/x.md") == "veja docs/x.md"  # relative = prose
    assert _absorb_dropped_paths(f'já é mention @"{abs_notes}"') == f'já é mention @"{abs_notes}"'

    # R19 paste blocks and the note flow
    import builtins
    real_input = builtins.input
    try:
        feed = iter(["l1", "l2", "/end"])
        builtins.input = lambda prompt="": next(feed)
        assert _read_paste_block() == "l1\nl2"
        assert _finish_block("l1\nl2", "no-input") == "l1\nl2"  # note prompt skipped headless
        assert _finish_block("   \n", "no-input") == ""  # empty paste discarded
        feed = iter(["why does this fail?"])
        builtins.input = lambda prompt="": next(feed)
        assert _finish_block("l1\nl2", "numbered") == "why does this fail?\n\nl1\nl2"
        feed = iter(["/paste", "a", "b", "/end"])
        builtins.input = lambda prompt="": next(feed)
        assert _read_message("no-input") == ("block", "a\nb")
        feed = iter(["/help"])
        builtins.input = lambda prompt="": next(feed)
        assert _read_message("no-input") == ("line", "/help")
        feed = iter(["foo \\", "bar"])
        builtins.input = lambda prompt="": next(feed)
        assert _read_message("no-input") == ("block", "foo\nbar")
        feed = iter(["hi"])
        builtins.input = lambda prompt="": next(feed)
        assert _read_message("no-input") == ("line", "hi")
    finally:
        builtins.input = real_input

    # R21 fuzzy mention resolution (finder stubbed; repo listing exercised live)
    real_fuzzy = prompt_kit.fuzzy_select
    try:
        prompt_kit.fuzzy_select = lambda title, files, query="", mode=None: "scripts/harness.py"
        out = _resolve_mentions(tmp, "veja @harness.pyy e @?", "numbered")
        assert out == "veja @scripts/harness.py e @scripts/harness.py", out
        assert _resolve_mentions(tmp, "veja @qualquer", "no-input") == "veja @qualquer"
        assert _resolve_mentions(tmp, "see @notes.txt", "numbered") == "see @notes.txt"  # resolved: no finder
        prompt_kit.fuzzy_select = lambda *a, **k: None
        assert _resolve_mentions(tmp, "veja @nao-existe", "numbered") == "veja @nao-existe"  # skip keeps text
    finally:
        prompt_kit.fuzzy_select = real_fuzzy
    assert "scripts/harness.py" in _list_repo_files(Path(__file__).resolve().parents[2])

    # SPEC-114 R37 (Fix A): the target-switch re-resolve gate. A change under an
    # unpinned model/effort rebuilds; a flag/session pin is never silently overridden;
    # no change stays silent (the session is not disrupted).
    base = {"engine": ("claude", "default"), "model": ("fable", "card default"),
            "effort": ("high", "card default"), "endpoint": (None, "default")}
    changed_unpinned = dict(base, model=("opus", "routing"), effort=("high", "routing"))
    assert _reresolve_needed(base, changed_unpinned) is True                     # routing rebind → rebuild
    assert _reresolve_needed(base, dict(base)) is False                          # identical → silent
    pinned = dict(base, model=("opus", "flag"))
    assert _reresolve_needed(pinned, changed_unpinned) is False                  # flag pin wins
    pinned_sess = dict(base, effort=("max", "session"))
    assert _reresolve_needed(pinned_sess, changed_unpinned) is False             # /config session pin wins

    # SPEC-114 v3: the ready emit carries engine identity (model/effort/endpoint) for the panel chip
    cfg_probe = {"engine": ("claude", "routing"), "model": ("fable", "routing"),
                 "effort": ("high", "routing"), "endpoint": (None, "default")}
    ready = {"event": "ready", "mode": "manual", **_turn_identity(cfg_probe, "overseer"),
             "endpoint": cfg_probe["endpoint"][0], "target": None}
    assert ready["model"] == "fable" and ready["effort"] == "high", ready
    assert ready["modelSource"] == "routing" and ready["role"] == "overseer", ready
    final_cfg = dict(cfg_probe, model=("opus", "failover"), effort=("max", "failover"))
    final_identity = _turn_identity(final_cfg, "overseer")
    assert final_identity["model"] == "opus" and final_identity["effort"] == "max", final_identity
    assert final_identity["modelSource"] == "failover", final_identity
    claude_measured = _turn_identity(cfg_probe, "overseer", {"model": "claude-sonnet-4-5"})
    assert claude_measured["model"] == "claude-sonnet-4-5", claude_measured
    assert claude_measured["modelSource"] == "measured this turn", claude_measured
    for engine in ("codex", "openai"):
        configured = dict(cfg_probe, engine=(engine, "routing"))
        identity = _turn_identity(configured, "overseer", {"model": "reported-but-not-measured"})
        assert identity["model"] == "fable" and identity["modelSource"] == "routing", identity
    missing = _turn_identity({"engine": ("manual", "default"), "model": (None, "default"),
                              "effort": (None, "default")}, None)
    assert missing["model"] is None and missing["effort"] is None and missing["role"] is None, missing

    # SPEC-114 v2: the emit lambda writes one \x1e-prefixed JSON line when enabled, else nothing
    import io
    os.environ["HARNESS_CHAT_EVENTS"] = "1"
    emit_on = ((lambda d: print("\x1e" + json.dumps(d), flush=True))
               if os.environ.get("HARNESS_CHAT_EVENTS") == "1" else (lambda d: None))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_on({"event": "ready", "mode": "manual"})
    out_line = buf.getvalue()
    assert out_line.startswith("\x1e") and json.loads(out_line[1:])["event"] == "ready", out_line
    del os.environ["HARNESS_CHAT_EVENTS"]
    emit_off = ((lambda d: print("\x1e" + json.dumps(d), flush=True))
                if os.environ.get("HARNESS_CHAT_EVENTS") == "1" else (lambda d: None))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_off({"event": "ready"})
    assert buf.getvalue() == "", buf.getvalue()

    print("chat_operator self-check ok")
