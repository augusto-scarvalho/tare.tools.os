"""Governance campaigns bound to one immutable CandidateSnapshot.

This is the kernel seam.  CLI adapters may construct campaigns, but they must
not substitute a developer working tree after a campaign has frozen.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from harness_lib import candidate_snapshot, event_ledger, processes, trusted_verifier
from harness_lib.common import HarnessError, now


STATES = frozenset({
    "CREATED", "FROZEN", "VALIDATING", "RECKONING", "MUTATING", "AUDITING",
    "SHIP", "BLOCK", "FREEZE_INVALIDATED", "NON_CONVERGENT", "COMMIT_ELIGIBLE",
    "AUDIT_INSTABILITY", "CLOSED",
})
MAX_AUDIT_REMEDIATION_ROUNDS = 3
MAX_CONTRACT_RETRIES = 1
_LEG_STATES = {"validation": "VALIDATING", "reckon": "RECKONING", "mutation": "MUTATING", "audit": "AUDITING"}
_TRANSIENT_AUDIT_FAILURES = frozenset({"TRANSPORT_FAILURE", "CONTRACT_FAILURE", "TIMEOUT", "PROCESS_FAILURE"})
T = TypeVar("T")


@dataclass(frozen=True)
class GovernanceExecution:
    """Controller-created provenance for a deterministic governance leg."""
    execution_id: str
    leg: str
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_id: str
    audit_context_digest: str | None
    status: str
    result_digest: str
    controller: str = "GovernanceCampaign"
    mechanism: str = "callback"
    trusted_verifier_id: str | None = None
    trusted_verifier_version: str | None = None
    trusted_verifier_identity_digest: str | None = None
    base_head: str | None = None
    proof_profile: str | None = None
    proof_profile_digest: str | None = None
    execution_authority: str = "MANUAL_ATTESTATION"
    proof_eligibility: str = "INELIGIBLE"


@dataclass(frozen=True)
class ReckonStamp:
    """Mechanized reckon proof, bound to one frozen candidate."""
    stamp_id: str
    candidate_tree_id: str
    snapshot_id: str
    execution_id: str
    verdict: str
    mechanism: str
    proof_profile: str = "manual-attestation"
    execution_authority: str = "MANUAL_ATTESTATION"
    proof_eligibility: str = "INELIGIBLE"


class ReckonController:
    """Runs deterministic analysis on CandidateSnapshot and captures its receipt."""

    def __init__(self, campaign: "GovernanceCampaign") -> None:
        self.campaign = campaign

    def run(self, analysis: Callable[[candidate_snapshot.CandidateSnapshot], Any]) -> tuple[Any, GovernanceExecution, ReckonStamp]:
        """Legacy/manual callback path; its stamp is never strict proof."""
        result, receipt = self.campaign.run_leg("reckon", analysis)
        verdict = str(result.get("verdict", "no-blocker") if isinstance(result, dict) else result)
        stamp = ReckonStamp(
            stamp_id=f"STAMP-REC-{receipt.execution_id[-16:]}",
            candidate_tree_id=receipt.candidate_tree_id,
            snapshot_id=receipt.snapshot_id,
            execution_id=receipt.execution_id,
            verdict=verdict,
            mechanism="MANUAL_ATTESTATION",
            execution_authority="MANUAL_ATTESTATION", proof_eligibility="INELIGIBLE",
        )
        return result, receipt, stamp

    def run_mechanized(self) -> tuple[dict[str, Any], GovernanceExecution, ReckonStamp]:
        """Minimum strict reckon: controller-owned Git integrity analysis."""
        def analysis(snapshot: candidate_snapshot.CandidateSnapshot) -> dict[str, Any]:
            executor = candidate_snapshot.SnapshotExecutor(snapshot)
            diff = executor.run(["git", "diff", "--check", f"{snapshot.base_head}..HEAD"],
                                timeout=120, capture_output=True, text=True)
            return {"verdict": "no-blocker" if diff.returncode == 0 else "blocker",
                    "mechanism": "git-diff-check", "exitCode": diff.returncode,
                    "stdout": diff.stdout or "", "stderr": diff.stderr or ""}
        result, receipt = self.campaign.run_leg("reckon", analysis)
        receipt = GovernanceExecution(**{**receipt.__dict__, "mechanism": "git-diff-check",
                                         "execution_authority": "CONTROLLER_MECHANIZED"})
        proof_profile = "git-integrity-minimum"
        result["proofProfile"] = proof_profile
        receipt = GovernanceExecution(**{**receipt.__dict__, "proof_profile": proof_profile,
                                         "proof_eligibility": "INELIGIBLE"})
        stamp = ReckonStamp(f"STAMP-REC-{receipt.execution_id[-16:]}", receipt.candidate_tree_id,
                            receipt.snapshot_id, receipt.execution_id, result["verdict"], "git-diff-check",
                            proof_profile, "CONTROLLER_MECHANIZED", "INELIGIBLE")
        return result, receipt, stamp

    @staticmethod
    def require_strict(stamp: ReckonStamp, required_profile: str = "harness-deterministic-v1") -> None:
        if (stamp.mechanism != "git-diff-check" or stamp.verdict != "no-blocker"
                or stamp.proof_profile != required_profile):
            raise HarnessError("RECKON_ELIGIBILITY_DEPENDS_ON_REQUIRED_PROFILE")


@dataclass(frozen=True)
class AuditExecutionReceipt:
    """Audit receipt whose adapter is unable to omit snapshot identity."""
    execution_id: str
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_id: str
    audit_context_digest: str | None
    outcome: str


def _developer_status(root: Path) -> str:
    proc = processes.run_quiet(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                               timeout=120, cwd=str(root), capture_output=True, text=True)
    if proc.returncode:
        raise HarnessError("cannot read developer worktree state for mutation isolation")
    return proc.stdout or ""


@dataclass
class GovernanceCampaign:
    campaign_id: str
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_id: str
    snapshot_path: Path
    created_at: str
    base_head: str = ""
    remediation_round: int = 0
    state: str = "CREATED"
    audit_context_digest: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    trusted_verifier_id: str | None = None
    _trusted_verifier: trusted_verifier.TrustedVerifier | None = field(repr=False, default=None)
    _root: Path = field(repr=False, default=Path("."))
    _snapshot: candidate_snapshot.CandidateSnapshot = field(repr=False, default=None)  # type: ignore[assignment]
    _semantic_verdicts: list[str] = field(default_factory=list, repr=False)
    _transient_retries: dict[str, int] = field(default_factory=dict, repr=False)

    @classmethod
    def create(cls, root: Path | str, *, remediation_round: int = 0,
               audit_context_digest: str | None = None,
               required_gates: list[str] | tuple[str, ...] = (),
               verifier_record: trusted_verifier.TrustedVerifierRecord | None = None) -> tuple["GovernanceCampaign", candidate_snapshot.CandidateSnapshot]:
        snapshot = candidate_snapshot.CandidateSnapshot.create_from_index(root)
        record = verifier_record or trusted_verifier.load_trusted_verifier_record(root)
        verifier = trusted_verifier.TrustedVerifier.from_record(root, record) if record else None
        campaign = cls(
            campaign_id=f"GC-{uuid.uuid4().hex[:16]}",
            candidate_tree_id=snapshot.candidate_tree_id,
            candidate_fingerprint=snapshot.candidate_fingerprint,
            snapshot_id=snapshot.snapshot_id,
            snapshot_path=snapshot.snapshot_path,
            created_at=now(),
            base_head=snapshot.base_head,
            remediation_round=remediation_round,
            state="FROZEN",
            audit_context_digest=audit_context_digest,
            trusted_verifier_id=verifier.verifier_id if verifier else None,
            _trusted_verifier=verifier,
            _root=Path(root).resolve(),
            _snapshot=snapshot,
        )
        return campaign, snapshot

    @property
    def trusted_verifier(self) -> trusted_verifier.TrustedVerifier:
        if self._trusted_verifier is None:
            raise HarnessError("STRICT_VALIDATION_INELIGIBLE: CURRENT_HEAD_IS_NOT_IMPLICIT_TRUST_ROOT")
        return self._trusted_verifier

    @property
    def snapshot(self) -> candidate_snapshot.CandidateSnapshot:
        if self._snapshot is None:
            raise HarnessError("GovernanceCampaign has no canonical CandidateSnapshot")
        return self._snapshot

    def metadata(self) -> dict[str, Any]:
        return {
            "campaignId": self.campaign_id,
            "candidateTreeId": self.candidate_tree_id,
            "candidateFingerprint": self.candidate_fingerprint,
            "snapshotId": self.snapshot_id,
            "snapshotPath": str(self.snapshot_path),
            "remediationRound": self.remediation_round,
            "state": self.state,
            "createdAt": self.created_at,
            "baseHead": self.base_head,
            "trustedVerifierId": self.trusted_verifier_id,
            "invalidatedAt": self.invalidated_at,
            "invalidationReason": self.invalidation_reason,
        }

    def _invalidate(self, reason: str) -> None:
        if self.state == "FREEZE_INVALIDATED":
            return
        self.state = "FREEZE_INVALIDATED"
        self.invalidated_at = now()
        self.invalidation_reason = reason
        event_ledger.append_critical("governance_campaign_freeze_invalidated", {
            "campaignId": self.campaign_id,
            "candidateTreeId": self.candidate_tree_id,
            "candidateFingerprint": self.candidate_fingerprint,
            "snapshotId": self.snapshot_id,
            "reason": reason,
            "hooksRequired": False,
        }, root=self._root)

    def assert_frozen(self, boundary: str) -> None:
        if self.state in {"FREEZE_INVALIDATED", "NON_CONVERGENT", "AUDIT_INSTABILITY", "CLOSED"}:
            raise HarnessError(f"GovernanceCampaign {self.campaign_id} is terminal: {self.state}")
        live = candidate_snapshot.current_candidate_tree(self._root)
        if live != self.candidate_tree_id:
            self._invalidate(f"candidate tree drift {self.candidate_tree_id} -> {live} at {boundary}")
            raise HarnessError(f"GovernanceCampaign freeze invalidated at {boundary}")
        self.snapshot.verify()

    def begin_leg(self, leg: str) -> None:
        if leg not in _LEG_STATES:
            raise HarnessError(f"unknown governance leg {leg!r}")
        self.assert_frozen(f"before:{leg}")
        self.state = _LEG_STATES[leg]

    def finish_leg(self, leg: str, *, outcome: str | None = None,
                   diagnostic_repeat: bool = False) -> None:
        self.assert_frozen(f"after:{leg}")
        if leg == "audit" and outcome:
            self.record_audit_outcome(outcome, diagnostic_repeat=diagnostic_repeat)
        elif self.state == _LEG_STATES.get(leg):
            self.state = "FROZEN"

    def _execution(self, leg: str, result: Any, status: str = "completed") -> GovernanceExecution:
        encoded = json.dumps(result, sort_keys=True, default=str).encode("utf-8")
        return GovernanceExecution(
            execution_id=f"GX-{leg[:3].upper()}-{uuid.uuid4().hex[:16]}", leg=leg,
            candidate_tree_id=self.candidate_tree_id,
            candidate_fingerprint=self.candidate_fingerprint, snapshot_id=self.snapshot_id,
            audit_context_digest=self.audit_context_digest, status=status,
            result_digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
            trusted_verifier_id=self.trusted_verifier_id,
            trusted_verifier_version=self._trusted_verifier.verifier_version if self._trusted_verifier else None,
            trusted_verifier_identity_digest=self._trusted_verifier.executable_identity_digest if self._trusted_verifier else None,
            base_head=self.base_head, proof_profile=self._trusted_verifier.proof_profile if self._trusted_verifier else None,
            proof_profile_digest=self._trusted_verifier.record.proof_profile_digest if self._trusted_verifier else None,
        )

    def run_leg(self, leg: str, action: Callable[[candidate_snapshot.CandidateSnapshot], T]) -> tuple[T, GovernanceExecution]:
        """Execute an action against the canonical snapshot and prove both boundaries."""
        self.begin_leg(leg)
        try:
            result = action(self.snapshot)
        except Exception:
            # A failed leg may still have raced a candidate edit.  The freeze
            # check is mandatory on this failure path too.
            self.assert_frozen(f"after:{leg}")
            raise
        self.finish_leg(leg)
        return result, self._execution(leg, result)

    def run_mutation(self, mutator: Callable[[Path], T]) -> tuple[T, GovernanceExecution]:
        """Run one mutant in a disposable fork without touching either authority."""
        self.begin_leg("mutation")
        before_status = _developer_status(self._root)
        before_tree = candidate_snapshot.current_candidate_tree(self._root)
        fork = self.snapshot.fork(label="mutation")
        try:
            result = mutator(fork)
        except Exception:
            self.assert_frozen("after:mutation")
            raise
        finally:
            self.snapshot.cleanup_fork(fork)
        after_status = _developer_status(self._root)
        after_tree = candidate_snapshot.current_candidate_tree(self._root)
        if before_status != after_status or before_tree != after_tree:
            raise HarnessError("mutation sandbox changed developer worktree or CandidateTreeId")
        self.snapshot.verify()
        self.finish_leg("mutation")
        return result, self._execution("mutation", result)

    def run_audit(self, adapter: Callable[[dict[str, str], candidate_snapshot.CandidateSnapshot], str]) -> AuditExecutionReceipt:
        """Run a deterministic audit adapter against the canonical snapshot only."""
        self.begin_leg("audit")
        packet = {
            "campaignId": self.campaign_id,
            "candidateTreeId": self.candidate_tree_id,
            "candidateFingerprint": self.candidate_fingerprint,
            "snapshotId": self.snapshot_id,
            "snapshotPath": str(self.snapshot_path),
            "auditContextDigest": self.audit_context_digest or "",
        }
        try:
            outcome = adapter(packet, self.snapshot).upper()
        except Exception:
            self.assert_frozen("after:audit")
            raise
        self.finish_leg("audit", outcome=outcome)
        execution = self._execution("audit", {"packet": packet, "outcome": outcome})
        return AuditExecutionReceipt(
            execution_id=execution.execution_id, candidate_tree_id=self.candidate_tree_id,
            candidate_fingerprint=self.candidate_fingerprint, snapshot_id=self.snapshot_id,
            audit_context_digest=self.audit_context_digest, outcome=outcome,
        )

    def shard_forks(self, count: int) -> list[Path]:
        """Create parallel-ready shard roots from this snapshot; no serial fallback."""
        if count < 1:
            raise HarnessError("parallel snapshot execution requires at least one shard")
        self.assert_frozen("before:parallel-shards")
        shards = [self.snapshot.fork(label=f"shard-{index}") for index in range(1, count + 1)]
        try:
            for shard in shards:
                got = candidate_snapshot._require_git(shard, "rev-parse", "HEAD^{tree}",
                                                       env=self.snapshot.execution_env())
                if got != self.candidate_tree_id:
                    raise HarnessError("ALL_SHARDS_BIND_SAME_CANDIDATE_TREE failed")
            self.assert_frozen("after:parallel-shards")
            return shards
        except Exception:
            for shard in shards:
                self.snapshot.cleanup_fork(shard)
            raise

    def record_audit_outcome(self, outcome: str, *, diagnostic_repeat: bool = False) -> None:
        normalized = outcome.upper()
        if normalized not in {"BLOCK", "SHIP", *_TRANSIENT_AUDIT_FAILURES}:
            raise HarnessError(f"unknown audit outcome {outcome!r}")
        if normalized in _TRANSIENT_AUDIT_FAILURES:
            key = normalized
            self._transient_retries[key] = self._transient_retries.get(key, 0) + 1
            if normalized == "CONTRACT_FAILURE" and self._transient_retries[key] > MAX_CONTRACT_RETRIES:
                self.state = "BLOCK"
                raise HarnessError("CONTRACT_FAILURE retry budget exhausted")
            self.state = "FROZEN"
            return
        if self.state == "BLOCK" and not diagnostic_repeat:
            raise HarnessError("SEMANTIC_BLOCK_REQUIRES_CANDIDATE_CHANGE")
        if self._semantic_verdicts and self._semantic_verdicts[-1] != normalized:
            if diagnostic_repeat:
                self.state = "AUDIT_INSTABILITY"
                raise HarnessError("AUDIT_INSTABILITY")
            if self._semantic_verdicts[-1] == "BLOCK":
                raise HarnessError("SEMANTIC_BLOCK_REQUIRES_CANDIDATE_CHANGE")
        self._semantic_verdicts.append(normalized)
        self.state = normalized

    def mark_commit_eligible(self) -> None:
        self.assert_frozen("commit-eligibility")
        if self.state != "SHIP":
            raise HarnessError("only SHIP campaigns can become COMMIT_ELIGIBLE")
        self.state = "COMMIT_ELIGIBLE"

    def remediate(self, root: Path | str) -> tuple["GovernanceCampaign", candidate_snapshot.CandidateSnapshot]:
        if self.state != "BLOCK":
            raise HarnessError("candidate remediation requires a semantic BLOCK")
        next_round = self.remediation_round + 1
        if next_round > MAX_AUDIT_REMEDIATION_ROUNDS:
            self.state = "NON_CONVERGENT"
            raise HarnessError("MAX_AUDIT_REMEDIATION_ROUNDS exceeded; campaign is NON_CONVERGENT")
        next_campaign, snapshot = self.create(root, remediation_round=next_round,
                                               audit_context_digest=self.audit_context_digest)
        if next_campaign.candidate_tree_id == self.candidate_tree_id:
            snapshot.cleanup()
            raise HarnessError("semantic BLOCK retry refused: remediation must create a new candidate tree")
        return next_campaign, snapshot

    def close(self) -> None:
        self.state = "CLOSED"
        if self._trusted_verifier is not None:
            self._trusted_verifier.cleanup()
