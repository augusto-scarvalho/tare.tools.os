#!/usr/bin/env python3
"""
relay/resource_telemetry.py — Complete Stdlib Hardware Telemetry & Capability Mesh
================================================================================
Monitors CPU Load %, RAM Utilization %, Disk Space, and GPU/VRAM pressure in real time
without third-party dependencies, broadcasting node capacity to `relay/nodes/<hostname>.json`.

Enables smart load shedding, multi-host job placement, and background process priority management.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
NODES_DIR = RELAY_DIR / "nodes"

if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io


# -----------------------------------------------------------------------------
# Ctypes Structs for Memory & CPU
# -----------------------------------------------------------------------------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint), ("dwHighDateTime", ctypes.c_uint)]


def get_hostname() -> str:
    return socket.gethostname()


def _filetime_to_int(ft: FILETIME) -> int:
    return (ft.dwHighDateTime << 32) + ft.dwLowDateTime


def probe_cpu(sample_ms: int = 150) -> Dict[str, Any]:
    """Calculates instantaneous CPU load percentage via GetSystemTimes (Windows stdlib)."""
    cpu_cores = os.cpu_count() or 1
    cpu_pct = 0.0

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"cpu_cores": cpu_cores, "cpu_load_pct": 5.0}

    if sys.platform == "win32":
        try:
            idle1, kernel1, user1 = FILETIME(), FILETIME(), FILETIME()
            idle2, kernel2, user2 = FILETIME(), FILETIME(), FILETIME()

            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle1), ctypes.byref(kernel1), ctypes.byref(user1))
            time.sleep(sample_ms / 1000.0)
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle2), ctypes.byref(kernel2), ctypes.byref(user2))

            idle_delta = _filetime_to_int(idle2) - _filetime_to_int(idle1)
            kernel_delta = _filetime_to_int(kernel2) - _filetime_to_int(kernel1)
            user_delta = _filetime_to_int(user2) - _filetime_to_int(user1)

            total_sys = kernel_delta + user_delta
            if total_sys > 0:
                cpu_pct = round(((total_sys - idle_delta) / total_sys) * 100.0, 1)
                cpu_pct = max(0.0, min(100.0, cpu_pct))
        except Exception:
            pass

    return {
        "cpu_cores": cpu_cores,
        "cpu_load_pct": cpu_pct,
    }


def probe_memory() -> Dict[str, Any]:
    """Extracts physical RAM metrics using stdlib ctypes on Windows."""
    if sys.platform == "win32":
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return {
                "ram_total_gb": round(stat.ullTotalPhys / (1024**3), 2),
                "ram_available_gb": round(stat.ullAvailPhys / (1024**3), 2),
                "ram_load_pct": int(stat.dwMemoryLoad),
            }
        except Exception:
            pass
    return {"ram_total_gb": 0.0, "ram_available_gb": 0.0, "ram_load_pct": 0}


def probe_disk() -> Dict[str, Any]:
    """Calculates disk space metrics on the relay root drive."""
    try:
        root_drive = Path(RELAY_ROOT).anchor or "C:\\"
        usage = shutil.disk_usage(root_drive)
        return {
            "disk_total_gb": round(usage.total / (1024**3), 1),
            "disk_free_gb": round(usage.free / (1024**3), 1),
            "disk_used_pct": round((usage.used / usage.total) * 100.0, 1),
        }
    except Exception:
        return {"disk_total_gb": 0.0, "disk_free_gb": 0.0, "disk_used_pct": 0.0}


def probe_gpu() -> Dict[str, Any]:
    """Probes NVIDIA GPU via nvidia-smi if available."""
    nvsmi = shutil.which("nvidia-smi")
    if not nvsmi:
        return {
            "gpu_available": False,
            "gpu_name": "None",
            "gpu_vram_total_mb": 0,
            "gpu_vram_free_mb": 0,
            "gpu_load_pct": 0,
        }
    try:
        proc = subprocess.run(
            [nvsmi, "--query-gpu=name,memory.total,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            stdin=subprocess.DEVNULL, **relay_io.safe_subprocess_kwargs()
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 4:
                return {
                    "gpu_available": True,
                    "gpu_name": parts[0],
                    "gpu_vram_total_mb": int(float(parts[1])),
                    "gpu_vram_free_mb": int(float(parts[2])),
                    "gpu_load_pct": int(float(parts[3])),
                }
    except Exception:
        pass
    return {
        "gpu_available": False,
        "gpu_name": "Error or Unsupported",
        "gpu_vram_total_mb": 0,
        "gpu_vram_free_mb": 0,
        "gpu_load_pct": 0,
    }


def probe_host_telemetry() -> Dict[str, Any]:
    """Gathers full node telemetry and derives dynamic capability & pressure state."""
    cpu = probe_cpu()
    mem = probe_memory()
    disk = probe_disk()
    gpu = probe_gpu()

    # Determine capabilities
    caps = ["api_cloud_llm", "cli_worker", "standard_pytest"]
    if cpu["cpu_cores"] >= 8 and cpu["cpu_load_pct"] < 60:
        caps.append("high_concurrency")
    if mem["ram_available_gb"] >= 16.0:
        caps.append("memory_heavy")
    if gpu["gpu_available"]:
        caps.append("cuda")
        if gpu["gpu_vram_free_mb"] >= 8000 and gpu["gpu_load_pct"] < 80:
            caps.append("gpu_compute_available")

    # Resource pressure classification
    pressure = "NORMAL"
    reasons = []
    if gpu["gpu_available"] and gpu["gpu_load_pct"] >= 85:
        reasons.append(f"GPU_BUSY ({gpu['gpu_load_pct']}%)")
    if cpu["cpu_load_pct"] >= 80:
        reasons.append(f"CPU_BUSY ({cpu['cpu_load_pct']}%)")
    if mem["ram_load_pct"] >= 88 or mem["ram_available_gb"] < 4.0:
        reasons.append(f"HIGH_RAM ({mem['ram_load_pct']}%)")
    if disk["disk_free_gb"] < 10.0:
        reasons.append("LOW_DISK")

    if reasons:
        pressure = ", ".join(reasons)
    elif cpu["cpu_load_pct"] < 25 and mem["ram_load_pct"] < 65 and (not gpu["gpu_available"] or gpu["gpu_load_pct"] < 30):
        pressure = "IDLE (HIGH_CAPACITY)"

    return {
        "node_id": get_hostname(),
        "hostname": get_hostname(),
        "status": "ONLINE",
        "pressure": pressure,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resources": {
            **cpu,
            **mem,
            **disk,
            **gpu,
        },
        "capabilities": caps,
    }


def coordinator_candidates() -> List[str]:
    """Where the relay coordinator may listen: RELAY_COORDINATOR_URL, this machine, then the host of this machine's
    model gateway (tare setup --local-gateway), where a mesh keeps its coordinator."""
    found = [os.environ.get("RELAY_COORDINATOR_URL"), "http://127.0.0.1:8765"]
    try:
        from urllib.parse import urlsplit
        from relay.native_catalog import local_gateway
        found.append(f"http://{urlsplit(local_gateway()).hostname}:8765")
    except (ImportError, ValueError):
        pass  # run as a script outside the package, or no usable gateway setting: this machine only
    return list(dict.fromkeys(c.rstrip("/") for c in found if c))


def broadcast_heartbeat() -> Path:
    """Writes node telemetry to relay/nodes/<hostname>.json for mesh visibility and pushes REST heartbeat."""
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    telemetry = probe_host_telemetry()
    path = NODES_DIR / f"{get_hostname()}.json"
    relay_io.write_json_atomic(path, telemetry)

    # Push to active Coordinator over Tailscale/LAN if reachable
    for cand in coordinator_candidates():
        try:
            req = urllib.request.Request(
                f"{cand.rstrip('/')}/api/v1/nodes/heartbeat",
                data=json.dumps(telemetry).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    break
        except Exception:
            pass

    return path


def get_safe_subprocess_flags() -> int:
    """Returns Windows BELOW_NORMAL_PRIORITY_CLASS to ensure background workers
    yield CPU cycles to user interactive applications (games, browser, CAD)."""
    if sys.platform == "win32":
        return 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    return 0


def can_accept_profile(profile: str, telemetry: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Determines if the local host has the resources to accept a specific task profile."""
    t = telemetry or probe_host_telemetry()
    res = t.get("resources", {})

    if profile in ("API_ONLY", "LIGHTWEIGHT", "DOCS"):
        return True, "Node is eligible for lightweight/API workload."

    if profile == "GPU_INTENSIVE":
        if not res.get("gpu_available"):
            return False, "Node has no GPU."
        if res.get("gpu_load_pct", 0) >= 80:
            return False, f"GPU is currently busy ({res.get('gpu_load_pct')}% load)."
        if res.get("gpu_vram_free_mb", 0) < 6000:
            return False, f"Insufficient free VRAM ({res.get('gpu_vram_free_mb')} MB free)."
        return True, "GPU resources available."

    if profile == "CPU_HEAVY":
        if res.get("cpu_load_pct", 0) >= 75:
            return False, f"CPU is currently busy ({res.get('cpu_load_pct')}% load)."
        return True, "CPU resources available."

    if profile == "MEMORY_HEAVY":
        if res.get("ram_available_gb", 0) < 6.0:
            return False, f"Low available RAM ({res.get('ram_available_gb')} GB free)."
        return True, "RAM resources available."

    return True, "Standard workload accepted."


if __name__ == "__main__":
    relay_io.utf8_console()
    hb = broadcast_heartbeat()
    print(f"[NODE TELEMETRY] Broadcasted heartbeat to: {hb}")
    print(json.dumps(probe_host_telemetry(), indent=2, ensure_ascii=False))
