#!/usr/bin/env python3
"""
relay/start_swarm.py — Master Autonomous Swarm Orchestrator
===========================================================
Launches the full autonomous Multi-Agent Relay Mesh (MARM) stack on this machine:
1. Owner Cockpit Web UI (http://localhost:8765/)
2. Mobile Push & 1-Tap Approval Listener (ntfy.sh)
3. Supervisor Daemon v3 (Codex Planner + Claude Opus Implementer + Antigravity Auditor + Sentinel Auto-Reaper)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import cluster_coordinator, human_watcher, watcher_daemon, resource_telemetry, relay_mesh


def start_mobile_watcher_thread() -> threading.Thread:
    """Runs the human watcher approval listener in background."""
    def _run():
        try:
            human_watcher.approve_listener()
        except Exception as exc:
            print(f"[HUMAN WATCHER] Listener error: {exc}", flush=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def start_notification_poller_thread() -> threading.Thread:
    """Periodically checks pending approvals and sends mobile push notifications."""
    def _run():
        last_shown: dict[str, float] = {}
        while True:
            try:
                human_watcher.poll_and_notify_pending(last_shown)
            except Exception as exc:
                pass
            time.sleep(15)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def main() -> None:
    relay_io.utf8_console()
    relay_mesh.ensure_dirs()

    lan_ip = cluster_coordinator.get_lan_ip()
    ts_ip = cluster_coordinator.get_tailscale_ip()

    print("=" * 80)
    print("  🚀 TARE.TOOLS MULTI-AGENT RELAY MESH (MARM) — INICIANDO ENXAME AUTÔNOMO")
    print("=" * 80)
    print(f"  • Hostname:                 {relay_mesh.get_hostname()}")
    print(f"  • Cockpit Web (Local):      http://localhost:8765/")
    print(f"  • Cockpit Web (Wi-Fi/LAN):  http://{lan_ip}:8765/")
    if ts_ip:
        print(f"  • Cockpit Web (Tailscale):  http://{ts_ip}:8765/ (4G/5G Privado)")
    print(f"  • Notificações Móveis:      ntfy.sh/tare-relay-166cb5b879bd")
    print(f"  • Papéis Ativos no Enxame:")
    print(f"      1. Planejador:          Codex GPT 5.6 Sol High (autonomous_planner.py)")
    print(f"      2. Implementador:       Claude Opus 4.8 High (autonomous_implementer.py)")
    print(f"      3. Auditor de Plano:    Antigravity Adversarial (U-7D Matrix)")
    print(f"      4. Auditor de Código:   Antigravity Adversarial (U-7D Matrix)")
    print(f"      5. Auto-Sentinel:       Lease Reaper & Deadlock Breaker (sentinel.py)")
    print(f"      6. Gatekeeper:          Operador (1-Tap no Celular)")
    print("=" * 80)
    print("Pressione Ctrl+C para parar o enxame a qualquer momento.\n", flush=True)

    # 1. Start Owner Cockpit Web Server & Cluster Coordinator (REST RPC)
    cluster_coordinator.start_coordinator()

    # 2. Start Mobile Notification & 1-Tap Approval Listeners
    start_mobile_watcher_thread()
    start_notification_poller_thread()

    force = ("--takeover" in sys.argv or "--force" in sys.argv)
    # 3. Start Supervisor Daemon (Main Thread Loop)
    try:
        watcher_daemon.run_daemon(force_takeover=force)
    except KeyboardInterrupt:
        print("\n[SWARM] Encerrando enxame com segurança (fail-closed). Até logo!")


if __name__ == "__main__":
    main()
