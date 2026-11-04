"""Session discovery and explicit last-session continuation, without inference."""
import json
from pathlib import Path

import pytest

from relay import native_agent, native_sessions
import tare_ops


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    home = tmp_path/'sessions'
    home.mkdir()
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', home)
    return home


def save(home, name, repo, stamp, status='PROPOSED'):
    directory = home/name
    directory.mkdir()
    (directory/'native-agent.json').write_text(json.dumps({
        'schema': 'tare.tools/native-agent/2', 'session_id': name,
        'instruction': 'Repair a CLI failure\nAdditional context',
        'config': {'repositories': {'repo': str(repo)}, 'role': 'implementer'},
        'deployment': {'id': 'selected-route', 'native_profile': {'model': 'local-or-http'}},
        'invocations': [{'started_at': stamp-10, 'ended_at': stamp, 'status': status}],
    }))
    return directory


def test_lists_existing_sessions_read_only_and_filters_exact_root(catalog, tmp_path, capsys):
    project, other = tmp_path/'project', tmp_path/'other'
    old = save(catalog, 'old', project, 100)
    newer = save(catalog, 'new', project, 200, 'INTERRUPTED')
    save(catalog, 'other', other, 300)
    before = {p: p.read_bytes() for p in catalog.rglob('*') if p.is_file()}
    assert tare_ops.main(['sessions', '--root', str(project), '--format', 'json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r['session_id'] for r in result['sessions']] == ['new', 'old']
    assert result['sessions'][0]['last_status'] == 'INTERRUPTED'
    assert result['sessions'][0]['task'] == 'Repair a CLI failure'
    assert native_sessions.latest(project) == newer
    assert {p: p.read_bytes() for p in catalog.rglob('*') if p.is_file()} == before
    with pytest.raises(ValueError, match='no_session_for_repository'):
        native_sessions.latest(project/'child')


def test_corrupt_session_is_visible_and_cannot_silently_select_older(catalog, tmp_path):
    save(catalog, 'old', tmp_path, 100)
    bad = catalog/'bad'
    bad.mkdir()
    (bad/'native-agent.json').write_text('{')
    assert native_sessions.inventory()['unavailable'][0]['session'] == str(bad)
    with pytest.raises(ValueError, match='catalog_incomplete'):
        native_sessions.latest(tmp_path)


def test_empty_catalog_does_not_create_files(catalog, tmp_path, monkeypatch):
    missing = tmp_path/'missing'
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', missing)
    assert native_sessions.inventory()['sessions'] == []
    assert not missing.exists()


def test_last_uses_project_selection_and_persisted_settings(catalog, tmp_path, monkeypatch):
    chosen = save(catalog, 'chosen', tmp_path, 100)
    save(catalog, 'other', tmp_path/'other', 200)
    seen = {}
    def run(config, deployment, custody, instruction, **kwargs):
        seen.update(custody=custody, **kwargs)
        assert config is None and deployment is None and instruction is None
        assert kwargs['resume'] and kwargs['limits'] == {}
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert native_agent.main(['--last', '--root', str(tmp_path), '--format', 'json',
                              '--follow-up', 'Continue with the next change']) == 0
    assert seen['custody'] == chosen and seen['follow_up'] == 'Continue with the next change'
    assert seen['message_id']


def test_last_uses_cwd_and_interactive_eof_does_not_add_a_turn(catalog, tmp_path, monkeypatch):
    chosen = save(catalog, 'chosen', tmp_path, 100)
    monkeypatch.chdir(tmp_path)
    calls = []
    def run(config, deployment, custody, instruction, **kwargs):
        calls.append(custody)
        return {'status': 'PROPOSED', 'model_calls': 0}
    state = json.loads((chosen/'native-agent.json').read_text())
    state.update(budget={'max_model_calls': None}, deadline_utc=None)
    (chosen/'native-agent.json').write_text(json.dumps(state))
    monkeypatch.setattr(native_agent, 'run_session', run)
    monkeypatch.setattr(native_agent, '_interactive_line', lambda: None)
    assert native_agent.main(['--last', '--interactive']) == 0
    assert calls == [chosen]


@pytest.mark.parametrize('args', [['--last', '--session', 'explicit'], ['--last', '--unlimited']])
def test_invalid_last_options_never_reach_runner(catalog, tmp_path, monkeypatch, args):
    save(catalog, 'chosen', tmp_path, 100)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **k: pytest.fail('unexpected dispatch'))
    assert native_agent.main(args) == 2
