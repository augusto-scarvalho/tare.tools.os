"""Drive a UI proof against an in-process panel and a detached Vite server."""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harness_lib import processes
from harness_lib.common import HarnessError, ROOT


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_vite(port: int, proc: Any, timeout: float = 40) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    return False


def _wait_closed(*ports: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_port_open(port) for port in ports):
            return True
        time.sleep(0.1)
    return not any(_port_open(port) for port in ports)


def cmd_ui_proof(args: argparse.Namespace) -> None:
    panel_port = int(args.panel_port)
    if not 1 <= panel_port <= 65535:
        raise HarnessError("--panel-port must be between 1 and 65535")
    if args.timeout <= 0:
        raise HarnessError("--timeout must be positive")
    if _port_open(panel_port):
        raise HarnessError(
            f"panel port {panel_port} is already in use; Vite proxies /api to that fixed port"
        )

    driver = Path(args.driver).expanduser().resolve()
    if not driver.is_file():
        raise HarnessError(f"UI proof driver is not readable: {driver}")
    if not driver.is_relative_to(ROOT / "ui"):
        # Enforced, not just documented: this verb executes the driver with node,
        # so an arbitrary path would be an unconstrained code-exec seam (security
        # audit 2026-07-29). Node ESM also resolves packages from the script's
        # own path, so outside ui/ the driver cannot import playwright anyway.
        raise HarnessError(f"driver must live under ui/ (e.g. ui/tests/x.tmp.mjs): {driver}")
    ui = ROOT / "ui"
    npx = shutil.which("npx")
    if not npx:
        raise HarnessError("npx was not found on PATH")

    import harness_ui  # lazy: cli_registry must not pull the panel into every command

    server = None
    vite = None
    vite_port = _free_port()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = ROOT / ".harness" / "runs" / f"ui-proof-{ts}-vite.log"
    rc = 1
    try:
        try:
            # The token stays only in memory and the loopback driver's argv.
            server, token = harness_ui.make_server(ROOT, panel_port, None)
        except OSError as exc:
            raise HarnessError(f"cannot bind fixed panel port {panel_port}: {exc}") from exc
        threading.Thread(target=server.serve_forever, daemon=True).start()

        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("wb") as log_f:
            vite = processes.popen_detached(
                # --host 127.0.0.1: Vite's default "localhost" can bind IPv6-only
                # on Windows, while the readiness poll and driver URL are IPv4.
                [npx, "vite", "--host", "127.0.0.1", "--port", str(vite_port), "--strictPort"],
                cwd=ui,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
        if not _wait_vite(vite_port, vite):
            raise HarnessError(f"Vite did not become ready within 40s; see {log.relative_to(ROOT)}")

        try:
            # Stdio INHERITED (no capture): the driver's PROOF lines are this
            # command's output. run_quiet's CREATE_NO_WINDOW is a no-op for
            # inherited stdio, so nothing is swallowed.
            rc = processes.run_quiet(
                ["node", str(driver), f"http://127.0.0.1:{vite_port}", token],
                timeout=args.timeout,
                cwd=ui,
            ).returncode
        except subprocess.TimeoutExpired:
            print(f"ui-proof: driver timed out after {args.timeout}s", file=sys.stderr)
            rc = 124
    finally:
        if vite is not None:
            processes.signal_process_tree(vite.pid, signal.SIGTERM)
        if server is not None:
            try:
                server.sessions.stop_all()
            except Exception:
                pass
            server.shutdown()
            server.server_close()
        if _wait_closed(panel_port, vite_port):
            print("ui-proof: ports closed")
        else:
            print("WARN ui-proof: one or more ports remain open after cleanup", file=sys.stderr)
    raise SystemExit(rc)
