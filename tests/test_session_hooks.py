"""Lifecycle composition uses real owners, never a vendor session or model."""
import io
import json
from pathlib import Path

import pytest

from relay.session_hooks import dispatch, main
from test_tare_ops import project
from test_audit_cases import cases, request_data


@pytest.mark.parametrize("argv", [
    ["claude", "--session-config", "missing.json", "--event", "stop"],
    ["claude-code", "--session-config", "missing.json"],
    ['--bad'], ['claude', '--session-config'], ['claude'],
    ['claude', '--session-config', 'missing.json', '--bogus'],
])
def test_stop_bad_argv_is_neutral(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(
        b'{"hook_event_name":"Stop","cwd":"C:/","session_id":"x"}')))
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_equals_option_routes_session_handler(monkeypatch, capsys):
    import tare_ops
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(
        b'{"hook_event_name":"Stop","cwd":"C:/","session_id":"x"}')))
    assert tare_ops.main(["hook", "claude", "--session-config=missing.json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize('argv', [['--bad'], ['claude', '--session-config'], ['claude'],
    ['claude', '--session-config', 'missing.json', '--bogus']])
def test_invalid_tool_argv_has_no_neutral_output(monkeypatch, capsys, argv):
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo hello"}}')))
    assert main(argv) == 2
    assert capsys.readouterr().out == ''


def test_equals_option_tool_input_reaches_kernel_denial(monkeypatch, capsys):
    import tare_ops
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(
        b'{"hook_event_name":"PreToolUse","cwd":".","tool_name":"Bash","tool_input":{"command":"rg x ."}}')))
    assert tare_ops.main(['hook', 'claude', '--session-config=missing.json']) == 0
    assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('phase', ['Stop', 'PreToolUse'])
def test_facade_equals_preserves_owner_argv_stdin_and_status(monkeypatch, phase):
    import tare_ops
    raw = json.dumps({'hook_event_name': phase}).encode()
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(raw)))
    def owner(argv):
        import sys
        assert argv == ['claude', '--session-config=missing.json']
        assert sys.stdin.buffer.read() == raw
        return 7
    monkeypatch.setattr('relay.session_hooks.main', owner)
    assert tare_ops.main(['hook', 'claude', '--session-config=missing.json']) == 7


def test_deep_json_is_typed_tool_failure(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(b'[' * 60000)))
    assert main(['claude', '--session-config', 'missing.json']) == 2
    assert capsys.readouterr().out == ''


def test_deep_json_explicit_agy_stop_remains_neutral(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(b'[' * 60000)))
    assert main(['agy', '--session-config', 'missing.json', '--event', 'stop']) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_receipt_budget_matches_written_bytes(project, monkeypatch):
    root, config = setup(project)
    config["max_bytes"] = 16384
    monkeypatch.setattr("tare_ops.context", lambda **kw: {
        "status": "READY_CONTEXT", "pad": ["x"] * 3000})
    payload, _ = event("claude", root, "stop")
    _, report = dispatch("claude", payload, config)
    receipt = Path(config["receipt"])
    assert receipt.stat().st_size <= config["max_bytes"]
    stamp = receipt.stat().st_mtime_ns
    assert dispatch("claude", payload, config)[1] == report
    assert json.loads(receipt.read_bytes()) == report
    assert receipt.stat().st_mtime_ns == stamp


def test_receipt_exact_boundary_unicode_and_json_normalization(project, monkeypatch):
    from relay.audit_cases import json_bytes
    root, config = setup(project)
    config['max_bytes'] = 16384
    work = {'unicode': 'ação λ', 'tuple': (1, 2), 'pad': ''}
    monkeypatch.setattr('tare_ops.context', lambda **kw: work)
    payload, _ = event('claude', root, 'stop')
    _, report = dispatch('claude', payload, config)
    work['pad'] = 'x' * (16384 - len(json_bytes(report)))
    _, report = dispatch('claude', payload, config)
    receipt = Path(config['receipt'])
    assert receipt.read_bytes() == json_bytes(report) and receipt.stat().st_size == 16384
    stamp = receipt.stat().st_mtime_ns
    def no_rewrite(*args): pytest.fail('identical receipt rewritten')
    monkeypatch.setattr('relay.session_hooks.write_text_atomic', no_rewrite)
    dispatch('claude', payload, config)
    assert receipt.stat().st_mtime_ns == stamp
    assert json.loads(receipt.read_bytes())['work']['unicode'] == 'ação λ'


