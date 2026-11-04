"""Planning preserves explicit project graph ownership across UI and CLI."""
import asyncio
import hashlib
import json
from pathlib import Path
import threading

import pytest
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import add_node_to_graph
from graph_backlog.validation import validate_work_graph
from textual.widgets import Button, Input, Select

from relay import native_plan, relay_mesh
from relay.console_backend import Backend, Selection, command
from relay.native_console import WorkConsole
from test_native_plan import proposal, write


def test_command_new_plan_includes_graph_and_preserves_global_graph(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    custom_graph = (tmp_path / 'custom-work-graph.json').resolve()
    base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    base = add_node_to_graph(WorkGraph(base), 'custom-existing', 'Custom existing task')
    write(custom_graph, base)

    original_global_graph = relay_mesh.WORK_GRAPH_FILE

    selection = Selection(
        root=str(project),
        mode='plan',
        prompt='Plan custom feature decomposition',
        graph_path=str(custom_graph),
    )
    cmd = command(selection)
    assert '--graph' in cmd
    graph_idx = cmd.index('--graph')
    assert cmd[graph_idx + 1] == str(custom_graph)
    assert relay_mesh.WORK_GRAPH_FILE == original_global_graph

    selection_default = Selection(
        root=str(project),
        mode='plan',
        prompt='Plan default feature decomposition',
    )
    cmd_default = command(selection_default)
    assert cmd_default[cmd_default.index('--graph') + 1] == str(Path(original_global_graph).resolve())
    assert relay_mesh.WORK_GRAPH_FILE == original_global_graph


def test_command_retry_review_and_replan_never_append_graph_from_changed_defaults(proposal, tmp_path):
    directory, graph, value, base = proposal
    project = directory.parent / 'project'
    changed_graph = (tmp_path / 'changed-defaults-graph.json').resolve()

    selection_retry = Selection(
        root=str(project),
        mode='plan',
        plan=str(directory),
        graph_path=str(changed_graph),
    )
    args_retry = command(selection_retry, resume=True)
    assert selection_retry.graph_path == str(graph)
    assert '--retry-review' in args_retry
    assert '--graph' not in args_retry
    assert '--plan' in args_retry
    assert args_retry[args_retry.index('--plan') + 1] == str(directory)

    selection_replan = Selection(
        root=str(project),
        mode='plan',
        plan=str(directory),
        prompt='Revise tasks to simplify decomposition',
        graph_path=str(changed_graph),
    )
    args_replan = command(selection_replan, replan=True)
    assert selection_replan.graph_path == str(graph)
    assert '--replan' in args_replan
    assert args_replan[args_replan.index('--replan') + 1] == str(directory)
    assert '--graph' not in args_replan


def test_native_plan_main_refuses_graph_override_for_preview_and_apply(proposal, tmp_path):
    directory, graph, value, base = proposal
    view = native_plan.preview(directory)
    digest = view['proposal_sha256']
    unrelated_graph = (tmp_path / 'unrelated-graph.json').resolve()

    graph_before = graph.read_bytes()

    rc_preview = native_plan.main(['--plan', str(directory), '--preview', '--graph', str(unrelated_graph)])
    assert rc_preview == 2
    assert graph.read_bytes() == graph_before
    assert not unrelated_graph.exists()

    rc_apply = native_plan.main(['--plan', str(directory), '--apply', digest, '--graph', str(unrelated_graph)])
    assert rc_apply == 2
    assert graph.read_bytes() == graph_before
    assert not unrelated_graph.exists()

    rc_retry = native_plan.main(['--plan', str(directory), '--retry-review', '--graph', str(unrelated_graph)])
    assert rc_retry == 2
    rc_replan = native_plan.main(['--replan', str(directory), '--instruction', 'Fix', '--graph', str(unrelated_graph)])
    assert rc_replan == 2
    assert graph.read_bytes() == graph_before


def test_native_plan_apply_returns_saved_graph_path_and_isolates_global_graph(proposal, tmp_path, monkeypatch):
    directory, graph, value, base = proposal

    global_graph = (tmp_path / 'global-work-graph.json').resolve()
    global_base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    global_base = add_node_to_graph(WorkGraph(global_base), 'existing', 'Overlapping existing node')
    global_base = add_node_to_graph(WorkGraph(global_base), 'plan-fixture-change', 'Overlapping planned node')
    global_base['revision'] = compute_revision_hash(global_base)
    write(global_graph, global_base)
    global_before = global_graph.read_bytes()
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', global_graph)

    view = native_plan.preview(directory)
    expected_digest = view['proposal_sha256']

    result = native_plan.apply(directory, expected_digest)
    assert result['status'] == 'APPLIED'
    assert result['graph_path'] == str(graph.resolve())
    assert result['graph_path'] == value['graph_path']

    assert global_graph.read_bytes() == global_before

    applied_graph = json.loads(graph.read_bytes())
    assert any(node['id'] == 'plan-fixture-change' for node in applied_graph['nodes'])
    assert validate_work_graph(applied_graph)['status'] == 'PASS'

    re_result = native_plan.apply(directory, expected_digest)
    assert re_result['status'] == 'ALREADY_APPLIED'
    assert re_result['graph_path'] == str(graph.resolve())
    assert global_graph.read_bytes() == global_before


def test_work_console_review_apply_sets_graph_path_before_selecting_work(proposal, tmp_path):
    directory, graph, value, base = proposal
    project = directory.parent / 'project'

    divergent_graph = (tmp_path / 'divergent-graph.json').resolve()
    divergent_base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    divergent_base = add_node_to_graph(WorkGraph(divergent_base), 'divergent-node', 'Divergent node')
    write(divergent_graph, divergent_base)
    divergent_before = divergent_graph.read_bytes()

    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}],
                    'sessions': [], 'works': [], 'errors': []}

    async def scenario():
        app = WorkConsole(project, backend=LocalBackend())
        async with app.run_test(size=(140, 46)) as pilot:
            await pilot.pause()

            app.project_graph_path = str(divergent_graph)

            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            await pilot.pause()
            assert app.query_one('#apply-plan', Button).disabled

            await app.review_selection().wait()
            assert not app.query_one('#apply-plan', Button).disabled

            app.apply_reviewed_plan()
            while app.busy:
                await pilot.pause(0.05)

            assert app.last_result['status'] == 'APPLIED'
            assert app.last_result['graph_path'] == str(graph.resolve())

            assert app.project_graph_path == str(graph.resolve())
            assert app.selection().graph_path == str(graph.resolve())

            assert app.query_one('#mode', Select).value == 'work'
            assert app.query_one('#work-id', Input).value == 'plan-fixture-change'

            assert divergent_graph.read_bytes() == divergent_before

    asyncio.run(scenario())


