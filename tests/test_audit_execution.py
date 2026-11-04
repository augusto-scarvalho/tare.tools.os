"""Real Git candidates and existing case consumer; fake provider, no model cost."""
from datetime import datetime, timezone
import functools
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from compute_plane.audit_driver import ClaudeAuditDriver
from relay.audit_cases import checksum, json_bytes
from relay.audit_execution import execute
from test_audit_cases import cases, request_data


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), "-c", "core.autocrlf=false",
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args]).decode().strip()


@functools.cache
def _base_candidate(basetemp):
    """The committed candidate and its object ids, built once per session (Git processes are slow on Windows)."""
    root = Path(basetemp) / "candidate-template"
    root.mkdir()
    _git(root, "-c", "init.templateDir=", "init", "-q")
    (root / "fix.py").write_bytes(b"pass\n")
    _git(root, "add", "fix.py")
    _git(root, "commit", "-qm", "Fixture")
    return root, _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}"), _git(root, "rev-parse", "HEAD:fix.py")


@pytest.fixture
def candidate(tmp_path, tmp_path_factory, request_data):
    template, commit, tree, blob = _base_candidate(tmp_path_factory.getbasetemp())
    root = tmp_path / "candidate"
    shutil.copytree(template, root)
    _git(root, "update-index", "-q", "--refresh")  # the copy's index then equals a fresh checkout's
    content = (root / "fix.py").read_bytes()
    request_data["candidate"] = {"commit": commit, "tree": tree}
    request_data["candidate_artifacts"] = [{"path": "fix.py", "git_blob": blob,
        "sha256": checksum(content), "size_bytes": len(content), "role": "candidate-artifact",
        "provenance": "git-tree:" + request_data["candidate"]["tree"]}]
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b'{"test":"fixture","passed":true}')
    request_data["test_execution_sha256"] = checksum(receipt.read_bytes())
    return root, receipt


class FakeProvider(ClaudeAuditDriver):
    def __init__(self, verdict="REJECTED", mutate=None):
        super().__init__([sys.executable])
        self.verdict, self.calls, self.mutate = verdict, [], mutate

    def run(self, command, *, root, prompt, timeout):
        self.calls.append(command)
        request = json.loads(prompt.split(b"\nEXACT REQUEST:\n")[1])
        session = command[-1]
        decision = {"schema": "tare.tools/audit-verdict/3.0", "role": "independent-auditor",
            "candidate_tree": request["candidate"]["tree"],
            **{key: request[key] for key in ("scope", "candidate_artifacts", "test_execution_sha256")},
            "auditor_profile": "fixture", "audit_method": "read-only receipt review",
            "issued_at": datetime.now(timezone.utc).isoformat(), "verdict": self.verdict,
            "blocking_findings": ["Defect"] if self.verdict == "REJECTED" else [], "residual_findings": []}
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "structured_output": decision,
                  "modelUsage": {command[command.index("--model") + 1]: {}}}
        if self.mutate:
            self.mutate(root, result)
        self.raw = json_bytes(result) + b"\n"
        return 0, self.raw


def run_case(cases, request_data, candidate, driver):
    root, receipt = candidate
    attempt = len(cases.load(request_data["case_id"])["attempts"])
    return execute(cases, request_data["case_id"], attempt, root=root, receipt=receipt,
                   driver=driver, max_usd=.25)


def test_execute_resume_and_close_reuse_exact_session(cases, request_data, candidate):
    cases.begin(request_data)
    driver = FakeProvider()
    first = run_case(cases, request_data, candidate, driver)
    assert first["status"] == "REJECTED"
    assert "--session-id" in driver.calls[0] and "--resume" not in driver.calls[0]
    cases.begin({"case_id": request_data["case_id"], "continuation": {
        "kind": "correction", "reason": "New receipt examined"}}, resume=True)
    driver.verdict = "QUALIFIED_BOUNDED"
    second = run_case(cases, request_data, candidate, driver)
    assert second["status"] == "CLOSED" and second["authority"] == "NONE"
    assert "--resume" in driver.calls[1] and driver.calls[0][-1] == driver.calls[1][-1]
    assert "--fallback-model" not in driver.calls[1]
    assert driver.calls[1][driver.calls[1].index("--tools") + 1] == "Read,Glob,Grep"
    import base64
    stored = cases.load(request_data["case_id"])["attempts"][-1]["artifacts"]["result.json"]
    assert base64.b64decode(stored["bytes"]) == driver.raw


@pytest.mark.parametrize("mutation", [
    lambda root, result: result.update(session_id="wrong"),
    lambda root, result: result.update(modelUsage={"unapproved": {}}),
    lambda root, result: (root / "fix.py").write_text("changed", encoding="utf-8"),
])
def test_false_success_never_closes_case(cases, request_data, candidate, mutation):
    cases.begin(request_data)
    report = run_case(cases, request_data, candidate, FakeProvider("QUALIFIED_BOUNDED", mutation))
    assert report["status"] == "EXECUTION_FAILED" and report["decision"] is None


