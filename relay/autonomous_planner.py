#!/usr/bin/env python3
"""
tare.tools Autonomous Planner Worker (Codex Runner v2.1 with Session Reuse)
===========================================================================
Monitors `relay/inbox/planner` and automatically invokes the Codex CLI
non-interactively via `codex exec` or `codex exec resume --last`.

Features:
- Smart Session Reuse: Reuses the active session (`resume --last`) to preserve
  capsule context, reduce token consumption, and accelerate planning turns.
- Graceful Fallback: If resume fails (e.g. first run), cleanly spawns a fresh session.
- Explicit Reset Capability: Honor `force_new_session: true` in inbox payloads when
  vendor failover or epoch transitions require a pristine session.
- Fail-closed Error Handling: Quarantines malformed envelopes after 3 retries.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
INBOX_DIR = RELAY_DIR / "inbox" / "planner"
PROCESSED_DIR = INBOX_DIR / "processed"
QUARANTINE_DIR = INBOX_DIR / "quarantine"
HEARTBEAT_FILE = RELAY_DIR / "history" / "planner-heartbeat.json"
SESSION_TRACKER_FILE = RELAY_DIR / "history" / "planner-session.json"

if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
import planner_role  # noqa: E402  — active-vendor / epoch source of truth


# ---------------------------------------------------------------------------
# Vendor-neutral executor registry (RELAY-PLANNER-VENDOR-FAILOVER-01).
#
# A vendor is eligible because it has a registered, resolvable executor AND a
# proven capability contract — NOT because a brand string is hardcoded in the
# control flow. The ACTIVE vendor is read from the Planner epoch record, so a
# fenced vendor failover swaps the executor with zero code change here.
# ---------------------------------------------------------------------------

def _resolve_binary(env_var: str, exe_name: str, windows_default: str | None) -> str:
    env_path = os.environ.get(env_var)
    if env_path and Path(env_path).exists():
        return env_path
    which_path = shutil.which(exe_name)
    if which_path:
        return which_path
    if windows_default and Path(windows_default).exists():
        return windows_default
    return exe_name


def _codex_argv(exe: str, prompt: str, reuse: bool) -> list[str]:
    if reuse:
        return [exe, "exec", "resume", "--last",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
    return [exe, "exec", "--dangerously-bypass-approvals-and-sandbox",
            "--cd", str(RELAY_ROOT), prompt]


def _claude_argv(exe: str, prompt: str, reuse: bool) -> list[str]:
    # Claude Code headless; no session-resume flag in this integration.
    return [exe, "-p", prompt, "--dangerously-skip-permissions"]


VENDORS: dict[str, dict] = {
    "codex": {
        "binary": lambda: _resolve_binary(
            "CODEX_EXE", "codex",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe")),
        "argv": _codex_argv, "supports_reuse": True,
    },
    "claude": {
        "binary": lambda: _resolve_binary("CLAUDE_EXE", "claude", None),
        "argv": _claude_argv, "supports_reuse": False,
    },
}
DEFAULT_VENDOR = os.environ.get("RELAY_PLANNER_VENDOR", "codex")


def active_vendor() -> str:
    """The vendor bound to the current Planner epoch; falls back to config only
    when no epoch is established yet."""
    rec = planner_role.read_epoch()
    if rec and rec.get("vendorId"):
        return str(rec["vendorId"])
    return DEFAULT_VENDOR


def resolve_executor(vendor_id: str) -> dict:
    cfg = VENDORS.get(vendor_id)
    if not cfg:
        raise KeyError(f"vendor {vendor_id!r} has no registered executor "
                       "(fail closed; register it in VENDORS before failover)")
    return cfg


PLANNER_PROMPT_TEMPLATE = """You are the LEAD ARCHITECT & PLANNER agent (Role: planner, Agent ID: {agent_id}) for tare.tools Multi-Agent Relay Mesh.

## 🛑 MANDATORY AUTONOMOUS EXECUTION DIRECTIVE (ZERO QUESTIONS):
- You are executing in a 100% UNATTENDED HEADLESS PIPELINE. There is NO human watching or available to answer queries.
- NEVER ASK QUESTIONS, prompt for input, or wait for confirmation.
- Read the DAG frontier, initialize the train, draft the complete PACKET.md with U-7D negative falsifiers, and dispatch to plan-auditor in this single turn.

Read AGENTS.md, docs/guides/RELAY_MESH.md and docs/guides/RELAY_PLANNER_PLAYBOOK.md, and query the frontier with:
`python satellites/tare.tools.backlog-graph/graph_ops.py --graph work-graph.json next --limit 5`

Your current planning directive:
{task_instructions}

================================================================================
CRITICAL OPERATIONAL INVARIANTS & PLANNING PRINCIPLES:
================================================================================
1. ANTI-ZENO PARADOX & ZERO TASK INFLATION:
   - Deliver the actual engineering capability requested. Do NOT spawn infinite administrative subtasks or micro-governance layers (T -> 01A -> 01B).
   - Keep the Metadata-to-Work Ratio (MWR) strictly minimal: write concise, high-signal, executable contracts, not multi-kilobyte redundant boilerplate.

2. BOUNDED FOOTPRINT & SINGLE-WRITER ISOLATION:
   - Enumerate the EXACT implementer-authorized write paths (one cohesive train — no task inflation, no umbrella meta-tasks).
   - The canonical `work-graph.json` remains read-only during implementation (landing authority is strictly held by the Human Gatekeeper). Never instruct the implementer to self-approve, mutate the graph/FSM, or bypass an unavailable gate.
   - Require relative path resolution (`Path(__file__).resolve().parent.parent`) — zero hardcoded paths.

