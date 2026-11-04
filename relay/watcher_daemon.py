#!/usr/bin/env python3
"""
tare.tools Relay Supervisor Daemon v3.0 (TRAIN-06, Seam B)

Single versioned supervisor for automated reconciliation:
- LEVEL-TRIGGERED: every tick recomputes actionable state from trains/inboxes
  as they are NOW (work present before startup is found; Drive replay,
  reorder and resurrection are deduplicated by durable action outcomes);
- 15-second minimum evaluation floor;
- non-destructive message plane: processed/ on success, quarantine/ after 3
  bounded attempts; operator/human messages are NEVER auto-consumed;
- audits are delegated to REAL LLM critics (RELAY_PLAN_AUDIT_CMD /
  RELAY_CODE_AUDIT_CMD); absence fails closed and holds the gate with one
  deduplicated human notification — a deterministic script never rubber-stamps
  (2026-08-16 incidents);
- heartbeat with version/PID/host; a second live supervisor holds fail-closed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
# The mechanical code-audit gate runs `pytest tests/`, whose tcp-* scenarios
# import harness_lib from $HARNESS_REPO_DIR. Default it to THIS relay root — its
# scripts/harness_lib mirror is complete (incl. the landed tcp07 module), so the
# gate never breaks on a harness repo that lags the mirror, regardless of how the
# supervisor was launched (env inheritance through Start-Process is unreliable).
os.environ.setdefault("HARNESS_REPO_DIR", str(RELAY_ROOT))
# Default the on-demand auditor wiring so the daemon delegates to the local agy
# critic regardless of how it was launched (env inheritance through Start-Process
# is unreliable). An operator env var still overrides these.
os.environ.setdefault("RELAY_PLAN_AUDIT_CMD", 'python relay/autonomous_auditor.py plan-audit "{prompt_file}"')
os.environ.setdefault("RELAY_CODE_AUDIT_CMD", 'python relay/autonomous_auditor.py code-audit "{prompt_file}"')
sys.path.insert(0, str(RELAY_ROOT))
sys.path.insert(0, str(RELAY_DIR))
from relay import relay_mesh  # noqa: E402
import relay_io  # noqa: E402
import intake_processor  # noqa: E402  — async human intake queue (TRAIN-10)

_INTAKE_FAILURES: dict = {}  # bounded per-file retry counters for the intake sweep

VERSION = "3.0"
TICK_SECONDS = 15  # mandatory floor (Seam B.4)
HEARTBEAT_FILE = RELAY_DIR / "history" / "daemon-heartbeat.json"
HEARTBEAT_STALE_SECONDS = 90
LOG_FILE = RELAY_DIR / "watcher.log"
AUDIT_TIMEOUT = 3600
LEASE_TTL = AUDIT_TIMEOUT + 900  # C3: TTL exceeds subprocess timeout + margin
PROTECTED_INBOXES = {"human", "antigravity", "planner"}  # never auto-consumed (Seam B.2)

PLAN_AUDIT_PROMPT = """You are the LEAD ADVERSARIAL PLAN AUDITOR for the tare.tools Multi-Agent Relay Mesh (MARM).
Your mandate is STRICT UNIVERSAL ADVERSARIAL SCRUTINY with final architectural authority (Owner Ruling 2026-08-16).
You are NOT an assistant trying to be agreeable; you are a PRINCIPAL SECURITY & SYSTEMS GATEKEEPER whose sole job is to destroy fragile designs, uncover hidden deadlocks, expose race conditions, and prevent catastrophic failures BEFORE code is written. The swarm develops ANY software (kernels, web apps, distributed databases, ML pipelines, CLIs, financial systems).

================================================================================
UNIVERSAL 7-DIMENSION ADVERSARIAL ATTACK MATRIX (U-7D):
================================================================================
1. LOGIC, INVARIANTS & SEMANTIC INTEGRITY:
   - Are domain invariants, mathematical preconditions, and postconditions formally defined?
   - What happens on empty inputs, nulls, negative numbers, extreme scale, clock skew, and boundary edges?
   - Is every state transition explicit? Does the system make impossible states unrepresentable?
   - Is idempotency guaranteed (f(x) == f(f(x))) across retries, crashes, and duplicate events?

2. CONCURRENCY, ASYNCHRONY & DISTRIBUTED SYSTEMS:
   - What happens if 2+ instances, threads, hosts, or agents execute concurrently?
   - Identify all TOCTOU (Time-of-Check to Time-of-Use), check-then-write hazards, and race windows.
   - Look for lock order inversions (deadlocks), starvations, and lost updates.
   - If distributed/replicated: evaluate network partitions, out-of-order delivery, and split-brain risks.

