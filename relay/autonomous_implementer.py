#!/usr/bin/env python3
"""
tare.tools Autonomous Implementer Worker (Claude Opus 4.8 Runner)
===============================================================
Monitors `relay/inbox/implementer/` and active trains in `PLAN_APPROVED` or
`AUDIT_REJECTED` states, and automatically spawns the Claude Code CLI
non-interactively to implement code, run test falsifiers, and dispatch to the auditor.

Architecture:
- Triggered by Relay Supervisor Daemon v3 (`watcher_daemon.py`).
- Implementer Profile: Claude Opus 4.8 High (`claude` CLI).
- Enforces strict Bounded Footprint, U-7D negative falsifiers, and pytest green.
- Dispatches automatically to `auditor` upon successful implementation.
- Releases lease fail-closed on timeout or failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
INBOX_DIR = RELAY_DIR / "inbox" / "implementer"
PROCESSED_DIR = INBOX_DIR / "processed"
QUARANTINE_DIR = INBOX_DIR / "quarantine"
HEARTBEAT_FILE = RELAY_DIR / "history" / "implementer-heartbeat.json"

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import relay_mesh, vendor_chairs

AGENT_ID = os.environ.get("RELAY_IMPLEMENTER_AGENT", "claude_opus")
CLAUDE_EXE = os.environ.get("CLAUDE_EXE") or shutil.which("claude") or "claude"
TIMEOUT_SECONDS = int(os.environ.get("RELAY_IMPLEMENTER_TIMEOUT", "900"))  # 15 min max per train


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{stamp}] [AUTONOMOUS IMPLEMENTER] {msg}", flush=True)


IMPLEMENTER_PROMPT_TEMPLATE = """You are the LEAD IMPLEMENTER agent ({agent_id}) in the tare.tools Multi-Agent Relay Mesh (MARM).
Your counterpart is the External Adversarial Auditor agent (antigravity).

## 🛑 MANDATORY AUTONOMOUS EXECUTION DIRECTIVE (ZERO QUESTIONS):
- You are executing in a 100% UNATTENDED HEADLESS PIPELINE. There is NO human watching or available to answer queries.
- NEVER ASK QUESTIONS, prompt for input, or wait for confirmation.
- Make defensive, robust autonomous engineering decisions, implement the code, run pytest, produce evidence artifacts, and dispatch to auditor in this single turn.

## 🏛️ REPOSITORY ARCHAEOLOGY & SOURCE-OF-TRUTH GROUNDING:
- The tare.tools universal harness prototype codebase is located in `scripts/harness_lib/` (186 modules including `tasks_store.py`, `graph_backlog_adapter.py`, `vendor_fuel.py`, `agent_parity.py`, `async_runtime.py`, `route_loop.py`).
- The standalone DAG Graph Backlog engine is located in `satellites/tare.tools.backlog-graph/`.
- ZERO-TOLERANCE ANTI-EVASION DIRECTIVE: NEVER emit a 0-byte code diff or write a dummy markdown receipt claiming "missing source files" / "BLOCKED_RAW_EXPORT". You MUST read the existing python modules in `scripts/harness_lib/`, implement concrete Python code, write unit test falsifiers in `tests/`, and pass `pytest tests/`.

CURRENT TASK CONTEXT:
- Train ID: {train_id}
- Target Tasks: {task_ids}
- Train Title: {title}
- Trigger Mode: {mode}

---

## 1. TASK SPECIFICATION (PACKET.md):
{packet_content}

---

## 2. PLAN AUDIT REPORT:
{plan_audit_content}

---

## 3. AUDIT FEEDBACK (IF REMEDIATING):
{audit_feedback}

---

## 4. U-7D DEFENSIVE IMPLEMENTATION DIRECTIVES:
1. STRICT BOUNDED FOOTPRINT:
   - Touch ONLY the authorized files listed in PACKET.md.
   - Do NOT edit or commit changes to `work-graph.json` directly (it lands atomically upon approval).
   - Zero hardcoded paths: use `Path(__file__).resolve().parent.parent` dynamically.

