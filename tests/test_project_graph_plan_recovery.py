"""Project graph identity survives CLI dispatch, crashes and demand navigation."""
import asyncio
from dataclasses import asdict
import json

import pytest

from relay import console_demand, console_mutation, console_recovery, demand_conduction
from relay import native_installation, native_plan, relay_mesh
from relay.console_backend import Selection, command
from relay.native_console import WorkConsole
from test_native_plan import proposal
from test_plan_routing import selection, provider
from test_mutation_recovery import LocalBackend


def test_plan_receipt_recovery_uses_requested_graph_not_current_defaults(proposal, monkeypatch):
    directory, graph, value, _ = proposal
    identity = 'c'*32
    result = native_plan.retry_review(directory, invocation_id=identity)
    selected = asdict(Selection(value['roots']['project'], mode='plan', plan=str(directory), graph_path=str(graph)))
    demand = {'root': selected['root'], 'control_root': str(relay_mesh.RELAY_ROOT),
        'events': [{'kind': 'execution_requested', 'selection': selected, 'detail': {'invocation_id': identity}}]}
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', directory.parent/'another-default.json')
    observed = console_recovery.observe(demand)
    assert observed['state'] == 'RECOVERED' and observed['result'] == result
    selected['graph_path'] = str(directory.parent/'wrong.json')
    assert console_recovery.observe(demand)['state'] == 'UNKNOWN'


def test_cli_plan_in_project_graph_without_control_graph(proposal, selection, provider, monkeypatch, capsys):
    directory, graph, value, _ = proposal
    control = directory.parent/'empty-control'; control.mkdir()
    # Restore all globals after the real installed CLI's scoped subprocess-style rebind.
    original = relay_mesh.RELAY_ROOT
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(directory.parent/'storage'))
    monkeypatch.setattr(native_plan, 'plan_root', lambda: directory.parent/'plans')
    selected = Selection(value['roots']['project'], mode='plan', graph_path=str(graph),
        prompt='Change and test', deployment='local-qwen')
    args = command(selected)
    try:
        assert native_installation.main(['--os-root', str(control), *args]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['proposal']['graph_path'] == str(graph)
        assert relay_mesh.RELAY_ROOT == control
        assert not (control/'work-graph.json').exists()
        assert len(provider[0]) == 2
        applied = native_plan.apply(selected.plan, result['proposal_sha256'])
        assert applied['graph_path'] == str(graph)
    finally:
        relay_mesh.rebind_root(original)


def test_recovered_apply_pins_work_stages_and_blocks_wrong_graph_conduction(proposal, monkeypatch):
    directory, graph, value, _ = proposal
    root = value['roots']['project']
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(directory.parent/'state'))
    choice = Selection(root, mode='plan', plan=str(directory))
    snapshot = {'selection': asdict(choice), 'entry_history': [], 'entry_questions': [],
        'entry_proposal': None, 'entry_selection': None, 'last_result': None}
    digest = native_plan.preview(directory)['proposal_sha256']
    demand = console_demand.save(None, 0, root, relay_mesh.RELAY_ROOT, 'graph_apply_requested', snapshot,
        {'plan': str(directory), 'sha256': digest, 'mutation_id': 'd'*32})
    result = console_mutation.record(demand, lambda: native_plan.apply(directory, digest))
    assert console_demand.result_summary(result)['graph_path'] == str(graph)

    async def scenario():
        app = WorkConsole(root, backend=LocalBackend())
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.restore_selection(asdict(choice))
            app.demand_id, app.demand_revision, app.demand_state = demand['id'], demand['revision'], demand
            await app.reconcile_demand().wait()
            await pilot.pause()
            assert app.selection().mode == 'work'
            assert app.selection().graph_path == str(graph)
            stages = [s for s in app.demand_stages.values() if s['mode'] == 'work']
            assert stages and all(s['graph_path'] == str(graph) for s in stages)
            with pytest.raises(ValueError, match='not connected yet'):
                demand_conduction.next_step(app.demand_state)
            # The same Work IDs in the OS graph must never be used as a substitute.
            args = command(app.selection())
            assert args[args.index('--project-graph')+1] == str(graph)
    asyncio.run(scenario())


def test_invalid_planning_graph_refuses_before_model_calls(proposal, provider):
    directory, graph, value, _ = proposal
    graph.write_text('{}')
    with pytest.raises(ValueError, match='Invalid Backlog'):
        native_plan.generate(value['roots']['project'], graph, directory.parent/'new',
            'Plan the change', 'auto')
    assert provider[0] == []
