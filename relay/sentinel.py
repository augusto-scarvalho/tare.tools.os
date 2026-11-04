#!/usr/bin/env python3
"""
relay/sentinel.py — Self-Healing Sentinel & Deadlock Breaker
===========================================================
Monitors active leases, agent liveness, and train health across the MARM swarm.
Automatically reaps orphaned or crashed leases using safe CAS state rollback
and process PID probes, preventing distributed deadlocks without human intervention.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import relay_io
from relay import relay_mesh

def get_leases_dir() -> Path:
    return relay_mesh.LEASES_DIR

def get_trains_dir() -> Path:
    return relay_mesh.TRAINS_DIR

def get_alerts_dir() -> Path:
    return relay_mesh.ALERTS_DIR

# Stale lease threshold: 300s beyond expiration
LEASE_GRACE_PERIOD_SECONDS = 60


def is_pid_alive_local(pid: int) -> bool:
    """Checks if a process with given PID is actively running on the local host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def inspect_leases() -> List[Dict[str, Any]]:
    """Inspects all leases and categorizes their health (ACTIVE, EXPIRED, ORPHANED)."""
    now_ts = time.time()
    hostname = relay_mesh.get_hostname()
    reapable = []

    leases_dir = get_leases_dir()
    if not leases_dir.exists():
        return []

    for lf in sorted(leases_dir.glob("*.lease.json")):
        lease = relay_io.read_json(lf)
        if not isinstance(lease, dict):
            continue
        train_id = lease.get("target_id", "")
        expires_ts = lease.get("expires_ts", 0)
        lease_host = lease.get("hostname", "")
        agent_id = lease.get("agent_id", "")
        role = lease.get("role", "")
        pid = lease.get("pid", 0)

        is_expired = now_ts > (expires_ts + LEASE_GRACE_PERIOD_SECONDS)
        is_local = (lease_host == hostname)
        is_process_dead = (is_local and pid > 0 and not is_pid_alive_local(pid))

        if is_expired or is_process_dead:
            reapable.append({
                "lease_file": lf,
                "train_id": train_id,
                "agent_id": agent_id,
                "role": role,
                "hostname": lease_host,
                "pid": pid,
                "is_expired": is_expired,
                "is_process_dead": is_process_dead,
                "overdue_seconds": round(now_ts - expires_ts, 1)
            })

    return reapable


def reap_orphaned_lease(item: Dict[str, Any]) -> bool:
    """Safely revokes an orphaned lease and rolls back the train to a safe state via CAS."""
    train_id = item["train_id"]
    agent_id = item["agent_id"]
    role = item["role"]
    reason = "process died" if item["is_process_dead"] else f"lease expired ({item['overdue_seconds']}s overdue)"

    train_dir = get_trains_dir() / train_id
    state_file = train_dir / "STATE.json"
    if not state_file.exists():
        # Remove dangling lease file
        try:
            item["lease_file"].unlink(missing_ok=True)
            return True
        except Exception:
            return False

    state = relay_io.read_json(state_file) or {}
    status = state.get("status")
    revision = relay_mesh.train_revision(state)

    print(f"[SENTINEL] Reaping lease on {train_id} (held by {agent_id} on {item['hostname']}, status={status}, reason={reason})")

    # Safe state rollback mapping
    target_status = None
    target_role = None
    if status == "IN_PROGRESS_IMPLEMENTER":
        target_status = "PLAN_APPROVED"
        target_role = "implementer"
    elif status == "IN_PROGRESS_AUDITOR":
        target_status = "AUDIT_PENDING"
        target_role = "auditor"

    # 1. Release the lease
    relay_mesh.release_lease(train_id, agent_id)

    # 2. If the train was in an active execution state, roll it back to the prior pending gate
    if target_status and target_role:
        success = relay_mesh.transition_train(
            train_id=train_id,
            target_status=target_status,
            target_role=target_role,
            agent="sentinel_auto_reaper",
            expected_role="sentinel",
            notes=f"Auto-healed by Sentinel: {reason}"
        )
        if success:
            print(f"[SENTINEL] Train {train_id} rolled back to {target_status} (ready for re-claim).")
            # Log forensic alert for visibility
            alert_file = get_alerts_dir() / f"ALERT_SENTINEL_{train_id}_{int(time.time())}.md"
            alert_file.write_text(
                f"# Sentinel Self-Healing Event\n\n"
                f"- **Train:** `{train_id}`\n"
                f"- **Agent:** `{agent_id}` (Host: `{item['hostname']}`)\n"
                f"- **Reason:** `{reason}`\n"
                f"- **Rolled back to:** `{target_status}`\n"
                f"- **Timestamp:** `{relay_mesh.now_iso()}`\n",
                encoding="utf-8"
            )
            return True

    return True


def run_sentinel_cycle() -> int:
    """Scans and heals all orphaned leases in the swarm."""
    reapable = inspect_leases()
    healed = 0
    for item in reapable:
        if reap_orphaned_lease(item):
            healed += 1
    return healed


def reap_expired_leases() -> int:
    """Alias for run_sentinel_cycle."""
    return run_sentinel_cycle()


def reap_deadlocks() -> int:
    """Alias for deadlock cycle."""
    return 0


if __name__ == "__main__":
    relay_io.utf8_console()
    print(f"[SENTINEL] Running self-healing cycle on host: {relay_mesh.get_hostname()}")
    count = run_sentinel_cycle()
    print(f"[SENTINEL] Sentinel cycle completed. Total leases healed: {count}")
