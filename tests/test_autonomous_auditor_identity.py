import json
from pathlib import Path

import pytest

from relay import autonomous_auditor, relay_mesh
from relay.landing_policy import attest_identity, write_identity


ROOT = Path(__file__).resolve().parents[1]


def _strict_train(tmp_path: Path, model_id: str, runtime_key: str, runtime_model: str):
    relay_dir = tmp_path / "relay"
    train = relay_dir / "trains" / "TRAIN-ONE"
    train.mkdir(parents=True)
    roster = relay_dir / "round_table_roster.json"
    roster.write_bytes((ROOT / "relay" / "round_table_roster.json").read_bytes())
    (train / "STATE.json").write_text(
        json.dumps({"execution_descriptor": {"sha256": "1" * 64}}),
        encoding="utf-8",
    )
    identity = attest_identity(
        roster_path=roster,
        agent_id="impl",
        role="implementer",
        model_id=model_id,
        runtime_key=runtime_key,
        runtime_model=runtime_model,
    )
    write_identity(train / "IMPLEMENTER_IDENTITY.json", identity)
    return relay_dir, train


def test_strict_audit_router_excludes_same_openai_bias_domain(tmp_path, monkeypatch):
    relay_dir, train = _strict_train(
        tmp_path, "gpt-5.6-sol", "codex", "gpt-5.6-sol"
    )
    monkeypatch.setattr(relay_mesh, "RELAY_DIR", relay_dir)

    implementer, candidates = autonomous_auditor._strict_audit_routing(
        train, "code-audit"
    )

    assert implementer.bias_domain == "openai/gpt"
    assert "codex" not in candidates
    assert set(candidates) == {"antigravity", "claude"}


def test_strict_audit_router_excludes_same_google_bias_domain(tmp_path, monkeypatch):
    relay_dir, train = _strict_train(
        tmp_path,
        "gemini-3.7-medium",
        "google-cli",
        "gemini-3.7-flash-medium",
    )
    monkeypatch.setattr(relay_mesh, "RELAY_DIR", relay_dir)

    implementer, candidates = autonomous_auditor._strict_audit_routing(
        train, "code-audit"
    )

    assert implementer.bias_domain == "google/gemini"
    assert "antigravity" not in candidates
    assert set(candidates) == {"codex", "claude"}


def test_unattested_vendor_alias_is_skipped_without_poisoning_other_auditors(
    tmp_path, monkeypatch
):
    relay_dir, train = _strict_train(
        tmp_path, "gpt-5.6-sol", "codex", "gpt-5.6-sol"
    )
    monkeypatch.setattr(relay_mesh, "RELAY_DIR", relay_dir)
    monkeypatch.setenv("AGY_MODEL", "unknown-unattested-model")

    _implementer, candidates = autonomous_auditor._strict_audit_routing(
        train, "code-audit"
    )

    assert set(candidates) == {"claude"}


def test_composed_prompt_returns_finite_scope_and_sources(tmp_path):
    (tmp_path / "PACKET.md").write_text("task-exact: bounded objective", encoding="utf-8")
    prompt = autonomous_auditor._compose("code-audit", tmp_path, "Verify the fix")
    assert isinstance(prompt, str)
    assert "task-exact" in prompt and "Verify the fix" in prompt
    assert "accepted limitations" in prompt and "never inherited approval" in prompt


@pytest.mark.parametrize("fresh,host,expected", [(False, "here", "original-session"),
                                               (True, "here", None), (False, "elsewhere", None)])
def test_audit_actually_uses_exact_stage_session(tmp_path, monkeypatch, fresh, host, expected):
    relay_mesh = autonomous_auditor.relay_mesh
    relay_dir, train = _strict_train(tmp_path, "gpt-5.6-sol", "codex", "gpt-5.6-sol")
    (train / "IMPLEMENTER_IDENTITY.json").rename(train / "PLANNER_IDENTITY.json")
    trains = train.parent
    monkeypatch.setattr(relay_mesh, "RELAY_DIR", relay_dir)
    prompt = train / "prompt.txt"
    prompt.write_text("Audit this bounded plan", encoding="utf-8")
    (train / "PACKET.md").write_text("Task one", encoding="utf-8")
    monkeypatch.setattr(relay_mesh, "TRAINS_DIR", trains)
    monkeypatch.setattr(relay_mesh, "get_hostname", lambda: "here")
    monkeypatch.setattr(relay_mesh, "audit_valid", lambda *a: (True, "valid"))
    monkeypatch.setattr(autonomous_auditor, "TRACKER_FILE", tmp_path / "tracker.json")
    autonomous_auditor._save_tracker({
        "TRAIN-ONE::plan-audit": {"conversation_id": "original-session", "host": host, "rounds": 1},
        "TRAIN-ONE::code-audit": {"conversation_id": "other-stage", "host": "here", "rounds": 1},
    })
    calls = []
    def transport(composed, conversation):
        calls.append(conversation)
        return {"status": "SUCCESS", "response": "PLAN_VERDICT: APPROVED", "conversation_id": "returned"}
    monkeypatch.setattr(autonomous_auditor, "_run_agy", transport)
    assert autonomous_auditor.audit("plan-audit", prompt, fresh=fresh) == 0
    assert calls == [expected]