3. RESILIENCE, FAILURE MODES & CRASH SURVIVAL:
   - Does the design FAIL CLOSED (stop safely, alert, hold) or fail open (silently corrupt data/state)?
   - Blast Radius: Does a secondary module failure crash the primary core (lack of bulkhead isolation)?
   - Retries: Are retries bounded with exponential backoff and jitter to prevent thundering herds?
   - Crash Consistency: If power is cut / SIGKILL issued mid-write, can the system recover cleanly on reboot?

4. SECURITY, THREAT MODEL & INJECTION SURFACES:
   - Are untrusted inputs sanitized at boundaries? Check for command injection, SQL/NoSQL injection, prompt injection, and path traversal (../).
   - Are credentials, secret keys, or PII exposed to logs, telemetry, error messages, or client payloads?
   - Evaluate authentication, authorization, and confused deputy vulnerabilities.

5. PHYSICAL RESOURCE BOUNDS & ALGORITHMIC COMPLEXITY:
   - Is algorithmic complexity bounded? Flag hidden O(N^2) / O(2^N) nested loops and ReDoS regex patterns.
   - Are memory and I/O strictly bounded? (Streams vs loading entire files/tables in RAM, connection pools, backpressure).
   - Are file descriptors, sockets, threads, and subprocesses guaranteed to close/cleanup via RAII / try-finally?

6. COMPATIBILITY, MIGRATION & ROLLBACK SAFETY:
   - Does this introduce breaking API, schema, or binary changes for existing consumers?
   - If a rollback is performed 1 hour after deployment, will persisted state remain readable and uncorrupted?

7. FORENSIC OBSERVABILITY & ACTIONABILITY:
   - Are failure modes clearly identifiable with structured contextual errors (who, what payload, why)?
   - Can an operator reconstruct the exact sequence of events from logs/metrics alone without guesswork?

================================================================================
DOMAIN-ADAPTIVE LENSES (Detect and Apply):
================================================================================
- Frontend/UI: Re-render loops, async race conditions, responsive layout breakage, XSS, event listener leaks.
- APIs/Microservices: Semantic HTTP status codes, idempotent headers, connection timeouts, circuit breakers.
- Systems/CLI: Exit codes (0 vs !=0), stdio piping, SIGINT/SIGTERM handling, cross-platform path resolution.
- Data/Databases: ACID transactions, write-ahead logs, serializable isolation, index lock contention.
- Multi-Agent/AI: FSM invariants, token budget overruns, agent hallucination loops, non-deterministic drift.

================================================================================
YOUR ACTIONS:
================================================================================
1. Read {packet_path} against current repository state, specs, and the U-7D matrix.
2. If you find architectural defects, ambiguities, or missing falsifiers: Correct PACKET.md IN-PLACE by appending '## Plan audit corrections' with exact constraints (do NOT return it to the planner).
3. Write {report_path} containing:
   - Explicit listing of all task IDs: {task_ids}
   - Systematic evaluation across the U-7D attack matrix with concrete failure scenarios identified
   - Bounded footprint assessment
   - An explicit residual-risk / trade-off decision (what remains uncovered and why it is acceptable)
   - Required corrections applied to PACKET.md
   - Exactly one terminal verdict line at the end:
     PLAN_VERDICT: APPROVED
     or
     PLAN_VERDICT: BLOCKED_ARCHITECTURE <detailed actionable reason>

Train: {train_id} | Tasks: {task_ids} | Packet: {packet_path}
"""

CODE_AUDIT_PROMPT = """You are the EXTERNAL ADVERSARIAL CODE AUDITOR for train {train_id} (tasks: {task_ids}).
Your mandate is RIGOROUS ADVERSARIAL FALSIFICATION across the Universal 7-Dimension Matrix (Owner Ruling 2026-08-16).
You are NOT an assistant looking to approve code. You are an ELITE RED TEAM AUDITOR seeking to break, falsify, and expose edge cases, race conditions, memory/resource leaks, security flaws, and footprint violations before anything reaches production.

================================================================================
UNIVERSAL 7-DIMENSION CODE FALSIFICATION (U-7D):
================================================================================
1. LOGIC, INVARIANTS & FALSIFIER VERIFICATION:
   - Verify every test in DIFF.patch and IMPLEMENTATION_EVIDENCE.md.
   - Attack beyond the implementer's happy path: test empty/nil/corrupted inputs, boundary conditions, and invalid state transitions.
   - Verify that all falsifiers (FAL-01..FAL-xx / F-01..F-xx) in {packet_path} are verified with negative tests.

2. CONCURRENCY, RACES & ATOMICITY:
   - Inspect all file/state mutations for check-then-write race conditions, lock inversions, and CAS desynchronization.
   - Ensure outcome keys, retry timers, and leases do NOT reset or loop infinitely.