2. U-7D CODE RESILIENCE (address ALL SEVEN dimensions explicitly):
   - D1 Logic, Invariants & Boundaries: Handle None, empty values, extremes, off-by-one, and preserve idempotency (f(x) == f(f(x))).
   - D2 Concurrency & State: Prevent check-then-write / TOCTOU races; respect CAS revisions and lease ownership; no self-approval or out-of-order FSM transitions.
   - D3 Resilience & Fail-Closed: Never swallow errors with opaque `except: pass`. Fail closed (hold/alert), bound retries, raise contextual exceptions.
   - D4 Security & Input Handling: Sanitize untrusted inputs at boundaries; parameterize commands/paths; no path traversal, no `shell=True`, zero secret/credential leakage.
   - D5 Resource Hygiene: Wrap files, sockets, and subprocesses in context managers (`with` / `try...finally`); keep memory/handles bounded.
   - D6 Compatibility & Rollback: Preserve existing call signatures, placeholders, and contracts; ensure the change reverts cleanly with no orphaned state.
   - D7 Actionable Observability: Emit structured, contextual errors and evidence (exact paths, test receipts) so a post-mortem needs no guesswork.

3. NEGATIVE FALSIFIER UNIT TESTS:
   - Implement both positive happy paths AND explicit negative falsifiers (asserting failure on invalid inputs).
   - Run `pytest tests/` locally to ensure 100% green build.

---

## 5. EXECUTION WORKFLOW:
Step 1: Acquire lease (if not active):
        python relay/relay_mesh.py claim --train {train_id} --agent "{agent_id}" --role "implementer"
Step 2: Implement code changes and new unit test falsifiers in `tests/`.
Step 3: Run `pytest tests/` and verify 100% assertions pass.
Step 4: Generate diff patch and implementation evidence:
        git diff HEAD > relay/trains/{train_id}/DIFF.patch
        Write test outputs and summary to `relay/trains/{train_id}/IMPLEMENTATION_EVIDENCE.md`.
Step 5: Dispatch to Adversarial Auditor:
        python relay/relay_mesh.py dispatch --train {train_id} --next-role "auditor" --agent "{agent_id}" --notes "Implemented code, negative falsifiers verified green."

