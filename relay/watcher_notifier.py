#!/usr/bin/env python3
"""
tare.tools Enhanced Multi-Surface Relay Notifier (v3.0 - Zero Rubber-Stamping)
Monitors relay/inbox/, relay/trains/, relay/approvals/, and root intake artifacts over Google Drive.
Emits structured wakeup alerts whenever plans, code audits, initiatives, or mobile approvals arrive.
"""

from __future__ import annotations
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
MIN_POLL_SECONDS = 15  # TRAIN-06 Seam B.4: mandatory floor; notifications only,
                       # never landing authority
INBOX_DIR = RELAY_DIR / "inbox"
APPROVALS_DIR = RELAY_DIR / "approvals"
TRAINS_DIR = RELAY_DIR / "trains"

def read_json_safe(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}

def check_for_events() -> list[str]:
    events = []
    
    # 1. Check for Mobile / Drive approvals
    if APPROVALS_DIR.exists():
        drive_approvals = [f for f in APPROVALS_DIR.glob("APPROVED_*") if f.is_file()] + \
                          [f for f in APPROVALS_DIR.glob("APPROVE_*") if f.is_file() and f.suffix in [".md", ".txt"]]
        for f in drive_approvals:
            events.append(f"📱 [MOBILE APPROVAL] Ready to land: {f.name}")

    # 2. Check for all Inbox messages across all roles
    if INBOX_DIR.exists():
        for role_dir in INBOX_DIR.iterdir():
            if role_dir.is_dir() and not role_dir.name.startswith("."):
                for msg_file in role_dir.glob("*.json"):
                    data = read_json_safe(msg_file)
                    subj = data.get("subject", msg_file.name)
                    payload = data.get("payload", {})
                    train_id = payload.get("train_id")
                    init_id = payload.get("initiative_id")
                    target = train_id or init_id or "General"
                    
                    if role_dir.name in ["plan-auditor", "antigravity", "planner"]:
                        events.append(f"📋 [PLAN/INTAKE] Role '{role_dir.name}' target '{target}': {subj}")
                    elif role_dir.name == "auditor":
                        events.append(f"🛡️ [CODE AUDIT] Role 'auditor' target '{target}': {subj}")
                    elif role_dir.name == "human":
                        events.append(f"👤 [HUMAN ESCALATION] Message in 'human': {subj}")
                    else:
                        events.append(f"📬 [INBOX MSG] Role '{role_dir.name}' target '{target}': {subj}")

    # 3. Check for active trains in PENDING states
    if TRAINS_DIR.exists():
        for td in TRAINS_DIR.iterdir():
            state_file = td / "STATE.json"
            if td.is_dir() and state_file.exists():
                state = read_json_safe(state_file)
                st = state.get("status")
                tid = state.get("train_id", td.name)
                if st in ["PLAN_AUDIT_PENDING", "AUDIT_PENDING", "AUDIT_HOLD_AWAITING_LLM_AUDITOR", "IN_PROGRESS_AUDITOR"]:
                    events.append(f"⏳ [TRAIN ACTION] Train '{tid}' is in state '{st}' (Assigned: {state.get('current_role')})")

    # 4. Check for newly added root INTAKE / PLAN files
    now = time.time()
    for root_file in RELAY_ROOT.glob("*INTAKE*.md"):
        try:
            mtime = root_file.stat().st_mtime
            if now - mtime < 2 * 3600:
                events.append(f"📄 [INTAKE ARTIFACT] Active proposal in root: {root_file.name}")
        except Exception:
            pass

    return events

def run_notifier():
    print("🛰️ Enhanced Relay Notifier v3.0 started. Monitoring all inboxes and Google Drive...", flush=True)
    seen_events: Set[str] = set()
    
    while True:
        try:
            current_events = set(check_for_events())
            new_events = current_events - seen_events
            
            if new_events:
                print("\n" + "="*80, flush=True)
                print("🚨 WAKEUP NOTIFICATION: RELAY EVENTS REQUIRING LLM ATTENTION:", flush=True)
                for ev in sorted(new_events):
                    print(f"  -> {ev}", flush=True)
                print("="*80 + "\n", flush=True)
                seen_events = current_events
            elif not current_events:
                seen_events.clear()
            else:
                # Retain only current events to allow detection of re-added events
                seen_events = seen_events.intersection(current_events)
        except Exception as e:
            print(f"Notifier error: {e}", flush=True)
            
        time.sleep(MIN_POLL_SECONDS)

if __name__ == "__main__":
    run_notifier()
