#!/usr/bin/env python3
"""SPEC-182 Governed Commit Authority V1.

Single chokepoint for governed commits. Enforces that risk-bearing changes
cannot be committed without valid, matching ValidationStamp, ReckonStamp,
MutationStamp, and AuditStamp (SHIP verdict) for the staged fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness_lib import event_ledger, processes, validation_stamp
from harness_lib.common import HarnessError, ROOT



def get_staged_fingerprint(root: Path | str | None = None) -> str:
    r_path = Path(root) if root else ROOT
    try:
        policy = validation_stamp.load_policy(r_path) or {}
        staged = validation_stamp.staged_manifest(r_path, policy)
        if staged is None:
            return ""
        return validation_stamp.manifest_fingerprint(staged)
    except Exception:
        return ""


def _transaction_log(root: Path) -> Path:
    return root / ".harness" / "state" / "commit-transactions.jsonl"


def get_consumed_proof_sets(root: Path) -> dict[str, str]:
    """{proofSetId: transactionId} mapping of all consumed proof sets."""
    log_file = _transaction_log(root)
    if not log_file.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("proofSetId"):
                    out[row["proofSetId"]] = row.get("transactionId") or "unknown"
            except Exception:
                pass
    except OSError:
        pass
    return out


def query_commit_transaction(root: Path, commit_sha: str) -> dict[str, Any] | None:
    """Exact commit-proof tie lookup (PA-10). Query by full or short commit SHA."""
    log_file = _transaction_log(root)
    if not log_file.exists():
        return None
    try:
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict) and (row.get("commitSha") or "").startswith(commit_sha):
                    return row
            except Exception:
                pass
    except OSError:
        pass
    return None


def _check_blocking_escalations(root: Path) -> tuple[bool, str | None]:
    """PA-11 Escalation Authority: Check if any unresolved blocking escalation exists."""
    esc_path = root / ".harness" / "state" / "escalations.json"
    if not esc_path.exists():
        return False, None
    try:
        doc = json.loads(esc_path.read_text(encoding="utf-8"))
        items = doc.get("items") or doc.get("escalations") or []
        if isinstance(items, dict):
            items = list(items.values())
        for item in items:
            if isinstance(item, dict) and item.get("status") in ("open", "pending") and item.get("blocking") is True:
                esc_id = item.get("id") or "unknown"
                ask = item.get("ask") or item.get("title") or "unresolved issue"
                return True, f"Blocking escalation {esc_id}: {ask}"
    except Exception:
        pass
    return False, None


def compute_proof_set_id(val_id: str, rec_id: str, mut_id: str, aud_id: str) -> str:
    raw = f"{val_id}:{rec_id}:{mut_id}:{aud_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_commit_eligibility(root: Path | str | None = None) -> tuple[bool, dict[str, Any]]:
    r_path = Path(root) if root else ROOT
    staged_fp = get_staged_fingerprint(r_path)

    val_status, val_reason = validation_stamp.check_staged(r_path)
    rec_status, rec_reason = validation_stamp.check_reckon(r_path)
    mut_status, mut_reason = validation_stamp.check_mutate(r_path)
    aud_status, aud_reason = validation_stamp.check_audit(r_path)

    policy = validation_stamp.load_policy(r_path) or {}
    val_ok = val_status == "pass"
    rec_ok = rec_status == "pass" and ("MANUAL_ATTESTATION" not in rec_reason or bool(policy.get("allowManualReckonAttestation")))
    mut_ok = mut_status in ("pass", "warn")
    aud_ok = aud_status in ("pass", "warn")
    reasons: list[str] = []


    # ER-2 & ER-6: Verify audit seat completion receipt when audit status is pass
    if aud_status == "pass":
        from harness_lib import audit_leg
        audit_run_dir = r_path / ".harness" / "runs" / f"audit-{staged_fp[:12]}"
        has_completed_seat = False
        if audit_run_dir.is_dir():
            for seat_dir in audit_run_dir.glob("seat-*"):
                is_rc_valid, _msg = audit_leg.verify_seat_receipt(r_path, seat_dir)
                if is_rc_valid:
                    has_completed_seat = True
                    break
        if not has_completed_seat:
            aud_ok = False
            reasons.append("Audit: missing valid process-captured SeatCompletionReceipt (status: launched or missing receipt is refused)")

    if not val_ok:
        reasons.append(f"Validation: {val_reason}")
    if not rec_ok:
        if "MANUAL_ATTESTATION" in rec_reason and not policy.get("allowManualReckonAttestation"):
            reasons.append(f"Reckon: reckon stamp is MANUAL_ATTESTATION without mechanized reckon proof or explicit policy waiver ({rec_reason})")
        else:
            reasons.append(f"Reckon: {rec_reason}")
    if not mut_ok:
        reasons.append(f"Mutation: {mut_reason}")
    if not aud_ok:
        reasons.append(f"Audit: {aud_reason}")



    # PA-11: Blocking Escalation Check
    has_esc, esc_reason = _check_blocking_escalations(r_path)
    if has_esc:
        reasons.append(f"Escalation: {esc_reason}")

    # Generate proofSetId based on exact stamp identities
    qs = validation_stamp._read_quality(r_path) or {}
    val_id = ((qs.get("lastValidation") or {}).get("stagedFingerprint") or {}).get("sha256") or staged_fp
    rec_id = ((qs.get("lastReckon") or {}).get("stagedFingerprint") or {}).get("sha256") or staged_fp
    mut_id = staged_fp
    aud_id = ((qs.get("lastAudit") or {}).get("stagedFingerprint") or {}).get("sha256") or staged_fp
    proof_set_id = compute_proof_set_id(val_id, rec_id, mut_id, aud_id)

    # PA-9: Proof Replay Check
    consumed = get_consumed_proof_sets(r_path)
    if proof_set_id in consumed:
        reasons.append(f"ProofReplay: Proof set {proof_set_id[:12]} was already consumed by transaction {consumed[proof_set_id]}")
        replay_blocked = True
    else:
        replay_blocked = False

    is_eligible = val_ok and rec_ok and mut_ok and aud_ok and (not has_esc) and (not replay_blocked)
    has_waiver = (mut_status == "warn") or (aud_status == "warn") or ("MANUAL_ATTESTATION" in rec_reason)
    if is_eligible:
        governed_classification = "GOVERNED_WITH_WAIVER" if has_waiver else "GOVERNED_STRICT"
    else:
        governed_classification = "UNGOVERNED"


    details = {
        "stagedFingerprint": staged_fp,
        "proofSetId": proof_set_id,
        "governedClassification": governed_classification,
        "validationOk": val_ok,
        "reckonOk": rec_ok,
        "mutationOk": mut_ok,
        "auditOk": aud_ok,
        "escalationOk": not has_esc,
        "replayOk": not replay_blocked,
        "validationStatus": val_status,
        "reckonStatus": rec_status,
        "mutationStatus": mut_status,
        "auditStatus": aud_status,
        "auditVerdict": "ship" if aud_ok else ("block" if aud_status in ("fail", "block") else "none"),
        "reasons": reasons,
        "governedEligible": is_eligible,
        "stamps": {
            "validationStampId": f"STAMP-VAL-{val_id[:12]}",
            "reckonStampId": f"STAMP-REC-{rec_id[:12]}",
            "mutationStampId": f"STAMP-MUT-{mut_id[:12]}",
            "auditStampId": f"STAMP-AUD-{aud_id[:12]}",
        }
    }


    return is_eligible, details


def _get_head_sha(root: Path) -> str:
    proc = processes.run_quiet(["git", "rev-parse", "HEAD"], timeout=30, cwd=str(root), capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def execute_commit(
    message: str,
    *,
    bypass_reason: str | None = None,
    actor: str | None = None,
    no_verify: bool = False,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Single chokepoint for commits with GovernanceTransaction semantics (PA-8).
    
    Governed path: verifies proof-carrying stamps, escalation authority, and
    replay protection before executing git commit. Binds proofSetId to SHA.
    """
    r_path = Path(root) if root else ROOT
    old_head = _get_head_sha(r_path)
    policy = validation_stamp.load_policy(r_path) or {}

    # Reject empty staged diff (where staged == HEAD)
    staged = validation_stamp.staged_manifest(r_path, policy)
    head = validation_stamp.head_manifest(r_path, policy)
    if staged == head and not (bypass_reason or no_verify):
        return {
            "status": "no_changes_to_commit",
            "preventiveGovernanceLost": False,
            "reasons": ["Staged diff is empty (staged surface matches HEAD)"],
        }

    if bypass_reason or no_verify:
        b_reason = bypass_reason or "Emergency commit bypass requested"
        b_actor = actor or os.environ.get("USER") or os.environ.get("USERNAME") or "agent"
        b_tx_id = f"TX-BYPASS-{validation_stamp.now().replace(':', '').replace('-', '').replace('.', '')[:15]}"

        try:
            event_ledger.append_critical("emergency_commit_bypass", {
                "transactionId": b_tx_id,
                "reason": b_reason,
                "actor": b_actor,
                "severity": "high",
                "stagedFingerprint": get_staged_fingerprint(r_path),
            }, root=r_path)
        except Exception:
            pass

        proc = processes.run_quiet(
            ["git", "commit", "--no-verify", "-m", message],
            timeout=60,
            cwd=str(r_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "status": "failed",
                "transactionId": b_tx_id,
                "exitCode": proc.returncode,
                "output": proc.stderr or proc.stdout,
                "preventiveGovernanceLost": True,
            }

        new_head = _get_head_sha(r_path)
        if new_head == old_head:
            return {
                "status": "failed",
                "transactionId": b_tx_id,
                "reason": "HEAD did not advance after git commit",
                "preventiveGovernanceLost": True,
            }

        return {
            "status": "emergency_bypass_committed",
            "transactionId": b_tx_id,
            "commitSha": new_head,
            "oldHead": old_head,
            "preventiveGovernanceLost": True,
            "bypassReason": b_reason,
            "actor": b_actor,
        }

    # Governed commit path
    is_eligible, details = check_commit_eligibility(r_path)
    if not is_eligible:
        return {
            "status": "refused",
            "preventiveGovernanceLost": False,
            "reasons": details["reasons"],
            "details": details,
        }

    now_str = validation_stamp.now()
    tx_digest = hashlib.sha256(f"{details['proofSetId']}:{now_str}".encode()).hexdigest()[:8]
    transaction_id = f"TX-{now_str.replace(':', '').replace('-', '').replace('.', '')[:15]}-{tx_digest}"

    # Execute git commit normally (WITHOUT --no-verify)
    proc = processes.run_quiet(
        ["git", "commit", "-m", message],
        timeout=60,
        cwd=str(r_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "failed",
            "transactionId": transaction_id,
            "exitCode": proc.returncode,
            "output": proc.stderr or proc.stdout,
            "preventiveGovernanceLost": False,
        }

    new_head = _get_head_sha(r_path)
    if new_head == old_head or not new_head:
        return {
            "status": "failed",
            "transactionId": transaction_id,
            "reason": "HEAD did not advance after git commit",
            "output": proc.stdout or proc.stderr,
            "preventiveGovernanceLost": False,
        }

    # Record transaction permanently to consume proof set and establish EXACT commit tie
    tx_row = {
        "transactionId": transaction_id,
        "commitSha": new_head,
        "oldHead": old_head,
        "stagedFingerprint": details["stagedFingerprint"],
        "proofSetId": details["proofSetId"],
        "stamps": details["stamps"],
        "committedAt": now_str,
        "commitMessage": message,
    }
    tx_file = _transaction_log(r_path)
    tx_file.parent.mkdir(parents=True, exist_ok=True)
    with tx_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(tx_row, ensure_ascii=False) + "\n")

    try:
        event_ledger.append("governed_commit_executed", {
            "transactionId": transaction_id,
            "commitSha": new_head,
            "oldHead": old_head,
            "stagedFingerprint": details["stagedFingerprint"],
            "proofSetId": details["proofSetId"],
            "auditVerdict": details["auditVerdict"],
        }, root=r_path)
    except Exception:
        pass

    return {
        "status": "governed_commit_success",
        "transactionId": transaction_id,
        "commitSha": new_head,
        "oldHead": old_head,
        "proofSetId": details["proofSetId"],
        "preventiveGovernanceLost": False,
        "details": details,
    }


def cmd_commit(args: Any) -> None:
    """CLI handler for `harness commit`."""
    message = str(getattr(args, "message", "") or "").strip()
    if not message:
        raise HarnessError("Commit message is required (`harness commit -m \"...\"`)")
    res = execute_commit(
        message,
        bypass_reason=getattr(args, "bypass_reason", None),
        actor=getattr(args, "actor", None),
        no_verify=bool(getattr(args, "no_verify", False)),
    )
    if res.get("status") in ("refused", "no_changes_to_commit"):
        reasons = "\n".join(f"  - {r}" for r in (res.get("reasons") or []))
        raise HarnessError(f"Commit refused by CommitAuthority ({res.get('status')}):\n{reasons}")
    elif res.get("status") == "failed":
        raise HarnessError(f"Git commit failed ({res.get('transactionId')}):\n{res.get('output') or res.get('reason')}")
    else:
        print(json.dumps(res, indent=2))


