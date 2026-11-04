"""Hermetic case lifecycle and runner-artifact bridge; never starts an auditor."""
import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay.audit_cases import AuditCases, checksum, json_bytes, main


ROOT = Path(__file__).resolve().parents[1]


def test_frugality_projection_v2_and_v1_unknown_metrics():
    from relay.audit_cases import frugality
    state = {'schema': 'tare.tools/audit-case/2', 'case_id': 'successor',
             'status': 'REJECTED', 'anchor': {'reopen': {'case_id': 'old', 'reason': 'New candidate'}},
             'attempts': [
                 {'status': 'REJECTED', 'result': {'metrics': {'duration_ms': {'value': 17},
                     'provider_token_usage': {'model': {'inputTokens': 12, 'outputTokens': 3}},
                     'cost_estimate_usd': {'value': 0.25}}, 'review': {'secret': 'never print'}}},
                 {'status': 'RUNNING', 'packet': {'secret': 'never print'}}]}
    original = deepcopy(state)
    value = frugality(state)
    assert state == original and value['authority'] == 'NONE'
    assert value['reopen']['reason'] == 'New candidate' and value['needs_adjudication']
    assert (value['attempts'][0]['duration_ms'], value['attempts'][0]['token_count'],
            value['attempts'][0]['cost_estimate_usd']) == (17, 15, 0.25)
    assert all(value['attempts'][1][key] is None for key in ('duration_ms', 'token_count', 'cost_estimate_usd'))
    assert 'secret' not in json.dumps(value)
    state['schema'] = 'tare.tools/audit-case/1'
    assert frugality(state)['attempts'][0]['token_count'] is None



@pytest.fixture
def cases(tmp_path):
    return AuditCases(tmp_path / "cases", ROOT / "relay" / "round_table_roster.json")


@pytest.fixture
def request_data():
    return {
        "case_id": "finite-review", "project": "example", "stage": "code",
        "exploration": None,  # Existing packet/legacy transport fixtures.
        "objective": "Fix the scoped defect", "non_goals": ["Redesign"],
        "obligations": ["Regression falsifier passes", "No scope expansion"],
        "candidate": {"commit": "a" * 40, "tree": "b" * 40},
        "scope": {"nodes": ["fix-1"], "trains": []},
        "candidate_artifacts": [{"path": "fix.py", "git_blob": "c" * 40,
                                 "sha256": "d" * 64, "size_bytes": 12,
                                 "role": "candidate-artifact", "provenance": "git-tree:" + "b" * 40}],
        "test_execution_sha256": "e" * 64,
        "implementer": {"agent_id": "impl", "model_id": "gpt-5.6-sol",
                        "runtime_key": "codex", "runtime_model": "gpt-5.6-sol"},
        "auditor": {"agent_id": "review", "model_id": "fable-5",
                    "runtime_key": "claude", "runtime_model": "claude-fable-5"},
    }


