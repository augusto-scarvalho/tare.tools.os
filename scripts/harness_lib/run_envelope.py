#!/usr/bin/env python3
"""SPEC-178 First-Class Run / Execution Envelope V1.

Provides a unified execution envelope (`RunEnvelope`) that links task execution,
subagent worker spawns, workflows, routing decisions, and validation outcomes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from harness_lib import event_ledger, processes

RUNS_STORE = ".harness/state/runs.json"

RUN_STATUSES = frozenset({
    "created",
    "running",
    "completed",
    "partial",
    "failed",
    "blocked",
    "abandoned",
})


def generate_run_id(prefix: str = "run") -> str:
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand_hex = secrets.token_hex(4)
    return f"{prefix}-{now_str}-{rand_hex}"


def load_runs(root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    r_path = Path(root) if root else Path.cwd()
    store = r_path / ".harness" / "state" / "runs.json"
    if not store.exists():
        return {}
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
        return dict(data.get("runs", {}))
    except Exception:
        return {}


def save_runs(runs: dict[str, dict[str, Any]], root: Path | str | None = None) -> None:
    r_path = Path(root) if root else Path.cwd()
    store = r_path / ".harness" / "state" / "runs.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
    }
    store.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def start_run(
    task_id: str,
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    workflow_id: str | None = None,
    target: str | None = None,
    config_versions: dict[str, str] | None = None,
    decision_ids: list[str] | None = None,
    trace_id: str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    r_path = Path(root) if root else Path.cwd()
    r_id = run_id or generate_run_id()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    t_id = trace_id
    if not t_id:
        try:
            from harness_lib import tracing
            t_id = tracing.stable_trace_id(workflow_id)
        except Exception:
            t_id = secrets.token_hex(16)

    envelope = {
        "runId": r_id,
        "taskId": task_id,
        "parentRunId": parent_run_id,
        "workflowId": workflow_id,
        "target": target or "local",
        "startedAt": now_iso,
        "endedAt": None,
        "status": "running",
        "pid": os.getpid(),
        "epoch": 1,
        "configVersions": config_versions or {"harness": "1.0"},
        "decisionIds": list(decision_ids or []),
        "traceId": t_id,
        "validationIds": [],
        "failureReason": None,
    }

    runs = load_runs(r_path)
    runs[r_id] = envelope
    save_runs(runs, r_path)

    try:
        event_ledger.append("run_started", {
            "runId": r_id,
            "taskId": task_id,
            "workflowId": workflow_id,
            "parentRunId": parent_run_id,
            "pid": os.getpid(),
        }, root=r_path)
    except Exception:
        pass

    return envelope


def update_run(
    run_id: str,
    *,
    add_decision_id: str | None = None,
    add_validation_id: str | None = None,
    status: str | None = None,
    failure_reason: str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    r_path = Path(root) if root else Path.cwd()
    runs = load_runs(r_path)
    if run_id not in runs:
        return None

    envelope = runs[run_id]
    if add_decision_id and add_decision_id not in envelope["decisionIds"]:
        envelope["decisionIds"].append(add_decision_id)
    if add_validation_id and add_validation_id not in envelope["validationIds"]:
        envelope["validationIds"].append(add_validation_id)
    if status:
        if status not in RUN_STATUSES:
            raise ValueError(f"Invalid status '{status}', must be one of {RUN_STATUSES}")
        envelope["status"] = status
    if failure_reason:
        envelope["failureReason"] = failure_reason

    save_runs(runs, r_path)
    return envelope


def finish_run(
    run_id: str,
    status: str = "completed",
    *,
    failure_reason: str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    r_path = Path(root) if root else Path.cwd()
    runs = load_runs(r_path)
    if run_id not in runs:
        return None

    if status not in RUN_STATUSES:
        raise ValueError(f"Invalid status '{status}', must be one of {RUN_STATUSES}")

    envelope = runs[run_id]
    envelope["status"] = status
    envelope["endedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if failure_reason:
        envelope["failureReason"] = failure_reason

    save_runs(runs, r_path)

    try:
        event_ledger.append("run_completed" if status == "completed" else "run_finished", {
            "runId": run_id,
            "status": status,
            "failureReason": failure_reason,
        }, root=r_path)
    except Exception:
        pass

    return envelope


def detect_abandoned_runs(root: Path | str | None = None) -> list[str]:
    """Detect active runs whose recorded PID is dead or non-existent, marking them abandoned."""
    r_path = Path(root) if root else Path.cwd()
    runs = load_runs(r_path)
    abandoned: list[str] = []

    for r_id, envelope in runs.items():
        if envelope.get("status") in ("created", "running"):
            pid = envelope.get("pid")
            if isinstance(pid, int):
                if not processes.pid_alive(pid):
                    envelope["status"] = "abandoned"
                    envelope["endedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                    envelope["failureReason"] = f"Process PID {pid} died unexpectedly"
                    abandoned.append(r_id)
                    try:
                        event_ledger.append("run_abandoned", {
                            "runId": r_id,
                            "pid": pid,
                            "reason": envelope["failureReason"],
                        }, root=r_path)
                    except Exception:
                        pass


    if abandoned:
        save_runs(runs, r_path)

    return abandoned
