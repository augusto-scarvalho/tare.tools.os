"""Operator model discovery requires neither a working repository nor a provider."""
import socket
import pytest

from relay import environment, native_agent, native_catalog, native_generation


@pytest.mark.parametrize('contributor', [False, True])
def test_listing_needs_no_session_credentials_network_or_repository(monkeypatch, tmp_path, capsys, contributor):
    def forbidden(*args, **kwargs):
        raise AssertionError('Listing must stay offline and must not create a session')

    monkeypatch.setattr(native_agent, 'run_session', forbidden)
    monkeypatch.setattr(native_agent, 'vault_secret', forbidden)
    monkeypatch.setattr(native_generation, 'vault_secret', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(environment, 'read', lambda: {'model_data_use': {'meta_contributor': contributor}})
    repository, session = tmp_path/'missing-repo', tmp_path/'missing-session'
    assert native_agent.main(['--list-models', '--format', 'text', '--root', str(repository),
                              '--session', str(session)]) == 0
    output = capsys.readouterr().out
    guarded = {name for name in native_catalog.LOCAL if name.endswith('-contributor')}  # OpenRouter and OpenCode
    assert 'openrouter-muse-spark-1-3-contributor' in guarded
    assert all(name in output for name in native_catalog.LOCAL if name not in guarded)
    assert all((name in output) is contributor for name in guarded)
    assert 'availability not checked' in output
    assert not repository.exists() and not session.exists()


def test_new_catalog_choice_is_listed_without_leaking_profile_fields(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path/'profiles'))
    monkeypatch.setattr(native_catalog, 'LOCAL', {'operator-added': 'custom'})
    selected = []

    def select(name):
        selected.append(name)
        return {'native_credential_env': 'PRIVATE_TOKEN_NAME', 'api_key': 'PRIVATE_VALUE',
                'native_profile': {'model': 'custom-model', 'protocol': 'openai-chat',
                                   'endpoint': 'https://private.example'}}

    monkeypatch.setattr(native_catalog, 'select', select)
    assert native_agent.main(['--list-models', '--format', 'text']) == 0
    assert selected == ['operator-added']
    output = capsys.readouterr().out
    assert 'operator-added | custom-model | openai-chat' in output
    assert 'PRIVATE' not in output and 'private.example' not in output
