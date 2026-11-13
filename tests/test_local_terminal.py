"""A local model never gets the unsandboxed terminal (owner, 2026-11-13); a hosted model keeps it unchanged."""
import pytest

from compute_plane.native_terminal import TERMINAL_SPECS
from relay import native_agent
from relay.project_permissions import permissions_view
from test_native_agent import Client, deployment, setup  # noqa: F401 (setup is a fixture)


class Terminal:  # the hosted grant without a host process: only the tool list is under test
    execution_environment = {'platform': 'fixture'}

    def observations(self):
        return []


@pytest.mark.parametrize('local', [False, True])
def test_terminal_tools_go_to_a_hosted_model_only(setup, tmp_path, monkeypatch, local):
    monkeypatch.setattr('relay.challenger.local', lambda _: local)
    monkeypatch.setattr('relay.console_terminal.native_terminal', lambda *a, **k: Terminal())
    config, custody, root = setup
    config.update(verifiers={}, terminal=True, capabilities=['read', 'list', 'search', 'edit', *TERMINAL_SPECS])
    client = Client(steps=[('tare_deliver', {'summary': 'Inspected.'})])
    result = native_agent.run_session(config, deployment(), custody, 'Inspect a.py.', client=client,
                                      local_counter=client)
    body = [body for path, body in client.calls if path == '/chat/completions'][-1]
    tools = {tool['function']['name'] for tool in body['tools']}
    told = native_agent.LOCAL_TERMINAL in body['messages'][0]['content']
    effective = permissions_view(tmp_path/'preferences', root, tmp_path/'control', session=custody)['effective_text']
    if local:
        assert not tools & set(TERMINAL_SPECS) and {'read', 'edit'} <= tools and told
        assert '[tare] '+native_agent.LOCAL_TERMINAL in result['output']
        assert 'System commands: None for a local model' in effective and 'terminal_start' not in effective
    else:  # unchanged: every terminal tool, no notice
        assert set(TERMINAL_SPECS) <= tools and not told and native_agent.LOCAL_TERMINAL not in result['output']
        assert 'System commands: Terminal on this machine, not sandboxed, not reviewed' in effective
        # A session saved with the terminal never continues on a local model, nor reaches its open processes.
        monkeypatch.setattr('relay.challenger.local', lambda _: True)
        later = Client(steps=[])
        with pytest.raises(ValueError, match='saved with the terminal and runs on a local model'):
            native_agent.run_session(None, None, custody, resume=True, follow_up='Type into it.',
                                     message_id='m2', client=later)
        assert later.generations == 0
