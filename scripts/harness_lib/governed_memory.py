#!/usr/bin/env python3
"""Governed-memory item registry + GM-3 provenance firewall (SPEC-162, measure-only).

The GM-3 firewall is the poisoning (OWASP) BLOCKER of all governed memory:
retrieved memory NEVER acquires authority >= the policy it feeds. But governed
retrieval does not exist in the code yet, so this module ships the committable
first step (the GM-5 bootstrap): the minimal typed governed-item RECORD carrying
an `authority` tier and `scopeTag` from birth, PLUS the pure firewall/scope
functions -- in **measure-only** mode. Nothing is enforced; `shadow_scan` only
COUNTS how many items a real retrieval WOULD refuse/challenge. Retrieval
enforcement is an owner-gated follow-up (trigger N6).

- Record (invariant 1): `{id, content, authority, scopeTag, provenance, state, at}`.
- `firewall_verdict` (invariant 2): pure, deterministic; memory with authority
  >= policy is refused; `revoked`/`expired` refused via a distinct state gate.
- `scope_challenged` (invariant 3): a scopeTag glob hitting a changed path marks
  the item a CANDIDATE for re-validation -- challenge != delete.
- `shadow_scan` (invariant 4): measure-before-enforce; never mutates, fail-open.

Authority tiers align with SPEC-161 actorType (a single actor vocabulary across
the harness); `signed_policy` outranks all live memory.

Self-check: python scripts/harness_lib/governed_memory.py.
"""
from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib.common import now  # noqa: E402

# Ordered low -> high; the index IS the rank. Aligned with SPEC-161 actorType,
# with `active_memory` as the lowest live-memory tier and `signed_policy` above all.
AUTHORITY_TIERS = ("untrusted", "active_memory", "worker", "overseer", "signed_policy")
STATES = ("candidate", "validated", "active", "challenged", "expired", "revoked")


def _rank(authority: Any) -> int:
    """Tier rank (index in AUTHORITY_TIERS). Unknown/unhashable -> -1, never raises."""
    try:
        return AUTHORITY_TIERS.index(authority)
    except (ValueError, TypeError):
        return -1


def make_item(item_id: str, content: Any, authority: str = "active_memory",
              scope_tag: Any = None, provenance: Any = None,
              state: str = "candidate") -> dict[str, Any]:
    """Build a governed-memory record with safe defaults (invariant 1).

    Out-of-vocabulary `authority`/`state` fall back to the safe default and NEVER
    raise -- a measure-only bootstrap must never break a caller. `scopeTag` is a
    list of path globs (a bare string is wrapped, not exploded); `provenance` is a
    SPEC-161 actor record OR a free `{source}` dict (default `{}`)."""
    if authority not in AUTHORITY_TIERS:
        authority = "active_memory"  # lowest live-memory tier: the safe default
    if state not in STATES:
        state = "candidate"
    if not scope_tag:
        scope = []
    elif isinstance(scope_tag, str):
        scope = [scope_tag]
    else:
        scope = list(scope_tag)
    return {"id": str(item_id), "content": content, "authority": authority,
            "scopeTag": scope, "provenance": provenance if provenance is not None else {},
            "state": state, "at": now()}


def firewall_verdict(item: dict[str, Any], policy_authority: str) -> tuple[bool, str]:
    """GM-3 firewall (invariant 2): (admit, reason), pure and deterministic.

    `revoked`/`expired` items are refused by a state gate first (distinct reason).
    An UNRECOGNIZED authority (item OR policy) FAILS CLOSED with a distinct
    `authority-invalid` reason -- an anti-poisoning gate must never treat a
    garbage/corrupted authority as the weakest known tier and wave it through
    (the 2026-07-21 reckon reproduced exactly that bypass at every policy tier).
    Otherwise admit iff `rank(item.authority) < rank(policy_authority)` -- retrieved
    memory NEVER acquires authority >= the policy it feeds. Assumes a well-formed
    dict item; `shadow_scan` is the fail-open aggregator that tolerates the rest."""
    state = item["state"]
    if state in ("revoked", "expired"):
        return (False, f"state-gate: {state}")
    item_authority = item["authority"]
    item_rank, policy_rank = _rank(item_authority), _rank(policy_authority)
    if item_rank < 0 or policy_rank < 0:  # unknown tier -> fail closed, NEVER admit
        return (False, f"authority-invalid: item={item_authority!r} policy={policy_authority!r} "
                       "(unrecognized tier refused)")
    if item_rank < policy_rank:
        return (True, f"admit: {item_authority}({item_rank}) < {policy_authority}({policy_rank})")
    return (False, f"authority-gate: {item_authority}({item_rank}) >= "
                   f"{policy_authority}({policy_rank})")


def scope_challenged(item: dict[str, Any], changed_paths: list[str]) -> bool:
    """Scope-match challenge trigger (invariant 3): True iff any scopeTag glob hits
    any changed path (the `git diff --name-only`). A True marks the item a CANDIDATE
    for re-validation, NEVER for deletion -- challenge != delete. Empty scope -> False."""
    scope = item.get("scopeTag") or []
    return any(fnmatch.fnmatch(path, glob) for glob in scope for path in changed_paths)


