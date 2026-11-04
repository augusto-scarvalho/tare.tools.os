"""Offline FLOW-18 descriptive projection and additive fail-open UI contracts."""
from copy import deepcopy
import asyncio
import json

import pytest

from relay import behavioral_profiles as profiles, flow_usage
from scripts.round_table import report_round_table_telemetry as telemetry
from test_completed_work_review import settled
from test_completed_work_usage import owner, saved_work_rows
from test_inference_telemetry import save
from test_native_work_telemetry import native_call, usage, work
from test_work_call_attribution_report import claim


def evidence(tmp_path):
    save(tmp_path/'origin/run.json', work('origin', repository_root='project-a',
         runtime_identity='runtime/1 effort=low', task_class='change',
         inference_calls=[claim(stage='implementer')]))
    save(tmp_path/'resume/run.json', settled('resume', 22, resumed_from='origin',
         repository_root='project-b', runtime_identity='runtime/2 effort=high',
         recorded_audit_outcome='ACCEPT',
         inference_calls=[claim('session/model/2', 'code-audit'),
                          claim('session/model/3', 'implement')]))
    save(tmp_path/'copy/run.json', settled('resume', 22, resumed_from='origin',
         repository_root='project-b', runtime_identity='runtime/2 effort=high',
         recorded_audit_outcome='ACCEPT',
         inference_calls=[claim('session/model/2', 'code-audit'),
                          claim('session/model/3', 'implement')]))
    save(tmp_path/'usage.json', usage(native_call(),
         native_call('session/model/2', observation={'requested_model': 'audit-only',
                     'observed_model': 'auditor', 'status': 'complete'}),
         native_call('session/model/3')))
    return telemetry.collect([tmp_path], include_work_metadata=True)


def test_resume_failed_attempts_work_not_calls_and_stage_separation(tmp_path):
    report = evidence(tmp_path)
    before = deepcopy(report)
    result = profiles.behavioral_profile_review(report, {'requested_model': 'requested'})
    assert report == before
    assert result['completed_execution_count'] == 1
    row, = result['rows']
    assert {a['status'] for a in row['attempts']} == {'FAILED', 'SETTLED'}
    assert len(row['attempts']) == 2
    assert row['model_call_usage']['count'] == 3
    assert row['model_call_usage']['by_stage'] == {'implementer': 1, 'code-audit': 1, 'implement': 1}
    assert row['implementer_models'] == {'requested_model': ['requested'], 'observed_model': ['observed']}
    assert row['comparison']['requested_model']['status'] == 'agreement'
    assert row['comparison']['observed_model']['status'] == 'mismatch'
    metadata = row['metadata']
    assert metadata['repository_root']['values'] == ['project-a', 'project-b']
    assert metadata['repository_root']['conflict']
    assert metadata['runtime_identity']['values'] == ['runtime/1 effort=low', 'runtime/2 effort=high']
    assert metadata['task_class']['missing_attempts'] == 1
    assert result['metadata_coverage']['repository_root']['conflicting_executions'] == 1
    assert {'metadata_conflict', 'metadata_missing', 'audit_verdict_recorded'} <= set(row['findings'])
    for key in ('quality', 'independence', 'adjudicated_audit_findings', 'full_cost_usd',
                'runtime_version', 'runtime_effort'):
        assert row[key] is None
    text = profiles.render_behavioral_profiles(result)
    for phrase in ('not model calls', 'not measured delivery quality', 'not an adjudicated audit finding',
                   'not a quality preference or equivalence judgment', 'runtime/1 effort=low'):
        assert phrase in text


@pytest.mark.parametrize('model,status', [('requested', 'agreement'), ('Requested', 'mismatch'),
                                          ('requested-v2', 'mismatch'), (None, 'unknown')])
def test_exact_baseline_unknown_effort_version_and_whitelist(tmp_path, model, status):
    baseline = profiles.selected_baseline({'id': 'selected-id', 'auth': 'PRIVATE',
        'native_profile': {'model': model, 'protocol': 'responses', 'endpoint': 'PRIVATE',
                           'parameters_json': '{"reasoning":{"effort":"high"},"secret":"PRIVATE"}'}})
    assert 'PRIVATE' not in json.dumps(baseline)
    assert baseline['effort'] == 'high' and baseline['version'] is None
    row, = profiles.behavioral_profile_review(evidence(tmp_path), baseline)['rows']
    assert row['comparison']['requested_model']['status'] == status
    assert row['runtime_version'] is row['runtime_effort'] is None


