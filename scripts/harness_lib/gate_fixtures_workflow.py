"""Gate fixtures — workflow domain (SPEC — MF.2 extraction).

Moved out of scripts/spec_test_gate.py to keep the gate runner readable
and cheap to load; shared gate helpers arrive via bind(env) from the
gate at import time (same idiom as harness_lib.workflow_reduce).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_lib.workflow_plan import width_declaration_required


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from spec_test_gate."""
    globals().update(env)


def check_controlled_write_fixture() -> list[dict[str, str]]:
    """Exercise controlled write planning, create/delete/rename merge detection, and rollback.

    Uses CLI for plan/prepare and direct harness function calls for merge/apply/rollback
    to avoid slow subprocess churn while still validating runtime behavior.
    """
    cleanup_test_artifacts()
    out: list[dict[str, str]] = []
    fixture = ROOT / "tmp-workflow-write-fixture"
    shutil.rmtree(fixture, ignore_errors=True)
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "existing.txt").write_text("original\n", encoding="utf-8")
    (fixture / "delete.txt").write_text("delete-me\n", encoding="utf-8")
    (fixture / "rename-old.txt").write_text("rename-me\n", encoding="utf-8")
    cmd = [sys.executable, "scripts/harness.py", "workflow", "plan", "--profile", "repository-review", "--split", "explicit", "--shard", "tmp-workflow-write-fixture", "--max-workers", "1", "--task", "controlled write fixture", "--write-allowed", "--approval-token", "I_APPROVE_CONTROLLED_WRITES"]
    proc = run_fixture_command(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        shutil.rmtree(fixture, ignore_errors=True)
        return [result("workflow:controlled-write-fixture:plan", "fail", proc.stderr.strip()[:300])]
    try:
        wfid = json.loads(proc.stdout)["workflowId"]
        mark_fixture_workflow(wfid, "controlled-write")
    except Exception as exc:
        shutil.rmtree(fixture, ignore_errors=True)
        return [result("workflow:controlled-write-fixture:plan", "fail", f"invalid output: {exc}")]
    try:
        proc = run_fixture_command([
            sys.executable, "scripts/harness.py", "workflow", "prepare-write", wfid,
            "--approval-token", "I_APPROVE_CONTROLLED_WRITES",
            "--create-lock", "worker-001:tmp-workflow-write-fixture/created.txt",
            "--delete-lock", "worker-001:tmp-workflow-write-fixture/delete.txt",
            "--rename-lock", "worker-001:tmp-workflow-write-fixture/rename-old.txt:tmp-workflow-write-fixture/rename-new.txt",
        ], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out.append(result("workflow:controlled-write-fixture:prepare", "pass" if proc.returncode == 0 else "fail", proc.stderr.strip()[:300] if proc.returncode else "ok"))
        if proc.returncode != 0:
            return out
        wf = json.loads((HARNESS / "workflows" / "active" / wfid / "workflow.json").read_text(encoding="utf-8"))
        workspace = ROOT / wf["workers"][0]["workspacePath"]
        sparse_marker = workspace / ".harness-workspace.json"
        sparse_ok = sparse_marker.exists() and not (workspace / "AGENTS.md").exists() and (workspace / "tmp-workflow-write-fixture" / "existing.txt").exists()
        out.append(result("workflow:controlled-write-fixture:sparse-temp-copy", "pass" if sparse_ok else "fail", "locked paths only" if sparse_ok else "workspace copied unexpected repository files or missed locked files"))
        if not sparse_ok:
            return out
        (workspace / "tmp-workflow-write-fixture" / "existing.txt").write_text("changed\n", encoding="utf-8")
        (workspace / "tmp-workflow-write-fixture" / "created.txt").write_text("created\n", encoding="utf-8")
        (workspace / "tmp-workflow-write-fixture" / "delete.txt").unlink()
        (workspace / "tmp-workflow-write-fixture" / "rename-old.txt").rename(workspace / "tmp-workflow-write-fixture" / "rename-new.txt")
        try:
            merge = call_harness_function("workflow_merge_plan", wfid)
            ok = bool(merge.get("createdFiles")) and bool(merge.get("deletedFiles")) and bool(merge.get("renamedFiles"))
            out.append(result("workflow:controlled-write-fixture:merge-plan", "pass" if ok else "fail", "create/delete/rename planned" if ok else str(merge)[:300]))
            if not ok:
                return out
            apply = call_harness_function("workflow_apply_merge", wfid, approval_token="I_APPROVE_CONTROLLED_WRITES")
            out.append(result("workflow:controlled-write-fixture:apply", "pass" if apply.get("status") == "applied" else "fail", "ok" if apply.get("status") == "applied" else str(apply)[:300]))
            if apply.get("status") != "applied":
                return out
            rb = call_harness_function("workflow_rollback", wfid)
            restored = (fixture / "existing.txt").read_text(encoding="utf-8") == "original\n" and (fixture / "delete.txt").exists() and (fixture / "rename-old.txt").exists() and not (fixture / "created.txt").exists() and not (fixture / "rename-new.txt").exists()
            out.append(result("workflow:controlled-write-fixture:rollback", "pass" if restored else "fail", "ok" if restored else str(rb)[:300]))
            apply = call_harness_function("workflow_apply_merge", wfid, approval_token="I_APPROVE_CONTROLLED_WRITES", validation_gate="invalid-gate", rollback_on_validation_gate_failure=True)
            rolled = apply.get("status") == "rolled_back"
            restored2 = (fixture / "existing.txt").read_text(encoding="utf-8") == "original\n" and (fixture / "delete.txt").exists() and (fixture / "rename-old.txt").exists() and not (fixture / "created.txt").exists() and not (fixture / "rename-new.txt").exists()
            out.append(result("workflow:controlled-write-fixture:validation-rollback", "pass" if rolled and restored2 else "fail", "rolled back" if rolled and restored2 else str(apply)[:300]))
        except Exception as exc:
            out.append(result("workflow:controlled-write-fixture:runtime", "fail", str(exc)[:300]))
    finally:
        shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        shutil.rmtree(fixture, ignore_errors=True)
    return out

def check_workflow_lifecycle_fixture() -> list[dict[str, str]]:
    """Exercise a tiny workflow lifecycle without requiring a real agent executor."""
    cleanup_test_artifacts()
    out: list[dict[str, str]] = []
    cmd = [sys.executable, "scripts/harness.py", "workflow", "plan", "--profile", "repository-review", "--split", "explicit", "--shard", "specs/00-universal", "--shard", "tasks", "--task", "workflow lifecycle fixture"]
    proc = run_fixture_command(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return [result("workflow:lifecycle-fixture:plan", "fail", proc.stderr.strip()[:300])]
    try:
        payload = json.loads(proc.stdout)
        wfid = payload["workflowId"]
        mark_fixture_workflow(wfid, "workflow-lifecycle")
    except Exception as exc:
        return [result("workflow:lifecycle-fixture:plan", "fail", f"invalid output: {exc}")]
    base = HARNESS / "workflows" / "active" / wfid
    try:
        workflow = read_json(base / "workflow.json", {}) or {}
        workers = workflow.get("workers", [])
        if len(workers) < 2:
            out.append(result("workflow:lifecycle-fixture:workers", "fail", "expected at least two workers"))
            return out
        for idx, worker in enumerate(workers[:2], start=1):
            status = "done" if idx == 1 else "failed"
            wr = {
                "workflowId": wfid,
                "workerId": worker["workerId"],
                "workerRole": worker.get("workerRole", "map-worker"),
                "status": status,
                "summary": f"fixture {status}",
                "itemsAnalyzed": ["specs/00-universal" if idx == 1 else "tasks"],
                "findings": [],
                "sourceFilesVerified": [],
                "graphify": {"status": "not_applicable", "queries": []}
            }
            (base / worker["resultPath"]).write_text(json.dumps(wr, indent=2) + "\n", encoding="utf-8")
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "validate-results", wfid], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out.append(result("workflow:lifecycle-fixture:validate", "pass" if proc.returncode == 0 else "fail", proc.stderr.strip()[:300] if proc.returncode else "ok"))
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "reduce", wfid], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            out.append(result("workflow:lifecycle-fixture:reduce", "fail", proc.stderr.strip()[:300]))
        else:
            red = json.loads(proc.stdout)
            out.append(result("workflow:lifecycle-fixture:failed-status-semantic", "pass" if red.get("status") in {"partial", "failed"} and red.get("workersFailed", 0) >= 1 else "fail", f"status={red.get('status')}; workersFailed={red.get('workersFailed')}"))
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "finalize", wfid, "--no-update-context", "--no-regenerate-handoff"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out.append(result("workflow:lifecycle-fixture:finalize", "pass" if proc.returncode == 0 else "fail", proc.stderr.strip()[:300] if proc.returncode else "ok"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
        cleanup_test_artifacts()
    return out

def check_workflow_execute_output_fixture() -> list[dict[str, str]]:
    """Ensure workflow execute exposes run validation at top level."""
    executors_path = HARNESS / "routing" / "executors.json"
    original = executors_path.read_bytes()  # SPEC-108 H2: byte-exact restore
    fake_path = ROOT / "_tmp_fixture_executor.py"
    try:
        fake_path.write_text("""#!/usr/bin/env python3
import json, os, pathlib
result = pathlib.Path(os.environ['HARNESS_WORKER_RESULT_PATH'])
wfid = os.environ['HARNESS_WORKFLOW_ID']
wid = os.environ['HARNESS_WORKER_ID']
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps({
  'workflowId': wfid,
  'workerId': wid,
  'workerRole': 'map-worker',
  'status': 'done',
  'summary': 'fixture execute worker complete',
  'itemsAnalyzed': ['README.md'],
  'findings': [],
  'sourceFilesVerified': ['README.md'],
  'graphify': {'status': 'not_applicable', 'queries': []},
  'nextStep': None
}, indent=2) + '\\n')
""", encoding="utf-8")
        fake_path.chmod(0o755)
        data = json.loads(original)
        data.setdefault("executors", {})["fixture"] = {
            "type": "cli",
            "description": "Harness workflow execute fixture executor",
            "commandTemplate": [sys.executable, str(fake_path.relative_to(ROOT)), "{prompt}"],
            "runnable": True,
            "placeholder": False,
            "defaultSpawn": {"profile": "scan", "model": "fixture", "effort": "low", "agent": "fixture"},
            "runtimeLimits": {"maxConcurrency": 1}
        }
        executors_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            payload = call_harness_function(
                "workflow_execute",
                "workflow execute output fixture",
                profile_name="repository-review",
                split="explicit",
                shards=["README.md"],
                executor_name="fixture",
                finalize=False,
            )
        except Exception as exc:
            return [result("workflow:execute-output-fixture", "fail", str(exc)[:300])]
        wfid = payload.get("workflowId")
        if wfid:
            mark_fixture_workflow(str(wfid), "workflow-execute-output")
        ok = isinstance(payload.get("validation"), dict) and payload.get("validation", {}).get("workflowId") == wfid
        return [result("workflow:execute-output-fixture:top-level-validation", "pass" if ok else "fail", "validation exposed" if ok else "validation missing/null")]
    finally:
        executors_path.write_bytes(original)
        fake_path.unlink(missing_ok=True)
        cleanup_test_artifacts()

def check_git_worktree_fixture() -> list[dict[str, str]]:
    """Exercise git-worktree controlled write isolation in a temporary Git repository."""
    cleanup_test_artifacts()
    if shutil.which("git") is None:
        return [result("workflow:git-worktree-fixture", "skip", "git not available")]
    tmp = Path(tempfile.mkdtemp(prefix="harness-worktree-fixture-"))
    out: list[dict[str, str]] = []
    try:
        for item in ["scripts", ".harness", "schemas", "tools", "docs"]:
            src = ROOT / item
            dst = tmp / item
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", "*.jsonl", "WF-*", "validation-results.jsonl", "active", "runs"))
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        for item in ["README.md", "AGENTS.md", "EXPORT_MANIFEST.md"]:
            if (ROOT / item).exists():
                shutil.copy2(ROOT / item, tmp / item)
        active = tmp / ".harness" / "workflows" / "active"
        shutil.rmtree(active, ignore_errors=True)
        active.mkdir(parents=True, exist_ok=True)
        (active / ".gitkeep").touch()
        run_fixture_command(["git", "init"], cwd=tmp, timeout=20)
        run_fixture_command(["git", "config", "user.email", "harness@example.invalid"], cwd=tmp, timeout=20)
        run_fixture_command(["git", "config", "user.name", "Harness Fixture"], cwd=tmp, timeout=20)
        run_fixture_command(["git", "add", "."], cwd=tmp, timeout=30)
        commit = run_fixture_command(["git", "commit", "-m", "fixture"], cwd=tmp, timeout=30)
        if commit.returncode != 0:
            return [result("workflow:git-worktree-fixture:commit", "fail", commit.stderr.strip()[:300] or commit.stdout.strip()[:300])]
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "plan", "--profile", "repository-review", "--split", "explicit", "--shard", "README.md", "--max-workers", "1", "--task", "git worktree fixture", "--write-allowed", "--approval-token", "I_APPROVE_CONTROLLED_WRITES"], cwd=tmp, timeout=30)
        if proc.returncode != 0:
            return [result("workflow:git-worktree-fixture:plan", "fail", proc.stderr.strip()[:300])]
        wfid = json.loads(proc.stdout)["workflowId"]
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "prepare-write", wfid, "--mode", "git-worktree", "--approval-token", "I_APPROVE_CONTROLLED_WRITES"], cwd=tmp, timeout=45)
        out.append(result("workflow:git-worktree-fixture:prepare-write", "pass" if proc.returncode == 0 else "fail", "ok" if proc.returncode == 0 else proc.stderr.strip()[:300]))
        if proc.returncode != 0:
            return out
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "prepare-worktrees", wfid, "--approval-token", "I_APPROVE_CONTROLLED_WRITES"], cwd=tmp, timeout=60)
        ok_prepare = proc.returncode == 0 and '"status": "prepared"' in proc.stdout
        out.append(result("workflow:git-worktree-fixture:prepare-worktrees", "pass" if ok_prepare else "fail", "prepared" if ok_prepare else (proc.stderr or proc.stdout)[:300]))
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "cleanup-worktrees", wfid, "--force"], cwd=tmp, timeout=60)
        ok_cleanup = proc.returncode == 0
        out.append(result("workflow:git-worktree-fixture:cleanup-worktrees", "pass" if ok_cleanup else "fail", "cleaned" if ok_cleanup else (proc.stderr or proc.stdout)[:300]))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def check_async_workflow_fixture() -> list[dict[str, str]]:
    """Exercise durable async start/await/collect/status/cancel with local fixture executors."""
    cleanup_test_artifacts()
    out: list[dict[str, str]] = []
    executors_path = HARNESS / "routing" / "executors.json"
    original_executors = read_json(executors_path, {}) or {}
    agent_path = ROOT / "testing" / "fixtures" / "async-fixture-agent.py"
    slow_agent_path = ROOT / "testing" / "fixtures" / "async-cancel-fixture-agent.py"
    wfids: list[str] = []
    try:
        if not agent_path.exists() or not slow_agent_path.exists():
            return [result("workflow:async-fixture:fixture-files", "fail", "missing testing/fixtures async executors")]
        cfg = json.loads(json.dumps(original_executors))
        cfg.setdefault("executors", {})["fixture-async"] = {
            "type": "cli",
            "description": "Local fixture executor for async workflow validation.",
            "commandTemplate": ["{python}", "{root}/testing/fixtures/async-fixture-agent.py"],
            "runnable": True,
            "defaultSpawn": {"profile": "scan", "model": "fixture", "effort": "low", "agent": "fixture"},
            "runtimeLimits": {"maxConcurrency": 3, "backoffSeconds": 1, "stopOnRateLimit": True}
        }
        cfg.setdefault("executors", {})["fixture-async-slow"] = {
            "type": "cli",
            "description": "Local slow fixture executor for async cancellation validation.",
            "commandTemplate": ["{python}", "{root}/testing/fixtures/async-cancel-fixture-agent.py"],
            "runnable": True,
            "defaultSpawn": {"profile": "scan", "model": "fixture", "effort": "low", "agent": "fixture"},
            "runtimeLimits": {"maxConcurrency": 2, "backoffSeconds": 1, "stopOnRateLimit": True}
        }
        write_json(executors_path, cfg)

        plan_payload = call_harness_function(
            "plan_workflow",
            "async workflow fixture",
            None,
            profile_name="repository-review",
            split="explicit",
            shards=["specs/00-universal/agentic-async-await.md", "schemas", "docs/WORKFLOW_OPERATIONS_RUNBOOK.md"],
            max_workers=3,
            concurrency=3,
        )
        wfid = str(plan_payload.get("workflowId"))
        wfids.append(wfid)
        mark_fixture_workflow(wfid, "async-workflow")
        try:
            start_payload = call_harness_function("workflow_start", wfid, "fixture-async", 3, 5, mode="all-settled")
            start_ok = bool(start_payload.get("supervisorPid")) and int(start_payload.get("tasksStarted", 0)) == 3
        except Exception as exc:
            start_payload = {"error": str(exc)}
            start_ok = False
        out.append(result("workflow:async-fixture:start", "pass" if start_ok else "fail", "started" if start_ok else str(start_payload)[:300]))
        if not start_ok:
            return out
        try:
            await_payload = call_harness_function("workflow_await", wfid, mode="all-settled", timeout=20, poll_interval=0.2)
            counts = await_payload.get("counts", {}) if isinstance(await_payload.get("counts", {}), dict) else {}
        except Exception as exc:
            await_payload = {"error": str(exc)}
            counts = {}
        ok_await = await_payload.get("satisfied") is True and counts.get("fulfilled") == 1 and counts.get("rejected") == 1 and counts.get("timeout") == 1
        out.append(result("workflow:async-fixture:await", "pass" if ok_await else "fail", "fulfilled/rejected/timeout settled" if ok_await else str(await_payload)[-300:]))
        collect_payload = call_harness_function("workflow_async_collect", wfid, recover=True)
        collect_counts = collect_payload.get("counts", {}) if isinstance(collect_payload.get("counts", {}), dict) else {}
        ok_collect = collect_counts.get("fulfilled") == 1 and collect_counts.get("rejected") == 1 and collect_counts.get("timeout") == 1 and collect_payload.get("canReduce") is True
        out.append(result("workflow:async-fixture:collect", "pass" if ok_collect else "fail", "partial collection can reduce" if ok_collect else str(collect_payload)[-300:]))
        status_payload = call_harness_function("workflow_async_status", wfid, recover=True)
        ok_status = isinstance(status_payload.get("tasks"), list) and isinstance(status_payload.get("metrics"), dict) and "timeoutRate" in status_payload.get("metrics", {})
        out.append(result("workflow:async-fixture:status", "pass" if ok_status else "fail", "ok" if ok_status else str(status_payload)[-300:]))
        events_payload = call_harness_function("workflow_async_events", wfid, limit=20)
        events_text = json.dumps(events_payload, ensure_ascii=False)
        ok_events = "async_group" in events_text and "async_task_settled" in events_text
        out.append(result("workflow:async-fixture:watch", "pass" if ok_events else "fail", "events visible" if ok_events else events_text[-300:]))
        try:
            recover_payload = call_harness_function("workflow_async_recover", wfid)
            recover_ok = isinstance(recover_payload, dict)
        except Exception as exc:
            recover_payload = {"error": str(exc)}
            recover_ok = False
        out.append(result("workflow:async-fixture:recover", "pass" if recover_ok else "fail", "ok" if recover_ok else str(recover_payload)[-300:]))

        cancel_payload_plan = call_harness_function(
            "plan_workflow",
            "async cancellation fixture",
            None,
            profile_name="repository-review",
            split="explicit",
            shards=["README.md"],
            max_workers=1,
            concurrency=1,
        )
        cancel_wfid = str(cancel_payload_plan.get("workflowId"))
        wfids.append(cancel_wfid)
        mark_fixture_workflow(cancel_wfid, "async-cancel-workflow")
        cancel_base = HARNESS / "workflows" / "active" / cancel_wfid
        cancel_async = cancel_base / "async"
        cancel_tasks = cancel_async / "tasks"
        cancel_tasks.mkdir(parents=True, exist_ok=True)
        sleeper_kwargs: dict[str, Any] = {"cwd": ROOT, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            sleeper_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            sleeper_kwargs["start_new_session"] = True
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **sleeper_kwargs)
        try:
            group_id = f"AG-{cancel_wfid}-fixture"
            write_json(cancel_async / "async-group.json", {
                "schemaVersion": "1.0",
                "asyncGroupId": group_id,
                "workflowId": cancel_wfid,
                "mode": "all-settled",
                "status": "running",
                "tasks": ["AT-001"],
                "concurrency": 1,
                "queueMaxSize": 1,
                "timeoutSeconds": 60,
                "settlementPolicy": {"mode": "all-settled", "allowPartialReduce": True, "groupTimeoutSeconds": 60},
                "scheduler": {},
                "executor": "fixture-cancel",
                "createdAt": now(),
                "startedAt": now(),
                "settledAt": None,
                "supervisorPid": None,
                "counts": {"running": 1},
            })
            write_json(cancel_async / "cancellation.json", {"requested": False, "reason": None, "requestedAt": None, "graceSeconds": 10, "killAfterGrace": True})
            write_json(cancel_tasks / "AT-001.json", {
                "schemaVersion": "1.0",
                "asyncTaskId": "AT-001",
                "asyncGroupId": group_id,
                "workflowId": cancel_wfid,
                "workerId": "worker-001",
                "executor": "fixture-cancel",
                "command": [sys.executable, "-c", "import time; time.sleep(60)"],
                "pid": sleeper.pid,
                "status": "running",
                "promiseState": "pending",
                "createdAt": now(),
                "startedAt": now(),
                "settledAt": None,
                "timeoutSeconds": 60,
                "resultPath": "workers/worker-001.result.json",
                "stdoutPath": str((cancel_base / "run-logs" / "worker-001.stdout.log").relative_to(ROOT)),
                "stderrPath": str((cancel_base / "run-logs" / "worker-001.stderr.log").relative_to(ROOT)),
                "error": None,
            })
            cancelled_payload = call_harness_function(
                "workflow_cancel",
                cancel_wfid,
                reason="async fixture cancellation",
                grace_seconds=0,
            )
            try:
                sleeper.wait(timeout=3)
            except subprocess.TimeoutExpired:
                processes.signal_process_tree(sleeper.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                sleeper.wait(timeout=3)
            alive = call_harness_function("workflow_process_alive", sleeper.pid)
            cancel_counts = call_harness_function(
                "workflow_await",
                cancel_wfid,
                mode="all-settled",
                timeout=10,
                poll_interval=0.2,
            ).get("counts", {})
        finally:
            if sleeper.poll() is None:
                processes.signal_process_tree(sleeper.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                try:
                    sleeper.wait(timeout=3)
                except Exception:
                    pass
        out.append(result("workflow:async-fixture:cancel", "pass" if cancelled_payload.get("cancellation", {}).get("requested") is True and not alive else "fail", "running process signalled" if cancelled_payload.get("cancellation", {}).get("requested") is True and not alive else str(cancelled_payload)[:300]))
        ok_cancel_await = cancel_counts.get("cancelled", 0) >= 1
        out.append(result("workflow:async-fixture:cancel-await", "pass" if ok_cancel_await else "fail", "cancelled task settled" if ok_cancel_await else str(cancel_counts)[:300]))
        return out
    finally:
        write_json(executors_path, original_executors)
        for wfid in wfids:
            cleanup_fixture_workflow(HARNESS / "workflows" / "active" / wfid)
        cleanup_test_artifacts()

def check_handoff_budget_fixture() -> list[dict[str, str]]:
    """Verify minimal-first handoff generation stays within token budget for high-risk profiles."""
    out: list[dict[str, str]] = []
    paths = [
        HARNESS / "handoff" / "handoff.md",
        HARNESS / "handoff" / "handoff.json",
        HARNESS / "handoff" / "next-agent-prompt.md",
    ]
    # SPEC-108 H2: bytes round-trip — text mode rewrites newlines per platform.
    backups: dict[Path, bytes | None] = {}
    for path in paths:
        backups[path] = path.read_bytes() if path.exists() else None
    try:
        project = project_config()
        budgets = ((project.get("workflows") or {}).get("budgets") or {}) if isinstance(project.get("workflows"), dict) else {}
        handoff_budget = budgets.get("handoffBudget", {}) if isinstance(budgets.get("handoffBudget"), dict) else {}
        max_required_tokens = int(handoff_budget.get("maxRequiredReadTokens", 0) or 0)
        tasks = {
            "security": "Audit secrets, auth headers, privacy, API security, and supply chain risk.",
            "implementation": "Implement a bounded feature with tests and coverage checks.",
        }
        for label, task in tasks.items():
            try:
                call_harness_function("generate_handoff", task, None)
            except Exception as exc:
                out.append(result(f"workflow:handoff-budget:{label}", "fail", str(exc)[:300]))
                continue
            handoff = read_json(HARNESS / "handoff" / "handoff.json", {}) or {}
            ctx = handoff.get("contextBudget", {}) if isinstance(handoff.get("contextBudget"), dict) else {}
            required_tokens = int(ctx.get("estimatedRequiredReadTokens", 0) or 0)
            profile = str(handoff.get("taskProfile") or "unknown")
            ok = bool(max_required_tokens and required_tokens and required_tokens <= max_required_tokens)
            out.append(result(
                f"workflow:handoff-budget:{label}",
                "pass" if ok else "fail",
                f"profile={profile}; requiredTokens={required_tokens}; maxRequiredReadTokens={max_required_tokens}; demoted={len(ctx.get('budgetDemotedReads') or [])}",
            ))
        return out
    finally:
        for path, content in backups.items():
            if content is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

def check_workflow_token_budget_fixture() -> list[dict[str, str]]:
    """Plan each workflow profile at configured fan-out and verify token economics."""
    cleanup_test_artifacts()
    out: list[dict[str, str]] = []
    wfids: list[str] = []
    try:
        profiles_payload = read_json(HARNESS / "workflows" / "workflow-profiles.json", {}) or {}
        profiles = profiles_payload.get("profiles", {}) if isinstance(profiles_payload.get("profiles"), dict) else {}
        if not profiles:
            return [result("workflow:token-budget-fixture", "fail", "no profiles configured")]
        representative_shards = [
            "README.md",
            "docs/AGENTIC_WORKFLOWS.md",
            "docs/WORKFLOW_OPERATIONS_RUNBOOK.md",
            "docs/WORKFLOW_TOKEN_ECONOMICS.md",
            "schemas/workflow.schema.json",
            "schemas/worker-result.schema.json",
            "scripts/harness.py",
            "scripts/spec_test_gate.py",
            ".harness/workflows/workflow-profiles.json",
            ".harness/project.json",
        ]
        summaries: list[dict[str, Any]] = []
        for name, cfg in sorted(profiles.items()):
            try:
                configured_workers = max(1, int(cfg.get("maxWorkers", 3) or 3))
                kwargs: dict[str, Any] = {
                    "profile_name": name,
                    "max_workers": configured_workers,
                    "concurrency": 1,
                }
                # D010: research-* profiles (and any widthDeclarationRequired) refuse to plan
                # without an explicit fan-out width — mirror workflow_plan.py's own guard so the
                # fixture can probe their token economics instead of failing "No width written".
                if width_declaration_required(name, cfg):
                    kwargs["width_mode"] = "custom"
                    kwargs["width_why"] = f"token-budget fixture probe at profile maxWorkers={configured_workers}"
                if cfg.get("type") == "map-reduce":
                    kwargs.update({"split": "explicit", "shards": representative_shards[:configured_workers]})
                payload = call_harness_function(
                    "plan_workflow",
                    f"token budget fixture {name}",
                    None,
                    **kwargs,
                )
                wfid = str(payload.get("workflowId"))
                wfids.append(wfid)
                mark_fixture_workflow(wfid, "token-budget")
                workflow_path = HARNESS / "workflows" / "active" / wfid / "workflow.json"
                workflow = read_json(workflow_path, {}) or {}
                workers = workflow.get("workers", []) if isinstance(workflow.get("workers"), list) else []
                if workers:
                    write_sample_worker_result(wfid, workers[0])
                report = call_harness_function("workflow_token_audit", wfid, write_report=True, update_workflow=True)
                summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
                summaries.append(summary)
                checks = report.get("checks", []) if isinstance(report.get("checks", []), list) else []
                report_path = HARNESS / "workflows" / "active" / wfid / "reduce" / "token-audit.json"
                observed_ok = int(summary.get("totalObservedTokens", 0) or 0) >= int(summary.get("totalWorkerPromptTokens", 0) or 0)
                result_ok = not workers or int(summary.get("totalWorkerResultTokens", 0) or 0) > 0
                worker_count_ok = int(summary.get("workers", 0) or 0) == configured_workers
                ok = (
                    summary.get("status") in {"pass", "warn"}
                    and summary.get("totalWorkerPromptTokens", 0) > 0
                    and report_path.exists()
                    and all(c.get("status") != "fail" for c in checks)
                    and observed_ok
                    and result_ok
                    and worker_count_ok
                )
                detail = (
                    f"{wfid}; workers={summary.get('workers')}/{configured_workers}; status={summary.get('status')}; "
                    f"planned={summary.get('totalPlannedTokens')}; observed={summary.get('totalObservedTokens')}; "
                    f"resultTokens={summary.get('totalWorkerResultTokens')}"
                )
                out.append(result(f"workflow:token-budget-fixture:{name}", "pass" if ok else "fail", detail if ok else str(report)[:300]))
            except Exception as exc:
                out.append(result(f"workflow:token-budget-fixture:{name}", "fail", str(exc)[:300]))
        try:
            rollup = call_harness_function("token_audit_rollup_by_type", summaries)
            expected_types = {str(cfg.get("type")) for cfg in profiles.values() if cfg.get("type")}
            rollup_ok = expected_types.issubset(set(rollup.keys())) and all(int(v.get("workflowCount", 0) or 0) > 0 for v in rollup.values())
            out.append(result("workflow:token-budget-fixture:by-type-rollup", "pass" if rollup_ok else "fail", ", ".join(sorted(rollup.keys())) or "empty"))
        except Exception as exc:
            out.append(result("workflow:token-budget-fixture:by-type-rollup", "fail", str(exc)[:300]))
        return out
    finally:
        for wfid in wfids:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_workflow_soak_fixture() -> list[dict[str, str]]:
    """Run bounded repeated workflow planning/token-audit cycles as a release soak smoke test."""
    cleanup_test_artifacts()
    out: list[dict[str, str]] = []
    wfids: list[str] = []
    try:
        raw_cycles = int(os.environ.get("HARNESS_SOAK_CYCLES", "3"))
        cycles = max(1, min(raw_cycles, 10))
        profiles_payload = read_json(HARNESS / "workflows" / "workflow-profiles.json", {}) or {}
        profiles = profiles_payload.get("profiles", {}) if isinstance(profiles_payload.get("profiles"), dict) else {}
        by_type: dict[str, str] = {}
        for name, cfg in sorted(profiles.items()):
            typ = str(cfg.get("type") or "")
            if typ and typ not in by_type:
                by_type[typ] = name
        expected_types = {"map-reduce", "fork-join"}
        if not expected_types.issubset(set(by_type)):
            return [result("workflow:soak-fixture:profiles", "fail", "missing profile type coverage: " + ", ".join(sorted(expected_types - set(by_type))))]
        ids_seen: set[str] = set()
        type_counts: dict[str, int] = {typ: 0 for typ in expected_types}
        failures: list[str] = []
        for i in range(cycles):
            for typ in sorted(expected_types):
                profile_name = by_type[typ]
                kwargs: dict[str, Any] = {
                    "profile_name": profile_name,
                    "max_workers": 2,
                    "concurrency": 1,
                }
                if typ == "map-reduce":
                    kwargs.update({"split": "explicit", "shards": ["README.md", "docs/AGENTIC_WORKFLOWS.md"]})
                try:
                    payload = call_harness_function("plan_workflow", f"soak fixture cycle {i+1} {profile_name}", None, **kwargs)
                    wfid = str(payload.get("workflowId"))
                    wfids.append(wfid)
                    mark_fixture_workflow(wfid, "workflow-soak")
                    if not wfid or wfid in ids_seen:
                        failures.append(f"duplicate-or-empty workflowId: {wfid}")
                    ids_seen.add(wfid)
                    workflow = read_json(HARNESS / "workflows" / "active" / wfid / "workflow.json", {}) or {}
                    workers = workflow.get("workers", []) if isinstance(workflow.get("workers"), list) else []
                    if not workers:
                        failures.append(f"{wfid}: no workers")
                    if workers:
                        write_sample_worker_result(wfid, workers[0])
                    report = call_harness_function("workflow_token_audit", wfid, write_report=True, update_workflow=True)
                    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
                    checks = report.get("checks", []) if isinstance(report.get("checks", []), list) else []
                    if summary.get("status") not in {"pass", "warn"} or any(c.get("status") == "fail" for c in checks):
                        failures.append(f"{wfid}: token audit {summary.get('status')}")
                    type_counts[typ] += 1
                except Exception as exc:
                    failures.append(f"{typ}/{profile_name}: {str(exc)[:180]}")
        expected_workflows = cycles * len(expected_types)
        out.append(result("workflow:soak-fixture:cycles", "pass" if not failures and len(ids_seen) == expected_workflows else "fail", f"cycles={cycles}; workflows={len(ids_seen)}/{expected_workflows}; failures={len(failures)}" if not failures else "; ".join(failures[:5])))
        out.append(result("workflow:soak-fixture:type-coverage", "pass" if all(type_counts.get(t, 0) == cycles for t in expected_types) else "fail", ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))))
        return out
    finally:
        for wfid in wfids:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_state_store_fixture() -> list[dict[str, str]]:
    """Exercise FileStateStore and SQLiteStateStore contracts."""
    out: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from harness_lib.state_store import FileStateStore, SQLiteStateStore  # type: ignore
            tmp_path = Path(tmp)
            stores = [
                ("file", FileStateStore(tmp_path / "file-state")),
                ("sqlite", SQLiteStateStore(tmp_path / "state.sqlite3")),
            ]
            failures: list[str] = []
            for name, store in stores:
                store.put_json("workflows/WF-test", {"workflowId": "WF-test", "status": "planned"})
                got = store.get_json("workflows/WF-test")
                if not isinstance(got, dict) or got.get("status") != "planned":
                    failures.append(f"{name}: read-after-write failed")
                keys = store.list_keys("workflows")
                if "workflows/WF-test" not in keys:
                    failures.append(f"{name}: list_keys missing key")
                store.delete("workflows/WF-test")
                if store.get_json("workflows/WF-test") is not None:
                    failures.append(f"{name}: delete failed")
                try:
                    store.put_json("../escape", {"bad": True})
                    failures.append(f"{name}: traversal accepted")
                except ValueError:
                    pass
            out.append(result("state-store:file-sqlite-contract", "pass" if not failures else "fail", "file/sqlite ok" if not failures else "; ".join(failures[:5])))
        finally:
            try:
                sys.path.remove(str(ROOT / "scripts"))
            except ValueError:
                pass
    return out

