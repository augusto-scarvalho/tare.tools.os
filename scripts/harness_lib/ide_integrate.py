"""IDE shard integration: snapshot, protected scan, apply, rollback, and commit."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in (None, "") and str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib import ide_shard, processes, sandbox_spawn  # noqa: E402
from harness_lib.common import truncate_text  # noqa: E402
from harness_lib.gate_parallel import _run_git  # noqa: E402
from harness_lib.workflow_writes import protected_workspace_mutations  # noqa: E402

SNAPSHOT_MESSAGE = "ide-shard integrate snapshot"
REVIEW_DIFF_CAP = 50_000
_STATUS = {"A", "M", "D", "R"}


class IdeIntegrateRefused(RuntimeError):
    """An integration step cannot safely proceed."""


def _manifest(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    manifest = ide_shard._valid(root)
    if manifest is None:
        raise IdeIntegrateRefused("IDE shard integration refused: no active shard")
    return ide_shard._paths(root)[1], manifest


def _reason(proc: Any, fallback: str) -> str:
    return (proc.stderr or proc.stdout or fallback).strip()


def _head(root: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=root, timeout=60).stdout.strip()


def _fresh(root: Path, manifest: dict[str, Any]) -> str:
    head = _head(root)
    base = str(manifest["baseSha"])
    if base != head:
        raise IdeIntegrateRefused(
            f"stale-shard: shard baseSha {base} != main HEAD {head}")
    return head


def _snapshot(shard: Path) -> str:
    add = _run_git(["add", "-A"], cwd=shard, timeout=60, check=False)
    if add.returncode != 0:
        raise IdeIntegrateRefused(_reason(add, "ide-shard snapshot add failed"))
    changed = _run_git(["diff", "--cached", "--quiet"], cwd=shard, timeout=60,
                       check=False).returncode != 0
    if changed:
        commit = _run_git(["commit", "-m", SNAPSHOT_MESSAGE], cwd=shard, timeout=120,
                          check=False)
        if commit.returncode != 0:
            raise IdeIntegrateRefused(_reason(commit, "ide-shard snapshot commit failed"))
    return _run_git(["rev-parse", "HEAD"], cwd=shard, timeout=60).stdout.strip()


def _name_status(shard: Path, base: str) -> list[dict[str, Any]]:
    proc = _run_git(["diff", "--name-status", "-z", "-M", f"{base}..{ide_shard.BRANCH}"],
                    cwd=shard, timeout=60)
    parts = (proc.stdout or "").split("\0")
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        i += 1
        kind = status[:1]
        if kind not in _STATUS:
            raise IdeIntegrateRefused(f"unsupported shard diff status: {status}")
        if kind == "R":
            if i + 1 >= len(parts):
                raise IdeIntegrateRefused("malformed shard rename diff")
            old, new = parts[i], parts[i + 1]
            i += 2
            rows.append({"status": "R", "path": new.replace("\\", "/"),
                         "oldPath": old.replace("\\", "/")})
        else:
            if i >= len(parts):
                raise IdeIntegrateRefused("malformed shard name-status diff")
            rows.append({"status": kind, "path": parts[i].replace("\\", "/")})
            i += 1
    return rows


def _numstat(shard: Path, base: str) -> tuple[dict[str, tuple[int | None, int | None]],
                                               int, int, int]:
    proc = _run_git(["diff", "--numstat", "-z", "-M", f"{base}..{ide_shard.BRANCH}"],
                    cwd=shard, timeout=60)
    parts = (proc.stdout or "").split("\0")
    by_path: dict[str, tuple[int | None, int | None]] = {}
    additions = deletions = binaries = 0
    i = 0
    while i < len(parts) and parts[i]:
        fields = parts[i].split("\t", 2)
        i += 1
        if len(fields) != 3:
            raise IdeIntegrateRefused("malformed shard numstat diff")
        added = None if fields[0] == "-" else int(fields[0])
        deleted = None if fields[1] == "-" else int(fields[1])
        path = fields[2]
        if not path:
            if i + 1 >= len(parts):
                raise IdeIntegrateRefused("malformed shard rename numstat")
            _old, path = parts[i], parts[i + 1]
            i += 2
        path = path.replace("\\", "/")
        by_path[path] = (added, deleted)
        if added is None or deleted is None:
            binaries += 1
        else:
            additions += added
            deletions += deleted
    return by_path, additions, deletions, binaries


def _applied_paths(rows: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for row in rows:
        if row["status"] == "R":
            paths.extend((row["oldPath"], row["path"]))
        else:
            paths.append(row["path"])
    return list(dict.fromkeys(paths))


def _safe_applied(applied: Any) -> list[str]:
    if not isinstance(applied, list) or not applied:
        raise IdeIntegrateRefused("applied paths must be a non-empty list")
    out: list[str] = []
    for value in applied:
        path = str(value)
        pure = PurePosixPath(path)
        if (not path or "\\" in path or pure.is_absolute() or ".." in pure.parts
                or path != pure.as_posix()):
            raise IdeIntegrateRefused(f"invalid applied path: {path!r}")
        out.append(path)
    if len(out) != len(set(out)):
        raise IdeIntegrateRefused("applied paths must not contain duplicates")
    return out


def summary(root: Path) -> dict[str, Any]:
    """Snapshot shard edits and return the stable integration summary."""
    root = Path(root).resolve()
    shard_status = ide_shard.status(root)
    if not shard_status["exists"]:
        return {
            "ok": True, "exists": False, "branch": ide_shard.BRANCH,
            "baseSha": None, "headSha": None, "branchSha": None, "aheadCount": 0,
            "stale": False, "files": [],
            "counts": {"files": 0, "additions": 0, "deletions": 0, "binaryFiles": 0},
            "writers": shard_status["writers"], "reviewRecommended": False,
        }
    shard, manifest = _manifest(root)
    head = _head(root)
    branch_sha = _snapshot(shard)
    base = str(manifest["baseSha"])
    rows = _name_status(shard, base)
    stats, additions, deletions, binaries = _numstat(shard, base)
    for row in rows:
        row["additions"], row["deletions"] = stats.get(row["path"], (0, 0))
    writers = ide_shard.status(root)["writers"]
    ahead = int(_run_git(["rev-list", "--count", f"{base}..{ide_shard.BRANCH}"],
                         cwd=shard, timeout=60).stdout.strip() or "0")
    return {
        "ok": True, "exists": True, "branch": ide_shard.BRANCH,
        "baseSha": base, "headSha": head, "branchSha": branch_sha,
        "aheadCount": ahead, "stale": base != head, "files": rows,
        "counts": {"files": len(rows), "additions": additions,
                   "deletions": deletions, "binaryFiles": binaries},
        "writers": writers, "reviewRecommended": bool(writers.get("agent")),
    }


def apply(root: Path) -> dict[str, Any]:
    """Apply the snapshotted shard diff to the main worktree and index."""
    root = Path(root).resolve()
    shard, manifest = _manifest(root)
    _fresh(root, manifest)
    protected = protected_workspace_mutations(
        shard, sandbox_spawn.protected_rel_paths(root), root)
    if protected:
        raise IdeIntegrateRefused(
            "protected-path-modified (rule 11): " + ", ".join(protected))
    _snapshot(shard)
    _fresh(root, manifest)
    rows = _name_status(shard, str(manifest["baseSha"]))
    applied = _applied_paths(rows)
    restore = [row["path"] for row in rows if row["status"] in {"A", "M"}]
    deletes = [row["path"] for row in rows if row["status"] == "D"]
    for row in rows:
        if row["status"] == "R":
            restore.append(row["path"])
            deletes.append(row["oldPath"])
    try:
        if restore:
            proc = _run_git(
                ["restore", f"--source={ide_shard.BRANCH}", "--worktree", "--staged",
                 "--", *restore], cwd=root, timeout=120, check=False)
            if proc.returncode != 0:
                raise IdeIntegrateRefused(_reason(proc, "shard restore failed"))
        for path in deletes:
            proc = _run_git(["rm", "--quiet", "--", path], cwd=root, timeout=60,
                            check=False)
            if proc.returncode != 0:
                raise IdeIntegrateRefused(_reason(proc, f"shard delete failed: {path}"))
    except Exception:
        if applied:
            rollback(root, applied)
        raise
    return {"ok": True, "applied": applied, "files": rows}


def rollback(root: Path, applied: Any) -> dict[str, Any]:
    """Restore only the applied paths to main HEAD; leave every bystander untouched."""
    root = Path(root).resolve()
    paths = _safe_applied(applied)
    tracked: list[str] = []
    added: list[str] = []
    for path in paths:
        exists = _run_git(["cat-file", "-e", f"HEAD:{path}"], cwd=root, timeout=30,
                          check=False).returncode == 0
        (tracked if exists else added).append(path)
    if tracked:
        proc = _run_git(["restore", "--source=HEAD", "--worktree", "--staged", "--",
                         *tracked], cwd=root, timeout=120, check=False)
        if proc.returncode != 0:
            raise IdeIntegrateRefused(_reason(proc, "integration rollback failed"))
    for path in added:
        proc = _run_git(["rm", "--cached", "--quiet", "--ignore-unmatch", "--", path],
                        cwd=root, timeout=60, check=False)
        if proc.returncode != 0:
            raise IdeIntegrateRefused(_reason(proc, f"rollback unstage failed: {path}"))
        target = root / Path(*PurePosixPath(path).parts)
        if os.path.lexists(target):
            target.unlink()
    return {"ok": True, "restored": paths, "shardUntouched": True}


def _first_json(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def commit(root: Path, applied: Any, message: Any) -> dict[str, Any]:
    """Commit exactly the applied index after the existing gate/reckon pre-check."""
    root = Path(root).resolve()
    paths = _safe_applied(applied)
    # Expand a detected rename back to its A+D path set, matching fact 2's
    # applied set independently of the operator's diff.renames configuration.
    staged = _run_git(["diff", "--cached", "--name-only", "--no-renames"],
                      cwd=root, timeout=60).stdout
    staged_paths = [line.replace("\\", "/") for line in staged.splitlines() if line]
    if set(staged_paths) != set(paths):
        raise IdeIntegrateRefused(
            "integrate commit refused: staged paths drifted; "
            f"applied={sorted(paths)}, staged={sorted(staged_paths)}")
    note = str(message or "").strip()
    if not note:
        raise IdeIntegrateRefused("integrate commit refused: commit message is required")
    verify = processes.run_quiet(
        [sys.executable, str(root / "scripts" / "harness.py"), "verify-status", "--json"],
        cwd=str(root), timeout=120, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    verify_out = ((verify.stdout or "") + (verify.stderr or "")).strip()
    verdict = _first_json(verify.stdout or "")
    if verify.returncode != 0 or not verdict or verdict.get("readyToCommit") is not True:

        raise IdeIntegrateRefused(verify_out or "verify-status refused the integrate commit")
    from harness_lib import commit_authority
    res = commit_authority.execute_commit(note, root=root)
    if res.get("status") in ("refused", "failed"):
        raise IdeIntegrateRefused(f"git commit refused: {res.get('reasons') or res.get('output')}")
    sha = str(res.get("commitSha") or _head(root))
    ide_shard.dispose(root)
    return {"ok": True, "committed": sha, "disposed": True}




def review_pack(root: Path) -> dict[str, Any]:
    """Return a bounded staged diff plus the existing review --json advisory rows."""
    root = Path(root).resolve()
    diff = _run_git(["diff", "--cached", "--no-ext-diff", "--binary"], cwd=root,
                    timeout=120).stdout
    diff, truncated = truncate_text(diff, REVIEW_DIFF_CAP)
    proc = processes.run_quiet(
        [sys.executable, str(root / "scripts" / "harness.py"), "review", "--json"],
        cwd=str(root), timeout=120, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    advisory = _first_json(proc.stdout or "")
    if proc.returncode != 0 or not advisory or not isinstance(advisory.get("rows"), list):
        raise IdeIntegrateRefused(
            ((proc.stdout or "") + (proc.stderr or "")).strip()
            or "harness.py review --json returned no advisory rows")
    return {
        "diff": diff, "diffTruncated": truncated,
        "advisoryRows": advisory["rows"], "advisoryVerdict": advisory["verdict"],
    }


def _self_check() -> None:
    assert _safe_applied(["a.py", "dir/b.py"]) == ["a.py", "dir/b.py"]
    for bad in (["../a"], ["/a"], ["a\\b"], ["a", "a"], []):
        try:
            _safe_applied(bad)
        except IdeIntegrateRefused:
            pass
        else:
            raise AssertionError(bad)
    assert _first_json('noise\n{"readyToCommit": true}\n-- ok') == {
        "readyToCommit": True}


if __name__ == "__main__":
    _self_check()
    print("ide_integrate self-check: ok")