Execute the above steps immediately.
"""


def _resolve_claude() -> str:
    if Path(CLAUDE_EXE).exists():
        return str(CLAUDE_EXE)
    w = shutil.which("claude")
    if w:
        return w
    default_win = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"
    if default_win.exists():
        return str(default_win)
    return "claude"


def run_implementer(prompt: str) -> subprocess.CompletedProcess:
    res = vendor_chairs.dispatch_role("implementer", prompt, cwd=RELAY_ROOT, timeout=TIMEOUT_SECONDS)
    completed = subprocess.CompletedProcess(
        args=["vendor_chairs", res.engine],
        returncode=res.returncode,
        stdout=res.stdout,
        stderr=res.stderr,
    )
    completed.runtime_key = res.runtime_key
    completed.runtime_model = res.runtime_model
    return completed


def run_claude(prompt: str) -> subprocess.CompletedProcess:
    return run_implementer(prompt)


def implement_train(train_id: str, force: bool = False) -> bool:
    """Spawns an implementer session (Claude Opus -> Antigravity Gemini 3.7 -> Codex) on the fly."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("RELAY_DISABLE_AUTONOMOUS_WORKERS"):
        return False
    train_dir = relay_mesh.TRAINS_DIR / train_id
    state_file = train_dir / "STATE.json"
    if not state_file.exists():
        log(f"Train {train_id} does not exist.")
        return False

    state = relay_io.read_json(state_file) or {}
    status = state.get("status")
    if status not in ("PLAN_APPROVED", "AUDIT_REJECTED"):
        log(f"Train {train_id} is in status {status!r}; implementer not actionable.")
        return False

    revision = relay_mesh.train_revision(state)
    key = f"{train_id}::r{revision}::implementer"
    if not force and not relay_io.outcome_actionable(key):
        log(f"Train {train_id} (rev {revision}) is inside backoff window; skipping.")
        return False

    packet_file = train_dir / "PACKET.md"
    plan_audit_file = train_dir / "PLAN_AUDIT_REPORT.md"
    code_audit_file = train_dir / "AUDIT_REPORT.md"

    packet_content = relay_io.read_text(packet_file) if packet_file.exists() else "No PACKET.md found."
    plan_audit_content = relay_io.read_text(plan_audit_file) if plan_audit_file.exists() else "No PLAN_AUDIT_REPORT.md found."
    audit_feedback = relay_io.read_text(code_audit_file) if (status == "AUDIT_REJECTED" and code_audit_file.exists()) else "None. Fresh implementation."

    prompt = IMPLEMENTER_PROMPT_TEMPLATE.format(
        agent_id=AGENT_ID,
        train_id=train_id,
        task_ids=", ".join(state.get("task_ids", [])),
        title=state.get("title", ""),
        mode="Remediation" if status == "AUDIT_REJECTED" else "Initial Implementation",
        packet_content=packet_content,
        plan_audit_content=plan_audit_content,
        audit_feedback=audit_feedback,
    )

    # Acquire exclusive process lease BEFORE starting implementer (falsifier protection)
    if not relay_mesh.acquire_lease(train_id, AGENT_ID, "implementer"):
        log(f"Cannot acquire lease for {train_id}; actively held by another host/process. Skipping.")
        return False

    try:
        proc = run_implementer(prompt)
        log(f"Implementer returned exit code {proc.returncode}")
        
        # Check if the train successfully transitioned to AUDIT_PENDING
        new_state = relay_io.read_json(state_file) or {}
        new_status = new_state.get("status")
        if new_status == "AUDIT_PENDING":
            if state.get("execution_descriptor") is not None:
                try:
                    from relay.landing_policy import (
                        IMPLEMENTER_IDENTITY_FILE,
                        attest_identity,
                        model_id_for_runtime,
                        write_identity,
                    )

                    engine = str(proc.args[1])
                    roster = relay_mesh.RELAY_DIR / "round_table_roster.json"
                    identity = attest_identity(
                        roster_path=roster,
                        agent_id=AGENT_ID,
                        role="implementer",
                        model_id=model_id_for_runtime(
                            roster, proc.runtime_key, proc.runtime_model
                        ),
                        runtime_key=proc.runtime_key,
                        runtime_model=proc.runtime_model,
                    )
                    write_identity(train_dir / IMPLEMENTER_IDENTITY_FILE, identity)
                    log(
                        f"Attested strict implementer identity for {engine}: "
                        f"{identity.model_id}."
                    )
                except Exception as exc:
                    err = f"Strict implementer identity unavailable: {exc}"
                    log(f"ERROR: {err}")
                    relay_io.outcome_retry(key, err)
                    return False
            log(f"SUCCESS: Train {train_id} transitioned to AUDIT_PENDING.")
            relay_io.outcome_ack(key, f"Dispatched to auditor by {AGENT_ID}")
            return True
        else:
            err = f"Implementer completed but train status is {new_status!r} (expected AUDIT_PENDING). Output: {proc.stderr[:300]}"
            log(f"WARN: {err}")
            relay_io.outcome_retry(key, err)
            relay_mesh.release_lease(train_id, AGENT_ID)
            return False
    except subprocess.TimeoutExpired:
        err = f"Implementer timed out after {TIMEOUT_SECONDS}s on train {train_id}."
        log(f"ERROR: {err}")
        relay_io.outcome_retry(key, err)
        relay_mesh.release_lease(train_id, AGENT_ID)
        return False
    except Exception as exc:
        err = f"Implementer execution exception: {type(exc).__name__}: {exc}"
        log(f"ERROR: {err}")
        relay_io.outcome_retry(key, err)
        relay_mesh.release_lease(train_id, AGENT_ID)
        return False
def process_implementer_inbox() -> int:
    """Scans inbox and actionable trains, spawning implementer sessions on the fly."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    effects = 0
    # 1. Scan active trains
    if relay_mesh.TRAINS_DIR.exists():
        for td in sorted(relay_mesh.TRAINS_DIR.iterdir()):
            if not td.is_dir():
                continue
            state = relay_io.read_json(td / "STATE.json") or {}
            if state.get("tombstone"):
                continue
            status = state.get("status")
            if status in ("PLAN_APPROVED", "AUDIT_REJECTED"):
                train_id = state.get("train_id", td.name)
                log(f"Actionable train found: {train_id} (status={status})")
                if implement_train(train_id):
                    effects += 1

    # 2. Sweep inbox messages
    for msg_file in sorted(INBOX_DIR.glob("*.json")):
        try:
            target = PROCESSED_DIR / msg_file.name
            msg_file.rename(target)
        except Exception:
            pass

    return effects


def main() -> None:
    relay_io.utf8_console()
    log(f"Autonomous Implementer starting (agent={AGENT_ID}, exe={_resolve_claude()})...")
    effects = process_implementer_inbox()
    log(f"Implementer sweep completed ({effects} effects applied).")


if __name__ == "__main__":
    main()
