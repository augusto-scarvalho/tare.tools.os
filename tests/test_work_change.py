"""Work revisions use real graph transactions and Git-pinned source bodies."""
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from graph_backlog.jsonutil import compute_revision_hash
from graph_backlog.grounding import ground_work_item
from relay import relay_mesh, work_change, work_prepare, work_run
from test_source_admission import admission, git


def test_sequence_reuses_unaffected_completion_and_reopens_only_revised_dependency_cone(completed):
    from relay import work_sequence
    case=completed; root=case['owner_roots']['owner']
    directory=case['work_graph'].parent/'sequence'
    ids=['WORK-1','DEPENDENT','TRANSITIVE','UNRELATED']
    original=work_sequence.create(directory,ids,root,{})
    revised=preview(case); work_change.apply(revised)
    for key in ['DEPENDENT','TRANSITIVE']:
        work_prepare.apply(work_prepare.prepare(case['work_graph'],key,root,
            'owner:docs/spec.md',['AC-01'],[sys.executable,'-m','pytest','-q']))
    view=work_sequence.preview_revision(directory,'Corrected the upstream premise.')
    assert view['retained']==['UNRELATED']
    assert [r['work_id'] for r in view['rerun']]==ids[:3]
    after=work_sequence.revise(directory,view['sha256'],view['reason'])
    assert after['items'][-1]==original['items'][-1]
    assert all(r['status']=='PENDING' for r in after['items'][:3])


def test_sequence_adopts_multiple_explicit_revisions_without_intermediate_execution(completed):
    from relay import work_sequence
    root=completed['owner_roots']['owner']; directory=completed['work_graph'].parent/'sequence'
    work_sequence.create(directory,['WORK-1','UNRELATED'],root,{})
    first=work_change.apply(preview(completed))
    second=work_change.apply(preview(completed,paths=['src/revised']))
    view=work_sequence.preview_revision(directory,'Adopt both corrections.')
    assert view['retained']==['UNRELATED']
    assert view['rerun'][0]['revision_receipts']==[second['receipt'],first['receipt']]


@pytest.mark.parametrize('fault',['running','unknown','candidate','missing-state','receipt-changed',
                                'pending-effect','unknown-effect'])
def test_failed_revision_requires_finished_evidence_and_rechecks_it(completed,fault):
    train=work_run.train_dir_for('WORK-1'); train.mkdir(parents=True)
    (train/'STATE.json').write_text(json.dumps({'status':'IN_PROGRESS_IMPLEMENTER'}))
    from uuid import uuid4
    run_id=uuid4().hex
    file=work_run.receipt_path(run_id); file.parent.mkdir(parents=True)
    receipt={'run_id':run_id,'work_id':'WORK-1','train_id':train.name,'status':'FAILED',
        'ended_at':'2026-09-23T00:00:00Z','repository_root':str(completed['owner_roots']['owner'])}
    file.write_text(json.dumps(receipt))
    if fault=='receipt-changed':
        view=preview(completed)
        receipt['error']='changed after review'
    elif fault=='running': receipt.pop('ended_at')
    elif fault=='unknown': receipt['status']='RUNNING'
    elif fault=='candidate': (train/'CANDIDATE_CHANGE.json').write_text('{}')
    elif fault.endswith('-effect'):
        from compute_plane.native_journal import NativeJournal, save_json
        custody=train/'implementation'; custody.mkdir()
        save_json(train/'WORK_RUN.json',{'implementation_custody':str(custody)})
        save_json(custody/'attempt.json',{'attempts':[{'ended_at':'finished'}]})
        journal=NativeJournal(custody/'journal.jsonl')
        journal.append({'kind':'effect_requested','effect_id':'command'})
        if fault=='unknown-effect':
            journal.append({'kind':'effect_returned','result':{'effect_id':'command','status':'unknown'}})
    else: (train/'STATE.json').unlink()
    file.write_text(json.dumps(receipt))
    before=completed['work_graph'].read_bytes()
    with pytest.raises(ValueError):
        work_change.apply(view) if fault=='receipt-changed' else preview(completed)
    assert completed['work_graph'].read_bytes()==before


@pytest.fixture
def tmp_path(tmp_path):
    # Receipts live beside the relay root (RELAY_ROOT.parent); nest the case so that
    # folder is per test instead of pytest's shared base directory.
    case = tmp_path/'case'; case.mkdir()
    return case