def check_state_runtime_fixture() -> list[dict[str, str]]:
    """Verify workflow runtime mirrors state into StateStore and can recover from it."""
    cleanup_test_artifacts()
    wfid = ""
    try:
        payload = call_harness_function(
            "plan_workflow",
            "state runtime mirror fixture",
            None,
            profile_name="repository-review",
            max_workers=2,
            concurrency=1,
            split="explicit",
            shards=["README.md", "docs/STATE_STORE_CONTRACT.md"],
        )
        wfid = str(payload.get("workflowId"))
        mark_fixture_workflow(wfid, "state-runtime")
        summary = call_harness_function("workflow_state_summary", wfid)
        entries = summary.get("entries", []) if isinstance(summary.get("entries"), list) else []
        failures: list[str] = []
        keys = {str(item.get("key")) for item in entries if isinstance(item, dict)}
        if f"workflows/{wfid}/workflow" not in keys:
            failures.append("workflow mirror missing")
        if f"workflows/{wfid}/plan" not in keys:
            failures.append("plan mirror missing")
        report = call_harness_function("workflow_token_audit", wfid, write_report=True, update_workflow=True)
        summary2 = call_harness_function("workflow_state_summary", wfid)
        keys2 = {str(item.get("key")) for item in (summary2.get("entries", []) if isinstance(summary2.get("entries"), list) else []) if isinstance(item, dict)}
        if f"workflows/{wfid}/token-audit" not in keys2:
            failures.append("token-audit mirror missing")
        workflow_json = HARNESS / "workflows" / "active" / wfid / "workflow.json"
        if not workflow_json.exists():
            failures.append("workflow json not created")
        else:
            workflow_json.unlink()
            _base, recovered = call_harness_function("load_workflow", wfid)
            if recovered.get("workflowId") != wfid or not workflow_json.exists():
                failures.append("fallback recovery failed")
        proc = run_fixture_command([sys.executable, "scripts/harness.py", "workflow", "state", wfid], timeout=fixture_timeout())
        if proc.returncode != 0 or '"entries"' not in proc.stdout:
            failures.append("workflow state CLI failed")
        return [result("state-runtime:workflow-mirror-recovery", "pass" if not failures else "fail", f"entries={len(entries)}" if not failures else "; ".join(failures[:5]))]
    finally:
        if wfid:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        cleanup_test_artifacts()