3. RESILIENCE & FAIL-CLOSED ENFORCEMENT:
   - Trace negative execution paths: subprocess failures, network timeouts, unhandled exceptions, and corrupt files.
   - Confirm that the system fails closed (holds / alerts / isolates) and never hangs in a silent zombie state.

4. SECURITY, INJECTION & SANITIZATION:
   - Verify that all external inputs (shell commands, queries, paths, payloads) are strictly parameterized/sanitized.
   - Verify zero secret, credential, or PII leakage in logs, error strings, or responses.

5. PHYSICAL RESOURCE HYGIENE:
   - Verify all file handles, sockets, database connections, and subprocesses are safely managed with context managers / try-finally.
   - Check that memory consumption is bounded (streaming vs read-all).

6. BOUNDED FOOTPRINT & CONTRACT COMPATIBILITY:
   - Inspect DIFF.patch line-by-line: verify that EXACTLY the authorized production files were modified and ZERO unauthorized files were touched.
   - Ensure backward compatibility and safe rollback behavior.

7. OBSERVABILITY & ACTIONABLE TELEMETRY:
   - Ensure errors include structured, actionable context and avoid opaque/swallowed exceptions ('except: pass').

8. ZERO-TOLERANCE ANTI-EVASION CHECK:
   - For all P0 implementation tasks: verify that real code and passing tests were produced. If DIFF.patch has 0 lines of code change or claims 'missing files' without inspecting `scripts/harness_lib/` (186 modules of the universal harness prototype) and `satellites/tare.tools.backlog-graph/`, you MUST FAIL the audit with: `AUDIT_VERDICT: FAIL Zero code diff / Evasion of P0 task implementation`.

================================================================================
REQUIRED REPORT STRUCTURE IN {report_path}:
================================================================================
1. Header naming every task id ({task_ids}) and BINDING the packet digest: {packet_digest}.
2. Section '1. Executive Summary & Seam Verification'.
3. Section '2. Adversarial Battery & Falsifier Assessment' (systematic assessment against the U-7D dimensions).
4. Section '3. Production Footprint Verification' (line-by-line diff review of authorized paths).
5. Section '4. Audit Recommendation: Approve vs Do Not Approve' (explicit trade-off analysis, technical debt, and residual risks).
6. Terminal Verdict Marker:
   - If 100% compliant: include 'VERIFIED_PASS' and end with 'AUDIT_VERDICT: VERIFIED_PASS'.
   - If any defect/risk remains: end with 'AUDIT_VERDICT: FAIL <detailed actionable reason>'.
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_current_epoch() -> int:
    hb = relay_io.read_json(HEARTBEAT_FILE)
    if isinstance(hb, dict) and isinstance(hb.get("epoch"), int):
        return hb["epoch"]
    return 1


def heartbeat(last_reconcile: float, epoch: Optional[int] = None) -> None:
    # Multi-host admission gate (TRAIN-21): a denied/standby host writes NO
    # heartbeat. Re-checked on every beat so a mid-run credential removal halts
    # the async heartbeat thread on its next tick (no in-memory latching).
    if not relay_mesh.coordination_authorized()[0]:
        return
    if epoch is None:
        epoch = get_current_epoch()
    relay_io.write_json_atomic(HEARTBEAT_FILE, {
        "version": VERSION, "pid": os.getpid(), "host": relay_mesh.get_hostname(),
        "epoch": epoch,
        "lastScan": time.time(), "lastReconcile": last_reconcile,
        "nextRetry": time.time() + TICK_SECONDS})


def another_supervisor_live() -> bool:
    hb = relay_io.read_json(HEARTBEAT_FILE)
    if not isinstance(hb, dict):
        return False
    fresh = time.time() - float(hb.get("lastScan", 0)) < HEARTBEAT_STALE_SECONDS
    same = hb.get("pid") == os.getpid() and hb.get("host") == relay_mesh.get_hostname()
    if fresh and not same:
        if hb.get("host") == relay_mesh.get_hostname():
            try:
                import psutil
                if not psutil.pid_exists(int(hb.get("pid", -1))):
                    return False  # stale dead process on same host
            except Exception:
                pass
        return True
    return False


def _notify_hold_once(key: str, title: str, details: str) -> None:
    """One deduplicated human notification per held gate (C7)."""
    hold_key = f"hold-notice::{key}"
    if relay_io.outcome_get(hold_key) is None:
        relay_io.outcome_ack(hold_key, note=title)
        relay_mesh.raise_alert(title, "high", details, f"relay-supervisor-v{VERSION}")


