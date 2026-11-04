"""Whole delivery accounting, recovery duplicates and user-visible unknowns."""
import asyncio
import json

import pytest
from textual.widgets import Button, Input, TextArea

from relay.audit_delivery import report, audit_rounds


def receipt(store, run, *, work='task', project='project', status='FAILED', descriptor='d', seconds=12):
    path = store/run/'run.json'
    path.parent.mkdir(parents=True)
    value = {'schema': 'tare.tools/work-run/1', 'run_id': run, 'work_id': work,
             'repository_root': project, 'status': status, 'duration_ms': seconds*1000,
             'started_at': '2026-09-24T00:00:00+00:00', 'landing': {'landed': status == 'SETTLED'},
             'stages': [{'stage': 'descriptor', 'at': '2026-09-24T00:00:01+00:00', 'sha256': descriptor},
                        {'stage': 'plan_audit', 'at': '2026-09-24T00:00:11+00:00', 'verdict': 'ACCEPT'}]}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_failed_attempts_recovery_and_project_identity_are_preserved(tmp_path):
    for run, status in [('a', 'FAILED'), ('b', 'SETTLED')]:
        receipt(tmp_path, run, status=status)
        p = tmp_path/run/'plan-audit'/'session.json'; p.parent.mkdir()
        p.write_text(json.dumps({'id': run, 'calls': 1, 'tokens': {'input': 10, 'output': 1}}))
    receipt(tmp_path, 'c', status='SETTLED')  # landing recovery is not a second delivery
    receipt(tmp_path, 'd', project='other', descriptor='other')
    first = report(tmp_path)
    assert first['settled_count'] == 1 and first['delivery_count'] == 2
    delivered = next(r for r in first['deliveries'] if r['settled'])
    assert delivered['attempt_count'] == 3 and delivered['seconds'] == 36
    assert delivered['audit_seconds'] == 30 and delivered['repeated_accepted_plans'] == 1
    assert delivered['audit_round_count'] == 2 and delivered['audit_stage_visits'] == 3
    assert first['false_blocks_adjudicated'] is None and first['authority'] == 'NONE'
    assert not first['cohort_review_due']
    # A copied receipt cannot count as another dispatch or delivery.
    duplicate = tmp_path/'copied'/'run.json'; duplicate.parent.mkdir()
    duplicate.write_bytes((tmp_path/'b'/'run.json').read_bytes())
    assert report(tmp_path)['deliveries'] == first['deliveries']


def test_legacy_helpers_and_missing_audit_stage_keep_usage_and_unknown_time(tmp_path):
    p = receipt(tmp_path, 'a')
    d = json.loads(p.read_text()); d['stages'] = d['stages'][:1]; p.write_text(json.dumps(d))
    session = p.parent/'plan-audit'/'session.json'; session.parent.mkdir()
    session.write_text(json.dumps({'id': 'audit', 'calls': 2, 'tokens': {'input': 100, 'output': 10},
        'estimated_cost_usd': '0.01', 'delegation': {'calls': 1, 'input_tokens': 20, 'output_tokens': 4}}))
    row = report(tmp_path)['deliveries'][0]
    assert row['audit_round_count'] == 1 and row['audit_seconds'] is None
    assert row['unmeasured_review_intervals'] == 1
    legacy = row['usage']['legacy'][0]
    assert (legacy['calls'], legacy['input_tokens'], legacy['output_tokens']) == (3, 120, 14)
    assert legacy['cost_estimate_usd'] is None  # helper cost is unavailable


