"""Reviewed progress uses owner CAS without discarding evidence or admission."""
import copy
import json

import pytest

from graph_backlog.jsonutil import compute_revision_hash, RevisionMismatchError, UsageError
from graph_backlog.validation import validate_work_graph
from relay import backlog_delivery as delivery
from relay.project_graph import observe
from test_work_console import graph_root


def record(identity='ready', *, remaining=None):
    row = {'work_id': identity, 'summary': 'Implemented the reviewed behavior.',
           'evidence': ['git:commit:report.md'], 'remaining': remaining or [],
           'criteria': [{'criterion': 'unit', 'evidence': 'report.md: reviewed verifier and negative control'}]}
    if not remaining:
        row.update(evidence_grade='C', evidence_grade_reason='Reviewed local report; no independent attestation.')
    return row


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def commit(path, records):
    view = delivery.preview(path, records)
    return delivery.apply(path, view, view['revision'])


def test_partial_then_done_preserves_history_and_other_nodes(graph_root):
    path = graph_root/'work-graph.json'
    original = read(path)
    partial = record(remaining=['POSIX smoke remains.'])
    result = commit(path, [partial])
    node = result['nodes'][0]
    assert validate_work_graph(result)['status'] == 'PASS'
    assert node['work_status'] == 'IN_PROGRESS'
    assert node['completion']['status'] == 'PARTIAL'
    assert node['completion']['residuals'] == partial['remaining']
    assert node['completion']['dod_satisfied'] is False
    assert node['completion']['completed_at'] is None
    assert node['metrics']['delivery_history'][0]['previous_completion'] == original['nodes'][0]['completion']
    assert result['nodes'][1:] == original['nodes'][1:]
    assert result['edges'] == original['edges']
    for key in original['nodes'][0]:
        if key not in {'completion', 'metrics', 'work_status', 'observed_at', 'provenance', 'source_refs'}:
            assert node[key] == original['nodes'][0][key]
    completed = commit(path, [record()])
    node = completed['nodes'][0]
    assert node['completion']['status'] == 'DONE'
    assert node['completion']['dod_satisfied'] is True
    assert node['completion']['completed_at']
    assert node['work_status'] == 'done'
    assert node['admission_state'] == original['nodes'][0]['admission_state']
    assert node['completion']['evidence_grade'] == 'C'
    assert node['completion']['evidence_grade_reason'] == record()['evidence_grade_reason']
    assert node['metrics']['delivery_latest']['evidence_grade'] == 'C'
    assert node['metrics']['delivery_history'][-1]['record']['evidence_grade'] == 'C'
    assert record()['evidence'][0] in node['provenance']
    assert len(node['metrics']['delivery_history']) == 2
    assert node['metrics']['delivery_history'][1]['previous_completion'] == result['nodes'][0]['completion']
    assert node['completion']['residuals'] == []
    assert node['completion']['dod_evidence'] == ['git:commit:report.md']


@pytest.mark.parametrize('admission', ['ADMITTED', 'PROPOSED_FROM_RESEARCH_NOT_ADMITTED',
    'OPERATOR_AUTHORIZED_IMPLEMENTATION', 'OWNER_DIRECTED_BACKLOG'])
def test_completed_delivery_never_invents_pending_admission(graph_root, admission):
    path = graph_root/'work-graph.json'
    before = read(path)
    before['nodes'][0]['admission_state'] = admission
    before['nodes'][0]['authority_required'] = 'Existing owner checks'
    save(path, before)
    result = commit(path, [record()])
    node = result['nodes'][0]
    assert node['completion']['status'] == 'DONE' and node['work_status'] == 'done'
    assert node['admission_state'] == admission
    assert node['authority_required'] == 'Existing owner checks'
    assert node['canonical_revision'] == before['nodes'][0]['canonical_revision']


def legacy_delivery(path, *, ungraded=False):
    raw = commit(path, [record()])
    node = raw['nodes'][0]
    node['work_status'] = delivery.LEGACY_DONE
    if ungraded:
        node['completion']['evidence_grade'] = None
        node['completion']['evidence_grade_reason'] = None
        node['metrics']['delivery_latest'].pop('evidence_grade')
        node['metrics']['delivery_latest'].pop('evidence_grade_reason')
    save(path, raw)
    return read(path)


