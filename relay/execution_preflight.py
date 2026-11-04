"""Incumbent/shadow/qualification admission without provider duplication."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from relay import relay_io
from relay.train_execution import (
    DESCRIPTOR_FILE,
    ExecutionDescriptorRejected,
    read_execution_descriptor,
    validate_descriptor_for_train,
)


MODES = {"incumbent", "shadow", "strict"}


@dataclass(frozen=True)
class PreflightResult:
    mode: str
    admissible: bool
    reason: str
    descriptor_sha256: str | None = None


def configured_mode(environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    mode = source.get("TARE_AGENT_RUNTIME_MODE", "incumbent").strip().lower()
    if mode not in MODES:
        raise ValueError("AGENT_RUNTIME_MODE_INVALID")
    return mode


def preflight_train_execution(
    train_dir: str | Path,
    *,
    work_graph_file: str | Path,
    repository_root: str | Path | None,
    mode: str,
) -> PreflightResult:
    """Read-only admission. It never calls a provider or creates a worktree."""

    if mode not in MODES:
        return PreflightResult(mode, False, "AGENT_RUNTIME_MODE_INVALID")
    if mode == "incumbent":
        return PreflightResult(mode, True, "INCUMBENT_UNCHANGED")
    root = Path(train_dir).resolve()
    try:
        state = json.loads((root / "STATE.json").read_bytes())
        if not isinstance(state, dict):
            raise ValueError
    except (OSError, json.JSONDecodeError, ValueError):
        return PreflightResult(mode, False, "TRAIN_STATE_UNAVAILABLE")
    try:
        descriptor = read_execution_descriptor(root / DESCRIPTOR_FILE)
        seal = state.get("execution_descriptor")
        if not isinstance(seal, dict) or seal.get("sha256") != descriptor.sha256:
            raise ExecutionDescriptorRejected("DESCRIPTOR_SEAL_MISMATCH")
        validate_descriptor_for_train(
            descriptor,
            train_id=state.get("train_id", root.name),
            task_ids=state.get("task_ids", []),
            work_graph_file=work_graph_file,
        )
        if repository_root is None:
            raise ExecutionDescriptorRejected("EXECUTION_REPOSITORY_UNAVAILABLE")
        from compute_plane.workspace import (
            WorkspaceQualificationRejected,
            require_base_commit,
        )

        try:
            require_base_commit(repository_root, descriptor.base_commit)
        except WorkspaceQualificationRejected as exc:
            raise ExecutionDescriptorRejected("BASE_COMMIT_UNAVAILABLE") from exc
    except ModuleNotFoundError:
        return PreflightResult(mode, False, "KERNEL_UNAVAILABLE")
    except (ExecutionDescriptorRejected, OSError) as exc:
        return PreflightResult(mode, False, str(exc))
    return PreflightResult(mode, True, "ADMISSIBLE", descriptor.sha256)


def record_shadow_preflight(train_dir: str | Path, result: PreflightResult) -> None:
    """Bounded non-authoritative observation; repeated identical results are no-ops."""

    if result.mode != "shadow":
        return
    relay_io.write_json_if_changed(Path(train_dir) / "SHADOW_PREFLIGHT.json", {
        "schema": "tare.tools/shadow-preflight/1",
        "mode": result.mode,
        "admissible": result.admissible,
        "reason": result.reason,
        "descriptor_sha256": result.descriptor_sha256,
        "provider_calls": 0,
    })
