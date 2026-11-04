#!/usr/bin/env python3
"""BACKLOG-ALIGN-01 — read-only canonical TaskStore reconciliation.

Reconciles PROPOSED intake slices (e.g. GitHub Issue #18) against the canonical
harness TaskStore WITHOUT writing anything. For each slice it runs a multi-term
absence search over the existing rows and classifies it ADOPT / ADAPT / ADD /
GAP_UNSUPPORTED, then proves 1:1 board parity and verifies zero cycles / zero
deadlocks through the *incumbent* topology checker (`tasks_board.topo_order`,
Kahn 1962) — reused, not reinvented.

Single-writer invariant (the reason this is a module and not a CLI verb): this
is INPUT EVIDENCE, never a writer. `WRITES is False`; it imports no store
mutator, and the crosswalk is an advisory *intent* only — nothing here
transitions a task to done/closed, so causal monotonicity is preserved by
construction. The owner/auditor land any resulting `tasks` mutations through the
single write path (`scripts/harness.py tasks ...`).

Self-check: `python scripts/harness_lib/backlog_align.py`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import tasks_board  # noqa: E402

WRITES = False  # structural single-writer proof: this module never mutates the store
# The dep kinds the native `tasks dep` / topo checker can represent. Anything else
# a slice asks for is recorded GAP_UNSUPPORTED rather than forced into an invalid edge.
SUPPORTED_DEP_KINDS = ("dependsOn", "blocks", "relatesTo", "duplicates")
CLASSES = ("ADOPT", "ADAPT", "ADD", "GAP_UNSUPPORTED")
_ADAPT_MIN_OVERLAP = 2  # >=2 shared >=3-char tokens before a slice ADAPTs an existing row
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(*parts: Any) -> set[str]:
    """Lowercased alphanumeric tokens (len>=3) — the absence-search keyspace.
    None/empty parts contribute nothing (boundary: an empty slice yields set())."""
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        out.update(t for t in _TOKEN.findall(str(part).lower()) if len(t) >= 3)
    return out


def _index(store_rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    """id -> its search tokens (id + title). Deterministic, read-only."""
    idx: dict[str, set[str]] = {}
    for row in store_rows or []:
        rid = row.get("id")
        if not rid:  # never-crash collector idiom: a malformed row is skipped, not fatal
            continue
        idx[rid] = _tokens(rid, row.get("title"))
    return idx


def classify(slice_: dict[str, Any], index: dict[str, set[str]],
             *, supported: tuple[str, ...] = SUPPORTED_DEP_KINDS) -> dict[str, Any]:
    """Classify ONE intake slice against the store index (pure, deterministic).

    ADOPT   an existing id matches the slice key exactly;
    ADAPT   no id match but >= _ADAPT_MIN_OVERLAP shared tokens with a row (mapped);
    ADD     no match anywhere (multi-term absence proven);
    GAP_UNSUPPORTED  the slice asks for a dep kind the checker can't encode — this
                     OVERRIDES the match verb, because the slice cannot be fully
                     aligned. ponytail: one label per slice; split match-vs-edge
                     axes only if a slice ever needs both reported at once.
    """
    key = str(slice_.get("key") or "").strip()
    if not key:
        raise ValueError("intake slice missing non-empty 'key'")
    unsupported = sorted({str(d.get("kind")) for d in slice_.get("deps") or []
                          if d.get("kind") not in supported})
    if key in index:
        cls, matched = "ADOPT", key
    else:
        terms = _tokens(key, slice_.get("title"), *(slice_.get("keywords") or []))
        # best row by shared-token count; ties broken by sorted id (determinism).
        best_id, best_n = None, 0
        for rid in sorted(index):
            n = len(terms & index[rid])
            if n > best_n:
                best_id, best_n = rid, n
        if best_n >= _ADAPT_MIN_OVERLAP:
            cls, matched = "ADAPT", best_id
        else:
            cls, matched = "ADD", None
    if unsupported:
        cls = "GAP_UNSUPPORTED"
    return {"key": key, "classification": cls, "matched": matched,
            "unsupportedDeps": unsupported}


def verify_topology(store_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Zero-cycles / zero-deadlocks verdict via the incumbent Kahn checker.

    A deadlock IS a dependency cycle here (mutually-blocked ids never reach
    indegree 0). Fail-closed reading: `ok` is True ONLY when no cycle group and
    every id is placed in the topological order."""
    topo = tasks_board.topo_order(store_rows or [])
    cycles = topo.get("cycles") or []
    order = topo.get("order") or []
    placed = len(order) == len({r.get("id") for r in (store_rows or []) if r.get("id")})
    return {"cycle_detected": bool(cycles), "cycles": cycles,
            "deadlocks": cycles, "order": order,
            "ok": not cycles and placed}


