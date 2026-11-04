"""Persistent GUI editing shard backed by one Git worktree."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from harness_lib import sandbox_spawn
from harness_lib.common import now, read_json, rmtree_robust, write_json
from harness_lib.gate_parallel import _run_git

ADMIN_REL = Path(".harness/runs/ide-shard")
BRANCH = "harness/ide-shard"


class IdeShardRefused(RuntimeError):
    """A shard mutation cannot safely proceed."""


def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    admin = root / ADMIN_REL
    return admin, admin / "worktree", admin / "shard.json", admin / "writers.jsonl"


def _registered(root: Path, worktree: Path) -> bool:
    try:
        proc = _run_git(["worktree", "list", "--porcelain"], cwd=root, timeout=30,
                        check=False)
        wanted = worktree.resolve()
        return proc.returncode == 0 and any(
            line.startswith("worktree ")
            and Path(line.removeprefix("worktree ")).resolve() == wanted
            for line in proc.stdout.splitlines())
    except Exception:
        return False


def _valid(root: Path) -> dict[str, Any] | None:
    _admin, worktree, manifest_path, _writers = _paths(root)
    manifest = read_json(manifest_path, None)
    if (not isinstance(manifest, dict) or not worktree.is_dir()
            or manifest.get("branch") != BRANCH or not manifest.get("baseSha")
            or not _registered(root, worktree)):
        return None
    return manifest


def _require_repo(root: Path) -> None:
    try:
        proc = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root, timeout=30,
                        check=False)
    except Exception as exc:
        raise IdeShardRefused(f"IDE shard requires a Git repository: {exc}") from exc
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        reason = (proc.stderr or proc.stdout or "not a Git worktree").strip()
        raise IdeShardRefused(f"IDE shard requires a Git repository: {reason}")


def ensure(root: Path) -> Path:
    """Return the persistent shard, creating it at the current HEAD when absent."""
    root = Path(root).resolve()
    if _valid(root) is not None:
        return _paths(root)[1]
    _require_repo(root)
    admin, worktree, manifest_path, _writers = _paths(root)
    if admin.exists():
        dispose(root)
    admin.mkdir(parents=True, exist_ok=True)
    proc = _run_git(["worktree", "add", "-b", BRANCH, str(worktree), "HEAD"],
                    cwd=root, timeout=30, check=False)
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "git worktree add failed").strip()
        dispose(root)
        raise IdeShardRefused(f"IDE shard creation refused: {reason}")
    try:
        base_sha = _run_git(["rev-parse", "HEAD"], cwd=root, timeout=60).stdout.strip()
        backend = sandbox_spawn.fs_backend()
        if os.name == "nt":
            if backend != "nt-icacls":
                raise RuntimeError("mandatory nt filesystem confinement is unavailable")
            applied = sandbox_spawn.fs_confine_nt(
                worktree, sandbox_spawn.protected_rel_paths(worktree))
        else:
            # ponytail: GUI-only writes may rely on resolve_confined off nt; fail closed
            # here when non-GUI shard writers land.
            applied = []
        write_json(manifest_path, {
            "baseSha": base_sha,
            "branch": BRANCH,
            "createdAt": now(),
            "fsBackend": backend,
            "fsApplied": applied,
        })
        return worktree
    except Exception as exc:
        dispose(root)
        raise IdeShardRefused(f"IDE shard creation refused: {exc}") from exc


def active_root(root: Path) -> Path:
    """Read from the shard after its first mutation, otherwise from the main tree."""
    root = Path(root).resolve()
    return _paths(root)[1] if _valid(root) is not None else root


def _writer_counts(path: Path) -> dict[str, int]:
    counts = {"gui": 0, "agent": 0}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                writer = json.loads(line).get("writer")
            except (AttributeError, json.JSONDecodeError):
                continue
            if writer in counts:
                counts[writer] += 1
    except OSError:
        pass
    return counts


def status(root: Path) -> dict[str, Any]:
    """Return the stable Lane-B shard status contract."""
    root = Path(root).resolve()
    manifest = _valid(root)
    _admin, worktree, _manifest, writers_path = _paths(root)
    try:
        main_sha = _run_git(["rev-parse", "HEAD"], cwd=root, timeout=30).stdout.strip()
    except Exception:
        main_sha = None
    if manifest is None:
        return {"ok": True, "exists": False, "baseSha": None, "mainSha": main_sha,
                "branch": None,
                "dirtyCount": 0, "writers": _writer_counts(writers_path)}
    try:
        porcelain = _run_git(["status", "--porcelain"], cwd=worktree, timeout=30).stdout
        dirty_count = len(porcelain.splitlines())
    except Exception:
        dirty_count = 0
    return {"ok": True, "exists": True, "baseSha": manifest["baseSha"],
            "mainSha": main_sha,
            "branch": manifest["branch"], "dirtyCount": dirty_count,
            "writers": _writer_counts(writers_path)}


def record_write(root: Path, action: str, rel: str, *, writer: str,
                 new_path: str | None = None) -> None:
    """Append one durable attribution row after a successful mutation."""
    _admin, _worktree, _manifest, writers_path = _paths(Path(root).resolve())
    row = {"at": now(), "action": action, "path": rel, "writer": writer}
    if new_path is not None:
        row["newPath"] = new_path
    writers_path.parent.mkdir(parents=True, exist_ok=True)
    with writers_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def dispose(root: Path) -> None:
    """Release confinement, remove the worktree/branch, and delete sidecars."""
    root = Path(root).resolve()
    admin, worktree, _manifest, _writers = _paths(root)
    sandbox_spawn.fs_release(worktree)
    for args in (["worktree", "remove", "--force", str(worktree)],
                 ["worktree", "prune"],
                 ["branch", "-D", BRANCH]):
        try:
            _run_git(args, cwd=root, timeout=60, check=False)
        except Exception:
            pass
    try:
        rmtree_robust(admin)
    except OSError:
        shutil.rmtree(admin, ignore_errors=True)


def prune(root: Path) -> None:
    """Keep a valid persistent shard; dispose broken remnants and prune Git metadata."""
    root = Path(root).resolve()
    admin = _paths(root)[0]
    if admin.exists() and _valid(root) is None:
        dispose(root)
    try:
        _run_git(["worktree", "prune"], cwd=root, timeout=60, check=False)
    except Exception:
        pass
