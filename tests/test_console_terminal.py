import pytest

from compute_plane.terminal_host import TerminalClient
from relay.console_terminal import ConsoleTerminals, child_environment, native_terminal
from relay.native_agent import configuration


def test_command_environment_uses_user_home_and_git_without_copying_provider_secrets(monkeypatch):
    monkeypatch.setenv('HOME','/user/home')
    monkeypatch.setenv('SSH_AUTH_SOCK','/user/socket')
    monkeypatch.setenv('TARE_TERMINAL_CONNECTION','private descriptor')
    monkeypatch.setenv('EXAMPLE_PROVIDER_KEY','private credential')
    monkeypatch.delenv('GIT_CONFIG_GLOBAL',raising=False)
    env = child_environment()
    assert env['HOME'] == '/user/home' and env['SSH_AUTH_SOCK'] == '/user/socket'
    assert 'GIT_CONFIG_GLOBAL' not in env
    assert not {'TARE_TERMINAL_CONNECTION','EXAMPLE_PROVIDER_KEY'} & env.keys()
    monkeypatch.setenv('GIT_CONFIG_GLOBAL','user-config')
    assert child_environment()['GIT_CONFIG_GLOBAL'] == 'user-config'


def test_native_terminal_requires_exact_custody_and_roots(tmp_path):
    project = tmp_path/'project'; project.mkdir()
    other = tmp_path/'other'; other.mkdir()
    custody = tmp_path/'session'
    terminals = ConsoleTerminals()
    try:
        descriptor = terminals.connection(custody,{'project':str(project.resolve())})
        client = TerminalClient(descriptor)
        config = {'repositories':{'project':str(project.resolve())}}
        port = native_terminal(client,custody=custody,config=config,session_id='s',binding=None)
        assert port.session_id == 's'
        for changed_custody, changed_config in (
                (tmp_path/'other-session',config),
                (custody,{'repositories':{'project':str(other.resolve())}})):
            with pytest.raises(ValueError,match='grant_mismatch'):
                native_terminal(client,custody=changed_custody,config=changed_config,session_id='s',binding=None)
        with pytest.raises(ValueError,match='host_required'):
            native_terminal(None,custody=custody,config=config,session_id='s',binding=None)
    finally:
        terminals.close()
    with pytest.raises(ValueError,match='host_unavailable'):
        native_terminal(client,custody=custody,config=config,session_id='s',binding=None)


@pytest.mark.parametrize('changes',[
    {'terminal':'yes'}, {'role':'explorer'}, {'capabilities':['read']},
    {'capabilities':None}, {'terminal':False},
])
def test_terminal_configuration_rejects_inert_or_unpermitted_selection(tmp_path,changes):
    config = {'schema':'tare.tools/native-agent-config/1','role':'implementer',
              'repositories':{'project':str(tmp_path)}, 'terminal':True,
              'capabilities':['terminal_list']}
    assert configuration(config)['terminal'] is True
    with pytest.raises(ValueError,match='native_agent_'):
        configuration({**config,**changes})