@pytest.mark.parametrize('data', [None, [], {'work_runs': [None]}, {'work_runs': 'bad'}])
def test_malformed_optional_projection_is_unavailable(data):
    result = profiles.behavioral_profile_review(data)
    assert result['status'] == 'unavailable'
    assert 'unavailable' in profiles.render_behavioral_profiles(result)


def test_empty_legacy_and_administrative_verdicts_stay_unknown(tmp_path):
    empty = profiles.behavioral_profile_review({})
    assert empty['rows'] == [] and empty['completed_execution_count'] == 0
    for day, verdict in enumerate(('SETTLED', 'ACCEPT', 'ADMITTED'), 1):
        save(tmp_path/str(day)/'run.json', settled(str(day), day, recorded_audit_outcome=verdict))
    result = profiles.behavioral_profile_review(telemetry.collect([tmp_path], include_work_metadata=True),
                                               {'requested_model': 'current', 'effort': 'high'})
    assert result['representative_quality_cohort'] is result['model_preference'] is None
    for row in result['rows']:
        assert row['implementer_models']['requested_model'] == []
        assert row['comparison']['requested_model']['status'] == 'unknown'
        assert row['metadata']['runtime_identity']['values'] == []
        assert row['quality'] is row['adjudicated_audit_findings'] is None


def test_bounded_projection(tmp_path):
    for day in range(1, 12):
        save(tmp_path/str(day)/'run.json', settled(str(day), day))
    result = profiles.behavioral_profile_review(telemetry.collect([tmp_path]))
    assert len(result['rows']) == result['completed_execution_count'] == 10
    assert result['representative_quality_cohort'] is None


def test_additive_projection_reuses_report_and_failure_keeps_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_usage, 'completed_work_usage_rows', saved_work_rows)
    control, custody = owner(tmp_path)
    save(custody/'done/run.json', settled('done'))
    calls = []
    collect = telemetry.collect
    def counted(*args, **kwargs):
        report = collect(*args, **kwargs)
        calls.append(report)
        return report
    monkeypatch.setattr(telemetry, 'collect', counted)
    seen = []
    project = profiles.behavioral_profile_review
    def capture(report, baseline=None, audit_evidence=None):
        seen.append(report)
        return project(report, baseline, audit_evidence=audit_evidence)
    monkeypatch.setattr(profiles, 'behavioral_profile_review', capture)
    rows = flow_usage.completed_work_usage_rows(control, include_provider_pilot=True,
                                                include_behavioral_profiles=True)
    assert len(calls) == 1 and seen[0] is calls[0]
    assert [r['id'] for r in rows] == ['completed-work-review', 'provider-pilot-review', 'behavioral-profiles']
    original = rows[:2]
    def broken(*args, **kwargs):
        raise ValueError('PRIVATE')
    monkeypatch.setattr(profiles, 'render_behavioral_profiles', broken)
    failed = flow_usage.completed_work_usage_rows(control, include_provider_pilot=True,
                                                  include_behavioral_profiles=True)
    assert failed[:2] == original
    assert 'unavailable' in failed[-1]['label'] and 'PRIVATE' not in json.dumps(failed)
    assert len(flow_usage.completed_work_usage_rows(control)) == 1


def test_baseline_loading_failure_does_not_hide_usage(tmp_path, monkeypatch):
    from relay.console_workbench import WorkbenchPanel
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from dataclasses import replace
    monkeypatch.setattr(flow_usage, 'completed_work_usage_rows', saved_work_rows)
    control, custody = owner(tmp_path)
    save(custody/'done/run.json', settled('done'))
    monkeypatch.setattr('relay.relay_mesh.RELAY_ROOT', control)
    monkeypatch.setattr(flow_usage, 'flow_usage_rows', lambda *a, **k: [])
    def broken(*args):
        raise ValueError('PRIVATE baseline')
    monkeypatch.setattr('relay.native_generation.load_deployment', broken)
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            selection = replace(app.selection(), profile='explicit.json', deployment='selected')
            monkeypatch.setattr(app, 'selection', lambda: selection)
            app.action_usage(); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            ids = [r['id'] for r in app.screen.rows]
            assert all(key in ids for key in ('completed-work-review', 'provider-pilot-review', 'behavioral-profiles'))
            assert 'PRIVATE' not in json.dumps(app.screen.rows)
            assert not backend.launches
    asyncio.run(scenario())


