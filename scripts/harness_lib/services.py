#!/usr/bin/env python3
"""svc-registry-mvp (P0) — declarative service registry + lifecycle manager.

The harness (or a governed target) can DECLARE a long-lived dependency ("the
ponytail MCP server must be running"), START it idempotently, HEALTH-CHECK it,
and STOP it with no orphan process. Services are NOT harness daemons: every
start records an ownerPid, and `status()` reaps dead services (and stops
live services whose owner died) at the next harness invocation — no watchdog,
no polling loop (observation must pay for itself).

Registry (declarative, two scopes, one schema):
  * harness scope — `.harness/services.json` `{"services": {name: entry}}`;
  * target scope  — optional `"services"` key in `target.json`.
Entry: `{"command": [...], "cwd": null, "env": {}, "ready": {"type":
"tcp"|"http", "port"/"url", "timeoutSeconds"}, "stopGraceSeconds": 5,
"autoStart": [], "mcp": {...}}`. `ready` absent = pid-alive after a short
settle. `autoStart` wiring is P1 (svc-autostart-owners); `mcp` consumption is
P2 (svc-mcp-wiring) — both OPEN in the backlog.

State per service (supervisor-pidfile pattern): `.harness/state/services/
<name>.json` (target scope under `.harness/state/targets/<t>/services/`),
logs beside them. Spawns use processes.process_group_kwargs() — on Windows
that means CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW (never a visible
console) and taskkill /T /F reaches grandchildren on stop.

Self-check: `python scripts/harness_lib/services.py` (registry/probe logic
only — spawns live in the acceptance scenario `sv_services.py`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes  # noqa: E402
from harness_lib import targets as targets_lib  # noqa: E402
from harness_lib.common import HarnessError, ROOT, read_json, write_json  # noqa: E402

REGISTRY_REL = ".harness/services.json"
_SETTLE_S = 0.4  # probe-less services: pid must still be alive after this


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_registry(root: Path | str, target: str | None = None) -> dict[str, Any]:
    """Declared services for one scope. Target scope reads target.json."""
    root = Path(root)
    if target:
        entry = targets_lib.resolve(target)
        return dict(entry.get("services") or {})
    doc = read_json(root / REGISTRY_REL, None) or {}
    return dict(doc.get("services") or {})


def _state_dir(root: Path | str, target: str | None) -> Path:
    base = (targets_lib.state_dir(target) if target
            else Path(root) / ".harness" / "state")
    return base / "services"


def _state_path(root: Path | str, name: str, target: str | None) -> Path:
    return _state_dir(root, target) / f"{name}.json"


def _probe_once(ready: dict[str, Any]) -> bool:
    kind = str(ready.get("type") or "")
    try:
        if kind == "tcp":
            with socket.create_connection(("127.0.0.1", int(ready["port"])), timeout=1.5):
                return True
        if kind == "http":
            with urllib.request.urlopen(str(ready["url"]), timeout=2) as resp:  # noqa: S310 — loopback health probe
                return int(resp.status) < 500
    except Exception:
        return False
    return False


def _await_ready(entry: dict[str, Any], pid: int) -> None:
    """Block until the ready probe passes (or pid-alive settle for probe-less
    services); raise on timeout — the caller stops the tree."""
    ready = entry.get("ready") or {}
    if not ready:
        time.sleep(_SETTLE_S)
        if not processes.pid_alive(pid):
            raise HarnessError("service exited immediately (no ready probe declared)")
        return
    deadline = time.monotonic() + float(ready.get("timeoutSeconds", 15))
    while time.monotonic() < deadline:
        if _probe_once(ready):
            return
        if not processes.pid_alive(pid):
            raise HarnessError("service process died before becoming ready")
        time.sleep(0.25)
    raise HarnessError(
        f"ready probe {ready.get('type')} timed out after {ready.get('timeoutSeconds', 15)}s")


def ensure(root: Path | str, name: str, target: str | None = None,
           owner_pid: int | None = None) -> dict[str, Any]:
    """Idempotent start: a live declared service is returned as-is; a stale
    pidfile restarts; a failed ready probe stops the tree and raises legibly."""
    root = Path(root)
    registry = load_registry(root, target)
    entry = registry.get(name)
    if not isinstance(entry, dict) or not entry.get("command"):
        raise HarnessError(
            f"unknown service {name!r} in {'target ' + target if target else 'harness'} scope\n"
            f"cause: no registry entry with a command\n"
            f"fix: declare it in {'target.json services' if target else REGISTRY_REL}")
    state_path = _state_path(root, name, target)
    state = read_json(state_path, None) or {}
    if state.get("pid") and processes.pid_alive(int(state["pid"])):
        return {**state, "status": "already-running"}

    log_dir = _state_dir(root, target) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    if target:
        t_entry = targets_lib.resolve(target)
        env = {**targets_lib.spawn_env(t_entry),
               "HARNESS_TARGET_NAME": target,
               "HARNESS_TARGET_ROOT": str(targets_lib.target_root(t_entry)),
               "PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "TEMP": os.environ.get("TEMP", "")}
    else:
        env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
    cwd = entry.get("cwd") or str(root)
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(  # noqa: S603 — operator-declared registry command
            [str(c) for c in entry["command"]], cwd=cwd, env=env,
            stdout=log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **processes.process_group_kwargs())
    try:
        _await_ready(entry, proc.pid)
    except HarnessError as exc:
        processes.signal_process_tree(proc.pid, signal.SIGTERM)
        state_path.unlink(missing_ok=True)
        raise HarnessError(
            f"service {name} failed to start: {exc}\n"
            f"cause: see its log\n"
            f"fix: inspect {log_path} and repair the declared command/probe") from exc
    state = {"name": name, "pid": proc.pid, "startedAt": _now(),
             "command": [str(c) for c in entry["command"]],
             "ownerPid": int(owner_pid or os.getpid()),
             "scope": target or "self", "logPath": str(log_path)}
    write_json(state_path, state)
    return {**state, "status": "started"}


def stop(root: Path | str, name: str, target: str | None = None) -> dict[str, Any]:
    """Orphan-free stop: term the tree, wait grace, verify dead, drop state."""
    root = Path(root)
    state_path = _state_path(root, name, target)
    state = read_json(state_path, None) or {}
    pid = int(state.get("pid") or 0)
    if not pid or not processes.pid_alive(pid):
        state_path.unlink(missing_ok=True)
        return {"name": name, "status": "not-running"}
    grace = float((load_registry(root, target).get(name) or {}).get("stopGraceSeconds", 5))
    processes.signal_process_tree(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and processes.pid_alive(pid):
        time.sleep(0.2)
    if processes.pid_alive(pid):  # escalate (on nt term already == kill)
        processes.signal_process_tree(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.3)
    still = processes.pid_alive(pid)
    if not still:
        state_path.unlink(missing_ok=True)
    return {"name": name, "pid": pid, "status": "stopped" if not still else "kill-failed"}


def status(root: Path | str, reap: bool = True) -> list[dict[str, Any]]:
    """One row per state file across scopes; dead pids reaped, live services
    with a dead owner stopped (orphan recovery at the next invocation)."""
    root = Path(root)
    rows: list[dict[str, Any]] = []
    dirs = [(_state_dir(root, None), None)]
    targets_base = root / ".harness" / "state" / "targets"
    if targets_base.is_dir():
        dirs.extend((d / "services", d.name) for d in targets_base.iterdir() if d.is_dir())
    for base, target in dirs:
        if not base.is_dir():
            continue
        for state_file in sorted(base.glob("*.json")):
            state = read_json(state_file, None) or {}
            pid = int(state.get("pid") or 0)
            alive = processes.pid_alive(pid)
            owner_alive = processes.pid_alive(int(state.get("ownerPid") or 0))
            row = {**state, "alive": alive, "ownerAlive": owner_alive}
            if reap and not alive:
                state_file.unlink(missing_ok=True)
                row["status"] = "reaped-dead"
            elif reap and alive and not owner_alive:
                stop(root, str(state.get("name")), target)
                row["status"] = "stopped-orphan"
            else:
                row["status"] = "running" if alive else "dead"
            rows.append(row)
    return rows


def ensure_autostart(root: Path | str, entry_point: str,
                     target: str | None = None) -> list[dict[str, Any]]:
    """svc-autostart-owners (P1): start every declared service whose
    `autoStart` names this entry point (`ui` / `chat` / `workflow`).
    Never-crash: a failing service is reported, the entry point still opens."""
    rows: list[dict[str, Any]] = []
    try:
        registry = load_registry(root, target)
    except Exception:
        return rows
    for name, entry in sorted(registry.items()):
        if not isinstance(entry, dict) or entry_point not in (entry.get("autoStart") or []):
            continue
        try:
            rows.append(ensure(root, name, target))
        except Exception as exc:  # a dead dependency must not kill the owner
            rows.append({"name": name, "status": "failed", "error": str(exc)[:200]})
    return rows


def stop_owned(root: Path | str, owner_pid: int | None = None) -> list[dict[str, Any]]:
    """Stop every service THIS owner started (state ownerPid match) — the
    explicit counterpart of the status() reap, for clean owner shutdowns."""
    root = Path(root)
    owner = int(owner_pid or os.getpid())
    rows: list[dict[str, Any]] = []
    for row in status(root, reap=False):
        if int(row.get("ownerPid") or 0) != owner or not row.get("alive"):
            continue
        target = None if row.get("scope") in (None, "self") else str(row["scope"])
        rows.append(stop(root, str(row.get("name")), target))
    return rows


def cmd_services(args) -> int:
    """`harness.py services list|start|stop|status [name] [--target T]`."""
    from harness_lib import common

    action = getattr(args, "action", "list") or "list"
    name = getattr(args, "name", None)
    target = getattr(args, "target", None)
    if action == "list":
        registry = load_registry(ROOT, target)
        live = {r["name"]: r for r in status(ROOT, reap=False) if
                r.get("scope") == (target or "self")}
        rows = [{"name": n, "declared": True,
                 "autoStart": ",".join(e.get("autoStart") or []) or None,
                 "ready": (e.get("ready") or {}).get("type"),
                 "status": live.get(n, {}).get("status", "not-running"),
                 "pid": live.get(n, {}).get("pid")}
                for n, e in sorted(registry.items())]
        if common.agent_output_compact():
            common.emit(rows, tsv=True)
        elif getattr(args, "json", False):
            common.emit(rows)
        elif not rows:
            print(f"no services declared in {'target ' + target if target else 'harness'} scope"
                  f" ({REGISTRY_REL})")
        else:
            for r in rows:
                print(f"{r['name']:<24} {r['status']:<12} pid={r['pid'] or '-':<8} "
                      f"ready={r['ready'] or '-'} autoStart={r['autoStart'] or '-'}")
        return 0
    if action == "status":
        rows = status(ROOT)
        common.emit(rows, tsv=True) if getattr(args, "json", False) else [  # TE.5/SPEC-139: flat list -> TSV under compact
            print(f"{r.get('name'):<24} {r['status']:<16} pid={r.get('pid')} "
                  f"scope={r.get('scope')}") for r in rows]
        return 0
    if not name:
        raise HarnessError(f"usage: services {action} <name> [--target T]")
    if action == "start":
        out = ensure(ROOT, name, target)
        print(f"{name}: {out['status']} (pid {out['pid']}, log {out.get('logPath')})")
        return 0
    if action == "stop":
        out = stop(ROOT, name, target)
        print(f"{name}: {out['status']}")
        return 0
    raise HarnessError(f"unknown services action: {action}")


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reg = root / REGISTRY_REL
        reg.parent.mkdir(parents=True)
        reg.write_text(json.dumps({"services": {
            "svc-x": {"command": [sys.executable, "-c", "pass"]}}}), encoding="utf-8")
        assert "svc-x" in load_registry(root)
        try:
            ensure(root, "ghost")
            raise AssertionError("unknown service accepted")
        except HarnessError as exc:
            assert "unknown service" in str(exc)
        # probe primitives: a closed port is not ready; absent probe shape is falsy.
        assert _probe_once({"type": "tcp", "port": 1}) is False
        assert _probe_once({"type": "nope"}) is False
        assert status(root) == []
    print("services self-check: ok")


if __name__ == "__main__":
    _demo()
