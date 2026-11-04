"""Workflow StateStore mirroring and artifact recovery helpers.

The workflow packet on disk remains the human-readable source of truth. This
module owns the optional machine-readable mirror used for recovery/migration so
``scripts/harness.py`` can stay focused on CLI orchestration.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from . import state_store
from .common import now, read_json, write_json

EventSink = Callable[[str, dict[str, Any]], None]

DEFAULT_STATE_STORE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "backend": "file",
    "path": ".harness/state-store",
    "sqlitePath": ".harness/state-store/workflows.sqlite3",
    "mirrorWorkflowState": True,
    "fallbackOnMissingWorkflowJson": True,
}


def state_store_config(workflow_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized workflow state-store configuration."""
    cfg = (workflow_cfg or {}).get("stateStore", {}) if isinstance(workflow_cfg, dict) else {}
    return dict(cfg) if isinstance(cfg, dict) and cfg else dict(DEFAULT_STATE_STORE_CONFIG)


def state_store_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enabled", True) and cfg.get("mirrorWorkflowState", True))


def open_state_store(root: Path, cfg: dict[str, Any]) -> state_store.StateStore:
    backend = str(cfg.get("backend", "file")).lower()
    if backend == "sqlite":
        return state_store.SQLiteStateStore(root / str(cfg.get("sqlitePath", DEFAULT_STATE_STORE_CONFIG["sqlitePath"])))
    return state_store.FileStateStore(root / str(cfg.get("path", DEFAULT_STATE_STORE_CONFIG["path"])))


def state_key(wfid: str, name: str) -> str:
    safe_wfid = re.sub(r"[^A-Za-z0-9_.-]", "-", wfid)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    return f"workflows/{safe_wfid}/{safe_name}"


def put_state(root: Path, cfg: dict[str, Any], wfid: str, name: str, value: dict[str, Any], *, on_error: EventSink | None = None) -> None:
    if not state_store_enabled(cfg):
        return
    try:
        payload = dict(value)
        payload.setdefault("workflowId", wfid)
        payload["stateStoreMirror"] = {"name": name, "updatedAt": now(), "version": 1}
        open_state_store(root, cfg).put_json(state_key(wfid, name), payload)
    except Exception as exc:
        if on_error:
            on_error("workflow_state_store_mirror_failed", {"workflowId": wfid, "name": name, "error": str(exc)[:300]})


def get_state(root: Path, cfg: dict[str, Any], wfid: str, name: str) -> dict[str, Any] | None:
    try:
        data = open_state_store(root, cfg).get_json(state_key(wfid, name), None)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def artifact_specs() -> dict[str, str]:
    """Return JSON workflow artifacts mirrored into StateStore."""
    return {
        "workflow": "workflow.json",
        "plan": "plan.json",
        "locks": "locks.json",
        "validation": "reduce/validation.json",
        "token-audit": "reduce/token-audit.json",
        "reduce-result": "reduce/reducer.result.json",
        "harness-result": "reduce/harness-result.json",
        "reviewer-result": "reduce/reviewer.result.json",
        "merge-plan": "reduce/merge-plan.json",
        "merge-apply-result": "reduce/merge-apply-result.json",
        "merge-reviewer-result": "reduce/merge-reviewer.result.json",
        "rollback-result": "reduce/rollback-result.json",
        "async-collect-result": "async/collect.result.json",
        "async-await-result": "async/await.result.json",
        "async-cancellation": "async/cancellation.json",
    }


def put_artifact(root: Path, cfg: dict[str, Any], wfid: str, name: str, path: Path, *, on_error: EventSink | None = None) -> bool:
    data = read_json(path, None) if path.exists() else None
    if not isinstance(data, dict):
        return False
    payload = dict(data)
    try:
        payload.setdefault("workflowId", wfid)
        payload["stateStoreArtifact"] = {"name": name, "path": str(path.relative_to(root)), "mirroredAt": now()}
    except Exception:
        payload["stateStoreArtifact"] = {"name": name, "path": str(path), "mirroredAt": now()}
    put_state(root, cfg, wfid, name, payload, on_error=on_error)
    return True


def mirror_known_artifacts(root: Path, cfg: dict[str, Any], wfid: str, workflow_dir: Path, *, on_error: EventSink | None = None) -> dict[str, Any]:
    mirrored: list[str] = []
    skipped: list[str] = []
    for name, relpath in artifact_specs().items():
        path = workflow_dir / relpath
        if put_artifact(root, cfg, wfid, name, path, on_error=on_error):
            mirrored.append(name)
        else:
            skipped.append(name)
    return {"workflowId": wfid, "mirrored": mirrored, "skipped": skipped}


def recover_artifacts(root: Path, cfg: dict[str, Any], wfid: str, workflow_dir: Path, *, names: list[str] | None = None, on_recovered: EventSink | None = None) -> dict[str, Any]:
    recovered: list[str] = []
    skipped: list[str] = []
    specs = artifact_specs()
    for name in names or list(specs):
        relpath = specs.get(name)
        if not relpath:
            skipped.append(name)
            continue
        data = get_state(root, cfg, wfid, name)
        if not isinstance(data, dict):
            skipped.append(name)
            continue
        target = workflow_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, data)
        recovered.append(name)
    if recovered and on_recovered:
        on_recovered("workflow_state_store_artifacts_recovered", {"workflowId": wfid, "artifacts": recovered})
    return {"workflowId": wfid, "recovered": recovered, "skipped": skipped}


def summary(root: Path, cfg: dict[str, Any], wfid: str, workflow_dir: Path, *, mirror: bool = False, recover: bool = False, on_event: EventSink | None = None) -> dict[str, Any]:
    if mirror:
        mirror_known_artifacts(root, cfg, wfid, workflow_dir, on_error=on_event)
    recovery = recover_artifacts(root, cfg, wfid, workflow_dir, on_recovered=on_event) if recover else None
    store = open_state_store(root, cfg)
    prefix = f"workflows/{re.sub(r'[^A-Za-z0-9_.-]', '-', wfid)}/"
    entries: list[dict[str, Any]] = []
    for key in store.list_keys(prefix):
        item = store.get_json(key, {})
        artifact = (item.get("stateStoreArtifact") or {}) if isinstance(item, dict) else {}
        entries.append({
            "key": key,
            "name": ((item.get("stateStoreMirror") or {}).get("name") if isinstance(item, dict) else None),
            "artifactPath": artifact.get("path") if isinstance(artifact, dict) else None,
            "workflowId": item.get("workflowId") if isinstance(item, dict) else None,
            "status": item.get("status") if isinstance(item, dict) else None,
            "updatedAt": ((item.get("stateStoreMirror") or {}).get("updatedAt") if isinstance(item, dict) else None),
        })
    result: dict[str, Any] = {"workflowId": wfid, "backend": str(cfg.get("backend", "file")), "enabled": state_store_enabled(cfg), "entries": entries}
    if recovery is not None:
        result["recovery"] = recovery
    return result
