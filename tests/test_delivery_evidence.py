"""Exact delivery/audit joins, cost coverage and optional owner-recorded review."""
import asyncio
from copy import deepcopy
import json

import pytest
from textual.widgets import Button, Input, Select

from relay import behavioral_profiles, delivery_evidence as evidence, relay_mesh, work_run
from relay.delivery_review_panel import DeliveryReviewPanel
from scripts.round_table import report_round_table_telemetry as telemetry
from test_behavioral_profiles import saved_audit
from test_completed_work_review import settled
from test_completed_work_usage import owner, saved_work_rows
from test_inference_telemetry import save
from test_native_work_telemetry import native_call, usage, work
from test_work_call_attribution_report import claim


ORIGIN, FINAL = 'a'*32, 'b'*32


def runtime(**changes):
    value = {'schema': 'tare.tools/work-runtime-observation/1', 'runtime_version': 'runtime/1',
             'requested_model': 'requested', 'protocol': 'responses', 'deployment_id': 'selected', 'effort': 'medium'}
    value.update(changes)
    return value


def receipt(identity=FINAL, **changes):
    value = settled(identity, work_id='W', repository_root='project', task_class='bounded-fix',
        graph_path='C:/fixture/work-graph.json', candidate_commit='c'*40,
        runtime_observation=runtime(), inference_calls=[claim(stage='implementer')])
    value.update(changes)
    return value


def review(saved, **changes):
    audit = saved.get('audit', {}).get('observation', {})
    value = {'schema': evidence.SCHEMA, 'subject': evidence.subject(saved),
             'accepted': True, 'material_rework': False, 'reference': 'Recorded review: local check #1',
             'findings': [{'index': i, 'disposition': 'rejected', 'cause': 'unknown',
                          'correction_reference': None} for i, _ in enumerate(audit.get('findings', []))]}
    value.update(changes)
    return value


def report(tmp_path, receipts, calls):
    paths = []
    for row in receipts:
        path = tmp_path/row['run_id']/'run.json'
        save(path, row); paths.append(path)
    path = tmp_path/'usage.json'; save(path, usage(*calls)); paths.append(path)
    result = telemetry.collect(paths, include_work_metadata=True)
    result['saved_delivery_observations'] = [evidence.capture(r) for r in receipts]
    result['saved_audit_observations'] = [
        {'id': r['run_id'], 'work_id': r['work_id'], 'project': r['repository_root'],
         'source_revision': r['candidate_commit'], 'auditor': 'AUDITOR_ONLY',
         'execution_origin_run_id': r.get('resumed_from') or r['run_id'],
         'observation': r['audit']['observation']}
        for r in receipts if r.get('audit', {}).get('observation')]
    return result


def test_exact_join_keeps_failed_attempts_auditor_separate_cost_and_cache(tmp_path):
    prior = receipt(ORIGIN, status='FAILED', landing=None,
                    audit={'observation': saved_audit()})
    prior['delivery_review'] = review(prior, accepted=False, material_rework=True,
        findings=[{'index': 0, 'disposition': 'corrected', 'cause': 'tooling',
                   'correction_reference': 'fix commit ddddd'}])
    final = receipt(resumed_from=ORIGIN, inference_calls=[claim('session/model/2', 'code_auditor')])
    # A clean last attempt cannot erase the original candidate's corrective work.
    final['delivery_review'] = review(final, material_rework=False)
    data = report(tmp_path, [prior, final], [native_call(), native_call('session/model/2',
        observation={'requested_model': 'auditor', 'observed_model': 'auditor-observed', 'status': 'complete'})])
    before = deepcopy(data)
    projected = evidence.project(data)
    assert data == before
    row, = projected['deliveries']
    assert row['attempt_count'] == 2
    assert row['accepted_without_material_rework'] is False
    assert row['confirmed_findings_by_cause']['tooling'] == 1
    assert row['confirmed_findings_by_cause']['implementation'] == 0
    assert row['usage']['call_intents'] == 2 and row['usage']['recorded_model_cost_usd'] == '0.50'
    assert row['usage']['cache_read_tokens'] == {'recorded_sum': 160, 'unknown_calls': 0}
    assert row['usage']['full_cost_usd'] is None
    group, = projected['groups']
    assert group['scope']['observed_model'] == 'observed'
    assert 'auditor' not in json.dumps(group)
    assert group['accepted_without_material_rework'] == {'yes': 0, 'no': 1, 'unknown': 0}
    assert group['recorded_use']['recorded_model_cost_usd'] == '0.50'
    assert group['recorded_use']['cache_read_tokens']['recorded_sum'] == 160
    assert projected['model_preference'] is None