def bundle(tmp_path, opened, verdict="QUALIFIED_BOUNDED", blockers=None, failed=False, peak=500):
    run = tmp_path / ("run-" + str(opened["attempt"]))
    run.mkdir()
    request = opened["runner_request"]
    session = "session-one"
    decision = {"schema": "tare.tools/audit-verdict/3.0", "role": "independent-auditor",
                "candidate_tree": request["candidate"]["tree"],
                **{key: request[key] for key in ("scope", "candidate_artifacts", "test_execution_sha256")},
                "auditor_profile": "finite-review", "audit_method": "targeted tests",
                "verdict": verdict, "blocking_findings": blockers or [], "residual_findings": ["Accepted limitation"],
                "issued_at": "2026-09-06T12:00:00Z"}
    result = {"type": "result", "is_error": failed, "subtype": "error" if failed else "success",
              "session_id": session, "structured_output": decision}
    raw = {"result": json.dumps(result, indent=3).encode() + b"\n",
           "decision": json_bytes(decision), "report": b"Original report\r\n", "memory": b"Auditor memory\r\n"}
    manifest = {"feature": request["feature"], "session_id": session, "run_id": run.name,
                "execution_status": "failed" if failed else "completed", "result_valid": not failed,
                "decision": None if failed else verdict, "error": "quota_exhausted" if failed else None,
                "artifacts": {}, "artifact_sha256": {}, "peak_context_tokens": peak}
    for key, value in raw.items():
        name = key + (".md" if key in {"report", "memory"} else ".json")
        (run / name).write_bytes(value)
        # Paths may name a different host. Consumer must use the explicit bundle.
        manifest["artifacts"][key] = "Z:/remote/private/" + name
        manifest["artifact_sha256"][key] = checksum(value)
    (run / "manifest.json").write_bytes(json_bytes(manifest))
    (run / "launch.json").write_bytes(json_bytes({"candidate": request["candidate"],
        "session_id": session, "arguments": ["claude", "--model", "claude-fable-5"]}))
    (run / "prompt.txt").write_bytes(b"Finite audit\nEXACT REQUEST:\n" + json_bytes(request))
    return run


def change_json(path, **updates):
    value = json.loads(path.read_bytes())
    value.update(updates)
    path.write_bytes(json_bytes(value))


@pytest.mark.parametrize('artifact', ['manifest', 'launch', 'decision', 'result'])
def test_record_deep_artifacts_fail_typed_without_state_mutation(cases, request_data, tmp_path, capsys, artifact):
    run = bundle(tmp_path, cases.begin(request_data))
    depth = 10000
    raw = b'[' * depth + b']' * depth
    (run / (artifact + '.json')).write_bytes(raw)
    if artifact in {'decision', 'result'}:
        manifest = json.loads((run / 'manifest.json').read_bytes())
        manifest['artifact_sha256'][artifact] = checksum(raw)
        (run / 'manifest.json').write_bytes(json_bytes(manifest))
    before = cases.path(request_data['case_id']).read_bytes()
    assert main(['record', '--store', str(cases.store), '--roster', str(cases.roster),
                 '--case', request_data['case_id'], '--attempt', '1', '--run', str(run)]) == 1
    assert json.loads(capsys.readouterr().out) == {'authority': 'NONE', 'error': 'artifact_json_too_deep'}
    assert cases.path(request_data['case_id']).read_bytes() == before


def continuation(request):
    request = deepcopy(request)
    request["continuation"] = {"kind": "correction", "reason": "Added the regression falsifier",
                               "witness_sha256": "f" * 64,
                               "affected_obligations": [request["obligations"][0]]}
    return request


def test_accept_closes_without_authority_and_preserves_original_bytes(cases, request_data, tmp_path):
    opened = cases.begin(request_data)
    run = bundle(tmp_path, opened)
    result = cases.record(request_data["case_id"], 1, run)
    assert result["status"] == "CLOSED"
    assert result["authority"] == "NONE"
    stored = cases.load(request_data["case_id"])
    for name, artifact in stored["attempts"][0]["artifacts"].items():
        assert base64.b64decode(artifact["bytes"]) == (run / name).read_bytes()
    assert base64.b64decode(stored["memory"]["bytes"]) == b"Auditor memory\r\n"
    with pytest.raises(ValueError, match="case_closed"):
        cases.begin(continuation(request_data), resume=True)
    with pytest.raises(ValueError, match="case_already_exists"):
        cases.begin(request_data)


@pytest.mark.parametrize("verdict,blockers", [("REJECTED", ["Defect"]), ("NO_GO", []),
                                               ("QUALIFIED_BOUNDED", ["Unresolved"])] )