@pytest.mark.parametrize('ungraded', [False, True])
def test_status_repair_changes_only_the_legacy_label_and_is_idempotent(graph_root, ungraded):
    path = graph_root/'work-graph.json'
    before = legacy_delivery(path, ungraded=ungraded)
    original, stamp = path.read_bytes(), path.stat().st_mtime_ns
    plan = delivery.preview_status_repair(path)
    assert plan['repairs'] == ['ready'] and plan['skipped'] == []
    assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp
    result = delivery.repair_statuses(path, plan, plan['revision'])
    expected = copy.deepcopy(before)
    expected['nodes'][0]['work_status'] = 'done'
    expected['revision'] = compute_revision_hash(expected)
    assert result == expected and read(path) == expected
    original, stamp = path.read_bytes(), path.stat().st_mtime_ns
    replay = delivery.preview_status_repair(path)
    assert replay['repairs'] == []
    delivery.repair_statuses(path, replay, replay['revision'])
    assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize('fault', ['partial', 'dod', 'residual', 'remaining', 'record',
    'criterion', 'evidence', 'missing-evidence', 'managed', 'signoff'])
def test_status_repair_preserves_real_obligations_and_changed_or_managed_records(graph_root, fault):
    path = graph_root/'work-graph.json'
    raw = legacy_delivery(path)
    node = raw['nodes'][0]
    if fault == 'partial': node['completion']['status'] = 'PARTIAL'
    elif fault == 'dod': node['completion']['dod_satisfied'] = False
    elif fault == 'residual': node['completion']['residuals'] = ['Real approval pending.']
    elif fault == 'remaining': node['metrics']['delivery_latest']['remaining'] = ['Real approval pending.']
    elif fault == 'record': node['metrics'].pop('delivery_latest')
    elif fault == 'criterion': node['exit_criteria'].append('New requirement')
    elif fault == 'evidence': node['metrics']['delivery_latest']['evidence'] = ['Missing reference']
    elif fault == 'missing-evidence': node['completion']['dod_evidence'] = None
    elif fault == 'managed': node['metrics']['verification_checks'] = [{'criterion': 'unit'}]
    elif fault == 'signoff': node['completion']['dod_evidence'].append('SMART_POLICY:owner')
    save(path, raw)
    original, stamp = path.read_bytes(), path.stat().st_mtime_ns
    if validate_work_graph(raw)['status'] != 'PASS':
        with pytest.raises(ValueError, match='Invalid backlog graph'):
            delivery.preview_status_repair(path)
        assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp
        return
    plan = delivery.preview_status_repair(path)
    assert plan['repairs'] == [] and plan['skipped'] == ['ready']
    delivery.repair_statuses(path, plan, plan['revision'])
    assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp


def test_status_repair_rejects_stale_or_tampered_previews_atomically(graph_root):
    path = graph_root/'work-graph.json'
    raw = legacy_delivery(path)
    plan = delivery.preview_status_repair(path)
    tampered = copy.deepcopy(plan)
    tampered['repairs'].append('blocked')
    original = path.read_bytes()
    with pytest.raises(ValueError, match='preview again'):
        delivery.repair_statuses(path, tampered, plan['revision'])
    assert path.read_bytes() == original
    raw['nodes'][1]['title'] = 'Concurrent change'
    save(path, raw)
    original = path.read_bytes()
    with pytest.raises(RevisionMismatchError):
        delivery.repair_statuses(path, plan, plan['revision'])
    assert path.read_bytes() == original


