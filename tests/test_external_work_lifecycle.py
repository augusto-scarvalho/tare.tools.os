"""External Work uses real Git/settlement, with scripted inference and admission inputs."""
import json
from pathlib import Path

import pytest

from relay import relay_mesh, work_run
from test_work_resume import host
from test_work_run import os_root


@pytest.mark.parametrize('interruption', ['before_graph', 'target_advanced', 'after_landing'])
def test_external_work_lands_and_recovers_without_touching_control(host, monkeypatch, capsys, interruption):
    import tare_ops
    root, repo, profile, client, plans = host
    control_before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    graph = repo / 'work-graph.json'
    graph.write_bytes(control_before)
    relay_mesh.init_train('CONTROL-OWNER', 'Other project', ['work-one'], 'planner')
    real_write = relay_mesh.relay_io.write_text_atomic
    def interrupt(path, *args, **kwargs):
        if Path(path) == graph:
            raise SystemExit('external graph write interrupted')
        return real_write(path, *args, **kwargs)
    args = ['run', 'work-one', '--project-graph', str(graph), '--repo-root', str(repo),
            '--native-deployment', str(profile), '--unlimited']
    if interruption in {'before_graph', 'target_advanced'}:
        with monkeypatch.context() as fault:
            fault.setattr(relay_mesh.relay_io, 'write_text_atomic', interrupt)
            with pytest.raises(SystemExit):
                tare_ops.main(args)
    else:
        assert tare_ops.main(args) == 0
    capsys.readouterr()
    count = len(client.generations)
    train = work_run.train_dir_for('work-one', graph_path=graph)
    descriptor = json.loads((train / 'EXECUTION_DESCRIPTOR.json').read_bytes())
    if interruption == 'target_advanced':
        from test_work_run import _git
        _git('commit', '--allow-empty', '-qm', 'Following commit', cwd=repo)
        advanced = _git('rev-parse', 'HEAD', cwd=repo)
    assert Path(descriptor['work_graph']) == graph
    assert 'Strict Work' in (train / 'PACKET.md').read_text()
    assert tare_ops.main(['run', 'work-one', '--project-graph', str(graph), '--repo-root', str(repo), '--resume']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'SETTLED'
    assert len(client.generations) == count
    assert json.loads(graph.read_bytes())['nodes'][0]['completion']['status'] == 'DONE'
    assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == control_before
    assert work_run.status('work-one')['last_run'] is None
    assert work_run.status('work-one', graph_path=graph)['train']['status'] == 'LANDED'
    assert work_run.coordinator_path(graph) != work_run.coordinator_path()
    if interruption == 'target_advanced':
        assert _git('rev-parse', 'HEAD', cwd=repo) == advanced
        assert result['recovery']['git_already_landed'] is True
