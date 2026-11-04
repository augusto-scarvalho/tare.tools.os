"""TRAIN-32-BACKLOG-ALIGN-01 falsifiers: read-only canonical TaskStore
reconciliation (backlog_align). Positive crosswalk/topology/parity paths plus
explicit negative falsifiers proving the module fails closed on invalid input.
"""
import sys
from pathlib import Path

import pytest

RELAY_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = RELAY_DIR / "scripts"
for p in (str(SCRIPTS),):
    if p not in sys.path:
        sys.path.insert(0, p)

from harness_lib import backlog_align as ba  # noqa: E402


STORE = [
    {"id": "assurance-topology", "title": "Assurance topology checker"},
    {"id": "trusted-judge", "title": "Trusted judge and provenance gate"},
    {"id": "a", "title": "node a", "deps": {"dependsOn": ["b"]}},
    {"id": "b", "title": "node b"},
]


def test_selfcheck_runs():
    ba._demo()  # the module's own asserts, exercised under pytest too


def test_crosswalk_classes_are_exhaustive_and_deterministic():
    proposed = [
        {"key": "trusted-judge"},
        {"key": "assurance-topology-checker", "title": "assurance topology checker POC"},
        {"key": "memory-evolution", "title": "adaptive memory evolution"},
        {"key": "cross-repo", "title": "x", "deps": [{"kind": "conditional-branch"}]},
    ]
    out = ba.reconcile(STORE, proposed)
    got = {r["key"]: r["classification"] for r in out["crosswalk"]}
    assert got["trusted-judge"] == "ADOPT"
    assert got["assurance-topology-checker"] == "ADAPT"
    assert got["memory-evolution"] == "ADD"
    assert got["cross-repo"] == "GAP_UNSUPPORTED"
    assert out["ok"] and out["writes"] is False
    # deterministic + idempotent
    assert ba.reconcile(STORE, proposed)["crosswalk"] == out["crosswalk"]


def test_topology_flags_zero_cycles_on_acyclic_store():
    v = ba.verify_topology(STORE)
    assert v["cycle_detected"] is False and v["ok"] and "a" in v["order"]


def test_deadlock_is_detected_and_strict_fails_closed():
    cyclic = [{"id": "x", "deps": {"dependsOn": ["y"]}},
              {"id": "y", "deps": {"dependsOn": ["x"]}}]
    v = ba.verify_topology(cyclic)
    assert v["cycle_detected"] and v["deadlocks"] and not v["ok"]
    with pytest.raises(ValueError):
        ba.reconcile(cyclic, [], strict=True)


def test_empty_slice_key_fails_closed():
    with pytest.raises(ValueError):
        ba.reconcile(STORE, [{"key": ""}])


def test_duplicate_intake_key_fails_closed():
    with pytest.raises(ValueError):
        ba.reconcile(STORE, [{"key": "d"}, {"key": "d"}])


def test_board_parity_reports_missing_and_extra():
    par = ba.board_parity(["a", "b"], ["a", "c"])
    assert par["parity"] is False
    assert par["missing"] == ["b"] and par["extra"] == ["c"]


def test_empty_inputs_are_boundary_safe():
    out = ba.reconcile([], [])
    assert out["crosswalk"] == [] and out["counts"]["ADD"] == 0
    assert out["topology"]["ok"] and out["parity"]["parity"]