def test_cli_status_repair_requires_preview_revision_and_preserves_evidence(graph_root, capsys):
    path = graph_root/'work-graph.json'
    before = legacy_delivery(path, ungraded=True)
    args = ['--graph', str(path), '--repair-statuses']
    original = path.read_bytes()
    assert delivery.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert path.read_bytes() == original
    with pytest.raises(SystemExit) as exc:
        delivery.main(args+['--apply'])
    assert exc.value.code == 2 and path.read_bytes() == original
    capsys.readouterr()
    assert delivery.main(args+['--apply', '--expected-revision', preview['revision']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['authority'] == 'NONE' and result['repaired'] == ['ready']
    after = read(path)
    assert after['nodes'][0]['completion'] == before['nodes'][0]['completion']
    assert after['nodes'][0]['metrics'] == before['nodes'][0]['metrics']
    assert after['nodes'][0]['admission_state'] == before['nodes'][0]['admission_state']


def test_replay_has_no_write_or_timestamp_churn_and_changed_state_is_not_ignored(graph_root):
    path = graph_root/'work-graph.json'
    rows = [record(remaining=['External cohort still pending.'])]
    commit(path, rows)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    commit(path, rows)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    raw = read(path)
    raw['nodes'][0]['completion']['status'] = 'NOT_DONE'
    raw['revision'] = compute_revision_hash(raw)
    path.write_text(json.dumps(raw), encoding='utf-8')
    result = commit(path, rows)
    assert result['nodes'][0]['completion']['status'] == 'PARTIAL'
    assert len(result['nodes'][0]['metrics']['delivery_history']) == 2
    result['nodes'][0]['completion']['dod_evidence'] = []
    result['revision'] = compute_revision_hash(result)
    path.write_text(json.dumps(result), encoding='utf-8')
    repaired = commit(path, rows)
    assert repaired['nodes'][0]['completion']['dod_evidence'] == rows[0]['evidence']
    assert len(repaired['nodes'][0]['metrics']['delivery_history']) == 3


def test_atomic_mixed_batch_and_stale_preview(graph_root):
    path = graph_root/'work-graph.json'
    rows = [record(), record('blocked', remaining=['One criterion not yet qualified.'])]
    view = delivery.preview(path, rows)
    original = read(path)
    original['nodes'][1]['title'] = 'Concurrent edit'
    path.write_text(json.dumps(original), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(RevisionMismatchError):
        delivery.apply(path, view, view['revision'])
    assert path.read_bytes() == before
    result = commit(path, rows)
    assert [n['completion']['status'] for n in result['nodes']] == ['DONE', 'PARTIAL']
    assert result['nodes'][1]['title'] == 'Concurrent edit'
    assert result['revision'] == compute_revision_hash(result)


@pytest.mark.parametrize('change', [
    {'work_id': 'absent'}, {'evidence': []}, {'evidence': [' ']},
    {'remaining': None}, {'remaining': [' ']}, {'summary': ' '},
    {'criteria': []}, {'criteria': [{'criterion': 'invented', 'evidence': 'claim'}]},
    {'criteria': [{'criterion': 'unit', 'evidence': ''}]},
])
def test_invalid_batch_does_not_write(graph_root, change):
    path = graph_root/'work-graph.json'
    rows = [record('blocked'), {**record(), **change}]
    before = path.read_bytes()
    with pytest.raises(ValueError):
        commit(path, rows)
    assert path.read_bytes() == before


def test_duplicate_superseded_and_managed_work_are_refused(graph_root):
    path = graph_root/'work-graph.json'
    with pytest.raises(ValueError, match='duplicate'):
        commit(path, [record(), record()])
    raw = read(path)
    raw['nodes'][0]['completion']['status'] = 'SUPERSEDED'
    path.write_text(json.dumps(raw), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='SUPERSEDED'):
        commit(path, [record()])
    assert path.read_bytes() == before
    raw['nodes'][0]['completion']['status'] = 'NOT_DONE'
    raw['nodes'][0]['metrics']['verification_checks'] = [{'criterion': 'unit', 'command': ['python', 'check.py']}]
    path.write_text(json.dumps(raw), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='managed Work'):
        commit(path, [record()])
    assert path.read_bytes() == before


def test_cli_preview_apply_replay_and_stale_error(graph_root, tmp_path, capsys):
    path = graph_root/'work-graph.json'
    source = tmp_path/'records.json'
    source.write_text(json.dumps([record(remaining=['Qualification remains.'])]), encoding='utf-8')
    args = ['--graph', str(path), '--record', str(source)]
    before = path.read_bytes()
    assert delivery.main(args) == 0
    view = json.loads(capsys.readouterr().out)
    assert path.read_bytes() == before
    assert delivery.main(args+['--apply', '--expected-revision', view['revision']]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved['authority'] == 'NONE'
    assert delivery.main(args+['--apply', '--expected-revision', view['revision']]) == 2
    assert 'error' in json.loads(capsys.readouterr().out)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    assert delivery.main(args+['--apply', '--expected-revision', saved['revision']]) == 0
    capsys.readouterr()
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


def test_summary_exposes_real_remaining_and_bounds_utf8_payload(graph_root):
    path = graph_root/'work-graph.json'
    commit(path, [record(remaining=['POSIX smoke remains.'])])
    view = observe(str(path))
    assert 'PARTIAL' in view['summary'] and 'remaining: POSIX smoke remains.' in view['summary']
    assert '1/1 delivery records shown' in view['summary']
    raw = read(path)
    template = raw['nodes'][0]
    for i in range(30):
        node = copy.deepcopy(template)
        node['id'] = f'excerpt-{i}'
        node['metrics']['delivery_latest']['summary'] = '\u754c'*10000
        node['metrics']['delivery_latest']['remaining'] = ['\u754c'*10000]*10
        raw['nodes'].append(node)
    raw['revision'] = compute_revision_hash(raw)
    path.write_text(json.dumps(raw), encoding='utf-8')
    view = observe(str(path))
    assert len(json.dumps(view, ensure_ascii=False, separators=(',', ':')).encode()) <= 32768
    assert '/31 delivery records shown' in view['summary']
    assert 'full evidence and obligations remain in the graph' in view['summary']


def source():
    return {'kind': 'repo_file', 'title': 'Synthetic reviewed report',
            'recoverability': 'CONTENT_ADDRESSED', 'repository_ref': 'fixture:report.md',
            'sha256': 'a'*64, 'locator': 'fixture@'+'b'*40+':report.md'}


@pytest.mark.parametrize('fault',['unreferenced','overwrite'])
def test_observed_source_additions_cannot_hide_extra_sources_or_replace_existing_records(graph_root,fault):
    path=graph_root/'work-graph.json'
    raw=read(path)
    if fault=='overwrite': raw['sources']['report']={**source(),'title':'Original source metadata'}
    save(path,raw)
    before,stamp=path.read_bytes(),path.stat().st_mtime_ns
    row=record()
    if fault=='overwrite': row['source_refs']=['report']
    with pytest.raises(ValueError): delivery.preview(path,[row],sources={'report':source()})
    assert path.read_bytes()==before and path.stat().st_mtime_ns==stamp


def test_source_registration_cannot_mask_an_invalid_saved_graph_revision(graph_root):
    path=graph_root/'work-graph.json'
    raw=read(path); raw['revision']='f'*64
    path.write_text(json.dumps(raw),encoding='utf-8')
    before,stamp=path.read_bytes(),path.stat().st_mtime_ns
    row=record(); row.update(evidence_grade='A',source_refs=['report'])
    with pytest.raises(ValueError,match='Invalid backlog graph'):
        delivery.preview(path,[row],sources={'report':source()})
    assert path.read_bytes()==before and path.stat().st_mtime_ns==stamp


def save(path, graph):
    graph['revision'] = compute_revision_hash(graph)
    path.write_text(json.dumps(graph), encoding='utf-8')


@pytest.mark.parametrize('fault', ['grade', 'reason', 'blank-reason', 'bad-grade',
                                   'A', 'B', 'missing-source', 'bad-refs'])
@pytest.mark.parametrize('direct_apply', [False, True])
def test_evidence_refusals_do_not_write_even_without_preview(graph_root, fault, direct_apply):
    path = graph_root/'work-graph.json'
    row = record()
    if fault == 'grade':
        row.pop('evidence_grade')
    elif fault == 'reason':
        row.pop('evidence_grade_reason')
    elif fault == 'blank-reason':
        row['evidence_grade_reason'] = ' '
    elif fault == 'bad-grade':
        row['evidence_grade'] = ['C']
    elif fault in ('A', 'B'):
        row['evidence_grade'] = fault
    elif fault == 'missing-source':
        row['source_refs'] = ['nonexistent']
    else:
        row['source_refs'] = 'not-a-list'
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises((ValueError, UsageError)):
        if direct_apply:
            revision = compute_revision_hash(read(path))
            delivery.apply(path, {'revision': revision, 'records': [row]}, revision)
        else:
            delivery.preview(path, [row])
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize('grade', ['A', 'B', 'C', 'D'])
def test_registered_source_metadata_history_and_exact_replay(graph_root, grade):
    path = graph_root/'work-graph.json'
    raw = read(path)
    raw['sources']['report'] = source()
    node = raw['nodes'][0]
    node['provenance'] = ['historical-review']
    node['completion'].update(evidence_grade_original='D', custom_metadata={'keep': True},
                              dod_evidence=['historical-evidence'])
    save(path, raw)
    row = {**record(), 'evidence_grade': grade, 'source_refs': ['report']}
    result = commit(path, [row])
    node = result['nodes'][0]
    assert node['completion']['evidence_grade'] == grade
    assert node['completion']['evidence_grade_original'] == 'D'
    assert node['completion']['custom_metadata'] == {'keep': True}
    assert node['completion']['dod_evidence'] == ['historical-evidence', *row['evidence']]
    assert node['provenance'] == ['historical-review', *row['evidence']]
    assert node['source_refs'] == ['report']
    assert result['sources'] == raw['sources']
    entry = node['metrics']['delivery_history'][-1]
    assert entry['previous_completion'] == raw['nodes'][0]['completion']
    assert entry['previous_provenance'] == ['historical-review']
    assert entry['record']['source_refs'] == ['report']
    assert entry['record']['evidence_grade_reason'] == row['evidence_grade_reason']
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    commit(path, [row])
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    changed = {**row, 'evidence_grade_reason': 'Revised explicit review rationale.'}
    revised = commit(path, [changed])
    assert len(revised['nodes'][0]['metrics']['delivery_history']) == 2
    assert revised['nodes'][0]['completion']['evidence_grade_original'] == 'D'


@pytest.mark.parametrize('missing', ['locator', 'recoverability'])
def test_high_grade_requires_recoverable_registered_source(graph_root, missing):
    path = graph_root/'work-graph.json'
    raw = read(path)
    raw['sources']['report'] = source()
    raw['sources']['report'].pop(missing)
    save(path, raw)
    row = {**record(), 'evidence_grade': 'A', 'source_refs': ['report']}
    before = path.read_bytes()
    with pytest.raises((ValueError, UsageError)):
        delivery.preview(path, [row])
    revision = compute_revision_hash(raw)
    with pytest.raises((ValueError, UsageError)):
        delivery.apply(path, {'revision': revision, 'records': [row]}, revision)
    assert path.read_bytes() == before


def test_legacy_done_replay_and_partial_do_not_migrate_history(graph_root):
    path = graph_root/'work-graph.json'
    raw = read(path)
    row = record()
    row.pop('evidence_grade')
    row.pop('evidence_grade_reason')
    node = raw['nodes'][0]
    node['completion'].update(status='DONE', dod_satisfied=True, completed_at='2020-01-01',
                              residuals=[], dod_evidence=row['evidence'])
    node['work_status'] = 'delivered_pending_admission'
    node['metrics'].update(delivery_latest={k: v for k, v in row.items() if k != 'work_id'},
                           delivery_history=[{'historical': True}])
    save(path, raw)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    commit(path, [row])
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    result = commit(path, [record('blocked', remaining=['Real obligation.'])])
    assert result['nodes'][0] == raw['nodes'][0]
    assert result['nodes'][1]['completion']['status'] == 'PARTIAL'
    assert result['nodes'][1]['completion'].get('evidence_grade') is None
    with pytest.raises(ValueError, match='EVIDENCE_GRADE_REQUIRED'):
        delivery.preview(path, [{**row, 'summary': 'A revised completion cannot inherit a grade.'}])


@pytest.mark.parametrize('change', [{'evidence_grade': None}, {'evidence_grade_reason': 42},
                                    {'source_refs': [42]}, {'source_refs': ['absent']}])
def test_partial_explicit_metadata_is_validated(graph_root, change):
    path = graph_root/'work-graph.json'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        commit(path, [{**record(remaining=['Not complete.']), **change}])
    assert path.read_bytes() == before


def test_partial_explicit_grade_is_a_claim_not_content_attestation(graph_root):
    path = graph_root/'work-graph.json'
    raw = read(path)
    raw['sources']['report'] = source()
    save(path, raw)
    row = {**record(remaining=['Review still needed.']), 'evidence_grade': 'C',
           'evidence_grade_reason': 'Declared local claim only.', 'source_refs': ['report']}
    result = commit(path, [row])
    node = result['nodes'][0]
    assert node['completion']['status'] == 'PARTIAL'
    assert node['completion']['dod_satisfied'] is False
    assert node['metrics']['delivery_latest']['source_refs'] == ['report']
    assert node['source_refs'] == ['report']


def test_preview_and_apply_use_owner_guard_on_candidate(graph_root, monkeypatch):
    path = graph_root/'work-graph.json'
    calls = []
    guard = delivery.validate_completion_evidence
    def observe_guard(before, after, policy=None):
        calls.append((copy.deepcopy(before), copy.deepcopy(after)))
        return guard(before, after, policy)
    monkeypatch.setattr(delivery, 'validate_completion_evidence', observe_guard)
    before = path.read_bytes()
    plan = delivery.preview(path, [record()])
    assert path.read_bytes() == before
    assert len(calls) == 1 and calls[0][1]['nodes'][0]['completion']['status'] == 'DONE'
    delivery.apply(path, plan, plan['revision'])
    assert len(calls) == 2
    assert calls[0][0]['nodes'][0]['completion']['status'] == 'NOT_DONE'
    assert calls[1][0]['nodes'][0]['completion']['status'] == 'NOT_DONE'


def test_revised_done_cannot_inherit_existing_grade_or_reason(graph_root):
    path = graph_root/'work-graph.json'
    commit(path, [record()])
    before = path.read_bytes()
    for field in ('evidence_grade', 'evidence_grade_reason'):
        row = {**record(), 'summary': 'A different reviewed conclusion.'}
        row.pop(field)
        with pytest.raises(ValueError, match='EVIDENCE_GRADE'):
            delivery.preview(path, [row])
        revision = compute_revision_hash(read(path))
        with pytest.raises(ValueError, match='EVIDENCE_GRADE'):
            delivery.apply(path, {'revision': revision, 'records': [row]}, revision)
        assert path.read_bytes() == before