def _delegate(train_id: str, env_var: str, prompt: str, prompt_name: str, agent: str = "antigravity_auditor") -> bool:
    stage = "plan-audit" if "PLAN" in prompt_name else "code-audit"
    default_cmd = f'"{sys.executable}" "{RELAY_DIR / "autonomous_auditor.py"}" {stage} "{{prompt_file}}"'
    llm_cmd = os.environ.get(env_var, "").strip() or default_cmd
    prompt_file = relay_mesh.TRAINS_DIR / train_id / prompt_name
    relay_io.write_text_atomic(prompt_file, prompt)
    cmd_str = llm_cmd.replace("{prompt_file}", str(prompt_file))
    import shlex
    try:
        cmd = shlex.split(cmd_str, posix=(sys.platform != "win32"))
    except Exception:
        cmd = cmd_str.split()

    log(f"Delegating {prompt_name} for '{train_id}' to: {' '.join(cmd)}")
    relay_mesh.renew_lease(train_id, agent, additional_ttl=LEASE_TTL)
    try:
        proc = subprocess.run(cmd,
                              shell=False, cwd=str(RELAY_ROOT), timeout=AUDIT_TIMEOUT,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              **relay_io.safe_subprocess_kwargs())
        if proc.returncode != 0 and proc.stderr:
            log(f"Delegate command stderr for '{train_id}': {proc.stderr[:300]}")
        return proc.returncode == 0
    except Exception as exc:
        log(f"Delegate command failed for '{train_id}': {type(exc).__name__}: {exc}")
        return False


def run_plan_audit(train_id: str, revision: int) -> bool:
    key = f"{train_id}::r{revision}::plan-audit"
    if not relay_io.outcome_actionable(key):
        return False
    agent = "antigravity_plan_auditor"
    if not relay_mesh.acquire_lease(train_id, agent, "plan-auditor", ttl_seconds=LEASE_TTL):
        return False
    try:
        train_dir = relay_mesh.TRAINS_DIR / train_id
        state = relay_io.read_json(train_dir / "STATE.json") or {}
        current_status = state.get("status")
        if current_status != "PLAN_AUDIT_PENDING":
            return False
        task_ids = state.get("task_ids", [])
        packet = train_dir / "PACKET.md"
        text = relay_io.read_text(packet)
        if not packet.exists() or [t for t in task_ids if t not in text] or \
                relay_mesh.active_task_overlap(task_ids, exclude_train=train_id):
            record = relay_io.outcome_retry(key, "mechanical precheck failed (packet/tasks/overlap)")
            if record.get("state") == "QUARANTINED":
                _notify_hold_once(key, f"Plan audit quarantined: {train_id}", "Precheck failed repeatedly")
                relay_mesh.transition_train(train_id, "PLAN_AUDIT_HOLD", "plan-auditor", agent, "plan-auditor",
                                            "Quarantined: mechanical precheck failed")
            return False
        if not os.environ.get("RELAY_PLAN_AUDIT_CMD"):
            _notify_hold_once(key, f"Plan audit held: {train_id}",
                              "No critic command configured. Set RELAY_PLAN_AUDIT_CMD or run: "
                              f"python relay/relay_mesh.py audit --train {train_id} --status PASS")
            return False
        ok = _delegate(train_id, "RELAY_PLAN_AUDIT_CMD",
                       PLAN_AUDIT_PROMPT.format(train_id=train_id, task_ids=", ".join(task_ids),
                                                packet_path=packet,
                                                report_path=train_dir / "PLAN_AUDIT_REPORT.md"),
                       "PLAN_AUDIT_PROMPT.txt", agent=agent)
        if not ok:
            record = relay_io.outcome_retry(key, "critic unavailable or nonzero exit")
            if record.get("state") == "QUARANTINED":
                _notify_hold_once(key, f"Plan audit quarantined: {train_id}", "Critic unavailable after max retries")
                relay_mesh.transition_train(train_id, "PLAN_AUDIT_HOLD", "plan-auditor", agent, "plan-auditor",
                                            "Quarantined after max retry attempts on critic invocation")
            return False
        valid, why = relay_mesh.audit_valid(train_id, "PLAN")
        if not valid:
            record = relay_io.outcome_retry(key, why)
            if record.get("state") == "QUARANTINED":
                _notify_hold_once(key, f"Plan audit quarantined: {train_id}", why)
                relay_mesh.transition_train(train_id, "PLAN_AUDIT_HOLD", "plan-auditor", agent, "plan-auditor",
                                            f"Quarantined: {why}")
            return False
        done = relay_mesh.transition_train(train_id, "PLAN_APPROVED", "implementer",
                                           agent, "plan-auditor",
                                           "Plan critiqued by LLM auditor; corrections applied in PACKET.md.")
        if done:
            relay_io.outcome_ack(key)
        return done
    except Exception as exc:
        log(f"Plan audit unhandled exception for {train_id}: {type(exc).__name__}: {exc}")
        record = relay_io.outcome_retry(key, f"Unhandled exception: {type(exc).__name__}: {exc}")
        if record.get("state") == "QUARANTINED":
            _notify_hold_once(key, f"Plan audit quarantined on exception: {train_id}", str(exc))
            relay_mesh.transition_train(train_id, "PLAN_AUDIT_HOLD", "plan-auditor", agent, "plan-auditor",
                                        f"Quarantined on exception: {exc}")
        return False
    finally:
        relay_mesh.release_lease(train_id, agent)