def board_parity(store_ids: Any, board_ids: Any) -> dict[str, Any]:
    """1:1 projection parity between the canonical store and its board view."""
    a, b = set(store_ids), set(board_ids)
    return {"parity": a == b, "missing": sorted(a - b), "extra": sorted(b - a)}


def reconcile(store_rows: list[dict[str, Any]], proposed: list[dict[str, Any]],
              *, board_rows: list[dict[str, Any]] | None = None,
              supported: tuple[str, ...] = SUPPORTED_DEP_KINDS,
              strict: bool = False) -> dict[str, Any]:
    """Deterministic, idempotent (f(x) == f(f(x))) read-only reconciliation.

    Returns the crosswalk (sorted by slice key), per-class counts, the topology
    verdict, and board parity. `strict=True` FAILS CLOSED (raises) on a detected
    cycle — encoding new dependencies must never introduce a deadlock."""
    index = _index(store_rows)
    seen: set[str] = set()
    crosswalk: list[dict[str, Any]] = []
    for slice_ in sorted(proposed or [], key=lambda s: str(s.get("key") or "")):
        row = classify(slice_, index, supported=supported)
        if row["key"] in seen:  # duplicate intake key: report, never let the last win silently
            raise ValueError(f"duplicate intake slice key: {row['key']}")
        seen.add(row["key"])
        crosswalk.append(row)
    counts = {c: sum(1 for r in crosswalk if r["classification"] == c) for c in CLASSES}
    topo = verify_topology(store_rows)
    if strict and topo["cycle_detected"]:
        raise ValueError(f"topology check failed — cycle/deadlock: {topo['cycles']}")
    store_ids = [r.get("id") for r in (store_rows or []) if r.get("id")]
    parity = board_parity(store_ids,
                          [r.get("id") for r in board_rows] if board_rows is not None
                          else store_ids)
    return {"writes": WRITES, "crosswalk": crosswalk, "counts": counts,
            "topology": topo, "parity": parity,
            "ok": topo["ok"] and parity["parity"]}


def _demo() -> None:
    store = [
        {"id": "assurance-topology", "title": "Assurance topology checker"},
        {"id": "trusted-judge", "title": "Trusted judge and provenance gate"},
        {"id": "a", "title": "node a", "deps": {"dependsOn": ["b"]}},
        {"id": "b", "title": "node b"},
    ]
    proposed = [
        {"key": "trusted-judge"},                                   # ADOPT (exact id)
        {"key": "assurance-topology-checker",                       # ADAPT (token overlap)
         "title": "assurance topology checker POC"},
        {"key": "memory-evolution", "title": "adaptive memory evolution"},  # ADD
        {"key": "cross-repo-edge", "title": "x",                    # GAP_UNSUPPORTED
         "deps": [{"kind": "conditional-branch"}]},
    ]
    out = reconcile(store, proposed)
    got = {r["key"]: r["classification"] for r in out["crosswalk"]}
    assert got["trusted-judge"] == "ADOPT", got
    assert got["assurance-topology-checker"] == "ADAPT", got
    assert got["memory-evolution"] == "ADD", got
    assert got["cross-repo-edge"] == "GAP_UNSUPPORTED", got
    assert out["counts"] == {"ADOPT": 1, "ADAPT": 1, "ADD": 1, "GAP_UNSUPPORTED": 1}, out
    # zero cycles on an acyclic store, board parity holds, and it never writes.
    assert out["topology"]["cycle_detected"] is False and out["topology"]["ok"], out
    assert out["parity"]["parity"] and out["writes"] is False, out
    assert out["ok"], out

    # idempotency: f(x) == f(f(x)) for the deterministic crosswalk + counts.
    out2 = reconcile(store, proposed)
    assert out2["crosswalk"] == out["crosswalk"] and out2["counts"] == out["counts"]

    # falsifier: a mutual dependency is a hard-detected deadlock, and strict fails closed.
    cyclic = [{"id": "x", "deps": {"dependsOn": ["y"]}},
              {"id": "y", "deps": {"dependsOn": ["x"]}}]
    v = verify_topology(cyclic)
    assert v["cycle_detected"] and not v["ok"], v
    try:
        reconcile(cyclic, [], strict=True)
        raise AssertionError("strict reconcile accepted a cycle")
    except ValueError:
        pass

    # falsifier: an empty key and a duplicate key both fail closed.
    for bad in ([{"key": ""}], [{"key": "d"}, {"key": "d"}]):
        try:
            reconcile(store, bad)
            raise AssertionError(f"accepted bad intake: {bad}")
        except ValueError:
            pass
    print("backlog_align self-check: ok")


if __name__ == "__main__":
    _demo()