@pytest.fixture
def completed(admission, monkeypatch):
    path = admission['work_graph']
    original = relay_mesh.RELAY_ROOT
    relay_mesh.rebind_root(path.parent)
    monkeypatch.setattr(work_change, 'change_root', lambda: path.parent/'work-changes')
    graph = json.loads(path.read_bytes())
    template = graph['nodes'][0]
    template['completion'] = {'status': 'DONE', 'dod_satisfied': True, 'evidence_grade': 'LOCAL',
                              'summary': 'Previous verification'}
    template['exit_criteria'] = ['Previous criterion']
    for name in ['DEPENDENT', 'TRANSITIVE', 'UNRELATED', 'UNSTARTED']:
        node = copy.deepcopy(template); node.update(id=name, canonical_id=name)
        if name == 'UNSTARTED':
            node['completion'] = {'status': 'NOT_DONE', 'dod_satisfied': False}
        graph['nodes'].append(node)
    for source, target in [('WORK-1', 'DEPENDENT'), ('DEPENDENT', 'TRANSITIVE'), ('WORK-1', 'UNSTARTED')]:
        graph['edges'].append({'from': source, 'to': target, 'type': 'UNLOCKS', 'semantic': True,
            'source_refs': [], 'source_details': [], 'confidence': {'score': 1}, 'notes': [], 'requirements': {}})
    graph['revision'] = compute_revision_hash(graph)
    path.write_text(json.dumps(graph))
    yield admission
    relay_mesh.rebind_root(original)


def preview(case, **kwargs):
    return work_change.preview(case['work_graph'], 'WORK-1', case['owner_roots']['owner'],
        'owner:docs/spec.md', ['AC-01'], [sys.executable, '-m', 'pytest', '-q'],
        'Canonical acceptance criterion changed', **kwargs)


def test_revision_preserves_history_invalidates_only_claimed_dependents_and_is_replayable(completed):
    path = completed['work_graph']; before = path.read_bytes()
    old_train = work_run.train_dir_for('WORK-1'); old_train.mkdir(parents=True)
    (old_train/'STATE.json').write_text(json.dumps({'status': 'LANDED'}))
    (old_train/'CANDIDATE_CHANGE.json').write_text('historical candidate')
    runs = relay_mesh.RELAY_ROOT.parent/'.reconciliation'/'work-runs'/'old'
    runs.mkdir(parents=True)
    (runs/'run.json').write_text(json.dumps({'work_id': 'WORK-1', 'train_id': 'RUN-WORK-1', 'status': 'SETTLED'}))
    assert work_run.status('WORK-1')['last_run']['status'] == 'SETTLED'
    view = preview(completed)
    assert path.read_bytes() == before and not Path(view['change']['receipt_path']).exists()
    result = work_change.apply(view)
    assert result['invalidated'] == ['WORK-1', 'DEPENDENT', 'TRANSITIVE']
    saved = json.loads(Path(result['receipt']).read_bytes())
    assert saved == view and all(n['completion']['status'] == 'DONE' for n in saved['change']['before'])
    graph = json.loads(path.read_bytes()); nodes = {n['id']: n for n in graph['nodes']}
    assert nodes['WORK-1']['exit_criteria'] == ['AC-01: preserve exact intent']
    assert nodes['WORK-1']['admission_state'] == 'ADMITTED'
    assert nodes['WORK-1']['completion']['status'] == 'NOT_DONE'
    for name in ['DEPENDENT', 'TRANSITIVE']:
        assert nodes[name]['admission_state'] == work_prepare.PROPOSED
        assert nodes[name]['completion']['dod_satisfied'] is False
        with pytest.raises(ValueError, match='WORK_REQUIRES_PREPARATION'):
            work_run.load_node(path, name)
        assert ground_work_item(path, name, max_bytes=65536).status == 'BLOCKED'
    originals = {n['id']: n for n in json.loads(before)['nodes']}
    for name in ['UNRELATED', 'UNSTARTED']:
        assert nodes[name] == originals[name]
    assert graph['edges'] == json.loads(before)['edges']
    assert work_run.train_dir_for('WORK-1') != old_train
    assert work_run.status('WORK-1')['last_run'] is None
    assert work_run.status('WORK-1')['train'] is None
    assert (old_train/'CANDIDATE_CHANGE.json').read_text() == 'historical candidate'
    after = path.read_bytes()
    assert work_change.apply(view) == result and path.read_bytes() == after
    with pytest.raises(ValueError, match='No criteria'):
        preview(completed)


@pytest.mark.parametrize('fault', ['graph', 'head', 'preview', 'candidate', 'active', 'lock', 'receipt'])
def test_changed_inputs_or_busy_work_cannot_apply_revision(completed, monkeypatch, fault):
    view = preview(completed)
    if fault == 'graph':
        path = completed['work_graph']; graph = json.loads(path.read_bytes())
        graph['nodes'][0]['title'] = 'Concurrent change'
        graph['revision'] = compute_revision_hash(graph); path.write_text(json.dumps(graph))
    elif fault == 'head':
        git(completed['owner_roots']['owner'], 'commit', '--allow-empty', '-qm', 'new head')
    elif fault == 'preview':
        view['change']['reason'] = 'Tampered'
    elif fault in {'candidate', 'active'}:
        train = work_run.train_dir_for('DEPENDENT'); train.mkdir(parents=True)
        (train/'STATE.json').write_text(json.dumps({'status': 'IN_PROGRESS_IMPLEMENTER'}))
        if fault == 'candidate': (train/'CANDIDATE_CHANGE.json').write_text('{}')
    elif fault == 'receipt':
        from compute_plane import native_journal
        def fail(*args): raise OSError('receipt write failed')
        monkeypatch.setattr(native_journal, 'save_json', fail)
    before = completed['work_graph'].read_bytes()
    if fault == 'lock':
        with work_change.work_locks(['DEPENDENT']):
            with pytest.raises((ValueError, OSError, RuntimeError)):
                work_change.apply(view)
    else:
        with pytest.raises((ValueError, OSError)):
            work_change.apply(view)
    assert completed['work_graph'].read_bytes() == before


