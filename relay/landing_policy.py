"""Deterministic landing admission with optional independent model audit.

The OS owns settlement policy. Runtime and Kernel remain evidence producers:
this module consumes their immutable receipts and does not execute providers,
mutate repositories, or infer model identity from a CLI transport.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from relay.train_execution import canonical_json_bytes


POLICY_SCHEMA = "tare.tools/landing-policy/1"
IDENTITY_SCHEMA = "tare.tools/agent-identity/1"
EVIDENCE_SCHEMA = "tare.tools/landing-evidence/1"
DECISION_SCHEMA = "tare.tools/admission-verdict/1"
DEFAULT_AUDIT_MODE = "optional"
IMPLEMENTER_IDENTITY_FILE = "IMPLEMENTER_IDENTITY.json"
AUDITOR_IDENTITY_FILE = "AUDITOR_IDENTITY.json"
EVIDENCE_FILE = "LANDING_EVIDENCE.json"
VERDICT_FILE = "ADMISSION_VERDICT.json"
AUDIT_MODES = frozenset({"off", "optional", "required"})
VERDICTS = frozenset({"ADMITTED", "INCONCLUSIVE", "REFUSED"})

_POLICY_FIELDS = {"schema", "audit_mode", "cross_vendor_preferred"}
_IDENTITY_FIELDS = {
    "schema", "agent_id", "role", "model_id", "runtime_key",
    "runtime_model", "vendor", "bias_domain", "roster_sha256",
}
_EVIDENCE_FIELDS = {
    "schema", "train_id", "descriptor_sha256", "candidate_sha256",
    "work_scope_sha256", "specgraph_scope_sha256", "checks",
}
_CHECK_NAMES = frozenset({
    "candidate_immutable",
    "scope_complete",
    "work_current",
    "specgraph_current",
    "verification_complete",
    "protected_paths_clear",
    "secrets_clear",
    "cas_ready",
})
_HARD_CHECKS = frozenset({
    "candidate_immutable",
    "scope_complete",
    "verification_complete",
    "protected_paths_clear",
    "secrets_clear",
    "cas_ready",
})
_FRESHNESS_CHECKS = frozenset({"work_current", "specgraph_current"})


class LandingPolicyRejected(ValueError):
    """A policy, identity, evidence receipt, or verdict is invalid."""


@dataclass(frozen=True)
class LandingPolicy:
    audit_mode: str = DEFAULT_AUDIT_MODE
    cross_vendor_preferred: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "audit_mode": self.audit_mode,
            "cross_vendor_preferred": self.cross_vendor_preferred,
        }


@dataclass(frozen=True)
class AgentIdentity:
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str

    @property
    def model_id(self) -> str:
        return str(self.payload["model_id"])

    @property
    def vendor(self) -> str:
        return str(self.payload["vendor"])

    @property
    def bias_domain(self) -> str:
        return str(self.payload["bias_domain"])


@dataclass(frozen=True)
class LandingEvidence:
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class AdmissionVerdict:
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str

    @property
    def verdict(self) -> str:
        return str(self.payload["verdict"])


def parse_policy(raw: bytes) -> LandingPolicy:
    payload = _parse_canonical_object(raw, _POLICY_FIELDS, "POLICY")
    if payload.get("schema") != POLICY_SCHEMA:
        raise LandingPolicyRejected("POLICY_SCHEMA_INVALID")
    audit_mode = payload.get("audit_mode")
    if audit_mode not in AUDIT_MODES:
        raise LandingPolicyRejected("AUDIT_MODE_INVALID")
    if payload.get("cross_vendor_preferred") is not True:
        raise LandingPolicyRejected("CROSS_VENDOR_PREFERENCE_MUST_REMAIN_ENABLED")
    return LandingPolicy(audit_mode=audit_mode, cross_vendor_preferred=True)


def default_policy() -> LandingPolicy:
    """Smart deterministic admission, optional model audit, cross-vendor preferred."""

    return LandingPolicy()


def attest_identity(
    *,
    roster_path: str | Path,
    agent_id: str,
    role: str,
    model_id: str,
    runtime_key: str,
    runtime_model: str,
) -> AgentIdentity:
    """Bind identity to an exact roster alias; transport alone is never identity."""

    if role not in {"implementer", "auditor"}:
        raise LandingPolicyRejected("IDENTITY_ROLE_INVALID")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (agent_id, model_id, runtime_key, runtime_model)
    ):
        raise LandingPolicyRejected("IDENTITY_VALUE_INVALID")
    roster_file = Path(roster_path)
    try:
        roster_raw = roster_file.read_bytes()
        roster = json.loads(roster_raw)
        model = roster["models"][model_id]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LandingPolicyRejected("IDENTITY_ROSTER_UNAVAILABLE") from exc
    if not isinstance(model, dict):
        raise LandingPolicyRejected("IDENTITY_MODEL_INVALID")
    runtime = model.get("runtime")
    if not isinstance(runtime, dict) or runtime.get(runtime_key) != runtime_model:
        raise LandingPolicyRejected("IDENTITY_RUNTIME_ALIAS_MISMATCH")
    vendor = model.get("vendor")
    bias_domain = model.get("bias_domain")
    if not all(isinstance(value, str) and value.strip() for value in (vendor, bias_domain)):
        raise LandingPolicyRejected("IDENTITY_LINEAGE_UNAVAILABLE")
    payload = {
        "schema": IDENTITY_SCHEMA,
        "agent_id": agent_id.strip(),
        "role": role,
        "model_id": model_id,
        "runtime_key": runtime_key,
        "runtime_model": runtime_model,
        "vendor": vendor,
        "bias_domain": bias_domain,
        "roster_sha256": hashlib.sha256(roster_raw).hexdigest(),
    }
    return _identity_from_payload(payload)


def model_id_for_runtime(
    roster_path: str | Path, runtime_key: str, runtime_model: str
) -> str:
    """Resolve one exact runtime alias; ambiguity and absence fail closed."""

    try:
        roster = json.loads(Path(roster_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LandingPolicyRejected("IDENTITY_ROSTER_UNAVAILABLE") from exc
    matches = [
        model_id
        for model_id, model in roster.get("models", {}).items()
        if isinstance(model, dict)
        and isinstance(model.get("runtime"), dict)
        and model["runtime"].get(runtime_key) == runtime_model
    ]
    if len(matches) != 1:
        raise LandingPolicyRejected("IDENTITY_RUNTIME_ALIAS_NOT_UNIQUE")
    return matches[0]


def read_identity(path: str | Path) -> AgentIdentity:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise LandingPolicyRejected("IDENTITY_UNAVAILABLE") from exc
    payload = _parse_canonical_object(raw, _IDENTITY_FIELDS, "IDENTITY")
    return _identity_from_payload(payload, expected_raw=raw)


def write_identity(path: str | Path, identity: AgentIdentity) -> None:
    _write_create_only(Path(path), identity.canonical_bytes, "IDENTITY_ALREADY_EXISTS")


def create_evidence(
    *,
    train_id: str,
    descriptor_sha256: str,
    candidate_sha256: str,
    work_scope_sha256: str,
    specgraph_scope_sha256: str,
    checks: Mapping[str, bool],
) -> LandingEvidence:
    if set(checks) != _CHECK_NAMES or any(type(value) is not bool for value in checks.values()):
        raise LandingPolicyRejected("EVIDENCE_CHECKS_INVALID")
    values = (train_id, descriptor_sha256, candidate_sha256, work_scope_sha256,
              specgraph_scope_sha256)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise LandingPolicyRejected("EVIDENCE_IDENTITY_INVALID")
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "train_id": train_id,
        "descriptor_sha256": descriptor_sha256,
        "candidate_sha256": candidate_sha256,
        "work_scope_sha256": work_scope_sha256,
        "specgraph_scope_sha256": specgraph_scope_sha256,
        "checks": {name: checks[name] for name in sorted(checks)},
    }
    raw = canonical_json_bytes(payload)
    return LandingEvidence(payload, raw, hashlib.sha256(raw).hexdigest())


def read_evidence(path: str | Path) -> LandingEvidence:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise LandingPolicyRejected("EVIDENCE_UNAVAILABLE") from exc
    payload = _parse_canonical_object(raw, _EVIDENCE_FIELDS, "EVIDENCE")
    if payload.get("schema") != EVIDENCE_SCHEMA:
        raise LandingPolicyRejected("EVIDENCE_SCHEMA_INVALID")
    checks = payload.get("checks")
    if not isinstance(checks, dict) or set(checks) != _CHECK_NAMES:
        raise LandingPolicyRejected("EVIDENCE_CHECKS_INVALID")
    if any(type(value) is not bool for value in checks.values()):
        raise LandingPolicyRejected("EVIDENCE_CHECKS_INVALID")
    return LandingEvidence(payload, raw, hashlib.sha256(raw).hexdigest())


def write_evidence(path: str | Path, evidence: LandingEvidence) -> None:
    _write_create_only(Path(path), evidence.canonical_bytes, "EVIDENCE_ALREADY_EXISTS")


def evaluate_admission(
    evidence: LandingEvidence,
    implementer: AgentIdentity,
    *,
    policy: LandingPolicy | None = None,
    auditor: AgentIdentity | None = None,
    audit_passed: bool | None = None,
    audit_sha256: str = "",
) -> AdmissionVerdict:
    """Evaluate immutable evidence without provider calls or repository mutation."""

    policy = policy or default_policy()
    if policy.audit_mode not in AUDIT_MODES or not policy.cross_vendor_preferred:
        raise LandingPolicyRejected("POLICY_INVALID")
    if implementer.payload.get("role") != "implementer":
        raise LandingPolicyRejected("IMPLEMENTER_IDENTITY_REQUIRED")
    audit_present = auditor is not None or audit_passed is not None or bool(audit_sha256)
    if audit_present and not (
        auditor is not None and type(audit_passed) is bool and _is_sha256(audit_sha256)
    ):
        raise LandingPolicyRejected("AUDIT_EVIDENCE_INCOMPLETE")
    if auditor is not None and auditor.payload.get("role") != "auditor":
        raise LandingPolicyRejected("AUDITOR_IDENTITY_REQUIRED")

    checks = evidence.payload["checks"]
    failed_hard = sorted(name for name in _HARD_CHECKS if not checks[name])
    failed_freshness = sorted(name for name in _FRESHNESS_CHECKS if not checks[name])
    reason_codes: list[str] = []
    warnings: list[str] = []

    independent = False
    cross_vendor = False
    audit_counted = False
    if auditor is not None:
        same_model = auditor.model_id == implementer.model_id
        same_bias = auditor.bias_domain == implementer.bias_domain
        independent = not same_model and not same_bias
        cross_vendor = auditor.vendor != implementer.vendor
        if policy.audit_mode == "off":
            pass
        elif not independent:
            reason_codes.append("AUDITOR_NOT_INDEPENDENT")
        else:
            audit_counted = True
            if not cross_vendor and policy.cross_vendor_preferred:
                warnings.append("CROSS_VENDOR_AUDITOR_PREFERRED")
            if audit_passed is False:
                reason_codes.append("INDEPENDENT_AUDIT_FAILED")

    if failed_hard:
        verdict = "REFUSED"
        reason_codes.extend(f"CHECK_FAILED:{name}" for name in failed_hard)
    elif failed_freshness:
        verdict = "INCONCLUSIVE"
        reason_codes.extend(f"CHECK_INCONCLUSIVE:{name}" for name in failed_freshness)
    elif "AUDITOR_NOT_INDEPENDENT" in reason_codes:
        verdict = "REFUSED"
    elif "INDEPENDENT_AUDIT_FAILED" in reason_codes:
        verdict = "REFUSED"
    elif policy.audit_mode == "required" and not audit_counted:
        verdict = "INCONCLUSIVE"
        reason_codes.append("INDEPENDENT_AUDIT_REQUIRED")
    else:
        verdict = "ADMITTED"
        reason_codes.append("SMART_CHECKS_PASSED")

    audit_recommended = policy.audit_mode == "optional" and not audit_counted
    if audit_recommended:
        warnings.append("INDEPENDENT_AUDIT_RECOMMENDED")
    payload = {
        "schema": DECISION_SCHEMA,
        "verdict": verdict,
        "reason_codes": sorted(set(reason_codes)),
        "warnings": sorted(set(warnings)),
        "policy": policy.payload(),
        "evidence_sha256": evidence.sha256,
        "implementer_identity_sha256": implementer.sha256,
        "audit": {
            "present": audit_present,
            "counted": audit_counted,
            "passed": audit_passed if audit_present else None,
            "sha256": audit_sha256 if audit_present else "",
            "auditor_identity_sha256": auditor.sha256 if auditor else "",
            "independent": independent,
            "cross_vendor": cross_vendor,
            "recommended": audit_recommended,
        },
    }
    raw = canonical_json_bytes(payload)
    return AdmissionVerdict(payload, raw, hashlib.sha256(raw).hexdigest())


def read_verdict(path: str | Path) -> AdmissionVerdict:
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise LandingPolicyRejected("VERDICT_UNAVAILABLE") from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise LandingPolicyRejected("VERDICT_NOT_CANONICAL")
    if payload.get("schema") != DECISION_SCHEMA or payload.get("verdict") not in VERDICTS:
        raise LandingPolicyRejected("VERDICT_INVALID")
    return AdmissionVerdict(payload, raw, hashlib.sha256(raw).hexdigest())


def write_verdict(path: str | Path, verdict: AdmissionVerdict) -> None:
    _write_create_only(Path(path), verdict.canonical_bytes, "VERDICT_ALREADY_EXISTS")


def verify_identity_against_roster(
    identity: AgentIdentity, roster_path: str | Path
) -> None:
    """Re-attest an identity receipt against the current exact roster bytes."""

    roster_file = Path(roster_path)
    try:
        roster_raw = roster_file.read_bytes()
        roster = json.loads(roster_raw)
        model = roster["models"][identity.model_id]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LandingPolicyRejected("IDENTITY_ROSTER_UNAVAILABLE") from exc
    if hashlib.sha256(roster_raw).hexdigest() != identity.payload["roster_sha256"]:
        raise LandingPolicyRejected("IDENTITY_ROSTER_STALE")
    runtime = model.get("runtime") if isinstance(model, dict) else None
    if (
        not isinstance(runtime, dict)
        or runtime.get(identity.payload["runtime_key"])
        != identity.payload["runtime_model"]
        or model.get("vendor") != identity.vendor
        or model.get("bias_domain") != identity.bias_domain
    ):
        raise LandingPolicyRejected("IDENTITY_ROSTER_MISMATCH")


def verify_admission_bundle(
    train_dir: str | Path,
    *,
    roster_path: str | Path,
    train_id: str,
    descriptor_sha256: str,
    candidate_sha256: str,
    audit_passed_validator: Any | None = None,
) -> AdmissionVerdict:
    """Recompute a stored verdict from its exact receipts before settlement."""

    root = Path(train_dir)
    evidence = read_evidence(root / EVIDENCE_FILE)
    implementer = read_identity(root / IMPLEMENTER_IDENTITY_FILE)
    stored = read_verdict(root / VERDICT_FILE)
    verify_identity_against_roster(implementer, roster_path)
    if (
        evidence.payload["train_id"] != train_id
        or evidence.payload["descriptor_sha256"] != descriptor_sha256
        or evidence.payload["candidate_sha256"] != candidate_sha256
    ):
        raise LandingPolicyRejected("EVIDENCE_CANDIDATE_BINDING_MISMATCH")
    policy_payload = stored.payload.get("policy")
    if not isinstance(policy_payload, dict):
        raise LandingPolicyRejected("VERDICT_POLICY_INVALID")
    policy = parse_policy(canonical_json_bytes(policy_payload))
    audit = stored.payload.get("audit")
    if not isinstance(audit, dict):
        raise LandingPolicyRejected("VERDICT_AUDIT_INVALID")
    auditor = None
    audit_passed = None
    audit_sha256 = ""
    if audit.get("present"):
        auditor = read_identity(root / AUDITOR_IDENTITY_FILE)
        verify_identity_against_roster(auditor, roster_path)
        report = root / "AUDIT_REPORT.md"
        try:
            audit_sha256 = hashlib.sha256(report.read_bytes()).hexdigest()
        except OSError as exc:
            raise LandingPolicyRejected("AUDIT_REPORT_UNAVAILABLE") from exc
        if audit_passed_validator is None:
            raise LandingPolicyRejected("AUDIT_VALIDATOR_REQUIRED")
        audit_passed = bool(audit_passed_validator())
    recomputed = evaluate_admission(
        evidence,
        implementer,
        policy=policy,
        auditor=auditor,
        audit_passed=audit_passed,
        audit_sha256=audit_sha256,
    )
    if recomputed.canonical_bytes != stored.canonical_bytes:
        raise LandingPolicyRejected("VERDICT_RECOMPUTE_MISMATCH")
    if recomputed.verdict != "ADMITTED":
        raise LandingPolicyRejected(f"VERDICT_{recomputed.verdict}")
    return recomputed


def _identity_from_payload(
    payload: Mapping[str, Any], *, expected_raw: bytes | None = None
) -> AgentIdentity:
    if set(payload) != _IDENTITY_FIELDS or payload.get("schema") != IDENTITY_SCHEMA:
        raise LandingPolicyRejected("IDENTITY_FIELDS_INVALID")
    string_fields = _IDENTITY_FIELDS - {"schema"}
    if not all(isinstance(payload.get(name), str) and payload[name].strip() for name in string_fields):
        raise LandingPolicyRejected("IDENTITY_VALUE_INVALID")
    if payload.get("role") not in {"implementer", "auditor"}:
        raise LandingPolicyRejected("IDENTITY_ROLE_INVALID")
    if not _is_sha256(str(payload.get("roster_sha256", ""))):
        raise LandingPolicyRejected("IDENTITY_ROSTER_DIGEST_INVALID")
    raw = canonical_json_bytes(dict(payload))
    if expected_raw is not None and raw != expected_raw:
        raise LandingPolicyRejected("IDENTITY_NOT_CANONICAL")
    return AgentIdentity(dict(payload), raw, hashlib.sha256(raw).hexdigest())


def _parse_canonical_object(raw: bytes, fields: set[str], label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LandingPolicyRejected(f"{label}_JSON_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != fields:
        raise LandingPolicyRejected(f"{label}_FIELDS_INVALID")
    if raw != canonical_json_bytes(payload):
        raise LandingPolicyRejected(f"{label}_NOT_CANONICAL")
    return payload


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _write_create_only(path: Path, raw: bytes, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LandingPolicyRejected(reason) from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
