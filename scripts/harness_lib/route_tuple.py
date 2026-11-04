#!/usr/bin/env python3
"""Route tuple (Article 3.5 / SF-3b, C13) -- the versioned pin that makes a route
comparable across vendors.

An effort label ("high", "xhigh") is NOT comparable between vendors without the
surrounding pin: {vendor, model, effort, taskProfile, topology, writeAllowed,
sandbox, harnessSha, adapterSchema, at}. Trigger A2 (a 2nd vendor in production
in the rooms, live since 925daea) is reached, so the tuple is stamped on the
route records; future C3VR comparability and drift detection then have a real
substrate.

ADDITIVE + OBSERVATIONAL: no consumer changes behavior on it today -- it is a
pin, not a gate. Absent fields are null, never invented (an unresolved model
stays null, not a guess). The one git touch (short HEAD sha) is fail-open
"unknown"; a missing adapter registry -> null adapterSchema.

Pure stdlib, EN-only, ASCII-safe. Self-check: python scripts/harness_lib/route_tuple.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # standalone self-check imports siblings
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import processes  # noqa: E402

EXECUTORS_REL = ".harness/routing/executors.json"
TOPOLOGIES = ("workflow-worker", "detached-dispatch", "room", "inline")


def _harness_sha(root: Path) -> str:
    """Short git HEAD sha via the mediated spawn; fail-open "unknown" (no git, no
    HEAD, any error). The ONLY git touch in the builder -- run_quiet, never raw."""
    try:
        out = processes.run_quiet(["git", "rev-parse", "--short", "HEAD"],
                                  cwd=str(root), capture_output=True, text=True, timeout=10)
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _adapter_schema(root: Path):
    """schemaVersion of the executors adapter registry; null when unreadable."""
    try:
        doc = json.loads((Path(root) / EXECUTORS_REL).read_text(encoding="utf-8"))
        version = doc.get("schemaVersion")
        return version if isinstance(version, str) else None
    except Exception:
        return None


def _sandbox(vendor: str, write_allowed: bool) -> str:
    """Vendor-derived confinement label (the inner layer sandbox_spawn composes,
    rule 7): codex maps writeAllowed to its --sandbox flag, claude confines via the
    allowedTools ceiling, an HTTP open-model worker has no fs tool at all."""
    if vendor == "codex":
        return "workspace-write" if write_allowed else "read-only"
    if vendor == "claude":
        return "allowedTools-ceiling"
    return "none"


def route_tuple(*, executor: str, model: str | None = None, effort: str | None = None,
                task_profile: str | None = None, topology: str, write_allowed: bool = False,
                epoch: int | None = None, root: Path) -> dict:
    """The versioned route pin stamped on a route record (Article 3.5/C13).

    ADDITIVE + observational: nothing reads it to change behavior; it exists so
    effort labels become comparable across vendors (C3VR) and drift is detectable.
    Absent fields (model/effort/taskProfile unresolved at the call site) stay null
    -- never guessed. `harnessSha` fail-opens to "unknown"; `adapterSchema` to null.

    SPEC-149: `epoch` (the ownership fencing token of a run-birth topology) is
    carried ONLY when supplied -- a room/inline tuple has no epoch, so the key is
    omitted rather than nulled, keeping the base 10-field shape unchanged.
    """
    pin = {
        "vendor": executor,
        "model": model,
        "effort": effort,
        "taskProfile": task_profile,
        "topology": topology,
        "writeAllowed": bool(write_allowed),
        "sandbox": _sandbox(executor, bool(write_allowed)),
        "harnessSha": _harness_sha(Path(root)),
        "adapterSchema": _adapter_schema(root),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if epoch is not None:
        pin["epoch"] = int(epoch)
    return pin


def _self_check() -> None:
    import shutil
    import tempfile

    keys = {"vendor", "model", "effort", "taskProfile", "topology", "writeAllowed",
            "sandbox", "harnessSha", "adapterSchema", "at"}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)  # no git, no adapter registry -> both fail-open paths fire
        bare = route_tuple(executor="claude", topology="room", root=root)
        assert set(bare) == keys, bare
        # absent fields stay null, never invented
        assert bare["model"] is None and bare["effort"] is None and bare["taskProfile"] is None, bare
        # sha fail-open on a git-less root; adapterSchema null on a missing registry
        assert bare["harnessSha"] == "unknown", bare["harnessSha"]
        assert bare["adapterSchema"] is None, bare["adapterSchema"]
        # claude confines via the tools ceiling; writeAllowed defaults False
        assert bare["sandbox"] == "allowedTools-ceiling", bare
        assert bare["writeAllowed"] is False and bare["vendor"] == "claude", bare
        # ASCII-safe json round-trip; the null-safe fields serialize to JSON null
        blob = json.dumps(bare, ensure_ascii=True)
        assert '"model": null' in blob and '"adapterSchema": null' in blob, blob
        assert all(ord(c) < 128 for c in blob), "tuple JSON must be ASCII-safe"

        # sandbox derivation matrix (vendor x write): codex write-dependent, http none
        cx_w = route_tuple(executor="codex", topology="detached-dispatch", write_allowed=True, root=root)
        cx_r = route_tuple(executor="codex", topology="workflow-worker", write_allowed=False, root=root)
        http = route_tuple(executor="local-llama", topology="workflow-worker", root=root)
        assert cx_w["sandbox"] == "workspace-write" and cx_r["sandbox"] == "read-only", (cx_w, cx_r)
        assert http["sandbox"] == "none", http

        # a real executors.json under the temp root -> adapterSchema is READ, not null
        reg = root / EXECUTORS_REL
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"schemaVersion": "9.9"}), encoding="utf-8")
        assert route_tuple(executor="claude", topology="room", root=root)["adapterSchema"] == "9.9"

        # resolved fields pass straight through (the comparability substrate)
        full = route_tuple(executor="codex", model="gpt-5.5", effort="high",
                           task_profile="scan", topology="workflow-worker", root=root)
        assert full["model"] == "gpt-5.5" and full["effort"] == "high" and full["taskProfile"] == "scan"

        # SPEC-149: epoch carried only when supplied (additive; base shape unchanged)
        assert "epoch" not in route_tuple(executor="claude", topology="room", root=root)
        assert route_tuple(executor="codex", topology="detached-dispatch", epoch=42, root=root)["epoch"] == 42

    # when git is available, the real repo root yields a real short sha (not
    # "unknown") -- proving the fail-open path is not masking a broken happy path
    if shutil.which("git"):
        live = route_tuple(executor="claude", topology="inline", root=_SCRIPTS.parent)
        assert live["harnessSha"] != "unknown" and len(live["harnessSha"]) >= 4, live["harnessSha"]

    print("route_tuple self-check OK")


if __name__ == "__main__":
    _self_check()
