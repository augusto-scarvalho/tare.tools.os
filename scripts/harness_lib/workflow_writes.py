"""Controlled-write orchestration: prepare/merge-plan/apply/rollback/worktrees/review (extracted from harness.py, MF.1 round 2).

Shared harness helpers/constants arrive via bind(env) at import time —
the same idiom as the other extracted harness_lib modules (MF discipline;
this extraction also unblocks HT-T5 subject routing).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_lib import responsibility  # SPEC-161 N-AUTHCHAIN: typed accepter on mergeReview
from harness_lib import sandbox_spawn  # SPEC-148: release protected-path ACLs before disposal
from harness_lib import processes  # cm-5/rec-wf-19: worktree spawns go through the
# CREATE_NO_WINDOW-safe mediation layer — raw runs here flashed console windows
# on the owner's desktop whenever the panel drove a write workflow.


def bind(env: dict[str, Any]) -> None:
    """Receive shared runtime helpers/constants from scripts/harness.py."""
    globals().update(env)


def lock_paths_for_worker(locks: list[dict[str, Any]], wid: str) -> dict[str, Any]:
    write_paths: list[str] = []
    create_paths: list[str] = []
    delete_paths: list[str] = []
    rename_items: list[dict[str, str]] = []
    for lock in locks:
        if str(lock.get("ownerWorkerId")) != wid:
            continue
        mode = str(lock.get("mode"))
        if mode == "write":
            write_paths.append(normalize_repo_path(str(lock.get("path"))))
        elif mode == "create":
            create_paths.append(normalize_repo_path(str(lock.get("path"))))
        elif mode == "delete":
            delete_paths.append(normalize_repo_path(str(lock.get("path"))))
        elif mode == "rename":
            rename_items.append({
                "oldPath": normalize_repo_path(str(lock.get("oldPath") or lock.get("path"))),
                "newPath": normalize_repo_path(str(lock.get("newPath"))),
            })
    return {
        "lockedPaths": sorted(dict.fromkeys(write_paths)),
        "createLockedPaths": sorted(dict.fromkeys(create_paths)),
        "deleteLockedPaths": sorted(dict.fromkeys(delete_paths)),
        "renameLocks": rename_items,
    }

def workflow_prepare_write(wfid: str, *, mode: str = "temp-copy", create_workspaces: bool = True, allow_conflicts: bool = False, approval_token: str | None = None, create_locks: list[str] | None = None, delete_locks: list[str] | None = None, rename_locks: list[str] | None = None) -> dict[str, Any]:
    require_write_approval(approval_token)
    base, workflow = load_workflow(wfid)

    if (workflow or {}).get("target"):
        raise HarnessError(
            "controlled writes on target repositories are not supported yet (HT-T5b)\n"
            "cause: locks/backups/merge machinery is harness-root-relative\n"
            "fix: keep target workflows read-only, or adopt the harness inside the target for write workflows"
        )
    policy = workflow.setdefault("policy", {})
    policy["writeAllowed"] = True
    isolation = workflow.setdefault("executionIsolation", {})
    isolation["mode"] = mode
    isolation["writeSandbox"] = True
    locks, conflicts = compute_write_locks(base, workflow, create_locks=create_locks, delete_locks=delete_locks, rename_locks=rename_locks)
    if conflicts and not allow_conflicts:
        write_json(base / "locks.json", {"workflowId": wfid, "status": "conflict", "mode": mode, "locks": locks, "conflicts": conflicts, "updatedAt": now()})
        workflow_state_put_artifact(wfid, "locks", base / "locks.json")
        raise HarnessError(f"Write lock conflicts detected for workflow {wfid}; inspect {rel(base / 'locks.json')} or rerun with --allow-conflicts after review")
    lock_data = {
        "workflowId": wfid,
        "status": "prepared",
        "mode": mode,
        "parallelWritesAllowed": bool(policy.get("parallelWritesAllowed")),
        "locks": locks,
        "conflicts": conflicts,
        "createdAt": now(),
        "approval": {"required": True, "provided": True, "tokenName": "controlledWrites.approvalToken"},
        "supportedLockModes": ["write", "create", "delete", "rename"],
    }
    write_json(base / "locks.json", lock_data)
    workflow_state_put_artifact(wfid, "locks", base / "locks.json")
    for worker in workflow.get("workers", []):
        wid = str(worker.get("workerId"))
        worker["writeAllowed"] = True
        grouped = lock_paths_for_worker(locks, wid)
        worker.update(grouped)
        if create_workspaces:
            if mode == "temp-copy":
                worker["workspacePath"] = create_temp_workspace(base, wfid, wid, grouped)
            elif mode == "git-worktree":
                worker["workspacePath"] = str((base / "worktrees" / wid).relative_to(ROOT))
                worker["worktreeCommand"] = f"git worktree add -b harness/{wfid}/{wid} {shlex.quote(worker['workspacePath'])} HEAD"
                worker["worktreePrepared"] = False
            else:
                raise HarnessError("supported write isolation modes are temp-copy and git-worktree")
        append_write_instructions(ROOT / str(worker.get("promptPath")), worker, mode)
    workflow["writePreparation"] = {"status": "prepared", "mode": mode, "locksPath": str((base / "locks.json").relative_to(ROOT)), "workspacesCreated": bool(create_workspaces), "updatedAt": now()}
    workflow["phase"] = "write_prepared"
    workflow_update(base, workflow, force=True)
    workflow_event(wfid, "workflow_write_prepared", {"mode": mode, "locks": len(locks), "conflicts": len(conflicts), "workspacesCreated": bool(create_workspaces)})
    return {"workflowId": wfid, "status": "prepared", "mode": mode, "locks": len(locks), "conflicts": conflicts, "locksPath": str((base / "locks.json").relative_to(ROOT))}

def protected_workspace_mutations(wroot: Path, protected_rels: list[str], root: Path) -> list[str]:
    """SPEC-148 P2 pre-merge validator: protected canonical paths whose workspace
    copy differs from the live tree. The spawn-time ACL should make this
    impossible; a hit means containment was bypassed — block the merge loudly.
    Pure (no bound module globals) so scenarios can exercise it standalone."""
    return [rel for rel in protected_rels
            if (wroot / rel).is_file() and (root / rel).is_file()
            and (wroot / rel).read_bytes() != (root / rel).read_bytes()]

def workflow_merge_plan(wfid: str, *, validation_gate: str | None = None) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    protected_rels = sandbox_spawn.protected_rel_paths(ROOT)
    locks_data = read_json(base / "locks.json", None)
    if not locks_data or locks_data.get("status") not in {"prepared", "merged", "applied"}:
        raise HarnessError("Write locks missing or not prepared; run workflow prepare-write first")
    locks = locks_data.get("locks", []) if isinstance(locks_data.get("locks"), list) else []
    workers_out: list[dict[str, Any]] = []
    blocked = False
    all_created: list[str] = []
    all_deleted: list[str] = []
    all_renamed: list[dict[str, str]] = []
    for worker in workflow.get("workers", []):
        wid = str(worker.get("workerId"))
        workspace = worker.get("workspacePath")
        grouped = lock_paths_for_worker(locks, wid)
        write_paths = grouped["lockedPaths"]
        create_paths = grouped["createLockedPaths"]
        delete_paths = grouped["deleteLockedPaths"]
        rename_locks = grouped["renameLocks"]
        changed: list[str] = []
        created: list[str] = []
        deleted: list[str] = []
        renamed: list[dict[str, str]] = []
        conflicts: list[dict[str, Any]] = []
        missing_workspace = False
        if workspace:
            wroot = ROOT / str(workspace)
            missing_workspace = not wroot.exists()
            workspace_files = workspace_file_set(wroot) if not missing_workspace else set()
            allowed_new = set(create_paths) | {str(item.get("newPath")) for item in rename_locks}
            if not missing_workspace:
                for relp in write_paths:
                    src = wroot / relp
                    dst = ROOT / relp
                    if src.exists():
                        if file_changed_between(src, dst):
                            changed.append(relp)
                    elif dst.exists() and relp not in delete_paths and relp not in {str(item.get("oldPath")) for item in rename_locks}:
                        conflicts.append({"path": relp, "reason": "delete-without-delete-lock"})
                for relp in create_paths:
                    src = wroot / relp
                    dst = ROOT / relp
                    if src.exists():
                        if dst.exists():
                            conflicts.append({"path": relp, "reason": "create-target-exists"})
                        else:
                            created.append(relp)
                for relp in delete_paths:
                    src = wroot / relp
                    dst = ROOT / relp
                    if dst.exists() and not src.exists():
                        deleted.append(relp)
                for item in rename_locks:
                    oldp = str(item.get("oldPath"))
                    newp = str(item.get("newPath"))
                    old_src = wroot / oldp
                    new_src = wroot / newp
                    if new_src.exists() and not old_src.exists():
                        if (ROOT / newp).exists():
                            conflicts.append({"oldPath": oldp, "newPath": newp, "reason": "rename-target-exists"})
                        else:
                            renamed.append({"oldPath": oldp, "newPath": newp})
                    elif new_src.exists() and old_src.exists():
                        conflicts.append({"oldPath": oldp, "newPath": newp, "reason": "rename-old-path-still-exists"})
                # Detect unauthorized new files without walking the entire root tree. Root-wide
                # scans become expensive/noisy while a temp-copy workspace is active because the
                # active workflow directory itself contains a copy of the repository.
                unauthorized_created: list[str] = []
                for relp in sorted(workspace_files - allowed_new):
                    if relp.startswith(".harness/workflows/active/") or path_is_excluded(relp):
                        continue
                    if not (ROOT / relp).exists():
                        unauthorized_created.append(relp)
                for relp in unauthorized_created[:50]:
                    conflicts.append({"path": relp, "reason": "created-without-create-or-rename-lock"})
                for relp in protected_workspace_mutations(wroot, protected_rels, ROOT):
                    conflicts.append({"path": relp, "reason": "protected-path-modified"})
        else:
            missing_workspace = True
        risk = "high" if missing_workspace or conflicts or deleted or renamed else ("medium" if len(changed) + len(created) > 20 else "low")
        if (missing_workspace and worker.get("writeAllowed")) or conflicts:
            blocked = True
        ready = bool(changed or created or deleted or renamed) and not missing_workspace and not conflicts
        all_created.extend(created)
        all_deleted.extend(deleted)
        all_renamed.extend(renamed)
        workers_out.append({
            "workerId": wid,
            "workspacePath": workspace,
            "lockedPaths": write_paths,
            "createLockedPaths": create_paths,
            "deleteLockedPaths": delete_paths,
            "renameLocks": rename_locks,
            "filesChanged": sorted(changed),
            "createdFiles": sorted(created),
            "deletedFiles": sorted(deleted),
            "renamedFiles": renamed,
            "mergeRisk": risk,
            "validationGate": validation_gate,
            "missingWorkspace": missing_workspace,
            "conflicts": conflicts,
            "readyForMerge": ready,
        })
    plan = {
        "schemaVersion": "1.1",
        "workflowId": wfid,
        "status": "blocked" if blocked else "pending_approval",
        "mergeMode": "sequential",
        "requiresHumanApproval": True,
        "approvalToken": controlled_write_token(),
        "workers": workers_out,
        "order": [w["workerId"] for w in workers_out if w.get("readyForMerge")],
        "blocked": blocked,
        "validationGate": validation_gate,
        "createdFiles": sorted(dict.fromkeys(all_created)),
        "deletedFiles": sorted(dict.fromkeys(all_deleted)),
        "renamedFiles": all_renamed,
        "createdAt": now(),
    }
    write_json(base / "reduce" / "merge-plan.json", plan)
    workflow_state_put_artifact(wfid, "merge-plan", base / "reduce" / "merge-plan.json")
    lines = [f"# Merge Plan for {wfid}", "", f"Status: `{plan['status']}`", "", "## Merge order", ""]
    for wid in plan["order"]:
        lines.append(f"- `{wid}`")
    lines.extend(["", "## Worker changes", ""])
    for item in workers_out:
        lines.append(f"### {item['workerId']}")
        lines.append(f"- Risk: `{item['mergeRisk']}`")
        lines.append(f"- Ready: `{item['readyForMerge']}`")
        lines.append(f"- Files changed: `{len(item['filesChanged'])}`")
        lines.append(f"- Files created: `{len(item['createdFiles'])}`")
        lines.append(f"- Files deleted: `{len(item['deletedFiles'])}`")
        lines.append(f"- Files renamed: `{len(item['renamedFiles'])}`")
        if item.get("conflicts"):
            lines.append("- Conflicts:")
            for conflict in item["conflicts"][:20]:
                lines.append(f"  - `{conflict}`")
        for label, files in [("Changed", item["filesChanged"]), ("Created", item["createdFiles"]), ("Deleted", item["deletedFiles"] )]:
            if files:
                lines.append(f"- {label}:")
                for file in files[:50]:
                    lines.append(f"  - `{file}`")
        if item["renamedFiles"]:
            lines.append("- Renamed:")
            for ren in item["renamedFiles"][:50]:
                lines.append(f"  - `{ren.get('oldPath')}` -> `{ren.get('newPath')}`")
        lines.append("")
    write_text(base / "reduce" / "merge-plan.md", "\n".join(lines).rstrip() + "\n")
    workflow["mergePlanPath"] = str((base / "reduce" / "merge-plan.json").relative_to(ROOT))
    workflow["phase"] = "merge_planned"
    workflow_update(base, workflow, force=True)
    workflow_event(wfid, "workflow_merge_plan_created", {"status": plan["status"], "readyWorkers": len(plan["order"]), "created": len(all_created), "deleted": len(all_deleted), "renamed": len(all_renamed)})
    return plan

DIFF_PREVIEW_LINES = 40


def _op_preview(op: dict[str, Any], wroot: Path) -> str:
    """Bounded human preview for one planned operation (P-2 interactive apply)."""
    import difflib

    typ = str(op.get("type"))
    relp = str(op.get("path") or op.get("newPath") or "")
    if typ == "modify":
        try:
            before = (ROOT / relp).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            before = []
        after = (wroot / relp).read_text(encoding="utf-8", errors="ignore").splitlines()
        diff = list(difflib.unified_diff(before, after, fromfile=f"a/{relp}", tofile=f"b/{relp}", lineterm=""))
        return "\n".join(diff[:DIFF_PREVIEW_LINES]) + ("\n… [diff truncated]" if len(diff) > DIFF_PREVIEW_LINES else "")
    if typ == "create":
        head = (wroot / relp).read_text(encoding="utf-8", errors="ignore").splitlines()[:20]
        return f"new file {relp}:\n" + "\n".join(head)
    if typ == "delete":
        return f"delete {relp}"
    return f"rename {op.get('oldPath')} -> {op.get('newPath')}"


def collect_interactive_decisions(plan: dict[str, Any], decider) -> dict[str, Any]:
    """P-2 per-item approval, phase 1: walk EVERY planned operation before a
    single byte applies; `decider(descriptor) -> True | False | "all" | "quit"`.
    Returns the filtered plan + the audit trail. "quit" aborts with nothing
    applied; "all" approves the remainder without further prompts."""
    approve_rest = False
    denied: list[dict[str, Any]] = []
    approved = 0
    for item in plan.get("workers", []):
        wroot = ROOT / str(item.get("workspacePath"))
        ops: list[tuple[str, dict[str, Any]]] = (
            [("filesChanged", {"type": "modify", "path": p}) for p in item.get("filesChanged", [])]
            + [("createdFiles", {"type": "create", "path": p}) for p in item.get("createdFiles", [])]
            + [("deletedFiles", {"type": "delete", "path": p}) for p in item.get("deletedFiles", [])]
            + [("renamedFiles", dict(r, type="rename")) for r in item.get("renamedFiles", [])])
        keep: dict[str, list[Any]] = {"filesChanged": [], "createdFiles": [], "deletedFiles": [], "renamedFiles": []}
        for bucket, op in ops:
            descriptor = {"workerId": item.get("workerId"), **op,
                          "preview": _op_preview(op, wroot)}
            verdict = True if approve_rest else decider(descriptor)
            if verdict == "quit":
                raise HarnessError("interactive apply aborted by the operator; nothing was applied")
            if verdict == "all":
                approve_rest = True
                verdict = True
            if verdict:
                keep[bucket].append(op.get("path") if bucket != "renamedFiles"
                                    else {"oldPath": op.get("oldPath"), "newPath": op.get("newPath")})
                approved += 1
            else:
                denied.append({"workerId": item.get("workerId"), "type": op.get("type"),
                               "path": op.get("path") or f"{op.get('oldPath')} -> {op.get('newPath')}"})
        item.update(keep)
    return {"plan": plan, "approved": approved, "deniedByOperator": denied}


def _tty_decider(descriptor: dict[str, Any]):
    """Interactive terminal decider: y approve / n deny / a approve-all / q abort."""
    print(f"\n[{descriptor.get('workerId')}] {descriptor.get('type')}: "
          f"{descriptor.get('path') or descriptor.get('oldPath')}")
    print(descriptor.get("preview", ""))
    while True:
        answer = input("apply? [y]es / [n]o / [a]ll / [q]uit: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"a", "all"}:
            return "all"
        if answer in {"q", "quit"}:
            return "quit"


def workflow_apply_merge(wfid: str, *, approval_token: str | None = None, validation_gate: str | None = None, final_gate: str | None = None, dry_run: bool = False, rollback_on_final_gate_failure: bool = False, rollback_on_validation_gate_failure: bool = False, interactive: bool = False, decider=None) -> dict[str, Any]:
    require_write_approval(approval_token)
    if interactive and dry_run:
        raise HarnessError("--interactive and --dry-run are exclusive: dry-run previews, interactive applies")
    if interactive and decider is None:
        if not sys.stdin.isatty():
            raise HarnessError("interactive apply needs a real terminal (panel/worker contexts run with stdin closed)\n"
                               "fix: run from a shell, or preview with --dry-run and apply non-interactively")
        decider = _tty_decider
    base, workflow = load_workflow(wfid)
    plan = read_json(base / "reduce" / "merge-plan.json", None)
    if dry_run:
        # SPEC-102: preview writes nothing — not even a missing merge plan.
        if plan is None:
            return {
                "workflowId": wfid,
                "dryRun": True,
                "mergePlan": None,
                "nextCommand": f"python scripts/harness.py workflow merge-plan {wfid}",
            }
        locks = read_json(base / "reduce" / "locks.json", None) or read_json(base / "locks.json", None)
        return {
            "workflowId": wfid,
            "dryRun": True,
            "blocked": bool(plan.get("blocked")),
            "order": plan.get("order", []),
            "mergePlanPath": str((base / "reduce" / "merge-plan.json").relative_to(ROOT)),
            "filesPerWorker": {str(item.get("workerId")): len(item.get("filesChanged", [])) for item in plan.get("workers", [])},
            "locks": locks,
            "backupsDir": str((base / "backups").relative_to(ROOT)),
            "wouldBackupBeforeApply": True,
        }
    if plan is None:
        plan = workflow_merge_plan(wfid, validation_gate=validation_gate)
    if plan.get("blocked"):
        raise HarnessError("Merge plan is blocked; inspect reduce/merge-plan.json")
    interactive_audit: dict[str, Any] | None = None
    if interactive:
        # P-2: content-level approval. The token above approved INTENT (batch);
        # this approves each concrete diff before phase 2 touches the tree.
        decisions = collect_interactive_decisions(plan, decider)
        plan = decisions["plan"]
        interactive_audit = {"approved": decisions["approved"],
                             "deniedByOperator": decisions["deniedByOperator"]}
    backups = base / "backups"
    applied: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    for wid in plan.get("order", []):
        item = next((x for x in plan.get("workers", []) if x.get("workerId") == wid), None)
        if not item:
            continue
        wroot = ROOT / str(item.get("workspacePath"))
        worker_backup = backups / wid
        actions: list[dict[str, Any]] = []
        changed_files: list[str] = []
        created_files: list[str] = []
        deleted_files: list[str] = []
        renamed_files: list[dict[str, str]] = []
        for relp in item.get("filesChanged", []):
            src = wroot / relp
            dst = ROOT / relp
            if not src.exists():
                continue
            had_backup = backup_existing(worker_backup, relp)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed_files.append(relp)
            actions.append({"type": "modify", "path": relp, "hadOriginal": had_backup})
        for relp in item.get("createdFiles", []):
            src = wroot / relp
            dst = ROOT / relp
            if not src.exists():
                continue
            if dst.exists():
                raise HarnessError(f"Refusing to create {relp}; target already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            created_files.append(relp)
            actions.append({"type": "create", "path": relp})
        for relp in item.get("deletedFiles", []):
            dst = ROOT / relp
            had_backup = backup_existing(worker_backup, relp)
            if dst.exists():
                remove_path(dst)
            deleted_files.append(relp)
            actions.append({"type": "delete", "path": relp, "hadOriginal": had_backup})
        for ren in item.get("renamedFiles", []):
            oldp = str(ren.get("oldPath"))
            newp = str(ren.get("newPath"))
            src = wroot / newp
            if not src.exists():
                continue
            had_old = backup_existing(worker_backup, oldp)
            had_new = backup_existing(worker_backup, newp)
            old_dst = ROOT / oldp
            new_dst = ROOT / newp
            if old_dst.exists():
                remove_path(old_dst)
            if new_dst.exists():
                remove_path(new_dst)
            new_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, new_dst)
            renamed_files.append({"oldPath": oldp, "newPath": newp})
            actions.append({"type": "rename", "oldPath": oldp, "newPath": newp, "hadOld": had_old, "hadNew": had_new})
        if actions:
            write_json(worker_backup / "__actions.json", {"workflowId": wfid, "workerId": wid, "actions": actions, "createdAt": now()})
        gate_result = run_validation_gate(validation_gate)
        if gate_result:
            validation.append({"workerId": wid, **gate_result})
            if not gate_result.get("passed"):
                status = "validation_blocked"
                rollback_result = None
                if rollback_on_validation_gate_failure or rollback_on_final_gate_failure:
                    rollback_result = workflow_rollback(wfid)
                    status = "rolled_back"
                result = {
                    "workflowId": wfid,
                    "status": status,
                    "failedStage": "validationGate",
                    "failedWorkerId": wid,
                    "applied": applied + [{"workerId": wid, "filesChanged": changed_files, "createdFiles": created_files, "deletedFiles": deleted_files, "renamedFiles": renamed_files, "backupPath": str(worker_backup.relative_to(ROOT))}],
                    "validation": validation,
                    "finalValidation": None,
                    "rollbackOnValidationGateFailure": rollback_on_validation_gate_failure,
                    "rollbackOnFinalGateFailure": rollback_on_final_gate_failure,
                    "rollbackResult": rollback_result,
                    "appliedAt": now(),
                }
                write_json(base / "reduce" / "merge-apply-result.json", result)
                workflow_state_put_artifact(wfid, "merge-apply-result", base / "reduce" / "merge-apply-result.json")
                workflow["phase"] = "rolled_back" if status == "rolled_back" else "merge_blocked"
                workflow["mergeApplyResultPath"] = str((base / "reduce" / "merge-apply-result.json").relative_to(ROOT))
                workflow_update(base, workflow, force=True)
                workflow_event(wfid, "workflow_merge_validation_failed", {"workerId": wid, "status": status, "gate": validation_gate})
                if status == "rolled_back":
                    return result
                raise HarnessError(f"Validation gate {validation_gate!r} failed after merging {wid}; run workflow rollback {wfid} --worker-id {wid} or use --rollback-on-validation-gate-failure")
        applied.append({"workerId": wid, "filesChanged": changed_files, "createdFiles": created_files, "deletedFiles": deleted_files, "renamedFiles": renamed_files, "backupPath": str(worker_backup.relative_to(ROOT))})
    final_validation = run_validation_gate(final_gate)
    status = "applied"
    rollback_result = None
    if final_validation and not final_validation.get("passed"):
        status = "blocked"
        if rollback_on_final_gate_failure:
            rollback_result = workflow_rollback(wfid)
            status = "rolled_back"
    result = {"workflowId": wfid, "status": status, "applied": applied, "validation": validation, "finalValidation": final_validation, "rollbackOnValidationGateFailure": rollback_on_validation_gate_failure, "rollbackOnFinalGateFailure": rollback_on_final_gate_failure, "rollbackResult": rollback_result, "interactive": interactive_audit, "appliedAt": now()}
    write_json(base / "reduce" / "merge-apply-result.json", result)
    workflow_state_put_artifact(wfid, "merge-apply-result", base / "reduce" / "merge-apply-result.json")
    workflow["phase"] = "rolled_back" if status == "rolled_back" else ("merge_blocked" if status == "blocked" else "merged")
    workflow["mergeApplyResultPath"] = str((base / "reduce" / "merge-apply-result.json").relative_to(ROOT))
    workflow_update(base, workflow, force=True)
    workflow_event(wfid, "workflow_merge_applied", {"workers": len(applied), "status": status, "finalValidationPassed": None if final_validation is None else final_validation.get("passed")})
    return result

def workflow_rollback(wfid: str, *, worker_id: str | None = None) -> dict[str, Any]:
    base, _workflow = load_workflow(wfid)
    backups = base / "backups"
    restored: list[str] = []
    removed_created: list[str] = []
    if not backups.exists():
        return {"workflowId": wfid, "restored": [], "removedCreated": [], "message": "No backups found"}
    targets = [backups / worker_id] if worker_id else [p for p in backups.iterdir() if p.is_dir()]
    for target in targets:
        if not target.exists():
            continue
        meta = read_json(target / "__actions.json", {}) or {}
        actions = list(meta.get("actions", [])) if isinstance(meta.get("actions", []), list) else []
        if actions:
            for action in reversed(actions):
                typ = action.get("type")
                if typ == "create":
                    dst = ROOT / str(action.get("path"))
                    if dst.exists():
                        remove_path(dst)
                        removed_created.append(str(action.get("path")))
                elif typ == "modify":
                    path = str(action.get("path"))
                    if restore_backup_file(target, path):
                        restored.append(path)
                elif typ == "delete":
                    path = str(action.get("path"))
                    if restore_backup_file(target, path):
                        restored.append(path)
                elif typ == "rename":
                    newp = str(action.get("newPath"))
                    oldp = str(action.get("oldPath"))
                    new_dst = ROOT / newp
                    if action.get("hadNew"):
                        restore_backup_file(target, newp)
                        restored.append(newp)
                    elif new_dst.exists():
                        remove_path(new_dst)
                        removed_created.append(newp)
                    if action.get("hadOld") and restore_backup_file(target, oldp):
                        restored.append(oldp)
            continue
        # Backward-compatible rollback for older backups.
        for file in target.rglob("*"):
            if file.is_file() and file.name != "__actions.json":
                try:
                    relp = file.relative_to(target / "before")
                except Exception:
                    relp = file.relative_to(target)
                dst = ROOT / relp
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, dst)
                restored.append(relp.as_posix())
    result = {"workflowId": wfid, "workerId": worker_id, "restored": restored, "removedCreated": removed_created, "rolledBackAt": now()}
    write_json(base / "reduce" / "rollback-result.json", result)
    workflow_state_put_artifact(wfid, "rollback-result", base / "reduce" / "rollback-result.json")
    workflow_event(wfid, "workflow_rollback", {"workerId": worker_id, "restored": len(restored), "removedCreated": len(removed_created)})
    return result

def workflow_cleanup_write(wfid: str, *, force: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    removed: list[str] = []
    for name in ["workspaces", "worktrees"]:
        path = base / name
        if path.exists():
            if not force:
                raise HarnessError(f"Refusing to remove {path.relative_to(ROOT)} without --force")
            sandbox_spawn.fs_release(path)  # SPEC-148: deny-ACEs block delete too
            shutil.rmtree(path)
            removed.append(str(path.relative_to(ROOT)))
    workflow["writeCleanup"] = {"removed": removed, "updatedAt": now()}
    workflow_update(base, workflow, force=True)
    workflow_event(wfid, "workflow_write_cleanup", {"removed": removed})
    return {"workflowId": wfid, "removed": removed}

def workflow_prepare_worktrees(wfid: str, *, approval_token: str | None = None, force: bool = False) -> dict[str, Any]:
    require_write_approval(approval_token)
    base, workflow = load_workflow(wfid)
    prep = workflow.get("writePreparation", {}) if isinstance(workflow.get("writePreparation"), dict) else {}
    if prep.get("mode") != "git-worktree":
        raise HarnessError("workflow prepare-worktrees requires prepare-write --mode git-worktree first")
    if not (ROOT / ".git").exists():
        raise HarnessError("git worktree automation requires a Git repository")
    results: list[dict[str, Any]] = []
    for worker in workflow.get("workers", []):
        wid = str(worker.get("workerId"))
        path = ROOT / str(worker.get("workspacePath") or (base / "worktrees" / wid).relative_to(ROOT))
        if path.exists() and not force:
            results.append({"workerId": wid, "path": str(path.relative_to(ROOT)), "status": "exists"})
            continue
        branch = f"harness/{wfid}/{wid}"
        if path.exists() and force:
            sandbox_spawn.fs_release(path)  # SPEC-148: deny-ACEs block delete too
            try:
                processes.run_quiet(["git", "worktree", "remove", "--force", str(path)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                shutil.rmtree(path, ignore_errors=True)
        try:
            proc = processes.run_quiet(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], 124, stdout=exc.stdout or "", stderr=(exc.stderr or "") + "\ntimeout after 30s")
        status = "prepared" if proc.returncode == 0 else "failed"
        if proc.returncode == 0:
            worker["workspacePath"] = str(path.relative_to(ROOT))
            worker["worktreePrepared"] = True
        results.append({"workerId": wid, "path": str(path.relative_to(ROOT)), "branch": branch, "status": status, "stdout": proc.stdout[-1000:], "stderr": proc.stderr[-1000:]})
    workflow_update(base, workflow, force=True)
    workflow_event(wfid, "workflow_worktrees_prepared", {"workers": len(results)})
    return {"workflowId": wfid, "worktrees": results}

def workflow_cleanup_worktrees(wfid: str, *, force: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    removed: list[dict[str, Any]] = []
    for worker in workflow.get("workers", []):
        path_s = worker.get("workspacePath")
        if not path_s or "/worktrees/" not in str(path_s):
            continue
        path = ROOT / str(path_s)
        if not path.exists():
            continue
        sandbox_spawn.fs_release(path)  # SPEC-148: deny-ACEs block delete too
        if (ROOT / ".git").exists():
            cmd = ["git", "worktree", "remove", "--force", str(path)] if force else ["git", "worktree", "remove", str(path)]
            try:
                proc = processes.run_quiet(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                return_code = proc.returncode
                stderr = proc.stderr[-1000:]
            except subprocess.TimeoutExpired as exc:
                return_code = 124
                stderr = ((exc.stderr or "") if isinstance(exc.stderr, str) else "") + "\ntimeout after 30s"
                if force:
                    shutil.rmtree(path, ignore_errors=True)
                    try:
                        processes.run_quiet(["git", "worktree", "prune"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
                    except Exception:
                        pass
            removed.append({"path": str(path.relative_to(ROOT)), "returnCode": return_code, "stderr": stderr})
        else:
            if not force:
                raise HarnessError(f"Refusing to remove {path.relative_to(ROOT)} without --force")
            shutil.rmtree(path)
            removed.append({"path": str(path.relative_to(ROOT)), "returnCode": 0})
    workflow_event(wfid, "workflow_worktrees_cleaned", {"removed": len(removed)})
    return {"workflowId": wfid, "removed": removed}

def workflow_review_merge(wfid: str, *, executor: str | None = None, validate_existing: bool = False) -> dict[str, Any]:
    base, workflow = load_workflow(wfid)
    plan_path = base / "reduce" / "merge-plan.json"
    apply_path = base / "reduce" / "merge-apply-result.json"
    if not plan_path.exists():
        raise HarnessError("merge plan missing; run workflow merge-plan first")
    prompt_path = base / "reduce" / "merge-reviewer.prompt.md"
    review_path = base / "reduce" / "merge-reviewer.result.json"
    if validate_existing:
        data = read_json(review_path, None)
        if data is None:
            raise HarnessError(f"merge reviewer result missing at {review_path.relative_to(ROOT)}")
        errors = validate_reviewer_result(data, expected_workflow_id=wfid)
        if not errors and data.get("approvedForFinalize"):
            workflow["mergeReview"] = {"status": "approved", "path": str(review_path.relative_to(ROOT)), "updatedAt": now(), "actor": responsibility.make_actor("accepter")}
            workflow_update(base, workflow, force=True)
        return {"workflowId": wfid, "reviewResultPath": str(review_path.relative_to(ROOT)), "valid": not errors, "approved": not errors and bool(data.get("approvedForFinalize")), "errors": errors}
    prompt = f"""# Controlled Merge Semantic Reviewer Packet

