"""Check OS-owned local executor receipts against the exact sealed candidate.

This validates recorded observations within the existing OS train custody. It
does not authenticate an external signer or qualify Tier 1 execution coverage.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from relay.train_execution import canonical_json_bytes


def verification_observed(train_dir: str | Path, descriptor: Any, candidate_receipt: Any) -> bool:
    """Fail closed for absent, altered, failed, or differently bound receipts."""
    try:
        path = Path(train_dir) / "EXECUTOR_OBSERVATION.json"
        if path.is_symlink() or path.stat().st_size > 65536:
            return False
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return False
        observed = json.loads(raw)
        if raw != canonical_json_bytes(observed):
            return False
        if (observed.get("schema") != "tare.tools/executor-observation/1"
                or observed.get("candidate_sha256") != candidate_receipt.sha256
                or observed.get("descriptor_sha256") != descriptor.sha256
                or observed.get("executor") != "tare.tools.kernel/compute_plane.managed_work"
                or observed.get("status") != "NEEDS_REVALIDATION"
                or observed.get("authority") != "NONE"
                or observed.get("attestation") != {"status": "NOT_VERIFIED"}
                or observed.get("coverage") != {"status": "NOT_OBSERVED"}
                or observed.get("evidence_grade") is not None):
            return False
        receipts = observed["receipts"]
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= 64:
            return False
        # Reapply the consumer's full receipt/binding contract. Hashes prove
        # identity inside local custody; they are never relabeled signatures.
        from specgraph.executor_evidence import project_executor_observation
        evidence = canonical_json_bytes({"schema": "tare.tools/verification-progress/1", "receipts": receipts})
        progress = SimpleNamespace(evidence_bytes=evidence, evidence_sha256=observed["receipt_sha256"],
                                   state_sha256=observed["workspace_state_sha256"],
                                   remaining_obligations=observed["remaining_obligations"],
                                   verified_evidence_count=observed["observed_checks_passed"])
        commands = [SimpleNamespace(criterion=check.criterion, argv=check.command) for check in descriptor.verification_checks]
        checked = project_executor_observation(
            progress, commands=commands, executor=observed["executor"],
            candidate_sha256=candidate_receipt.sha256, descriptor_sha256=descriptor.sha256, max_bytes=65536)
        return (checked == observed and checked["remaining_obligations"] == 0
                and all(item["status"] == "PASS" and item.get("exit_code") == 0 for item in receipts))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return False
