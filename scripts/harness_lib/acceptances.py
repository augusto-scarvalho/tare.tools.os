"""Environment acceptances: what a vendor is SILENTLY skipping until the owner says yes.

Row env-acceptances-inbox. The motivating fact (2026-07-29): codex had been running
this repo's lanes with EVERY one of its 12 hooks inert -- `agent_spawn_economy` (the
cost-incident guard) and `gate_hold_guard` (the write-during-gate guard) among them
-- and the only surface that said so was an interactive TUI prompt the owner never
opened. An acceptance nobody can see is an acceptance nobody gives.

HOW IT ASKS (owner's call 2026-07-29, and it is the `vendor_fuel` pattern): the
vendor's OWN non-interactive interface, never a scraped screen. Here that is the
`hooks/list` request of `codex app-server` -- the same JSON-RPC-over-stdio transport
`codex_appserver.py` already uses for chat, so ZERO new dependencies and no pty.
The vendor answers with a per-hook `trustStatus`:

  trusted   -- runs
  untrusted -- never accepted; does NOT run
  modified  -- accepted ONCE, but the hook changed since; does NOT run
  managed   -- enterprise-managed, not the owner's to accept

`modified` is why this module does not just diff `~/.codex/config.toml`: a first
draft did exactly that and reported 3 pending when the vendor's own answer was 12,
because a stale `trusted_hash` looks identical to a live one from outside (the hash
algorithm is undocumented and was not reproducible -- probed 2026-07-29). Ask the
vendor; do not re-derive what it already knows.

HOW IT GRANTS. The protocol exposes `hooks/list` and NO trust-granting method, so
once the owner says yes there is nowhere to say it but the vendor's own TUI. Two
mechanisms, both ending in the owner's consent (owner's call 2026-07-29):

  visible  -- hand the owner the exact command; they answer the prompt themselves.
  headless -- `drive_headless` types the yes into a hidden pty, one key at a time.

The headless path never DECIDES anything. Consent was given in the panel; here a
deliberately dumb reader (`screen_reader`, which is told nothing about codex or
hooks) describes each screen, `authorize_keystroke` is the only thing that may
permit a key, and any denial, missing pty or unreachable reader ends the drive and
falls back to visible. It is a convenience over the visible flow, never an authority.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from harness_lib import processes, pty_drive, screen_reader
from harness_lib.common import ROOT, gate_holding, now, read_json, write_json

HANDSHAKE_TIMEOUT_S = 30.0
LIST_TIMEOUT_S = 30.0
# What "the vendor will actually run it" means. Anything else is a pending acceptance.
RUNNING = {"trusted", "managed"}
# How many keys may be spent WITHOUT the vendor reporting progress. The drive stops
# the instant `hooks/list` comes back clear, so this is not "how long the job takes"
# — it is the ceiling on answering screens that are NOT the one the owner consented
# to. Kept small on purpose (security audit 2026-07-29, finding 1): the reader is
# deliberately generic, so any confirm-shaped screen looks like the target one.
DRIVE_BUDGET = 4
# Deliberately generic (owner's call): the reader is never told it is looking at
# codex, at hooks, or at anything this repo cares about. It reads a screen.
DRIVE_INTENT = "Select the option that accepts or confirms what the screen is asking."


def _find_codex() -> str | None:
    return shutil.which("codex")


def _rpc_hooks_list(binary: str, root: Path, *, budget_s: float | None = None) -> dict[str, Any]:
    """One short-lived `codex app-server`: initialize -> hooks/list -> close.

    `budget_s` is the TOTAL wall clock, split evenly across the two legs — one knob
    because a caller who cannot wait cannot wait for either leg. The session-start
    surface passes a few seconds; the CLI keeps the generous default.
    """
    # `or` would turn an explicit budget_s=0 back into the 30s default — the exact
    # hang a caller passing 0 is trying to avoid.
    handshake_s, list_s = ((budget_s / 2, budget_s / 2) if budget_s is not None
                           else (HANDSHAKE_TIMEOUT_S, LIST_TIMEOUT_S))
    proc = processes.popen_interactive(
        [binary, "app-server"], cwd=root,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    # readline() blocks unbounded, so the per-message deadlines only fire BETWEEN
    # lines: a vendor that connects and then says nothing would hang the owner's
    # terminal forever. This watchdog is the real ceiling (security audit M3).
    watchdog = threading.Timer(handshake_s + list_s + 5.0, proc.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        def send(msg: dict[str, Any]) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def await_id(rid: int, timeout: float) -> dict[str, Any] | None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    return None
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # notifications and banners are not our answer
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    return msg
            return None

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "universal_agent_harness",
                           "title": "Universal Agent Harness", "version": "0"},
            "capabilities": {},
        }})
        handshake = await_id(1, handshake_s)
        if not handshake or "result" not in handshake:
            return {"error": "app-server handshake failed"}
        send({"method": "initialized", "params": {}})

        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}})
        answer = await_id(2, list_s)
        if not answer:
            return {"error": "hooks/list: no response"}
        if "error" in answer:
            return {"error": f"hooks/list: {answer['error'].get('message') or answer['error']}"}
        return {"result": answer["result"]}
    finally:
        watchdog.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        # kill(), NOT signal_process_tree: popen_interactive does not isolate the
        # child, so on POSIX its killpg would SIGTERM the harness's OWN process
        # group and the operator's shell (security audit H1). codex_appserver does
        # the same thing for the same reason.
        proc.kill()
        try:
            proc.wait(timeout=5)  # reap; a zombie would otherwise linger on POSIX
        except Exception:
            pass


def _in_repo(source_path: str, root: Path) -> bool:
    """Is this hook declared by THIS repo?

    Path semantics, not string prefix: a bare `startswith` also matches a sibling
    like `...-prototype-EVIL`, which would let a neighbouring checkout's hooks be
    reported (or hidden) as ours.
    """
    try:
        return Path(source_path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def probe_codex(root: Path, *, rpc: Any = None, budget_s: float | None = None) -> dict[str, Any]:
    """Which hooks codex will NOT run in this repo until the owner accepts them.

    `rpc` injects the vendor answer (tests pass a fixture); default asks the vendor.
    `budget_s` caps the vendor round trip for callers that cannot wait (session start).
    """
    binary = _find_codex()
    if not binary and rpc is None:
        return {"vendor": "codex", "status": "vendor-absent", "pending": [], "running": 0,
                "detail": "codex CLI not found on PATH"}
    if not (root / ".codex" / "hooks.json").is_file():
        return {"vendor": "codex", "status": "no-hooks", "pending": [], "running": 0,
                "detail": "no .codex/hooks.json in this repo; nothing to accept"}

    try:
        answer = rpc(root) if rpc is not None else _rpc_hooks_list(binary, root, budget_s=budget_s)
    except OSError as exc:
        return {"vendor": "codex", "status": "probe-failed", "pending": [], "running": 0,
                "detail": f"app-server spawn failed: {exc}"}
    if "error" in answer:
        return {"vendor": "codex", "status": "probe-failed", "pending": [], "running": 0,
                "detail": answer["error"]}

    pending, running, disabled = [], 0, 0
    for entry in answer["result"].get("data") or []:
        for hook in entry.get("hooks") or []:
            # Plugin hooks live outside the repo and are the owner's own business.
            if not _in_repo(str(hook.get("sourcePath") or ""), root):
                continue
            # Switched off is not the same as unaccepted: accepting would not make
            # it run, so it is never reported as a pending acceptance.
            if not hook.get("enabled", True):
                disabled += 1
                continue
            status = str(hook.get("trustStatus") or "")
            if status in RUNNING:
                running += 1
                continue
            pending.append({
                "trustStatus": status,
                "event": str(hook.get("eventName") or ""),
                "matcher": hook.get("matcher") or "",
                "command": hook.get("command") or "",
                "enabled": bool(hook.get("enabled", True)),
                "key": str(hook.get("key") or ""),
            })
    return {
        "vendor": "codex",
        "status": "pending" if pending else "clear",
        "running": running,
        "disabled": disabled,
        "pending": pending,
        "grantedBy": "the vendor's own UI — the protocol exposes no trust-granting method",
    }


# Every probe takes (root, *, budget_s=None) -> report. `budget_s` is passed
# EXPLICITLY rather than through **kwargs so a new vendor probe that forgets it
# fails loudly at the call site instead of silently muting the session surface.
PROBES = {"codex": probe_codex}


def probe_all(root: Path, *, budget_s: float | None = None) -> list[dict[str, Any]]:
    return [probe(root, budget_s=budget_s) for probe in PROBES.values()]


# --------------------------------------------------------------------------- #
# the cached probe: what a POLLED surface may read
# --------------------------------------------------------------------------- #
CACHE_REL = ".harness/state/acceptances.json"
MAX_LABEL = 40


def labels(pending: list[dict[str, Any]]) -> list[str]:
    """Short names for the inert hooks -- the script is what makes it real.

    `agent_spawn_economy is inert` lands; `PreToolUse [Bash] is inert` does not.
    The SCRIPT token, not the last one: a hook registered WITH arguments (this repo
    has `update_agent_handoff.py --check`) would otherwise be reported as `--check`.
    Falls back to the event name when the command is not a python script.

    Bounded on purpose. This text is vendor-reported and both readers put it in front
    of someone -- an agent's context at session start, a button's question in the
    panel -- so a label is one whitespace-free stem of at most MAX_LABEL chars, not
    somewhere a crafted path can write a sentence.
    """
    out = []
    for row in pending:
        script = next((tok for tok in reversed(str(row.get("command") or "").split())
                       if tok.endswith(".py")), "")
        out.append((Path(script).stem or str(row.get("event") or "?"))[:MAX_LABEL])
    return out


def write_cache(root: Path, reports: list[dict[str, Any]]) -> str | None:
    """Park a fresh probe where a POLLED surface can read it. None if it did not land.

    The decision inbox is re-collected on every panel poll and every `decide`, so it
    must never spawn an app-server: it reads this file instead (the `vendor_fuel`
    shape). Both live probers write it — the CLI verb and the SessionStart surface —
    which is what keeps it fresh without anything polling the vendor: opening a
    session refreshes the panel.

    Never during a gate hold: the hold swaps the live tree, so the write would be
    discarded and the panel would keep showing a stale answer as if it were current.
    """
    if gate_holding(root):
        return None
    path = root / CACHE_REL
    try:
        write_json(path, {"schemaVersion": 1, "probedAt": now(), "reports": reports})
    except OSError:
        return None
    return str(path)


def refresh(root: Path, *, budget_s: float | None = None) -> list[dict[str, Any]]:
    """Ask every vendor and park the answer. The ONLY way the cache is written.

    Every live prober goes through here, including after an accept: a cache that
    survives a successful drive unrefreshed leaves the panel showing a button for
    work already done, and clicking it again drives the vendor a second time.
    """
    reports = probe_all(root, budget_s=budget_s)
    write_cache(root, reports)
    return reports


def cached(root: Path) -> dict[str, Any]:
    """The last probe, or {} when there is none. Missing/corrupt is NOT "clear":
    callers must treat an empty cache as "unknown", never as "nothing pending".

    `read_json` RAISES on torn JSON, and this file is read by the decision inbox --
    which every panel poll and every `decide` goes through. A half-written cache must
    degrade to "unknown", not take out the whole decisions surface.
    """
    try:
        data = read_json(root / CACHE_REL, {}) or {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_WHY = {
    "untrusted": "never accepted",
    "modified": "accepted once, but the hook changed since",
    "": "unknown trust state",
}


def _print_report(report: dict[str, Any]) -> None:
    vendor, status = report["vendor"], report["status"]
    pending = report.get("pending") or []
    if status != "pending":
        print(f"{vendor}\t{status}\t{report.get('detail', 'nothing pending')}")
        return
    total = len(pending) + report["running"]
    print(f"{vendor}\tPENDING {len(pending)} of {total} repo hook(s) — these do NOT run "
          f"in {vendor} lanes until you accept them in {vendor} itself:")
    for row in pending:
        why = _WHY.get(row["trustStatus"], row["trustStatus"])
        matcher = f" [{row['matcher']}]" if row["matcher"] else ""
        print(f"  - {row['event']}{matcher}: {row['command']}")
        print(f"      {row['trustStatus']} — {why}")


def _visible_plan(root: Path, vendor: str) -> dict[str, Any]:
    """The floor every other path falls back to: the owner answers the prompt."""
    return {
        "mode": "visible",
        "instruction": (
            f"Open {vendor} in this repository and accept the pending hooks in the vendor prompt."
        ),
        # Absolute path, not the bare name: Windows CreateProcess searches the app
        # dir and cwd before PATH, so a `codex.bat` dropped in the repo would win
        # (security audit M2).
        "launch": {"argv": [_find_codex() or vendor], "cwd": str(root)},
    }


def _write_transcript(root: Path, vendor: str, result: dict[str, Any],
                      steps: list[dict[str, Any]]) -> str | None:
    """Every screen, verdict and decision of a drive, on disk. None if it did not land.

    The gate check is REPEATED here rather than inherited from the start of the
    drive: a hold that begins mid-drive would discard this write, and a reported
    path that does not survive is worse than admitting nothing was recorded. The pid
    joins the timestamp because two drives in the same second must not overwrite
    each other's only audit record.
    """
    if gate_holding(root):
        return None
    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    path = root / ".harness" / "runs" / f"acceptance-{vendor}-{stamp}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**result, "steps": steps}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        # A full disk must not erase a drive that really succeeded.
        return None
    return str(path)


def drive_headless(
    root: Path,
    vendor: str,
    *,
    budget: int = DRIVE_BUDGET,
    session_factory: Any = None,
    reader: Any = None,
    reprobe: Any = None,
) -> dict[str, Any]:
    """Type the owner's ALREADY-GIVEN yes into a hidden vendor TUI, bounded and logged.

    The loop is deliberately dull: read a screen, ask the reader what it says, let
    `authorize_keystroke` decide whether one key may go out, send it, forget the
    screen. It never retries a denial and never composes a string.

    `verified` is the only claim here worth trusting. What the screen appeared to say
    and what the vendor actually recorded are different facts, so the drive re-asks
    the vendor over `hooks/list` -- the same question the probe asks -- after EVERY
    keystroke, and stops the moment the answer is "nothing pending".

    Worst case with the defaults is bounded but slow: budget x (screen 15s + reader
    45s + reprobe ~65s). There is no single wall clock; every leg carries its own.
    """
    if gate_holding(root):
        return {"mode": "headless", "status": "gate-hold",
                "reason": "the gate holds the tree; a transcript written now is discarded"}
    ok, transport = pty_drive.available()
    if session_factory is None and not ok:
        return {"mode": "headless", "status": "transport-unavailable", "reason": transport}

    read = reader or screen_reader.read_screen
    probe = reprobe or PROBES[vendor]
    factory = session_factory or (
        lambda: pty_drive.PtySession([_find_codex() or vendor], cwd=root))
    steps: list[dict[str, Any]] = []
    status, reason = "no-question", "the vendor never asked anything"
    after: dict[str, Any] | None = None
    try:
        session = factory()
    except Exception as exc:  # noqa: BLE001 — a transport that will not open is a fallback, not a crash
        return {"mode": "headless", "status": "transport-failed",
                "reason": f"could not open a pty: {exc}"}
    try:
        for sent in range(budget):
            screen = session.screen()
            if not screen.strip():
                status, reason = "vendor-quiet", "nothing on screen; the vendor may have exited"
                break
            # the vendor whose TUI this is reads its own screen (owner 2026-07-30):
            # its CLI is the one guaranteed present on a machine driving it.
            verdict = read(screen, DRIVE_INTENT, vendor=vendor)
            decision = screen_reader.authorize_keystroke(
                verdict, screen=screen, sends_remaining=budget - sent)
            steps.append({
                "step": sent + 1,
                # The SANITIZED screen: exactly what the reader was shown and what the
                # rail checked the verdict against. Provenance, not a debug log.
                "screen": screen_reader.sanitize(screen)[-2000:],
                "verdict": verdict,
                "decision": decision,
            })
            if not decision["allow"]:
                status, reason = "denied", decision["reason"]
                break
            # Forget the answered screen BEFORE typing, not after: the pump thread
            # can capture the vendor's reply between send() and clear(), and clearing
            # afterwards would wipe the very screen the next iteration must read.
            # Either order upholds the anti-carryover rail; only this one is a race.
            session.clear()
            session.send(decision["keys"])
            status, reason = "drove", f"{sent + 1} authorized keystroke(s)"
            # Stop the moment the vendor says the job is done. The owner's click
            # bought THIS acceptance, not an open-ended licence to answer whatever
            # the vendor asks next (security audit 2026-07-29, finding 1).
            after = probe(root)
            if after.get("status") == "clear":
                break
        else:
            status, reason = "budget-exhausted", f"still asking after {budget} keystrokes"
    except Exception as exc:  # noqa: BLE001 — a vendor that dies mid-drive falls back, never crashes
        status, reason = "transport-failed", f"the pty failed mid-drive: {exc}"
    finally:
        session.close()

    if after is None or after.get("status") != "clear":
        after = probe(root)
    verified = after.get("status") == "clear"
    sends = sum(1 for step in steps if step["decision"]["allow"])
    result = {
        "mode": "headless",
        "status": "accepted" if verified else status,
        # How the LOOP ended is not why the drive succeeded — a vendor that accepts
        # and exits leaves an empty screen behind, which is not the headline.
        "reason": (f"{sends} authorized keystroke(s); the vendor reports nothing pending"
                   if verified else reason),
        "verified": verified,
        "sends": sends,
        "pendingAfter": len(after.get("pending") or []),
        "transport": "injected" if session_factory is not None else transport,
    }
    result["transcript"] = _write_transcript(root, vendor, result, steps)
    if result["transcript"] is None:
        # An invisible action with no surviving record is exactly what the transcript
        # exists to prevent — say so out loud instead of reporting a phantom path.
        result["transcriptWarning"] = "no durable record: gate hold or write error"
    return result


def acceptance_plan(
    root: Path,
    vendor: str,
    mode: str,
    *,
    dry_run: bool = False,
    report: dict[str, Any] | None = None,
    drive: Any = None,
) -> dict[str, Any]:
    """Decide what actually happens for one vendor: report, drive, or hand it over."""
    report = report or PROBES[vendor](root)
    if dry_run:
        # The one flag whose whole contract is "never sends anything" must short
        # out ahead of every action path, or the pty lane inherits a false promise
        # (security audit M1).
        return {"mode": mode, "status": "dry-run",
                "reason": "dry run: reported only, nothing launched or typed",
                "pending": len(report.get("pending") or [])}
    if report.get("status") == "clear":
        return {"mode": mode, "status": "nothing-pending", "reason": "nothing pending"}
    if report.get("status") != "pending":
        return {
            "mode": mode,
            "status": str(report.get("status") or "probe-failed"),
            "reason": str(report.get("detail") or "acceptance probe failed"),
        }
    if mode == "headless":
        driven = (drive or drive_headless)(root, vendor)
        if driven.get("status") == "accepted":
            return driven
        # Anything else — no pty, an unreachable reader, a denied verdict, a screen
        # the vendor never cleared — hands the owner the flow they can see. That is
        # the whole fallback contract: headless is a convenience, visible is the floor.
        return {**driven, "fallback": _visible_plan(root, vendor)}
    return _visible_plan(root, vendor)


def cmd_acceptances(args: argparse.Namespace) -> None:
    if getattr(args, "action", None) == "accept":
        report = PROBES[args.vendor](ROOT)
        result = acceptance_plan(
            ROOT, args.vendor, args.mode, dry_run=args.dry_run, report=report,
        )
        if not args.dry_run:
            # Whatever just happened, the parked answer is now a guess. Re-park it so
            # the panel is not offering a button for work this command already did.
            refresh(ROOT)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if result.get("status") == "nothing-pending":
            print("nothing pending")
        elif result.get("status") == "dry-run":
            _print_report(report)
            print(f"dry-run\t{result['reason']}")
        elif report.get("status") != "pending":
            _print_report(report)
        elif result["mode"] == "headless":
            _print_report(report)
            print(f"headless\t{result['status']}\t{result['reason']}")
            if result.get("transcript"):
                print(f"transcript: {result['transcript']}")
            fallback = result.get("fallback")
            if fallback:
                print(f"falling back to the visible flow: {fallback['instruction']}")
                print(f"launch: {fallback['launch']['argv']} (cwd: {fallback['launch']['cwd']})")
        else:
            _print_report(report)
            print(result["instruction"])
            print(f"launch: {result['launch']['argv']} (cwd: {result['launch']['cwd']})")
        return

    reports = refresh(ROOT)
    if args.json:
        print(json.dumps({"reports": reports}, ensure_ascii=False, indent=2))
        return
    for report in reports:
        _print_report(report)