def test_empty_owner_still_exposes_selected_baseline_without_creating_custody(tmp_path):
    control, custody = owner(tmp_path)
    before = set(tmp_path.rglob('*'))
    assert saved_work_rows(control) == []  # Existing default stays compatible.
    row, = saved_work_rows(control, include_behavioral_profiles=True,
                          baseline={'requested_model': 'selected-v2', 'effort': 'medium'})
    assert row['id'] == 'behavioral-profiles'
    assert 'Completed Work executions: 0/10' in row['body']
    assert 'selected-v2' in row['body'] and 'medium' in row['body']
    assert not custody.exists() and set(tmp_path.rglob('*')) == before


def test_unavailable_projection_is_visible_and_keeps_other_reviews(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    save(custody/'done/run.json', settled('done'))
    existing = saved_work_rows(control, include_provider_pilot=True)
    monkeypatch.setattr(profiles, 'behavioral_profile_review',
                        lambda *a, **k: {'status': 'unavailable', 'rows': [], 'baseline': None})
    rows = saved_work_rows(control, include_provider_pilot=True, include_behavioral_profiles=True)
    assert rows[:-1] == existing
    assert rows[-1]['id'] == 'behavioral-profiles' and 'unavailable' in rows[-1]['label']


# Small public fixtures; source paths are intentionally nonexistent citations.
def audit_fixture():
    return {'schema': profiles.AUDIT_SCHEMA, 'authority': 'NONE',
            'sources': [{'id': 'source', 'path': '/never/open/private.json', 'sha256': 'a'*64}],
            'reviews': [{'id': 'review-A', 'regime': 'controlled_replay',
                         'source_id': 'source', 'source_pointer': '/runs/0',
                         'requested_model': ' Exact Model ', 'auditor': 'not-implementer',
                         'project': 'project', 'work_id': 'work', 'execution_id': 'A',
                         'candidate_sha256': 'b'*64, 'treatment': 'A',
                         'accepted': True, 'functional_obligation_passed': True,
                         'model_calls': 2, 'reported_total_tokens': 12}],
            'findings': []}


def test_audit_packaged_smoke_and_separate_counts():
    data = profiles.load_behavioral_evidence()
    before = deepcopy(data)
    result = profiles.behavioral_profile_review({}, {'requested_model': 'gpt-6.1-sol'}, data)
    assert data == before
    assert result['completed_execution_count'] == 0
    audit = result['audit_evidence']
    assert audit['regime_counts'] == {'controlled_replay': 16, 'historical_audit': 8}
    assert audit['historical_disposition_counts'] == {'confirmed_corrected': 4, 'unreviewed': 4}
    a, b = (audit['controlled_treatments'][k] for k in ('A', 'B'))
    assert a['review_count'] == b['review_count'] == 8
    assert a['accepted'] == {'true': 8, 'false': 0, 'unknown': 0}
    assert b['accepted'] == {'true': 7, 'false': 1, 'unknown': 0}
    assert b['incomplete_delivery']['true'] == 1
    assert a['model_calls']['known_reviews'] == b['reported_total_tokens']['known_reviews'] == 8
    for row in audit['reviews']:
        assert row['observed_model'] == 'gpt-6.1-sol'
        assert row['runtime_version'] is row['independence'] is None
        assert row['full_cost_usd'] is None
        assert row['baseline_requested_model_comparison']['status'] == 'agreement'
        assert row['baseline_observed_model_comparison']['status'] == 'agreement'
        assert row['model_observation_count'] == row['model_calls']
        assert row['model_observation_source']['sha256']
    assert sum(r['model_observation_count'] for r in audit['reviews']) == 98
    assert all(f['current_status'] is None for f in audit['findings'])
    assert all(f['original_id'] and f['claim'] and f['source']['sha256'] for f in audit['findings'])
    text = profiles.render_behavioral_profiles(result)
    assert 'confirmed_corrected' in text and 'unreviewed' in text
    assert 'CTX-P2-1' in text and 'graph_lock' in text
    assert 'not attest to current source' in text


@pytest.mark.parametrize('baseline,status', [(' Exact Model ', 'agreement'),
    ('Exact Model', 'mismatch'), (' exact Model ', 'mismatch'), (None, 'unknown')])
def test_audit_exact_identities_and_baseline(baseline, status):
    data = audit_fixture()
    data['reviews'][0]['prompt'] = 'PRIVATE EXTRA PROMPT'
    result = profiles.project_audit_evidence(data, {'requested_model': baseline})
    row, = result['reviews']
    assert row['requested_model'] == ' Exact Model '
    assert row['observed_model'] is None
    assert row['baseline_requested_model_comparison']['status'] == status
    assert 'PRIVATE' not in json.dumps(result)


def test_audit_missing_and_ill_typed_values_are_not_pass_or_zero():
    data = audit_fixture()
    data['reviews'][0].update(accepted=1, test_integrity='true', model_calls=True,
                              reported_total_tokens=-1)
    audit = profiles.project_audit_evidence(data)
    row, = audit['reviews']
    assert row['accepted'] is row['test_integrity'] is row['model_calls'] is None
    assert row['incomplete_delivery'] is row['observed_model'] is None
    a = audit['controlled_treatments']['A']
    assert a['accepted'] == {'true': 0, 'false': 0, 'unknown': 1}
    assert a['model_calls']['recorded_total'] is None
    assert a['reported_total_tokens']['unknown_reviews'] == 1


def test_audit_duplicate_identity_and_source_conflicts():
    data = audit_fixture()
    data['reviews'].append(deepcopy(data['reviews'][0]))
    assert profiles.project_audit_evidence(data)['regime_counts']['controlled_replay'] == 1
    data['reviews'].append(dict(data['reviews'][0], accepted=False))
    audit = profiles.project_audit_evidence(data)
    assert audit['reviews'] == [] and audit['conflicting_ids'] == ['review-A']
    data = audit_fixture()
    data['sources'].append(dict(data['sources'][0], sha256='c'*64))
    audit = profiles.project_audit_evidence(data)
    assert audit['reviews'] == [] and audit['conflicting_ids'] == ['source']


@pytest.mark.parametrize('mutation', ['schema', 'source_id', 'source_hash', 'pointer', 'oversize'])
def test_invalid_audit_data_keeps_ordinary_rows_costs_baseline(tmp_path, mutation):
    report = evidence(tmp_path)
    old = profiles.behavioral_profile_review(report, {'requested_model': 'requested'})
    data = audit_fixture()
    if mutation == 'schema':
        data['schema'] = 'invalid'
    elif mutation == 'source_id':
        data['reviews'][0]['source_id'] = 'absent'
    elif mutation == 'source_hash':
        data['sources'][0]['sha256'] = 'not-a-hash'
    elif mutation == 'pointer':
        data['reviews'][0]['source_pointer'] = []
    else:
        data['reviews'] *= 257
    new = profiles.behavioral_profile_review(report, {'requested_model': 'requested'}, data)
    assert new.pop('audit_evidence')['reviews'] == []
    assert new == old
    assert profiles.behavioral_profile_review(report, {'requested_model': 'requested'}, None) == old


def test_audit_pure_projection_never_follows_source_paths(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('pure evidence projection attempted IO')
    monkeypatch.setattr(profiles, 'read_json', forbidden)
    monkeypatch.setattr(profiles.Path, 'open', forbidden)
    audit = profiles.project_audit_evidence(audit_fixture())
    assert audit['regime_counts']['controlled_replay'] == 1
    assert audit['sources'][0]['path'] == '/never/open/private.json'


def test_loader_adjacent_bound_missing_malformed_and_no_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, '__file__', str(tmp_path/'behavioral_profiles.py'))
    target = tmp_path/'behavioral_evidence.json'
    assert profiles.load_behavioral_evidence() == {'schema': None}
    target.write_text('{bad', encoding='utf-8')
    assert profiles.load_behavioral_evidence() == {'schema': None}
    target.write_bytes(b' ' * (profiles.AUDIT_MAX_BYTES + 1))
    assert profiles.load_behavioral_evidence() == {'schema': None}
    target.write_text(json.dumps(audit_fixture()), encoding='utf-8')
    before = target.read_bytes()
    def forbidden(*a, **k):
        pytest.fail('loader attempted writes')
    monkeypatch.setattr(profiles.Path, 'write_text', forbidden)
    monkeypatch.setattr(profiles.Path, 'write_bytes', forbidden)
    assert profiles.load_behavioral_evidence()['schema'] == profiles.AUDIT_SCHEMA
    assert target.read_bytes() == before
    seen = []
    def capture(path, *, max_bytes):
        seen.append((path, max_bytes))
        return audit_fixture()
    monkeypatch.setattr(profiles, 'read_json', capture)
    profiles.load_behavioral_evidence()
    assert seen == [(target, 256*1024)]


@pytest.mark.parametrize('bad_custody', [False, True])
def test_audit_independent_empty_or_bad_ordinary_custody(tmp_path, monkeypatch, bad_custody):
    control, custody = owner(tmp_path)
    if bad_custody:
        path = custody/'bad/run.json'
        path.parent.mkdir(parents=True)
        path.write_text('{bad', encoding='utf-8')
    reads = []
    def load():
        reads.append(True)
        return audit_fixture()
    monkeypatch.setattr(profiles, 'load_behavioral_evidence', load)
    saved_work_rows(control)
    assert not reads  # Opt in only; no default evidence IO.
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows = saved_work_rows(control, include_behavioral_profiles=True)
    assert len(reads) == 1
    assert 'controlled_replay' in rows[-1]['body']
    if bad_custody:
        assert rows[0]['id'] == 'completed-work-review' and 'unavailable' in rows[0]['label']
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_audit_render_before_work_and_bad_evidence_fail_open(tmp_path):
    report = evidence(tmp_path)
    good = profiles.behavioral_profile_review(report, audit_evidence=audit_fixture())
    text = profiles.render_behavioral_profiles(good)
    assert text.index('Source-backed audit evidence') < text.index('Work work-')
    bad = profiles.behavioral_profile_review(report, audit_evidence={'schema': 'bad'})
    assert bad['completed_execution_count'] == 1
    assert bad['rows'] == good['rows']
    assert 'Optional audit evidence unavailable' in profiles.render_behavioral_profiles(bad)


def saved_audit():
    cited = {'repository': 'candidate', 'path': '../citation-only.py', 'sha256': 'a'*64}
    return {'schema': 'tare.tools/work-audit-observation/1', 'artifact_sha256': 'b'*64,
            'verdict': 'REVISE', 'summary': 'Review result', 'sources': [cited],
            'findings': [{'claim': 'Concrete defect', 'sources': [cited]}]}


def test_saved_work_audit_evidence_without_following_result_paths(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    receipt = settled('done', work_id='work-real', repository_root='project-real',
        audit={'deployment': 'auditor-only', 'observation': saved_audit(),
               'result': {'artifact_file': '/private/must-not-open.json', 'status': 'PROPOSED'}})
    save(custody/'done/run.json', receipt)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    seen = []
    original = profiles.project_work_audits
    def capture(observations):
        result = original(observations)
        seen.append(result)
        return result
    monkeypatch.setattr(profiles, 'project_work_audits', capture)
    rows = saved_work_rows(control, include_behavioral_profiles=True)
    row, = seen[0]['reviews']
    assert row['id'] == row['execution_origin_run_id'] == 'done'
    assert row['work_id'] == 'work-real' and row['project'] == 'project-real'
    assert row['recorded_verdict'] == 'REVISE'
    assert row['auditor'] == 'auditor-only' and row['implementer_model'] is None
    assert row['findings'][0]['adjudication'] is row['quality'] is None
    assert 'Concrete defect' in rows[-1]['body']
    assert 'must-not-open' not in rows[-1]['body']
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_saved_work_audit_conflict_missing_sources_and_administrative_acceptance():
    raw = {'id': 'run', 'work_id': 'work', 'observation': saved_audit()}
    result = profiles.project_work_audits([raw, deepcopy(raw)])
    assert len(result['reviews']) == 1
    changed = deepcopy(raw)
    changed['observation']['findings'][0]['claim'] = 'Conflicting claim'
    result = profiles.project_work_audits([raw, changed])
    assert result['reviews'] == [] and result['conflicting_ids'] == ['run']
    changed = deepcopy(raw)
    changed['observation']['findings'][0]['sources'] = []
    assert profiles.project_work_audits([changed])['invalid_record_count'] == 1
    changed['observation'].update(verdict='ACCEPT', findings=[])
    row, = profiles.project_work_audits([changed])['reviews']
    assert row['recorded_verdict'] == 'ACCEPT'
    assert row['quality'] is row['independence'] is row['implementer_model'] is None
    assert profiles.project_work_audits([raw]*129)['status'] == 'unavailable'


def test_bad_saved_audits_do_not_hide_good_archive_or_work_evidence(tmp_path):
    report = evidence(tmp_path)
    before = profiles.behavioral_profile_review(report)
    report['saved_audit_observations'] = 'invalid'
    result = profiles.behavioral_profile_review(report, audit_evidence=audit_fixture())
    assert result['work_audits']['status'] == 'unavailable'
    assert result['rows'] == before['rows']
    assert result['audit_evidence']['status'] == 'available'
