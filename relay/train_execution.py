"""Canonical OS-owned contracts for one strict train execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


DESCRIPTOR_SCHEMA = "tare.tools/train-execution/1"
DESCRIPTOR_FILE = "EXECUTION_DESCRIPTOR.json"
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_TRACKED_PATHS = 256
MAX_CHECKS = 64
MAX_ARGUMENTS = 128
MAX_STRING_BYTES = 4_096

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_TOP_LEVEL_FIELDS = {
    "schema",
    "train_id",
    "work_id",
    "work_graph",
    "execution_repository",
    "base_commit",
    "target_ref",
    "library_manifest",
    "tracked_paths",
    "verification_checks",
}
_CHECK_FIELDS = {"criterion", "command"}
_SHELL_PROGRAMS = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "env",
    "env.exe",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "busybox",
    "busybox.exe",
    "zsh",
}


class ExecutionDescriptorRejected(ValueError):
    """The descriptor cannot safely authorize strict execution."""


@dataclass(frozen=True)
class VerificationCheck:
    criterion: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class TrainExecutionDescriptor:
    schema: str
    train_id: str
    work_id: str
    work_graph: str
    execution_repository: str
    base_commit: str
    target_ref: str
    library_manifest: str
    tracked_paths: tuple[str, ...]
    verification_checks: tuple[VerificationCheck, ...]
    canonical_bytes: bytes
    sha256: str


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def parse_execution_descriptor(raw: bytes) -> TrainExecutionDescriptor:
    """Hash and parse the same bounded byte buffer; non-canonical input fails."""

    if not isinstance(raw, bytes) or not raw:
        raise ExecutionDescriptorRejected("DESCRIPTOR_EMPTY")
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ExecutionDescriptorRejected("DESCRIPTOR_TOO_LARGE")
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionDescriptorRejected("DESCRIPTOR_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise ExecutionDescriptorRejected("DESCRIPTOR_NOT_OBJECT")
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise ExecutionDescriptorRejected("DESCRIPTOR_FIELDS_MISMATCH")
    try:
        canonical = canonical_json_bytes(payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExecutionDescriptorRejected("DESCRIPTOR_NOT_CANONICAL") from exc
    if raw != canonical:
        raise ExecutionDescriptorRejected("DESCRIPTOR_NOT_CANONICAL")

    if payload["schema"] != DESCRIPTOR_SCHEMA:
        raise ExecutionDescriptorRejected("DESCRIPTOR_SCHEMA_UNSUPPORTED")
    train_id = _identity(payload["train_id"], "TRAIN_ID_INVALID")
    work_id = _identity(payload["work_id"], "WORK_ID_INVALID")
    work_graph = _owner_path(payload["work_graph"], "WORK_GRAPH_PATH_INVALID")
    repository = _identity(
        payload["execution_repository"], "EXECUTION_REPOSITORY_INVALID"
    )
    base_commit = payload["base_commit"]
    if not isinstance(base_commit, str) or not _COMMIT_RE.fullmatch(base_commit):
        raise ExecutionDescriptorRejected("BASE_COMMIT_INVALID")
    target_ref = _target_ref(payload["target_ref"])
    library_manifest = _owner_path(
        payload["library_manifest"], "LIBRARY_MANIFEST_PATH_INVALID"
    )

    tracked_raw = payload["tracked_paths"]
    if (
        not isinstance(tracked_raw, list)
        or not tracked_raw
        or len(tracked_raw) > MAX_TRACKED_PATHS
    ):
        raise ExecutionDescriptorRejected("TRACKED_PATHS_INVALID")
    tracked = tuple(
        _relative_path(item, "TRACKED_PATH_INVALID") for item in tracked_raw
    )
    if len(set(tracked)) != len(tracked) or tuple(sorted(tracked)) != tracked:
        raise ExecutionDescriptorRejected("TRACKED_PATHS_NOT_CANONICAL")
    for index, path in enumerate(tracked):
        for other in tracked[index + 1 :]:
            if other.startswith(f"{path}/"):
                raise ExecutionDescriptorRejected("TRACKED_PATHS_OVERLAP")

    checks_raw = payload["verification_checks"]
    if (
        not isinstance(checks_raw, list)
        or not checks_raw
        or len(checks_raw) > MAX_CHECKS
    ):
        raise ExecutionDescriptorRejected("VERIFICATION_CHECKS_INVALID")
    checks: list[VerificationCheck] = []
    for item in checks_raw:
        if not isinstance(item, dict) or set(item) != _CHECK_FIELDS:
            raise ExecutionDescriptorRejected("VERIFICATION_CHECK_FIELDS_INVALID")
        criterion = _bounded_text(item["criterion"], "CRITERION_INVALID")
        command_raw = item["command"]
        if (
            not isinstance(command_raw, list)
            or not command_raw
            or len(command_raw) > MAX_ARGUMENTS
        ):
            raise ExecutionDescriptorRejected("VERIFICATION_COMMAND_INVALID")
        command = tuple(
            _bounded_text(part, "VERIFICATION_ARGUMENT_INVALID")
            for part in command_raw
        )
        executable = PurePosixPath(command[0].replace("\\", "/")).name.lower()
        if executable in _SHELL_PROGRAMS:
            raise ExecutionDescriptorRejected("VERIFICATION_SHELL_FORBIDDEN")
        checks.append(VerificationCheck(criterion, command))
    criteria = tuple(check.criterion for check in checks)
    if len(set(criteria)) != len(criteria):
        raise ExecutionDescriptorRejected("VERIFICATION_CRITERIA_DUPLICATED")

    return TrainExecutionDescriptor(
        schema=DESCRIPTOR_SCHEMA,
        train_id=train_id,
        work_id=work_id,
        work_graph=work_graph,
        execution_repository=repository,
        base_commit=base_commit,
        target_ref=target_ref,
        library_manifest=library_manifest,
        tracked_paths=tracked,
        verification_checks=tuple(checks),
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def read_execution_descriptor(path: str | Path) -> TrainExecutionDescriptor:
    """One filesystem read supplies both the digest and parsed authority."""

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ExecutionDescriptorRejected("DESCRIPTOR_UNAVAILABLE") from exc
    return parse_execution_descriptor(raw)


def create_execution_descriptor(path: str | Path, payload: Mapping[str, Any]) -> str:
    """Create exact canonical bytes once; never overwrite descriptor authority."""

    raw = canonical_json_bytes(payload)
    descriptor = parse_execution_descriptor(raw)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ExecutionDescriptorRejected("DESCRIPTOR_ALREADY_EXISTS") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return descriptor.sha256


def validate_descriptor_for_train(
    descriptor: TrainExecutionDescriptor,
    *,
    train_id: str,
    task_ids: Sequence[str],
    work_graph_file: str | Path,
) -> None:
    """Bind descriptor identity and checks to the current canonical Work."""

    if descriptor.train_id != train_id:
        raise ExecutionDescriptorRejected("DESCRIPTOR_TRAIN_MISMATCH")
    if tuple(task_ids) != (descriptor.work_id,):
        raise ExecutionDescriptorRejected("DESCRIPTOR_REQUIRES_ONE_WORK")
    graph_path = Path(work_graph_file).resolve()
    if (Path(descriptor.work_graph).resolve() != graph_path if Path(descriptor.work_graph).is_absolute()
            else descriptor.work_graph != graph_path.name):
        raise ExecutionDescriptorRejected("WORK_GRAPH_MISMATCH")
    try:
        graph = json.loads(graph_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionDescriptorRejected("WORK_GRAPH_UNAVAILABLE") from exc
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        raise ExecutionDescriptorRejected("WORK_GRAPH_INVALID")
    matches = [
        node for node in nodes
        if isinstance(node, dict) and node.get("id") == descriptor.work_id
    ]
    if len(matches) != 1:
        raise ExecutionDescriptorRejected("WORK_ITEM_UNAVAILABLE")
    criteria = matches[0].get("exit_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ExecutionDescriptorRejected("WORK_CRITERIA_INVALID")
    check_criteria = [check.criterion for check in descriptor.verification_checks]
    if len(set(criteria)) != len(criteria) or set(criteria) != set(check_criteria):
        raise ExecutionDescriptorRejected("WORK_CRITERIA_CHECK_MISMATCH")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionDescriptorRejected("DESCRIPTOR_DUPLICATE_KEY")
        result[key] = value
    return result


def _identity(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        raise ExecutionDescriptorRejected(reason)
    return value


def _bounded_text(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8", errors="strict")) > MAX_STRING_BYTES
    ):
        raise ExecutionDescriptorRejected(reason)
    return value


def _owner_path(value, reason):
    value = _bounded_text(value, reason)
    path = Path(value)
    if path.is_absolute():
        if '..' in path.parts or path.suffix.lower() != '.json':
            raise ExecutionDescriptorRejected(reason)
        from compute_plane.native_journal import regular
        return str(regular(path))
    return _relative_path(value, reason)


def _relative_path(value: Any, reason: str) -> str:
    value = _bounded_text(value, reason)
    if (
        "\\" in value
        or ":" in value
        or value.startswith("/")
        or value.endswith("/")
    ):
        raise ExecutionDescriptorRejected(reason)
    path = PurePosixPath(value)
    if value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExecutionDescriptorRejected(reason)
    if any(part.lower() == ".git" for part in path.parts):
        raise ExecutionDescriptorRejected(reason)
    return value


def _target_ref(value: Any) -> str:
    value = _bounded_text(value, "TARGET_REF_INVALID")
    if (
        not _REF_RE.fullmatch(value)
        or value.endswith(".")
        or value.endswith("/")
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(part.startswith(".") for part in value.split("/"))
        or any(part.endswith(".lock") for part in value.split("/"))
    ):
        raise ExecutionDescriptorRejected("TARGET_REF_INVALID")
    return value
