#!/usr/bin/env python3
"""SEC.3 control-plane liveness — a deterministic registry of (control, probe)
pairs so a silently-dead security control (a scrub call deleted in a refactor,
an env-filter flipped off, a protection snapshot gone) fails the gate instead
of dying unnoticed. "Declared control" is EXPLICIT DATA: exactly the seven
PROBES entries below, each source-verified.

`evaluate(root)` is PURE READS (no writes, no subprocesses):
  - `source`: the named symbol appears in the named file's text;
  - `file`:   the file exists AND `json.loads` parses it;
  - `config`: the dotted `.harness/project.json` value is NOT `False`
              (the escape hatch value; absent key stays alive).

A refactor that legitimately moves/renames a control must update PROBES in the
same commit. Self-check: `python scripts/harness_lib/control_liveness.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

# (control, kind, path-or-dotted-key, symbol-or-None) — the seven declared controls.
PROBES: list[tuple[str, str, str, str | None]] = [
    ("collect-secret-scrub", "source", "scripts/harness_lib/workflow_reduce.py", "secret_scan.scan"),
    ("discover-pre-egress", "source", "scripts/harness_lib/discovery.py", "_pre_egress_reason"),
    ("target-gate-env-filter", "source", "scripts/harness_lib/gate_generic.py", "filter_spawn_env"),
    ("security-baseline-wired", "source", "scripts/spec_test_gate.py", "security_baseline.evaluate"),
    ("protected-files-snapshot", "file", ".harness/protected-files.snapshot.json", None),
    ("worker-env-filter", "config", "workflows.workerEnvFilter", None),
    # ROUTE replaced BLOCK (owner decision 2026-07-13 #1) — a silently-dead
    # routing call would be strictly worse than the hard-fail it superseded.
    ("security-diff-routing", "source", "scripts/spec_test_gate.py", "security_routing.route"),
]


def evaluate(root: Path | str) -> list[dict[str, str]]:
    """One {"control","status","detail"} per PROBES entry; status ok|dead."""
    root = Path(root)
    out: list[dict[str, str]] = []
    for control, kind, path, symbol in PROBES:
        if kind == "source":
            try:
                text = (root / path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            ok = bool(symbol) and symbol in text
            detail = (f"{symbol} present in {path}" if ok
                      else f"{symbol} NOT found in {path}")
        elif kind == "file":
            try:
                json.loads((root / path).read_text(encoding="utf-8"))
                ok, detail = True, f"{path} present and parses"
            except (OSError, json.JSONDecodeError) as exc:
                ok, detail = False, f"{path}: {type(exc).__name__}"
        else:  # config
            try:
                value = json.loads((root / ".harness" / "project.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            for key in path.split("."):
                value = value.get(key) if isinstance(value, dict) else None
            ok = value is not False  # ponytail: only the explicit escape hatch kills it
            detail = f"{path}={value!r}"
        out.append({"control": control, "status": "ok" if ok else "dead", "detail": detail})
    return out


if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[2]
    rows = evaluate(repo)
    for row in rows:
        print(f"{row['status']:>4}  {row['control']} — {row['detail']}")
    assert len(rows) == len(PROBES) == 7, rows
    dead = [r["control"] for r in rows if r["status"] != "ok"]
    assert not dead, f"dead on this tree (fix the probe, not the control): {dead}"
    print("control_liveness self-check: ok")
