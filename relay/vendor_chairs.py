#!/usr/bin/env python3
"""relay/vendor_chairs.py — MARM Multi-Vendor Fuel Gauge & Model Chair Fallback Router.

Bridges the battle-tested tare.tools harness fallback architecture (SPEC-115 /
N-VENDORCREDIT) into the MARM Relay Mesh:
- Defines primary and fallback chairs per role (planner, implementer, auditor, bookkeeper).
- Maintains live vendor fuel state & circuit breakers in `relay/history/vendor_fuel_state.json`.
- When a vendor runs out of fuel (session limit, rate limit, quota, auth missing), trips
  the circuit breaker and dynamically routes to the next healthy chair without stalling
  the cluster or failing the release train.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import relay_io
import relay_mesh

FUEL_STATE_FILE = relay_mesh.RELAY_DIR / "history" / "vendor_fuel_state.json"
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 1800  # 30 minutes default backoff when no exact reset time is parsed

# Role-to-Chair fallback chains (ordered highest priority to lowest)
ROLE_CHAIRS: Dict[str, List[str]] = {
    "implementer": ["claude", "antigravity", "codex"],
    "planner": ["codex", "antigravity", "claude"],
    "plan-auditor": ["antigravity", "codex", "claude"],
    "auditor": ["antigravity", "codex", "claude"],
    "bookkeeper": ["antigravity", "codex", "claude"],
    "overseer": ["antigravity", "claude", "codex"],
}

# Signatures indicating fuel/quota/auth exhaustion
OUT_OF_FUEL_SIGNATURES = [
    "session limit",
    "hit your session limit",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "resource has been exhausted",
    "insufficient_quota",
    "authentication required",
    "please visit the url to log in",
    "status 429",
    "status 402",
    "credit balance is too low",
    "billing",
    "no fuel",
]


@dataclass
class DispatchResult:
    ok: bool
    engine: str
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    error_reason: Optional[str] = None
    runtime_key: str = ""
    runtime_model: str = ""


def runtime_pin(engine: str, role: str) -> tuple[str, str]:
    """Return the exact roster runtime key and model alias used for dispatch."""

    if engine == "claude":
        default = "claude-fable-5" if role in {"auditor", "plan-auditor"} else "claude-opus-5-5"
        return "claude", os.environ.get("CLAUDE_MODEL", default)
    if engine == "antigravity":
        default = (
            "gemini-3.7-flash-high"
            if role in {"auditor", "plan-auditor"}
            else "gemini-3.7-flash-medium"
        )
        return "google-cli", os.environ.get("AGY_MODEL", default)
    if engine == "codex":
        return "codex", os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
    return "", ""


CHAIR_ENDPOINTS = {"claude": "cli://claude", "antigravity": "cli://agy", "codex": "cli://codex"}


def chair_off(engine: str, role: str, model_known: bool = True):
    """The routing profile's SWITCHED_OFF_FOR_ROLE for a mesh chair in a role it knows, else None. The mesh's chairs
    are fixed, so the profile simply decides; bookkeeper and overseer are not its roles. `model_known=False`: the
    launcher passes no model, so the CLI runs whatever it is set to; any entry that switches a model off for the role
    on that CLI (or on every source) then refuses the chair, because tare cannot tell it is not that model."""
    from relay.environment import PROFILE_ROLES, profile, source, switched_off
    if role not in PROFILE_ROLES or engine not in CHAIR_ENDPOINTS:
        return None
    route = {"id": engine, "native_profile": {
        "protocol": "vendor-cli", "endpoint": CHAIR_ENDPOINTS[engine], "model": runtime_pin(engine, role)[1]}}
    if model_known:
        return switched_off(role, route)
    hit = next((text for text, _, rule in (profile(role=role)[0].get(role) or {}).get("off", ())
                if not isinstance(rule, str) and rule[3] in ("*", source(route))), None)
    return hit and (f"{engine} runs whatever model its CLI is set to, and {hit} switches models off for {role} there; "
                    f"tare cannot tell which one would run")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_fuel_state() -> Dict[str, Any]:
    """Reads the shared vendor fuel state with circuit breaker metadata."""
    if not FUEL_STATE_FILE.exists():
        return {"schemaVersion": 1, "vendors": {}, "updated_at": _now_iso()}
    data = relay_io.read_json(FUEL_STATE_FILE)
    if not isinstance(data, dict):
        return {"schemaVersion": 1, "vendors": {}, "updated_at": _now_iso()}
    return data


def save_fuel_state(state: Dict[str, Any]) -> None:
    """Atomically persists vendor fuel state."""
    state["updated_at"] = _now_iso()
    relay_io.write_json_atomic(FUEL_STATE_FILE, state)


def parse_reset_time(text: str) -> Optional[float]:
    """Attempts to parse reset timestamps from CLI error messages (e.g. 'resets 2:20am')."""
    m = re.search(r"resets?\s+(\d{1,2}):(\d{2})\s*([ap]m)?", text, re.IGNORECASE)
    if m:
        hr = int(m.group(1))
        mn = int(m.group(2))
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        now = datetime.now()
        target = now.replace(hour=hr, minute=mn, second=0, microsecond=0)
        if target <= now:
            # next day
            target += timedelta(days=1)  # replace(day=+1) broke on the month's last day
        return target.timestamp()
    return None


def is_vendor_healthy(vendor: str, host: Optional[str] = None) -> Tuple[bool, str]:
    """Checks if a vendor's circuit breaker is closed (healthy) on this host."""
    state = load_fuel_state()
    vendors = state.get("vendors", {})
    v_data = vendors.get(vendor, {})
    
    # Host-specific override if any (e.g. auth missing on specific machine)
    if host and host in v_data.get("hosts", {}):
        h_data = v_data["hosts"][host]
        if not h_data.get("healthy", True):
            cooldown_until = h_data.get("cooldown_until", 0)
            if time.time() < cooldown_until:
                return False, f"Host '{host}' disabled for {vendor}: {h_data.get('reason', 'unhealthy')}"

    # Global vendor status
    if not v_data.get("healthy", True):
        cooldown_until = v_data.get("cooldown_until", 0)
        if time.time() < cooldown_until:
            rem = int(cooldown_until - time.time())
            return False, f"Vendor '{vendor}' in circuit backoff ({rem}s remaining): {v_data.get('reason', 'out of fuel')}"
        else:
            # Half-open: test allowed
            return True, "Circuit half-open (cooldown expired)"
            
    return True, "Healthy"


