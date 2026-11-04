#!/usr/bin/env python3
"""
relay/process_all_pending_audits.py — Comprehensive Autonomous Swarm Auditor
===========================================================================
Audits all trains currently waiting in PLAN_AUDIT_PENDING or AUDIT_PENDING
using the autonomous adversarial auditor (agy / Antigravity), strictly honoring
the MARM FSM and lease-credential evidence gates.
"""
import os
import sys
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))

from relay import relay_io, relay_mesh, autonomous_auditor

def run_audits():
    relay_io.utf8_console()
    print("=" * 80)
    print("  🚀 PROCESSING ALL PENDING PLAN & CODE AUDITS IN CLUSTER")
    print("=" * 80)

    trains = sorted(relay_mesh.TRAINS_DIR.glob("TRAIN-*"))
    for td in trains:
        if not td.is_dir():
            continue
        st_file = td / "STATE.json"
        if not st_file.exists():
            continue
        st = relay_io.read_json(st_file) or {}
        status = st.get("status")
        tid = st.get("train_id", td.name)
        rev = st.get("revision", 1)

        if status == "PLAN_AUDIT_PENDING":
            print(f"\n[{time.strftime('%H:%M:%S')}] 📋 [PLAN AUDIT] Processing '{tid}' (rev={rev})...")
            packet = td / "PACKET.md"
            if not packet.exists():
                print(f"  ❌ Missing PACKET.md for '{tid}', skipping.")
                continue
            
            agent = "antigravity_plan_auditor"
            if not relay_mesh.acquire_lease(tid, agent, "plan-auditor", ttl_seconds=300):
                print(f"  ⚠️ Could not acquire lease for '{tid}', skipping.")
                continue

            try:
                prompt_file = td / "PLAN_AUDIT_PROMPT.txt"
                prompt_text = f"Audit proposed plan packet for train {tid}.\nPacket: {packet}\nReport path: {td / 'PLAN_AUDIT_REPORT.md'}"
                relay_io.write_text_atomic(prompt_file, prompt_text)
                
                # Run plan audit
                rc = autonomous_auditor.audit("plan-audit", prompt_file)
                if rc == 0:
                    valid, why = relay_mesh.audit_valid(tid, "PLAN")
                    if valid:
                        done = relay_mesh.transition_train(
                            tid, "PLAN_APPROVED", "implementer",
                            agent, "plan-auditor",
                            "Plan approved by autonomous adversarial auditor (agy)."
                        )
                        print(f"  ✅ '{tid}' transitioned to PLAN_APPROVED: {done}")
                    else:
                        print(f"  ⚠️ Plan audit invalid: {why}")
                else:
                    print(f"  ❌ Plan audit returned exit code {rc}")
            finally:
                relay_mesh.release_lease(tid, agent)

        elif status in ("AUDIT_PENDING", "IN_PROGRESS_AUDITOR"):
            print(f"\n[{time.strftime('%H:%M:%S')}] 🔍 [CODE AUDIT] Processing '{tid}' (rev={rev})...")
            agent = "antigravity_auditor"
            if not relay_mesh.acquire_lease(tid, agent, "auditor", ttl_seconds=600):
                print(f"  ⚠️ Could not acquire lease for '{tid}', skipping.")
                continue

            try:
                if status == "AUDIT_PENDING":
                    claim_ok = relay_mesh.transition_train(
                        tid, "IN_PROGRESS_AUDITOR", "auditor",
                        agent, "auditor", "Audit claim"
                    )
                    if not claim_ok:
                        print(f"  ⚠️ Could not transition '{tid}' to IN_PROGRESS_AUDITOR.")
                        continue

                packet = td / "PACKET.md"
                prompt_file = td / "CODE_AUDIT_PROMPT.txt"
                digest = relay_io.sha256_hex(packet.read_bytes()) if packet.exists() else ""
                prompt_text = f"Audit code implementation for train {tid}.\nPacket: {packet} (digest: {digest[:12]})\nReport path: {td / 'AUDIT_REPORT.md'}"
                relay_io.write_text_atomic(prompt_file, prompt_text)
                
                # Run code audit
                rc = autonomous_auditor.audit("code-audit", prompt_file)
                if rc == 0:
                    valid, why = relay_mesh.audit_valid(tid, "AUDIT")
                    report_text = relay_io.read_text(td / "AUDIT_REPORT.md")
                    if "AUDIT_VERDICT: FAIL" in report_text:
                        reason = report_text.split("AUDIT_VERDICT: FAIL", 1)[1].strip()[:300]
                        relay_mesh.transition_train(
                            tid, "AUDIT_REJECTED", "implementer",
                            agent, "auditor",
                            f"LLM audit rejected: {reason}"
                        )
                        print(f"  🛑 '{tid}' REJECTED by auditor: {reason[:100]}")
                    elif valid:
                        done = relay_mesh.transition_train(
                            tid, "READY_FOR_HUMAN_APPROVAL", "human",
                            agent, "auditor",
                            "Mechanical gate passed + LLM adversarial audit VERIFIED_PASS."
                        )
                        print(f"  🏆 '{tid}' transitioned to READY_FOR_HUMAN_APPROVAL: {done}")
                    else:
                        print(f"  ⚠️ Code audit report invalid: {why}")
                else:
                    print(f"  ❌ Code audit returned exit code {rc}")
            finally:
                relay_mesh.release_lease(tid, agent)

    relay_mesh.update_board()
    print(f"\n[{time.strftime('%H:%M:%S')}] 🏁 Finished audit sweep across cluster.")

if __name__ == "__main__":
    run_audits()
