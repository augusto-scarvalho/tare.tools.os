"""Controlled-write workflow helpers.

This module keeps the path-locking and sparse workspace policy outside the
large CLI router. It is dependency-light and receives project-specific
callbacks/paths from the caller so the harness command surface stays stable.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Type

DEFAULT_APPROVAL_TOKEN = "I_APPROVE_CONTROLLED_WRITES"


class ControlledWriteError(RuntimeError):
    """Raised when controlled-write policy validation fails."""


def controlled_write_config(workflow_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = (workflow_cfg or {}).get("controlledWrites", {})
    return cfg if isinstance(cfg, dict) else {}


def controlled_write_token(workflow_cfg: dict[str, Any] | None) -> str:
    return str(controlled_write_config(workflow_cfg).get("approvalToken", DEFAULT_APPROVAL_TOKEN))


def require_write_approval(token: str | None, workflow_cfg: dict[str, Any] | None, *, error_cls: Type[Exception] = ControlledWriteError) -> None:
    expected = controlled_write_token(workflow_cfg)
    if token != expected:
        raise error_cls(
            f"Controlled write workflows require explicit approval token {expected!r}\n"
            f"cause: parallel source edits are supervisor-gated by policy\n"
            f"fix: rerun with --approval-token {expected} (policy: docs/CONTROLLED_PARALLEL_WRITES.md)"
        )


def normalize_repo_path(path: str) -> str:
    path = str(path).replace("\\", "/").strip().lstrip("./")
    while "//" in path:
        path = path.replace("//", "/")
    return path


def _is_secret_env(name: str) -> bool:
    """A worker-workspace copy must never carry live credentials (B1, event-log
    integrity round 3: `.env` holds NVIDIA/GEMINI keys and was NOT excluded, so a
    write-worker's temp-copy of the repo root leaked them). Mirrors .gitignore:
    `.env` and `.env.*` are secret; `.env.example` is the checked-in template.
    Case-insensitive: on a case-insensitive FS (Windows/default macOS) `.ENV` IS
    `.env`, so a differently-cased name must not slip the exclusion (reckon 2026-07-21)."""
    low = name.lower()
    return low == ".env" or (low.startswith(".env.") and low != ".env.example")


def workflow_runtime_ignore(dirpath: str, names: list[str], *, harness_root: Path) -> set[str]:
    blocked = {".git", "graphify-out", "node_modules", "__pycache__", ".pytest_cache", "coverage", "dist", "build", "outputs", "data"}
    ignored = {name for name in names if name in blocked or _is_secret_env(name)
               or name.endswith(".pyc") or name.endswith(".log") or name.endswith(".jsonl")}
    d = Path(dirpath).resolve()
    if d == (harness_root / "workflows").resolve():
        ignored.add("active")
    if d == harness_root.resolve():
        ignored.add("runs")
        # Security (workspace-state-exclusion): never fan a worker's temp-copy the
        # supervisor's cross-project state/coordinates. state/state-store hold
        # escalations, worklog, quality-state; targets holds other projects'
        # coordinates + secret refs. Scoped to the .harness root ONLY (not the
        # top-level `blocked` set) because these names also occur inside targets'
        # own trees, which a worker legitimately edits.
        ignored.add("state")
        ignored.add("state-store")
        ignored.add("targets")
    return ignored


def expand_scope_to_files(paths: list[str], *, root: Path, path_is_excluded: Callable[[str], bool]) -> list[str]:
    files: list[str] = []
    for raw in paths:
        p = normalize_repo_path(str(raw).split(":", 1)[0])
        if not p:
            continue
        candidate = root / p
        if candidate.is_file():
            files.append(p)
        elif candidate.is_dir():
            for item in candidate.rglob("*"):
                if not item.is_file():
                    continue
                relp = item.relative_to(root).as_posix()
                if path_is_excluded(relp):
                    continue
                files.append(relp)
    return sorted(dict.fromkeys(files))


def parse_worker_path_spec(spec: str, *, kind: str, error_cls: Type[Exception] = ControlledWriteError) -> dict[str, str]:
    parts = str(spec).split(":")
    if kind == "rename":
        if len(parts) < 3:
            raise error_cls(f"rename lock must use worker-id:old-path:new-path, got {spec!r}")
        worker_id = parts[0].strip()
        old_path = normalize_repo_path(parts[1])
        new_path = normalize_repo_path(":".join(parts[2:]))
        if not worker_id or not old_path or not new_path:
            raise error_cls(f"invalid rename lock spec {spec!r}")
        return {"ownerWorkerId": worker_id, "path": old_path, "oldPath": old_path, "newPath": new_path, "mode": "rename"}
    if len(parts) < 2:
        raise error_cls(f"{kind} lock must use worker-id:path, got {spec!r}")
    worker_id = parts[0].strip()
    path = normalize_repo_path(":".join(parts[1:]))
    if not worker_id or not path:
        raise error_cls(f"invalid {kind} lock spec {spec!r}")
    return {"ownerWorkerId": worker_id, "path": path, "mode": kind}


def manual_write_locks(
    create_specs: list[str] | None = None,
    delete_specs: list[str] | None = None,
    rename_specs: list[str] | None = None,
    *,
    now_func: Callable[[], str],
    error_cls: Type[Exception] = ControlledWriteError,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in create_specs or []:
        item = parse_worker_path_spec(spec, kind="create", error_cls=error_cls)
        item["createdAt"] = now_func()
        out.append(item)
    for spec in delete_specs or []:
        item = parse_worker_path_spec(spec, kind="delete", error_cls=error_cls)
        item["createdAt"] = now_func()
        out.append(item)
    for spec in rename_specs or []:
        item = parse_worker_path_spec(spec, kind="rename", error_cls=error_cls)
        item["createdAt"] = now_func()
        out.append(item)
    return out


def lock_paths(lock: dict[str, Any]) -> list[str]:
    mode = str(lock.get("mode"))
    if mode == "rename":
        return [normalize_repo_path(str(lock.get("oldPath") or lock.get("path") or "")), normalize_repo_path(str(lock.get("newPath") or ""))]
    return [normalize_repo_path(str(lock.get("path") or ""))]


def compute_write_locks(
    base: Path,
    workflow: dict[str, Any],
    *,
    root: Path,
    worker_scope_paths: Callable[[Path, dict[str, Any]], list[str]],
    path_is_excluded: Callable[[str], bool],
    now_func: Callable[[], str],
    create_locks: list[str] | None = None,
    delete_locks: list[str] | None = None,
    rename_locks: list[str] | None = None,
    workers: list[dict[str, Any]] | None = None,
    error_cls: Type[Exception] = ControlledWriteError,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workers = workers or list(workflow.get("workers", []))
    ownership: dict[str, str] = {}
    locks: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    valid_workers = {str(w.get("workerId")) for w in workers}
    for worker in workers:
        wid = str(worker.get("workerId"))
        paths = worker_scope_paths(base, worker)
        for file in expand_scope_to_files(paths, root=root, path_is_excluded=path_is_excluded):
            owner = ownership.get(file)
            if owner and owner != wid:
                conflicts.append({"path": file, "owners": sorted({owner, wid}), "reason": "write-lock-conflict"})
            else:
                ownership[file] = wid
                locks.append({"path": file, "ownerWorkerId": wid, "mode": "write", "createdAt": now_func()})
    for lock in manual_write_locks(create_locks, delete_locks, rename_locks, now_func=now_func, error_cls=error_cls):
        wid = str(lock.get("ownerWorkerId"))
        if wid not in valid_workers:
            conflicts.append({"path": lock.get("path"), "ownerWorkerId": wid, "mode": lock.get("mode"), "reason": "unknown-worker"})
            continue
        mode = str(lock.get("mode"))
        paths = [p for p in lock_paths(lock) if p]
        for path in paths:
            owner = ownership.get(path)
            if owner and owner != wid:
                conflicts.append({"path": path, "owners": sorted({owner, wid}), "mode": mode, "reason": "manual-lock-conflict"})
        if mode == "create" and (root / str(lock.get("path"))).exists():
            conflicts.append({"path": lock.get("path"), "ownerWorkerId": wid, "mode": mode, "reason": "create-path-already-exists"})
        if mode in {"delete", "rename"} and not (root / str(lock.get("oldPath") or lock.get("path"))).exists():
            conflicts.append({"path": lock.get("oldPath") or lock.get("path"), "ownerWorkerId": wid, "mode": mode, "reason": f"{mode}-source-missing"})
        if mode == "rename" and (root / str(lock.get("newPath"))).exists():
            conflicts.append({"path": lock.get("newPath"), "ownerWorkerId": wid, "mode": mode, "reason": "rename-target-already-exists"})
        for path in paths:
            ownership[path] = wid
        locks.append(lock)
    return locks, conflicts


def create_temp_workspace(
    base: Path,
    wfid: str,
    worker_id: str,
    lock_scope: dict[str, Any] | None = None,
    *,
    root: Path,
    harness_root: Path,
    path_is_excluded: Callable[[str], bool],
    write_json_func: Callable[[Path, Any], None],
    now_func: Callable[[], str],
) -> str:
    workspaces = base / "workspaces"
    target = workspaces / worker_id
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    ignore = lambda dirpath, names: workflow_runtime_ignore(dirpath, names, harness_root=harness_root)
    if not lock_scope:
        shutil.copytree(root, target, ignore=ignore)
        return str(target.relative_to(root))
    target.mkdir(parents=True, exist_ok=True)

    def copy_existing(rel_path: str) -> None:
        rel_norm = normalize_repo_path(rel_path)
        if not rel_norm or path_is_excluded(rel_norm) or rel_norm.startswith(".harness/workflows/active/"):
            return
        src = root / rel_norm
        dst = target / rel_norm
        if src.is_dir():
            shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    copy_paths: list[str] = []
    copy_paths.extend(str(x) for x in lock_scope.get("lockedPaths", []) or [])
    copy_paths.extend(str(x) for x in lock_scope.get("deleteLockedPaths", []) or [])
    for item in lock_scope.get("renameLocks", []) or []:
        if isinstance(item, dict):
            copy_paths.append(str(item.get("oldPath", "")))
            new_path = normalize_repo_path(str(item.get("newPath", "")))
            if new_path:
                (target / new_path).parent.mkdir(parents=True, exist_ok=True)
    for rel_path in sorted(dict.fromkeys(copy_paths)):
        copy_existing(rel_path)
    for rel_path in lock_scope.get("createLockedPaths", []) or []:
        rel_norm = normalize_repo_path(str(rel_path))
        if rel_norm:
            (target / rel_norm).parent.mkdir(parents=True, exist_ok=True)
    write_json_func(target / ".harness-workspace.json", {
        "schemaVersion": "1.0",
        "workflowId": wfid,
        "workerId": worker_id,
        "mode": "temp-copy-sparse",
        "lockedPaths": sorted(dict.fromkeys(str(x) for x in lock_scope.get("lockedPaths", []) or [])),
        "createLockedPaths": sorted(dict.fromkeys(str(x) for x in lock_scope.get("createLockedPaths", []) or [])),
        "deleteLockedPaths": sorted(dict.fromkeys(str(x) for x in lock_scope.get("deleteLockedPaths", []) or [])),
        "renameLocks": lock_scope.get("renameLocks", []) or [],
        "createdAt": now_func(),
    })
    return str(target.relative_to(root))


def append_write_instructions(prompt_path: Path, worker: dict[str, Any], mode: str, *, read_text_func: Callable[[Path], str], write_text_func: Callable[[Path, str], None]) -> None:
    text = read_text_func(prompt_path)
    text = text.replace("- Write allowed: `False`", "- Write allowed: `True`")
    text += f"""

## Controlled write overlay

This worker is part of a controlled write workflow.

- Isolation mode: `{mode}`.
- Workspace path: `{worker.get('workspacePath', 'not prepared')}`.
- Write only paths listed in `lockedPaths`, `createLockedPaths`, `deleteLockedPaths`, or `renameLocks` for this worker.
- Create new files only when covered by a create lock.
- Delete files only when covered by a delete lock.
- Rename or move files only when covered by a rename lock.
- Protected canonical files (`AGENTS.md`, `CLAUDE.md`, `.harness/prompts/`, and the rest of `.harness/protected-files.json`) are OS-locked read-only inside your workspace (SPEC-148). A `PermissionError` on them is by design: treat the file as off-limits, never retry or try to unlock it; mutation attempts surface at merge as `protected-path-modified` and block the merge.
- Save the required `WORKER_RESULT` to the absolute path in `HARNESS_WORKER_RESULT_PATH`.
- Do not modify the main working tree directly.
- Do not merge your own changes. The harness will build a merge plan and apply it only after approval.
"""
    write_text_func(prompt_path, text)


def validate_write_workflow_ready(workflow: dict[str, Any], *, error_cls: Type[Exception] = ControlledWriteError) -> None:
    policy = workflow.get("policy", {}) if isinstance(workflow.get("policy"), dict) else {}
    if not policy.get("writeAllowed"):
        raise error_cls("Workflow is not write-enabled. Plan with --write-allowed and explicit approval, or run read-only.")
    prep = workflow.get("writePreparation", {}) if isinstance(workflow.get("writePreparation"), dict) else {}
    if prep.get("status") != "prepared":
        raise error_cls("Write workflow is not prepared. Run workflow prepare-write first.")


def _self_check() -> None:
    import os
    import tempfile

    hroot = Path(tempfile.mkdtemp()) / ".harness"
    for rel in ("state/escalations.json", "state-store/db.sqlite", "targets/x/target.json"):
        p = hroot / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    for d in ("workflows/WF-1", "workflows/active", "prompts", "runs"):
        (hroot / d).mkdir(parents=True, exist_ok=True)

    # At the .harness root: sensitive supervisor state is excluded; packet dirs stay.
    ignored = workflow_runtime_ignore(str(hroot), os.listdir(hroot), harness_root=hroot)
    for leaf in ("state", "state-store", "targets", "runs"):
        assert leaf in ignored, f"{leaf} must be excluded from worker workspaces: {ignored}"
    for keep in ("workflows", "prompts"):
        assert keep not in ignored, f"{keep} must stay copied for workers: {ignored}"

    # Inside workflows/: only `active` is dropped; the WF-1 packet dir survives.
    wf_ignored = workflow_runtime_ignore(str(hroot / "workflows"), os.listdir(hroot / "workflows"), harness_root=hroot)
    assert "active" in wf_ignored and "WF-1" not in wf_ignored, wf_ignored

    # Below the root the names state/targets must NOT be blanket-excluded (a target's
    # own tree may contain them); the harness-root branch is what scopes the exclusion.
    tgt_tree = hroot / "targets" / "x"
    (tgt_tree / "state").mkdir(parents=True, exist_ok=True)
    deep = workflow_runtime_ignore(str(tgt_tree), os.listdir(tgt_tree), harness_root=hroot)
    assert "state" not in deep, f"state must not be excluded deep inside a target tree: {deep}"

    # B1 (secret exclusion): `.env`/`.env.*` carry live credentials and must NEVER be
    # copied into a worker workspace (copytree of the repo ROOT feeds this); the
    # `.env.example` template stays. Checked at every dir level (secrets are at the
    # repo root, not the .harness root).
    repo = Path(tempfile.mkdtemp())
    for f in (".env", ".env.local", ".env.example", "AGENTS.md"):
        (repo / f).write_text("x", encoding="utf-8")
    root_ignored = workflow_runtime_ignore(str(repo), os.listdir(repo), harness_root=repo / ".harness")
    assert ".env" in root_ignored and ".env.local" in root_ignored, root_ignored
    assert ".env.example" not in root_ignored and "AGENTS.md" not in root_ignored, root_ignored
    # case-insensitive (Windows/default-macOS FS): `.ENV` IS `.env`, must not slip.
    assert _is_secret_env(".ENV") and _is_secret_env(".Env.local"), "case-insensitive secret match"
    assert not _is_secret_env(".env.example") and not _is_secret_env(".ENV.EXAMPLE"), "template kept"

    print("controlled_writes self-check ok")


if __name__ == "__main__":
    _self_check()
