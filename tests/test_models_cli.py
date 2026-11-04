"""Offline connection projection and public models entry points."""
from copy import deepcopy
import os
from pathlib import Path
import shlex
import json
from types import SimpleNamespace

import pytest

from relay import execution_scope as scope, model_choices as models, native_catalog
from relay import native_routes, turn_engine, environment, work_audit
from test_model_choices import profile


@pytest.fixture(autouse=True)
def discovery_guards(monkeypatch, tmp_path):
    """Public discovery/help must remain read-only outside fixture-owned files."""
    import socket
    import subprocess
    from relay import native_generation, cli_discovery

    def forbidden(*args, **kwargs):
        pytest.fail('Discovery attempted network, inference, secrets or live discovery')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(native_generation, 'vault_secret', forbidden)
    monkeypatch.setattr(native_generation, 'generate_text', forbidden)
    monkeypatch.setattr(native_generation, 'inference_client', forbidden)
    monkeypatch.setattr(cli_discovery, 'discover', forbidden)
    monkeypatch.setattr(environment, 'ask_gateway', forbidden)
    monkeypatch.setattr(native_routes, 'ranked', forbidden)
    for method in ('write_text', 'write_bytes', 'mkdir'):
        original = getattr(Path, method)
        def guarded(path, *args, _original=original, **kwargs):
            assert path.resolve().is_relative_to(tmp_path.resolve()), 'write outside fixture: '+str(path)
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(Path, method, guarded)


@pytest.fixture
def catalog(monkeypatch):
    value = profile(model='claude-opus-5-5', effort='medium')
    value['id'] = 'test-cli'
    values = {'test-cli': value}
    monkeypatch.setattr(native_catalog, 'offline_deployments', lambda **kw: (
        (name, deepcopy(item), True) for name, item in values.items()))
    monkeypatch.setattr(native_catalog, 'select', lambda name: deepcopy(values[name]))
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': name} for name in values])
    monkeypatch.setattr(native_routes, 'configured', lambda: {'deployments': [
        {'id': 'test-cli', 'native_catalog': 'test-cli', 'roles': list(models.ROLES)}]})
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'researcher': ['test-cli']})
    monkeypatch.setattr(environment, 'read', lambda: {})
    monkeypatch.setattr(environment, 'listed', lambda *a: True)
    monkeypatch.setattr(work_audit, 'identity', lambda value, role: SimpleNamespace(
        vendor='Anthropic', model_id=value.get('model'), bias_domain='anthropic'))
    monkeypatch.delenv(scope.ENV, raising=False)
    return values


def test_projection_is_offline_and_selectors_round_trip(catalog, monkeypatch):
    from relay import native_generation
    monkeypatch.setattr(native_generation, 'vault_secret', lambda *a: pytest.fail('secret resolution'))
    monkeypatch.setattr(native_routes, 'ranked', lambda *a, **kw: pytest.fail('live ranking'))
    before = deepcopy(catalog)
    for role in models.ROLES:
        rows = models.projection(role=role)
        assert len(rows) == 1
        row = rows[0]
        assert row['vendor'] == 'Anthropic' and row['provider'] == 'anthropic'
        assert row['via'] == 'cli' and row['quota'] == row['availability'] == 'UNVERIFIED'
        if role == 'planner':
            assert set(row['allowed_reasoning']) == {'high', 'xhigh', 'max'}
        for selector in row['selectors']:
            value = models.select_exact(selector, role)
            assert scope.pin_matches(selector, value)
    assert catalog == before


def test_filters_and_unconfigured_metadata(catalog, monkeypatch):
    assert models.projection(via='api') == []
    assert models.projection(vendor='OpenAI') == []
    assert models.projection(provider='openai') == []
    assert len(models.projection(vendor='anthropic', provider='anthropic', via='cli')) == 1
    monkeypatch.setattr(environment, 'listed', lambda *a: False)
    assert models.projection() == []
    row = models.projection(include_unconfigured=True)[0]
    assert not row['configured'] and row['availability'] == 'UNVERIFIED'
    with pytest.raises(ValueError, match='tare models'):
        models.select_exact('anthropic/test-cli@high', 'planner')


