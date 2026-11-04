import asyncio
import json
import sys

import pytest
from graph_backlog.grounding import ground_work_item, encode_work_grounding
from graph_backlog.jsonutil import compute_revision_hash

from relay import sources, work_intake, work_prepare, work_run
from relay.source_admission import evaluate_source_admission
from relay.train_execution import parse_execution_descriptor, canonical_json_bytes
from test_source_admission import admission, git
from test_work_prepare import proposal


@pytest.fixture(autouse=True)
def configured_owner(proposal):
    root = proposal['owner_roots']['owner']
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.invalid')


def preview(proposal):
    return work_intake.preview(proposal['work_graph'], 'WORK-1', proposal['owner_roots']['owner'],
        ['Preserve café and whitespace as specified.'], [sys.executable, '-m', 'pytest', '-q'])


def test_intake_normalizes_plan_file_order(proposal):
    path = proposal['work_graph']
    value = json.loads(path.read_bytes())
    node = next(n for n in value['nodes'] if n['id'] == 'WORK-1')
    node['repository_scopes'][0]['target_paths'] = ['z.py', 'a.py']
    value['revision'] = compute_revision_hash(value)
    path.write_text(json.dumps(value), encoding='utf-8')
    result = work_intake.apply(preview(proposal))
    assert result['status'] == 'PREPARED'
    assert work_run.load_node(path, 'WORK-1')['repository_scopes'][0]['target_paths'] == ['a.py', 'z.py']


def run_sources(proposal, node, root):
    """The sources file a run writes; the intent is absent at HEAD, so its retained commit is read."""
    graph = json.loads(proposal['work_graph'].read_bytes())
    return sources.write(proposal['work_graph'].parent/'SOURCES.json', node, graph['sources'],
                         {'checkout-alias': (root, 'HEAD')})


def test_new_source_passes_owner_admission_without_existing_manifest_or_touching_index(proposal):
    root = proposal['owner_roots']['owner']; path = proposal['work_graph']
    proposal['library_manifest'].unlink()
    (root/'staged.txt').write_text('operator staged change')
    git(root, 'add', 'staged.txt')
    (root/'dirty.txt').write_text('operator untracked change')
    head = git(root, 'rev-parse', 'HEAD'); index = git(root, 'write-tree')
    status = git(root, 'status', '--porcelain'); before = path.read_bytes()
    view = preview(proposal)
    assert path.read_bytes() == before and not (path.parent/'.tare').exists()
    result = work_intake.apply(view)
    assert result['status'] == 'PREPARED' and result['generation_calls'] == 0
    assert git(root, 'rev-parse', 'HEAD') == head and git(root, 'write-tree') == index
    assert git(root, 'status', '--porcelain') == status
    assert git(root, 'rev-parse', result['source_ref']) == result['source_commit']
    node = work_run.load_node(path, 'WORK-1')
    assert not (path.parent/'.tare/work-intake'/result['intake_sha256']/'manifest.json').exists()
    manifest = run_sources(proposal, node, root)
    descriptor = parse_execution_descriptor(canonical_json_bytes(work_run.derive_descriptor(
        node, train_id='RUN-WORK-1', repository_root=root, library_manifest=str(manifest))))
    observed = evaluate_source_admission(work_graph=path,
        work_grounding=encode_work_grounding(ground_work_item(path, 'WORK-1', max_bytes=65536)),
        descriptor=descriptor, library_manifest=manifest, owner_roots={'checkout-alias': root})
    assert observed['status'] == 'MATCHED', observed
    assert work_intake.apply(view)['status'] == 'ALREADY_PREPARED'
    assert node['completion']['status'] == 'NOT_DONE'


@pytest.mark.parametrize('fault', ['graph', 'head', 'view', 'branch'])
def test_changed_preview_refuses_before_creating_source(proposal, fault):
    view = preview(proposal); root = proposal['owner_roots']['owner']; path = proposal['work_graph']
    if fault == 'graph':
        graph = json.loads(path.read_bytes()); graph['nodes'][0]['summary'] = 'Changed objective'
        graph['revision'] = compute_revision_hash(graph); path.write_text(json.dumps(graph))
    elif fault == 'head': git(root, 'commit', '--allow-empty', '-qm', 'changed')
    elif fault == 'branch': git(root, 'switch', '-qc', 'another')
    else: view['intake']['argv'] = ['changed']
    before = path.read_bytes()
    with pytest.raises(ValueError): work_intake.apply(view)
    assert path.read_bytes() == before
    assert not git(root, 'for-each-ref', 'refs/tare/work-intents')


@pytest.mark.parametrize('criteria,argv', [([], ['python']), (['x', 'x'], ['python']),
    (['x'], []), (['x'], ['cmd.exe', '/c', 'echo pass']), (['x'], ['python', ''])])
def test_invalid_criteria_or_verification_never_materializes(proposal, criteria, argv):
    with pytest.raises(ValueError):
        work_intake.preview(proposal['work_graph'], 'WORK-1', proposal['owner_roots']['owner'], criteria, argv)
    assert not (proposal['work_graph'].parent/'.tare').exists()


