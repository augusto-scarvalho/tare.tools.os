"""Work inspection follows the selected graph without granting execution."""
import json

import pytest

from relay import relay_mesh, work_console
from relay.console_backend import Backend, Selection, command
from test_work_console import graph_root


def test_same_work_id_in_two_projects_is_explained_from_selected_graph(graph_root, tmp_path, monkeypatch):
    control = graph_root / 'work-graph.json'
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', control)
    original = control.read_bytes()
    external = tmp_path / 'project-graph.json'
    graph = json.loads(original)
    graph['nodes'][0]['title'] = 'External project task'
    external.write_text(json.dumps(graph), encoding='utf-8')
    before = external.read_bytes()
    choice = Selection(str(tmp_path), mode='work', work='ready', graph_path=str(external))
    explanation = Backend().inspect(choice)
    assert 'External project task' in explanation
    assert str(external) in explanation
    assert 'ready title' not in explanation
    assert 'ready title' in work_console.render('explain', work_console.inspect_work(work_id='ready'))
    args = command(choice)
    assert args[args.index('--project-graph') + 1] == str(external)
    assert control.read_bytes() == original and external.read_bytes() == before


@pytest.mark.parametrize('fault', ['missing', 'invalid', 'relative'])
def test_bad_selected_graph_never_falls_back_to_control(graph_root, tmp_path, monkeypatch, fault):
    control = graph_root / 'work-graph.json'
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', control)
    original = control.read_bytes()
    external = tmp_path / 'selected.json'
    if fault == 'invalid':
        external.write_text('{}', encoding='utf-8')
    selected = external.name if fault == 'relative' else str(external)
    with pytest.raises((ValueError, OSError)):
        Backend().inspect(Selection(str(tmp_path), mode='work', work='ready', graph_path=selected))
    assert control.read_bytes() == original
