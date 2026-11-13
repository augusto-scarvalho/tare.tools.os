import json
import os

from relay import cli_discovery, native_profiles
from relay.native_generation import load_deployment


def test_found_clis_offer_their_models_without_a_call_and_a_proof_makes_one_ready(tmp_path, monkeypatch):
    from relay import environment
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.delenv('TARE_PROFILE_DIR', raising=False)
    claude, agy = tmp_path/'claude.exe', tmp_path/'agy.exe'
    claude.write_bytes(b'v1'); agy.write_bytes(b'v1')
    monkeypatch.setattr(cli_discovery, 'locate',
                        {'cli://claude': str(claude), 'cli://agy': str(agy), 'cli://codex': None}.get)
    calls = []
    monkeypatch.setattr(cli_discovery, 'acknowledge', lambda value: calls.append(value) or (True, ''))
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    claude_names = sorted(n for n, t in templates.items() if t['native_profile']['endpoint'] == 'cli://claude')
    existing = native_profiles.directory()/'claude-sonnet-5-5-cli.json'
    existing.parent.mkdir(parents=True)
    existing.write_text('{"kept": true}', encoding='utf-8')

    report = {r['endpoint']: r for r in cli_discovery.discover()}
    assert calls == []  # finding a CLI spends nothing
    assert report['cli://codex']['status'] == 'missing'
    assert report['cli://agy']['status'] == 'found'
    assert report['cli://agy']['added'] == ['agy-cli', 'agy-gemini-4-argon-cli', 'agy-gemini-4-hydrogen-cli']
    argon = load_deployment(native_profiles.local_path('agy-gemini-4-argon-cli'))
    assert argon['native_cli']['executable'] == str(agy)
    assert argon['native_profile']['model'] == 'gemini-4-argon-high'
    assert argon['native_reasoning_models'] == {
        effort: 'gemini-4-argon-'+effort for effort in ('low', 'medium', 'high')}
    assert report['cli://claude']['added'] == [n for n in claude_names if n != 'claude-sonnet-5-5-cli']
    assert json.loads(existing.read_text(encoding='utf-8')) == {'kept': True}
    added = load_deployment(native_profiles.local_path('claude-opus-5-5-cli'))
    assert added['native_cli']['executable'] == str(claude)
    assert f'Claude CLI found · {len(claude_names)-1} model(s) added' in cli_discovery.summary(report.values())
    haiku = load_deployment(native_profiles.local_path('claude-haiku-5-5-cli'))
    assert haiku['native_profile']['model'] == 'claude-haiku-5-5'
    assert json.loads(haiku['native_profile']['parameters_json']) == {'effort': 'low'}
    assert cli_discovery.summary(cli_discovery.discover()) == ''  # nothing new: nothing said

    shown = []
    assert environment.prove('cli:agy', lambda line: shown.append(line) or True)['ok']
    assert len(calls) == 1 and 'no dollars' in shown[0]
    report = {r['endpoint']: r for r in cli_discovery.discover()}
    assert report['cli://agy']['status'] == 'ready' and report['cli://claude']['status'] == 'found'
    assert environment.prove('cli:agy', lambda line: False)['reason'] == 'not confirmed' and len(calls) == 1
    claude.write_bytes(b'v2 longer')  # an update keeps its models; a proof is the owner's call again
    assert {r['endpoint']: r['status'] for r in cli_discovery.discover()}['cli://claude'] == 'found'


