import json
from pathlib import Path

import pytest

from relay.landing_policy import (
    LandingPolicy,
    LandingPolicyRejected,
    attest_identity,
    create_evidence,
    default_policy,
    evaluate_admission,
    read_identity,
    read_verdict,
    verify_admission_bundle,
    write_identity,
    write_evidence,
    write_verdict,
)


ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "relay" / "round_table_roster.json"
GREEN_CHECKS = {
    "candidate_immutable": True,
    "scope_complete": True,
    "work_current": True,
    "specgraph_current": True,
    "verification_complete": True,
    "protected_paths_clear": True,
    "secrets_clear": True,
    "cas_ready": True,
}


def identity(role: str, model: str, runtime_key: str, runtime_model: str):
    return attest_identity(
        roster_path=ROSTER,
        agent_id=f"{role}-one",
        role=role,
        model_id=model,
        runtime_key=runtime_key,
        runtime_model=runtime_model,
    )


def evidence(**overrides: bool):
    checks = {**GREEN_CHECKS, **overrides}
    return create_evidence(
        train_id="TRAIN-SMART-ONE",
        descriptor_sha256="1" * 64,
        candidate_sha256="2" * 64,
        work_scope_sha256="3" * 64,
        specgraph_scope_sha256="4" * 64,
        checks=checks,
    )


def implementer():
    return identity("implementer", "gpt-5.6-sol", "codex", "gpt-5.6-sol")


def test_default_is_smart_check_with_optional_recommended_audit():
    verdict = evaluate_admission(evidence(), implementer())

    assert default_policy() == LandingPolicy("optional", True)
    assert verdict.verdict == "ADMITTED"
    assert verdict.payload["audit"] == {
        "present": False,
        "counted": False,
        "passed": None,
        "sha256": "",
        "auditor_identity_sha256": "",
        "independent": False,
        "cross_vendor": False,
        "recommended": True,
    }
    assert verdict.payload["warnings"] == ["INDEPENDENT_AUDIT_RECOMMENDED"]


def test_required_audit_holds_without_provider_call():
    verdict = evaluate_admission(
        evidence(), implementer(), policy=LandingPolicy("required", True)
    )

    assert verdict.verdict == "INCONCLUSIVE"
    assert "INDEPENDENT_AUDIT_REQUIRED" in verdict.payload["reason_codes"]


def test_same_bias_domain_never_counts_even_for_another_model_generation():
    auditor = identity("auditor", "gpt-5.6-terra", "codex", "gpt-5.6-terra")
    verdict = evaluate_admission(
        evidence(),
        implementer(),
        auditor=auditor,
        audit_passed=True,
        audit_sha256="5" * 64,
    )

    assert verdict.verdict == "REFUSED"
    assert verdict.payload["audit"]["independent"] is False
    assert "AUDITOR_NOT_INDEPENDENT" in verdict.payload["reason_codes"]


def test_cross_vendor_independent_audit_is_counted_without_warning():
    auditor = identity(
        "auditor", "gemini-3.7", "google-cli", "gemini-3.7-flash-high"
    )
    verdict = evaluate_admission(
        evidence(),
        implementer(),
        auditor=auditor,
        audit_passed=True,
        audit_sha256="5" * 64,
    )

    assert verdict.verdict == "ADMITTED"
    assert verdict.payload["audit"]["counted"] is True
    assert verdict.payload["audit"]["cross_vendor"] is True
    assert verdict.payload["warnings"] == []


def test_same_vendor_different_bias_is_allowed_but_cross_vendor_is_recommended():
    impl = identity(
        "implementer", "claude-opus-4.8", "claude", "claude-opus-4-8"
    )
    auditor = identity("auditor", "fable-5", "claude", "claude-fable-5")
    verdict = evaluate_admission(
        evidence(),
        impl,
        auditor=auditor,
        audit_passed=True,
        audit_sha256="5" * 64,
    )

    assert verdict.verdict == "ADMITTED"
    assert verdict.payload["audit"]["counted"] is True
    assert verdict.payload["audit"]["independent"] is False  # counted, but never called independent
    assert verdict.payload["audit"]["cross_vendor"] is False
    assert verdict.payload["warnings"] == ["CROSS_VENDOR_AUDITOR_PREFERRED"]


def test_executed_independent_audit_failure_cannot_be_ignored_in_optional_mode():
    auditor = identity(
        "auditor", "gemini-3.7", "google-cli", "gemini-3.7-flash-high"
    )
    verdict = evaluate_admission(
        evidence(),
        implementer(),
        auditor=auditor,
        audit_passed=False,
        audit_sha256="5" * 64,
    )

    assert verdict.verdict == "REFUSED"
    assert "INDEPENDENT_AUDIT_FAILED" in verdict.payload["reason_codes"]


def test_off_mode_ignores_model_audit_but_keeps_smart_checks():
    same_lineage = identity(
        "auditor", "gpt-5.6-terra", "codex", "gpt-5.6-terra"
    )
    verdict = evaluate_admission(
        evidence(),
        implementer(),
        policy=LandingPolicy("off", True),
        auditor=same_lineage,
        audit_passed=False,
        audit_sha256="5" * 64,
    )

    assert verdict.verdict == "ADMITTED"
    assert verdict.payload["audit"]["counted"] is False
    assert verdict.payload["audit"]["recommended"] is False