def trip_vendor_circuit(vendor: str, reason: str, output_text: str = "", host: Optional[str] = None) -> None:
    """Trips the circuit breaker for a vendor when out-of-fuel or auth failure is detected."""
    state = load_fuel_state()
    vendors = state.setdefault("vendors", {})
    v_data = vendors.setdefault(vendor, {})
    
    reset_ts = parse_reset_time(output_text)
    if not reset_ts:
        reset_ts = time.time() + DEFAULT_CIRCUIT_COOLDOWN_SECONDS

    # If auth issue is host-specific, tag the host
    if "authentication required" in output_text.lower() and host:
        hosts = v_data.setdefault("hosts", {})
        hosts[host] = {
            "healthy": False,
            "reason": reason,
            "tripped_at": _now_iso(),
            "cooldown_until": reset_ts,
        }
    else:
        v_data["healthy"] = False
        v_data["reason"] = reason
        v_data["tripped_at"] = _now_iso()
        v_data["cooldown_until"] = reset_ts

    save_fuel_state(state)
    print(f"[{time.strftime('%H:%M:%S')}] ⚡ [CIRCUIT BREAKER] Tripped for vendor '{vendor}' ({reason}). Cooldown until {datetime.fromtimestamp(reset_ts).strftime('%H:%M:%S')}.", flush=True)


def report_vendor_success(vendor: str, host: Optional[str] = None) -> None:
    """Restores circuit breaker to healthy after a successful run."""
    state = load_fuel_state()
    vendors = state.setdefault("vendors", {})
    v_data = vendors.setdefault(vendor, {})
    if not v_data.get("healthy", True):
        v_data["healthy"] = True
        v_data["reason"] = "Restored on successful dispatch"
        v_data["restored_at"] = _now_iso()
        v_data.pop("cooldown_until", None)
    if host and host in v_data.get("hosts", {}):
        v_data["hosts"][host]["healthy"] = True
        v_data["hosts"][host].pop("cooldown_until", None)
    save_fuel_state(state)


def is_out_of_fuel(output: str) -> Tuple[bool, str]:
    """Detects whether standard output or error matches an out-of-fuel signature."""
    low = output.lower()
    for sig in OUT_OF_FUEL_SIGNATURES:
        if sig in low:
            return True, f"Matched out-of-fuel signature: {sig!r}"
    return False, ""