def test_a_cli_that_is_not_signed_in_offers_nothing_and_says_how_to_sign_in(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.delenv('TARE_PROFILE_DIR', raising=False)
    claude, codex = tmp_path/'claude.exe', tmp_path/'codex.exe'
    claude.write_bytes(b'v1'); codex.write_bytes(b'v1')
    monkeypatch.setattr(cli_discovery, 'locate', {'cli://claude': str(claude), 'cli://codex': str(codex), 'cli://agy': None}.get)
    asked = []
    def run(argv, **kw):  # the CLIs' own sign-in status: local, no model call
        asked.append(argv[1:])
        out = '{"loggedIn": false}' if argv[0] == str(claude) else 'Not logged in'
        return subprocess.CompletedProcess(argv, 0 if argv[0] == str(claude) else 1, out, '')
    monkeypatch.setattr(cli_discovery.subprocess, 'run', run)
    report = {r['endpoint']: r for r in cli_discovery.discover()}
    assert sorted(asked) == [['auth', 'status'], ['login', 'status']]
    assert report['cli://codex']['status'] == 'failed' and report['cli://codex']['added'] == []
    assert report['cli://claude']['added'] == [] and not native_profiles.directory().exists()
    assert 'Codex CLI found but not usable: not signed in (run codex login)' in cli_discovery.summary(report.values())


def test_cmd_shims_are_not_offered(tmp_path, monkeypatch):
    shim = tmp_path/'codex.cmd'; shim.write_text('@echo off')
    monkeypatch.setattr(cli_discovery, 'host_executable', lambda name: str(shim))
    monkeypatch.setitem(cli_discovery.LOCATIONS, 'cli://codex', ('codex', []))
    assert cli_discovery.locate('cli://codex') is None


def test_discovery_cannot_adopt_a_current_directory_cli(tmp_path, monkeypatch):
    project, host = tmp_path/'project', tmp_path/'host'
    project.mkdir(); host.mkdir()
    name = 'codex.exe' if os.name == 'nt' else 'codex'
    for directory in [project, host]:
        path = directory/name
        path.write_bytes(b'fixture program; never executed')
        path.chmod(0o755)
    monkeypatch.chdir(project)
    monkeypatch.setenv('PATH', os.pathsep.join(['.', str(host)]))
    monkeypatch.setitem(cli_discovery.LOCATIONS, 'cli://codex', ('codex', []))
    assert cli_discovery.locate('cli://codex') == str(host/name)
    monkeypatch.setenv('PATH', '.')
    assert cli_discovery.locate('cli://codex') is None


def test_login_observation_uses_the_host_launcher_directory(tmp_path, monkeypatch):
    import subprocess
    host = tmp_path/'host'
    executable = host/'codex.exe'
    observed = []
    def run(argv, **kwargs):
        observed.append(kwargs['cwd'])
        return subprocess.CompletedProcess(argv, 0, 'Signed in', '')
    monkeypatch.setattr(cli_discovery.subprocess, 'run', run)
    assert cli_discovery.logged_in('cli://codex', str(executable)) is True
    assert observed == [str(host)]


def test_console_reports_news_in_status_and_only_failures_as_notice(tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import Static
    from relay import relay_mesh
    from relay.console_backend import Backend
    from relay.native_console import WorkConsole
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'no-control-graph')
    setup = tmp_path/'environment.json'  # a set-up machine: the first-run notice is not this test's subject
    setup.write_text(json.dumps({'schema': 'tare.tools/environment/1', 'clis': {'use': True}}))
    monkeypatch.setenv('TARE_ENVIRONMENT_STORE', str(setup))
    monkeypatch.setattr(cli_discovery, 'discover', lambda: [
        {'label': 'Claude CLI', 'status': 'ready', 'added': ['claude-opus-5-5-cli']},
        {'label': 'AGY CLI', 'status': 'failed', 'reason': 'AUTH_FAILED', 'added': []}])
    project = tmp_path/'project'; project.mkdir()

    async def scenario(discover):
        app = WorkConsole(project, backend=Backend(), initial_mode='assistant',
                          storage_preferred=tmp_path/'state', discover_clis=discover)
        shown = []
        app.notify = lambda message, **kwargs: shown.append(message)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            return str(app.query_one('#status', Static).render()), shown
    readings = []
    monkeypatch.setattr('relay.provider_quota.refresh', lambda: readings.append('read'))
    status, shown = asyncio.run(scenario(True))
    assert 'Claude CLI ready · 1 model(s) added' in status
    assert shown == ['AGY CLI found but not usable: AUTH_FAILED']
    assert readings == ['read']  # the same start reads what each provider has left
    status, shown = asyncio.run(scenario(False))  # tests and fixtures never probe
    assert 'Claude CLI' not in status and shown == [] and readings == ['read']


def test_codex_models_follow_codex_and_only_discovery_profiles_are_updated(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.delenv('TARE_PROFILE_DIR', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'codex-home'))
    old, new = tmp_path/'old'/'codex.exe', tmp_path/'new'/'codex.exe'
    for exe in (old, new):
        exe.parent.mkdir(); exe.write_bytes(b'codex')
    where = {'cli://claude': None, 'cli://agy': None, 'cli://codex': str(old)}
    monkeypatch.setattr(cli_discovery, 'locate', lambda endpoint: where[endpoint])
    def record(slug, visibility='list', context=272000):
        return {'slug': slug, 'visibility': visibility, 'context_window': context, 'default_reasoning_level': 'medium',
                'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}]}
    cache = tmp_path/'codex-home'/'models_cache.json'
    cache.parent.mkdir()
    cache.write_text(json.dumps({'client_version': '0.158.0', 'models': [
        record('gpt-6-astra'), record('gpt-6-luna'), record('gpt-reserve', 'hide')]}), encoding='utf-8')

    report = cli_discovery.discover()
    codex = next(r for r in report if r['endpoint'] == 'cli://codex')
    # Exactly what Codex lists for this account: not the shipped template list, not hidden models.
    assert codex['added'] == ['codex-cli', 'codex-cli-gpt-6-luna']
    luna = load_deployment(native_profiles.local_path('codex-cli-gpt-6-luna'))
    assert luna['native_cli']['executable'] == str(old) and luna['native_cli']['model_catalog']['slug'] == 'gpt-6-luna'

    # Codex moves (reinstall/update): the profiles discovery wrote follow the executable.
    where['cli://codex'] = str(new)
    codex = next(r for r in cli_discovery.discover() if r['endpoint'] == 'cli://codex')
    assert codex['updated'] == ['codex-cli', 'codex-cli-gpt-6-luna'] and codex['added'] == []
    assert load_deployment(native_profiles.local_path('codex-cli-gpt-6-luna'))['native_cli']['executable'] == str(new)
    assert 'Codex CLI · 2 model(s) updated' in cli_discovery.summary(
        [codex])

    # A profile the user edited is theirs from then on.
    path = native_profiles.local_path('codex-cli-gpt-6-luna')
    edited = json.loads(path.read_text(encoding='utf-8'))
    edited['native_profile']['output_tokens'] = 8192
    path.write_text(json.dumps(edited), encoding='utf-8')
    where['cli://codex'] = str(old)
    codex = next(r for r in cli_discovery.discover() if r['endpoint'] == 'cli://codex')
    assert codex['updated'] == ['codex-cli']
    assert json.loads(path.read_text(encoding='utf-8')) == edited

    # A model Codex stops listing is removed; the user-edited profile stays.
    def listed(*slugs):
        cache.write_text(json.dumps({'client_version': '0.158.0', 'models': [record(s) for s in slugs]}), encoding='utf-8')
        return next(r for r in cli_discovery.discover() if r['endpoint'] == 'cli://codex')
    assert listed('gpt-6-astra', 'gpt-6-sol')['added'] == ['codex-cli-gpt-6-sol']
    codex = listed('gpt-6-astra')
    assert codex['removed'] == ['codex-cli-gpt-6-sol']
    assert not native_profiles.local_path('codex-cli-gpt-6-sol').exists() and path.exists()
    assert 'Codex CLI · 1 retired model(s) removed' in cli_discovery.summary([codex])


def test_console_pins_are_checked_in_its_history_folder_once_discovery_wrote_the_profiles(tmp_path, monkeypatch):
    """A new --storage-root has no CLI profiles yet; the first message failed 'Pin is unknown' (2026-11-03)."""
    from types import SimpleNamespace
    from relay import execution_scope, native_console
    monkeypatch.setattr(native_console.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(native_console.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'default'))
    monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
    project, history = tmp_path/'project', tmp_path/'history'
    project.mkdir()
    written, checked = [], []

    def check(args, **_):
        checked.append(os.environ['TARE_STORAGE_ROOT'])
        if not written:
            raise ValueError('Pin is unknown, unconfigured or incompatible; consult tare models --role planner.')
        return execution_scope.Scope()
    monkeypatch.setattr(execution_scope, 'from_arguments', check)
    monkeypatch.setattr(cli_discovery, 'discover', lambda: written.append(os.environ['TARE_STORAGE_ROOT']))
    monkeypatch.setattr(native_console, 'WorkConsole', lambda *a, **k: SimpleNamespace(run=lambda: None))
    assert native_console.main([str(project), '--storage-root', str(history), '--planner', 'openai/x@high']) == 0
    assert checked == [str(history.resolve())]*2 and written == [str(history.resolve())]