def run_code_audit(train_id: str, revision: int) -> bool:
    train_dir = relay_mesh.TRAINS_DIR / train_id
    state = relay_io.read_json(train_dir / "STATE.json") or {}
    current_status = state.get("status")
    if current_status not in ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"):
        return False

    key = f"{train_id}::r{revision}::code-audit"
    if not relay_io.outcome_actionable(key):
        return False
    agent = "antigravity_auditor"
    if not relay_mesh.acquire_lease(train_id, agent, "auditor", ttl_seconds=LEASE_TTL):
        return False
    try:
        # If in AUDIT_PENDING, perform the claim transition to IN_PROGRESS_AUDITOR.
        # If already IN_PROGRESS_AUDITOR (level-triggered recovery after retry or daemon crash),
        # do NOT repeat the claim transition (avoids self-transition refusal and preserves revision).
        if current_status == "AUDIT_PENDING":
            if not relay_mesh.transition_train(train_id, "IN_PROGRESS_AUDITOR", "auditor",
                                               agent, "auditor", "Supervisor audit claim"):
                relay_io.outcome_retry(key, "claim transition refused")
                return False

        # Mechanical gate first (necessary, never sufficient). Force HARNESS_REPO_DIR
        # to this relay root so the tcp-* scenarios import harness_lib from the complete
        # local mirror (never a lagging harness repo) regardless of the daemon's env.
        gate_env = {**os.environ, "HARNESS_REPO_DIR": str(RELAY_ROOT)}
        # stdin=DEVNULL: the daemon runs headless (pythonw, no console), so an
        # inherited invalid stdin handle makes tests that spawn subprocesses fail
        # with "[WinError 6] The handle is invalid". A real DEVNULL handle cascades
        # valid stdio to pytest and its grandchildren.
        test_proc = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                                   cwd=str(RELAY_ROOT), capture_output=True, text=True,
                                   timeout=900, env=gate_env, stdin=subprocess.DEVNULL,
                                   **relay_io.safe_subprocess_kwargs())
        if test_proc.returncode != 0:
            log(f"MECHANICAL GATE FAILED {train_id}: pytest tests/ rc={test_proc.returncode}\n"
                + (test_proc.stdout or "")[-800:])
            if relay_mesh.transition_train(train_id, "AUDIT_REJECTED", "implementer", agent,
                                        "auditor", "Mechanical gate failed: pytest tests/ nonzero"):
                relay_io.outcome_ack(key, note="rejected: tests red")
            return False
        state = relay_io.read_json(train_dir / "STATE.json") or {}
        packet = train_dir / "PACKET.md"
        digest = relay_io.sha256_hex(packet.read_bytes()) if packet.exists() else ""
        ok = _delegate(train_id, "RELAY_CODE_AUDIT_CMD",
                       CODE_AUDIT_PROMPT.format(train_id=train_id,
                                                task_ids=", ".join(state.get("task_ids", [])),
                                                packet_path=packet, packet_digest=digest[:12],
                                                report_path=train_dir / "AUDIT_REPORT.md"),
                       "CODE_AUDIT_PROMPT.txt", agent=agent)
        if not ok:
            record = relay_io.outcome_retry(key, "critic unavailable or nonzero exit")
            if record.get("state") == "QUARANTINED":
                _notify_hold_once(key, f"Code audit quarantined: {train_id}", "Critic unavailable after max retries")
                relay_mesh.transition_train(train_id, "AUDIT_HOLD", "auditor", agent, "auditor",
                                            "Quarantined after max retry attempts on critic invocation")
            return False
        valid, why = relay_mesh.audit_valid(train_id, "AUDIT")
        report_text = relay_io.read_text(train_dir / "AUDIT_REPORT.md")
        if "AUDIT_VERDICT: FAIL" in report_text:
            reason = report_text.split("AUDIT_VERDICT: FAIL", 1)[1].strip()[:300]
            if relay_mesh.transition_train(train_id, "AUDIT_REJECTED", "implementer", agent,
                                        "auditor", f"LLM audit failed: {reason}"):
                relay_io.outcome_ack(key, note="rejected by critic")
            return False
        if not valid:
            record = relay_io.outcome_retry(key, why)
            if record.get("state") == "QUARANTINED":
                _notify_hold_once(key, f"Code audit quarantined: {train_id}", why)
                relay_mesh.transition_train(train_id, "AUDIT_HOLD", "auditor", agent, "auditor",
                                            f"Quarantined: {why}")
            return False
        done = relay_mesh.transition_train(train_id, "READY_FOR_HUMAN_APPROVAL", "human",
                                           agent, "auditor",
                                           "Mechanical gate green + LLM adversarial audit PASS.")
        if done:
            relay_io.outcome_ack(key)
        return done
    except Exception as exc:
        log(f"Code audit unhandled exception for {train_id}: {type(exc).__name__}: {exc}")
        record = relay_io.outcome_retry(key, f"Unhandled exception: {type(exc).__name__}: {exc}")
        if record.get("state") == "QUARANTINED":
            _notify_hold_once(key, f"Code audit quarantined on exception: {train_id}", str(exc))
            relay_mesh.transition_train(train_id, "AUDIT_HOLD", "auditor", agent, "auditor",
                                        f"Quarantined on exception: {exc}")
        return False
    finally:
        relay_mesh.release_lease(train_id, agent)