def test_administrative_closure_and_unpriced_usage_stay_unknown(tmp_path):
    saved = receipt(runtime_observation=runtime(effort=None), audit={'observation': {
        **saved_audit(), 'verdict': 'ACCEPT', 'findings': []}})
    data = report(tmp_path, [saved], [native_call(cost={}, normalized={'tokens': {'cache_read_tokens': 0}})])
    projected = evidence.project(data)
    row, = projected['deliveries']
    assert row['accepted_without_material_rework'] is None
    assert row['usage']['unpriced_calls'] == 1
    assert row['usage']['recorded_model_cost_usd'] is None
    assert row['usage']['cache_read_tokens'] == {'recorded_sum': 0, 'unknown_calls': 0}
    assert row['usage']['input_tokens'] == {'recorded_sum': None, 'unknown_calls': 1}
    assert projected['groups'] == [] and 'effort_missing_or_mixed' in row['comparison_gaps']


def test_runtime_changes_and_unknown_prior_rework_do_not_become_clean_delivery(tmp_path):
    prior = receipt(ORIGIN, status='FAILED', landing=None)
    final = receipt(resumed_from=ORIGIN, runtime_observation=runtime(runtime_version='runtime/2'),
                    inference_calls=[claim('session/model/2', 'implementer')])
    final['delivery_review'] = review(final)
    result = evidence.project(report(tmp_path, [prior, final], [native_call(), native_call('session/model/2')]))
    row, = result['deliveries']
    assert row['accepted_without_material_rework'] is None
    assert 'runtime_version_missing_or_mixed' in row['comparison_gaps']
    assert result['groups'] == []


def test_controller_metadata_in_direct_usage_is_separate_and_whitelisted():
    from relay.console_workbench import usage_rows
    body = usage_rows({'model_calls': 1, 'last_runtime_observation': {
        **runtime(), 'endpoint': 'SECRET', 'api_key': 'SECRET'}})[0]['body']
    assert 'direct session; separate from completed Works' in body
    assert 'runtime_version: runtime/1' in body and 'effort: medium' in body
    assert 'SECRET' not in body


@pytest.mark.parametrize('field', ['candidate_commit', 'repository_root', 'work_id'])
def test_wrong_delivery_subject_is_not_attached_or_reviewable(tmp_path, field):
    data = report(tmp_path, [receipt()], [native_call()])
    data['saved_delivery_observations'][0][field] = 'unrelated'
    result = evidence.project(data)
    assert result['review_targets'] == [] and result['invalid_record_count'] == 1
    assert result['groups'] == []


def test_conflicts_cross_revision_audits_stale_reviews_and_duplicates(tmp_path):
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved)
    data = report(tmp_path, [saved], [native_call()])
    data['saved_delivery_observations'] *= 2
    assert len(evidence.project(data)['deliveries']) == 1
    changed = deepcopy(data['saved_delivery_observations'][0]); changed['runtime']['effort'] = 'high'
    data['saved_delivery_observations'].append(changed)
    assert evidence.project(data)['conflicting_ids'] == [FINAL]
    assert evidence.project(data)['groups'] == []
    data['saved_delivery_observations'] = [evidence.capture(saved)]
    data['saved_audit_observations'][0]['source_revision'] = 'wrong'
    result = evidence.project(data)
    attempt, = result['deliveries'][0]['attempts']
    assert attempt['audit'] is None and attempt['review'] is None
    assert result['invalid_record_count'] >= 1


def test_existing_audits_remain_independent_from_bad_optional_delivery_input(tmp_path):
    saved = receipt(audit={'observation': saved_audit()})
    data = report(tmp_path, [saved], [native_call()])
    data['saved_delivery_observations'] = 'invalid'
    result = behavioral_profiles.behavioral_profile_review(data)
    assert result['status'] == result['work_audits']['status'] == 'available'
    assert result['delivery_evidence']['status'] == 'unavailable'