@pytest.mark.parametrize('after_apply', [False, True])
def test_resume_after_interruption_reuses_source_and_preparation(proposal, monkeypatch, after_apply):
    view = preview(proposal); original = work_prepare.apply
    def interrupted(value):
        if after_apply: original(value)
        raise OSError('interrupted')
    monkeypatch.setattr(work_prepare, 'apply', interrupted)
    with pytest.raises(OSError): work_intake.apply(view)
    root = proposal['owner_roots']['owner']; refs = git(root, 'for-each-ref', 'refs/tare/work-intents')
    monkeypatch.setattr(work_prepare, 'apply', original)
    result = work_intake.apply(view)
    assert result['status'] == ('ALREADY_PREPARED' if after_apply else 'PREPARED')
    assert git(root, 'for-each-ref', 'refs/tare/work-intents') == refs


def test_graph_change_during_materialization_cannot_admit_new_scope(proposal, monkeypatch):
    original = work_intake._pin; path = proposal['work_graph']; view = preview(proposal)
    def changed(*args):
        result = original(*args)
        graph = json.loads(path.read_bytes()); graph['nodes'][0]['repository_scopes'][0]['target_paths'] = ['another.py']
        graph['revision'] = compute_revision_hash(graph); path.write_text(json.dumps(graph))
        return result
    monkeypatch.setattr(work_intake, '_pin', changed)
    with pytest.raises(ValueError, match='changed'): work_intake.apply(view)
    assert json.loads(path.read_bytes())['nodes'][0]['admission_state'] == work_prepare.PROPOSED


def test_plan_review_can_read_pinned_intent_absent_from_checkout(proposal):
    from relay.native_work_host import plan_context
    root = proposal['owner_roots']['owner']; control = proposal['work_graph'].parent
    result = work_intake.apply(preview(proposal))
    assert not (root/result['source_path']).exists()
    node = work_run.load_node(proposal['work_graph'], 'WORK-1')
    train = control/'train'; train.mkdir()
    descriptor = work_run.derive_descriptor(node, train_id='RUN-WORK-1', repository_root=root,
        library_manifest=str(run_sources(proposal, node, root)))
    (train/'EXECUTION_DESCRIPTOR.json').write_bytes(canonical_json_bytes(descriptor))
    name = plan_context(train, control, control/'attempt', repository_root=root)
    payload = json.loads((train/name).read_bytes())
    acquired = payload['pinned_source']
    assert acquired['status'] == 'READY'
    assert acquired['documents'][0]['revision'] == result['source_commit']
    assert acquired['documents'][0]['body'] == preview_body(result, control)


def test_intake_work_revises_against_its_retained_intent_absent_at_head(proposal, monkeypatch):
    from relay import relay_mesh, work_change
    root = proposal['owner_roots']['owner']; path = proposal['work_graph']
    result = work_intake.apply(preview(proposal))
    ref = 'checkout-alias:'+result['source_path']
    assert not (root/result['source_path']).exists()
    assert ref in work_prepare.source_options(path, 'WORK-1', root)  # the console picker offers the kept intent
    assert work_prepare.source_details(path, 'WORK-1', root, ref)['revision'] == result['source_commit']
    original = relay_mesh.RELAY_ROOT
    relay_mesh.rebind_root(path.parent)
    monkeypatch.setattr(work_change, 'change_root', lambda: path.parent/'work-changes')
    try:
        view = work_change.preview(path, 'WORK-1', root, ref, ['AC-01'], [sys.executable, '-m', 'pytest', '-x'],
                                   'Verify with a stricter command')
        assert work_change.apply(view)['status'] == 'REVISED'
    finally:
        relay_mesh.rebind_root(original)
    node = work_run.load_node(path, 'WORK-1')
    assert node['spec_ref'] == ref and node['metrics']['verification_checks'][0]['command'][-1] == '-x'
    record = json.loads(path.read_bytes())['sources'][node['source_refs'][0]]
    assert record['locator'] == 'checkout-alias@'+result['source_commit']+':'+result['source_path']


def preview_body(result, control):
    return json.loads((control/'.tare/work-intake'/result['intake_sha256']/'intent.json').read_bytes())['intake']['body']


def test_tui_draft_review_change_and_confirmation_without_external_preparation(proposal, monkeypatch):
    from relay import relay_mesh
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend
    from textual.widgets import Input, Select, TextArea
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', proposal['work_graph'])
    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}], 'sessions': [], 'works': [], 'errors': []}
    async def scenario():
        app = WorkConsole(proposal['owner_roots']['owner'], backend=LocalBackend())
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-1'
            await app.draft_intake().wait()
            assert app.query_one('#intake-criteria', TextArea).text == 'Model-proposed wording'
            app.query_one('#intake-argv', TextArea).load_text(sys.executable+'\n-m\npytest\n-q')
            await app.preview_preparation(intake=True).wait()
            assert app.reviewed_preparation is not None
            app.query_one('#intake-criteria', TextArea).load_text('Changed by operator')
            app.confirm_preparation()
            assert app.reviewed_preparation is None
            await app.preview_preparation(intake=True).wait()
            app.confirm_preparation()
            while app.busy: await pilot.pause(.05)
            assert app.last_result['status'] == 'PREPARED'
            node = work_run.load_node(proposal['work_graph'], 'WORK-1')
            assert node['exit_criteria'] == ['AC-01: Changed by operator']
            assert 'library_manifest' not in node['metrics']
    asyncio.run(scenario())
