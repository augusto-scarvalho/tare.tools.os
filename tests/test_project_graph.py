"""Graph bindings stay explicit, project-scoped and stable across resume."""
import json
from pathlib import Path

import pytest

from relay import native_agent, relay_mesh
from relay.console_backend import Selection, command
from relay.project_graph import binding, graph_path, graph_view, observe, preference_path, read_defaults, save_defaults
from test_native_agent import Client, deployment, setup
from test_work_console import graph_root


def test_backlog_observation_and_scoped_cas_defaults(graph_root, tmp_path):
    graph = graph_root/'work-graph.json'
    before = graph.read_bytes()
    original = relay_mesh.WORK_GRAPH_FILE
    observation = observe(str(graph))
    assert 'ready title · Ready' in observation['summary']
    assert 'authority' in observation and observation['authority'] == 'NONE'
    assert graph.read_bytes() == before and relay_mesh.WORK_GRAPH_FILE == original
    state, control = tmp_path/'state', tmp_path/'control'
    assert read_defaults(state, graph_root, control) == ('', 'missing')
    selected, revision = save_defaults(state, graph_root, control, str(graph), 'missing')
    assert selected == str(graph)
    with pytest.raises(ValueError, match='Refresh'):
        save_defaults(state, graph_root, control, '', 'missing')
    assert read_defaults(state, tmp_path/'other-project', control) == ('', 'missing')
    assert read_defaults(state, graph_root, tmp_path/'other-control') == ('', 'missing')
    with pytest.raises(ValueError, match='outside'):
        read_defaults(graph_root/'state', graph_root, control)
    assert save_defaults(state, graph_root, control, '', revision)[0] == ''
    assert graph.read_bytes() == before


def test_corruption_invalid_graph_and_missing_binding_are_not_silently_replaced(graph_root, tmp_path):
    state, control = tmp_path/'state', tmp_path/'control'
    graph = graph_root/'work-graph.json'
    save_defaults(state, graph_root, control, str(graph), 'missing')
    path, _, _ = preference_path(state, graph_root, control)
    before = path.read_bytes()
    graph.write_text('{}')
    with pytest.raises(ValueError, match='Invalid Backlog'):
        save_defaults(state, graph_root, control, str(graph), 'missing')
    assert path.read_bytes() == before
    graph.unlink()
    view = graph_view(state, graph_root, control, pending=str(graph))
    assert view['effective_path'] == str(graph) and 'unavailable' in view['summary']
    path.write_text('{broken')
    with pytest.raises(ValueError):
        save_defaults(state, graph_root, control, '', 'missing')
    assert path.read_text() == '{broken'


@pytest.mark.parametrize('value', ['work-graph.json', '../graph.json', '', 7, None])
def test_invalid_binding_rejected(value):
    with pytest.raises(ValueError):
        binding({'repository': 'repo', 'path': value}, {'repo': '/project'})


def test_graph_reader_accepts_owner_graphs_larger_than_model_envelopes(graph_root):
    graph = graph_root/'work-graph.json'
    raw = json.loads(graph.read_text())
    raw['nodes'][0]['notes'] = ['x'*(1024*1024)]
    graph.write_text(json.dumps(raw))
    assert graph.stat().st_size > 1024*1024
    assert 'ready title' in observe(str(graph))['summary']


