"""Current routing owners preserve why no route can start, without inference."""
import json
import asyncio
from io import StringIO

import pytest

from relay import native_catalog, native_generation, turn_engine
from test_native_agent import deployment


@pytest.fixture
def blocked_routes(monkeypatch):
    names = ['blocked-route-'+str(i).zfill(2) for i in range(12)]
    auto = native_catalog.bundled('tare-auto')
    def select(name=None, *args, **kwargs):
        if name == 'tare-auto':
            return auto
        value = deployment(endpoint='https://scheduler.example.test/v1')
        value.update(id=name, native_credential_env='MISSING_RES_IP_B_CREDENTIAL')
        return value
    monkeypatch.setattr(native_catalog, 'select', select)
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [names]})
    monkeypatch.setattr(native_generation, 'vault_secret', lambda _: None)
    monkeypatch.delenv('MISSING_RES_IP_B_CREDENTIAL', raising=False)
    return names


def test_all_unschedulable_routes_survive_the_error_boundary(blocked_routes):
    with pytest.raises(ValueError) as caught:
        turn_engine.resolve_auto({'native_auto_routes': 'implementer'})
    assert blocked_routes[-1] in str(caught.value)
    report = caught.value.report
    assert [r['route'] for r in report['reasons']] == blocked_routes
    assert {r['code'] for r in report['reasons']} == {'CREDENTIAL_UNAVAILABLE'}
    assert report['omitted'] == 0


def test_cli_preflight_returns_structured_reasons_and_does_not_create_a_session(blocked_routes, tmp_path, capsys):
    from relay import native_agent
    session = tmp_path/'never-entered'
    assert native_agent.main(['--native', '--deployment', 'tare-auto', '--root', str(tmp_path),
        '--session', str(session), '--instruction', 'hello', '--format', 'json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'UNSCHEDULABLE'
    assert result['execution'] == 'agent_not_started' and result['model_calls'] == 0
    assert [r['route'] for r in result['unschedulable']['reasons']] == blocked_routes
    assert not (session/'native-agent.json').exists()


def test_native_role_selection_returns_its_diagnostic_codes(monkeypatch):
    from relay import native_routes
    calls = []
    def ranked(role, **options):
        calls.append((role, options))
        return [], {'candidates': [{'deployment': 'one', 'eligible': False, 'reason': 'GPU_RESERVED'},
                                   {'deployment': 'two', 'eligible': False, 'reason': 'AUDITOR_NOT_INDEPENDENT'}]}
    monkeypatch.setattr(native_routes, 'ranked', ranked)
    with pytest.raises(ValueError) as caught:
        native_catalog.select('auto', role='implementer')
    assert {r['code'] for r in caught.value.report['reasons']} == {'GPU_RESERVED', 'AUDITOR_NOT_INDEPENDENT'}
    assert calls == [('implementer', {'check_resources': True})]


@pytest.mark.parametrize('state,code', [
    ({'gpu_coordination': {'held': True, 'owner': {'kind': 'image'}}}, 'GPU_BUSY'),
    ({'gpu_coordination': {'reservation': {'id': 'someone-else'}}}, 'GPU_RESERVED'),
    ([], 'RESOURCE_OBSERVATION_INVALID'),
    ({'gpu_coordination': {'queue': [None]}}, 'RESOURCE_OBSERVATION_INVALID'),
])
def test_gpu_refusal_uses_the_observed_cause(monkeypatch, state, code):
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: StringIO(json.dumps(state)))
    model = deployment(endpoint='http://127.0.0.1:8080/v1')
    reason = turn_engine.local_gpu_busy(model)
    assert reason.diagnostic['code'] == code
    assert reason.diagnostic['category'] == ('unknown' if code == 'RESOURCE_OBSERVATION_INVALID' else 'resource')


def test_text_queue_is_eligible_and_image_queue_explanation_names_the_job(monkeypatch):
    from relay.scheduling_diagnostics import diagnostic
    state = {'gpu_coordination': {'held': True, 'owner': {'kind': 'text'}, 'queue': [{'kind': 'text'}]}}
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: StringIO(json.dumps(state)))
    assert turn_engine.local_gpu_busy(deployment(endpoint='http://127.0.0.1:8080/v1')) is None
    assert 'image job' in diagnostic('GPU_IMAGE_WAITING')['message']


@pytest.mark.parametrize('kind,code', [('policy', 'MACHINE_POLICY'), ('cli', 'CLI_UNAVAILABLE'),
                                      ('quota', 'QUOTA_EXHAUSTED')])