@pytest.mark.parametrize('paths', [[], 'src', [None], ['../outside']])
def test_bad_scope_is_refused(completed, paths):
    before = completed['work_graph'].read_bytes()
    with pytest.raises(ValueError): preview(completed, paths=paths)
    assert completed['work_graph'].read_bytes() == before


def test_tui_requires_review_of_unchanged_inputs_then_applies(completed):
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend
    from textual.widgets import Input, Select, Button
    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [], 'sessions': [], 'works': [], 'errors': []}
    async def scenario():
        app = WorkConsole(completed['owner_roots']['owner'], backend=LocalBackend())
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'WORK-1'
            await app.load_sources().wait()
            app.query_one('#source-ref', Select).value = 'owner:docs/spec.md'
            app.query_one('#criterion-ids', Input).value = 'AC-01'
            app.query_one('#work-verifier', Input).value = json.dumps([sys.executable, '-m', 'pytest', '-q'])
            app.query_one('#revision-reason', Input).value = 'Canonical criteria changed'
            before = completed['work_graph'].read_bytes()
            await app.preview_revision().wait()
            assert not app.query_one('#confirm-revision', Button).disabled
            app.query_one('#revision-reason', Input).value = 'Different reason'
            app.confirm_revision()
            assert app.reviewed_revision is None and completed['work_graph'].read_bytes() == before
            await app.preview_revision().wait()
            app.confirm_revision()
            while app.busy: await pilot.pause(0.05)
            assert app.last_result['status'] == 'REVISED'
            assert app.query_one('#confirm-revision', Button).disabled
    asyncio.run(scenario())


def test_revised_train_can_start_without_reusing_landed_train(os_root):
    # A deterministic injected auditor isolates train identity from model quality.
    _, repo = os_root
    path = relay_mesh.WORK_GRAPH_FILE; graph = json.loads(path.read_bytes())
    graph['nodes'][0]['metrics']['execution_revision'] = 'a'*24
    path.write_text(json.dumps(graph))
    old = relay_mesh.TRAINS_DIR/'RUN-work-one'; old.mkdir()
    (old/'STATE.json').write_text(json.dumps({'status': 'LANDED', 'task_ids': ['work-one']}))
    result = work_run.run_work('work-one', repository_root=repo, dry_run=True,
        plan_auditor=lambda *args: {'verdict': 'ACCEPT', 'findings': []})
    assert result['status'] == 'DRY_RUN', result
    assert result['train_id'] == 'RUN-work-one-r'+'a'*24
    assert work_run.status('work-one')['train']['status'] == 'PLAN_APPROVED'
    assert json.loads((old/'STATE.json').read_bytes())['status'] == 'LANDED'
    refused = work_run.run_work('work-one', repository_root=repo, dry_run=True,
        plan_auditor=lambda *args: {'verdict': 'ACCEPT', 'findings': []})
    assert 'TRAIN_EXISTS_USE_RESUME' in refused['error']
    assert work_run.status('work-one')['last_run']['run_id'] == refused['run_id']


def test_two_execution_revisions_settle_separately(os_root):
    from test_work_run import evidence
    _, repo = os_root
    def implement(worktree, descriptor):
        (worktree/'src/value.txt').write_text('strict\n', encoding='utf-8')
        return {'status': 'SUCCEEDED'}
    options = dict(repository_root=repo, plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []},
                   implementer=implement, evidence_factory=evidence)
    first = work_run.run_work('work-one', **options)
    assert first['status'] == 'SETTLED', first
    old = work_run.train_dir_for('work-one')
    historical = {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()}
    graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    node = graph['nodes'][0]
    node['metrics']['execution_revision'] = 'b'*24
    node['completion'] = {'status': 'NOT_DONE', 'dod_satisfied': False}
    node['work_status'] = 'planned'
    node['metrics']['verification_checks'][0]['command'][-1] = (
        "from pathlib import Path; assert Path('src/value.txt').read_text() == 'revised\\n'")
    graph['revision'] = compute_revision_hash(graph)
    relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph))
    def revised(worktree, descriptor):
        (worktree/'src/value.txt').write_text('revised\n', encoding='utf-8')
        return {'status': 'SUCCEEDED'}
    second = work_run.run_work('work-one', **(options | {'implementer': revised}))
    assert second['status'] == 'SETTLED', second
    assert second['candidate_commit'] != first['candidate_commit']
    assert work_run.status('work-one')['last_run']['run_id'] == second['run_id']
    assert work_run.train_dir_for('work-one') != old
    assert historical == {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()}
    assert (repo/'src/value.txt').read_text() == 'revised\n'


from test_work_run import os_root