def test_report_is_not_rewritten_to_manufacture_gate_compliance(tmp_path, monkeypatch):
    relay_mesh = autonomous_auditor.relay_mesh
    relay_dir, train = _strict_train(tmp_path, "gpt-5.6-sol", "codex", "gpt-5.6-sol")
    trains = train.parent
    monkeypatch.setattr(relay_mesh, "RELAY_DIR", relay_dir)
    prompt = train / "prompt.txt"
    prompt.write_text("Audit code", encoding="utf-8")
    monkeypatch.setattr(relay_mesh, "TRAINS_DIR", trains)
    monkeypatch.setattr(autonomous_auditor, "TRACKER_FILE", tmp_path / "tracker.json")
    monkeypatch.setattr(relay_mesh, "audit_valid", lambda *a: (False, "missing required heading"))
    monkeypatch.setattr(autonomous_auditor, "_run_agy", lambda *a: {
        "status": "SUCCESS", "response": "AUDIT_VERDICT: VERIFIED_PASS", "conversation_id": "one"})
    assert autonomous_auditor.audit("code-audit", prompt) == 1
    assert (train / "AUDIT_REPORT.md").read_text(encoding="utf-8") == "AUDIT_VERDICT: VERIFIED_PASS\n"


@pytest.mark.parametrize("stage", ["code-audit", "plan-audit"])
def test_legacy_audit_still_excludes_subject_producer(tmp_path, monkeypatch, stage):
    relay_dir, train = _strict_train(tmp_path, "gemini-3.7-medium", "google-cli",
                                     "gemini-3.7-flash-medium")
    (train / "STATE.json").write_text("{}", encoding="utf-8")
    if stage == "plan-audit":
        (train / "IMPLEMENTER_IDENTITY.json").rename(train / "PLANNER_IDENTITY.json")
    monkeypatch.setattr(autonomous_auditor.relay_mesh, "RELAY_DIR", relay_dir)
    author, candidates = autonomous_auditor._strict_audit_routing(train, stage)
    assert author.bias_domain == "google/gemini"
    assert set(candidates) == {"codex", "claude"}


@pytest.mark.parametrize("stage", ["code-audit", "plan-audit"])
def test_missing_producer_identity_refuses_before_any_dispatch(tmp_path, monkeypatch, stage):
    train = tmp_path / "trains" / "TRAIN-ONE"
    train.mkdir(parents=True)
    prompt = train / "prompt.txt"
    prompt.write_text("Audit", encoding="utf-8")
    monkeypatch.setattr(autonomous_auditor.relay_mesh, "TRAINS_DIR", train.parent)
    def forbidden(*a, **kw):
        pytest.fail("unattested subject reached an auditor")
    monkeypatch.setattr(autonomous_auditor, "_run_agy", forbidden)
    monkeypatch.setattr(autonomous_auditor.vendor_chairs, "dispatch_role", forbidden)
    assert autonomous_auditor.audit(stage, prompt) == 1
    assert not (train / autonomous_auditor.STAGE_REPORT[stage]).exists()


def test_compose_includes_diff_and_evidence_tails(tmp_path):
    for name in ["PACKET.md", "DIFF.patch", "IMPLEMENTATION_EVIDENCE.md", "PLAN_AUDIT_REPORT.md"]:
        (tmp_path / name).write_text("á" * 20000 + "SENTINEL_" + name, encoding="utf-8")
    prompt = autonomous_auditor._compose("code-audit", tmp_path, "Verify")
    for name in ["PACKET.md", "DIFF.patch", "IMPLEMENTATION_EVIDENCE.md", "PLAN_AUDIT_REPORT.md"]:
        assert "SENTINEL_" + name in prompt


@pytest.mark.parametrize("stage,name,mutation", [
    ("code-audit", "DIFF.patch", "edit"),
    ("code-audit", "IMPLEMENTATION_EVIDENCE.md", "delete"),
    ("code-audit", "DIFF.patch", "create"),
    ("plan-audit", "PACKET.md", "edit"),
    ("code-audit", "PACKET.md", "delete"),
])
def test_source_mutation_prevents_publishing_pass(tmp_path, monkeypatch, stage, name, mutation):
    relay_dir, train = _strict_train(tmp_path, "gpt-5.6-sol", "codex", "gpt-5.6-sol")
    if stage == "plan-audit":
        (train / "IMPLEMENTER_IDENTITY.json").rename(train / "PLANNER_IDENTITY.json")
    prompt = train / "prompt.txt"
    prompt.write_text("Audit", encoding="utf-8")
    target = train / name
    if mutation != "create":
        target.write_text("original", encoding="utf-8")
    mesh = autonomous_auditor.relay_mesh
    monkeypatch.setattr(mesh, "TRAINS_DIR", train.parent)
    monkeypatch.setattr(mesh, "RELAY_DIR", relay_dir)
    monkeypatch.setattr(autonomous_auditor, "TRACKER_FILE", tmp_path / "tracker.json")
    def transport(*a):
        if mutation == "delete":
            target.unlink()
        else:
            target.write_text("changed", encoding="utf-8")
        return {"status": "SUCCESS", "response": "AUDIT_VERDICT: VERIFIED_PASS", "conversation_id": "c"}
    monkeypatch.setattr(autonomous_auditor, "_run_agy", transport)
    assert autonomous_auditor.audit(stage, prompt) == 1
    assert not (train / autonomous_auditor.STAGE_REPORT[stage]).exists()
