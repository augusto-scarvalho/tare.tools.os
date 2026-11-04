"""OS composition of optional snapshot lookup, using existing offline Runtime fixtures."""
import asyncio
from dataclasses import asdict
import json

import pytest
from textual.widgets import Checkbox

from relay import native_agent
from relay.console_backend import Selection, command
from relay.native_console import WorkConsole
from test_native_agent import Client, deployment, run
from test_native_console import FakeBackend
from test_native_exploration import project


@pytest.mark.parametrize('flags,context,gate', [([], True, True), (['--no-context'], False, False),
                                               (['--specgraph-context'], True, False)])
@pytest.mark.parametrize('role', native_agent.ROLES)
def test_cli_context_and_gate_are_independent(tmp_path, monkeypatch, flags, context, gate, role):
    def capture(config, *args, **kwargs):
        assert ('context' in config['capabilities']) is context
        assert config['specgraph_edit_context'] is gate
        assert ('edit' in config['capabilities']) is (role == 'implementer')
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', capture)
    assert native_agent.main(['--root', str(tmp_path), '--session', str(tmp_path/'session'),
                              '--instruction', 'Inspect', '--role', role, *flags]) == 0


@pytest.mark.parametrize('flags', [ ['--resume', '--session', 'saved', '--specgraph-context'],
    ['--resume', '--session', 'saved', '--no-context'],
    ['--config', 'config.json', '--specgraph-context'], ['--no-context', '--specgraph-context']])
def test_cli_rejects_reconfiguration_before_model(monkeypatch, flags):
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **k: pytest.fail('Reached model'))
    if '--no-context' in flags and '--specgraph-context' in flags:
        with pytest.raises(SystemExit) as exc:
            native_agent.main(flags)
        assert exc.value.code == 2
    else:
        assert native_agent.main(flags) == 2


@pytest.mark.parametrize('enabled', [False, True])
def test_tui_command_and_resume_preserve_saved_choice(tmp_path, enabled):
    selected = Selection(str(tmp_path), prompt='Inspect', specgraph_context=enabled)
    args = command(selected)
    assert ('--specgraph-context' in args) is enabled
    assert ('--no-context' in args) is (not enabled)
    from pathlib import Path
    custody = Path(selected.session); custody.mkdir(parents=True)
    caps = ['read', 'list', 'search'] + (['context'] if enabled else [])
    (custody/'native-agent.json').write_text(json.dumps({'config': {
        'repositories': {'repo': str(tmp_path)}, 'capabilities': caps, 'specgraph_edit_context': False}}))
    resumed = command(selected, True)
    assert '--specgraph-context' not in resumed and '--no-context' not in resumed
    selected.specgraph_context = not enabled
    next_turn = command(selected, True)
    assert '--specgraph-context' not in next_turn and '--no-context' not in next_turn
    # Each explicit follow-up gets its own message identity; compare the saved
    # capability contract rather than requiring identical ephemeral argv IDs.
    assert next_turn[next_turn.index('--message-id') + 1] != resumed[resumed.index('--message-id') + 1]
    assert json.loads((custody/'native-agent.json').read_text())['config']['capabilities'] == caps
    assert json.loads((custody/'native-agent.json').read_text())['config']['capabilities'] == caps


@pytest.mark.parametrize('enabled', [False, True])
def test_tui_widget_and_selection_restore(tmp_path, enabled):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)):
            await app.workers.wait_for_complete()
            widget = app.query_one('#specgraph-context', Checkbox)
            assert widget.value is False
            selected = app.selection()
            selected.specgraph_context = enabled
            app.restore_selection(asdict(selected))
            assert widget.value is enabled
            assert app.selection().specgraph_context is enabled
    asyncio.run(scenario())


def test_old_session_resumes_independently_of_new_conversation_default(tmp_path, monkeypatch):
    """Opening a saved lookup session must not depend on a new-conversation checkbox."""
    from pathlib import Path
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', tmp_path/'sessions')
    custody = tmp_path/'saved'; custody.mkdir()
    (custody/'native-agent.json').write_text(json.dumps({'config': {
        'repositories': {'repo': str(tmp_path)},
        'capabilities': ['read', 'list', 'search', 'context'],
        'specgraph_edit_context': False}}))
    selected = Selection(str(tmp_path), session=str(custody), specgraph_context=False)
    args = command(selected, True)
    assert '--resume' in args and '--no-context' not in args and '--specgraph-context' not in args
    assert 'context' in json.loads((Path(selected.session)/'native-agent.json').read_text())['config']['capabilities']


@pytest.mark.parametrize('role', native_agent.ROLES)
@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_real_context_reaches_next_runtime_request_and_resume(project, tmp_path, monkeypatch, role, protocol):
    dep, state, custody = project
    root = tmp_path/'other'; root.mkdir()
    (root/'a.py').write_bytes(b'value = 1\n')
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', tmp_path/'sessions')
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': role,
              'repositories': {'repo': str(root)}, 'writable': {'repo': ['a.py']},
              'specgraph_edit_context': False}
    config.update(role=role, writable={} if role != 'implementer' else config['writable'], verifiers={},
                  capabilities=['read', 'list', 'search', 'context'] + (['edit'] if role == 'implementer' else []))
    config['repositories']['dependency'] = str(dep)
    config['index_states'] = {'dependency': str(state)}
    client = Client(protocol, [('context', {'repository': 'dependency', 'query': 'caller'}),
                               ('tare_deliver', {'summary': 'Snapshot inspected; source remains to be read.'})])
    result = run(config, custody, client)
    assert result['status'] == 'PROPOSED', result
    requests = [body for path, body in client.calls if path.endswith(('/completions', '/converse'))]
    assert len(requests) == 2
    assert 'src/lib.py:caller' in json.dumps(requests[1])
    assert (root/'a.py').read_bytes() == b'value = 1\n'
    saved = json.loads((custody/'native-agent.json').read_text())
    assert saved['config']['specgraph_edit_context'] is False
    again = Client(protocol, [('tare_deliver', {'summary': 'No new lookup needed.'})])
    resumed = native_agent.run_session(None, None, custody, resume=True, follow_up='Summarize',
        message_id='follow-up', client=again, local_counter=again)
    assert resumed['status'] == 'PROPOSED', resumed
    assert 'src/lib.py:caller' in json.dumps(again.calls)
    changed = {**config, 'capabilities': ['read', 'list', 'search']}
    with pytest.raises(ValueError, match='resume_configuration_changed'):
        native_agent.run_session(changed, None, custody, resume=True, client=again)
