"""Temporary restrictions survive a real child without touching global files."""
from dataclasses import asdict
import json, os, subprocess, sys
import pytest
from relay import execution_scope as scope


def test_child_receives_validated_policy_and_parent_is_restored(monkeypatch):
    monkeypatch.delenv(scope.ENV,raising=False)
    value=scope.Scope(only='api',restrictions=(('api','google'),)).validate()
    with scope.activated(value):
        result=subprocess.run([sys.executable,'-B','-c',
            'from dataclasses import asdict; import json; from relay.execution_scope import current; print(json.dumps(asdict(current())))'],
            capture_output=True,text=True,check=True)
        assert json.loads(result.stdout)==json.loads(json.dumps(asdict(value)))
    assert scope.ENV not in os.environ


def test_intersection_never_unions_allowlists():
    a=scope.Scope(restrictions=(('api','google'),)).validate()
    b=scope.Scope(restrictions=(('api','openai'),)).validate()
    assert len(a.intersect(b).restrictions)==2
    with pytest.raises(ValueError):scope.Scope(only='api').intersect(scope.Scope(only='local'))


@pytest.mark.parametrize('payload',[{'restrictions':None},{'only':'typo'},{'coordinator':[]},{'unknown':True}])
def test_invalid_inherited_payload_is_rejected(monkeypatch,payload):
    monkeypatch.setenv(scope.ENV,json.dumps(payload))
    with pytest.raises(ValueError):scope.current()


@pytest.mark.parametrize('recorded', [
    {'disable_judge': True}, {'only': 'api'}, {'prefer': 'subscription'},
    {'restrictions': [['api', 'google']]},
])
def test_reopening_session_does_not_reactivate_recorded_switches(monkeypatch, tmp_path, recorded):
    monkeypatch.delenv(scope.ENV, raising=False)
    deployment = {'id': 'fixture', 'native_execution_scope': recorded,
                  'native_coordinator_pin': 'google/gemini-3.8-flash@high'}
    saved = {'deployment': deployment, 'conversation_flow': True,
             'history': ['unchanged'], 'cache': {'key': 'stable'}, 'spend': 7}
    path = tmp_path / 'native-agent.json'
    path.write_text(json.dumps(saved), encoding='utf8')
    before = path.read_bytes()
    @scope.session_role
    def owner(config, deployment, custody, **kwargs):
        return scope.current()
    assert owner({}, None, tmp_path, resume=True) == scope.Scope()
    assert path.read_bytes() == before
    assert scope.ENV not in os.environ


def test_new_session_does_not_persist_temporary_authorization(monkeypatch, tmp_path):
    monkeypatch.delenv(scope.ENV, raising=False)
    @scope.session_role
    def owner(config, deployment, custody, **kwargs):
        assert scope.current().disable_judge
        return deployment
    value = {'id': 'fixture'}
    with scope.activated(scope.Scope(disable_judge=True)):
        result = owner({}, value, tmp_path, conversation=True)
    assert 'native_execution_scope' not in result
    assert value == {'id': 'fixture'}


def test_reopen_demand_ignores_old_switches_and_keeps_current_scope(monkeypatch, tmp_path):
    import asyncio
    from relay.native_console import WorkConsole
    from relay import relay_mesh
    from test_console_demand import Backend
    root = tmp_path / 'project'
    root.mkdir()
    control = tmp_path / 'control'
    control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.delenv(scope.ENV, raising=False)
    async def scenario():
        app = WorkConsole(root, backend=Backend(), initial_mode='conversation')
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            payload = asdict(app.selection())
            payload['execution_policy'] = {'only': 'local', 'disable_judge': True,
                                           'prefer': 'subscription'}
            # A valid historical policy may not authorize this new instance.
            payload['execution_policy'].pop('prefer')
            before = json.dumps(payload, sort_keys=True)
            with scope.activated(scope.Scope(only='api')):
                app.restore_selection(payload)
                assert scope.current() == scope.Scope(only='api')
                assert not app.selection().execution_policy.get('disable_judge')
                assert 'only' not in app.selection().execution_policy
            assert json.dumps(payload, sort_keys=True) == before
            assert app.process is None and not app.busy
    asyncio.run(scenario())
    assert scope.ENV not in os.environ


@pytest.mark.parametrize('default_pin', ['', 'google/gemini-3.8-flash@high'])
@pytest.mark.parametrize('baseline', [scope.Scope(), scope.Scope(
    only='api', disable_judge=True, restrictions=(('api', 'google;openai'),))])