def test_merit_rejection_resumes_same_case_without_inheriting_approval(cases, request_data, tmp_path, verdict, blockers):
    run = bundle(tmp_path, cases.begin(request_data), verdict, blockers)
    assert cases.record(request_data["case_id"], 1, run)["status"] == "REJECTED"
    with pytest.raises(ValueError, match="material_continuation"):
        cases.begin(request_data, resume=True)
    opened = cases.begin(continuation(request_data), resume=True)
    assert opened["runner_action"] == "resume" and opened["session_id"] == "session-one"
    assert opened["decision"] is None
    run = bundle(tmp_path, opened)
    assert cases.record(request_data["case_id"], 2, run)["status"] == "CLOSED"


@pytest.mark.parametrize("key,value", [("stage", "plan"), ("objective", "New goal"),
                                       ("obligations", ["Invented obligation"]),
                                       ("non_goals", []), ("scope", {"nodes": ["another"], "trains": []})])
def test_resume_freezes_anchor(cases, request_data, tmp_path, key, value):
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"])
    cases.record(request_data["case_id"], 1, run)
    changed = continuation(request_data)
    changed[key] = value
    with pytest.raises(ValueError, match="case_scope_changed"):
        cases.begin(changed, resume=True)


def test_new_revision_requires_new_evidence_not_old_result(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"])
    cases.record(request_data["case_id"], 1, run)
    changed = continuation(request_data)
    changed["candidate"]["commit"] = "f" * 40
    cases.begin(changed, resume=True)
    with pytest.raises(ValueError, match="request_binding_mismatch"):
        cases.record(request_data["case_id"], 2, run)


@pytest.mark.parametrize("peak,limit", [(120001, 4), (500, 1)])
def test_bounded_stop_is_inconclusive_not_rejection_or_approval(cases, request_data, tmp_path, peak, limit):
    request_data["limits"] = {"attempts": limit, "context_tokens": 120000}
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"], peak=peak)
    result = cases.record(request_data["case_id"], 1, run)
    assert result["status"] == "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
    assert result["decision"] == "REJECTED" and result["revisit_trigger"]
    with pytest.raises(ValueError, match="case_closed"):
        cases.begin(continuation(request_data), resume=True)


def test_execution_failure_is_not_merit_and_requires_explicit_recovery(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data), failed=True)
    result = cases.record(request_data["case_id"], 1, run)
    assert result["status"] == "EXECUTION_FAILED" and result["decision"] is None
    assert result["execution_error"] == "quota_exhausted"
    recovery = continuation(request_data)
    recovery["continuation"]["kind"] = "execution_recovery"
    assert cases.begin(recovery, resume=True)["runner_action"] == "resume"


