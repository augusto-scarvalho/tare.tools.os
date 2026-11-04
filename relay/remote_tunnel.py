#!/usr/bin/env python3
"""
relay/remote_tunnel.py — Zero-Install Public HTTPS Tunnel via Windows OpenSSH
=============================================================================
Creates an instant, encrypted, public HTTPS URL for the Owner Cockpit Web UI
using standard SSH reverse port forwarding (`pinggy.io` or `localhost.run`).
Requires zero external software installations (uses built-in C:\\Windows\\System32\\OpenSSH\\ssh.exe).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"

if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import cockpit_server

PORT = int(os.environ.get("RELAY_COCKPIT_PORT", "8765"))


def start_public_tunnel(port: int = PORT) -> None:
    """Spawns an instant public HTTPS tunnel using OpenSSH."""
    ssh_exe = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
    if not Path(ssh_exe).exists():
        print(f"[TUNNEL] ssh.exe not found at {ssh_exe}. Please use LAN IP.")
        return

    # Start cockpit server in background
    cockpit_server.start_cockpit_server(port=port)
    lan_ip = cockpit_server.get_lan_ip()

    print("=" * 80)
    print("  🌐 CRIANDO TÚNEL PÚBLICO HTTPS SEGURO PARA O CELULAR (4G/5G / EXTERNO)")
    print("=" * 80)
    print(f"  • Localhost (este PC):   http://localhost:{port}/")
    print(f"  • Wi-Fi Local (no Wi-Fi): http://{lan_ip}:{port}/")
    print("  • Conectando túnel HTTPS público via OpenSSH (aguarde alguns segundos)...")
    print("=" * 80)

    # Use pinggy / localhost.run
    cmd = [
        ssh_exe,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-p", "443",
        "-R0:localhost:" + str(port),
        "qr@a.pinggy.io"
    ]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        proc.wait()
    except KeyboardInterrupt:
        print("\n[TUNNEL] Encerrando túnel.")


if __name__ == "__main__":
    relay_io.utf8_console()
    start_public_tunnel()
