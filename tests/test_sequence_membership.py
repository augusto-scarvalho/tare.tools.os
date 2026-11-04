"""Structural sequence revision: explicit membership over real Git/Work fixtures, never restarting Work."""
import copy
import json

import pytest
from relay import relay_mesh, work_run, work_sequence
from relay.work_change import work_locks
from test_sequence_revision import stop_after_first
from test_work_sequence import sequence, create, save_graph
from test_work_resume import host
from test_work_run import os_root


def graph_data():
    return json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())


def saved(case):
    return (case['dir']/work_sequence.FILE).read_bytes()


def edge(data, source, target, kind='UNLOCKS'):
    return {**copy.deepcopy(data['edges'][0]), 'from': source, 'to': target, 'type': kind}


def add_work(key, prerequisite='work-one', **fields):
    """A prepared copy of work-two, the way a split or replacement owner would add a successor."""
    data = graph_data()
    node = copy.deepcopy(next(n for n in data['nodes'] if n['id'] == 'work-two'))
    node.update(id=key, canonical_id=key, **fields)
    data['nodes'].append(node)
    if prerequisite:
        data['edges'].append(edge(data, prerequisite, key))
    save_graph(data)


def supersede(old, *successors):
    data = graph_data()
    next(n for n in data['nodes'] if n['id'] == old)['completion'] = {'status': 'SUPERSEDED', 'dod_satisfied': False}
    data['edges'] += [edge(data, old, key, 'SUPERSEDED_BY') for key in successors]
    save_graph(data)


def fake_train(train_id, state=True):
    train = relay_mesh.TRAINS_DIR/train_id
    train.mkdir(parents=True)
    if state:
        (train/'STATE.json').write_text(json.dumps({'status': 'IN_PROGRESS_IMPLEMENTER'}))


def test_split_unstarted_work_preserves_completed_work_and_prior_membership(sequence, monkeypatch):
    create(sequence)
    first = copy.deepcopy(stop_after_first(sequence, monkeypatch)['items'][0])
    add_work('work-two-a'); add_work('work-two-b'); supersede('work-two', 'work-two-a', 'work-two-b')
    ids = ['work-one', 'work-two-a', 'work-two-b']
    before, calls = saved(sequence), len(sequence['calls'])
    view = work_sequence.preview_revision(sequence['dir'], 'Split second Work', work_ids=ids)
    assert [r['work_id'] for r in view['removed']] == ['work-two']
    assert view['removed'][0]['graph_status'] == 'SUPERSEDED'
    assert [r['work_id'] for r in view['added']] == ['work-two-a', 'work-two-b']
    assert view['added'][0]['exit_criteria'] == ['content-is-valid'] and view['added'][0]['verification_checks']
    assert view['retained'] == ['work-one'] and view['items'][0] == first and not view['changes']
    assert view['requested_order'] == ids and view['previous_requested_order'] == ['work-two', 'work-one']
    assert saved(sequence) == before and len(sequence['calls']) == calls
    result = work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)
    assert result['status'] == 'READY' and result['requested_order'] == ids
    assert [r['work_id'] for r in result['items']] == ids and result['items'][0] == first
    history = json.loads(saved(sequence))['revisions'][-1]
    assert history['previous']['requested_order'] == ['work-two', 'work-one']
    assert [r['work_id'] for r in history['previous']['items']] == ['work-one', 'work-two']
    assert history['previous']['items'][0] == first and history['preview_sha256'] == view['sha256']
    assert len(sequence['calls']) == calls
    with pytest.raises(ValueError, match='No Work changes'):
        work_sequence.preview_revision(sequence['dir'], 'Again', work_ids=ids)


def test_replace_unstarted_work_without_graph_supersession(sequence):
    create(sequence); add_work('work-three')
    ids = ['work-three', 'work-one']
    view = work_sequence.preview_revision(sequence['dir'], 'Replace second Work', work_ids=ids)
    assert [r['work_id'] for r in view['removed']] == ['work-two'] and view['removed'][0]['graph_status'] == 'NOT_DONE'
    assert [r['work_id'] for r in view['items']] == ['work-one', 'work-three']
    result = work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)
    assert result['requested_order'] == ids and not sequence['calls']