def shadow_scan(registry: list[dict[str, Any]], policy_authority: str,
                changed_paths: list[str]) -> dict[str, int]:
    """Measure-before-enforce (invariant 4): count what a real retrieval WOULD do,
    without changing a single item. `wouldRefuse` splits into `refuseByAuthority`,
    `refuseByState`, and `refuseByInvalid` (an unrecognized-tier row that fails
    closed) -- the last is a DISTINCT poisoning-risk signal the owner reads before
    enabling enforcement, NEVER silently bucketed under `admitted` (the 2026-07-21
    reckon: the measure must surface this class, not hide it). `wouldChallenge`
    (scope-match) is orthogonal to the firewall. Fail-open: a malformed row
    (missing keys) is counted under `malformed` and skipped, never raised."""
    result = {"total": len(registry), "admitted": 0, "wouldRefuse": 0,
              "refuseByAuthority": 0, "refuseByState": 0, "refuseByInvalid": 0,
              "wouldChallenge": 0, "malformed": 0}
    for item in registry:
        try:
            admit, reason = firewall_verdict(item, policy_authority)
            if admit:
                result["admitted"] += 1
            else:
                result["wouldRefuse"] += 1
                if reason.startswith("state-gate:"):
                    result["refuseByState"] += 1
                elif reason.startswith("authority-invalid:"):
                    result["refuseByInvalid"] += 1
                else:
                    result["refuseByAuthority"] += 1
            if scope_challenged(item, changed_paths):
                result["wouldChallenge"] += 1
        except Exception:  # noqa: BLE001 -- invariant 4: a measure never crashes
            result["malformed"] += 1
    return result


def _self_check() -> int:
    import copy

    # invariant 1: defaults + out-of-vocab never raises.
    it = make_item("m1", "note")
    assert it["authority"] == "active_memory" and it["state"] == "candidate", it
    assert it["scopeTag"] == [] and it["provenance"] == {} and it["at"], it
    assert it["id"] == "m1" and it["content"] == "note", it
    bad = make_item("m2", "x", authority="martian", state="nonsense", scope_tag="scripts/**")
    assert bad["authority"] == "active_memory" and bad["state"] == "candidate", bad
    assert bad["scopeTag"] == ["scripts/**"], bad  # a bare string is wrapped, not exploded

    # _rank: ordering + tolerant unknown.
    assert _rank("untrusted") == 0 < _rank("signed_policy") == 4, AUTHORITY_TIERS
    assert _rank("who") == -1 and _rank({"x": 1}) == -1  # unknown/unhashable -> -1

    # invariant 2: tier ordering + state gate with a DISTINCT reason.
    admit, r_lo = firewall_verdict(make_item("a", "x", authority="active_memory"), "signed_policy")
    assert admit is True, r_lo
    admit, _ = firewall_verdict(make_item("b", "x", authority="untrusted"), "active_memory")
    assert admit is True  # untrusted < active_memory
    admit, r_eq = firewall_verdict(make_item("c", "x", authority="overseer"), "overseer")
    assert admit is False and r_eq.startswith("authority-gate:"), r_eq  # >= refused
    admit, r_state = firewall_verdict(make_item("d", "x", state="revoked"), "signed_policy")
    assert admit is False and r_state.startswith("state-gate:") and r_state != r_eq, r_state
    assert firewall_verdict(make_item("e", "x", state="expired"), "signed_policy")[0] is False
    # FAIL CLOSED on an unrecognized authority (2026-07-21 reckon BLOCKER): a
    # HAND-BUILT row (bypassing make_item's sanitization, i.e. a corrupted/loaded
    # registry) with garbage authority must be REFUSED at EVERY policy tier, never
    # admitted as if it were the weakest known tier.
    evil = {"id": "z", "content": "poison", "authority": "lixo", "scopeTag": [],
            "provenance": {}, "state": "active", "at": "t"}
    for pol in ("untrusted", "signed_policy"):
        admit, r_inv = firewall_verdict(evil, pol)
        assert admit is False and r_inv.startswith("authority-invalid:"), (pol, r_inv)
    # an unrecognized POLICY also fails closed (never admit against an unknown bar).
    assert firewall_verdict(make_item("y", "x", authority="untrusted"), "garbage")[0] is False

    # invariant 3: glob match, empty scope never challenges.
    scoped = make_item("f", "x", scope_tag=["scripts/**"])
    assert scope_challenged(scoped, ["scripts/x.py"]) is True
    assert scope_challenged(scoped, ["docs/y.md"]) is False
    assert scope_challenged(make_item("g", "x"), ["scripts/x.py"]) is False

    # invariant 4: counters + ZERO mutation + fail-open on a malformed row + the
    # invalid-authority row surfaces as refuseByInvalid, NEVER admitted (reckon).
    registry = [
        make_item("h", "x", authority="active_memory"),           # admitted
        make_item("i", "x", authority="signed_policy"),           # refuseByAuthority
        make_item("j", "x", state="revoked"),                     # refuseByState
        make_item("k", "x", authority="active_memory", scope_tag=["scripts/**"]),  # admit+challenge
        {"id": "bad", "content": "p", "authority": "lixo", "scopeTag": [],
         "provenance": {}, "state": "active", "at": "t"},          # refuseByInvalid (hand-built)
        {"authority": 123},                                       # malformed: no state key
    ]
    before = copy.deepcopy(registry)
    scan = shadow_scan(registry, "signed_policy", ["scripts/x.py"])
    assert registry == before, "shadow_scan mutated the registry"
    assert scan["total"] == 6 and scan["malformed"] == 1, scan
    assert scan["admitted"] == 2 and scan["wouldRefuse"] == 3, scan
    assert scan["refuseByAuthority"] == 1 and scan["refuseByState"] == 1, scan
    assert scan["refuseByInvalid"] == 1, scan  # the poison row is a DISTINCT signal, not admitted
    assert scan["wouldChallenge"] == 1, scan

    print("governed_memory self-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