def test_successor_preserves_closed_case_and_requires_reason(cases, request_data, tmp_path, capsys):
    run = bundle(tmp_path, cases.begin(request_data))
    cases.record(request_data["case_id"], 1, run)
    original = cases.path(request_data["case_id"]).read_bytes()
    successor = deepcopy(request_data)
    successor["case_id"] = "new-evidence"
    successor["reopen"] = {"case_id": request_data["case_id"]}
    with pytest.raises(ValueError, match="reopen_reason"):
        cases.begin(successor)
    successor["reopen"]["reason"] = "New counterexample"
    assert cases.begin(successor)["runner_action"] == "run"
    assert cases.path(request_data["case_id"]).read_bytes() == original
    before = cases.path("new-evidence").read_bytes()
    assert main(["frugality", "--store", str(cases.store), "--case", "new-evidence"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reopen"]["reason"] == "New counterexample"
    assert report["attempts"][0]["duration_ms"] is None
    assert report["status"] == "RUNNING" and report["authority"] == "NONE"
    assert cases.path("new-evidence").read_bytes() == before


@pytest.mark.parametrize("conflict", ["same_identity", "alias_mismatch", "same_fable_lineage"])
def test_independence_and_unknown_alias_fail_closed(cases, request_data, conflict):
    if conflict == "same_identity":
        request_data["auditor"] = request_data["implementer"]
    elif conflict == "same_fable_lineage":
        request_data["implementer"].update(model_id="fable-5", runtime_key="claude",
                                           runtime_model="claude-fable-5")
        request_data["auditor"].update(model_id="fable-5.1", runtime_model="claude-fable-5-1")
    else:
        request_data["auditor"]["runtime_model"] = "claude-fable-5-1"
    with pytest.raises(ValueError, match="independent_lineage|ALIAS_MISMATCH"):
        cases.begin(request_data)
    assert not cases.path(request_data["case_id"]).exists()


@pytest.mark.parametrize("name", ["result.json", "decision.json"])
def test_hash_mismatch_cannot_close(cases, request_data, tmp_path, name):
    run = bundle(tmp_path, cases.begin(request_data))
    (run / name).write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash_mismatch"):
        cases.record(request_data["case_id"], 1, run)
    assert cases.load(request_data["case_id"])["status"] == "RUNNING"


@pytest.mark.parametrize("value", [[], "text", 1, None, True])
@pytest.mark.parametrize("name", ["result", "decision"])
def test_import_non_object_has_typed_error_and_preserves_case(
        cases, request_data, tmp_path, capsys, value, name):
    run = bundle(tmp_path, cases.begin(request_data))
    raw = json_bytes(value)
    (run / (name + ".json")).write_bytes(raw)
    manifest = json.loads((run / "manifest.json").read_bytes())
    manifest["artifact_sha256"][name] = checksum(raw)
    (run / "manifest.json").write_bytes(json_bytes(manifest))
    before = cases.path(request_data["case_id"]).read_bytes()
    assert main(["record", "--store", str(cases.store), "--case", request_data["case_id"],
                 "--attempt", "1", "--run", str(run)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "provider_result_not_object", "authority": "NONE"}
    assert cases.path(request_data["case_id"]).read_bytes() == before


@pytest.mark.parametrize("filename,updates", [("manifest.json", {"session_id": "wrong"}),
                                             ("manifest.json", {"feature": "other"}),
                                             ("launch.json", {"arguments": ["claude", "--model", "wrong"]})])
def test_bindings_fail_closed(cases, request_data, tmp_path, filename, updates):
    run = bundle(tmp_path, cases.begin(request_data))
    change_json(run / filename, **updates)
    with pytest.raises(ValueError, match="mismatch"):
        cases.record(request_data["case_id"], 1, run)


def test_missing_memory_is_warning_not_invalidated_decision(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data))
    (run / "memory.md").unlink()
    result = cases.record(request_data["case_id"], 1, run)
    assert result["status"] == "CLOSED" and result["warnings"] == ["memory.md:not_imported"]


def test_lock_and_duplicate_record_prevent_double_effects(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data))
    lock = cases.store / ".active.lock"
    lock.write_text("interrupted operator-owned process")
    with pytest.raises(FileExistsError):
        cases.record(request_data["case_id"], 1, run)
    assert lock.read_text() == "interrupted operator-owned process"
    lock.unlink()
    cases.record(request_data["case_id"], 1, run)
    with pytest.raises(ValueError, match="stale_or_completed"):
        cases.record(request_data["case_id"], 1, run)


def test_corrupt_state_does_not_restart(cases, request_data):
    cases.begin(request_data)
    cases.path(request_data["case_id"]).write_bytes(b"bad json")
    with pytest.raises(ValueError):
        cases.begin(continuation(request_data), resume=True)


def test_cli_outputs_only_status_and_no_private_logs(cases, request_data, tmp_path, capsys):
    run = bundle(tmp_path, cases.begin(request_data))
    (run / "private.stream.jsonl").write_text("DO NOT READ")
    cases.record(request_data["case_id"], 1, run)
    assert main(["status", "--store", str(cases.store), "--case", request_data["case_id"]]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "CLOSED"
    assert "artifacts" not in value and "DO NOT READ" not in str(value)


def test_artifact_tampering_in_store_detected(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data))
    cases.record(request_data["case_id"], 1, run)
    state = cases.load(request_data["case_id"])
    state["attempts"][0]["artifacts"]["result.json"]["bytes"] = base64.b64encode(b"forged").decode()
    cases.path(request_data["case_id"]).write_bytes(json_bytes(state))
    with pytest.raises(ValueError, match="stored_artifact_changed"):
        cases.load(request_data["case_id"])


def test_interrupted_case_can_stop_but_late_result_cannot_close(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data))
    with pytest.raises(ValueError, match="hold_reason"):
        cases.hold(request_data["case_id"], 1, "", None)
    result = cases.hold(request_data["case_id"], 1, "Producer interruption diagnosed")
    assert result["status"] == "INCONCLUSIVE_WITH_REVISIT_TRIGGER" and result["decision"] is None
    with pytest.raises(ValueError, match="stale_or_completed"):
        cases.record(request_data["case_id"], 1, run)


