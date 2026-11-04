"""OS-owned immutable candidate receipt, verification, and CAS landing."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from relay.train_execution import TrainExecutionDescriptor, canonical_json_bytes


CANDIDATE_SCHEMA = "tare.tools/candidate-change/1"
CANDIDATE_FILE = "CANDIDATE_CHANGE.json"
_FIELDS = {
    "schema", "train_id", "descriptor_sha256", "work_id", "repository",
    "base_commit", "worktree_id", "candidate_commit", "candidate_tree",
    "workspace_ready_sha256", "diff_sha256", "changed_paths", "changed_entries",
}


class CandidateChangeRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateReceipt:
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str


def materialize_verify_and_record_candidate(
    train_dir: str | Path,
    descriptor: TrainExecutionDescriptor,
    lease: Any,
    *,
    verification_runner: Callable[..., Any] | None = None,
    recover: bool = False,
    check_control: Callable[[], None] = lambda: None,
    verification_timeout: float | None = 60.0,
) -> CandidateReceipt:
    """Materialize first, verify those detached bytes, reaffirm, then receipt."""

    from compute_plane.candidate_git import materialize_candidate_commit
    from compute_plane.managed_work import VerificationCommand, observe_work_progress
    from compute_plane.workspace import (
        WorkspaceQualificationRejected,
        qualify_ephemeral_git_worktree,
    )
    from relay.strict_workspace import read_workspace_ready

    ready_file = (
        Path(train_dir).resolve()
        / f"WORKSPACE_READY.{lease.worktree_id}.json"
    )
    ready = read_workspace_ready(ready_file)
    ready_raw = ready_file.read_bytes()
    if (
        ready.get("train_id") != descriptor.train_id
        or ready.get("descriptor_sha256") != descriptor.sha256
        or ready.get("repository") != descriptor.execution_repository
        or ready.get("base_commit") != descriptor.base_commit
        or ready.get("head") != descriptor.base_commit
        or ready.get("worktree_id") != lease.worktree_id
        or Path(ready.get("repository_root", "")).resolve() != lease.repository_root
        or Path(ready.get("destination", "")).resolve() != lease.root
        or Path(ready.get("git_common_dir", "")).resolve() != lease.git_common_dir
        or Path(ready.get("git_admin_dir", "")).resolve() != lease.git_admin_dir
    ):
        raise CandidateChangeRejected("WORKSPACE_READY_MISMATCH")

    check_control()
    if recover:
        # Native caller holds exclusive custody for this whole passage. The
        # recorded object, never the latest arbitrary HEAD, is the recovery key.
        from compute_plane.candidate_git import prepare_candidate_commit, pin_candidate_commit
        from compute_plane.native_journal import regular, save_json
        intent_path = regular(Path(train_dir) / 'CANDIDATE_INTENT.json')
        expected = {'descriptor_sha256': descriptor.sha256, 'worktree_id': lease.worktree_id,
                    'workspace_ready_sha256': hashlib.sha256(ready_raw).hexdigest()}
        if intent_path.exists():
            intent = json.loads(intent_path.read_bytes())
            if set(intent) != set(expected) | {'candidate_commit'} or any(intent[k] != v for k,v in expected.items()):
                raise CandidateChangeRejected('CANDIDATE_INTENT_MISMATCH')
        else:
            identity = prepare_candidate_commit(lease, tracked_paths=descriptor.tracked_paths)
            intent = expected | {'candidate_commit': identity.candidate_commit}
            save_json(intent_path, intent)
        check_control()
        identity, pinned_lease = pin_candidate_commit(lease, candidate_commit=intent['candidate_commit'],
                                                     tracked_paths=descriptor.tracked_paths)
        if (Path(train_dir) / CANDIDATE_FILE).exists():
            from relay.executor_observation import verification_observed
            receipt = read_candidate_receipt(Path(train_dir) / CANDIDATE_FILE)
            verify_candidate_receipt(receipt, descriptor, repository_root=lease.repository_root)
            if receipt.payload['candidate_commit'] != identity.candidate_commit or not verification_observed(train_dir, descriptor, receipt):
                raise CandidateChangeRejected('CANDIDATE_RECOVERY_EVIDENCE_MISMATCH')
            return receipt
    else:
        identity, pinned_lease = materialize_candidate_commit(
            lease, tracked_paths=descriptor.tracked_paths
        )
    checks = tuple(
        VerificationCommand(check.criterion, check.command, timeout_seconds=verification_timeout)
        for check in descriptor.verification_checks
    )
    kwargs: dict[str, Any] = {
        "workspace": pinned_lease.root,
        "exit_criteria": tuple(check.criterion for check in checks),
        "checks": checks,
        "tracked_paths": descriptor.tracked_paths,
    }
    if verification_runner is not None:
        kwargs["runner"] = verification_runner
    check_control()
    progress = observe_work_progress(**kwargs)
    check_control()
    if progress.remaining_obligations != 0:
        raise CandidateChangeRejected("CANDIDATE_VERIFICATION_FAILED")
    try:
        reaffirmed = qualify_ephemeral_git_worktree(pinned_lease.root)
    except WorkspaceQualificationRejected as exc:
        raise CandidateChangeRejected("CANDIDATE_DRIFT_AFTER_VERIFICATION") from exc
    if reaffirmed.head != identity.candidate_commit or not reaffirmed.clean:
        raise CandidateChangeRejected("CANDIDATE_DRIFT_AFTER_VERIFICATION")
    payload = {
        "schema": CANDIDATE_SCHEMA,
        "train_id": descriptor.train_id,
        "descriptor_sha256": descriptor.sha256,
        "work_id": descriptor.work_id,
        "repository": descriptor.execution_repository,
        "base_commit": descriptor.base_commit,
        "worktree_id": pinned_lease.worktree_id,
        "workspace_ready_sha256": hashlib.sha256(ready_raw).hexdigest(),
        "candidate_commit": identity.candidate_commit,
        "candidate_tree": identity.candidate_tree,
        "diff_sha256": identity.diff_sha256,
        "changed_paths": list(identity.changed_paths),
        "changed_entries": [
            {
                "path": entry.path,
                "old_mode": entry.old_mode,
                "new_mode": entry.new_mode,
                "status": entry.status,
            }
            for entry in identity.changed_entries
        ],
    }
    raw = canonical_json_bytes(payload)
    # Preserve the actual Kernel observation separately from immutable Git
    # identity. This is advisory owner evidence, not a cryptographic signature.
    from specgraph.executor_evidence import project_executor_observation
    observation = project_executor_observation(
        progress, commands=checks, executor="tare.tools.kernel/compute_plane.managed_work",
        candidate_sha256=hashlib.sha256(raw).hexdigest(), descriptor_sha256=descriptor.sha256,
        max_bytes=65536,
    )
    if observation["status"] != "NEEDS_REVALIDATION":
        raise CandidateChangeRejected("EXECUTOR_OBSERVATION_INVALID")
    if recover:
        # A verifier can be rerun before the final receipt; its prior observation
        # is not yet settled evidence. Publish the receipt last, atomically.
        save_json(regular(Path(train_dir) / 'EXECUTOR_OBSERVATION.json'), observation)
        save_json(regular(Path(train_dir) / CANDIDATE_FILE), payload)
    else:
        _write_create_only(Path(train_dir).resolve() / "EXECUTOR_OBSERVATION.json", canonical_json_bytes(observation))
        _write_create_only(Path(train_dir).resolve() / CANDIDATE_FILE, raw)
    return CandidateReceipt(payload, raw, hashlib.sha256(raw).hexdigest())


def read_candidate_receipt(path: str | Path) -> CandidateReceipt:
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateChangeRejected("CANDIDATE_RECEIPT_UNAVAILABLE") from exc
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise CandidateChangeRejected("CANDIDATE_RECEIPT_FIELDS_INVALID")
    if payload.get("schema") != CANDIDATE_SCHEMA or raw != canonical_json_bytes(payload):
        raise CandidateChangeRejected("CANDIDATE_RECEIPT_NOT_CANONICAL")
    return CandidateReceipt(payload, raw, hashlib.sha256(raw).hexdigest())


def verify_candidate_receipt(
    receipt: CandidateReceipt,
    descriptor: TrainExecutionDescriptor,
    *,
    repository_root: str | Path,
) -> None:
    """Audit immutable objects and complete path/mode enumeration from scratch."""

    from compute_plane.candidate_git import inspect_candidate_commit

    payload = receipt.payload
    expected = {
        "train_id": descriptor.train_id,
        "descriptor_sha256": descriptor.sha256,
        "work_id": descriptor.work_id,
        "repository": descriptor.execution_repository,
        "base_commit": descriptor.base_commit,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise CandidateChangeRejected("CANDIDATE_DESCRIPTOR_MISMATCH")
    identity = inspect_candidate_commit(
        repository_root,
        base_commit=descriptor.base_commit,
        candidate_commit=payload["candidate_commit"],
        tracked_paths=descriptor.tracked_paths,
    )
    actual_entries = [
        {
            "path": entry.path,
            "old_mode": entry.old_mode,
            "new_mode": entry.new_mode,
            "status": entry.status,
        }
        for entry in identity.changed_entries
    ]
    if (
        payload["candidate_tree"] != identity.candidate_tree
        or payload["diff_sha256"] != identity.diff_sha256
        or payload["changed_paths"] != list(identity.changed_paths)
        or payload["changed_entries"] != actual_entries
    ):
        raise CandidateChangeRejected("CANDIDATE_IDENTITY_MISMATCH")


def land_recorded_candidate(
    receipt: CandidateReceipt,
    descriptor: TrainExecutionDescriptor,
    *,
    repository_root: str | Path,
) -> str:
    """Reverify immutable identity and advance only the sealed target by CAS."""

    from compute_plane.candidate_git import land_candidate_ref

    verify_candidate_receipt(receipt, descriptor, repository_root=repository_root)
    return land_candidate_ref(
        repository_root,
        target_ref=descriptor.target_ref,
        base_commit=descriptor.base_commit,
        candidate_commit=receipt.payload["candidate_commit"],
    )


def _write_create_only(path: Path, raw: bytes) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CandidateChangeRejected("CANDIDATE_RECEIPT_ALREADY_EXISTS") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
