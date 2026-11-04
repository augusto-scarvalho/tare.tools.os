#!/usr/bin/env python3
"""
relay/cluster_worker.py — tare.tools Distributed Autonomous Worker Node
======================================================================
Connects to the Central Cluster Coordinator over Tailscale RPC (or localhost),
broadcasts local hardware telemetry, claims available trains atomically,
and executes Claude Opus / Antigravity / Pytest workloads with zero human questions.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import resource_telemetry, relay_mesh, autonomous_implementer, autonomous_auditor, vendor_chairs

def find_coordinator_url() -> str:
    env_url = os.environ.get("RELAY_COORDINATOR_URL")
    if env_url:
        return env_url.rstrip("/")
    candidates = resource_telemetry.coordinator_candidates()
    for cand in candidates:
        try:
            req = urllib.request.Request(f"{cand}/api/state")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return cand
        except Exception:
            pass
    return candidates[-1]


COORDINATOR_URL = find_coordinator_url()
POLL_INTERVAL = int(os.environ.get("RELAY_WORKER_POLL_INTERVAL", "10"))
AGENT_ID = os.environ.get("RELAY_WORKER_AGENT", f"worker_{relay_mesh.get_hostname()}")
MAX_CONCURRENT_JOBS = int(os.environ.get("RELAY_WORKER_CONCURRENCY", "2"))

ACTIVE_JOBS_LOCK = threading.Lock()
ACTIVE_JOBS: set[str] = set()


def compose_agent_runtime(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """The single authorized composition seam; no execution or turn policy lives here."""
    raise ValueError("RETIRED_AIDER_COMPOSITION: use native_work_host or the archived host")


def prepare_strict_agent_runtime(*args: Any, **kwargs: Any) -> Any:
    """Forward to the sole strict composition seam without adding loop policy."""
    raise ValueError("RETIRED_AIDER_COMPOSITION: use native_work_host or the archived host")


def _post_json(url: str, data: Dict[str, Any], timeout: int = 10) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        return {"ok": False, "status_code": err.code, "reason": str(err)}
    except Exception as exc:
        return None


def _get_json(url: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def start_telemetry_beacon_thread(coordinator_url: Optional[str] = None) -> threading.Thread:
    """Periodically pushes local CPU, RAM, and GPU telemetry to the Coordinator."""
    def _beacon():
        while True:
            try:
                c_url = coordinator_url or find_coordinator_url()
                telem = resource_telemetry.probe_host_telemetry()
                _post_json(f"{c_url}/api/v1/nodes/heartbeat", telem, timeout=5)
            except Exception:
                pass
            time.sleep(15)

    t = threading.Thread(target=_beacon, daemon=True)
    t.start()
    return t


def start_job_heartbeat_thread(train_id: str, token: str, stop_event: threading.Event, coordinator_url: Optional[str] = None) -> threading.Thread:
    """Continuously extends the worker's active job lease while compute is executing."""
    def _renew():
        while not stop_event.is_set():
            time.sleep(15)
            if stop_event.is_set():
                break
            try:
                c_url = coordinator_url or find_coordinator_url()
                _post_json(f"{c_url}/api/v1/jobs/heartbeat", {
                    "train_id": train_id,
                    "lease_token": token,
                    "ttl_seconds": 120
                }, timeout=5)
            except Exception:
                pass

    t = threading.Thread(target=_renew, daemon=True)
    t.start()
    return t