@pytest.mark.parametrize('phase', ['start', 'stop'])
def test_oversized_context_stays_advisory(project, monkeypatch, capsys, phase):
    root, config = setup(project)
    monkeypatch.setattr('tare_ops.context', lambda **kw: {'groundings': ['x' * 8192] * 2})
    payload, _ = event('claude', root, phase)
    with pytest.raises(ValueError, match='session_context_over_budget'):
        dispatch('claude', payload, config)
    path = root.parent / 'config.json'; path.write_text(json.dumps(config))
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert main(['claude', '--session-config', str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert not Path(config['receipt']).exists()


def test_unicode_context_survives_ascii_console(project, monkeypatch):
    root, config = setup(project)
    monkeypatch.setattr("tare_ops.context", lambda **kw: {"status": "READY_CONTEXT", "text": "ação λ"})
    path = root.parent / "unicode-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    payload, _ = event("codex", root, "start")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    buffer = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(buffer, encoding="ascii", write_through=True))
    assert main(["codex", "--session-config", str(path)]) == 0
    context = json.loads(buffer.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "ação λ" in context


def setup(project):
    temporary, graph, manifest = project
    root = temporary / "host"
    root.mkdir()
    return root, {"root": str(root), "graph": str(graph), "work": "WORK-1",
                  "library_manifest": str(manifest), "receipt": str(temporary / "stop.json")}


def event(vendor, root, phase):
    if vendor == "agy":
        return {"workspacePaths": [str(root)]}, phase
    return {"cwd": str(root), "session_id": "host-session", "source": "resume",
            "hook_event_name": {"start": "SessionStart", "stop": "Stop", "end": "SessionEnd"}[phase]}, None


@pytest.mark.parametrize("vendor", ["claude", "codex", "agy"])
@pytest.mark.parametrize("phase", ["start", "stop", "end"])
def test_lifecycle_context_and_neutral_exit(project, vendor, phase):
    root, config = setup(project)
    payload, hint = event(vendor, root, phase)
    rendered, report = dispatch(vendor, payload, config, event_hint=hint)
    assert report["work"]["status"] == "READY_CONTEXT" and report["authority"] == "NONE"
    assert report["work"]["work"]["work_id"] == "WORK-1"
    assert not list(root.iterdir())
    receipt = Path(config["receipt"])
    if phase == "start":
        assert not receipt.exists()
        assert "READY_CONTEXT" in json.dumps(rendered)
        assert not {"decision", "permissionDecision"} & rendered.keys()
    else:
        assert rendered == {}
        assert json.loads(receipt.read_bytes()) == report
        stamp = receipt.stat().st_mtime_ns
        dispatch(vendor, payload, config, event_hint=hint)
        assert receipt.stat().st_mtime_ns == stamp


def test_resume_recomputes_work_and_missing_work_is_not_frontier(project):
    root, config = setup(project)
    payload, _ = event("claude", root, "start")
    _, first = dispatch("claude", payload, config)
    assert first["resumed"] is True and first["work"]["status"] == "READY_CONTEXT"
    config["work"] = "missing"
    _, second = dispatch("claude", payload, config)
    assert second["work"]["status"] == "BLOCKED"
    assert not Path(config["receipt"]).exists()


@pytest.mark.parametrize("change,reason", [
    ({"root": "relative"}, "absolute_configured"),
    ({"graph": "relative"}, "absolute_setting"),
    ({"max_bytes": 1}, "invalid_session_budget"),
    ({"work": None}, "explicit_work"),
    ({"audit_case": "x"}, "incomplete_audit"),
    ({"surprise": True}, "unknown_session"),
])
def test_invalid_configuration_is_rejected(project, change, reason):
    root, config = setup(project)
    payload, _ = event("codex", root, "stop")
    with pytest.raises(ValueError, match=reason):
        dispatch("codex", payload, {**config, **change})


def test_workspace_and_receipt_confinement(project):
    root, config = setup(project)
    payload, _ = event("codex", root.parent, "stop")
    with pytest.raises(ValueError, match="workspace_mismatch"):
        dispatch("codex", payload, config)
    payload["cwd"] = str(root)
    config["receipt"] = str(root / "bad.json")
    with pytest.raises(ValueError, match="receipt_inside"):
        dispatch("codex", payload, config)


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"x" * 65537,
    b'{"hook_event_name":"PreToolUse","cwd":".","tool_name":"Bash","tool_input":{"command":"rg x ."}}'],
    ids=["invalid-json", "wrong-shape", "oversized", "search"])
def test_single_entrypoint_does_not_turn_invalid_tool_input_into_allow(monkeypatch, capsys, raw):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert main(["claude", "--session-config", "missing.json"]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_broken_stop_configuration_never_traps_the_host(monkeypatch, capsys):
    payload = b'{"hook_event_name":"Stop","cwd":"C:/","session_id":"x"}'
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(payload)))
    assert main(["codex", "--session-config", "missing.json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {} and "unavailable" in output.err


def test_agy_explicit_post_keeps_owner_contract(monkeypatch, capsys):
    payload = b'{"workspacePaths":["C:/"],"toolCall":{"name":"run_command","args":{"CommandLine":"echo hi"}}}'
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(payload)))
    assert main(["agy", "--session-config", "missing.json", "--event", "post"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_actual_facade_session_entrypoint(project):
    import os
    import subprocess
    import sys
    import compute_plane.session_hooks as owner
    root, config = setup(project)
    config_path = root.parent / "session.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    payload, _ = event("codex", root, "start")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([
        str(Path(__file__).resolve().parents[1]), str(Path(owner.__file__).resolve().parents[1])]))
    result = subprocess.run([sys.executable, "-B", "-m", "tare_ops", "hook", "codex",
        "--session-config", str(config_path)], input=json.dumps(payload).encode(), cwd=root,
        env=env, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert "READY_CONTEXT" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_missing_tool_owner_is_not_neutral_success(monkeypatch, capsys):
    import sys
    monkeypatch.setitem(sys.modules, "compute_plane.hook_cli", None)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"invalid")))
    assert main(["claude", "--session-config", "missing.json"]) == 2
    output = capsys.readouterr()
    assert "unavailable" in output.err and output.out == ''


def test_stop_observes_pending_case_without_advancing_it(project, cases, request_data):
    root, config = setup(project)
    request_data["scope"]["nodes"] = ["WORK-1"]
    cases.begin(request_data)
    path = cases.path(request_data["case_id"])
    before = path.read_bytes()
    config.update(audit_store=str(cases.store), audit_case=request_data["case_id"])
    payload, _ = event("claude", root, "stop")
    rendered, report = dispatch("claude", payload, config)
    assert rendered == {} and report["audit"]["status"] == "RUNNING"
    assert path.read_bytes() == before