def sweep_inboxes(failures: dict) -> None:
    """Non-destructive wakeup sweep: automation-owned role inboxes only.
    Messages are wakeups, never authority — actionable state comes from the
    train scan. processed/ = ACK; quarantine/ after 3 attempts."""
    # Multi-host admission gate (TRAIN-21): a denied/standby host consumes no
    # inbox items (no processed/ ack, no quarantine, no outcome write).
    if not relay_mesh.coordination_authorized()[0]:
        return
    for role_dir in relay_mesh.INBOX_DIR.iterdir() if relay_mesh.INBOX_DIR.exists() else []:
        if not role_dir.is_dir() or role_dir.name in PROTECTED_INBOXES:
            continue
        processed = role_dir / "processed"
        quarantine = role_dir / "quarantine"
        for msg_file in list(role_dir.glob("*.json")):
            try:
                json.loads(msg_file.read_text(encoding="utf-8-sig"))
                processed.mkdir(exist_ok=True)
                msg_file.rename(processed / msg_file.name)
            except Exception as exc:
                count = failures.get(msg_file.name, 0) + 1
                failures[msg_file.name] = count
                if count >= relay_io.MAX_ATTEMPTS:
                    quarantine.mkdir(exist_ok=True)
                    relay_io.write_text_atomic(quarantine / (msg_file.name + ".error.txt"),
                                               f"{type(exc).__name__}: {exc}")
                    msg_file.rename(quarantine / msg_file.name)
                    failures.pop(msg_file.name, None)
                    log(f"QUARANTINED malformed message {msg_file.name}: {exc}")


AUTOPLAN_COOLDOWN = 300  # s; long enough for the planner to turn a trigger into a train
AUTOPLAN_STATE = RELAY_DIR / "history" / "autoplan-state.json"


AUTOPLAN_SKIP = RELAY_DIR / "history" / "autoplan-skip.json"


def _skip_ids() -> set:
    """Frontier items the auto-planner must NOT nominate (deferred/blocked-on-human).
    Prevents re-spawning trains that will just BLOCK again (agy guard, 2026-08-16)."""
    data = relay_io.read_json(AUTOPLAN_SKIP) or {}
    return set(data.get("skip", []))


def _top_frontier() -> str | None:
    """The highest-ranked actionable frontier task id NOT on the skip-list, or None."""
    try:
        graph_ops = RELAY_ROOT / "satellites" / "tare.tools.backlog-graph" / "graph_ops.py"
        proc = subprocess.run(
            [sys.executable, str(graph_ops), "--graph", str(relay_mesh.WORK_GRAPH_FILE),
             "next", "--limit", "8"], capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL, env={**os.environ, "HARNESS_REPO_DIR": str(RELAY_ROOT)},
            **relay_io.safe_subprocess_kwargs())
        data = json.loads(proc.stdout)
        items = data if isinstance(data, list) else data.get("items", data.get("frontier", []))
        skip = _skip_ids()
        for item in items:
            if item.get("id") not in skip:
                return item["id"]
        return None
    except Exception:
        return None