def _execute_train_worker(tid: str, role: str, status: str, token: str, coord_url: str) -> None:
    stop_hb = threading.Event()
    hb_thread = start_job_heartbeat_thread(tid, token, stop_hb, coordinator_url=coord_url)
    try:
        train_dir = relay_mesh.TRAINS_DIR / tid
        if role == "implementer":
            from relay.execution_preflight import (
                configured_mode,
                preflight_train_execution,
                record_shadow_preflight,
            )

            mode = configured_mode()
            repository_root = os.environ.get("TARE_EXECUTION_REPOSITORY_ROOT")
            preflight = preflight_train_execution(
                train_dir,
                work_graph_file=relay_mesh.WORK_GRAPH_FILE,
                repository_root=repository_root,
                mode=mode,
            )
            if mode == "shadow":
                record_shadow_preflight(train_dir, preflight)
            elif mode == "strict":
                if os.environ.get("TARE_STRICT_QUALIFICATION") != "1":
                    raise RuntimeError("STRICT_CUTOVER_NOT_AUTHORIZED")
                if not preflight.admissible:
                    raise RuntimeError(preflight.reason)
                raise RuntimeError("STRICT_QUALIFICATION_REQUIRES_DISPOSABLE_RUNNER")
            print(f"[{time.strftime('%H:%M:%S')}] ⚡ Starting autonomous implementation for '{tid}'...")
            success = autonomous_implementer.implement_train(tid, force=True)
            print(f"[{time.strftime('%H:%M:%S')}] Implementation result for '{tid}': {'SUCCESS' if success else 'RETRY'}")
        
        elif role == "plan-auditor":
            print(f"[{time.strftime('%H:%M:%S')}] 📋 Starting autonomous plan audit for '{tid}'...")
            packet = train_dir / "PACKET.md"
            prompt_file = train_dir / "PLAN_AUDIT_PROMPT.txt"
            prompt_text = f"Audit proposed plan packet for train {tid}.\nPacket: {packet}\nReport path: {train_dir / 'PLAN_AUDIT_REPORT.md'}"
            relay_io.write_text_atomic(prompt_file, prompt_text)
            rc = autonomous_auditor.audit("plan-audit", prompt_file)
            if rc == 0:
                valid, why = relay_mesh.audit_valid(tid, "PLAN")
                if valid:
                    relay_mesh.transition_train(
                        tid, "PLAN_APPROVED", "implementer",
                        AGENT_ID, "plan-auditor",
                        "Plan approved by autonomous auditor worker."
                    )
                    print(f"[{time.strftime('%H:%M:%S')}] ✅ '{tid}' plan approved.")
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Plan audit report invalid for '{tid}': {why}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ❌ Plan audit failed with exit code {rc}")

        elif role == "auditor":
            print(f"[{time.strftime('%H:%M:%S')}] 🔍 Starting autonomous code audit for '{tid}'...")
            packet = train_dir / "PACKET.md"
            prompt_file = train_dir / "CODE_AUDIT_PROMPT.txt"
            digest = relay_io.sha256_hex(packet.read_bytes()) if packet.exists() else ""
            candidate_context = ""
            state = relay_io.read_json(train_dir / "STATE.json") or {}
            if state.get("execution_descriptor") is not None:
                descriptor, receipt, repository_root, _ready = relay_mesh._strict_candidate_context(
                    train_dir, state
                )
                candidate_context = (
                    f"\nImmutable candidate: {receipt.payload['candidate_commit']}"
                    f"\nRepository: {repository_root}"
                    "\nAudit this object identity; do not infer candidate bytes from cwd."
                )
                evidence_file = train_dir / "LANDING_EVIDENCE.json"
                identity_file = train_dir / "IMPLEMENTER_IDENTITY.json"
                if not evidence_file.exists():
                    try:
                        relay_mesh.materialize_admission_evidence(tid)
                    except Exception as exc:
                        print(
                            f"[{time.strftime('%H:%M:%S')}] smart evidence "
                            f"materialization failed for '{tid}': {exc}"
                        )
                        return
                if not identity_file.exists():
                    print(
                        f"[{time.strftime('%H:%M:%S')}] strict implementer identity "
                        f"missing for '{tid}'; holding before provider audit."
                    )
                    return
                pre_audit = relay_mesh.prepare_admission_verdict(tid)
                if pre_audit.verdict == "ADMITTED":
                    if relay_mesh.admit_train(tid):
                        print(
                            f"[{time.strftime('%H:%M:%S')}] '{tid}' landed by "
                            "smart checks; optional model audit was not invoked."
                        )
                    return
                if pre_audit.verdict == "REFUSED":
                    print(
                        f"[{time.strftime('%H:%M:%S')}] strict admission refused "
                        f"for '{tid}': {pre_audit.payload['reason_codes']}"
                    )
                    return
            prompt_text = f"Audit code implementation for train {tid}.\nPacket: {packet} (digest: {digest[:12]}){candidate_context}\nReport path: {train_dir / 'AUDIT_REPORT.md'}"
            relay_io.write_text_atomic(prompt_file, prompt_text)
            rc = autonomous_auditor.audit("code-audit", prompt_file)
            if rc == 0:
                valid, why = relay_mesh.audit_valid(tid, "AUDIT")
                report_text = relay_io.read_text(train_dir / "AUDIT_REPORT.md")
                if "AUDIT_VERDICT: FAIL" in report_text:
                    reason = report_text.split("AUDIT_VERDICT: FAIL", 1)[1].strip()[:300]
                    relay_mesh.transition_train(
                        tid, "AUDIT_REJECTED", "implementer",
                        AGENT_ID, "auditor", f"Audit failed: {reason}"
                    )
                    print(f"[{time.strftime('%H:%M:%S')}] 🛑 '{tid}' rejected by auditor.")
                elif valid:
                    if state.get("execution_descriptor") is not None:
                        post_audit = relay_mesh.prepare_admission_verdict(tid)
                        if post_audit.verdict == "ADMITTED" and relay_mesh.admit_train(tid):
                            print(
                                f"[{time.strftime('%H:%M:%S')}] '{tid}' landed by "
                                "smart policy with independent audit."
                            )
                        else:
                            print(
                                f"[{time.strftime('%H:%M:%S')}] strict admission held "
                                f"for '{tid}': {post_audit.payload['reason_codes']}"
                            )
                    else:
                        relay_mesh.transition_train(
                            tid, "READY_FOR_HUMAN_APPROVAL", "human",
                            AGENT_ID, "auditor", "Mechanical & adversarial audit VERIFIED_PASS."
                        )
                        print(
                            f"[{time.strftime('%H:%M:%S')}] '{tid}' moved to "
                            "READY_FOR_HUMAN_APPROVAL."
                        )
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Audit report invalid for '{tid}': {why}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ❌ Code audit failed with exit code {rc}")

    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] ❌ Error during execution of '{tid}': {exc}")
    finally:
        stop_hb.set()
        _post_json(f"{coord_url}/api/v1/jobs/complete", {
            "train_id": tid,
            "lease_token": token
        })
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.discard(tid)
        print(f"[{time.strftime('%H:%M:%S')}] 🏁 Finished execution cycle for '{tid}'.")