def _resolve_engine_cmd(engine: str, prompt: str, role: str) -> Optional[List[str]]:
    """Builds the CLI command list for a given engine."""
    if engine == "claude":
        claude_exe = shutil.which("claude") or "claude"
        default_win = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"
        if default_win.exists():
            claude_exe = str(default_win)
        cmd = [claude_exe, "-p", "--dangerously-skip-permissions"]
        _runtime_key, model = runtime_pin(engine, role)
        cmd.extend(["--model", model])
        return cmd

    elif engine == "antigravity":
        agy_exe = shutil.which("agy") or "agy"
        # agy takes the prompt directly via -p
        _runtime_key, model = runtime_pin(engine, role)
        return [agy_exe, "-p", prompt, "--model", model, "--dangerously-skip-permissions"]

    elif engine == "codex":
        codex_exe = shutil.which("codex") or "codex"
        if not shutil.which("codex"):
            return None
        _runtime_key, model = runtime_pin(engine, role)
        return [codex_exe, "exec", "--model", model, "-p", prompt]

    return None


def dispatch_role(
    role: str,
    prompt: str,
    cwd: Optional[Path] = None,
    timeout: int = 900,
    train_id: Optional[str] = None,
    excluded_engines: set[str] | None = None,
) -> DispatchResult:
    """Dispatches a prompt for the given role through its ordered fallback chain."""
    cwd_path = str(cwd or _ROOT)
    hostname = relay_mesh.get_hostname()
    chairs = ROLE_CHAIRS.get(role, ["antigravity", "claude", "codex"])
    
    last_result: Optional[DispatchResult] = None
    
    for engine in chairs:
        if engine in (excluded_engines or set()):
            continue
        healthy, status_msg = is_vendor_healthy(engine, host=hostname)
        off = healthy and chair_off(engine, role)
        if off:
            healthy, status_msg = False, f"SWITCHED_OFF_FOR_ROLE: {off}"
        if not healthy:
            print(f"[{time.strftime('%H:%M:%S')}] ⏭️ [CHAIR SKIP] '{engine}' skipped for role '{role}': {status_msg}", flush=True)
            continue

        cmd = _resolve_engine_cmd(engine, prompt, role)
        if not cmd:
            continue

        print(f"[{time.strftime('%H:%M:%S')}] 🪑 [CHAIR DISPATCH] Role '{role}' -> Engine '{engine}' (Host: {hostname})...", flush=True)
        t0 = time.time()
        try:
            # stdin prompt passing for claude/interactive tools
            stdin_data = prompt if engine == "claude" else None
            proc = subprocess.run(
                cmd,
                input=stdin_data,
                cwd=cwd_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **relay_io.safe_subprocess_kwargs(),
            )
            duration = time.time() - t0
            combined_out = f"{proc.stdout}\n{proc.stderr}"
            
            oof, oof_reason = is_out_of_fuel(combined_out)
            if oof or (proc.returncode != 0 and engine == "claude" and "session limit" in combined_out.lower()):
                trip_vendor_circuit(engine, oof_reason or "Exited non-zero with out-of-fuel signal", combined_out, host=hostname)
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Engine '{engine}' exhausted. Falling over to next chair in chain...", flush=True)
                last_result = DispatchResult(
                    ok=False,
                    engine=engine,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_s=duration,
                    error_reason=oof_reason or "Out of fuel",
                )
                continue

            # Check success
            if proc.returncode == 0:
                runtime_key, runtime_model = runtime_pin(engine, role)
                report_vendor_success(engine, host=hostname)
                print(f"[{time.strftime('%H:%M:%S')}] ✅ [CHAIR SUCCESS] Engine '{engine}' completed in {duration:.1f}s.", flush=True)
                return DispatchResult(
                    ok=True,
                    engine=engine,
                    returncode=0,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_s=duration,
                    runtime_key=runtime_key,
                    runtime_model=runtime_model,
                )
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ❌ Engine '{engine}' exited with code {proc.returncode}. Output:\n{proc.stderr[:300]}", flush=True)
                last_result = DispatchResult(
                    ok=False,
                    engine=engine,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_s=duration,
                    error_reason=f"Exit code {proc.returncode}",
                )

        except subprocess.TimeoutExpired:
            duration = time.time() - t0
            print(f"[{time.strftime('%H:%M:%S')}] ⏱️ Engine '{engine}' timed out after {timeout}s.", flush=True)
            last_result = DispatchResult(
                ok=False,
                engine=engine,
                returncode=-1,
                stdout="",
                stderr="TimeoutExpired",
                duration_s=duration,
                error_reason="Timeout",
            )
        except Exception as exc:
            duration = time.time() - t0
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Engine '{engine}' exception: {exc}", flush=True)
            last_result = DispatchResult(
                ok=False,
                engine=engine,
                returncode=-2,
                stdout="",
                stderr=str(exc),
                duration_s=duration,
                error_reason=str(exc),
            )

    # If all chairs failed
    return last_result or DispatchResult(
        ok=False,
        engine="none",
        returncode=-3,
        stdout="",
        stderr="All fallback chairs exhausted or unavailable.",
        duration_s=0.0,
        error_reason="All chairs exhausted",
    )