def maybe_autoplan() -> int:
    """Self-perpetuate: when the cluster has available planning capacity and the
    frontier has work, nudge the planner to start the next train. Time-deduplicated so
    it never spams while the planner is mid-plan or if codex is unavailable."""
    MAX_CONCURRENT_TRAINS = 4
    active_count = 0
    for td in relay_mesh.TRAINS_DIR.iterdir() if relay_mesh.TRAINS_DIR.exists() else []:
        state = relay_io.read_json(td / "STATE.json")
        if isinstance(state, dict) and not state.get("tombstone"):
            status = state.get("status", "")
            if status not in (relay_mesh.TERMINAL_STATUS, "DEFERRED_BLOCKED_DIRTY_TARGET",
                              "DEFERRED_BLOCKED_RAW_EXPORT", "DEFERRED_BLOCKED_CI_REGRESSION_02"):
                active_count += 1
    if active_count >= MAX_CONCURRENT_TRAINS:
        return 0  # cluster capacity saturated; pause auto-planning
    top = _top_frontier()
    if not top:
        return 0
    prev = relay_io.read_json(AUTOPLAN_STATE) or {}
    if prev.get("item") == top and time.time() - float(prev.get("sentAt", 0)) < AUTOPLAN_COOLDOWN:
        return 0  # already triggered recently for this item
    relay_mesh.send_inbox_msg("planner", f"INIT_TRAIN: plan next frontier item ({top})",
        {"action": "INIT_TRAIN", "top_task": top,
         "note": "Auto-trigger: board idle and frontier has work. Initialize one "
                 "non-overlapping train for this item and dispatch to the plan-auditor."},
        required_action="notify")
    relay_io.write_json_atomic(AUTOPLAN_STATE, {"item": top, "sentAt": time.time()})
    log(f"AUTO-PLAN: board idle -> nudged planner for '{top}'")
    return 1


def reconcile() -> int:
    """One level-triggered pass over actionable train states."""
    effects = 0
    # 0. Sentinel self-healing & lease reaper
    try:
        from relay import sentinel
        effects += sentinel.run_sentinel_cycle()
    except Exception as exc:
        log(f"Sentinel error (contained): {type(exc).__name__}: {exc}")

    # 1. Update host hardware resource telemetry
    try:
        from relay import resource_telemetry
        resource_telemetry.broadcast_heartbeat()
    except Exception:
        pass

    # Auto-Sentinel self-healing: reap deadlocks, expired leases, and stuck trains on every tick
    try:
        from relay import sentinel
        reaped_leases = sentinel.reap_expired_leases()
        reaped_deadlocks = sentinel.reap_deadlocks()
        if reaped_leases or reaped_deadlocks:
            log(f"[SELF-HEALING SENTINEL] Reaped {reaped_leases} expired lease(s) and {reaped_deadlocks} deadlock(s).")
            effects += (reaped_leases + reaped_deadlocks)
    except Exception as exc:
        log(f"Sentinel self-healing error (contained): {exc}")

    # Autonomous Planner execution: handled natively by the supervisor host
    try:
        from relay import autonomous_planner
        effects += autonomous_planner.process_planner_inbox()
    except Exception as exc:
        log(f"Autonomous planner error (contained): {type(exc).__name__}: {exc}")

    for td in relay_mesh.TRAINS_DIR.iterdir() if relay_mesh.TRAINS_DIR.exists() else []:
        state = relay_io.read_json(td / "STATE.json")
        if not isinstance(state, dict) or state.get("tombstone"):
            continue
        status = state.get("status")
        train_id = state.get("train_id", td.name)
        revision = relay_mesh.train_revision(state)
        if status == "PLAN_AUDIT_PENDING":
            effects += bool(run_plan_audit(train_id, revision))
        elif status in ("PLAN_APPROVED", "AUDIT_REJECTED"):
            try:
                from relay import autonomous_implementer
                effects += bool(autonomous_implementer.implement_train(train_id))
            except Exception as exc:
                log(f"Autonomous implementer error (contained): {type(exc).__name__}: {exc}")
        elif status in ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"):
            effects += bool(run_code_audit(train_id, revision))
    effects += relay_mesh.process_drive_approvals()
    try:
        effects += intake_processor.sweep_human()
    except Exception as exc:
        log(f"Intake sweep error: {exc}")
    effects += maybe_autoplan()  # self-perpetuate when idle + frontier has work
def reconcile_worker_tasks() -> int:
    """Executes implementation and code audit workloads as a worker node (excludes plan audit & planning)."""
    effects = 0
    # Update local hardware telemetry
    try:
        from relay import resource_telemetry
        resource_telemetry.broadcast_heartbeat()
    except Exception:
        pass

    for td in relay_mesh.TRAINS_DIR.iterdir() if relay_mesh.TRAINS_DIR.exists() else []:
        state = relay_io.read_json(td / "STATE.json")
        if not isinstance(state, dict) or state.get("tombstone"):
            continue
        status = state.get("status")
        train_id = state.get("train_id", td.name)
        revision = relay_mesh.train_revision(state)

        # Check if train already has an active lease on another host
        lease_file = relay_mesh.LEASES_DIR / f"{train_id}.lease.json"
        if lease_file.exists():
            l_data = relay_io.read_json(lease_file) or {}
            if l_data.get("expires_ts", 0) > time.time() and l_data.get("hostname") != relay_mesh.get_hostname():
                continue  # Train is actively held by another host

        # Workers only process implementation and code audit (no plan audit or central planning)
        if status in ("PLAN_APPROVED", "AUDIT_REJECTED"):
            try:
                from relay import autonomous_implementer
                effects += bool(autonomous_implementer.implement_train(train_id))
            except Exception as exc:
                log(f"Autonomous implementer error (contained): {type(exc).__name__}: {exc}")
        elif status in ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"):
            effects += bool(run_code_audit(train_id, revision))

    return effects