def run_worker_loop(allowed_roles: Optional[List[str]] = None):
    relay_io.utf8_console()
    coord_url = find_coordinator_url()
    hostname = relay_mesh.get_hostname()
    roles_str = ", ".join(allowed_roles) if allowed_roles else "ALL (implementer, plan-auditor, auditor)"
    print("=" * 70)
    print(f"  🚀 MARM CLUSTER WORKER INITIALIZED ON HOST '{hostname}'")
    print(f"  📡 Connected to Coordinator: {coord_url}")
    print(f"  ⚙️ Max Concurrent Compute Slots: {MAX_CONCURRENT_JOBS}")
    print(f"  🎭 Allowed Worker Roles: {roles_str}")
    print("=" * 70)

    # Start continuous host telemetry reporting
    start_telemetry_beacon_thread(coordinator_url=coord_url)

    while True:
        try:
            with ACTIVE_JOBS_LOCK:
                current_active_count = len(ACTIVE_JOBS)

            if current_active_count < MAX_CONCURRENT_JOBS:
                data = _get_json(f"{coord_url}/api/v1/jobs/available", timeout=5)
                if data and data.get("ok"):
                    jobs = data.get("jobs", [])
                    for job in jobs:
                        tid = job["train_id"]
                        status = job["status"]

                        with ACTIVE_JOBS_LOCK:
                            if len(ACTIVE_JOBS) >= MAX_CONCURRENT_JOBS:
                                break
                            if tid in ACTIVE_JOBS:
                                continue

                        target_role = None
                        if status in ("PLAN_APPROVED", "AUDIT_REJECTED"):
                            target_role = "implementer"
                        elif status == "PLAN_AUDIT_PENDING":
                            target_role = "plan-auditor"
                        elif status in ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"):
                            target_role = "auditor"

                        if target_role:
                            if allowed_roles and target_role not in allowed_roles:
                                continue

                            claim_res = _post_json(f"{coord_url}/api/v1/jobs/claim", {
                                "train_id": tid,
                                "role": target_role,
                                "hostname": hostname,
                                "agent_id": AGENT_ID,
                                "ttl_seconds": 180
                            })

                            if claim_res and claim_res.get("ok"):
                                claim_info = claim_res.get("claim", {})
                                token = claim_info.get("lease_token")
                                print(f"[{time.strftime('%H:%M:%S')}] 🎯 CLAIMED {target_role} job '{tid}' (token: {token[:8]}...). Spawning worker thread...")
                                with ACTIVE_JOBS_LOCK:
                                    ACTIVE_JOBS.add(tid)
                                
                                t = threading.Thread(
                                    target=_execute_train_worker,
                                    args=(tid, target_role, status, token, coord_url),
                                    daemon=True
                                )
                                t.start()
        except Exception as exc:
            print(f"[WORKER LOOP ERROR]: {exc}", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    roles = None
    for i, arg in enumerate(sys.argv):
        if arg in ("--roles", "-r") and i + 1 < len(sys.argv):
            roles = [r.strip() for r in sys.argv[i + 1].split(",")]
    if roles is None and relay_mesh.get_hostname() == "aaaaa":
        roles = ["implementer"]
    run_worker_loop(allowed_roles=roles)
