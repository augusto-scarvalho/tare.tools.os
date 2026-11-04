"""Validation/Assurance subject identity, independent from verifier authority."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationSubjectContext:
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_id: str
    subject_root: Path
    base_head: str

    @classmethod
    def from_snapshot(cls, snapshot):
        return cls(snapshot.candidate_tree_id, snapshot.candidate_fingerprint, snapshot.snapshot_id,
                   snapshot.snapshot_path, snapshot.base_head)

    def receipt_fields(self) -> dict[str, str]:
        return {"candidateTreeId": self.candidate_tree_id, "candidateFingerprint": self.candidate_fingerprint,
                "snapshotId": self.snapshot_id, "baseHead": self.base_head}