def test_external_work_dispatch_and_backend_methods_refuse_external_graph(tmp_path, monkeypatch):
    control_graph = (tmp_path / 'control-work-graph.json').resolve()
    control_base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    control_base = add_node_to_graph(WorkGraph(control_base), 'control-work-1', 'Control work')
    control_base['revision'] = compute_revision_hash(control_base)
    write(control_graph, control_base)

    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', str(control_graph))
    control_before = control_graph.read_bytes()

    external_graph = (tmp_path / 'external-work-graph.json').resolve()
    external_base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    external_base = add_node_to_graph(WorkGraph(external_base), 'ext-work-1', 'External work')
    external_base['revision'] = compute_revision_hash(external_base)
    write(external_graph, external_base)

    project = tmp_path / 'project'
    project.mkdir()

    selection = Selection(
        root=str(project),
        mode='work',
        work='ext-work-1',
        graph_path=str(external_graph),
        source_ref='REF-1',
        criterion_ids='AC-1',
        work_verifier='["python", "-m", "pytest"]',
        intake_criteria='AC-1: Test',
        intake_argv='pytest',
        revision_reason='Fix bug',
    )

    args = command(selection)
    assert args[args.index('--project-graph') + 1] == str(external_graph)
    backend = Backend()
    assert 'ext-work-1' in backend.progress(selection)
    assert 'External work' in backend.inspect(selection)
    assert 'No train' in backend.review(selection) or 'ext-work-1' in backend.review(selection)
    assert backend.decision(selection) is None
    # Ordinary preparation preconditions still apply to an unprepared external node.
    with pytest.raises(ValueError, match='unprepared PLAN proposal'):
        backend.prepare_work(selection)
    with pytest.raises(ValueError, match='unstarted PLAN proposal'):
        backend.work_intake_draft(selection)
    with pytest.raises(ValueError, match='unstarted PLAN proposal'):
        backend.preview_work_intake(selection)

    assert control_graph.read_bytes() == control_before


