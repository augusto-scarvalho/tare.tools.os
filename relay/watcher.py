#!/usr/bin/env python3
"""
tare.tools Role-Specific Watcher (for Planner, Implementer, or Auditor terminal)
Watches relay/inbox/<role>/ and prints alerts in real-time.
"""

from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
INBOX_DIR = RELAY_DIR / "inbox"

MIN_POLL_SECONDS = 15  # TRAIN-06 Seam B.4: mandatory evaluation floor


def watch_inbox(role: str = "planner", poll_interval: int = MIN_POLL_SECONDS):
    poll_interval = max(MIN_POLL_SECONDS, int(poll_interval))
    target_inbox = INBOX_DIR / role
    target_inbox.mkdir(parents=True, exist_ok=True)
    
    seen_messages = set(os.listdir(target_inbox))
    
    print(f"🛰️ [RELAY WATCHER ACTIVE] Monitoring inbox for role '{role}' at: {target_inbox}", flush=True)
    print(f"   Polling every {poll_interval}s...", flush=True)
    
    # Print existing pending messages on startup
    if seen_messages:
        print(f"\n📬 [EXISTING PENDING MESSAGES IN INBOX ({len(seen_messages)})]:", flush=True)
        for msg_name in sorted(seen_messages):
            msg_path = target_inbox / msg_name
            try:
                data = json.loads(msg_path.read_text(encoding="utf-8-sig"))
                print(f"  - {msg_name}: {data.get('subject')} | Payload: {data.get('payload')}", flush=True)
            except Exception:
                print(f"  - {msg_name}", flush=True)
        print("-" * 70 + "\n", flush=True)
    
    try:
        while True:
            time.sleep(poll_interval)
            
            current_messages = set(os.listdir(target_inbox))
            new_messages = current_messages - seen_messages
            
            if new_messages:
                for msg_name in sorted(new_messages):
                    msg_path = target_inbox / msg_name
                    try:
                        data = json.loads(msg_path.read_text(encoding="utf-8-sig"))
                        print("\n" + "="*70, flush=True)
                        print(f"🔔 [NOVA MENSAGEM / TAREFA PARA O {role.upper()}]:", flush=True)
                        print(f"   Assunto: {data.get('subject')}", flush=True)
                        print(f"   Host: {data.get('hostname')}", flush=True)
                        print(f"   Payload: {json.dumps(data.get('payload'), indent=2)}", flush=True)
                        print("="*70 + "\n", flush=True)
                    except Exception:
                        print(f"\n🔔 [NOVA MENSAGEM NA CAIXA]: {msg_name}\n", flush=True)
                
                seen_messages = current_messages
            elif len(current_messages) < len(seen_messages):
                seen_messages = current_messages
    except KeyboardInterrupt:
        print("\nWatcher finalizado.", flush=True)

if __name__ == "__main__":
    role_arg = sys.argv[1] if len(sys.argv) > 1 else "planner"
    watch_inbox(role_arg)