def test_invalid_receipt_and_dirty_candidate_never_launch(cases, request_data, candidate):
    cases.begin(request_data)
    root, receipt = candidate
    driver = FakeProvider()
    original = receipt.read_bytes()
    receipt.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="test_receipt_bytes"):
        run_case(cases, request_data, candidate, driver)
    receipt.write_bytes(original)
    (root / "fix.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="candidate_mismatch_or_dirty"):
        run_case(cases, request_data, candidate, driver)
    assert not driver.calls


def test_invalid_subject_preserves_reserved_attempt_without_redispatch(cases, request_data, candidate):
    cases.begin(request_data)
    driver = FakeProvider("QUALIFIED_BOUNDED",
                          lambda root, result: result["structured_output"].update(candidate_tree="f" * 40))
    with pytest.raises(ValueError, match="decision_subject"):
        run_case(cases, request_data, candidate, driver)
    assert cases.load(request_data["case_id"])["status"] == "RUNNING"
    with pytest.raises(ValueError, match="already_dispatched"):
        run_case(cases, request_data, candidate, driver)
    assert len(driver.calls) == 1


def test_timeout_is_execution_failure_not_verdict(cases, request_data, candidate):
    class TimedOut(FakeProvider):
        def run(self, *args, **kwargs):
            raise TimeoutError("fixture deadline")
    cases.begin(request_data)
    assert run_case(cases, request_data, candidate, TimedOut())["status"] == "EXECUTION_FAILED"


@pytest.mark.parametrize("raw", [b"", b"[]", b'{"type":"result","session_id":"wrong"}'])
def test_unproven_session_failure_recovers_with_fresh_vendor_session(
        cases, request_data, candidate, raw):
    class NoSession(FakeProvider):
        def run(self, command, **kwargs):
            self.calls.append(command)
            return 1, raw
    cases.begin(request_data)
    failed = NoSession()
    assert run_case(cases, request_data, candidate, failed)["status"] == "EXECUTION_FAILED"
    assert cases.load(request_data["case_id"])["session_id"] is None
    cases.begin({"case_id": request_data["case_id"], "continuation": {
        "kind": "execution_recovery", "reason": "Provider startup repaired"}}, resume=True)
    healthy = FakeProvider()
    assert run_case(cases, request_data, candidate, healthy)["status"] == "REJECTED"
    assert "--session-id" in healthy.calls[0] and "--resume" not in healthy.calls[0]
    assert healthy.calls[0][-1] != failed.calls[0][-1]


@pytest.mark.parametrize("value", [None, [], "text", 42, True])
@pytest.mark.parametrize("nested", [False, True])
def test_non_object_response_is_execution_failure(cases, request_data, candidate, value, nested):
    class BadJSON(FakeProvider):
        def run(self, *args, **kwargs):
            if nested:
                self.mutate = lambda root, result: result.update(structured_output=value)
                return super().run(*args, **kwargs)
            return 0, json_bytes(value)
    cases.begin(request_data)
    assert run_case(cases, request_data, candidate, BadJSON())["status"] == "EXECUTION_FAILED"


def test_roster_change_blocks_reserved_dispatch(cases, request_data, candidate, tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_bytes(cases.roster.read_bytes())
    cases.roster = roster
    cases.begin(request_data)
    # Even a byte-level roster revision must be re-attested, not grandfathered.
    roster.write_bytes(roster.read_bytes() + b"\n")
    driver = FakeProvider()
    with pytest.raises(ValueError, match="identity_or_roster_changed"):
        run_case(cases, request_data, candidate, driver)
    assert not driver.calls


def test_changed_artifact_identity_blocks_before_spend(cases, request_data, candidate):
    request_data["candidate_artifacts"][0]["sha256"] = "f" * 64
    cases.begin(request_data)
    driver = FakeProvider()
    with pytest.raises(ValueError, match="artifact_bytes_mismatch"):
        run_case(cases, request_data, candidate, driver)
    assert not driver.calls


@pytest.mark.parametrize('failure', ['deep_json', 'cleanup_timeout'])
def test_provider_boundary_records_deep_json_and_cleanup_failure(cases, request_data, candidate, failure):
    class Broken(FakeProvider):
        def run(self, command, **kwargs):
            self.calls.append(command)
            if failure == 'cleanup_timeout': raise subprocess.TimeoutExpired('provider cleanup', 5)
            return 0, b'[' * 200000 + b']' * 200000
    cases.begin(request_data)
    provider = Broken()
    status = run_case(cases, request_data, candidate, provider)
    assert status['status'] == 'EXECUTION_FAILED'
    state = cases.load(request_data['case_id'])
    error = state['attempts'][-1]['execution_error']
    if failure == 'deep_json':
        assert error == 'ValueError:artifact_json_too_deep'
    else:
        assert error.startswith('TimeoutExpired:')
    with pytest.raises(ValueError, match='stale_or_completed_attempt'):
        run_case(cases, request_data, candidate, provider)
    assert len(provider.calls) == 1


def test_real_transport_to_case_custody_with_fake_cli(cases, request_data, candidate, tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json, sys, datetime\n"
        "request=json.loads(sys.stdin.read().rsplit('\\nEXACT REQUEST:\\n',1)[1])\n"
        "decision=dict(schema='tare.tools/audit-verdict/3.0',role='independent-auditor',"
        "candidate_tree=request['candidate']['tree'],"
        "auditor_profile='fixture',audit_method='fixture only',verdict='QUALIFIED_BOUNDED',"
        "blocking_findings=[],residual_findings=[],"
        "issued_at=datetime.datetime.now(datetime.timezone.utc).isoformat())\n"
        "decision.update({k:request[k] for k in ['scope','candidate_artifacts','test_execution_sha256']})\n"
        "print(json.dumps(dict(type='result',subtype='success',is_error=False,"
        "session_id=sys.argv[-1],structured_output=decision,"
        "modelUsage={sys.argv[sys.argv.index('--model')+1]:{}})))\n", encoding="utf-8")
    cases.begin(request_data)
    driver = ClaudeAuditDriver([sys.executable, str(script)])
    report = run_case(cases, request_data, candidate, driver)
    assert report["status"] == "CLOSED" and report["authority"] == "NONE"
