"""TRAIN-01-BOOTSTRAP tests: graph_backlog_adapter (GRAPH-BACKLOG-02) and
bootstrap_envelope (PROCESS-REPAIR-01).

The adapter lives in the harness repo; override its location with
HARNESS_REPO_DIR if not at the default path.
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

RELAY_DIR = Path(__file__).resolve().parents[1]
HARNESS_LIB_LOCAL = RELAY_DIR / "scripts" / "harness_lib"
HARNESS_LIB_REMOTE = Path(os.environ.get(
    "HARNESS_REPO_DIR", "C:/projects/universal-agent-harness-prototype")) / "scripts" / "harness_lib"

for p in [str(HARNESS_LIB_REMOTE), str(HARNESS_LIB_LOCAL)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import bootstrap_envelope as be  # noqa: E402
from graph_backlog_adapter import GraphBacklogAdapter  # noqa: E402


@pytest.fixture(scope="module")
def adapter():
    return GraphBacklogAdapter(relay_dir=RELAY_DIR)


def _graph_digest():
    return hashlib.sha256((RELAY_DIR / "work-graph.json").read_bytes()).hexdigest()


def test_next_returns_ranked_frontier(adapter):
    before = _graph_digest()
    items = adapter.get_next(limit=3)
    assert isinstance(items, list) and 1 <= len(items) <= 3
    assert all("id" in i and "score" in i for i in items)
    assert _graph_digest() == before, "query verb must not mutate work-graph.json"


def test_packet_and_why_delegation(adapter):
    packet = adapter.get_packet("PROCESS-REPAIR-01")
    assert packet["work"]["id"] == "PROCESS-REPAIR-01"
    assert packet["work"]["exit_criteria"]
    why = adapter.why("GRAPH-BACKLOG-02")
    assert why["id"] == "GRAPH-BACKLOG-02"
    assert "ready" in why


def test_doctor_validates_graph(adapter):
    before = _graph_digest()
    report = adapter.diagnose()
    assert report["status"] == "PASS"
    assert report["blocking_sccs"] == []
    assert _graph_digest() == before


def test_envelope_deterministic_and_monotonic():
    t0 = 1_000_000.0
    r1 = be.mint_root("owner", ["A", "B"], max_children=2, issued_at=t0)
    r2 = be.mint_root("owner", ["B", "A"], max_children=2, issued_at=t0)
    assert r1["oid"] == r2["oid"]
    c1 = be.derive_child(r1, "A", now=t0 + 1)
    c2 = be.derive_child(r1, "B", now=t0 + 2)
    assert (c1["seq"], c2["seq"]) == (1, 2)
    assert be.verify_child(c1, r1) and be.verify_child(c2, r1)


def test_envelope_fails_closed():
    t0 = 1_000_000.0
    root = be.mint_root("owner", ["A"], max_children=1, issued_at=t0)
    child = be.derive_child(root, "A", now=t0 + 1)
    with pytest.raises(ValueError):
        be.derive_child(root, "A", now=t0 + 2)  # budget exhausted
    with pytest.raises(ValueError):
        be.derive_child(be.mint_root("o", ["A"], issued_at=t0), "X", now=t0 + 1)  # scope
    with pytest.raises(ValueError):
        expired = be.mint_root("o", ["A"], ttl_seconds=1, issued_at=t0)
        be.derive_child(expired, "A", now=t0 + 10)  # expired
    assert not be.verify_child(dict(child, task_id="B"), root)  # forgery