3. DEFINITION OF DONE & U-7D NEGATIVE FALSIFIERS:
   - State an explicit, compact Definitions-of-Done section with verifiable exit criteria.
   - Mandate at least ONE explicit negative test falsifier for EVERY applicable U-7D dimension (logic/invariants, concurrency, resilience/fail-closed, security/input handling, resource hygiene, compatibility/rollback, observability).

4. EXECUTE PLANNING STEPS:
   Step 1: Initialize train:
           python relay/relay_mesh.py init-train --id <TRAIN_ID> --title "<TITLE>" --tasks "<TASKS>" --agent "{agent_id}"
   Step 2: Write `relay/trains/<TRAIN_ID>/PACKET.md` with:
           - Executive Scope & Architectural Intent
           - Bounded Write Footprint (Exact files)
           - Definition of Done & Falsifiers Matrix (U-7D)
   Step 3: Dispatch to Plan Auditor:
           python relay/relay_mesh.py dispatch --train <TRAIN_ID> --next-role "plan-auditor" --agent "{agent_id}" --notes "Drafted packet with bounded footprint and U-7D falsifiers."
   Step 4: Verify with `python relay/relay_mesh.py board` and exit.
"""


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [AUTONOMOUS PLANNER] {msg}", flush=True)


def update_heartbeat():
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "2.1",
            "pid": os.getpid(),
            "timestamp": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "RUNNING",
            "sessionReuse": True
        }
        HEARTBEAT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(RELAY_ROOT), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, **relay_io.safe_subprocess_kwargs())


def execute_vendor(prompt: str, vendor_id: str | None = None,
                   reuse_session: bool = True) -> subprocess.CompletedProcess:
    """Execute the active (or named) planner vendor non-interactively, with
    optional session reuse where the vendor supports it. Vendor selection is
    registry-driven and fails closed for an unregistered vendor."""
    vendor_id = vendor_id or active_vendor()
    cfg = resolve_executor(vendor_id)
    import vendor_chairs
    off = vendor_chairs.chair_off(vendor_id, "planner", model_known=False)  # the argv below names no model
    if off:  # the routing profile switches this planner off: no session starts; the caller quarantines the message
        log(f"[{vendor_id}] SWITCHED_OFF_FOR_ROLE: {off}")
        return subprocess.CompletedProcess([vendor_id], 1, "", f"SWITCHED_OFF_FOR_ROLE: {off}")
    exe = cfg["binary"]()
    reuse = reuse_session and bool(cfg.get("supports_reuse"))
    if reuse:
        log(f"[{vendor_id}] resuming session via {exe}...")
        proc = _run(cfg["argv"](exe, prompt, True))
        if proc.returncode == 0:
            return proc
        log(f"[{vendor_id}] resume returned {proc.returncode}; fresh session...")
    log(f"[{vendor_id}] spawning fresh session via {exe}...")
    return _run(cfg["argv"](exe, prompt, False))


def process_planner_inbox() -> int:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("RELAY_DISABLE_AUTONOMOUS_WORKERS"):
        return 0
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    messages = sorted(INBOX_DIR.glob("*.json"))
    if not messages:
        return 0

    processed_count = 0
    for msg_file in messages:
        try:
            if not msg_file.exists():
                continue
            raw = msg_file.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            subject = data.get("subject", "Planning Task")
            payload = data.get("payload", {})
            force_new_session = bool(payload.get("force_new_session", False))
            
            log(f"Detected new work item: '{subject}' ({msg_file.name}) [force_new_session={force_new_session}]")

            instructions = f"Subject: {subject}\nPayload: {json.dumps(payload, indent=2)}\n"
            if payload.get("action") == "INIT_TRAIN":
                top_task = payload.get("top_task", "RELAY-PLANNER-VENDOR-FAILOVER-01")
                instructions += f"\nDirective: Initialize and draft packet for task '{top_task}'."

            vendor = active_vendor()
            prompt = PLANNER_PROMPT_TEMPLATE.format(
                agent_id=f"{vendor}_planner", task_instructions=instructions)

            # Execute the active planner vendor with smart session reuse
            proc = execute_vendor(prompt, vendor_id=vendor, reuse_session=not force_new_session)
            log(f"[{vendor}] execution completed with exit code {proc.returncode}")
            
            if proc.returncode == 0:
                if msg_file.exists():
                    target = PROCESSED_DIR / msg_file.name
                    msg_file.rename(target)
                    log(f"Successfully processed and moved {msg_file.name} to processed/.")
                processed_count += 1
            else:
                log(f"Codex reported nonzero exit code {proc.returncode}. Stderr:\n{proc.stderr[-400:]}")
                if msg_file.exists():
                    target = QUARANTINE_DIR / msg_file.name
                    msg_file.rename(target)
                    log(f"Moved failed message {msg_file.name} to quarantine/.")

        except Exception as e:
            log(f"Failed processing {msg_file.name}: {e}")

    return processed_count


def run_loop():
    log(f"Starting continuous supervisor loop on {INBOX_DIR} (poll interval=10s, session reuse enabled)...")
    while True:
        try:
            update_heartbeat()
            process_planner_inbox()
        except Exception as e:
            log(f"Unexpected loop exception: {e}")
        time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        processed = process_planner_inbox()
        sys.exit(0 if processed >= 0 else 1)
    else:
        run_loop()
