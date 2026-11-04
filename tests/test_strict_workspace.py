from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "compute_plane.workspace",
    reason="pinned private Kernel package is unavailable on this runner",
)

from compute_plane.workspace import cleanup_linked_worktree
from relay.strict_workspace import (
    StrictWorkspaceRejected,
    provision_train_workspace,
    read_workspace_ready,
    reconcile_workspace_intent,
)
from relay.train_execution import canonical_json_bytes, parse_execution_descriptor


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "file.txt").write_text("base\n", encoding="utf-8")
    _git("add", "file.txt", cwd=repository)
    _git("commit", "-m", "base", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _descriptor(base_commit: str):
    return parse_execution_descriptor(canonical_json_bytes({
        "schema": "tare.tools/train-execution/1",
        "train_id": "TRAIN-STRICT",
        "work_id": "work-one",
        "work_graph": "work-graph.json",
        "execution_repository": "repo-one",
        "base_commit": base_commit,
        "target_ref": "refs/heads/main",
        "library_manifest": "LIBRARY_MANIFEST.json",
        "tracked_paths": ["file.txt"],
        "verification_checks": [
            {"criterion": "passes", "command": ["git", "diff", "--exit-code"]}
        ],
    }))


def test_intent_precedes_ready_and_reconciles_exact_workspace(tmp_path: Path):
    repository, head = _repository(tmp_path)
    descriptor = _descriptor(head)
    train = tmp_path / "train"
    train.mkdir()
    (train / "STATE.json").write_text(json.dumps({
        "train_id": descriptor.train_id,
        "execution_descriptor": {"sha256": descriptor.sha256},
    }), encoding="utf-8")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    artifacts = provision_train_workspace(
        train,
        descriptor,
        repository_root=repository,
        workspace_parent=workspaces,
        id_factory=lambda: "wt-12345678",
    )
    assert artifacts.intent_file.exists()
    assert artifacts.ready_file.exists()
    assert read_workspace_ready(artifacts.ready_file)["head"] == head
    assert reconcile_workspace_intent(
        artifacts.intent_file, repository_root=repository
    ) == artifacts.lease
    cleanup_linked_worktree(artifacts.lease)


def test_missing_descriptor_seal_blocks_before_intent_or_worktree(tmp_path: Path):
    repository, head = _repository(tmp_path)
    descriptor = _descriptor(head)
    train = tmp_path / "train"
    train.mkdir()
    (train / "STATE.json").write_text(json.dumps({
        "train_id": descriptor.train_id,
    }), encoding="utf-8")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    with pytest.raises(StrictWorkspaceRejected, match="SEAL_MISMATCH"):
        provision_train_workspace(
            train,
            descriptor,
            repository_root=repository,
            workspace_parent=workspaces,
            id_factory=lambda: "wt-12345678",
        )
    assert not list(train.glob("WORKSPACE_*.json"))
    assert not list(workspaces.iterdir())
