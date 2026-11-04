"""TRAIN-07-TCP-06 falsifiers: Genesis activation gate, capability credential,
lease manager with deadlock prevention, and assignment boundary.

All fixtures live in isolated scratch roots (packet invariant: no production
trust seeding; the real trust store / work graph / relay stay untouched)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

RELAY_DIR = Path(__file__).resolve().parents[1]
HARNESS_LIB_LOCAL = RELAY_DIR / "scripts" / "harness_lib"
HARNESS_SCRIPTS_LOCAL = RELAY_DIR / "scripts"
HARNESS_LIB_REMOTE = Path(os.environ.get(
    "HARNESS_REPO_DIR", "C:/projects/universal-agent-harness-prototype")) / "scripts" / "harness_lib"
HARNESS_SCRIPTS_REMOTE = HARNESS_LIB_REMOTE.parent

for p in [str(HARNESS_SCRIPTS_LOCAL), str(HARNESS_LIB_LOCAL),
          str(HARNESS_LIB_REMOTE), str(HARNESS_SCRIPTS_REMOTE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from harness_lib import bootstrap_envelope as be  # noqa: E402
from harness_lib import decision_inbox  # noqa: E402
from harness_lib import genesis_activation as ga  # noqa: E402
from harness_lib import governance_receipt as gr  # noqa: E402
from harness_lib import sandbox_spawn as sbx  # noqa: E402
from harness_lib import tcp01_controller_root as tcp01  # noqa: E402
from harness_lib import trusted_ci as tci  # noqa: E402
from harness_lib import trusted_verifier as tv  # noqa: E402

TASK = "tcp06-demo-task"
AGENT = "agent-alpha"
SUBJECT = "fp-genesis-1"
TREE = "tree-genesis-1"
DECISION_ID = "DEC-GENESIS-1"
PY = sys.executable


def _record():
    return tv.TrustedVerifierRecord(
        "TV-GEN-1", TREE, "v1", "epoch-1", "2026-08-16T00:00:00+00:00", "owner", None,
        "evidence:test", "CANDIDATE", "HEAD", tv.STRICT_PROOF_PROFILE,
        ("tiny",), ("trusted-harness-test",), ("policy-v1",),
        {"decisionId": DECISION_ID,
         "decisionDigest": decision_inbox.decision_digest_v2(
             DECISION_ID, "escalation", "promote genesis verifier", TREE)})


def build_evidence(root: Path) -> dict:
    """Mint authentic, fresh evidence for all five seams in a scratch root."""
    # TCP-03: canonical test decision store
    content = "promote genesis verifier"
    row = {"id": DECISION_ID, "kind": "escalation", "content": content, "subject": TREE,
           "status": "resolved", "choice": "promote-verifier",
           "purpose": "trusted-verifier-promotion", "actor": {"role": "TEST_ONLY_OWNER"},
           **decision_inbox.decision_identity_record(DECISION_ID, "escalation", content, TREE)}
    store = root / ".harness" / "state-store" / "test-promotion-decisions.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"records": [row]}), encoding="utf-8")

    # TCP-01: root+child envelope, frozen registry, hash-chained ledger
    t0 = time.time()
    root_env = be.mint_root("owner", [TASK], max_children=4, issued_at=t0)
    child = be.derive_child(root_env, TASK, now=t0 + 1)
    controller = tcp01.ControllerRoot(root_env, root / "ledger.jsonl")
    controller.register("status", lambda: "ok", mutating=False)
    registry_digest = controller.freeze()
    controller.dispatch("status")

    # TCP-02: completed governance receipt
    receipt = gr.GovernanceExecutionReceipt(
        execution_id="EXEC-gen-1", kind="validation", run_id="r1", fingerprint=SUBJECT,
        status="completed", exit_code=0, ended_at=gr.now(),
        execution_context={"strictValidationContextDigest": "sha256:ctx-gen"})
    receipt.write(gr.get_receipt_path(root, "validation", SUBJECT))

    # TCP-04: confinement evidence
    sbx.mint_confinement_evidence(
        root, {"fsEnforced": True, "fsApplied": ["AGENTS.md"], "fsBackend": "nt-icacls"},
        execution_id="SX-gen-1", subject_fingerprint=SUBJECT)

    # TCP-05: hermetic CI receipt bound to the verifier + confinement link
    record = _record()
    ws = root / "ws"
    ws.mkdir(exist_ok=True)
    out = tci.run_hermetic([PY, "-c", "print('ok')"], root=root, workspace=ws,
                           subject_fingerprint=SUBJECT, timeout=120,
                           confinement_execution_id="SX-gen-1",
                           verifier_fields={"trustedVerifierId": record.trusted_verifier_id,
                                            "verifierTreeId": record.verifier_tree_id,
                                            "proofProfileDigest": record.proof_profile_digest})
    return {
        "controllerRoot": {"rootEnvelope": root_env, "childEnvelope": child,
                           "ledgerPath": str(root / "ledger.jsonl"),
                           "registryDigest": registry_digest},
        "provenanceBinding": {"reference": receipt.stamp_reference(),
                              "expectedContextDigest": "sha256:ctx-gen"},
        "filesystemConfinement": {"executionId": "SX-gen-1"},
        "trustedCI": {"executionId": out["executionId"]},
    }


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "scratch-root"
    root.mkdir()
    evidence = build_evidence(root)
    return root, evidence, _record(), ga.GenesisActivationGate(root)


def _eval(gate, evidence, record, **kw):
    return gate.evaluate(task_id=TASK, agent_id=AGENT, subject_fingerprint=SUBJECT,
                         evidence=evidence, record=record, test_only=True, **kw)


def test_five_authentic_seams_yield_one_deterministic_certificate(env):
    root, evidence, record, gate = env
    ev = _eval(gate, evidence, record)
    assert ev["verdict"] == "GENESIS_ELIGIBLE", ev["seams"]
    cert = gate.mint_certificate(ev, evidence)
    cert2 = gate.mint_certificate(_eval(gate, evidence, record), evidence)
    assert cert["certificateId"] == cert2["certificateId"], "minting must be deterministic"
    ok, why = gate.verify_certificate(cert["certificateId"], task_id=TASK, agent_id=AGENT,
                                      subject_fingerprint=SUBJECT, record=record, test_only=True)
    assert ok, why


def test_each_missing_seam_is_ineligible_and_mints_nothing(env):
    root, evidence, record, gate = env
    for seam in ("controllerRoot", "provenanceBinding", "filesystemConfinement", "trustedCI"):
        partial = {k: v for k, v in evidence.items() if k != seam}
        ev = _eval(gate, partial, record)
        assert ev["verdict"] == "GENESIS_INELIGIBLE", f"4/5 with missing {seam} must fail"
        assert ev["seams"][seam]["state"] == "FAIL"
        with pytest.raises(ga.GenesisRefused):
            gate.mint_certificate(ev, partial)


def test_caller_pass_booleans_cannot_fill_a_seam(env):
    root, evidence, record, gate = env
    forged = dict(evidence)
    forged["filesystemConfinement"] = {"executionId": "SX-nonexistent", "state": "PASS", "ok": True}
    ev = _eval(gate, forged, record)
    assert ev["seams"]["filesystemConfinement"]["state"] == "FAIL"
    assert ev["verdict"] == "GENESIS_INELIGIBLE"


def test_tcp01_falsifiers(env):
    root, evidence, record, gate = env
    e = json.loads(json.dumps(evidence))  # deep copy (envelopes are plain dicts)
    e["controllerRoot"]["childEnvelope"]["task_id"] = "other-task"
    assert _eval(gate, e, record)["seams"]["controllerRoot"]["state"] == "FAIL"  # out-of-scope
    e = json.loads(json.dumps(evidence))
    e["controllerRoot"]["childEnvelope"]["seq"] = 99
    assert _eval(gate, e, record)["seams"]["controllerRoot"]["state"] == "FAIL"  # out-of-range
    # ledger tamper
    ledger = Path(evidence["controllerRoot"]["ledgerPath"])
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(lines[0].replace('"status"', '"forged"') + "\n", encoding="utf-8")
    assert _eval(gate, evidence, record)["seams"]["controllerRoot"]["state"] == "FAIL"


def test_tcp02_falsifiers(env):
    root, evidence, record, gate = env
    e = json.loads(json.dumps(evidence))
    e["provenanceBinding"]["reference"]["executionId"] = "EXEC-other"
    assert _eval(gate, e, record)["seams"]["provenanceBinding"]["state"] == "FAIL"  # swap
    # stale execution
    ev = _eval(gate, evidence, record, now=time.time() + 7200)
    assert ev["seams"]["provenanceBinding"]["state"] == "FAIL"


def test_tcp03_falsifiers(env):
    root, evidence, record, gate = env
    store = root / ".harness" / "state-store" / "test-promotion-decisions.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["records"][0]["consumedBy"] = "TV-OTHER"  # replayed/consumed decision
    store.write_text(json.dumps(data), encoding="utf-8")
    ev = _eval(gate, evidence, record)
    assert ev["seams"]["canonicalDecision"]["state"] == "FAIL"
    assert ev["verdict"] == "GENESIS_INELIGIBLE"


def test_tcp04_and_tcp05_falsifiers(env):
    root, evidence, record, gate = env
    # override/bypass confinement is never PASS
    sbx.mint_confinement_evidence(root, {"fsEnforced": True, "fsApplied": ["x"],
                                         "sandboxBypassed": True},
                                  execution_id="SX-gen-1", subject_fingerprint=SUBJECT)
    ev = _eval(gate, evidence, record)
    assert ev["seams"]["filesystemConfinement"]["state"] == "FAIL"
    assert ev["seams"]["trustedCI"]["state"] == "FAIL"  # CI links the same refused evidence
    # wrong verifier binding on CI (receipt shopping)
    other = tv.TrustedVerifierRecord(
        "TV-OTHER", TREE, "v1", "e", "2026-08-16T00:00:00+00:00", "owner", None,
        "evidence:test", "CANDIDATE", "HEAD", tv.STRICT_PROOF_PROFILE,
        ("tiny",), ("trusted-harness-test",), ("policy-v1",), record.promotion_receipt)
    ev2 = _eval(gate, evidence, other)
    assert ev2["seams"]["trustedCI"]["state"] == "FAIL"


def test_test_only_fixtures_never_production_eligible(env):
    root, evidence, record, gate = env
    ev = _eval(gate, evidence, record)
    assert ev["verdict"] == "GENESIS_ELIGIBLE"
    assert ev["authority"] == "TEST_ONLY"
    assert ev["productionEligible"] is False, "falsifier 8: TEST_ONLY can never be production"
    cert = gate.mint_certificate(ev, evidence)
    assert cert["productionEligible"] is False


def test_certificate_tamper_and_replay_refused(env):
    root, evidence, record, gate = env
    cert = gate.mint_certificate(_eval(gate, evidence, record), evidence)
    cid = cert["certificateId"]
    ok, why = gate.verify_certificate(cid, task_id="other-task", agent_id=AGENT,
                                      subject_fingerprint=SUBJECT, record=record, test_only=True)
    assert not ok and "taskId" in why
    ok, why = gate.verify_certificate(cid, task_id=TASK, agent_id="agent-eve",
                                      subject_fingerprint=SUBJECT, record=record, test_only=True)
    assert not ok and "agentId" in why
    path = root / ga.CERT_DIR_REL / f"{cid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["productionEligible"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    ok, why = gate.verify_certificate(cid, task_id=TASK, agent_id=AGENT,
                                      subject_fingerprint=SUBJECT, record=record, test_only=True)
    assert not ok and "tamper" in why


def _manifest(root: Path) -> Path:
    path = root / "capability-manifest.json"
    path.write_text(json.dumps({"capabilities": {"fs-read": {"tools": ["Read"], "version": 1},
                                                 "py-run": {"tools": ["Bash"], "version": 2}}}),
                    encoding="utf-8")
    return path


def _credential(root: Path, evidence: dict, manifest: Path, **over):
    caps = {cap: ga._digest(json.loads(manifest.read_text())["capabilities"][cap])
            for cap in ("fs-read", "py-run")}
    kw = dict(agent_id=AGENT, role="implementer", task_id=TASK,
              subject_fingerprint=SUBJECT, capabilities=caps,
              child_envelope=evidence["controllerRoot"]["childEnvelope"],
              issuer_identity="harness-owner", decision_ref="records/decisions#D999")
    kw.update(over)
    return ga.issue_capability_credential(**kw)


def test_credential_falsifiers(env):
    root, evidence, record, gate = env
    manifest = _manifest(root)
    root_env = evidence["controllerRoot"]["rootEnvelope"]
    good = _credential(root, evidence, manifest)
    ok, why = ga.verify_capability_credential(good, agent_id=AGENT, role="implementer",
                                              task_id=TASK, subject_fingerprint=SUBJECT,
                                              manifest_path=manifest, root_envelope=root_env)
    assert ok, why
    # wrong agent / wildcard / drift / no-envelope / no-capabilities / bad issuer
    checks = [
        (dict(good, agentId="agent-eve"), "agentId"),
        (dict(good, taskId="*"), "taskId"),
        ({**good, "childEnvelope": None}, "authority envelope"),
        (dict(good, issuerIdentity="candidate-self"), "digest mismatch"),  # digest broke on edit
    ]
    for cred, expect in checks:
        ok, why = ga.verify_capability_credential(cred, agent_id=AGENT, role="implementer",
                                                  task_id=TASK, subject_fingerprint=SUBJECT,
                                                  manifest_path=manifest, root_envelope=root_env)
        assert not ok, f"{expect}: {why}"
    # manifest drift after issuing
    data = json.loads(manifest.read_text())
    data["capabilities"]["py-run"]["version"] = 3
    manifest.write_text(json.dumps(data), encoding="utf-8")
    ok, why = ga.verify_capability_credential(good, agent_id=AGENT, role="implementer",
                                              task_id=TASK, subject_fingerprint=SUBJECT,
                                              manifest_path=manifest, root_envelope=root_env)
    assert not ok and "drift" in why
    # valid manifest, missing owner envelope authority (issued fresh w/o envelope)
    no_auth = _credential(root, evidence, _manifest(root), child_envelope={})
    ok, why = ga.verify_capability_credential(no_auth, agent_id=AGENT, role="implementer",
                                              task_id=TASK, subject_fingerprint=SUBJECT,
                                              manifest_path=manifest, root_envelope=root_env)
    assert not ok
    # authority without capability evidence
    no_caps = _credential(root, evidence, _manifest(root), capabilities={})
    ok, why = ga.verify_capability_credential(no_caps, agent_id=AGENT, role="implementer",
                                              task_id=TASK, subject_fingerprint=SUBJECT,
                                              manifest_path=manifest, root_envelope=root_env)
    assert not ok and "no capabilities" in why


def test_lease_race_one_winner_and_rollback(env):
    root, _, _, _ = env
    mgr = ga.GenesisLeaseManager(root)
    first = mgr.acquire(agent_id="agent-a", task_id=TASK, subject_fingerprint=SUBJECT,
                        resources=["res-1", "res-2"], certificate_digest="c", credential_digest="d")
    with pytest.raises(ga.GenesisRefused):
        mgr.acquire(agent_id="agent-b", task_id=TASK, subject_fingerprint=SUBJECT,
                    resources=["res-2", "res-3"], certificate_digest="c", credential_digest="d")
    # loser holds NOTHING (full rollback of res-3 partial)
    assert not (root / ga.LEASE_DIR_REL / "res-3.lease.json").exists()
    # non-owner release/steal refused
    with pytest.raises(ga.GenesisRefused):
        mgr.release("res-1", agent_id="agent-b", token="forged")
    mgr.release("res-1", agent_id="agent-a", token=first["holderToken"])
    mgr.release("res-2", agent_id="agent-a", token=first["holderToken"])


def test_wait_for_cycle_detected_and_refused(env):
    root, _, _, _ = env
    mgr = ga.GenesisLeaseManager(root)
    mgr.acquire(agent_id="agent-a", task_id=TASK, subject_fingerprint=SUBJECT,
                resources=["res-A"], certificate_digest="c", credential_digest="d")
    mgr.acquire(agent_id="agent-b", task_id=TASK, subject_fingerprint=SUBJECT,
                resources=["res-B"], certificate_digest="c", credential_digest="d")
    # pending intent: a wants B (blocked by b)
    (root / ga.LEASE_DIR_REL / "agent-a.intent.json").write_text(
        json.dumps({"agentId": "agent-a", "resources": ["res-B"], "token": "t"}), encoding="utf-8")
    # b requesting A would close the cycle a->b->a
    with pytest.raises(ga.GenesisRefused, match="cycle"):
        mgr.acquire(agent_id="agent-b", task_id=TASK, subject_fingerprint=SUBJECT,
                    resources=["res-A"], certificate_digest="c", credential_digest="d")
    # existing holders were not deleted
    assert (root / ga.LEASE_DIR_REL / "res-A.lease.json").exists()
    assert (root / ga.LEASE_DIR_REL / "res-B.lease.json").exists()


def test_expiry_cas_reclaim_and_crash_reconcile(env):
    root, _, _, _ = env
    mgr = ga.GenesisLeaseManager(root)
    out = mgr.acquire(agent_id="agent-a", task_id=TASK, subject_fingerprint=SUBJECT,
                      resources=["res-X"], certificate_digest="c", credential_digest="d",
                      ttl_seconds=0)  # instantly expired
    time.sleep(0.01)
    out2 = mgr.acquire(agent_id="agent-b", task_id=TASK, subject_fingerprint=SUBJECT,
                       resources=["res-X"], certificate_digest="c", credential_digest="d")
    lease = json.loads((root / ga.LEASE_DIR_REL / "res-X.lease.json").read_text(encoding="utf-8"))
    assert lease["agentId"] == "agent-b" and lease["revision"] == 2  # CAS reclaim exactly once
    # stale-revision renew refused
    with pytest.raises(ga.GenesisRefused):
        mgr.renew("res-X", agent_id="agent-b", token=out2["holderToken"], revision=1)
    # crash simulation: partial acquisition = lease + surviving intent
    (root / ga.LEASE_DIR_REL / "res-P.lease.json").write_text(json.dumps(
        {"agentId": "agent-c", "resource": "res-P", "holderToken": "tokP",
         "revision": 1, "expiresAt": time.time() + 900}), encoding="utf-8")
    (root / ga.LEASE_DIR_REL / "agent-c.intent.json").write_text(json.dumps(
        {"agentId": "agent-c", "resources": ["res-P", "res-Q"], "token": "tokP"}), encoding="utf-8")
    dropped = mgr.reconcile()
    assert dropped == ["res-P"], "partial acquisition must vanish on reconciliation"
    assert mgr.reconcile() == []  # idempotent
    # corrupt lease fails closed and is quarantined
    (root / ga.LEASE_DIR_REL / "res-C.lease.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ga.GenesisRefused, match="corrupt"):
        mgr.acquire(agent_id="agent-d", task_id=TASK, subject_fingerprint=SUBJECT,
                    resources=["res-C"], certificate_digest="c", credential_digest="d")


def test_assignment_boundary_zero_handler_bytes_on_refusal(env):
    root, evidence, record, gate = env
    manifest = _manifest(root)
    root_env = evidence["controllerRoot"]["rootEnvelope"]
    cred = _credential(root, evidence, manifest)
    ran = []
    # refusal path: break one seam
    broken = {k: v for k, v in evidence.items() if k != "trustedCI"}
    with pytest.raises(ga.GenesisRefused):
        ga.assign_task(root, task_id=TASK, agent_id=AGENT, role="implementer",
                       subject_fingerprint=SUBJECT, evidence=broken, record=record,
                       credential=cred, manifest_path=manifest, root_envelope=root_env,
                       resources=["res-M"], handler=lambda: ran.append(1), test_only=True)
    assert ran == [], "zero handler bytes on refusal"
    assert not (root / ga.LEASE_DIR_REL / "res-M.lease.json").exists()
    # green path: exactly one certificate, lease set, receipt, handler ran once
    out = ga.assign_task(root, task_id=TASK, agent_id=AGENT, role="implementer",
                         subject_fingerprint=SUBJECT, evidence=evidence, record=record,
                         credential=cred, manifest_path=manifest, root_envelope=root_env,
                         resources=["res-M"], handler=lambda: ran.append(1) or "done",
                         test_only=True)
    assert ran == [1] and out["result"] == "done"
    assert out["receipt"]["certificateDigest"] == out["certificate"]["certificateDigest"]


def test_production_genesis_preconditions_preserved():
    # UNKNOWN remains ineligible; the registry keeps ALL preconditions.
    report = tv.genesis_precondition_report()
    assert set(report["preconditions"]) == set(tv.GENESIS_PRECONDITIONS)
    assert report["genesisEligible"] is False