IS_LEADER_ACTIVE = False
CURRENT_EPOCH = 1
LAST_RECONCILE_TS = 0.0


def start_async_heartbeat_thread() -> threading.Thread:
    """Spawns an independent heartbeat thread that beats every 10s, immune to task execution blocking."""
    def _beat():
        while True:
            try:
                if IS_LEADER_ACTIVE:
                    heartbeat(LAST_RECONCILE_TS, CURRENT_EPOCH)
            except Exception:
                pass
            time.sleep(10)
    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    return t


def run_daemon(force_takeover: bool = False) -> None:
    global IS_LEADER_ACTIVE, LAST_RECONCILE_TS, CURRENT_EPOCH
    relay_io.utf8_console()

    # Multi-host admission gate (TRAIN-21). Fail closed BEFORE any effect
    # (no ensure_dirs, heartbeat, sweep, reconcile, or delegation) when this host
    # is not the pre-provisioned coordinator. Bounded, zero-effect exit; no
    # per-tick alert spam. This is not automatic failover — promotion requires
    # the out-of-band owner runbook (see AGENT_RELAY_PROTOCOL.md).
    multi_host = relay_mesh.multi_host_enabled()
    if multi_host:
        ok, why = relay_mesh.coordination_authorized()
        if not ok:
            IS_LEADER_ACTIVE = False
            log(f"[STANDBY / DENIED] Multi-host safe mode: this host is NOT the "
                f"authorized coordinator — running with ZERO effects and exiting. {why}")
            return

    relay_mesh.ensure_dirs()

    if another_supervisor_live():
        hb = relay_io.read_json(HEARTBEAT_FILE) or {}
        other_host = hb.get("host", "unknown")
        if not force_takeover:
            log(f"Active Supervisor Leader detected on {other_host} (epoch={hb.get('epoch', 1)}). Running in STANDBY / COCKPIT MODE on {relay_mesh.get_hostname()}...")
            IS_LEADER_ACTIVE = False
        else:
            prev_epoch = int(hb.get("epoch", 1))
            CURRENT_EPOCH = prev_epoch + 1
            log(f"[LEADER TAKEOVER] Authoritatively transferring supervisor leadership from {other_host} to {relay_mesh.get_hostname()} (epoch {prev_epoch} -> {CURRENT_EPOCH})...")
            IS_LEADER_ACTIVE = True
            heartbeat(time.time(), CURRENT_EPOCH)
    else:
        CURRENT_EPOCH = get_current_epoch() + 1
        IS_LEADER_ACTIVE = True
        heartbeat(time.time(), CURRENT_EPOCH)

    # Start independent non-blocking heartbeat thread
    start_async_heartbeat_thread()

    # Start Owner Cockpit Web UI V0.1 in background (available on all nodes)
    try:
        from relay import cockpit_server
        cockpit_server.start_cockpit_server()
    except Exception as exc:
        log(f"Cockpit server start error (contained): {exc}")

    role_str = f"LEADER SUPERVISOR (epoch={CURRENT_EPOCH})" if IS_LEADER_ACTIVE else "STANDBY COCKPIT NODE"
    log(f"Relay Swarm Node v{VERSION} active as [{role_str}] (tick={TICK_SECONDS}s, pid={os.getpid()}).")
    failures: dict = {}

    while True:
        try:
            # Dynamic re-check (plan correction #1 / FAL-06): a coordinator
            # credential removed mid-run halts all effects on the next tick.
            if multi_host and not relay_mesh.coordination_authorized()[0]:
                if IS_LEADER_ACTIVE:
                    log("[HALT] Coordinator authorization lost mid-run; ceasing all "
                        "effects (no heartbeat, sweep, reconcile, or delegation).")
                IS_LEADER_ACTIVE = False
                time.sleep(TICK_SECONDS)
                continue
            if IS_LEADER_ACTIVE:
                sweep_inboxes(failures)
                effects = reconcile()
                if effects:
                    LAST_RECONCILE_TS = time.time()
            else:
                # Standby node: publish local hardware telemetry and allow web intake
                try:
                    from relay import resource_telemetry
                    resource_telemetry.broadcast_heartbeat()
                except Exception:
                    pass
        except Exception as exc:
            log(f"Node loop error (contained): {type(exc).__name__}: {exc}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    force = "--force-takeover" in sys.argv or "-f" in sys.argv
    run_daemon(force_takeover=force)
