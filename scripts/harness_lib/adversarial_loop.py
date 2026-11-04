#!/usr/bin/env python3
"""SPEC-183 Vendor-Neutral Adversarial Review State Machine V1.

Enforces explicit state machine transitions for risk-bearing tasks:
IMPLEMENTING -> VALIDATING -> RECKONING -> MUTATING -> AUDITING -> (BLOCK -> REMEDIATING -> VALIDATING) / (SHIP -> COMMIT_ELIGIBLE)
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from harness_lib import commit_authority, event_ledger, validation_stamp
from harness_lib.common import HarnessError, ROOT


GOVERNANCE_STATES = (
    "IMPLEMENTING",
    "VALIDATING",
    "RECKONING",
    "MUTATING",
    "AUDITING",
    "REMEDIATING",
    "COMMIT_ELIGIBLE",
)


def get_governance_state(root: Path | str | None = None) -> dict[str, Any]:
    r_path = Path(root) if root else ROOT
    is_eligible, details = commit_authority.check_commit_eligibility(r_path)
    staged_fp = details.get("stagedFingerprint") or ""

    if not staged_fp:
        return {
            "state": "IMPLEMENTING",
            "stagedFingerprint": "",
            "validationOk": False,
            "reckonOk": False,
            "mutationOk": False,
            "auditOk": False,
            "auditVerdict": None,
            "nextStep": "stage_files_and_validate",
        }

    val_ok = details["validationOk"]
    rec_ok = details["reckonOk"]
    mut_ok = details["mutationOk"]
    aud_ok = details["auditOk"]
    aud_verdict = details.get("auditVerdict") or "none"

    if not val_ok:
        state = "VALIDATING"
        next_step = "run_validation_gate"
    elif not rec_ok:
        state = "RECKONING"
        next_step = "run_reckon"
    elif not mut_ok:
        state = "MUTATING"
        next_step = "run_mutation_testing"
    elif aud_verdict == "block":
        state = "REMEDIATING"
        next_step = "remediate_block_findings_and_revalidate"
    elif not aud_ok or aud_verdict == "none":
        state = "AUDITING"
        next_step = "run_adversarial_audit"
    elif is_eligible or aud_verdict in ("ship", "waived"):
        state = "COMMIT_ELIGIBLE"
        next_step = "execute_governed_commit"
    else:
        state = "AUDITING"
        next_step = "resolve_audit_verdict"


    return {
        "state": state,
        "stagedFingerprint": staged_fp,
        "validationOk": val_ok,
        "reckonOk": rec_ok,
        "mutationOk": mut_ok,
        "auditOk": aud_ok,
        "auditVerdict": aud_verdict,
        "nextStep": next_step,
    }



def record_remediation_intent(reason: str, root: Path | str | None = None) -> dict[str, Any]:
    """Record remediation intent after audit BLOCK verdict. Resets state to VALIDATING."""
    r_path = Path(root) if root else ROOT
    staged_fp = validation_stamp.staged_fingerprint(r_path)

    try:
        event_ledger.append("remediation_started", {
            "reason": reason,
            "stagedFingerprint": staged_fp,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }, root=r_path)
    except Exception:
        pass

    return get_governance_state(r_path)


def cmd_governance_state(args: Any) -> None:
    """CLI handler for `harness governance-state`."""
    commit_sha = getattr(args, "commit", None)
    if getattr(args, "proof_graph", False):
        if not commit_sha:
            from harness_lib import commit_authority
            commit_sha = commit_authority._get_head_sha(ROOT)

        from harness_lib import governance_receipt
        graph_data = governance_receipt.generate_proof_graph(ROOT, commit_sha)
        print(json.dumps(graph_data, indent=2))
        return


    if commit_sha:
        from harness_lib import commit_authority
        tie = commit_authority.query_commit_transaction(ROOT, commit_sha)
        if not tie:
            raise HarnessError(f"No commit-proof tie transaction found for commit SHA '{commit_sha}'")
        print(json.dumps(tie, indent=2))
        return


    state = get_governance_state()
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2))
    else:
        print(f"Governance State:   {state['state']}")
        print(f"Staged Fingerprint: {state['stagedFingerprint'][:12] if state['stagedFingerprint'] else 'none'}")
        print(f"Validation:         {'OK' if state['validationOk'] else 'PENDING'}")
        print(f"Reckon:             {'OK' if state['reckonOk'] else 'PENDING'}")
        print(f"Mutation:           {'OK' if state['mutationOk'] else 'PENDING'}")
        print(f"Audit:              {'OK' if state['auditOk'] else 'PENDING'} (verdict: {state['auditVerdict']})")
        print(f"Next Action:        {state['nextStep']}")