def test_native_plan_reads_base_snapshots_above_one_mib(proposal):
    directory, graph, value, base = proposal
    project = directory.parent / 'project'

    large_payload = 'x' * (1024 * 1024 + 1024)
    base['meta']['large_context'] = large_payload
    new_revision = compute_revision_hash(base)
    base['revision'] = new_revision

    new_patch = native_plan.compile_patch(base, value['plan'], 'plan-fixture', 'project', value['observed_at'])
    value['base_revision'] = new_revision
    value['patch'] = new_patch

    write(directory / 'base-graph.json', base)
    write(graph, base)
    write(directory / 'input' / 'proposal.json', value)

    assert (directory / 'base-graph.json').stat().st_size > 1024 * 1024
    assert graph.stat().st_size > 1024 * 1024

    prop_sha = hashlib.sha256((directory / 'input' / 'proposal.json').read_bytes()).hexdigest()
    req_sha = hashlib.sha256((directory / 'input' / 'request.json').read_bytes()).hexdigest()

    artifact = json.loads((directory / 'review-artifact.json').read_bytes())
    artifact['payload']['sources'] = [
        {'repository': 'proposal', 'path': 'proposal.json', 'sha256': prop_sha},
        {'repository': 'proposal', 'path': 'request.json', 'sha256': req_sha},
    ]
    write(directory / 'review-artifact.json', artifact)
    write(directory / 'review.json', {
        'proposal_sha256': sha256_canonical(value),
        'review': artifact['payload'],
    })

    view = native_plan.preview(directory)
    assert view['status'] == 'REVIEWED'

    original_source = (project / 'a.py').read_bytes()
    (project / 'a.py').write_text('value = 999\n')
    before = graph.read_bytes()
    with pytest.raises(ValueError):
        native_plan.apply(directory, view['proposal_sha256'])
    assert graph.read_bytes() == before
    (project / 'a.py').write_bytes(original_source)

    result = native_plan.apply(directory, view['proposal_sha256'])
    assert result['status'] == 'APPLIED'
    assert result['graph_path'] == str(graph.resolve())

    updated = json.loads(graph.read_bytes())
    assert validate_work_graph(updated)['status'] == 'PASS'
    assert any(n['id'] == 'plan-fixture-change' for n in updated['nodes'])


def test_async_plan_review_selection_change_does_not_arm_apply(proposal, tmp_path):
    directory, graph, value, base = proposal
    project = directory.parent / 'project'
    other_plan = tmp_path / 'other-plan'
    other_plan.mkdir()

    class DelayedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.preview_started = threading.Event()
            self.preview_release = threading.Event()

        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}],
                    'sessions': [], 'works': [], 'errors': []}

        def plan_preview(self, selection):
            self.preview_started.set()
            self.preview_release.wait(timeout=10.0)
            return super().plan_preview(selection)

    async def scenario():
        backend = DelayedBackend()
        app = WorkConsole(project, backend=backend)
        async with app.run_test(size=(140, 46)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            await pilot.pause()
            assert app.query_one('#apply-plan', Button).disabled

            worker = app.review_selection()

            try:
                await asyncio.to_thread(backend.preview_started.wait, 5.0)
                assert backend.preview_started.is_set()

                app.query_one('#plan', Input).value = str(other_plan)
                await pilot.pause()
            finally:
                backend.preview_release.set()

            await worker.wait()
            await pilot.pause()

            assert app.query_one('#apply-plan', Button).disabled
            assert app.reviewed_plan is None

    asyncio.run(scenario())


def test_async_plan_review_project_change_does_not_arm_apply(proposal, tmp_path):
    directory, graph, value, base = proposal
    project_one = directory.parent / 'project'
    project_two = tmp_path / 'project-two'
    project_two.mkdir()

    class DelayedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.preview_started = threading.Event()
            self.preview_release = threading.Event()

        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}],
                    'sessions': [], 'works': [], 'errors': []}

        def plan_preview(self, selection):
            self.preview_started.set()
            self.preview_release.wait(timeout=10.0)
            return super().plan_preview(selection)

    async def scenario():
        backend = DelayedBackend()
        app = WorkConsole(project_one, backend=backend)
        async with app.run_test(size=(140, 46)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            await pilot.pause()
            assert app.query_one('#apply-plan', Button).disabled

            worker = app.review_selection()

            try:
                await asyncio.to_thread(backend.preview_started.wait, 5.0)
                assert backend.preview_started.is_set()

                app.open_project(str(project_two), app.project_navigation_scope())
                await pilot.pause()
            finally:
                backend.preview_release.set()

            await worker.wait()
            await pilot.pause()

            assert app.query_one('#apply-plan', Button).disabled
            assert app.reviewed_plan is None

    asyncio.run(scenario())
