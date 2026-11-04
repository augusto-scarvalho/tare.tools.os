"""Pending-escalation aggregation for the escalations CLI (extracted from harness.py, MF.1).

Shared harness helpers/constants arrive via bind(env) at import time —
the same idiom as harness_lib.workflow_reduce and the gate_fixtures modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from harness_lib.common import now  # SPEC-164 M5: bind-free timestamp for chainSeal.sealedAt


def bind(env: dict[str, Any]) -> None:
    """Receive shared runtime helpers/constants from scripts/harness.py."""
    globals().update(env)


_DURABLE_GENESIS = "0" * 64  # prevStateHash of the first durable write
_LIVE_GENESIS = "0" * 64  # mirrors harness._CHAIN_GENESIS (verify_event_chain's default start)


def _durable_state_hash(doc: dict[str, Any]) -> str:
    """sha256 over the canonical durable ledger EXCLUDING its own `stateHash`
    (prevStateHash IS included — it is the chain link). SPEC-164 v2 self-chain of
    escalations.json: each write's stateHash covers the seal + records + the prior
    stateHash, so a silent content edit breaks it. Same no-key ceiling as the live
    chain (the writer can recompute it), declared not hidden."""
    canonical = json.dumps({k: v for k, v in doc.items() if k != "stateHash"},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_durable_ledger(doc: dict[str, Any]) -> bool:
    """True iff the ledger's stored stateHash matches its content (no naive edit).
    Absent stateHash (a pre-v2 ledger) -> True: nothing to verify, back-compat."""
    stored = doc.get("stateHash")
    if not isinstance(stored, str):
        return True
    return _durable_state_hash(doc) == stored


def compact_supervision_events(events_path: Path, state_path: Path) -> None:
    """Fold supervision events into the durable state file and strip them from the log.

    SPEC-109: events.jsonl is transient (gates wipe it; release hygiene forbids
    .jsonl in the tree), but escalation decisions must survive — so raises and
    resolves live in .harness/state/escalations.json (class B: untracked,
    mirrored to the private state home). Pure/path-parameterized so the gate's
    cleanup can call it without the CLI's bind environment.
    """
    if not events_path.exists():
        return
    state = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    raised: dict[str, Any] = dict(state.get("raised", {}))
    resolved: set[str] = set(state.get("resolvedIds", []))
    resolved_records: dict[str, Any] = dict(state.get("resolvedRecords", {}))
    friction_log: list[dict[str, Any]] = list(state.get("frictionLog", []))
    kept_lines: list[str] = []
    changed = False
    removed_hashes: list[str] = []  # SPEC-164 v2 C+: hashes of folded critical rows, in order
    for line in events_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue
        kind = evt.get("event")
        if kind == "escalation_resolved" and evt.get("escalationId"):
            esc_id = str(evt["escalationId"])
            resolved.add(esc_id)
            record = raised.pop(esc_id, None)
            # I6 loop KPI: keep raise->resolve timestamps so triage latency is measurable.
            resolved_record = {**(record or {"escalationId": esc_id}), "resolvedAt": evt.get("timestamp")}
            # SPEC-161 N-AUTHCHAIN: propagate the typed accepter into the durable record
            # when the resolve event carries one (legacy events without it stay untouched).
            if evt.get("actor"):
                resolved_record["actor"] = evt["actor"]
            # B4 decision-provenance (SPEC-164 v2, anti-T1.5): capture the causal parent
            # of the resolve so a later human/retrospective audit can trace WHAT led to
            # the decision. Content-authenticity ("was this ratification legitimate, not
            # authentic garbage from a poisoned input?") is an audit property, not a
            # crypto one — the chain authenticates form; this gives the trail to review.
            if evt.get("parentEventId"):
                resolved_record["parentEventId"] = evt["parentEventId"]
            resolved_records[esc_id] = resolved_record
            changed = True
        elif kind == "harness_result_validated" and (evt.get("requiresEscalation") or evt.get("frictionObservations")):
            if evt.get("requiresEscalation"):
                esc_id = f"{evt.get('taskId') or 'unknown'}@{evt.get('timestamp')}"
                raised[esc_id] = {
                    "escalationId": esc_id, "taskId": evt.get("taskId"),
                    "suggestedProfile": evt.get("suggestedProfile"), "reason": evt.get("reason"),
                    "failureClass": evt.get("failureClass"), "raisedAt": evt.get("timestamp"),
                    "subject": evt.get("target") or "self",  # esc-subject-scope D1
                    "source": "harness-result",
                }
            # SE.4: friction observations are durable telemetry for the
            # self-review recurrence rule (friction x2 = missing tool).
            for obs in evt.get("frictionObservations", []) or []:
                friction_log.append({"observation": str(obs)[:200], "at": evt.get("timestamp")})
            changed = True
        elif kind == "security_routing_escalation" and evt.get("escalationId"):
            # security-diff-routing (owner decision 2026-07-13 #1): a routed hit is a
            # durable security-triage escalation. Id = hash of the finding set, so
            # re-runs are idempotent; a RESOLVED id is never re-raised — the owner's
            # decision stands until the finding set changes.
            esc_id = str(evt["escalationId"])
            if esc_id not in resolved:
                raised[esc_id] = {
                    "escalationId": esc_id, "taskId": esc_id,
                    "suggestedProfile": "security-triage", "reason": evt.get("reason"),
                    "failureClass": "security-baseline", "findings": evt.get("findings"),
                    "raisedAt": evt.get("timestamp"),
                    "subject": "self",  # the routing gate scans the harness tree
                    "source": "security-routing",
                }
            changed = True
        elif kind == "audit_iteration_escalation" and evt.get("escalationId"):
            # audit-iteration-closure: K consecutive audit BLOCKs on ONE iteration is
            # an owner call (accept the risk, revert, or redesign), never a lane's.
            # Id = the streak-head fingerprint, so every cycle of the same iteration
            # folds into one row; a RESOLVED id is never re-raised — the owner's
            # decision stands until a ship/waive closes the streak and starts a new one.
            esc_id = str(evt["escalationId"])
            if esc_id not in resolved:
                raised[esc_id] = {
                    "escalationId": esc_id, "taskId": esc_id,
                    "suggestedProfile": "review", "reason": evt.get("reason"),
                    "failureClass": "audit-iteration", "raisedAt": evt.get("timestamp"),
                    "subject": evt.get("subject") or "self", "source": "audit-iteration",
                }
            changed = True
        elif kind == "route_withheld_escalation" and evt.get("escalationId"):
            # SPEC-144 (porteiro incident 2026-07-17): a risk-flagged route is
            # WITHHELD at classify time; persist it so `escalations`/`decide`
            # agree with the printed decision. Id = hash of the demand, so
            # re-classifying the same demand never duplicates, and a RESOLVED
            # id is never re-raised — the owner's decision stands.
            esc_id = str(evt["escalationId"])
            if esc_id not in resolved:
                raised[esc_id] = {
                    "escalationId": esc_id, "taskId": esc_id,
                    "suggestedProfile": evt.get("workflowProfile") or "route-triage",
                    "reason": evt.get("reason"), "failureClass": "route-withheld",
                    "demand": evt.get("demand"), "raisedAt": evt.get("timestamp"),
                    "subject": "self", "source": "route-withheld",
                }
            changed = True
        elif kind == "self_review_finding" and evt.get("escalationId"):
            esc_id = str(evt["escalationId"])
            raised[esc_id] = {
                "escalationId": esc_id, "taskId": esc_id,
                "suggestedProfile": evt.get("suggestedProfile", "plan"), "reason": evt.get("reason"),
                "failureClass": None, "raisedAt": evt.get("timestamp"),
                "criticality": evt.get("criticality"),  # SPEC-111 R28 tier
                "subject": evt.get("subject") or "self",  # esc-subject-scope D1
                "source": "self-review",
            }
            changed = True
        elif kind and str(kind).endswith("_escalation") and evt.get("escalationId"):
            # CE.8-lite durable-raise floor (ref-arch adoption round, claim C2): ANY
            # *_escalation event carrying an escalationId folds into the ledger, so a
            # new emitter is durable by construction — no per-kind branch required
            # (the route-withheld incident 2026-07-17 needed one; the next one won't).
            # Specific branches above keep their richer mappings; this is the floor.
            esc_id = str(evt["escalationId"])
            if esc_id not in resolved:
                raised[esc_id] = {
                    "escalationId": esc_id, "taskId": evt.get("taskId") or esc_id,
                    "suggestedProfile": evt.get("suggestedProfile") or "triage",
                    "reason": evt.get("reason"),
                    "failureClass": evt.get("failureClass") or str(kind),
                    "raisedAt": evt.get("timestamp"),
                    "subject": evt.get("subject") or evt.get("target") or "self",
                    "source": str(kind),
                }
            changed = True
        else:
            kept_lines.append(line)
            continue
        # SPEC-164 M5/v2: reached only by a FOLDED row (kept rows `continue` above).
        # A folded critical row carrying a hash is a link removed from the live
        # chain — collect it so the C+ seal below can bind each removed hash to the
        # kept successor that legitimately links across it.
        h = evt.get("hash")
        if isinstance(h, str):
            removed_hashes.append(h)
    if not changed:
        return
    out: dict[str, Any] = {
        "schemaVersion": "1.0",
        "purpose": "Durable supervision ledger (SPEC-109): escalation raises/resolves and friction observations compacted out of the transient events.jsonl.",
        "raised": raised,
        "resolvedIds": sorted(resolved),
        "resolvedRecords": resolved_records,
        "frictionLog": friction_log[-200:],
    }
    ts = now()
    # SPEC-164 v2 C+ seal: successor-BOUND gap tolerance. Rebuild the binding map
    # {removed_hash: successor_hash} by walking the CURRENT live log positionally — a
    # kept hashed row whose prevHash != the running previous kept hash is a gap that
    # must be bound to its successor. Rebuilding from the FULL live log (not just this
    # round's removals) captures gaps left by PRIOR compaction rounds whose kept row is
    # STILL LIVE (the two-round false-WARN the 2026-07-21 reckon caught, where a
    # wholesale seal-overwrite dropped round 1's binding) AND self-prunes bindings whose
    # successor was itself folded later — bounded, never unbounded merge nor overwrite.
    prior = state.get("chainSeal") if isinstance(state.get("chainSeal"), dict) else {}
    if removed_hashes or prior:
        bindings: dict[str, str] = {}
        running = _LIVE_GENESIS
        for line in kept_lines:
            try:
                krow = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(krow, dict) and isinstance(krow.get("hash"), str):
                ph = krow.get("prevHash")
                if ph != running and isinstance(ph, str):
                    bindings[ph] = krow["hash"]
                running = krow["hash"]
        out["chainSeal"] = {
            "lastHash": removed_hashes[-1] if removed_hashes else prior.get("lastHash"),
            "sealedAt": ts,
            "count": int(prior.get("count", 0) or 0) + len(removed_hashes),  # cumulative removed evidence
            "removed": bindings,
        }
    # Durable self-chain (L1): stateHash covers the whole doc EXCEPT itself;
    # prevStateHash IS covered (it is the link). A silent content edit breaks
    # stateHash (verify_durable_ledger); a careless whole-file rollback to an older
    # valid ledger is caught by the cross-witness below.
    out["prevStateHash"] = state.get("stateHash") or _DURABLE_GENESIS
    out["stateHash"] = _durable_state_hash(out)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")
    # Cross-witness (live<->durable, anti-rollback): append the new durable head to
    # the live log so a later doctor catches a careless whole-file rollback of the
    # ledger. The live log is transient (gate-wiped), so the window is bounded --
    # declared, not hidden. Non-critical row (no `hash`) -> never perturbs the C+
    # live chain it witnesses.
    kept_lines.append(json.dumps(
        {"event": "supervision_compacted", "durableStateHash": out["stateHash"],
         "sealCount": len(removed_hashes), "timestamp": ts}, ensure_ascii=False))
    events_path.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8", newline="\n")


def list_escalations(subject: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Pending escalations from the durable ledger plus finalized workflow results (SPEC-102).

    The transient events.jsonl is compacted into `.harness/state/escalations.json`
    on every read, so escalation raises/resolves survive gate cleanups and
    release-hygiene keeps its no-.jsonl-in-tree rule (SPEC-109).

    esc-subject-scope D2: every pending row carries `subject` ("self" or a
    target name; records without the field read as "self" — no migration).
    `subject=` filters pending to one world; the unfiltered payload always
    carries `bySubject` counts so a mixed queue is at least visible.
    `root=` overrides the bound harness paths (dual-subject scenario support).
    """
    base = Path(root) if root else None
    state_path = (base or ROOT) / ".harness" / "state" / "escalations.json"
    events_path = (base / ".harness" / "runs" / "events.jsonl") if base else EVENTS
    wf_dir = (base / ".harness" / "workflows" / "active") if base else ACTIVE_WORKFLOWS_DIR
    compact_supervision_events(events_path, state_path)
    state = json_read(state_path)
    resolved: set[str] = set(state.get("resolvedIds", []))
    seen: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (state.get("raised", {}) or {}).items()}
    for wf_result in sorted(wf_dir.glob("WF-*/reduce/harness-result.json")):
        data = json_read(wf_result)
        if not data.get("requiresEscalation"):
            continue
        esc_id = f"{data.get('taskId')}@workflow"
        seen[esc_id] = {
            "escalationId": esc_id,
            "taskId": data.get("taskId"),
            "suggestedProfile": data.get("suggestedProfile"),
            "reason": data.get("reason"),
            "failureClass": data.get("failureClass"),
            "raisedAt": data.get("generatedAt"),
            "subject": data.get("target") or "self",  # G3: attributed at the fold
            "source": str(wf_result.relative_to(base or ROOT)),
        }
    pending = [item for item in seen.values() if item["escalationId"] not in resolved]
    for item in pending:
        item["subject"] = item.get("subject") or "self"  # legacy records read as self
    by_subject: dict[str, int] = {}
    for item in pending:
        by_subject[item["subject"]] = by_subject.get(item["subject"], 0) + 1
    if subject is not None:
        pending = [item for item in pending if item["subject"] == subject]
    by_class: dict[str, list[str]] = {}
    for item in pending:
        by_class.setdefault(str(item.get("failureClass") or "unclassified"), []).append(item["escalationId"])
    return {"pending": pending, "byFailureClass": by_class, "count": len(pending),
            "bySubject": by_subject, "subjectFilter": subject,
            "resolvedIds": sorted(resolved)}


def json_read(path: Path) -> dict[str, Any]:
    """Bind-free JSON read ({} default) so root-parameterized calls work in
    scenarios without the harness.py bind environment."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def scoped_resolve_check(pending: list[dict[str, Any]], esc_id: str,
                         subject: str | None) -> dict[str, Any]:
    """The resolve guard (D2): return the pending record for `esc_id`, or raise
    ValueError with a legible message — unknown/not-pending id, or a subject
    mismatch when a scope was requested (no cross-subject resolve)."""
    by_id = {item["escalationId"]: item for item in pending}
    record = by_id.get(esc_id)
    if record is None:
        raise ValueError(
            f"unknown escalation id {esc_id}\n"
            "cause: it is not pending (already resolved, or never raised)\n"
            f"fix: pick one of: {', '.join(by_id) if by_id else '(none pending)'}")
    actual = record.get("subject") or "self"
    if subject is not None and actual != subject:
        raise ValueError(
            f"escalation {esc_id} belongs to subject '{actual}', not '{subject}'\n"
            "cause: cross-subject resolve is refused (each subject keeps its world)\n"
            f"fix: re-run with --target {actual}, or without --target for an unscoped resolve")
    return record
