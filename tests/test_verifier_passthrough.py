"""A verifier's nested CLI flags belong to its argv, never the Tare entry parsers."""
from pathlib import Path

import pytest

from relay import agent, native_agent, native_installation, relay_mesh


@pytest.mark.parametrize('entry', ['native', 'agent', 'installed'])
@pytest.mark.parametrize('tail', [
    ['wsl.exe', '-d', 'Ubuntu', '--', 'python3', '-m', 'pytest', '-q'],
    ['python', 'check.py', '--os-root', 'child-root', '--resume', '--session', 'child-session', '--native-deployment', 'child-profile'],
])
def test_nested_verifier_argv_crosses_public_entries_unchanged(tmp_path, monkeypatch, capsys, entry, tail):
    seen, rebound = {}, []
    monkeypatch.setattr(native_agent, 'run_session', lambda config, *a, **k:
                        seen.update(config=config) or {'status': 'PROPOSED', 'output': 'Fixture result'})
    monkeypatch.setattr(native_installation, 'settings', lambda: {})
    monkeypatch.delenv('TARE_OS_ROOT', raising=False)
    monkeypatch.setattr(relay_mesh, 'rebind_root', lambda value: rebound.append(value))
    args = ['--root', str(tmp_path), '--session', str(tmp_path/'session'), '--role', 'implementer',
            '--instruction', 'Use the declared check', '--no-context', '--verify-command', *tail]
    if entry == 'installed':
        code = native_installation.main(['--os-root', str(tmp_path/'control'), 'agent', *args])
        assert rebound == [(tmp_path/'control').resolve()]
    else:
        code = (native_agent if entry == 'native' else agent).main(args)
        assert not rebound
    assert code == 0, capsys.readouterr()
    assert seen['config']['repositories'] == {tmp_path.name: str(tmp_path.resolve())}
    assert seen['config']['verifiers']['check']['argv'] == tail
    assert 'verify' in seen['config']['capabilities']
