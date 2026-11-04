"""PROCESS-REPAIR-01 — Phase-T Bootstrap Authority Composition v1.

Single bounded Owner envelope with monotonic deterministic child
derivation. A root envelope is authorized ONCE by the owner; every child
authority OID is derived by hash-chaining (parent_oid, seq) — no manual
freeze and no OID re-registration per delegation. Spec:
tare-tools-relay/temporary-evidence/PROCESS_REPAIR_01_PACKET.md
"""
from __future__ import annotations

import hashlib
import json
import time

SCHEMA = "tare.tools/bootstrap-envelope/1.0"


def _oid(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def mint_root(owner_id: str, scope: list[str], max_children: int = 64,
              ttl_seconds: int = 24 * 3600, issued_at: float | None = None) -> dict:
    """Mint the single bounded Phase-T root envelope (owner action)."""
    if not owner_id or not scope:
        raise ValueError("owner_id and non-empty scope are required")
    issued_at = time.time() if issued_at is None else float(issued_at)
    env = {
        "schema": SCHEMA,
        "kind": "root",
        "owner_id": owner_id,
        "scope": sorted(scope),
        "max_children": int(max_children),
        "issued_at": issued_at,
        "expires_at": issued_at + int(ttl_seconds),
        "seq": 0,
    }
    env["oid"] = _oid(SCHEMA, owner_id, json.dumps(env["scope"]),
                      str(env["max_children"]), repr(issued_at))
    return env


def derive_child(parent: dict, task_id: str, now: float | None = None) -> dict:
    """Derive the next child envelope. Deterministic, monotonic, bounded."""
    now = time.time() if now is None else float(now)
    if parent.get("schema") != SCHEMA:
        raise ValueError("not a bootstrap envelope")
    if now >= parent["expires_at"]:
        raise ValueError("parent envelope expired (fail closed)")
    if task_id not in parent["scope"]:
        raise ValueError(f"task {task_id!r} outside envelope scope")
    seq = parent["seq"] + 1
    if seq > parent["max_children"]:
        raise ValueError("child budget exhausted (bounded envelope)")
    parent["seq"] = seq  # monotonic counter lives on the root
    return {
        "schema": SCHEMA,
        "kind": "child",
        "parent_oid": parent["oid"],
        "task_id": task_id,
        "seq": seq,
        "expires_at": parent["expires_at"],
        "oid": _oid(parent["oid"], str(seq), task_id),
    }


def verify_child(child: dict, root: dict) -> bool:
    """Recompute the chain: a child is valid iff its OID re-derives from the root."""
    return (
        child.get("schema") == SCHEMA
        and child.get("parent_oid") == root.get("oid")
        and 1 <= child.get("seq", 0) <= root.get("max_children", 0)
        and child.get("task_id") in root.get("scope", [])
        and child.get("oid") == _oid(root["oid"], str(child["seq"]), child["task_id"])
    )


if __name__ == "__main__":  # self-check
    t0 = 1_000_000.0
    root = mint_root("owner", ["TCP-01", "GRAPH-BACKLOG-02"], max_children=2, issued_at=t0)
    root2 = mint_root("owner", ["GRAPH-BACKLOG-02", "TCP-01"], max_children=2, issued_at=t0)
    assert root["oid"] == root2["oid"], "root derivation must be deterministic"
    c1 = derive_child(root, "TCP-01", now=t0 + 1)
    c2 = derive_child(root, "GRAPH-BACKLOG-02", now=t0 + 2)
    assert c1["oid"] != c2["oid"] and c1["seq"] == 1 and c2["seq"] == 2
    assert verify_child(c1, root) and verify_child(c2, root)
    for bad, err in [
        (lambda: derive_child(root, "TCP-01", now=t0 + 3), "budget"),
        (lambda: derive_child(root2, "NOT-IN-SCOPE", now=t0 + 1), "scope"),
        (lambda: derive_child(root2, "TCP-01", now=root2["expires_at"] + 1), "expired"),
    ]:
        try:
            bad()
            raise AssertionError(f"should have failed closed: {err}")
        except ValueError:
            pass
    forged = dict(c1, task_id="TCP-02")
    assert not verify_child(forged, root), "forged child must not verify"
    print("bootstrap_envelope self-check: OK")
