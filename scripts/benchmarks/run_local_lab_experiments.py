#!/usr/bin/env python3
"""
scripts/run_local_lab_experiments.py — Canonical entry point for the local experiments backlog
===================================================================================================
Governance: ADR-048 (Local Inference Substrate, Agentic Harness & Empirical Acceleration)
Target hardware: the GPU workstation (RTX 3090 24GB VRAM), via TARE_NODE_HOST on port 8080

Usage:
  python scripts/run_local_lab_experiments.py --all
  python scripts/run_local_lab_experiments.py --exp EXP-01
  python scripts/run_local_lab_experiments.py --status
  python scripts/run_local_lab_experiments.py --mock   # Hermetic offline validation
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
BENCHMARK_SCRIPT = SCRIPTS_DIR / "local_lmm_benchmark_suite.py"

DEFAULT_HOST = os.getenv("TARE_NODE_HOST", "localhost")
DEFAULT_PORT = 8080


def check_node_connectivity(host: str) -> bool:
    """Check whether the GPU workstation answers a Tailscale ping."""
    cmd = ["ping", "-n", "1", "-w", "1000", host] if sys.platform == "win32" else ["ping", "-c", "1", "-W", "1", host]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Canonical entry point for the local experiments backlog (ADR-048)"
    )
    parser.add_argument("--all", action="store_true", help="Run every experiment (EXP-01 to EXP-05)")
    parser.add_argument("--exp", type=str, choices=["EXP-01", "EXP-02", "EXP-03", "EXP-04", "EXP-05"], help="Run one experiment")
    parser.add_argument("--status", action="store_true", help="Check connectivity to the GPU workstation and slop.cpp")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help=f"Tailscale host of the inference node (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"slop.cpp HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--mock", action="store_true", help="Run in offline mock mode to validate contracts")
    
    args = parser.parse_args()
    
    print("=" * 70)
    print("  🧪 TARE.TOOLS LOCAL LABS — HARDWARE EXPERIMENT RUNNER (ADR-048)")
    print(f"  Target node: {args.host}:{args.port} | Mode: {'MOCK' if args.mock else 'LIVE TAILSCALE'}")
    print("=" * 70)
    
    if args.status:
        is_online = check_node_connectivity(args.host)
        status_str = "🟢 ONLINE (Direct Tailscale)" if is_online else "🔴 OFFLINE (check that the GPU workstation is on and connected to Tailscale)"
        print(f"\n[STATUS] Connectivity to node '{args.host}': {status_str}")
        print(f"[INFO] Expected HTTP endpoint: http://{args.host}:{args.port}/v1")
        print("\nTo start slop.cpp on the GPU workstation:")
        print("  ./build/bin/llama-server -m /models/qwen36-fable-tc.gguf --chat-template-file /models/chat_template.jinja --host 0.0.0.0 --port 8080 -ngl 99\n")
        return 0 if is_online else 1

    if not args.all and not args.exp and not args.mock:
        parser.print_help()
        return 0

    cmd = [sys.executable, str(BENCHMARK_SCRIPT), "--host", args.host, "--port", str(args.port)]
    if args.all:
        cmd.append("--all")
    elif args.exp:
        cmd.extend(["--exp", args.exp])
    if args.mock:
        cmd.append("--mock")
        
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
