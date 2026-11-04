"""Ordinary native sessions need no hand-written configuration file."""
import json

import pytest

from relay import native_agent


def test_plain_start_passes_exact_verifier_argv_and_optional_limits(monkeypatch, tmp_path, capsys):
    seen = {}
    def run(config, deployment, custody, instruction, **kwargs):
        seen.update(config=config, deployment=deployment, limits=kwargs['limits'])
        return {'status': 'PROPOSED', 'output': 'Done'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    command = ['python', '-m', 'pytest', 'test with spaces.py', '-q']
    assert native_agent.main(['--root', str(tmp_path), '--session', str(tmp_path/'session'),
        '--role', 'implementer', '--instruction', 'Fix the bug', '--no-context', '--unlimited', '--max-model-calls', '12',
        '--verify-command', *command]) == 0
    config = seen['config']
    assert set(config['capabilities']) == {'read', 'list', 'search', 'edit', 'verify'}
    assert config['verifiers']['check']['argv'] == command
    assert config['specgraph_edit_context'] is False
    assert config['context_policy']['max_compactions'] is None
    assert seen['limits']['max_model_calls'] == 12
    assert seen['limits']['max_seconds'] is None and seen['limits']['read_seconds'] is None
    from relay.native_limits import resolve
    assert resolve(seen['deployment'], seen['limits'])['max_model_retries'] == resolve(seen['deployment'])['max_model_retries']
    assert seen['deployment']['native_profile']['context_window'] > 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PROPOSED'


def test_unlimited_keeps_the_per_request_bounds_the_deployment_declares(monkeypatch, tmp_path):
    from test_native_agent import deployment
    seen = {}
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kwargs: seen.update(kwargs['limits']) or {'status': 'PROPOSED'})
    profile = deployment()
    profile['native_execution_limits'] = {'max_seconds': 900, 'request_seconds': 300}
    (tmp_path/'profile.json').write_text(json.dumps(profile))
    assert native_agent.main(['--root', str(tmp_path), '--session', str(tmp_path/'session'), '--instruction', 'Hi',
                              '--native-deployment', str(tmp_path/'profile.json'), '--unlimited']) == 0
    assert seen['max_seconds'] is None and seen['max_model_calls'] is None
    assert seen['request_seconds'] == 300 and seen['read_seconds'] is None


@pytest.mark.parametrize('retries', [0, 1, 3])
def test_unlimited_resources_preserve_the_profiles_retry_policy(monkeypatch, tmp_path, retries):
    from test_native_agent import deployment
    from relay.native_limits import resolve
    seen = {}
    def run(config, selected, *args, **kwargs):
        seen.update(resolve(selected, kwargs['limits']))
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    profile = deployment(); profile['native_execution_limits'] = {'max_model_retries': retries}
    path = tmp_path/'profile.json'; path.write_text(json.dumps(profile))
    assert native_agent.main(['--root', str(tmp_path), '--session', str(tmp_path/'session'),
        '--native-deployment', str(path), '--instruction', 'Hello', '--unlimited']) == 0
    assert seen['max_model_calls'] is None and seen['max_seconds'] is None
    assert seen['max_model_retries'] == retries


def test_regular_start_keeps_inherited_limits_and_offers_search(monkeypatch, tmp_path):
    def run(config, deployment, custody, instruction, **kwargs):
        assert kwargs['limits'] == {}
        assert 'context' in config['capabilities'] and 'search' in config['capabilities']
        assert config['verifiers'] == {} and 'context_policy' not in config
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert native_agent.main(['--root', str(tmp_path), '--session', str(tmp_path/'session'),
                              '--instruction', 'Hello']) == 0


@pytest.mark.parametrize('args', [
    ['--resume', '--session', 'saved', '--no-context'],
    ['--resume', '--session', 'saved', '--unlimited'],
    ['--resume', '--session', 'saved', '--verify-command', 'python'],
    ['--verify-command'], ['--config', 'file.json', '--no-context'],
    ['--role', 'explorer', '--verify-command', 'python'],
])
def test_invalid_start_options_do_not_invoke_a_model(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid startup options reached the runner')
    monkeypatch.setattr(native_agent, 'run_session', forbidden)
    assert native_agent.main(args) == 2