def test_reader_reuses_fixed_owner_slots_without_new_writes_or_citation_reads(tmp_path):
    control, custody = owner(tmp_path)
    saved = receipt(audit={'deployment': 'auditor-only', 'observation': saved_audit(),
                           'result': {'artifact_file': '/never-open'}})
    save(custody/FINAL/'run.json', saved)
    save(custody/FINAL/'implement/scratch/usage.json', usage(native_call()))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows = saved_work_rows(control, include_behavioral_profiles=True)
    profile = rows[-1]
    assert len(profile['review_targets']) == 1
    assert 'Linked completed executions: 1; comparison groups: 1' in profile['body']
    assert 'never-open' not in profile['body']
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_empty_owner_also_exposes_delivery_view_without_review_actions(tmp_path):
    control, _ = owner(tmp_path)
    row, = saved_work_rows(control, include_behavioral_profiles=True)
    assert 'Linked completed executions: 0; comparison groups: 0' in row['body']
    assert row['review_targets'] == []


def test_explicit_review_uses_owner_lock_compare_and_swap_and_preserves_receipt(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    saved = receipt(audit={'observation': saved_audit()})
    path = custody/FINAL/'run.json'; save(path, saved)
    value = review(saved)
    captured = evidence.record_review(control, FINAL, evidence.digest(saved), value)
    after = json.loads(path.read_bytes())
    assert {k: v for k, v in after.items() if k != 'delivery_review'} == saved
    assert after['delivery_review']['recorded_by'] == 'operator_tui'
    assert captured['review']['subject']['candidate_commit'] == 'c'*40
    with pytest.raises(ValueError, match='changed'):
        evidence.record_review(control, FINAL, evidence.digest(saved), value)
    with pytest.raises(ValueError, match='identity'):
        evidence.record_review(control, '../escape', evidence.digest(after), value)
    before = path.read_bytes()
    from compute_plane.native_journal import custody_lock
    with custody_lock(custody/'locks'/'W'):
        with pytest.raises(Exception):
            evidence.record_review(control, FINAL, evidence.digest(after), value)
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['no_reference', 'wrong_subject', 'wrong_findings', 'correction', 'contradiction'])
def test_bad_review_has_no_effect(tmp_path, monkeypatch, change):
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    saved = receipt(audit={'observation': saved_audit()})
    path = custody/FINAL/'run.json'; save(path, saved)
    draft = review(saved)
    if change == 'no_reference': draft['reference'] = ''
    if change == 'wrong_subject': draft['subject']['candidate_commit'] = 'wrong'
    if change == 'wrong_findings': draft['findings'] = []
    if change == 'correction': draft['findings'][0]['disposition'] = 'corrected'
    if change == 'contradiction': draft['findings'][0]['disposition'] = 'confirmed'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        evidence.record_review(control, FINAL, evidence.digest(saved), draft)
    assert path.read_bytes() == before