def test_models_public_text_json_and_invalid_arguments(catalog, capsys):
    import tare_ops
    assert tare_ops.main(['models', '--role', 'planner', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['models'][0]['allowed_reasoning'] == ['high', 'xhigh', 'max']
    assert tare_ops.main(['models', '--via', 'cli']) == 0
    assert 'UNVERIFIED' in capsys.readouterr().out
    for argv in (['--via', 'network'], ['--role', 'unknown'], ['--unexpected']):
        with pytest.raises(SystemExit):
            models.main(argv)


def test_bootstrap_help_and_manager_forwarding(tmp_path, capsys):
    from relay import native_product
    for role in scope.PIN_ROLES:
        argv = ['--home', str(tmp_path), '--'+role, 'anthropic/test-cli@high', './project']
        forwarded = native_product.console_arguments(argv)
        assert forwarded[:3] == ['--home', str(tmp_path), 'console']
        assert forwarded[3:] == argv[2:]
    with pytest.raises(SystemExit) as error:
        native_product.main(['--home', str(tmp_path), '--help'])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert 'tare models' in help_text and '--researcher' in help_text
    assert not list(tmp_path.iterdir())


def test_installed_models_needs_no_graph(catalog, tmp_path, capsys):
    from relay import native_installation, relay_mesh
    def rebind(root):
        assert root == tmp_path
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(relay_mesh, 'rebind_root', rebind)
        assert native_installation.main(['--os-root', str(tmp_path), 'models', '--json']) == 0
        assert json.loads(capsys.readouterr().out)['models']
    finally:
        monkey.undo()


@pytest.mark.parametrize('flag,diagnostic', [('--vendor', 'Unknown vendor'), ('--provider', 'Unknown provider')])
def test_unknown_filter_is_not_an_empty_intersection(catalog, capsys, flag, diagnostic):
    import tare_ops
    with pytest.raises(SystemExit) as error:
        tare_ops.main(['models', flag, 'typo', '--json'])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert diagnostic in captured.err and 'tare models --include-unconfigured' in captured.err
    assert not captured.out


def test_known_domains_do_not_depend_on_setup_or_other_filters(catalog, monkeypatch, capsys):
    import tare_ops
    for args in (['--vendor', 'OpenAI', '--provider', 'CLAUDE'],
                 ['--vendor', 'ANTHROPIC', '--provider', 'AWS']):
        assert tare_ops.main(['models', *args, '--json']) == 0
        assert json.loads(capsys.readouterr().out)['models'] == []
    assert tare_ops.main(['models', '--vendor', 'OpenAI']) == 0
    assert 'No models match' in capsys.readouterr().out
    monkeypatch.setattr(environment, 'listed', lambda *a: False)
    assert tare_ops.main(['models', '--provider', 'ClAuDe', '--include-unconfigured', '--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert not row['configured'] and not row['selectors']
    assert all(not arguments for arguments in row['role_arguments'].values())


@pytest.mark.parametrize('transport', ['openrouter', 'local'])
def test_failed_identity_never_uses_transport_as_author(catalog, monkeypatch, capsys, transport):
    value = catalog['test-cli']
    value['native_profile'].update(protocol='openai-chat',
        endpoint='https://openrouter.ai/api/v1' if transport == 'openrouter' else 'http://127.0.0.1:8080')
    def unknown(*args):
        raise ValueError('IDENTITY_RUNTIME_ALIAS_MISMATCH')
    monkeypatch.setattr(work_audit, 'identity', unknown)
    assert models.main(['--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert row['vendor'] == 'UNKNOWN' and row['provider'] == transport
    assert models.main([]) == 0
    assert 'Author: UNKNOWN' in capsys.readouterr().out


@pytest.mark.parametrize('option', ['--cli_only', '--cli-only', '--subscription_only', '--subscription-only',
                                    '--prefer_cli', '--prefer-cli', '--prefer_subscription', '--prefer-subscription'])
def test_discovery_reuses_policy_aliases_and_optional_lists(catalog, capsys, option):
    assert models.main(['--role', 'planner', option, 'anthropic/test-cli@high', 'anthropic/test-cli@max', '--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert row['allowed_reasoning'] == ['high', 'max']
    assert models.main([option, '--json']) == 0
    assert json.loads(capsys.readouterr().out)['models']


def test_discovery_intersects_inherited_policy_without_activation(catalog, monkeypatch, capsys):
    from dataclasses import asdict
    inherited = scope.Scope(only='subscription', restrictions=(('subscription', 'anthropic/test-cli@high,max'),)).validate()
    monkeypatch.setenv(scope.ENV, json.dumps(asdict(inherited), indent=1))
    before = dict(os.environ)
    view, role = scope.POLICY_VIEW.get(), scope.ROLE.get()
    assert models.main(['--role', 'planner', '--cli-only=anthropic/test-cli@max', '--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert row['allowed_reasoning'] == ['max']
    for argument in row['role_arguments']['planner']:
        assert scope.pin_matches(argument.split()[1], models.select_exact(argument.split()[1], 'planner',
            scope=inherited.intersect(scope.Scope(restrictions=(('subscription', 'anthropic/test-cli@max'),)))))
    with pytest.raises(SystemExit):
        models.main(['--api-only', '--json'])
    assert 'conflict' in capsys.readouterr().err.lower()
    assert dict(os.environ) == before and scope.POLICY_VIEW.get() is view and scope.ROLE.get() == role


def test_text_metadata_and_all_role_level_arguments_round_trip(catalog, capsys):
    for role in models.ROLES:
        row = models.projection(role=role)[0]
        for argument in row['role_arguments'][role]:
            pin = argument.split()[1]
            assert scope.pin_matches(pin, models.select_exact(pin, role))
    assert models.main(['--role', 'planner']) == 0
    text = capsys.readouterr().out
    for label in ('Model: Claude Opus', 'ID:', 'Author:', 'Connection:', 'Access:',
                  'Local configuration:', 'Reasoning supported:', 'Reasoning permitted:', 'Copy:'):
        assert label in text
    assert 'authentication UNVERIFIED' in text
    assert text.count('anthropic/test-cli@high') == 1
    assert models.main(['--role', 'planner', '--cli-only=anthropic/test-cli@low', '--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert not row['allowed_reasoning'] and not row['selectors'] and not row['role_arguments']['planner']


def test_shared_public_help_examples_are_local_and_selectable(catalog, capsys):
    import tare_ops
    from relay import native_console, console_entry, native_installation
    summary = models.help_summary()
    before = deepcopy(catalog)
    seen_roles = set()
    for line in summary.splitlines():
        if line.startswith('Copy for '):
            role = line.split(':', 1)[0].removeprefix('Copy for ')
            argument = shlex.split(line.split(': ', 1)[1])[-1]
            seen_roles.add(role)
            assert scope.pin_matches(argument, models.select_exact(argument, role))
    assert seen_roles == set(models.ROLES) and catalog == before
    assert 'tare models --role coordinator' in summary
    assert summary in console_entry.help_text()
    for entry, arguments in ((tare_ops.main, ['--help']), (native_console.main, ['--help']),
                             (native_installation.main, ['console', '--help'])):
        with pytest.raises(SystemExit) as error:
            entry(arguments)
        text = capsys.readouterr().out
        assert error.value.code == 0 and summary in text
    with pytest.raises(SystemExit):
        models.main(['--help'])
    text = capsys.readouterr().out
    assert '--planner' in text and 'No fallback' in text and 'not author' in text


@pytest.mark.parametrize('form', ['separate', 'equals'])
@pytest.mark.parametrize('installation', ['absent', 'incomplete', 'complete'])
def test_manager_help_selects_home_without_bootstrap_or_fallback(catalog, tmp_path, monkeypatch, capsys, form, installation):
    from relay import native_product as product
    home = tmp_path/'selected'
    other = tmp_path/'other'
    monkeypatch.setenv('TARE_PRODUCT_HOME', str(other))
    if installation != 'absent':
        home.mkdir()
        identity = 'a'*64
        release = product.release_at(home, identity)
        release.mkdir(parents=True)
        (home/'active.json').write_text(json.dumps({'current': identity}))
        if installation == 'complete':
            (release/'ready.json').write_text('{}')
            python = product.python_at(release)
            python.parent.mkdir(parents=True)
            python.write_text('fixture executable')
    summary = models.help_summary()
    seen = []
    def helper(command, **options):
        seen.append(command)
        assert command[:4] == [str(product.python_at(release)), '-I', '-B', '-c']
        assert command[4].endswith('from relay.model_choices import help_summary; print(help_summary())')
        assert 'native_installation import main' not in command[4]
        assert options['env']['TARE_MANAGED_HOME'] == str(home.resolve())
        assert options['env']['TARE_STORAGE_ROOT'] == str(home.resolve()/'state')
        assert options['check'] and options['timeout'] == 15
        return SimpleNamespace(stdout=summary)
    monkeypatch.setattr(product.subprocess, 'run', helper)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    argv = ['--home', str(home)] if form == 'separate' else ['--home='+str(home)]
    with pytest.raises(SystemExit) as error:
        product.main([*argv, '--help'])
    text = capsys.readouterr().out
    assert error.value.code == 0
    if installation == 'complete':
        assert len(seen) == 1 and summary in text
    else:
        assert not seen and 'no configured example proved' in text
        assert 'syntax' in text and 'Copy for ' not in text
    assert not other.exists()
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_standalone_manager_help_does_not_import_relay(tmp_path, capsys):
    import builtins
    from relay import native_product
    source = Path(native_product.__file__).read_text(encoding='utf-8')
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        assert name != 'relay' and not name.startswith('relay.'), 'bootstrap imported relay'
        return original(name, *args, **kwargs)
    namespace = {'__name__': 'standalone_manager', '__builtins__': {**vars(builtins), '__import__': importing}}
    exec(compile(source, 'standalone_manager.py', 'exec'), namespace)
    with pytest.raises(SystemExit) as error:
        namespace['main'](['--home='+str(tmp_path/'absent'), '--help'])
    assert error.value.code == 0 and 'tare models' in capsys.readouterr().out
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('entry', ['tare', 'installed'])
def test_discovered_arguments_reach_public_console_without_inference(catalog, tmp_path, monkeypatch, entry):
    import tare_ops
    from relay import native_console, native_installation, coordinator_policy, route_facts
    # Keep the real coordinator policy and realization; isolate only live resource facts.
    monkeypatch.setattr(route_facts, 'collect', lambda *a, **kw: {
        'quota': {'status': 'UNKNOWN'}, 'resource': {}})
    monkeypatch.setattr(environment, 'allows', lambda value: scope.current().allows(value))
    monkeypatch.setattr(native_console.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(native_console.sys.stdout, 'isatty', lambda: True)
    seen = []
    class Console:
        def __init__(self, root, **options):
            assert root == tmp_path.resolve()
            self.coordinator = options['default_coordinator']
        def run(self):
            if self.coordinator:
                @scope.session_role
                def capture(config, value, custody, **kwargs):
                    seen.append(('coordinator', deepcopy(value)))
                with scope.for_coordinator(self.coordinator):
                    capture({}, None, tmp_path, conversation=True)
            else:
                policy = scope.current()
                role, pin = policy.pins[0]
                seen.append((role, models.select_exact(pin, role)))
    monkeypatch.setattr(native_console, 'WorkConsole', Console)
    before = deepcopy(catalog)
    before_env = os.environ.get(scope.ENV)
    invoke = tare_ops.main if entry == 'tare' else native_installation.main
    for role in models.ROLES:
        row = models.projection(role=role)[0]
        for argument in row['role_arguments'][role]:
            flag, selector = shlex.split(argument)
            expected_role = 'auditor' if role == 'plan-auditor' else role
            assert flag == '--'+expected_role
            assert invoke(['console', str(tmp_path), flag, selector]) == 0
            actual_role, value = seen.pop()
            assert actual_role == expected_role and scope.pin_matches(selector, value)
    assert os.environ.get(scope.ENV) == before_env and catalog == before and not seen
    # Supported by the model, but explicitly incompatible with coordinator policy.
    with pytest.raises(ValueError, match='Hosted coordinators require high'):
        invoke(['console', str(tmp_path), '--coordinator', 'anthropic/test-cli@medium'])
    assert not seen and catalog == before and not list(tmp_path.iterdir())


def test_coordinator_discovery_uses_explicit_policy_not_route_roles(catalog, monkeypatch, capsys):
    import tare_ops
    monkeypatch.setattr(native_routes, 'configured', lambda: {'deployments': []})
    before = deepcopy(catalog)
    assert tare_ops.main(['models', '--role', 'coordinator', '--via', 'cli',
                          '--vendor', 'Anthropic', '--provider', 'claude', '--json']) == 0
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert row['allowed_reasoning'] == ['high']
    assert row['selectors'] == ['anthropic/test-cli@high']
    assert row['role_arguments'] == {'coordinator': ['--coordinator anthropic/test-cli@high']}
    assert tare_ops.main(['models', '--role', 'coordinator']) == 0
    text = capsys.readouterr().out
    assert 'Copy: tare console ./project --coordinator anthropic/test-cli@high' in text
    assert 'Reasoning permitted: high' in text and 'quota UNVERIFIED' in text
    assert models.projection(role='coordinator', via='api') == []
    restricted = scope.Scope(only='api').validate()
    assert not models.projection(role='coordinator', scope=restricted)[0]['selectors']
    monkeypatch.setattr(environment, 'listed', lambda *a: False)
    assert models.projection(role='coordinator') == []
    assert not models.projection(role='coordinator', include_unconfigured=True)[0]['selectors']
    assert catalog == before and 'coordinator' not in scope.PIN_ROLES
    catalog['test-cli']['native_profile']['model'] = 'claude-sonnet-5-5'
    assert models.projection(role='coordinator', include_unconfigured=True) == []


@pytest.mark.parametrize('deployment', ['qwen38-gsq', 'swift-next', 'gemini-api-3-8-flash'])
def test_coordinator_declared_local_and_hosted_levels_round_trip(monkeypatch, deployment):
    from relay import coordinator_policy, route_facts
    value = native_catalog.select(deployment)
    before = deepcopy(value)
    monkeypatch.setattr(native_catalog, 'offline_deployments', lambda **kw: [(deployment, deepcopy(value), True)])
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': deployment}])
    monkeypatch.setattr(native_catalog, 'select', lambda name: deepcopy(value))
    monkeypatch.setattr(native_routes, 'configured', lambda: {'deployments': []})
    monkeypatch.setattr(environment, 'read', lambda: {})
    monkeypatch.setattr(environment, 'listed', lambda *a: True)
    monkeypatch.setattr(environment, 'allows', lambda value: (True, None))
    monkeypatch.setattr(native_routes, 'credential_available', lambda key: True)
    monkeypatch.setattr(route_facts, 'collect', lambda *a, **kw: {
        'quota': {'status': 'UNKNOWN'}, 'resource': {}})
    row = models.projection(role='coordinator')[0]
    expected = ['high'] if deployment.startswith('gemini') else ['low', 'medium', 'xhigh']
    assert row['allowed_reasoning'] == expected
    for selector in row['selectors']:
        assert scope.pin_matches(selector, coordinator_policy.select(selector))
    assert value == before


def test_implementer_discovery_matches_the_configured_work_ladder(catalog,monkeypatch):
    monkeypatch.setattr(native_routes,'configured',lambda:{'deployments':[
        {'id':'test-cli','native_catalog':'test-cli','roles':['auditor']}]})
    monkeypatch.setattr(turn_engine,'policy',lambda:{'researcher':[],'implementer':[['test-cli']]})
    rows=models.projection(role='implementer')
    assert len(rows)==1 and 'anthropic/test-cli@max' in rows[0]['selectors']
    from test_role_pins import parse
    with scope.activated(parse(['--implementer','anthropic/test-cli@max'])):
        assert scope.pin_matches('anthropic/test-cli@max',scope.pinned('implementer'))
