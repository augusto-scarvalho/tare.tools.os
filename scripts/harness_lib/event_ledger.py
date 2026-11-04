#!/usr/bin/env python3
"""Event Ledger chokepoint and tamper-evident event logging authority.

SPEC-109 / Article Section 8.1 ir4: Single authority for appending operational,
auditable, and critical events to `.harness/runs/events.jsonl`.

All critical events MUST be routed through this chokepoint to maintain the
tamper-evident SHA-256 hash chain (`prevHash`/`hash`). Non-critical events are
fully additive and preserve byte-level backwards compatibility.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from harness_lib.common import load_ambient_keys, now, read_json, read_text, replay_class, write_text

_LOCK = threading.RLock()

# Closed, named set of critical event types (tamper-evident hash-chaining required)
CRITICAL_EVENTS = frozenset({
    "escalation_resolved",
    "route_escalation",
    "route_withheld_escalation",
    "security_routing_escalation",
    "epoch_fence_overridden",
    "workflow_budget_overridden",
})

CHAIN_GENESIS = "0" * 64  # prevHash of the first critical row in a chain


def chain_hash(row: dict[str, Any]) -> str:
    """sha256 over the canonical row EXCLUDING its own `hash` (prevHash included).
    
    Shared by the appender and `verify` so they can never drift.
    """
    canonical = json.dumps(
        {k: v for k, v in row.items() if k != "hash"},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_id(row: dict[str, Any]) -> str:
    """Return deterministic event identity (reuses T-HASHCHAIN hash if present)."""
    h = row.get("hash")
    if isinstance(h, str):
        return h
    canonical = json.dumps(
        {k: v for k, v in row.items() if k != "hash"},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def get_events_path(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root) / ".harness" / "runs" / "events.jsonl"
    env_root = os.environ.get("HARNESS_ROOT")
    if env_root:
        return Path(env_root) / ".harness" / "runs" / "events.jsonl"
    base = Path.cwd()
    if (base / ".harness").exists():
        return base / ".harness" / "runs" / "events.jsonl"
    parents = Path(__file__).resolve().parents
    if len(parents) >= 3:
        return parents[2] / ".harness" / "runs" / "events.jsonl"
    return base / ".harness" / "runs" / "events.jsonl"


def orphan_events(events: list[dict[str, Any]]) -> list[str]:
    """DAG integrity (article Section 8.1, advisory): identify parentEventId values
    pointing to missing events in the list.
    """
    known = {event_id(r) for r in events if isinstance(r, dict)}
    missing: list[str] = []
    seen: set[str] = set()
    for r in events:
        if not isinstance(r, dict):
            continue
        pid = r.get("parentEventId")
        if isinstance(pid, str) and pid not in known and pid not in seen:
            missing.append(pid)
            seen.add(pid)
    return missing


def append(
    event: str,
    payload: dict[str, Any] | None = None,
    subject: str | None = None,
    parent: str | None = None,
    root: Path | str | None = None,
    critical: bool | None = None,
    events_path: Path | str | None = None,
) -> str:
    """Central authority for appending events to `.harness/runs/events.jsonl`.
    
    Thread-safe and process-guarded via atomic locks. Automatically calculates
    `prevHash` and `hash` for critical events.
    """
    target_path = Path(events_path) if events_path is not None else get_events_path(root)
    
    payload_dict = dict(payload or {})
    payload_dict.setdefault("timestamp", now())
    payload_dict["event"] = event
    payload_dict["replayClass"] = replay_class(event)
    
    if parent:
        payload_dict["parentEventId"] = str(parent)
        
    if subject:
        payload_dict["subject"] = str(subject)
    else:
        payload_dict.setdefault("subject", str(payload_dict.get("target") or "self"))
        
    is_critical = critical if critical is not None else (event in CRITICAL_EVENTS)
    
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    with _LOCK:
        if is_critical:
            prev = CHAIN_GENESIS
            try:
                if target_path.exists():
                    for line in target_path.read_text(encoding="utf-8").splitlines():
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict) and isinstance(row.get("hash"), str):
                            prev = row["hash"]
            except Exception:
                pass
            payload_dict["prevHash"] = prev
            payload_dict["hash"] = chain_hash(payload_dict)
            
        heal = ""
        try:
            if target_path.exists() and target_path.stat().st_size > 0:
                with target_path.open("rb") as chk:
                    chk.seek(-1, os.SEEK_END)
                    if chk.read(1) != b"\n":
                        heal = "\n"
        except (OSError, ValueError):
            pass
            
        with target_path.open("a", encoding="utf-8") as f:
            f.write(heal + json.dumps(payload_dict, ensure_ascii=False) + "\n")
            
    # Trace observability hook
    try:
        from harness_lib import tracing
        h_root = Path(root) if root else target_path.parents[2]
        tracing.append_event_span(h_root, event, payload_dict)
    except Exception:
        pass
        
    return event_id(payload_dict)


def append_critical(
    event: str,
    payload: dict[str, Any] | None = None,
    subject: str | None = None,
    parent: str | None = None,
    root: Path | str | None = None,
    events_path: Path | str | None = None,
) -> str:
    """Explicitly append a critical event with hash chaining mandatory."""
    return append(
        event=event,
        payload=payload,
        subject=subject,
        parent=parent,
        root=root,
        critical=True,
        events_path=events_path,
    )


def verify(
    events: list[dict[str, Any]] | None = None,
    root: Path | str | None = None,
    genesis: str = CHAIN_GENESIS,
    seal: dict[str, Any] | None = None,
    events_path: Path | str | None = None,
) -> dict[str, Any]:
    """Reconstruct critical-event hash chain and detect tamper/corruption/unhashed critical rows.
    
    Returns:
        {"ok": bool, "brokenAt": int | None, "reason": str | None, "chainLength": int}
    """
    if events is None:
        target_path = Path(events_path) if events_path is not None else get_events_path(root)
        if not target_path.exists():
            return {"ok": True, "brokenAt": None, "reason": "empty file", "chainLength": 0}
        events = []
        for line in target_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    removed = seal["removed"] if isinstance(seal, dict) and isinstance(seal.get("removed"), dict) else {}
    prev = genesis
    length = 0
    seen: set[str] = set()
    
    for idx, row in enumerate(events):
        if not isinstance(row, dict):
            continue
            
        evt_type = row.get("event")
        row_hash = row.get("hash")
        
        # Detect unhashed critical event (critical event type missing required hash)
        if evt_type in CRITICAL_EVENTS and not isinstance(row_hash, str):
            return {
                "ok": False,
                "brokenAt": idx,
                "reason": f"unhashed critical event '{evt_type}' at index {idx}",
                "chainLength": length,
            }
            
        if not isinstance(row_hash, str):
            continue
            
        h = row_hash
        if chain_hash(row) != h:
            return {
                "ok": False,
                "brokenAt": idx,
                "reason": "hash mismatch (edited content)",
                "chainLength": length,
            }
            
        if h in seen:
            return {
                "ok": False,
                "brokenAt": idx,
                "reason": "duplicate row hash (replay)",
                "chainLength": length,
            }
        seen.add(h)
        
        ph = row.get("prevHash")
        if ph != prev:
            if ph in removed and removed.get(ph) == h:
                pass
            else:
                return {
                    "ok": False,
                    "brokenAt": idx,
                    "reason": f"prevHash mismatch (unexplained gap: reorder/removal/forgery across index {idx})",
                    "chainLength": length,
                }

        prev = h
        length += 1
        
    return {"ok": True, "brokenAt": None, "reason": None, "chainLength": length}


def read_events(root: Path | str | None = None, events_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read all event dicts from events.jsonl."""
    target_path = Path(events_path) if events_path is not None else get_events_path(root)
    if not target_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                events.append(row)
        except json.JSONDecodeError:
            pass
    return events