@pytest.mark.parametrize(
    ("failed_check", "expected"),
    [
        ("candidate_immutable", "REFUSED"),
        ("scope_complete", "REFUSED"),
        ("verification_complete", "REFUSED"),
        ("protected_paths_clear", "REFUSED"),
        ("secrets_clear", "REFUSED"),
        ("cas_ready", "REFUSED"),
        ("work_current", "INCONCLUSIVE"),
        ("specgraph_current", "INCONCLUSIVE"),
    ],
)
def test_smart_checks_fail_closed_by_failure_kind(failed_check: str, expected: str):
    verdict = evaluate_admission(evidence(**{failed_check: False}), implementer())
    assert verdict.verdict == expected


def test_identity_requires_exact_roster_runtime_alias():
    with pytest.raises(LandingPolicyRejected, match="IDENTITY_RUNTIME_ALIAS_MISMATCH"):
        identity("auditor", "gemini-3.7", "google-cli", "unattested-alias")


def test_identity_and_verdict_are_canonical_create_only_receipts(tmp_path: Path):
    impl = implementer()
    verdict = evaluate_admission(evidence(), impl)
    identity_path = tmp_path / "IMPLEMENTER_IDENTITY.json"
    verdict_path = tmp_path / "ADMISSION_VERDICT.json"

    write_identity(identity_path, impl)
    write_verdict(verdict_path, verdict)
    assert read_identity(identity_path).sha256 == impl.sha256
    assert read_verdict(verdict_path).sha256 == verdict.sha256
    with pytest.raises(LandingPolicyRejected, match="IDENTITY_ALREADY_EXISTS"):
        write_identity(identity_path, impl)
    with pytest.raises(LandingPolicyRejected, match="VERDICT_ALREADY_EXISTS"):
        write_verdict(verdict_path, verdict)

    pretty = json.dumps(verdict.payload, indent=2).encode()
    verdict_path.write_bytes(pretty)
    with pytest.raises(LandingPolicyRejected, match="VERDICT_NOT_CANONICAL"):
        read_verdict(verdict_path)


def test_bundle_recomputes_exact_receipts_and_candidate_binding(tmp_path: Path):
    impl = implementer()
    ev = evidence()
    verdict = evaluate_admission(ev, impl)
    write_identity(tmp_path / "IMPLEMENTER_IDENTITY.json", impl)
    write_evidence(tmp_path / "LANDING_EVIDENCE.json", ev)
    write_verdict(tmp_path / "ADMISSION_VERDICT.json", verdict)

    verified = verify_admission_bundle(
        tmp_path,
        roster_path=ROSTER,
        train_id="TRAIN-SMART-ONE",
        descriptor_sha256="1" * 64,
        candidate_sha256="2" * 64,
    )
    assert verified.sha256 == verdict.sha256

    with pytest.raises(
        LandingPolicyRejected, match="EVIDENCE_CANDIDATE_BINDING_MISMATCH"
    ):
        verify_admission_bundle(
            tmp_path,
            roster_path=ROSTER,
            train_id="TRAIN-SMART-ONE",
            descriptor_sha256="1" * 64,
            candidate_sha256="9" * 64,
        )


def test_bundle_rechecks_independent_audit_report_bytes(tmp_path: Path):
    impl = implementer()
    auditor = identity(
        "auditor", "gemini-3.7", "google-cli", "gemini-3.7-flash-high"
    )
    ev = evidence()
    report = tmp_path / "AUDIT_REPORT.md"
    report.write_text("PASS\n", encoding="utf-8")
    report_sha = __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    verdict = evaluate_admission(
        ev,
        impl,
        auditor=auditor,
        audit_passed=True,
        audit_sha256=report_sha,
    )
    write_identity(tmp_path / "IMPLEMENTER_IDENTITY.json", impl)
    write_identity(tmp_path / "AUDITOR_IDENTITY.json", auditor)
    write_evidence(tmp_path / "LANDING_EVIDENCE.json", ev)
    write_verdict(tmp_path / "ADMISSION_VERDICT.json", verdict)

    assert verify_admission_bundle(
        tmp_path,
        roster_path=ROSTER,
        train_id="TRAIN-SMART-ONE",
        descriptor_sha256="1" * 64,
        candidate_sha256="2" * 64,
        audit_passed_validator=lambda: True,
    ).verdict == "ADMITTED"
    report.write_text("CHANGED\n", encoding="utf-8")
    with pytest.raises(LandingPolicyRejected, match="VERDICT_RECOMPUTE_MISMATCH"):
        verify_admission_bundle(
            tmp_path,
            roster_path=ROSTER,
            train_id="TRAIN-SMART-ONE",
            descriptor_sha256="1" * 64,
            candidate_sha256="2" * 64,
            audit_passed_validator=lambda: True,
        )