@pytest.mark.parametrize('fault', ['settled', 'train', 'receipt', 'done', 'gone', 'superseded-train'])
def test_removing_started_completed_or_unknown_work_is_refused(sequence, monkeypatch, fault):
    create(sequence)
    ids = ['work-one']
    if fault == 'settled':
        stop_after_first(sequence, monkeypatch); ids = ['work-two']
    elif fault == 'train':
        fake_train('RUN-work-two')
    elif fault == 'receipt':
        original = work_run.status
        monkeypatch.setattr(work_run, 'status',
                            lambda key: {'last_run': {'status': 'FAILED'}} if key == 'work-two' else original(key))
    elif fault in {'done', 'gone'}:
        data = graph_data()
        if fault == 'done':
            data['nodes'][1]['completion'] = {'status': 'DONE', 'dod_satisfied': True}
        else:
            data['nodes'] = data['nodes'][:1]; data['edges'] = []
        save_graph(data)
    else:
        add_work('work-three'); supersede('work-two', 'work-three')
        fake_train('RUN-work-two', state=False)  # Custody without a train state is still execution evidence.
        ids = ['work-one', 'work-three']
    before, calls = saved(sequence), len(sequence['calls'])
    with pytest.raises(ValueError, match='SEQUENCE_REMOVAL_UNSAFE'):
        work_sequence.preview_revision(sequence['dir'], 'Drop Work', work_ids=ids)
    with pytest.raises(work_sequence.RevisionRefused):
        work_sequence.revise(sequence['dir'], '0'*64, 'Drop Work', work_ids=ids)
    assert saved(sequence) == before and len(sequence['calls']) == calls


@pytest.mark.parametrize('fault', ['duplicate', 'empty', 'missing', 'unprepared', 'satisfied', 'partial',
                                   'other-repo', 'attempted', 'external', 'orphaned'])
def test_added_or_remaining_work_must_be_prepared_unstarted_and_self_contained(sequence, fault):
    create(sequence)
    ids = ['work-one', 'work-two', 'work-three']
    if fault == 'duplicate': ids = ['work-one', 'work-one']
    elif fault == 'empty': ids = []
    elif fault == 'missing': ids[-1] = 'absent'
    elif fault == 'unprepared': add_work('work-three', admission_state='PROPOSED_FROM_PLAN_NOT_ADMITTED')
    elif fault == 'satisfied': add_work('work-three', completion={'status': 'DONE', 'dod_satisfied': True})
    elif fault == 'partial': add_work('work-three', completion={'status': 'PARTIAL', 'dod_satisfied': False})
    elif fault == 'other-repo':
        scopes = copy.deepcopy(graph_data()['nodes'][1]['repository_scopes'])
        scopes[0]['repository'] = 'repo-two'
        add_work('work-three', repository_scopes=scopes)
    elif fault == 'attempted':
        add_work('work-three'); fake_train('RUN-work-three')
    elif fault == 'external':
        add_work('external', prerequisite=None); add_work('work-three', prerequisite='external')
    else:
        ids = ['work-two']  # Removing unstarted work-one leaves work-two with an unmet prerequisite.
    expected = {'duplicate': 'distinct', 'empty': 'distinct', 'missing': 'NOT_FOUND', 'unprepared': 'PREPARATION',
                'satisfied': 'NOT_UNSTARTED', 'partial': 'NOT_UNSTARTED', 'other-repo': 'ONE_PROJECT',
                'attempted': 'NOT_UNSTARTED', 'external': 'PREREQUISITE_OUTSIDE',
                'orphaned': 'PREREQUISITE_OUTSIDE'}[fault]
    before = saved(sequence)
    with pytest.raises(ValueError, match=expected):
        work_sequence.preview_revision(sequence['dir'], 'Change membership', work_ids=ids)
    assert saved(sequence) == before and not sequence['calls']