def test_clock_regression_and_two_code_auditors_use_distinct_intervals():
    events = [{'stage': 'code_audit', 'session': 'a', 'at': '2026-09-24T00:00:10+00:00'},
              {'stage': 'code_audit_result', 'status': 'FAILED', 'at': '2026-09-24T00:00:09+00:00'},
              {'stage': 'audit_fallback', 'at': '2026-09-24T00:00:20+00:00'},
              {'stage': 'code_audit', 'session': 'b', 'at': '2026-09-24T00:00:30+00:00'},
              {'stage': 'code_audit_result', 'status': 'PROPOSED', 'at': '2026-09-24T00:00:35+00:00'},
              {'stage': 'code_audit_accepted', 'at': '2026-09-24T00:00:40+00:00'}]
    rounds = audit_rounds({'stages': events})
    assert len(rounds) == 2 and [r['seconds'] for r in rounds] == [None, 5]
    assert rounds[0]['verdict'] == 'FAILED'


def test_native_cumulative_usage_and_helpers_count_once_without_extra_review(tmp_path, monkeypatch):
    from test_execution_usage_sources import direct
    from relay import delegation
    receipt(tmp_path, 'first')
    receipt(tmp_path, 'recovery', status='SETTLED')
    a, b, child = tmp_path/'first'/'plan-audit', tmp_path/'recovery'/'plan-audit', tmp_path/'child'
    direct(a, 'review'); direct(b, 'review'); direct(child, 'helper')
    monkeypatch.setattr(delegation, 'children', lambda root: [child])
    row = report(tmp_path)['deliveries'][0]
    assert row['usage']['native']['generation_intents'] == 2
    assert row['usage']['native']['unpriced_or_partial_calls'] == 2
    assert row['audit_round_count'] == 1
    assert row['repeated_accepted_plans'] == 0


def test_bad_receipt_is_visible_and_ten_work_review_stays_due(tmp_path):
    for n in range(11):
        receipt(tmp_path, str(n), work=str(n), status='SETTLED')
    p = tmp_path/'bad'/'run.json'; p.parent.mkdir(); p.write_text('{}')
    value = report(tmp_path)
    assert len(value['issues']) == 1 and value['cohort_review_due']
    assert value['next_review_at_settled_count'] == 10


def test_prepared_implementation_usage_is_counted_once_across_recovery(tmp_path):
    from test_execution_usage_sources import direct
    source = tmp_path/'prepared'/'session'
    direct(source, 'parallel-implementation')
    for run in ('first', 'recovered'):
        path = receipt(tmp_path/'runs', run, status='SETTLED')
        value = json.loads(path.read_text())
        value['prepared_implementation'] = {'path': str(source.parent), 'session': str(source), 'sha256': 'source'}
        value['preparation_sessions'] = [str(source)]
        path.write_text(json.dumps(value))
    row = report(tmp_path/'runs')['deliveries'][0]
    assert row['usage']['native']['generation_intents'] == 1
    assert row['usage']['native']['unpriced_or_partial_calls'] == 1
    assert row['audit_round_count'] == 0


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_audit_history_refresh_and_invalid_path(tmp_path, monkeypatch, size):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    store = tmp_path/'state'/'work-runs'
    receipt(store, 'first', status='SETTLED')
    before = (store/'first'/'run.json').read_bytes()

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.dispatch_conversation_command('/delivery', '')
            await pilot.pause()
            await pilot.click('#delivery-audits')
            await pilot.pause()
            assert '1 recorded deliveries' in app.screen.query_one('#delivery-result', TextArea).text
            assert app.screen.query_one('#delivery-save', Button).disabled
            receipt(store, 'second', work='other', status='SETTLED')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            await pilot.click('#delivery-audits'); await pilot.pause()
            assert '2 recorded deliveries' in app.screen.query_one('#delivery-result', TextArea).text
            app.screen.query_one('#delivery-study', Input).value = str(tmp_path/'missing')
            await pilot.pause(.4)
            await pilot.click('#delivery-audits'); await pilot.pause()
            assert 'No current comparison' in app.screen.query_one('#delivery-result', TextArea).text
            await pilot.press('escape')
    asyncio.run(scenario())
    assert (store/'first'/'run.json').read_bytes() == before
