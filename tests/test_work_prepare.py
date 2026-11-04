import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from graph_backlog.grounding import ground_work_item, encode_work_grounding
from graph_backlog.jsonutil import compute_revision_hash
from relay import work_prepare, work_run
from relay.source_admission import evaluate_source_admission
from relay.train_execution import parse_execution_descriptor, canonical_json_bytes
from test_source_admission import admission, git


@pytest.fixture
def proposal(admission):
    graph_path = admission['work_graph']
    graph = json.loads(graph_path.read_bytes())
    node = graph['nodes'][0]
    node.update(admission_state=work_prepare.PROPOSED, exit_criteria=['Model-proposed wording'],
                source_refs=[], projection_run_id='plan-test')
    node.pop('spec_ref')
    node['repository_scopes'][0].update(repository='checkout-alias', grounding_refs=[])
    node['metrics'] = {}
    graph['sources'] = {}
    graph['revision'] = compute_revision_hash(graph)
    graph_path.write_text(json.dumps(graph))
    return admission


def preview(proposal):
    return work_prepare.prepare(proposal['work_graph'], 'WORK-1', proposal['owner_roots']['owner'],
        'owner:docs/spec.md', ['AC-01'], [sys.executable, '-m', 'pytest', '-q'])


def test_preparation_uses_real_pinned_source_and_passes_existing_admission_prerequisite(proposal):
    path = proposal['work_graph']; before = path.read_bytes()
    with pytest.raises(ValueError, match='WORK_REQUIRES_PREPARATION'):
        work_run.load_node(path, 'WORK-1')
    view = preview(proposal)
    assert path.read_bytes() == before
    assert view['preparation']['before']['exit_criteria'] == ['Model-proposed wording']
    assert view['preparation']['after']['exit_criteria'] == ['AC-01: preserve exact intent']
    result = work_prepare.apply(view)
    assert result['status'] == 'PREPARED'
    node = work_run.load_node(path, 'WORK-1')
    descriptor = parse_execution_descriptor(canonical_json_bytes(work_run.derive_descriptor(
        node, train_id='RUN-WORK-1', repository_root=proposal['owner_roots']['owner'])))
    ground = ground_work_item(path, 'WORK-1', max_bytes=65536)
    assert ground.status == 'READY'
    observed = evaluate_source_admission(**(proposal | {'descriptor': descriptor}),
                                        work_grounding=encode_work_grounding(ground))
    assert observed['status'] == 'MATCHED', observed
    assert work_prepare.apply(view)['status'] == 'ALREADY_PREPARED'
    assert 'library_manifest' not in node['metrics']
    record = json.loads(path.read_bytes())['sources'][node['source_refs'][0]]
    assert record['locator'] == 'owner:docs/spec.md'  # where it lives, not a commit
    assert node['completion']['status'] == 'NOT_DONE'


@pytest.mark.parametrize('fault', ['graph', 'head', 'preview'])
def test_changed_inputs_cannot_apply_old_preparation(proposal, fault):
    view = preview(proposal)
    if fault == 'graph':
        graph = json.loads(proposal['work_graph'].read_bytes()); graph['nodes'][0]['title'] = 'Concurrent update'
        graph['revision'] = compute_revision_hash(graph); proposal['work_graph'].write_text(json.dumps(graph))
    elif fault == 'head':
        git(proposal['owner_roots']['owner'], 'commit', '--allow-empty', '-qm', 'new head')
    else:
        view['preparation']['argv'] = ['wrong']
    before = proposal['work_graph'].read_bytes()
    with pytest.raises(ValueError): work_prepare.apply(view)
    assert proposal['work_graph'].read_bytes() == before


@pytest.mark.parametrize('fault', ['unknown-ac', 'shell', 'missing-source', 'unqualified-source'])
def test_invalid_bindings_are_refused_before_mutation(proposal, fault):
    args = dict(graph_path=proposal['work_graph'], work_id='WORK-1', root=proposal['owner_roots']['owner'],
                source_ref='owner:docs/spec.md', criteria=['AC-01'], argv=[sys.executable, '-m', 'pytest'])
    if fault == 'unknown-ac': args['criteria'] = ['AC-99']
    if fault == 'shell': args['argv'] = ['cmd.exe', '/c', 'echo pass']
    if fault == 'missing-source': args['source_ref'] = 'owner:docs/absent.md'
    if fault == 'unqualified-source': args['source_ref'] = 'docs/spec.md'
    before = proposal['work_graph'].read_bytes()
    with pytest.raises(ValueError): work_prepare.prepare(**args)
    assert proposal['work_graph'].read_bytes() == before


def test_prepared_dependency_does_not_claim_execution_ready(proposal):
    path = proposal['work_graph']; graph = json.loads(path.read_bytes())
    dep = copy.deepcopy(graph['nodes'][0]); dep['id'] = 'PREREQUISITE'; dep['canonical_id'] = dep['id']
    graph['nodes'].append(dep)
    graph['edges'].append({'from': dep['id'], 'to': 'WORK-1', 'type': 'UNLOCKS', 'semantic': True,
        'source_refs': [], 'source_details': [], 'confidence': {'score': 1}, 'notes': [], 'requirements': {}})
    graph['revision'] = compute_revision_hash(graph); path.write_text(json.dumps(graph))
    result = work_prepare.apply(preview(proposal))
    assert not result['readiness']['ready']
    assert ground_work_item(path, 'WORK-1', max_bytes=65536).status == 'BLOCKED'


def test_tui_previews_exact_criteria_and_prevents_confirmation_after_selection_changes(proposal, monkeypatch):
    from relay import relay_mesh
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend
    from textual.widgets import Input, Select, Button
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', proposal['work_graph'])
    root = proposal['owner_roots']['owner']
    (root/'docs/notes.md').write_text('# Notes without acceptance criteria\n')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'notes')
    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}], 'sessions': [], 'works': [], 'errors': []}
    async def scenario():
        app = WorkConsole(proposal['owner_roots']['owner'], backend=LocalBackend())
        async with app.run_test(size=(140,48)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-1'
            await app.load_sources().wait()
            # Only admissible specs of the selected checkout, named as the Work names its repository.
            assert work_prepare.source_options(proposal['work_graph'], 'WORK-1', root) == ['checkout-alias:docs/spec.md']
            app.query_one('#source-ref', Select).value = 'checkout-alias:docs/spec.md'
            app.query_one('#criterion-ids', Input).value = 'AC-01'
            app.query_one('#work-verifier', Input).value = json.dumps([sys.executable, '-m', 'pytest', '-q'])
            before = proposal['work_graph'].read_bytes()
            await app.preview_preparation().wait()
            assert not app.query_one('#confirm-preparation', Button).disabled
            assert proposal['work_graph'].read_bytes() == before
            app.query_one('#criterion-ids', Input).value = 'AC-99'
            app.confirm_preparation()
            assert app.reviewed_preparation is None and proposal['work_graph'].read_bytes() == before
            app.query_one('#criterion-ids', Input).value = 'AC-01'
            await app.preview_preparation().wait()
            app.confirm_preparation()
            while app.busy: await pilot.pause(0.05)
            assert app.last_result['status'] == 'PREPARED'
    asyncio.run(scenario())