def test_membership_approval_is_bound_to_exact_selection_order_and_graph(sequence):
    create(sequence); add_work('work-three')
    ids = ['work-one', 'work-two', 'work-three']
    view = work_sequence.preview_revision(sequence['dir'], 'Add third Work', work_ids=ids)
    before = saved(sequence)
    for stale in (None, ['work-one', 'work-three', 'work-two'], ['work-one', 'work-two']):
        with pytest.raises(work_sequence.RevisionRefused):
            work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=stale)
    with pytest.raises(work_sequence.RevisionRefused, match='CHANGED_REVIEW'):
        work_sequence.revise(sequence['dir'], view['sha256'], 'Other reason', work_ids=ids)
    data = graph_data(); data['nodes'][-1]['title'] = 'Changed after review'; save_graph(data)
    with pytest.raises(work_sequence.RevisionRefused, match='CHANGED_REVIEW'):
        work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)
    assert saved(sequence) == before
    view = work_sequence.preview_revision(sequence['dir'], 'Add third Work', work_ids=ids)
    assert work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)['requested_order'] == ids
    # Order-only change is a real revision; repeating it is a no-op and refuses.
    order = ['work-one', 'work-three', 'work-two']
    view = work_sequence.preview_revision(sequence['dir'], 'Third before second', work_ids=order)
    assert not (view['changes'] or view['added'] or view['removed'])
    assert [r['work_id'] for r in view['items']] == order
    work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=order)
    with pytest.raises(ValueError, match='No Work changes'):
        work_sequence.preview_revision(sequence['dir'], 'Same order', work_ids=order)
    assert not sequence['calls']


@pytest.mark.parametrize('held', ['work-two', 'work-three'])
def test_confirmation_locks_union_of_previous_and_proposed_work(sequence, held):
    create(sequence); add_work('work-three')
    ids = ['work-one', 'work-three']
    view = work_sequence.preview_revision(sequence['dir'], 'Replace second Work', work_ids=ids)
    before = saved(sequence)
    with work_locks([held]), pytest.raises(work_sequence.RevisionRefused):
        work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)
    assert saved(sequence) == before
    assert work_sequence.revise(sequence['dir'], view['sha256'], view['reason'], work_ids=ids)['status'] == 'READY'
    assert not sequence['calls']


def test_cli_revision_works_only_with_revision_actions(sequence, capsys):
    create(sequence)
    args = ['--sequence', str(sequence['dir']), '--reason', 'Reorder']
    order = ['--revision-works', 'work-one', 'work-two']
    assert work_sequence.main([*args, *order]) == 2
    other = sequence['dir'].parent/'other'
    assert work_sequence.main(['--works', 'work-one', '--root', str(sequence['repo']), '--sequence', str(other),
                               '--native-deployment', str(sequence['host'][2]), *order]) == 2
    assert not (other/work_sequence.FILE).exists()
    with pytest.raises(SystemExit):
        work_sequence.main([*args, '--preview-revision', '--revision-works'])
    capsys.readouterr()
    assert work_sequence.main([*args, '--preview-revision', *order]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['requested_order'] == ['work-one', 'work-two']
    assert work_sequence.main([*args, '--revise', '--expected-revision', view['sha256']]) == 2
    assert work_sequence.main([*args, '--revise', '--expected-revision', view['sha256'],
                               '--revision-works', 'work-two', 'work-one']) == 2
    capsys.readouterr()
    assert work_sequence.main([*args, '--revise', '--expected-revision', view['sha256'], *order]) == 0
    assert json.loads(capsys.readouterr().out)['requested_order'] == ['work-one', 'work-two']
    stored = json.loads(saved(sequence))
    assert stored['requested_order'] == ['work-one', 'work-two']
    assert stored['revisions'][-1]['previous']['requested_order'] == ['work-two', 'work-one']
    assert not sequence['calls']
