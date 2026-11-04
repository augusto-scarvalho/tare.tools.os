"""Local Git/process falsifiers for bounded packets; no vendor calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from compute_plane.audit_driver import ClaudeAuditDriver
from relay.audit_cases import json_bytes, main
from relay.audit_execution import execute_packet
from relay.audit_packet import prepare, verify, recheck, validate_review, correction, prompt
from test_audit_execution import candidate
from test_audit_cases import cases, request_data


@pytest.fixture
def selection(candidate, request_data):
    root, receipt = candidate
    (root / "fix.py").write_text("answer = 1\n", encoding="utf-8")
    return {"packet_id": "fix-r1", "mode": "advisory", "intent": "Correct answer",
        "scope": "answer behavior", "non_goals": ["Redesign"],
        "repositories": [{"id": "os", "root": str(root), "base": request_data["candidate"]["commit"],
            "target": "WORKTREE", "changes": [{"id": "C1", "path": "fix.py", "why": "Correct answer"}]}],
        "obligations": [{"id": "O1", "claim": "Answer is correct", "change_ids": ["C1"],
                         "falsify": "Assert answer equals two"}],
        "evidence": [{"id": "T1", "path": str(receipt), "why": "Regression receipt",
                      "obligation_ids": ["O1"]}]}


@pytest.mark.parametrize('version, mapped', [(1, False), (2, False), (2, True)])
def test_shared_rubric_reaches_transport_prompt(selection, version, mapped, monkeypatch):
    from relay.blocker_rubric import BLOCKER_RUBRIC
    selection['review_schema_version'] = version
    if version == 2:
        selection['obligations'][0]['threat_model'] = {'actor': 'user', 'window': 'runtime', 'access': 'API'}
    if mapped:
        # This test targets prompt selection, not the separately tested map validator.
        monkeypatch.setattr('relay.audit_preflight.validate_map', lambda packet: None)
        selection['review_map'] = {}
    packet = prepare(selection)
    assert BLOCKER_RUBRIC in packet['input']['context']['review_guidance']
    assert BLOCKER_RUBRIC.encode() in prompt(packet)


def review(packet, verdict="CHANGES_REQUESTED"):
    failed = verdict == "CHANGES_REQUESTED"
    return {"schema": "tare.tools/audit-review/1", "packet_id": packet["input"]["packet_id"],
        "verdict": verdict, "coverage": [{"obligation_id": "O1", "status": "FAILED" if failed else "CHECKED",
            "finding_ids": ["F1"] if failed else [], "evidence_refs": ["C1.after", "T1"]}],
        "findings": [{"id": "F1", "obligation_id": "O1", "severity": "P1", "category": "regression",
            "location": {"source_ref": "C1.after", "start_line": 1, "end_line": 1},
            "claim": "Answer is one", "minimal_fix": "Use two",
            "falsifier": {"kind": "test", "execution": "PROPOSED", "setup": "Import module",
                "action": "Assert answer equals two", "expected": "Two", "observed_or_predicted": "One"}}] if failed else [],
        "context_requests": [], "residuals": [], "frugality_notes": []}


class PacketProvider(ClaudeAuditDriver):
    def __init__(self, verdict="CHANGES_REQUESTED", failure=None, mutation=None):
        super().__init__([sys.executable])
        self.calls, self.verdict, self.failure, self.mutation = [], verdict, failure, mutation

    def run(self, command, *, root, prompt, timeout, on_output, cancelled=None, on_stderr=None):
        self.calls.append(command)
        assert list(Path(root).iterdir()) == []
        data = json.loads(prompt.split(b"\n", 1)[1])
        value = review({"input": data}, self.verdict)
        if self.mutation:
            self.mutation(value)
        assistant = {"type": "assistant", "session_id": command[-1], "message": {
            "id": "msg-fixture", "model": command[command.index("--model") + 1], "content": [{
                "type": "tool_use", "name": "StructuredOutput", "id": "tool-fixture", "input": value}]}}
        on_output(json_bytes(assistant) + b"\n")
        if self.failure == "timeout":
            raise TimeoutError()
        terminal = {"type": "result", "session_id": command[-1],
            "subtype": "error_max_budget_usd" if self.failure == "budget" else "success",
            "is_error": self.failure is not None, "structured_output": None if self.failure else value,
            "modelUsage": {assistant["message"]["model"]: {}, "auxiliary-fixture": {}}}
        raw = json_bytes(terminal) + b"\n"
        on_output(raw)
        return int(self.failure is not None), raw


def test_packet_cleanup_timeout_is_recorded(selection, cases, request_data):
    class Broken(PacketProvider):
        def run(self, command, **kwargs):
            raise subprocess.TimeoutExpired('provider cleanup', 5)
    cases.begin_packet(prepare(selection), request_data)
    result = run(cases, request_data, Broken())
    assert result['status'] == 'EXECUTION_FAILED'
    stored = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert stored['execution_status'] == 'FAILED' and stored['reason'].startswith('TimeoutExpired:')


def run(cases, request_data, driver):
    return execute_packet(cases, request_data["case_id"], len(cases.load(request_data["case_id"])["attempts"]),
                          driver=driver, max_usd=.1, effort="xhigh", timeout=5)


def test_prepare_deterministic_and_bytes_rechecked(selection):
    packet = prepare(selection)
    assert prepare(selection) == packet
    assert len(prompt(packet)) < 24000
    damaged = deepcopy(packet)
    damaged["input"]["intent"] = "Other"
    with pytest.raises(ValueError, match="packet_bytes_changed"):
        verify(damaged)
    (Path(selection["repositories"][0]["root"]) / "fix.py").write_text("answer = 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="packet_source_changed"):
        recheck(packet)


@pytest.mark.parametrize('malformed', ['line_suffix', 'checked_findings', 'unchecked_findings'])
def test_provider_schema_rejects_known_format_failures(selection, malformed):
    from jsonschema import Draft7Validator, ValidationError
    from relay.audit_packet import review_schema
    from compute_plane.audit_driver import packet_wire_schema
    packet = prepare(selection)
    value = review(packet)
    schema = packet_wire_schema(review_schema(packet))
    assert not {'allOf', 'anyOf', 'oneOf'} & schema.keys()
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    validator.validate(value)
    validator.validate(review(packet, 'ACCEPTED_BOUNDED'))
    if malformed == 'line_suffix': value['coverage'][0]['evidence_refs'] = ['C1.after:1-2']
    elif malformed == 'checked_findings': value['coverage'][0]['status'] = 'CHECKED'
    elif malformed == 'unchecked_findings': value['coverage'][0]['status'] = 'NOT_CHECKED'
    with pytest.raises(ValidationError): validator.validate(value)


def test_output_schema_is_frozen_and_legacy_budget_unchanged(selection, cases, request_data):
    from relay.audit_packet import review_schema, budget, seal
    from compute_plane.audit_driver import packet_wire_schema
    packet = prepare(selection)
    legacy = deepcopy(packet); legacy.pop('review_output_schema'); legacy = seal(legacy)
    verify(legacy)
    assert review_schema(legacy) == review_schema()
    assert budget(legacy)['initial_context_bytes'] < budget(packet)['initial_context_bytes']
    cases.begin_packet(packet, request_data)
    driver = PacketProvider()
    run(cases, request_data, driver)
    command = driver.calls[0]
    assert json.loads(command[command.index('--json-schema') + 1]) == packet_wire_schema(packet['review_output_schema'])


def test_complete_context_limit_is_explicit_not_silent_truncation(selection):
    from relay.audit_packet import budget
    selection['intent'] = 'x' * 25000
    with pytest.raises(ValueError, match='audit_prompt_over_budget'): prepare(selection)
    selection['max_initial_context_bytes'] = 64000
    packet = prepare(selection)
    assert 24000 < budget(packet)['initial_context_bytes'] < 64000
    assert packet['input']['intent'] == selection['intent']
    selection['max_initial_context_bytes'] = 64001
    with pytest.raises(ValueError, match='invalid_preflight_input_limit'): prepare(selection)


def test_undeclared_changes_and_large_context_refused(selection):
    root = Path(selection["repositories"][0]["root"])
    (root / "other.py").write_text("new\n", encoding="utf-8")
    with pytest.raises(ValueError, match="undeclared"):
        prepare(selection)
    selection["repositories"][0]["excluded"] = [{"path": "other.py", "reason": "Separate feature"}]
    prepare(selection)
    (root / "fix.py").write_text("x" * 25000, encoding="utf-8")
    with pytest.raises(ValueError, match="over_budget"):
        prepare(selection)


def test_index_does_not_include_unstaged_bytes(selection):
    root = selection["repositories"][0]["root"]
    subprocess.run(["git", "-C", root, "add", "fix.py"], check=True)
    (Path(root) / "fix.py").write_text("unstaged = 7\n", encoding="utf-8")
    selection["repositories"][0]["target"] = "INDEX"
    packet = prepare(selection)
    after = next(source for source in packet["input"]["sources"] if source["id"] == "C1.after")
    assert after["text"] == "answer = 1\n"


def test_new_deleted_and_multi_repository(selection, tmp_path):
    root = Path(selection["repositories"][0]["root"])
    (root / "fix.py").rename(root / "renamed.py")
    selection["repositories"][0]["changes"].append({"id": "C2", "path": "renamed.py", "why": "Rename"})
    selection["obligations"][0]["change_ids"].append("C2")
    packet = prepare(selection)
    delta = next(s for s in packet['input']['sources'] if s['id'] == 'C2.diff')
    assert 'source C2.after' in delta['text'] and 'answer = 1' not in delta['text']
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--no-hardlinks", str(root), str(other)], check=True, capture_output=True)
    (other / "fix.py").write_text("answer = 3\n", encoding="utf-8")
    selection["repositories"].append({"id": "kernel", "root": str(other),
        "base": selection["repositories"][0]["base"], "target": "WORKTREE",
        "changes": [{"id": "C3", "path": "fix.py", "why": "Related transport"}]})
    selection["obligations"][0]["change_ids"].append("C3")
    packet = prepare(selection)
    assert {change["kind"] for change in packet["input"]["changes"]} == {"added", "deleted", "modified"}
    assert len(packet["repositories"]) == 2


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(coverage=[]),
    lambda value: value["coverage"][0].update(evidence_refs=["invented"]),
    lambda value: value["findings"][0]["location"].update(end_line=999),
    lambda value: value["findings"][0]["falsifier"].update(execution="EXECUTED"),
    lambda value: value.update(verdict="ACCEPTED_BOUNDED"),
    lambda value: value.update(packet_id="wrong"),
    lambda value: value["coverage"].append(deepcopy(value["coverage"][0])),
])
def test_invalid_review_never_accepted(selection, mutation):
    packet = prepare(selection)
    value = review(packet)
    validate_review(packet, value)
    mutation(value)
    with pytest.raises(ValueError):
        validate_review(packet, value)


@pytest.mark.parametrize("failure,status", [("budget", "BUDGET_STOP"), ("timeout", "TIMED_OUT")])
def test_draft_survives_failed_terminal_without_transcript(selection, cases, request_data, failure, status):
    packet = prepare(selection)
    cases.begin_packet(packet, request_data)
    assert run(cases, request_data, PacketProvider(failure=failure))["status"] == "EXECUTION_FAILED"
    state = cases.load(request_data["case_id"])
    result = state["attempts"][-1]["result"]
    assert result["execution_status"] == status and result["review_status"] == "PARTIAL"
    assert result["review"]["findings"][0]["id"] == "F1"
    assert result["next_action"] == "IMPLEMENT_FINDINGS"
    assert result["authority"] == "NONE"
    assert Path(state["attempts"][-1]["execution"]["run"], "draft.json").exists()
    assert len(json_bytes(cases.summary(state))) <= 2048
    assert "artifacts" not in result


@pytest.mark.parametrize("model_id,runtime_model", [
    ("fable-5", "claude-fable-5"),
    ("fable-5.1", "claude-fable-5-1"),
])
def test_two_rounds_reuse_session_with_real_correction(selection, cases, request_data, model_id, runtime_model):
    request_data["auditor"].update(model_id=model_id, runtime_model=runtime_model)
    first_packet = prepare(selection)
    cases.begin_packet(first_packet, request_data)
    driver = PacketProvider()
    assert run(cases, request_data, driver)["status"] == "REJECTED"
    selection["packet_id"] = "fix-r2"
    root = Path(selection["repositories"][0]["root"])
    (root / "fix.py").write_text("answer = 2\n", encoding="utf-8")
    second = prepare(selection)
    reply = {"schema": "tare.tools/audit-correction/1", "packet_id": "fix-r1",
        "dispositions": [{"finding_id": "F1", "disposition": "FIXED", "change_refs": ["C1"],
                          "test_receipt_refs": ["T1"], "reason": ""}]}
    cases.begin_packet(second, {"case_id": request_data["case_id"],
        "continuation": {"reason": "Corrected answer"}, "correction": reply}, resume=True)
    state = cases.load(request_data['case_id'])
    saved = state['attempts'][-1]['packet']
    assert saved['previous_review_archive'] == state['attempts'][0]['result']['review']
    assert saved['input']['previous_review']['schema'] == 'tare.tools/audit-review-navigation/1'
    assert saved['input']['previous_review']['findings'][0]['claim'] == saved['previous_review_archive']['findings'][0]['claim']
    driver.verdict = "ACCEPTED_BOUNDED"
    assert run(cases, request_data, driver)["status"] == "CLOSED"
    assert "--session-id" in driver.calls[0] and "--resume" in driver.calls[1]
    assert driver.calls[0][-1] == driver.calls[1][-1]
    assert driver.calls[1][driver.calls[1].index("--tools") + 1] == ""
    assert driver.calls[1][driver.calls[1].index("--effort") + 1] == "xhigh"
    for command in driver.calls:
        assert command[command.index("--model") + 1] == runtime_model
        assert "--fallback-model" not in command


def test_fixed_without_actual_delta_is_refused(selection):
    first = prepare(selection)
    selection["packet_id"] = "fix-r2"
    selection["repositories"][0]["changes"][0]["why"] = "Claimed corrected"
    reply = {"schema": "tare.tools/audit-correction/1", "packet_id": "fix-r1", "dispositions": [{
        "finding_id": "F1", "disposition": "FIXED", "change_refs": ["C1"], "test_receipt_refs": ["T1"], "reason": ""}]}
    with pytest.raises(ValueError, match="empty_delta"):
        correction(prepare(selection), first, review(first), reply)


@pytest.mark.parametrize('mutation', ['drop', 'claim', 'falsify', 'unlink'])
@pytest.mark.parametrize('disposition', ['DISPUTED', 'DEFERRED'])
def test_correction_retains_original_obligations(selection, mutation, disposition):
    root = Path(selection['repositories'][0]['root'])
    (root / 'extra.py').write_text('extra = 1\n', encoding='utf-8')
    selection['repositories'][0]['changes'].append({'id': 'C2', 'path': 'extra.py', 'why': 'Second change'})
    selection['obligations'].append({'id': 'O2', 'claim': 'Original second obligation',
                                    'falsify': 'Original falsifier', 'change_ids': ['C1', 'C2']})
    first = prepare(selection)
    prior = review(first)
    prior['coverage'].append({'obligation_id': 'O2', 'status': 'CHECKED',
                             'finding_ids': [], 'evidence_refs': ['C1.after']})
    selection['packet_id'] = 'fix-r2'
    if mutation == 'drop':
        selection['obligations'].pop()
        selection['obligations'][0]['change_ids'].append('C2')
    elif mutation == 'unlink':
        selection['obligations'][1]['change_ids'] = ['C2']
    else:
        selection['obligations'][1][mutation] = 'Rewritten obligation'
    reply = {'schema': 'tare.tools/audit-correction/1', 'packet_id': 'fix-r1',
             'dispositions': [{'finding_id': 'F1', 'disposition': disposition,
                'change_refs': [], 'test_receipt_refs': ['T1'], 'reason': 'Disposition'}]}
    with pytest.raises(ValueError, match='correction_scope_changed'):
        correction(prepare(selection), first, prior, reply)


def test_invalid_provider_review_is_typed_and_does_not_close(selection, cases, request_data):
    cases.begin_packet(prepare(selection), request_data)
    driver = PacketProvider("ACCEPTED_BOUNDED", mutation=lambda value: value.update(coverage=[]))
    assert run(cases, request_data, driver)["status"] == "EXECUTION_FAILED"
    assert cases.load(request_data["case_id"])["attempts"][-1]["result"]["review_status"] == "INVALID"


def test_cli_prepare_and_handoff(selection, cases, request_data, tmp_path, capsys):
    selected = tmp_path / "selection.json"
    selected.write_bytes(json_bytes(selection))
    path = tmp_path / "packet.json"
    assert main(["prepare", "--store", str(cases.store), "--request", str(selected), "--output", str(path)]) == 0
    capsys.readouterr()
    cases.begin_packet(json.loads(path.read_bytes()), request_data)
    run(cases, request_data, PacketProvider())
    assert main(["handoff", "--store", str(cases.store), "--case", request_data["case_id"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "tare.tools/audit-result/1" and "artifacts" not in result


@pytest.mark.parametrize("cost_settings", [{}, {"max_usd": .1}])
def test_real_background_process_returns_run_id_and_publishes_handoff(selection, cases, request_data, tmp_path, cost_settings):
    import time
    from relay.audit_execution import launch_packet
    request_data["auditor"].update(model_id="fable-5.1", runtime_model="claude-fable-5-1")
    packet = prepare(selection)
    cases.begin_packet(packet, request_data)
    script = tmp_path / "fake_stream.py"
    script.write_text(
        "import json,sys,time\n"
        "data=json.loads(sys.stdin.buffer.read().split(b'\\n',1)[1])\n"
        "time.sleep(.5)\n"
        "review=" + repr(review(packet)) + "\n"
        "session=sys.argv[-1]; model=sys.argv[sys.argv.index('--model')+1]\n"
        "print(json.dumps(dict(type='assistant',session_id=session,message=dict(id='msg-fake',model=model,"
        "content=[dict(type='tool_use',name='StructuredOutput',input=review)]))),flush=True)\n"
        "print(json.dumps(dict(type='result',session_id=session,subtype='success',is_error=False,"
        "structured_output=review)),flush=True)\n", encoding="utf-8")
    started = time.monotonic()
    launched = launch_packet(cases, request_data["case_id"], 1,
        driver=ClaudeAuditDriver([sys.executable, str(script)]), effort="xhigh", timeout=5, **cost_settings)
    assert launched["dispatch_status"] == "LAUNCHED" and launched["run_id"]
    assert time.monotonic() - started < 3
    with pytest.raises(ValueError, match="already_dispatched"):
        launch_packet(cases, request_data["case_id"], 1, driver=PacketProvider(), max_usd=.1, effort="xhigh")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = cases.load(request_data["case_id"])
        if state["status"] != "RUNNING":
            break
        time.sleep(.1)
    assert state["status"] == "REJECTED", list((cases.store / "dispatches").iterdir())
    result = json.loads(Path(launched["result_path"]).read_bytes())
    assert result["review_status"] == "VALID"
    assert result["primary_model_reported"] == request_data["auditor"]["runtime_model"]
    launch = json.loads(Path(launched["result_path"]).with_name("launch.json").read_bytes())
    assert launch["max_usd_soft"] == result["metrics"]["max_usd_soft"] == cost_settings.get("max_usd")
    assert ("--max-budget-usd" in launch["arguments"]) == bool(cost_settings)


def test_cancel_is_cooperative_and_typed(selection, cases, request_data):
    class Cancelled(PacketProvider):
        def run(self, command, *, root, prompt, timeout, on_output, cancelled, on_stderr=None):
            cases.cancel_packet(request_data["case_id"], 1)
            assert cancelled()
            raise InterruptedError()
    cases.begin_packet(prepare(selection), request_data)
    run(cases, request_data, Cancelled())
    result = cases.load(request_data["case_id"])["attempts"][-1]["result"]
    assert result["execution_status"] == "CANCELLED" and result["review_status"] == "MISSING"


def test_reconcile_dead_supervisor_preserves_draft_and_holds_orphan_risk(selection, cases, request_data, monkeypatch):
    from relay.relay_io import write_json_atomic
    cases.begin_packet(prepare(selection), request_data)
    run(cases, request_data, PacketProvider(failure="timeout"))
    state = cases.load(request_data["case_id"])
    state["status"] = "RUNNING"
    last = state["attempts"][-1]
    last.pop("result")
    result_file = Path(last["execution"]["run"]) / "result.json"
    result_file.rename(result_file.with_suffix(".previous.json"))
    write_json_atomic(cases.path(request_data["case_id"]), state)
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    summary = cases.reconcile_packet(request_data["case_id"])
    assert summary["status"] == "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
    result = cases.load(request_data["case_id"])["attempts"][-1]["result"]
    assert result["review_status"] == "PARTIAL" and result["review"]["findings"][0]["id"] == "F1"
    assert result["next_action"] == "DIAGNOSE_EXECUTION"


def test_deleted_target_absence_is_sealed(selection):
    root = Path(selection["repositories"][0]["root"])
    (root / "fix.py").rename(root.parent / "saved.py")
    packet = prepare(selection)
    (root / "fix.py").write_text("resurrected = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="packet_source_changed"):
        recheck(packet)


def test_versioned_schemas_match_actual_contracts(selection, cases, request_data):
    import jsonschema
    packet = prepare(selection)
    cases.begin_packet(packet, request_data)
    run(cases, request_data, PacketProvider())
    result = cases.load(request_data["case_id"])["attempts"][-1]["result"]
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    for name, value in [("input", packet["input"]), ("review", review(packet)), ("result", result)]:
        schema = json.loads((schema_root / ("audit-" + name + "-v1.schema.json")).read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(value, schema)


def test_atomic_writer_retries_only_bounded_windows_sharing_error(tmp_path, monkeypatch):
    from relay import relay_io
    target = tmp_path / "state.json"
    relay_io.write_json_atomic(target, {"old": True})
    replace = relay_io.os.replace
    calls = []
    def transient(source, destination):
        calls.append(1)
        if len(calls) < 3:
            error = PermissionError("fixture sharing conflict")
            error.winerror = 32
            raise error
        return replace(source, destination)
    monkeypatch.setattr(relay_io.os, "replace", transient)
    relay_io.write_json_atomic(target, {"new": True})
    assert len(calls) == 3 and json.loads(target.read_bytes()) == {"new": True}
    def permanent(source, destination):
        raise OSError("not a Windows sharing failure")
    monkeypatch.setattr(relay_io.os, "replace", permanent)
    with pytest.raises(OSError, match="not a Windows"):
        relay_io.write_json_atomic(target, {"bad": True})
    assert json.loads(target.read_bytes()) == {"new": True}
    assert not list(tmp_path.glob(".*.tmp*"))


@pytest.mark.parametrize("raw", [b"[]", b'{"broken":', b"\xff"], ids=["array", "truncated", "utf8"])
def test_reconcile_invalid_result_is_typed_not_running(selection, cases, request_data, monkeypatch, raw):
    from relay.relay_io import write_json_atomic
    cases.begin_packet(prepare(selection), request_data)
    run(cases, request_data, PacketProvider())
    state = cases.load(request_data["case_id"])
    state["status"] = "RUNNING"
    last = state["attempts"][-1]
    last.pop("result")
    result_path = Path(last["execution"]["run"]) / "result.json"
    result_path.write_bytes(raw)
    write_json_atomic(cases.path(request_data["case_id"]), state)
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    assert cases.reconcile_packet(request_data["case_id"])["status"] == "INCONCLUSIVE_WITH_REVISIT_TRIGGER"
    result = cases.load(request_data["case_id"])["attempts"][-1]["result"]
    assert result["review_status"] == "INVALID"
    assert result_path.with_name("result.before-reconcile.json").read_bytes() == raw


def test_explicit_lost_session_reset_keeps_case_and_artifact_context(selection, cases, request_data, capsys):
    cases.begin_packet(prepare(selection), request_data)
    driver = PacketProvider(failure="budget")
    run(cases, request_data, driver)
    state = cases.load(request_data["case_id"])
    old_session = state["session_id"]
    selection["packet_id"] = "fix-r2"
    reply = {"schema": "tare.tools/audit-correction/1", "packet_id": "fix-r1",
             "dispositions": [{"finding_id": "F1", "disposition": "DEFERRED",
                               "change_refs": [], "test_receipt_refs": [], "reason": "Recover session first"}]}
    request = {"case_id": request_data["case_id"], "continuation": {"reason": "Provider lost session"},
               "correction": reply, "session_reset": {"expected_session_id": "wrong", "reason": "Lost"}}
    with pytest.raises(ValueError, match="session_reset"):
        cases.begin_packet(prepare(selection), request, resume=True)
    request["session_reset"]["expected_session_id"] = old_session
    opened = cases.begin_packet(prepare(selection), request, resume=True)
    assert opened["runner_action"] == "run"
    state = cases.load(request_data["case_id"])
    assert state["session_history"][0]["session_id"] == old_session
    assert state["attempts"][-1]["packet"]["input"]["previous_review"]["findings"][0]["id"] == "F1"
    assert len(state["attempts"]) == 2
    from relay.audit_cases import main
    before = cases.path(request_data["case_id"]).read_bytes()
    assert main(["frugality", "--store", str(cases.store), "--case", request_data["case_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["attempts"][1]["continuation_reason"] == "Provider lost session"
    assert report["authority"] == "NONE" and report["false_blocks_adjudicated"] is None
    assert cases.path(request_data["case_id"]).read_bytes() == before


def test_closed_case_successor_has_link_but_no_inherited_session(selection, cases, request_data):
    cases.begin_packet(prepare(selection), request_data)
    run(cases, request_data, PacketProvider("ACCEPTED_BOUNDED"))
    old_id = request_data["case_id"]
    successor = {**request_data, "case_id": "successor", "reopen": {"case_id": old_id, "reason": "New evidence"}}
    selection["packet_id"] = "successor-r1"
    opened = cases.begin_packet(prepare(selection), successor)
    assert opened["runner_action"] == "run" and opened["session_id"] is None
    assert cases.load("successor")["anchor"]["reopen"]["case_id"] == old_id
    assert cases.load(old_id)["status"] == "CLOSED"


def test_transport_qualification_is_explicit_bounded_and_not_a_case(selection, tmp_path):
    from relay.audit_execution import qualify_packet
    packet = prepare(selection)
    target = tmp_path / "qualification"
    result = qualify_packet(packet, run=target, driver=PacketProvider(),
        model="claude-fixture-operator-selected", effort="xhigh", max_usd=.1, timeout=5)
    assert result["qualification_only"] is True and result["authority"] == "NONE"
    assert result["primary_model_reported"] == "claude-fixture-operator-selected"
    with pytest.raises(ValueError, match="already_reserved"):
        qualify_packet(packet, run=target, driver=PacketProvider(), model="same", effort="xhigh", max_usd=.1)
    with pytest.raises(ValueError, match="budget_exceeded"):
        qualify_packet(packet, run=tmp_path / "excessive", driver=PacketProvider(),
                       model="same", effort="xhigh", timeout=181)


@pytest.mark.parametrize("action", ["execute", "qualify"])
@pytest.mark.parametrize("max_usd", [None, .75])
def test_cli_optional_cap_preserves_terminal_metrics(selection, cases, request_data, tmp_path, monkeypatch, capsys, action, max_usd):
    class Metered(PacketProvider):
        def run(self, command, **kwargs):
            capture = kwargs["on_output"]
            def on_output(raw):
                value = json.loads(raw)
                if value["type"] == "result":
                    value.update(total_cost_usd=1.25, duration_ms=92000, num_turns=2,
                        modelUsage={request_data["auditor"]["runtime_model"]: {
                            "inputTokens": 10, "outputTokens": 8000, "thinkingTokens": 6400,
                            "cacheReadInputTokens": 5000, "cacheCreationInputTokens": 19000}})
                capture(json_bytes(value) + b"\n")
            return super().run(command, **{**kwargs, "on_output": on_output})
    driver = Metered()
    monkeypatch.setattr("compute_plane.audit_driver.ClaudeAuditDriver", lambda command: driver)
    packet = prepare(selection)
    args = [action, "--store", str(cases.store), "--roster", str(cases.roster),
            "--cli", sys.executable, "--effort", "xhigh", "--timeout", "5"]
    if max_usd is not None:
        args += ["--max-usd", str(max_usd)]
    if action == "execute":
        cases.begin_packet(packet, request_data)
        args += ["--case", request_data["case_id"], "--attempt", "1"]
    else:
        path = tmp_path / "packet.json"; path.write_bytes(json_bytes(packet))
        args += ["--packet", str(path), "--run", str(tmp_path / "qualification"),
                 "--model", request_data["auditor"]["runtime_model"]]
    assert main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    result = cases.load(request_data["case_id"])["attempts"][-1]["result"] if action == "execute" else printed
    assert result["execution_status"] == "COMPLETED" and result["review_status"] == "VALID"
    metrics = result["metrics"]
    assert metrics["max_usd_soft"] == max_usd
    assert metrics["cost_estimate_usd"] == {"value": 1.25, "source": "provider_list_estimate"}
    assert metrics["duration_ms"]["value"] == 92000 and metrics["num_turns"]["value"] == 2
    assert metrics["provider_token_usage"][request_data["auditor"]["runtime_model"]]["thinkingTokens"] == 6400
    assert metrics["billable_cost_usd"] is None and metrics["subscription_quota"] is None
    assert ("--max-budget-usd" in driver.calls[0]) == (max_usd is not None)


def test_review_v2_requires_scenarios_inside_the_threat_model(selection):
    import jsonschema
    from relay.audit_packet import review_schema
    model = {"actor": "local operator", "window": "during the run", "access": "worktree of the tracked paths"}
    with pytest.raises(ValueError, match="threat_model_required"):
        prepare({**selection, "review_schema_version": 2})
    bad = deepcopy(selection)
    bad["obligations"][0]["threat_model"] = {"actor": "x"}
    with pytest.raises(ValueError, match="invalid_threat_model"):
        prepare(bad)
    selection["review_schema_version"] = 2
    selection["obligations"][0]["threat_model"] = model
    packet = prepare(selection)
    assert packet["input"]["response_schema"] == "tare.tools/audit-review/2"
    assert packet["review_output_schema"]["properties"]["schema"]["const"] == "tare.tools/audit-review/2"
    assert "scenario" in packet["input"]["context"]["review_guidance"]
    assert packet["input"]["obligations"][0]["threat_model"] == model  # the auditor sees the model
    value = review(packet)
    value["schema"] = "tare.tools/audit-review/2"
    with pytest.raises(ValueError, match="invalid_finding_fields"):  # v2 without a scenario is not a review
        validate_review(packet, value)
    value["findings"][0]["scenario"] = {**model, "plausibility": "operational"}
    assert validate_review(packet, value) is value
    jsonschema.validate(value, review_schema(version=2))
    value["findings"][0]["scenario"]["actor"] = "remote attacker"  # outside the threat model: residual, not finding
    with pytest.raises(ValueError, match="finding_outside_threat_model"):
        validate_review(packet, value)
    value["findings"][0]["scenario"] = {**model, "plausibility": "hypothetical"}
    with pytest.raises(ValueError, match="invalid_scenario"):
        validate_review(packet, value)
    v1 = review(packet)  # a v1 review against a v2 packet is a mismatch, and vice versa
    with pytest.raises(ValueError, match="review_packet_mismatch"):
        validate_review(packet, v1)
    assert review_schema() == review_schema(version=1)  # the default contract did not move
