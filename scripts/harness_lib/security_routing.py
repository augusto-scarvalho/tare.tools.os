#!/usr/bin/env python3
"""security-diff-routing — ROUTE, don't block (owner decision 2026-07-13 #1).

`route(root, hits)` is THE named entry point between the security-regression
join (`spec_test_gate.ratchet_hits`) and whatever consumes a confirmed hit —
today one durable escalation suggesting the `security-triage` profile. It is a
single function on purpose: a future security-verification PIPELINE replaces
the consumer here without touching the gate seam.

Mandatory companion (same decision): the known-false-positive exemption
registry at `.harness/state/security-exemptions.json` — `{pattern, reason,
reviewedAt}` entries fnmatch'd against finding ids — so a known FP never
re-spends triage tokens/time. An entry missing any field is INVALID: it is
reported (the gate fails on it) and never applied; a silent or underspecified
exemption would be prose, not control.

Escalations stay single-writer: route() appends a `security_routing_escalation`
event to `.harness/runs/events.jsonl` and immediately compacts it into the
durable ledger via `escalations_lib.compact_supervision_events` (SPEC-109 —
raises must survive gate wipes). The escalation id is a stable hash of the
routed finding set: re-runs with the same hits are idempotent, and a RESOLVED
id is never re-raised — the owner's decision stands until the finding set
changes (add an exemption or re-baseline to retire the finding itself).

Self-check: `python scripts/harness_lib/security_routing.py`.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import escalations_lib  # noqa: E402  (compact_supervision_events is pure)
from harness_lib.common import replay_class  # noqa: E402  (L3: direct writers stamp too)

REGISTRY_REL = ".harness/state/security-exemptions.json"
_REQUIRED_FIELDS = ("pattern", "reason", "reviewedAt")


def load_exemptions(root: Path | str) -> tuple[list[dict], list[str]]:
    """(valid entries, invalid-entry descriptions). A missing/unparseable
    registry means no exemptions — absence can only widen routing, never
    silence it."""
    path = Path(root) / REGISTRY_REL
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("exemptions", [])
    except (OSError, json.JSONDecodeError):
        return [], []
    valid: list[dict] = []
    invalid: list[str] = []
    for i, entry in enumerate(entries):
        missing = [f for f in _REQUIRED_FIELDS
                   if not (isinstance(entry, dict) and str(entry.get(f) or "").strip())]
        if missing:
            invalid.append(f"exemptions[{i}]: missing {'/'.join(missing)}")
        else:
            valid.append(entry)
    return valid, invalid


def route(root: Path | str, hits: list[str]) -> dict:
    """Named entry point (future pipeline slot). Splits `hits` against the FP
    registry and raises ONE durable security-triage escalation for what
    remains. Returns {"routed", "exempted", "invalid", "escalationId"}."""
    root = Path(root)
    valid, invalid = load_exemptions(root)
    exempted = sorted({h for h in hits
                       if any(fnmatch.fnmatchcase(h, e["pattern"]) for e in valid)})
    routed = sorted(set(hits) - set(exempted))
    esc_id = None
    if routed:
        digest = hashlib.sha1("\n".join(routed).encode("utf-8")).hexdigest()[:12]
        esc_id = f"security-routing:{digest}"
        events = root / ".harness" / "runs" / "events.jsonl"
        state = root / ".harness" / "state" / "escalations.json"
        evt = {
            "escalationId": esc_id,
            "findings": routed,
            "reason": (f"{len(routed)} new security finding(s) in changed files: "
                       + ", ".join(routed))[:400],
        }
        from harness_lib import event_ledger
        event_ledger.append_critical("security_routing_escalation", evt, root=root)
        escalations_lib.compact_supervision_events(events, state)

    return {"routed": routed, "exempted": exempted, "invalid": invalid,
            "escalationId": esc_id}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reg = root / REGISTRY_REL
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"exemptions": [
            {"pattern": "sink:shell-true:scripts/legacy/*", "reason": "reviewed FP",
             "reviewedAt": "2026-07-13"},
            {"pattern": "sink:eval:scripts/x.py"},  # invalid: missing reason/reviewedAt
        ]}), encoding="utf-8")
        out = route(root, ["sink:shell-true:scripts/legacy/old.py",
                           "secret:aws:scripts/new.py"])
        assert out["exempted"] == ["sink:shell-true:scripts/legacy/old.py"], out
        assert out["routed"] == ["secret:aws:scripts/new.py"], out
        assert out["invalid"] == ["exemptions[1]: missing reason/reviewedAt"], out
        state = json.loads((root / ".harness/state/escalations.json").read_text(encoding="utf-8"))
        raised = state["raised"][out["escalationId"]]
        assert raised["suggestedProfile"] == "security-triage", raised
        # Idempotent: same hits, same id, still one raised entry.
        again = route(root, ["secret:aws:scripts/new.py"])
        assert again["escalationId"] == out["escalationId"], again
        # No hits, no escalation.
        assert route(root, [])["escalationId"] is None
    print("security_routing self-check: ok")


if __name__ == "__main__":
    _demo()