- Workflow: `{wfid}`
- Merge plan: `{plan_path.relative_to(ROOT)}`
- Apply result: `{apply_path.relative_to(ROOT) if apply_path.exists() else 'not applied yet'}`
- Expected review result: `{review_path.relative_to(ROOT)}`

Review semantic consistency after controlled writes. Inspect API/contract changes, architecture boundaries, security boundaries, validation results, created/deleted/renamed files, and unresolved reducer concerns. Return a valid REVIEWER_RESULT JSON object with keys: workflowId, status, summary, reviewFindings, approvedForFinalize, requiresHumanReview.

{workflow_reduce_lib.SKEPTICAL_REVIEW_BLOCK}
"""
    write_text(prompt_path, prompt)
    result: dict[str, Any] = {"workflowId": wfid, "promptPath": str(prompt_path.relative_to(ROOT)), "reviewResultPath": str(review_path.relative_to(ROOT)), "schema": "schemas/reviewer-result.schema.json", "preparedOnly": True, "executionRequired": True}
    if executor:
        cmd = workflow_spawn_command_for_prompt(str(prompt_path.relative_to(ROOT)), "review", executor)
        result["spawnCommand"] = " ".join(shlex.quote(p) for p in cmd)
    workflow_event(wfid, "workflow_merge_review_prepared", result)
    return result