def test_unavailable_dependency_policy_and_quota_never_start_a_model(monkeypatch, tmp_path, kind, code):
    import time
    from relay import environment
    model = deployment(endpoint='https://scheduler.example.test/v1')
    if kind == 'cli':
        model['native_cli'] = {'executable': str(tmp_path/'missing.exe')}
    monkeypatch.setattr(native_catalog, 'select', lambda *a, **kw: model)
    monkeypatch.setattr(environment, 'allows', lambda _: (False, 'owner excludes route') if kind == 'policy' else (True, None))
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda _: time.time()+30 if kind == 'quota' else None)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda _: pytest.fail('unavailable route was probed'))
    value, reason = turn_engine.available('example')
    assert value is None and reason.diagnostic['code'] == code
    if kind == 'quota':
        assert 0 <= reason.diagnostic['retry_after_seconds'] <= 30


def test_exception_after_owner_entry_does_not_invent_zero_usage(blocked_routes, monkeypatch, tmp_path, capsys):
    from relay import native_agent
    from relay.scheduling_diagnostics import UnschedulableError
    def entered(*args, **kwargs):
        raise UnschedulableError('later_owner_refusal', [{'route': 'example', 'reason': 'UNKNOWN'}])
    monkeypatch.setattr(native_agent, 'run_session', entered)
    assert native_agent.main(['--native', '--deployment', 'direct', '--root', str(tmp_path),
        '--instruction', 'hello', '--format', 'json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert 'model_calls' not in result and result.get('execution') != 'agent_not_started'


def test_an_existing_session_is_not_reported_as_zero_usage(blocked_routes, tmp_path, capsys):
    from relay import native_agent
    session = tmp_path/'existing'; session.mkdir()
    origin = session/'native-agent.json'
    origin.write_text('{"historical":"keep"}')
    before = origin.read_bytes()
    assert native_agent.main(['--native', '--deployment', 'tare-auto', '--root', str(tmp_path),
        '--session', str(session), '--instruction', 'hello', '--format', 'json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert 'model_calls' not in result and result.get('execution') != 'agent_not_started'
    assert origin.read_bytes() == before


def test_other_new_session_preflight_errors_also_distinguish_owner_entry(tmp_path, capsys):
    from relay import native_agent
    assert native_agent.main(['--native', '--root', str(tmp_path), '--role', 'explorer',
        '--write-path', 'example.py', '--instruction', 'hello', '--format', 'json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['execution'] == 'agent_not_started' and result['model_calls'] == 0


@pytest.mark.parametrize('size', [(80, 24), (150, 52)])
def test_tui_preflight_keeps_the_draft_and_never_resumes_a_nonexistent_session(tmp_path, monkeypatch, size):
    from textual.widgets import TextArea
    from relay.native_console import WorkConsole
    from relay.scheduling_diagnostics import UnschedulableError, Unavailable
    from relay.console_demand import result_summary
    from test_native_console import FakeBackend, Process
    report = UnschedulableError('tare_auto_no_route_available', [
        {'route': 'route_[one]', 'reason': Unavailable('excluded', 'MACHINE_POLICY')},
        {'route': 'route_[two]', 'reason': Unavailable('missing', 'CLI_UNAVAILABLE')}]).report
    result = {'status': 'BLOCKED', 'reason': 'UNSCHEDULABLE', 'execution': 'agent_not_started',
              'model_calls': 0, 'authority': 'NONE', 'unschedulable': report}
    assert result_summary(result)['unschedulable'] == report
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    escalations = []
    monkeypatch.setattr(WorkConsole, 'auto_escalate', lambda *a, **kw: escalations.append(a))
    class Refused(Process):
        async def communicate(self):
            self.returncode = 2
            return json.dumps(result).encode(), b''
    async def scenario():
        backend = FakeBackend(); backend.process = Refused()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('hello')
            app.begin_assistant()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.assistant_session is None and not app.active.session
            assert app.query_one('#prompt', TextArea).text == 'hello'
            assert not escalations and app.last_result['model_calls'] == 0
            delivery_note = app.query_one('#output').delivery_note
            assert delivery_note is None or not delivery_note.display
            assert 'F3 review changes' not in str(app.query_one('#status').render())
            widget = app.query_one('.execution-result .result-notice')
            notice = '\n'.join(''.join(segment.text for segment in widget.render_line(y))
                               for y in range(widget.size.height))
            assert 'MACHINE_POLICY' in notice and 'CLI_UNAVAILABLE' in notice
            assert 'No model or project tool ran' in notice
    asyncio.run(scenario())
