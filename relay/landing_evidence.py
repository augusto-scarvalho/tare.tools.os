"""Derive smart-landing evidence from exact Work, SpecGraph, and Git state."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from relay.landing_policy import LandingEvidence, create_evidence, write_evidence


_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        rb"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"
    ),
)
_PROTECTED_NAMES = frozenset({".env", "credentials", "credentials.json"})


def materialize_landing_evidence(
    *,
    train_dir: str | Path,
    train_id: str,
    descriptor: Any,
    candidate_receipt: Any,
    repository_root: str | Path,
    work_graph: str | Path,
    library_manifest: str | Path,
    backlog_command: Sequence[str] = ("backlog",),
    specgraph_command: Sequence[str] = ("specgraph",),
    backlog_runner: Callable[..., Any] | None = None,
    specgraph_runner: Callable[..., Any] | None = None,
    owner_roots: dict[str, str | Path] | None = None,
) -> LandingEvidence:
    """Re-observe canonical graphs and Git, then persist one create-only receipt."""

    from compute_plane.backlog_work import check_backlog_scope, materialize_backlog_work
    from compute_plane.specgraph_grounding import (
        check_specgraph_scope,
        materialize_specgraph_grounding,
    )
    from tare_tools_agent_runtime import GroundingEnvelope, WorkEnvelope

    work_kwargs: dict[str, Any] = {
        "work_id": descriptor.work_id,
        "work_graph": work_graph,
        "command": backlog_command,
    }
    if backlog_runner is not None:
        work_kwargs["runner"] = backlog_runner
    work_bytes = materialize_backlog_work(**work_kwargs)
    work = WorkEnvelope(work_bytes)
    work_check_kwargs: dict[str, Any] = {
        "work_id": descriptor.work_id,
        "work_graph": work_graph,
        "expected_scope_sha256": work.execution_scope_sha256,
        "command": backlog_command,
    }
    if backlog_runner is not None:
        work_check_kwargs["runner"] = backlog_runner
    work_check = check_backlog_scope(**work_check_kwargs)

    scopes = [
        scope
        for scope in work.repository_scopes
        if scope.repository == descriptor.execution_repository
    ]
    grounding_scope_hashes: list[str] = []
    specgraph_current = len(scopes) == 1
    scope_complete = len(scopes) == 1
    if scopes:
        scope = scopes[0]
        changed = tuple(candidate_receipt.payload["changed_paths"])
        scope_complete = _paths_within(changed, descriptor.tracked_paths) and _paths_within(
            changed, scope.target_paths
        )
        grounding_kwargs: dict[str, Any] = {
            "repository": scope.repository,
            "library_manifest": library_manifest,
            "include_ids": scope.grounding_refs,
            "command": specgraph_command,
        }
        if specgraph_runner is not None:
            grounding_kwargs["runner"] = specgraph_runner
        grounding = GroundingEnvelope(materialize_specgraph_grounding(**grounding_kwargs))
        grounding_scope_hashes.append(grounding.grounding_scope_sha256)
        check_kwargs: dict[str, Any] = {
            **grounding_kwargs,
            "expected_scope_sha256": grounding.grounding_scope_sha256,
        }
        check = check_specgraph_scope(**check_kwargs)
        specgraph_current = (
            grounding.status == "READY" and check.decision.value == "CURRENT"
        )

    repository = Path(repository_root).resolve()
    from relay.source_admission import evaluate_source_admission
    source_binding = evaluate_source_admission(
        work_graph=work_graph, work_grounding=work_bytes, descriptor=descriptor,
        library_manifest=library_manifest,
        owner_roots=owner_roots if owner_roots is not None else {descriptor.execution_repository: repository},
    )
    scope_complete = scope_complete and source_binding["status"] == "MATCHED"
    # The report explains hard source/AC refusals without extending the trust
    # policy's check vocabulary or promoting SpecGraph to an Authority owner.
    from relay.candidate_change import _write_create_only
    from relay.train_execution import canonical_json_bytes
    _persist_same(Path(train_dir) / "SOURCE_ADMISSION.json", canonical_json_bytes(source_binding), _write_create_only)
    from relay.executor_observation import verification_observed
    verification_complete = verification_observed(train_dir, descriptor, candidate_receipt)
    candidate = str(candidate_receipt.payload["candidate_commit"])
    base = descriptor.base_commit
    diff = _git_bytes(repository, "diff", "--no-ext-diff", "--unified=0", base, candidate)
    binary_change = b"Binary files " in diff or b"GIT binary patch" in diff
    added_lines = b"\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith(b"+") and not line.startswith(b"+++")
    )
    protected_paths_clear = not any(
        _is_protected(path) for path in candidate_receipt.payload["changed_paths"]
    )
    secrets_clear = not binary_change and not any(
        pattern.search(added_lines) for pattern in _SECRET_PATTERNS
    )
    cas_ready = (
        _git_text(repository, "rev-parse", descriptor.target_ref) == descriptor.base_commit
    )
    combined_grounding_scope = hashlib.sha256(
        "\n".join(sorted(grounding_scope_hashes)).encode("ascii")
    ).hexdigest()
    evidence = create_evidence(
        train_id=train_id,
        descriptor_sha256=descriptor.sha256,
        candidate_sha256=candidate_receipt.sha256,
        work_scope_sha256=work.execution_scope_sha256,
        specgraph_scope_sha256=combined_grounding_scope,
        checks={
            "candidate_immutable": True,
            "scope_complete": scope_complete,
            "work_current": work_check.decision.value == "CURRENT",
            "specgraph_current": specgraph_current,
            "verification_complete": verification_complete,
            "protected_paths_clear": protected_paths_clear,
            "secrets_clear": secrets_clear,
            "cas_ready": cas_ready,
        },
    )
    _persist_same(Path(train_dir) / "LANDING_EVIDENCE.json", evidence.canonical_bytes,
                  lambda path, raw: write_evidence(path, evidence))
    return evidence


def _persist_same(path, raw, create):
    """A fresh observation may reuse identical bytes; changed evidence needs reconciliation."""
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('LANDING_EVIDENCE_CHANGED:' + path.name)
    else:
        create(path, raw)


def _paths_within(changed_paths: Sequence[str], allowed_paths: Sequence[str]) -> bool:
    if not changed_paths or not allowed_paths:
        return False
    allowed = tuple(PurePosixPath(item) for item in allowed_paths)
    for raw in changed_paths:
        path = PurePosixPath(raw)
        if not any(path == root or root in path.parents for root in allowed):
            return False
    return True


def _is_protected(raw: str) -> bool:
    path = PurePosixPath(raw)
    lowered = tuple(part.casefold() for part in path.parts)
    return (
        ".git" in lowered
        or any(part in _PROTECTED_NAMES for part in lowered)
        or any(part.startswith(".env.") for part in lowered)
    )


def _git_text(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    ).stdout