def test_cli_open_record_result_end_to_end(cases, request_data, tmp_path, capsys):
    request_path = tmp_path / "case-request.json"
    request_path.write_bytes(json_bytes(request_data))
    common = ["--store", str(cases.store)]
    assert main(["open", *common, "--request", str(request_path)]) == 0
    opened = json.loads(capsys.readouterr().out)
    run = bundle(tmp_path, opened)
    assert main(["record", *common, "--case", request_data["case_id"], "--attempt", "1", "--run", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "CLOSED"
    assert main(["result", *common, "--case", request_data["case_id"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runner_request"] == opened["runner_request"]
    assert result["auditor_decision"]["verdict"] == "QUALIFIED_BOUNDED"


@pytest.mark.parametrize("field,value", [("role", "implementer"), ("audit_method", ""),
                                       ("issued_at", "2026-09-06"), ("blocking_findings", "none")])
def test_structured_decision_is_validated_even_with_matching_hashes(cases, request_data, tmp_path, field, value):
    run = bundle(tmp_path, cases.begin(request_data))
    decision = json.loads((run / "decision.json").read_bytes())
    decision[field] = value
    change_json(run / "result.json", structured_output=decision)
    (run / "decision.json").write_bytes(json_bytes(decision))
    manifest = json.loads((run / "manifest.json").read_bytes())
    for key in ("result", "decision"):
        manifest["artifact_sha256"][key] = checksum((run / (key + ".json")).read_bytes())
    (run / "manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError):
        cases.record(request_data["case_id"], 1, run)


def test_attempt_budget_exact_boundary(cases, request_data, tmp_path):
    request_data["limits"] = {"attempts": 2, "context_tokens": 500}
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"], peak=500)
    assert cases.record(request_data["case_id"], 1, run)["status"] == "REJECTED"
    run = bundle(tmp_path, cases.begin(continuation(request_data), resume=True), "REJECTED", ["Defect"], peak=500)
    assert cases.record(request_data["case_id"], 2, run)["status"] == "INCONCLUSIVE_WITH_REVISIT_TRIGGER"


def test_correction_needs_no_recopied_anchor_or_unverified_witness(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"])
    cases.record(request_data["case_id"], 1, run)
    opened = cases.begin({"case_id": request_data["case_id"],
                          "continuation": {"kind": "correction", "reason": "Added regression test"}}, resume=True)
    assert opened["runner_action"] == "resume"
    assert cases.load(request_data["case_id"])["anchor"]["obligations"] == request_data["obligations"]


def test_unknown_context_warns_but_does_not_force_bureaucratic_reopening(cases, request_data, tmp_path):
    run = bundle(tmp_path, cases.begin(request_data), "REJECTED", ["Defect"], peak=None)
    result = cases.record(request_data["case_id"], 1, run)
    assert result["status"] == "REJECTED" and "context_usage_unknown" in result["warnings"]
    assert cases.begin(continuation(request_data), resume=True)["attempt"] == 2
