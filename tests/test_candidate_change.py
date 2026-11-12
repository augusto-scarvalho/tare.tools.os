from __future__ import annotations

import subprocess
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip(
    "compute_plane.workspace",
    reason="pinned private Kernel package is unavailable on this runner",
)

from compute_plane.candidate_git import CandidateGitRejected
from compute_plane.workspace import cleanup_linked_worktree, provision_linked_worktree
from relay.candidate_change import (
    CANDIDATE_FILE,
    CandidateChangeRejected,
    land_recorded_candidate,
    materialize_verify_and_record_candidate,
    read_candidate_receipt,
    verify_candidate_receipt,
)
from relay.train_execution import canonical_json_bytes, parse_execution_descriptor


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _subject(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "src").mkdir()
    (repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=repository)
    _git("commit", "-m", "base", cwd=repository)
    _git("branch", "-M", "main", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    descriptor = parse_execution_descriptor(canonical_json_bytes({
        "schema": "tare.tools/train-execution/1",
        "train_id": "TRAIN-CANDIDATE",
        "work_id": "work-one",
        "work_graph": "work-graph.json",
        "execution_repository": "repo-one",
        "base_commit": base,
        "target_ref": "refs/heads/main",
        "library_manifest": "LIBRARY_MANIFEST.json",
        "tracked_paths": ["src"],
        "verification_checks": [{
            "criterion": "content-is-valid",
            "command": [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('src/value.txt').read_text() == 'changed\\n'",
            ],
        }],
    }))
    lease = provision_linked_worktree(
        repository,
        tmp_path / "worktree",
        base_commit=base,
        worktree_id="worktree-receipt1",
    )
    return repository, descriptor, lease


def _write_ready(train: Path, descriptor, lease) -> None:
    (train / f"WORKSPACE_READY.{lease.worktree_id}.json").write_bytes(
        canonical_json_bytes({
            "schema": "tare.tools/workspace-ready/1",
            "train_id": descriptor.train_id,
            "descriptor_sha256": descriptor.sha256,
            "worktree_id": lease.worktree_id,
            "repository": descriptor.execution_repository,
            "base_commit": descriptor.base_commit,
            "destination": str(lease.root),
            "repository_root": str(lease.repository_root),
            "git_common_dir": str(lease.git_common_dir),
            "git_admin_dir": str(lease.git_admin_dir),
            "head": lease.base_commit,
        })
    )


def test_receipt_binds_verified_commit_and_landing_uses_same_object(tmp_path: Path):
    repository, descriptor, lease = _subject(tmp_path)
    train = tmp_path / "train"
    train.mkdir()
    _write_ready(train, descriptor, lease)
    (lease.root / "src" / "value.txt").write_text("changed\n", encoding="utf-8")

    receipt = materialize_verify_and_record_candidate(train, descriptor, lease)
    persisted = read_candidate_receipt(train / CANDIDATE_FILE)
    assert persisted == receipt
    observation = json.loads((train / "EXECUTOR_OBSERVATION.json").read_bytes())
    assert observation["candidate_sha256"] == receipt.sha256
    assert observation["descriptor_sha256"] == descriptor.sha256
    assert observation["status"] == "NEEDS_REVALIDATION"
    assert observation["observed_checks_passed"] == 1
    assert observation["attestation"]["status"] == "NOT_VERIFIED"
    from relay.executor_observation import verification_observed
    assert verification_observed(train, descriptor, receipt)
    raw_observation = (train / "EXECUTOR_OBSERVATION.json").read_bytes()
    observation["candidate_sha256"] = "f" * 64
    (train / "EXECUTOR_OBSERVATION.json").write_bytes(canonical_json_bytes(observation))
    assert not verification_observed(train, descriptor, receipt)
    (train / "EXECUTOR_OBSERVATION.json").write_bytes(raw_observation)
    verify_candidate_receipt(receipt, descriptor, repository_root=repository)
    assert land_recorded_candidate(
        receipt, descriptor, repository_root=repository
    ) == "LANDED"
    assert _git("rev-parse", "refs/heads/main", cwd=repository) == receipt.payload[
        "candidate_commit"
    ]
    assert land_recorded_candidate(
        receipt, descriptor, repository_root=repository
    ) == "ALREADY_LANDED"
    cleanup_linked_worktree(
        replace(lease, pinned_commit=receipt.payload["candidate_commit"])
    )


def test_writer_after_verification_blocks_receipt_can11(tmp_path: Path):
    _repository, descriptor, lease = _subject(tmp_path)
    train = tmp_path / "train"
    train.mkdir()
    _write_ready(train, descriptor, lease)
    (lease.root / "src" / "value.txt").write_text("changed\n", encoding="utf-8")

    def drifting_runner(argv, **kwargs):
        (Path(kwargs["cwd"]) / "src" / "value.txt").write_text(
            "drifted\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with pytest.raises(CandidateChangeRejected, match="DRIFT_AFTER_VERIFICATION"):
        materialize_verify_and_record_candidate(
            train,
            descriptor,
            lease,
            verification_runner=drifting_runner,
        )
    assert not (train / CANDIDATE_FILE).exists()


def test_failed_verifier_never_emits_receipt(tmp_path: Path):
    _repository, descriptor, lease = _subject(tmp_path)
    train = tmp_path / "train"
    train.mkdir()
    _write_ready(train, descriptor, lease)
    (lease.root / "src" / "value.txt").write_text("wrong\n", encoding="utf-8")

    with pytest.raises(CandidateChangeRejected, match="VERIFICATION_FAILED"):
        materialize_verify_and_record_candidate(train, descriptor, lease)
    assert not (train / CANDIDATE_FILE).exists()


def _recorded(tmp_path: Path):
    repository, descriptor, lease = _subject(tmp_path)
    train = tmp_path / "train"
    train.mkdir()
    _write_ready(train, descriptor, lease)
    (lease.root / "src" / "value.txt").write_text("changed\n", encoding="utf-8")
    return repository, descriptor, materialize_verify_and_record_candidate(train, descriptor, lease)


@pytest.mark.parametrize("divergence", [
    "base_commit", "descriptor_sha256", "work_id", "candidate_tree", "diff_sha256",
    "changed_paths", "changed_entries", "other_commit", "other_descriptor", "moved_base",
])
def test_divergent_receipt_is_refused_by_verify_and_landing(tmp_path: Path, divergence: str):
    repository, descriptor, receipt = _recorded(tmp_path)
    payload = dict(receipt.payload)
    checked = descriptor
    if divergence in {"candidate_tree", "diff_sha256", "descriptor_sha256", "base_commit", "work_id"}:
        payload[divergence] = "f" * 40
    elif divergence == "changed_paths":
        payload[divergence] = ["src", "extra"]
    elif divergence == "changed_entries":
        payload[divergence] = payload[divergence] + [
            {"path": "src/extra", "old_mode": "000000", "new_mode": "100644", "status": "A"}]
    elif divergence == "other_commit":
        _git("switch", "-q", "-c", "other", cwd=repository)
        (repository / "src" / "value.txt").write_text("other\n", encoding="utf-8")
        _git("commit", "-qam", "other", cwd=repository)
        payload["candidate_commit"] = _git("rev-parse", "HEAD", cwd=repository)
        _git("switch", "-q", "main", cwd=repository)
    else:
        # The receipt is intact; the Work's descriptor (criteria or base) is what changed.
        raw = json.loads(descriptor.canonical_bytes)
        if divergence == "other_descriptor":
            raw["verification_checks"][0]["criterion"] = "another-criterion"
        else:
            (repository / "next.txt").write_text("next\n", encoding="utf-8")
            _git("add", ".", cwd=repository)
            _git("commit", "-qm", "target moved", cwd=repository)
            raw["base_commit"] = _git("rev-parse", "HEAD", cwd=repository)
        checked = parse_execution_descriptor(canonical_json_bytes(raw))
    divergent = replace(receipt, payload=payload)
    target = _git("rev-parse", "refs/heads/main", cwd=repository)
    with pytest.raises(CandidateChangeRejected, match="CANDIDATE_(DESCRIPTOR|IDENTITY)_MISMATCH"):
        verify_candidate_receipt(divergent, checked, repository_root=repository)
    with pytest.raises(CandidateChangeRejected, match="CANDIDATE_(DESCRIPTOR|IDENTITY)_MISMATCH"):
        land_recorded_candidate(divergent, checked, repository_root=repository)
    assert _git("rev-parse", "refs/heads/main", cwd=repository) == target


def test_landing_refuses_a_target_that_moved_after_the_candidate_and_changes_nothing(tmp_path: Path):
    repository, descriptor, receipt = _recorded(tmp_path)
    (repository / "next.txt").write_text("next\n", encoding="utf-8")
    _git("add", ".", cwd=repository)
    _git("commit", "-qm", "target moved", cwd=repository)
    moved = _git("rev-parse", "refs/heads/main", cwd=repository)
    verify_candidate_receipt(receipt, descriptor, repository_root=repository)  # the candidate itself is still exact
    with pytest.raises(CandidateGitRejected, match="BASE_REF_DIVERGED"):
        land_recorded_candidate(receipt, descriptor, repository_root=repository)
    assert _git("rev-parse", "refs/heads/main", cwd=repository) == moved  # no rebase, merge or rewind
    assert _git("rev-list", "--count", "refs/heads/main", cwd=repository) == "2"
