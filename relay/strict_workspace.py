"""OS ownership of intent/ready artifacts around the Kernel workspace lease."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from relay.train_execution import TrainExecutionDescriptor, canonical_json_bytes


INTENT_SCHEMA = "tare.tools/workspace-intent/1"
READY_SCHEMA = "tare.tools/workspace-ready/1"
_WORKTREE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_INTENT_FIELDS = {
    "schema", "train_id", "descriptor_sha256", "worktree_id", "repository",
    "base_commit", "destination",
}
_READY_FIELDS = _INTENT_FIELDS | {
    "repository_root", "git_common_dir", "git_admin_dir", "head",
}


class StrictWorkspaceRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class StrictWorkspaceArtifacts:
    intent_file: Path
    ready_file: Path
    intent: Mapping[str, Any]
    ready: Mapping[str, Any]
    lease: Any


def provision_train_workspace(
    train_dir: str | Path,
    descriptor: TrainExecutionDescriptor,
    *,
    repository_root: str | Path,
    workspace_parent: str | Path,
    id_factory: Callable[[], str] | None = None,
) -> StrictWorkspaceArtifacts:
    """Persist intent first, ask Kernel to provision, then persist ready once."""

    from compute_plane.workspace import provision_linked_worktree

    train_root = Path(train_dir).resolve()
    state = _load_state(train_root)
    _require_seal(state, descriptor)
    parent = Path(workspace_parent).resolve()
    if not parent.is_dir():
        raise StrictWorkspaceRejected("WORKSPACE_PARENT_UNAVAILABLE")
    worktree_id = (id_factory or (lambda: f"wt-{secrets.token_hex(16)}"))()
    if not isinstance(worktree_id, str) or not _WORKTREE_ID.fullmatch(worktree_id):
        raise StrictWorkspaceRejected("WORKTREE_ID_INVALID")
    destination = (parent / f"tare-worktree-{worktree_id}").resolve()
    intent = {
        "schema": INTENT_SCHEMA,
        "train_id": descriptor.train_id,
        "descriptor_sha256": descriptor.sha256,
        "worktree_id": worktree_id,
        "repository": descriptor.execution_repository,
        "base_commit": descriptor.base_commit,
        "destination": str(destination),
    }
    intent_file = train_root / f"WORKSPACE_INTENT.{worktree_id}.json"
    _write_create_only(intent_file, intent)
    lease = provision_linked_worktree(
        repository_root,
        destination,
        base_commit=descriptor.base_commit,
        worktree_id=worktree_id,
    )
    ready = {
        "schema": READY_SCHEMA,
        **{key: intent[key] for key in (
            "train_id", "descriptor_sha256", "worktree_id", "repository",
            "base_commit", "destination",
        )},
        "repository_root": str(lease.repository_root),
        "git_common_dir": str(lease.git_common_dir),
        "git_admin_dir": str(lease.git_admin_dir),
        "head": lease.base_commit,
    }
    ready_file = train_root / f"WORKSPACE_READY.{worktree_id}.json"
    try:
        _write_create_only(ready_file, ready)
    except Exception:
        # The linked worktree remains intentionally recoverable through intent.
        raise
    return StrictWorkspaceArtifacts(intent_file, ready_file, intent, ready, lease)


def reconcile_workspace_intent(
    intent_file: str | Path,
    *,
    repository_root: str | Path,
) -> Any | None:
    """Reconcile one named intent and no other directory entry."""

    from compute_plane.workspace import reconcile_linked_worktree

    intent = _load_contract(Path(intent_file), INTENT_SCHEMA)
    return reconcile_linked_worktree(
        repository_root,
        intent["destination"],
        base_commit=intent["base_commit"],
        worktree_id=intent["worktree_id"],
    )


def read_workspace_ready(path: str | Path) -> Mapping[str, Any]:
    return _load_contract(Path(path), READY_SCHEMA)


def recover_train_workspace(train_dir, descriptor, *, repository_root, workspace_parent):
    """Recover the sole recorded workspace, including death between add and READY."""
    from compute_plane.workspace import (WorkspaceLease, qualify_ephemeral_git_worktree,
                                         provision_linked_worktree)
    train, repository, parent = Path(train_dir), Path(repository_root).resolve(), Path(workspace_parent).resolve()
    _require_seal(_load_state(train), descriptor)
    intents = list(train.glob('WORKSPACE_INTENT.*.json'))
    if not intents:
        parent.mkdir(parents=True, exist_ok=True)
        return provision_train_workspace(train, descriptor, repository_root=repository,
                                         workspace_parent=parent).lease
    if len(intents) != 1:
        raise StrictWorkspaceRejected('WORKSPACE_RECOVERY_AMBIGUOUS')
    intent = _load_contract(intents[0], INTENT_SCHEMA)
    if (intent['train_id'] != descriptor.train_id or intent['descriptor_sha256'] != descriptor.sha256
            or intent['repository'] != descriptor.execution_repository or intent['base_commit'] != descriptor.base_commit
            or Path(intent['destination']).resolve().parent != parent):
        raise StrictWorkspaceRejected('WORKSPACE_INTENT_MISMATCH')
    ready_path = train/f"WORKSPACE_READY.{intent['worktree_id']}.json"
    if not ready_path.exists():
        lease = reconcile_workspace_intent(intents[0], repository_root=repository)
        if lease is None:
            lease = provision_linked_worktree(repository, intent['destination'], base_commit=descriptor.base_commit,
                                              worktree_id=intent['worktree_id'])
        _write_create_only(ready_path, dict(intent, schema=READY_SCHEMA, repository_root=str(lease.repository_root),
            git_common_dir=str(lease.git_common_dir), git_admin_dir=str(lease.git_admin_dir), head=lease.base_commit))
    ready = read_workspace_ready(ready_path)
    if (any(ready[k] != v for k, v in intent.items() if k != 'schema')
            or Path(ready['repository_root']).resolve() != repository):
        raise StrictWorkspaceRejected('WORKSPACE_READY_MISMATCH')
    observed = qualify_ephemeral_git_worktree(ready['destination'], require_clean=False)
    if (observed.git_common_dir != Path(ready['git_common_dir']).resolve()
            or observed.git_admin_dir != Path(ready['git_admin_dir']).resolve()):
        raise StrictWorkspaceRejected('WORKSPACE_READY_MISMATCH')
    if observed.head != descriptor.base_commit:
        pin = train/'CANDIDATE_INTENT.json'
        if not pin.exists() or json.loads(pin.read_bytes()).get('candidate_commit') != observed.head:
            raise StrictWorkspaceRejected('WORKSPACE_HEAD_CHANGED')
    return WorkspaceLease(intent['worktree_id'], repository, observed.root, observed.git_common_dir,
                          observed.git_admin_dir, descriptor.base_commit, observed.head)


def _load_state(train_root: Path) -> Mapping[str, Any]:
    try:
        state = json.loads((train_root / "STATE.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise StrictWorkspaceRejected("TRAIN_STATE_UNAVAILABLE") from exc
    if not isinstance(state, dict):
        raise StrictWorkspaceRejected("TRAIN_STATE_INVALID")
    return state


def _require_seal(state: Mapping[str, Any], descriptor: TrainExecutionDescriptor) -> None:
    seal = state.get("execution_descriptor")
    if not isinstance(seal, dict) or seal.get("sha256") != descriptor.sha256:
        raise StrictWorkspaceRejected("DESCRIPTOR_SEAL_MISMATCH")
    if state.get("train_id") != descriptor.train_id:
        raise StrictWorkspaceRejected("TRAIN_ID_MISMATCH")


def _write_create_only(path: Path, value: Mapping[str, Any]) -> None:
    raw = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StrictWorkspaceRejected(f"ARTIFACT_ALREADY_EXISTS:{path.name}") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _load_contract(path: Path, schema: str) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            raise StrictWorkspaceRejected("WORKSPACE_ARTIFACT_INVALID")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise StrictWorkspaceRejected("WORKSPACE_ARTIFACT_UNAVAILABLE") from exc
    fields = _INTENT_FIELDS if schema == INTENT_SCHEMA else _READY_FIELDS
    if (
        not isinstance(value, dict)
        or value.get("schema") != schema
        or set(value) != fields
    ):
        raise StrictWorkspaceRejected("WORKSPACE_ARTIFACT_INVALID")
    if raw != canonical_json_bytes(value):
        raise StrictWorkspaceRejected("WORKSPACE_ARTIFACT_NOT_CANONICAL")
    return value