@pytest.mark.parametrize('finding_count', [1, 2])
def test_public_usage_optional_review_saves_and_refreshes_without_launch(tmp_path, monkeypatch, finding_count):
    from relay.native_console import WorkConsole
    from relay.console_workbench import WorkbenchPanel
    from test_native_console import FakeBackend
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    monkeypatch.setattr('relay.flow_usage.completed_work_usage_rows', saved_work_rows)
    monkeypatch.setattr('relay.flow_usage.flow_usage_rows', lambda *a, **kw: [])
    saved = receipt(audit={'observation': saved_audit()})
    if finding_count == 2:
        second = deepcopy(saved['audit']['observation']['findings'][0])
        second['claim'] = 'Second concrete claim'
        saved['audit']['observation']['findings'].append(second)
    path = custody/FINAL/'run.json'; save(path, saved)
    save(custody/FINAL/'implement/scratch/usage.json', usage(native_call()))

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_usage(); await pilot.pause()
            panel = app.screen
            index = next(i for i, r in enumerate(panel.rows) if r['id'] == 'behavioral-profiles')
            panel.show_row(index); await pilot.pause()
            await pilot.click('#workbench-review'); await pilot.pause()
            assert isinstance(app.screen, DeliveryReviewPanel)
            app.screen.query_one('#review-accepted', Select).value = 'yes'
            app.screen.query_one('#review-rework', Select).value = 'no'
            app.screen.query_one('#review-disposition', Select).value = 'rejected'
            app.screen.query_one('#review-cause', Select).value = 'guidance'
            app.screen.query_one('#review-correction', Input).value = 'First reference'
            app.screen.query_one('#review-reference', Input).value = 'Reviewed original claim against test proof'
            await pilot.pause()
            if finding_count == 2:
                app.screen.query_one('#review-finding', Select).value = 1
                await pilot.pause()
                assert app.screen.finding_index == 1
                assert app.screen.review['findings'][0]['correction_reference'] == 'First reference'
                app.screen.query_one('#review-disposition', Select).value = 'rejected'
                app.screen.query_one('#review-cause', Select).value = 'tooling'
                app.screen.query_one('#review-correction', Input).value = 'Second reference'
                await pilot.pause()
                assert app.screen.review['findings'][0]['correction_reference'] == 'First reference'
                app.screen.query_one('#review-finding', Select).value = 0
                await pilot.pause()
                assert app.screen.finding_index == 0
                assert app.screen.query_one('#review-correction', Input).value == 'First reference'
                assert app.screen.query_one('#review-cause', Select).value == 'guidance'
            await pilot.click('#review-save'); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            assert 'Accepted without material rework: Yes' in app.screen.current['body']
            assert json.loads(path.read_bytes())['delivery_review']['findings'][0]['disposition'] == 'rejected'
            if finding_count == 2:
                assert json.loads(path.read_bytes())['delivery_review']['findings'][1]['correction_reference'] == 'Second reference'
            assert not backend.launches
    asyncio.run(scenario())


def test_optional_review_without_audit_can_be_cancelled_without_writing(tmp_path, monkeypatch):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr('relay.flow_usage.completed_work_usage_rows', saved_work_rows)
    monkeypatch.setattr('relay.flow_usage.flow_usage_rows', lambda *a, **kw: [])
    path = custody/FINAL/'run.json'; save(path, receipt())
    before = path.read_bytes()

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_usage(); await pilot.pause()
            panel = app.screen
            panel.show_row(next(i for i, r in enumerate(panel.rows) if r['id'] == 'behavioral-profiles'))
            await pilot.pause(); await pilot.click('#workbench-review'); await pilot.pause()
            assert isinstance(app.screen, DeliveryReviewPanel)
            assert app.screen.query_one('#review-finding', Select).value is Select.NULL
            assert app.screen.query_one('#review-disposition', Select).disabled
            await pilot.press('escape'); await pilot.pause()
            assert path.read_bytes() == before and not backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('metadata', [None, False, {}, {'reason': ''}, {'reason': 'mistaken assertion'},
                                      {'reason': 'stale', 'subject': {'candidate_commit': 'wrong'}}])
def test_any_withdrawal_metadata_excludes_quality_but_keeps_audit_and_usage(tmp_path, metadata):
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved, findings=[{
        'index': 0, 'disposition': 'corrected', 'cause': 'tooling', 'correction_reference': 'fix proof'}])
    before = evidence.project(report(tmp_path, [saved], [native_call()]))
    saved['delivery_review']['withdrawal'] = metadata
    after = evidence.project(report(tmp_path, [saved], [native_call()]))
    old, = before['deliveries']
    row, = after['deliveries']
    assert old['accepted_without_material_rework'] is True
    assert old['confirmed_findings_by_cause']['tooling'] == 1
    assert row['accepted_without_material_rework'] is None
    assert sum(row['confirmed_findings_by_cause'].values()) == 0
    assert row['unresolved_findings'] == 1
    assert row['usage'] == old['usage'] and row['attempt_count'] == old['attempt_count']
    assert row['attempts'][0]['audit'] == old['attempts'][0]['audit']
    assert 'withdrawal' in row['attempts'][0]['review']
    assert after['groups'][0]['accepted_without_material_rework'] == {'yes': 0, 'no': 0, 'unknown': 1}
    assert 'Review withdrawn; excluded from quality counts.' in evidence.render(after)


