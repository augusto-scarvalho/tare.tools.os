# Output Contract for Governance Execution Receipts (ER-13)
"""Common Governance Execution Receipt Engine (Round ER).

Implements the unified GovernanceExecutionReceipt envelope across all 4 governance legs:
- ValidationExecutionReceipt
- ReckonExecutionReceipt
- MutationExecutionReceipt
- SeatCompletionReceipt

Moves authority to process boundary output capture and provides proof graph traversal.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def compute_digest(text_or_bytes: str | bytes) -> str:
    data = text_or_bytes if isinstance(text_or_bytes, bytes) else text_or_bytes.encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class GovernanceExecutionReceipt:
    """Unified execution receipt envelope for governance operations."""

    def __init__(
        self,
        execution_id: str,
        kind: str,
        run_id: str,
        fingerprint: str,
        controller_identity: str = "harness-governance-controller",
        invocation_digest: Optional[str] = None,
        input_digest: Optional[str] = None,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        process_identity: Optional[Dict[str, Any]] = None,
        exit_code: Optional[int] = None,
        termination_reason: str = "launched",
        output_digests: Optional[Dict[str, str]] = None,
        evidence_refs: Optional[List[str]] = None,
        event_refs: Optional[List[str]] = None,
        status: str = "launched",
        parsed_result: Optional[Dict[str, Any]] = None,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.execution_id = execution_id
        self.kind = kind
        self.run_id = run_id
        self.fingerprint = fingerprint
        self.controller_identity = controller_identity  # ER-16: producerIdentity / controllerIdentity
        self.invocation_digest = invocation_digest or ""
        self.input_digest = input_digest or ""
        self.started_at = started_at or now()
        self.ended_at = ended_at
        self.process_identity = process_identity or {"pid": os.getpid(), "nonce": hashlib.sha256(os.urandom(16)).hexdigest()[:12]}
        self.exit_code = exit_code
        self.termination_reason = termination_reason
        self.output_digests = output_digests or {"stdoutDigest": "", "stderrDigest": "", "rawResultDigest": ""}
        self.evidence_refs = evidence_refs or []
        self.event_refs = event_refs or []
        self.status = status  # 'launched' | 'completed' | 'failed'
        self.parsed_result = parsed_result or {}
        # TCP-02: verifier/runtime/strict-context identity bound at execution
        # time (e.g. strictValidationContextDigest, trustedVerifierIdentityDigest,
        # verifierRuntimeIdentityDigest). Empty for legacy receipts.
        self.execution_context = execution_context or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executionId": self.execution_id,
            "kind": self.kind,
            "runId": self.run_id,
            "fingerprint": self.fingerprint,
            "controllerIdentity": self.controller_identity,
            "invocationDigest": self.invocation_digest,
            "inputDigest": self.input_digest,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "processIdentity": self.process_identity,
            "exitCode": self.exit_code,
            "terminationReason": self.termination_reason,
            "outputDigests": self.output_digests,
            "evidenceRefs": self.evidence_refs,
            "eventRefs": self.event_refs,
            "status": self.status,
            "parsedResult": self.parsed_result,
            "executionContext": self.execution_context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GovernanceExecutionReceipt":

        return cls(
            execution_id=data.get("executionId", ""),
            kind=data.get("kind", "generic"),
            run_id=data.get("runId", ""),
            fingerprint=data.get("fingerprint", ""),
            controller_identity=data.get("controllerIdentity", "harness-governance-controller"),
            invocation_digest=data.get("invocationDigest"),
            input_digest=data.get("inputDigest"),
            started_at=data.get("startedAt"),
            ended_at=data.get("endedAt"),
            process_identity=data.get("processIdentity"),
            exit_code=data.get("exitCode"),
            termination_reason=data.get("terminationReason", "unknown"),
            output_digests=data.get("outputDigests"),
            evidence_refs=data.get("evidenceRefs"),
            event_refs=data.get("eventRefs"),
            status=data.get("status", "launched"),
            parsed_result=data.get("parsedResult"),
            execution_context=data.get("executionContext"),
        )

    def is_completed_and_valid(self, expected_fingerprint: Optional[str] = None) -> Tuple[bool, str]:
        """ER-2 & ER-6: Enforces that status MUST be 'completed' and exitCode MUST be 0."""
        if self.status != "completed":
            return False, f"execution receipt status is {self.status!r}, not 'completed' (status: launched is not execution proof)"
        if self.exit_code != 0:
            return False, f"execution process exit code is {self.exit_code}, not 0"
        if expected_fingerprint and self.fingerprint and self.fingerprint != expected_fingerprint:
            return False, f"receipt fingerprint mismatch: expected {expected_fingerprint[:12]}, got {self.fingerprint[:12]}"
        if self.controller_identity not in ("harness-governance-controller", "harness-audit-controller"):
            return False, f"unauthorized controllerIdentity {self.controller_identity!r}"
        return True, "valid completed execution receipt"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # TCP-02: immutable receipt identity & stamp-side reference binding
    # ------------------------------------------------------------------

    def receipt_digest(self) -> str:
        """Canonical digest of the full receipt payload. Any byte of the
        persisted receipt changing (reseal, tamper, replay-edit) changes it."""
        return compute_digest(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")))

    def stamp_reference(self) -> Dict[str, str]:
        """What a strict stamp may embed: a REFERENCE to this receipt, never a
        copy that could be resealed. Verification always re-reads the canonical
        receipt from disk (verify_receipt_reference)."""
        return {
            "executionId": self.execution_id,
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "receiptDigest": self.receipt_digest(),
        }


def verify_receipt_reference(
    root: Path,
    reference: Dict[str, Any],
    expected_fingerprint: Optional[str] = None,
    expected_context_digest: Optional[str] = None,
) -> Tuple[bool, str]:
    """TCP-02 fail-closed check that a stamp-embedded reference names a REAL,
    completed, unmodified controller execution. Structural validity of the
    reference alone can never pass: the canonical on-disk receipt is re-read
    and re-digested every time."""
    if not isinstance(reference, dict):
        return False, "receipt reference is not a mapping"
    for key in ("executionId", "kind", "fingerprint", "receiptDigest"):
        if not reference.get(key):
            return False, f"receipt reference missing {key!r}"
    receipt = load_receipt(get_receipt_path(root, str(reference["kind"]), str(reference["fingerprint"])))
    if receipt is None:
        return False, "no canonical execution receipt on disk (missing/stale execution evidence)"
    if receipt.execution_id != reference["executionId"]:
        return False, "reference executionId does not match canonical receipt (replayed reference)"
    if receipt.receipt_digest() != reference["receiptDigest"]:
        return False, "canonical receipt digest mismatch (resealed or tampered receipt)"
    ok, reason = receipt.is_completed_and_valid(expected_fingerprint or str(reference["fingerprint"]))
    if not ok:
        return False, reason
    if expected_context_digest is not None:
        actual = (receipt.execution_context or {}).get("strictValidationContextDigest")
        if actual != expected_context_digest:
            return False, "strict execution context digest mismatch (verifier/runtime mismatch)"
    return True, "verified canonical execution receipt reference"


def load_receipt(path: Path) -> Optional[GovernanceExecutionReceipt]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "executionId" in data:
            return GovernanceExecutionReceipt.from_dict(data)
    except Exception:
        pass
    return None


def get_receipt_path(root: Path, kind: str, fp: str, seat_id: Optional[str] = None) -> Path:
    # Receipts are runtime evidence.  They must not be staged into the candidate
    # that they describe; legacy state receipts remain readable through callers
    # that explicitly need historical compatibility.
    base = root / ".harness" / "runs" / "governance-receipts"
    if kind == "audit_seat" and seat_id:
        return root / ".harness" / "runs" / f"audit-{fp[:12]}" / seat_id / "seat-execution.json"
    return base / f"{kind}-{fp[:12]}.json"


def generate_proof_graph(root: Path, commit_sha: str) -> Dict[str, Any]:
    """ER-14: Builds verifiable proof graph linking transaction -> stamps -> execution receipts."""
    from harness_lib import commit_authority, validation_stamp

    tx = commit_authority.query_commit_transaction(root, commit_sha)
    if not tx:
        return {"status": "not_found", "commitSha": commit_sha, "graph": None}

    stamps = tx.get("stamps") or {}
    fp = tx.get("stagedFingerprint", "")
    graph = {
        "commitSha": commit_sha,
        "transactionId": tx.get("transactionId"),
        "proofSetId": tx.get("proofSetId"),
        "stagedFingerprint": fp,
        "committedAt": tx.get("committedAt"),
        "nodes": [
            {
                "type": "CommitSHA",
                "id": commit_sha,
                "label": f"Commit {commit_sha[:8]}",
            },
            {
                "type": "GovernanceTransaction",
                "id": tx.get("transactionId"),
                "proofSetId": tx.get("proofSetId"),
            },
            {
                "type": "ProofSet",
                "id": tx.get("proofSetId"),
            },
        ],
        "edges": [
            {"from": commit_sha, "to": tx.get("transactionId"), "relation": "committed_via"},
            {"from": tx.get("transactionId"), "to": tx.get("proofSetId"), "relation": "requires_proof_set"},
        ],
    }

    # Add stamps & execution receipts to graph
    for leg, stamp_key in [("validation", "validationStampId"), ("reckon", "reckonStampId"), ("mutation", "mutationStampId"), ("audit", "auditStampId")]:
        stamp_id = stamps.get(stamp_key)
        if stamp_id:
            graph["nodes"].append({"type": f"{leg.capitalize()}Stamp", "id": stamp_id})
            graph["edges"].append({"from": tx.get("proofSetId"), "to": stamp_id, "relation": f"includes_{leg}_stamp"})

            rc_path = get_receipt_path(root, leg, fp)
            rc = load_receipt(rc_path)
            if rc:
                graph["nodes"].append({"type": f"{leg.capitalize()}ExecutionReceipt", "id": rc.execution_id, "status": rc.status, "exitCode": rc.exit_code})
                graph["edges"].append({"from": stamp_id, "to": rc.execution_id, "relation": "verified_by_receipt"})

    return {"status": "found", "commitSha": commit_sha, "graph": graph}