def check_state_artifacts_fixture() -> list[dict[str, str]]:
    """Verify reducer and controlled-write JSON artifacts mirror/recover through StateStore."""
    cleanup_test_artifacts()
    failures: list[str] = []
    wfids: list[str] = []
    try:
        # Reducer/finalize artifacts.
        payload = call_harness_function(
            "plan_workflow",
            "state artifact reducer fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
        )
        wfid = str(payload.get("workflowId"))
        wfids.append(wfid)
        mark_fixture_workflow(wfid, "state-artifacts")
        workflow = read_json(HARNESS / "workflows" / "active" / wfid / "workflow.json", {}) or {}
        worker = (workflow.get("workers") or [{}])[0]
        write_sample_worker_result(wfid, worker)
        call_harness_function("workflow_mark", wfid, str(worker.get("workerId")), "done", force=True)
        call_harness_function("workflow_reduce", wfid, allow_partial=True)
        call_harness_function("workflow_finalize", wfid, update_context=False, regenerate_handoff=False, skip_review=True)
        state = call_harness_function("workflow_state_summary", wfid, mirror=True)
        names = {str(item.get("name")) for item in state.get("entries", []) if isinstance(item, dict)}
        for required in ["reduce-result", "harness-result", "validation", "token-audit"]:
            if required not in names:
                failures.append(f"{required} mirror missing")
        reduce_path = HARNESS / "workflows" / "active" / wfid / "reduce" / "reducer.result.json"
        harness_path = HARNESS / "workflows" / "active" / wfid / "reduce" / "harness-result.json"
        reduce_path.unlink(missing_ok=True)
        harness_path.unlink(missing_ok=True)
        recovered = call_harness_function("workflow_state_recover_artifacts", wfid, names=["reduce-result", "harness-result"])
        if not reduce_path.exists() or not harness_path.exists() or len(recovered.get("recovered", [])) < 2:
            failures.append("reduce/harness artifact recovery failed")

        # Controlled-write artifacts.
        cw_payload = call_harness_function(
            "plan_workflow",
            "state artifact controlled write fixture",
            None,
            profile_name="repository-review",
            max_workers=1,
            concurrency=1,
            split="explicit",
            shards=["README.md"],
            write_allowed=True,
            parallel_writes=True,
            isolation_mode="temp-copy",
            approval_token="I_APPROVE_CONTROLLED_WRITES",
        )
        cwfid = str(cw_payload.get("workflowId"))
        wfids.append(cwfid)
        mark_fixture_workflow(cwfid, "state-artifacts")
        cw_workflow = read_json(HARNESS / "workflows" / "active" / cwfid / "workflow.json", {}) or {}
        cw_worker = (cw_workflow.get("workers") or [{}])[0]
        cw_worker_id = str(cw_worker.get("workerId", "worker-001"))
        prep = call_harness_function(
            "workflow_prepare_write",
            cwfid,
            approval_token="I_APPROVE_CONTROLLED_WRITES",
            create_locks=[f"{cw_worker_id}:tmp-state-artifact-created.txt"],
        )
        cw_workflow = read_json(HARNESS / "workflows" / "active" / cwfid / "workflow.json", {}) or {}
        cw_worker = (cw_workflow.get("workers") or [{}])[0]
        workspace = ROOT / str(cw_worker.get("workspacePath"))
        (workspace / "tmp-state-artifact-created.txt").write_text("fixture\n", encoding="utf-8")
        call_harness_function("workflow_merge_plan", cwfid)
        state2 = call_harness_function("workflow_state_summary", cwfid, mirror=True)
        names2 = {str(item.get("name")) for item in state2.get("entries", []) if isinstance(item, dict)}
        for required in ["locks", "merge-plan"]:
            if required not in names2:
                failures.append(f"{required} mirror missing")
        merge_path = HARNESS / "workflows" / "active" / cwfid / "reduce" / "merge-plan.json"
        locks_path = HARNESS / "workflows" / "active" / cwfid / "locks.json"
        merge_path.unlink(missing_ok=True)
        locks_path.unlink(missing_ok=True)
        rec2 = call_harness_function("workflow_state_recover_artifacts", cwfid, names=["merge-plan", "locks"])
        if not merge_path.exists() or not locks_path.exists() or len(rec2.get("recovered", [])) < 2:
            failures.append("controlled-write artifact recovery failed")
        status = "pass" if not failures else "fail"
        detail = "reduce/finalize/controlled-write artifacts mirrored and recovered" if not failures else "; ".join(failures[:5])
        return [result("state-runtime:artifact-mirror-recovery", status, detail)]
    finally:
        for wfid in wfids:
            shutil.rmtree(HARNESS / "workflows" / "active" / wfid, ignore_errors=True)
        try:
            (ROOT / "tmp-state-artifact-created.txt").unlink(missing_ok=True)
        except Exception:
            pass
        cleanup_test_artifacts()

def check_workflow_id_boundary_fixture() -> list[dict[str, str]]:
    """Verify workflow id helper boundary without touching runtime workflow state."""
    from harness_lib import workflow_ids  # type: ignore

    collisions = set()
    first = workflow_ids.allocate_workflow_id(lambda candidate: candidate in collisions)
    collisions.add(first)
    second = workflow_ids.allocate_workflow_id(lambda candidate: candidate in collisions)
    async_first = workflow_ids.allocate_async_group_id({})
    async_collision = workflow_ids.allocate_async_group_id({"asyncGroupId": async_first})
    safe_path = workflow_ids.safe_workflow_path(Path("/tmp/harness-active"), "WF:unsafe/path")
    failures: list[str] = []
    if not first.startswith("WF-") or first == second:
        failures.append("workflow id allocation/collision failed")
    if not async_first.startswith("AG-") or async_first == async_collision:
        failures.append("async group id allocation/collision failed")
    if safe_path.name != "WF-unsafe-path":
        failures.append(f"safe path normalization failed: {safe_path}")
    return [result("workflow-id-boundary:allocation", "pass" if not failures else "fail", "workflow/async ids and safe paths allocated" if not failures else "; ".join(failures))]