def test_reopen_a_new_demand_then_b_never_leaks_pin(monkeypatch, tmp_path, baseline, default_pin):
    """Public entry keeps instance defaults separate from saved conversation pins."""
    import asyncio
    from relay import console_demand, relay_mesh, native_console, native_installation, native_catalog
    from relay.console_backend import Backend as ProcessBackend
    from relay.native_console import WorkConsole
    from test_console_demand import Backend, open_saved, snapshot
    root = tmp_path / 'project'
    root.mkdir()
    control = tmp_path / 'control'
    control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    if baseline.active:
        monkeypatch.setenv(scope.ENV, json.dumps(asdict(baseline)))
    else:
        monkeypatch.delenv(scope.ENV, raising=False)
    original_env = os.environ.get(scope.ENV)
    pins = ['google/gemini-3.8-flash@high', 'openai/gpt-6.1-sol@high']
    demands = []
    for pin in pins:
        value = snapshot(root)
        value['selection']['execution_policy'] = {'coordinator': pin}
        demands.append(console_demand.save(None, 0, root, control, 'draft_saved', value))
    paths = [console_demand.path(d['id']) for d in demands]
    before = [p.read_bytes() for p in paths]
    spawn = asyncio.create_subprocess_exec
    async def offline_child(*args, **kwargs):
        # Exercise Backend.launch's actual child environment, with no provider.
        return await spawn(sys.executable, '-B', '-c',
            'import os; print(os.environ.get("TARE_SESSION_SCOPE", "{}"))', **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', offline_child)
    async def child_policy(pin=''):
        with scope.for_coordinator(pin):
            process = await ProcessBackend().launch(['fixture'], str(root))
        output, error = await process.communicate()
        assert process.returncode == 0, error
        assert os.environ.get(scope.ENV) == original_env
        return scope.Scope(**json.loads(output)).validate()
    async def scenario(app):
        assert app.default_coordinator == default_pin
        assert app.conversation_coordinator == default_pin
        assert scope.current() == baseline
        assert not scope.current().coordinator
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            await open_saved(app, demands[0]['id'])
            assert app.selection().execution_policy == {'coordinator': pins[0]}
            assert scope.current() == baseline
            assert await child_policy(pins[0]) == baseline.intersect(scope.Scope(coordinator=pins[0]))
            # Preserve A's bytes while leaving it; no explicit draft save required.
            monkeypatch.setattr(app, 'checkpoint', lambda *args, **kwargs: app.demand_state)
            app.new_demand()
            assert app.selection().execution_policy == {'coordinator': default_pin}
            assert await child_policy(default_pin) == baseline.intersect(scope.Scope(coordinator=default_pin))
            await open_saved(app, demands[1]['id'])
            assert app.selection().execution_policy == {'coordinator': pins[1]}
            assert pins[1] in str(app.query_one('#execution-scope').render())
            assert await child_policy(pins[1]) == baseline.intersect(scope.Scope(coordinator=pins[1]))
            assert await child_policy() == baseline
            assert scope.current() == baseline
            assert app.process is None and not app.busy
    class PublicConsole(WorkConsole):
        def __init__(self, root, **options):
            options.update(backend=Backend(), initial_mode='conversation', discover_clis=False)
            super().__init__(root, **options)

        def run(self):
            asyncio.run(scenario(self))

    from pathlib import Path
    template = json.loads((Path(native_catalog.__file__).parent / 'native_deployments' /
                           'vendor_cli_templates.json').read_text(encoding='utf8'))['agy-cli']
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': 'agy-cli'}])
    monkeypatch.setattr(native_catalog, 'select', lambda name: template)
    monkeypatch.setattr(native_console, 'WorkConsole', PublicConsole)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.delenv('TARE_OS_ROOT', raising=False)
    monkeypatch.setenv('TARE_MANAGED_HOME', str(tmp_path / 'managed'))
    arguments = ['--coordinator', default_pin] if default_pin else []
    assert native_installation.main(['console', str(root), *arguments]) == 0
    assert os.environ.get(scope.ENV) == original_env
    assert [p.read_bytes() for p in paths] == before


def test_coordinator_context_is_task_local_and_resets_on_error(monkeypatch):
    import asyncio
    monkeypatch.delenv(scope.ENV, raising=False)
    async def pinned(pin):
        with pytest.raises(RuntimeError):
            with scope.for_coordinator(pin):
                await asyncio.sleep(0)
                assert scope.current().coordinator == pin
                assert scope.ENV not in os.environ
                raise RuntimeError('offline fixture')
        assert scope.current() == scope.Scope()
    async def scenario():
        await asyncio.gather(pinned('google/gemini-3.8-flash@high'),
                             pinned('openai/gpt-6.1-sol@high'))
    asyncio.run(scenario())
    assert scope.ENV not in os.environ
