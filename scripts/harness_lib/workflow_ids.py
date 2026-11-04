"""Workflow and async identifier helpers.

The public CLI keeps the readable ``WF-``/``AG-`` prefixes, while this module
owns timestamp precision and collision probing so schema/runtime drift is tested
at one small boundary instead of inside the CLI wrapper.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable


class WorkflowIdAllocationError(RuntimeError):
    """Raised when an identifier cannot be allocated after collision probing."""


def timestamp_id(prefix: str) -> str:
    """Return a UTC timestamp-shaped identifier with microsecond precision."""
    return prefix + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def allocate_workflow_id(workflow_dir_exists: Callable[[str], bool]) -> str:
    """Allocate a collision-resistant workflow id.

    ``workflow_dir_exists`` lets the CLI keep ownership of its active workflow
    directory layout while this module keeps the ID algorithm deterministic and
    independently testable.
    """
    base = timestamp_id("WF-")
    if not workflow_dir_exists(base):
        return base
    for index in range(1, 1000):
        candidate = f"{base}-{index:03d}"
        if not workflow_dir_exists(candidate):
            return candidate
    raise WorkflowIdAllocationError("Unable to allocate a unique workflow id")


def allocate_async_group_id(existing_group: dict | None) -> str:
    """Allocate a collision-resistant async group id from current group state."""
    existing = existing_group if isinstance(existing_group, dict) else {}
    base = timestamp_id("AG-")
    if existing.get("asyncGroupId") != base:
        return base
    for index in range(1, 1000):
        candidate = f"{base}-{index:03d}"
        if existing.get("asyncGroupId") != candidate:
            return candidate
    raise WorkflowIdAllocationError("Unable to allocate a unique async group id")


def safe_workflow_path(base_dir: Path, wfid: str) -> Path:
    """Return the filesystem path for a workflow id using harness-safe chars."""
    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", wfid)
    return base_dir / safe