def test_withdrawal_projection_does_not_copy_unbounded_unknown_payload(tmp_path):
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved, withdrawal={
        'reason': 'Wrong assessment', 'recorded_by': 'operator_tui',
        'unexpected_payload': 'PRIVATE-UNBOUNDED-' * 2000})
    source = deepcopy(saved)
    result = evidence.project(report(tmp_path, [saved], [native_call()]))
    row, = result['deliveries']
    projected = row['attempts'][0]['review']['withdrawal']
    assert row['accepted_without_material_rework'] is None
    assert projected['reason'] == 'Wrong assessment'
    assert 'PRIVATE-UNBOUNDED-' not in json.dumps(result)
    assert len(json.dumps(projected)) < 2048
    assert saved == source


def test_withdraw_owner_receipt_and_explicit_save_reactivation(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved, accepted=None, extra_unknown={'future': None})
    path = custody/FINAL/'run.json'; save(path, saved)
    captured = evidence.withdraw_review(control, FINAL, evidence.digest(saved), 'Wrong candidate assessment')
    after = json.loads(path.read_bytes())
    assert {k: v for k, v in after.items() if k != 'delivery_review'} == {
        k: v for k, v in saved.items() if k != 'delivery_review'}
    assert {k: v for k, v in after['delivery_review'].items() if k not in ('withdrawal', 'schema')} == {
        k: v for k, v in saved['delivery_review'].items() if k != 'schema'}
    assert after['delivery_review']['schema'] == evidence.WITHDRAWN_SCHEMA != evidence.SCHEMA
    assert after['delivery_review']['withdrawal']['original_schema'] == saved['delivery_review']['schema']
    row, = evidence.project(report(tmp_path/'projection', [after], [native_call()]))['deliveries']
    assert row['accepted_without_material_rework'] is None and row['unresolved_findings'] == 1
    with pytest.raises(ValueError, match='already excluded'):
        evidence.withdraw_review(control, FINAL, captured['token'], 'again')
    evidence.record_review(control, FINAL, captured['token'], review(after))
    active = json.loads(path.read_bytes())
    assert 'withdrawal' not in active['delivery_review']
    row, = evidence.project(report(tmp_path/'active', [active], [native_call()]))['deliveries']
    assert row['accepted_without_material_rework'] is True


def test_withdrawn_schema_without_metadata_cannot_become_active(tmp_path):
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved, schema=evidence.WITHDRAWN_SCHEMA)
    result = evidence.project(report(tmp_path, [saved], [native_call()]))
    row, = result['deliveries']
    assert row['accepted_without_material_rework'] is None
    assert row['attempts'][0]['review'] is None
    assert row['unresolved_findings'] == 1
    assert result['invalid_record_count'] == 1


@pytest.mark.parametrize('conflict', ['token', 'candidate', 'audit', 'subject', 'busy', 'reason', 'missing', 'oversize'])
def test_withdraw_fails_closed(tmp_path, monkeypatch, conflict):
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved)
    token = evidence.digest(saved)
    if conflict == 'token': saved['source_extra'] = 'changed'
    if conflict == 'candidate': saved['candidate_commit'] = 'd'*40
    if conflict == 'audit': saved['audit']['observation']['artifact_sha256'] = 'e'*64
    if conflict == 'subject': saved['delivery_review']['subject']['candidate_commit'] = 'd'*40
    if conflict == 'missing': saved.pop('delivery_review')
    if conflict == 'oversize': saved['delivery_review']['extra'] = 'x'*(16*1024)
    if conflict in ('subject', 'missing', 'oversize'): token = evidence.digest(saved)
    path = custody/FINAL/'run.json'; save(path, saved)
    before = path.read_bytes()
    from contextlib import nullcontext
    from compute_plane.native_journal import custody_lock
    lock = custody/'locks'/'W'; lock.mkdir(parents=True, exist_ok=True)
    with custody_lock(lock) if conflict == 'busy' else nullcontext():
        with pytest.raises((ValueError, OSError)):
            evidence.withdraw_review(control, FINAL, token, '' if conflict == 'reason' else 'Mistake')
    assert path.read_bytes() == before