def test_session_snapshot_and_binding_survive_changed_defaults_graph_and_resume(setup, graph_root, tmp_path):
    config, custody, root = setup
    graph = graph_root/'work-graph.json'
    config['project_graph'] = {'repository': 'repo', 'path': str(graph)}
    config['role'], config['writable'], config['verifiers'] = 'explorer', {}, {}
    client = Client(steps=[('tare_deliver', {'summary': 'Read the initial graph.'})])
    native_agent.run_session(config, deployment(), custody, 'Inspect the backlog', client=client, local_counter=client)
    state = native_agent.read_json(custody/'native-agent.json')
    assert state['config']['project_graph'] == config['project_graph']
    assert 'ready title' in state['project_graph_snapshot']['summary']
    assert 'initial_graph_observation' in json.dumps(client.calls)
    assert 'reconcile that same item before the final answer' in json.dumps(client.calls)
    assert 'Managed Work must use its settlement/recovery owner' in json.dumps(client.calls)
    assert 'Work/flow execution integration is pending.' not in json.dumps(client.calls)
    directory, control = tmp_path/'preferences', tmp_path/'control'
    save_defaults(directory, root, control, '', 'missing')
    raw = json.loads(graph.read_text()); raw['nodes'][0]['title'] = 'Changed live title'
    graph.write_text(json.dumps(raw))
    view = graph_view(directory, root, control, session=custody)
    assert view['path'] == '' and view['effective_path'] == str(graph)
    assert 'Changed live title' in view['summary']
    graph.unlink()  # A disappearing graph cannot retarget an old conversation.
    next_client = Client(steps=[('tare_deliver', {'summary': 'Continue the saved conversation.'})])
    native_agent.run_session(None, None, custody, resume=True, client=next_client, local_counter=next_client)
    resumed = native_agent.read_json(custody/'native-agent.json')
    assert resumed['config'] == state['config']
    assert resumed['project_graph_snapshot'] == state['project_graph_snapshot']
    assert graph_view(directory, root, control, session=custody)['effective_path'] == str(graph)
    with pytest.raises(ValueError, match='another project'):
        graph_view(directory, tmp_path/'wrong', control, session=custody)


def test_backend_cli_passes_binding_only_at_start_and_rejects_resume_override(graph_root, tmp_path, monkeypatch):
    monkeypatch.setattr(native_agent, 'session_root', lambda: tmp_path/'sessions')
    selected = Selection(str(graph_root), prompt='Inspect', graph_path=str(graph_root/'work-graph.json'))
    argv = command(selected)
    assert argv[argv.index('--project-graph')+1] == selected.graph_path
    saved = Path(selected.session); saved.mkdir(parents=True)
    (saved/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'repo': str(graph_root)}}}))
    selected.prompt = ''
    assert '--project-graph' not in command(selected, resume=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid startup options reached the model')
    monkeypatch.setattr(native_agent, 'run_session', forbidden)
    assert native_agent.main(['--resume', '--session', str(saved), '--project-graph', selected.graph_path]) == 2
    assert native_agent.main(['--config', 'unused.json', '--project-graph', selected.graph_path]) == 2


def test_cli_binding_reaches_real_saved_session(setup, graph_root, monkeypatch):
    _, custody, root = setup
    graph = graph_root/'work-graph.json'
    real_run = native_agent.run_session
    client = Client(steps=[('tare_deliver', {'summary': 'Read the backlog.'})])
    def invoke(config, dep, path, prompt, **kwargs):
        return real_run(config, dep, path, prompt, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', invoke)
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **k: deployment())
    assert native_agent.main(['--root', str(root), '--session', str(custody), '--instruction', 'Inspect graph',
        '--no-context', '--role', 'explorer', '--project-graph', str(graph)]) == 0
    state = native_agent.read_json(custody/'native-agent.json')
    assert state['config']['project_graph'] == {'repository': root.name, 'path': str(graph)}
    assert 'ready title' in json.dumps(client.calls)


@pytest.mark.parametrize('mode', ['work', 'sequence'])
def test_project_graph_cannot_silently_dispatch_work_against_os_graph(graph_root, mode):
    selected = Selection(str(graph_root), mode=mode, graph_path=str(graph_root/'work-graph.json'),
                         work='ready', prompt='Implement a change', sequence_works='ready')
    if mode == 'sequence':
        with pytest.raises(ValueError, match='not connected yet'):
            command(selected)
    else:
        args = command(selected)
        assert args[args.index('--project-graph') + 1] == selected.graph_path
