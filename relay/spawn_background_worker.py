#!/usr/bin/env python3
"""
relay/spawn_background_worker.py — Completely Silent / Headless Background Worker Daemon Spawner
==============================================================================================
Launches distributed cluster workers or supervisor daemons on Windows/Linux in 100% headless
detached background mode (using pythonw.exe + CREATE_NO_WINDOW + DETACHED_PROCESS).
Guarantees zero console flashing, zero focus stealing, and zero foreground window interference.

Usage:
  python relay/spawn_background_worker.py start [--swarm] [--worker]
  python relay/spawn_background_worker.py stop
  python relay/spawn_background_worker.py status
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
LOGS_DIR = RELAY_DIR / "history" / "logs"
PID_FILE = RELAY_DIR / "history" / "background_workers.pid"

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))

import relay_io


def _get_pythonw() -> str:
    if sys.platform == "win32":
        pw = shutil.which("pythonw")
        if pw and Path(pw).exists():
            return pw
        default_pw = Path(sys.executable).parent / "pythonw.exe"
        if default_pw.exists():
            return str(default_pw)
    return sys.executable


def spawn_finite(command, *, cwd, stdout, env=None):
    """Launch one caller-owned finite job; no swarm PID file or daemon startup."""
    return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=stdout, **relay_io.safe_subprocess_kwargs())


def start_worker(as_swarm: bool = False) -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    py_exe = _get_pythonw()
    target_script = RELAY_DIR / ("start_swarm.py" if as_swarm else "cluster_worker.py")
    log_name = "swarm.log" if as_swarm else "worker.log"
    out_log = LOGS_DIR / log_name

    cmd = [py_exe, "-u", str(target_script)]
    if as_swarm:
        cmd.append("--takeover")

    flags = 0
    startupinfo = None
    if sys.platform == "win32":
        # 0x08000000 = CREATE_NO_WINDOW, 0x00000008 = DETACHED_PROCESS
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    with open(out_log, "a", encoding="utf-8") as out_f:
        out_f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] === Spawning Headless Background Process ===\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(RELAY_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=out_f,
            creationflags=flags,
            startupinfo=startupinfo,
            close_fds=(sys.platform != "win32")
        )

    # Save PID
    pids = relay_io.read_json(PID_FILE) or {}
    mode = "swarm" if as_swarm else "worker"
    pids[mode] = {
        "pid": proc.pid,
        "script": str(target_script),
        "started_at": relay_io.sha256_hex(str(time.time()))[:8],
        "log_file": str(out_log)
    }
    relay_io.write_json_atomic(PID_FILE, pids)
    print(f"🚀 [BACKGROUND SPAWNER] {mode.upper()} iniciado em background silencioso (PID {proc.pid}).")
    print(f"   Logs em tempo real: {out_log}")
    return proc.pid


def stop_workers() -> None:
    pids = relay_io.read_json(PID_FILE) or {}
    if not pids:
        print("[BACKGROUND SPAWNER] Nenhum worker registrado em background.")
        return

    for mode, meta in list(pids.items()):
        pid = meta.get("pid")
        if pid:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                                   capture_output=True, **relay_io.safe_subprocess_kwargs())
                else:
                    os.kill(pid, signal.SIGTERM)
                print(f"🛑 [BACKGROUND SPAWNER] Processo {mode} (PID {pid}) encerrado.")
            except Exception as exc:
                print(f"⚠️ [BACKGROUND SPAWNER] Não foi possível encerrar PID {pid}: {exc}")
    
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)


def status_workers() -> None:
    pids = relay_io.read_json(PID_FILE) or {}
    if not pids:
        print("ℹ️ [BACKGROUND SPAWNER] Nenhum worker registrado.")
        return
    print("📊 [BACKGROUND SPAWNER] Workers Ativos:")
    for mode, meta in pids.items():
        print(f"  • {mode}: PID {meta.get('pid')} | Log: {meta.get('log_file')}")


def main() -> None:
    relay_io.utf8_console()
    parser = argparse.ArgumentParser(description="Headless Background Worker Spawner")
    subparsers = parser.add_subparsers(dest="action")

    start_p = subparsers.add_parser("start", help="Start background worker")
    start_p.add_argument("--swarm", action="store_true", help="Start full swarm orchestrator")
    start_p.add_argument("--worker", action="store_true", help="Start distributed worker node")

    subparsers.add_parser("stop", help="Stop running background workers")
    subparsers.add_parser("status", help="Check status of background workers")

    args = parser.parse_args()
    if args.action == "start":
        start_worker(as_swarm=args.swarm)
    elif args.action == "stop":
        stop_workers()
    elif args.action == "status":
        status_workers()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
