import json
from pathlib import Path

import pytest

from relay.execution_usage_sources import execution_usage, session_entry
from relay.native_profiles import main


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def direct(root, identity):
    write(root/'native-agent.json', {'session_id': identity})
    write(root/'outcome.json', {'status': 'PROPOSED', 'model_calls': 1})
    write(root/'usage.json', {'schema': 'tare-native-usage/1', 'session_id': identity,
        'calls': [{'call_id': identity+'/model/1', 'purpose': 'model',
                   'execution': 'possibly_executed'}]})


def test_cli_preserves_single_session_and_aggregates_repeated_paths(tmp_path, capsys):
    direct(tmp_path, 'one')
    assert main(['usage', '--session', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['model_calls'] == 1
    assert main(['usage', '--session', str(tmp_path), '--session', str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['generation_intents'] == 1
    assert report['unpriced_or_partial_calls'] == 1
    assert report['acceptance'] == 'unknown'
    assert report['stages'][0]['tokens']['input_tokens']['unknown_calls'] == 1


def test_work_reports_prior_plan_attempt_and_implementation_without_inference(tmp_path):
    from compute_plane.native_journal import NativeJournal
    old, plan, impl, train = [tmp_path/name for name in ('old-plan', 'plan', 'implement', 'train')]
    direct(old, 'old'); direct(plan, 'new')
    write(impl/'host.json', {'session_id': 'impl', 'deployment': {}})
    write(impl/'attempt.json', {'identity': {'session_id': 'impl'}, 'attempts': [
        {'outcome': {'outcome': {'status': 'proposed', 'reason': 'DELIVERY_PROPOSED'}, 'observation_failures': 0}}]})
    journal = NativeJournal(impl/'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'impl', 'binding': {}})
    journal.append({'kind': 'model_requested', 'call_id': 'impl/model/1'})
    journal.append({'kind': 'model_failed', 'call_id': 'impl/model/1', 'retryable': True})
    journal.append({'kind': 'model_requested', 'call_id': 'impl/model/2'})
    journal.append({'kind': 'invocation_not_started', 'invocation_id': 'impl/model/2'})
    write(train/'WORK_RUN.json', {'work_id': 'work', 'plan_custody': str(plan),
        'plan_audit_attempts': [str(old)], 'implementation_custody': str(impl),
        'audit': {'routes': [{'eligible': True}]}})
    result = execution_usage(train=train)
    assert result['generation_intents'] == 4
    assert result['retry_intents'] == 1
    assert result['not_started'] == 1
    assert len(result['stages']) == 3
    assert result['unrecorded_stages'][0]['stage'] == 'audit'
    assert result['execution_path'] == 'managed_work'
    assert result['acceptance'] == 'unknown'


def test_mismatched_session_and_busy_custody_fail(tmp_path):
    from compute_plane.native_journal import custody_lock
    direct(tmp_path, 'one')
    with custody_lock(tmp_path), pytest.raises(ValueError):
        session_entry(tmp_path)
    write(tmp_path/'usage.json', {'schema': 'tare-native-usage/1', 'session_id': 'other', 'calls': []})
    with pytest.raises(ValueError, match='session mismatch'):
        execution_usage(sessions=[tmp_path])


def test_missing_report_is_unknown_not_free(tmp_path):
    direct(tmp_path, 'one')
    (tmp_path/'usage.json').unlink()
    result = execution_usage(sessions=[tmp_path])
    assert result['unknown_session_ids'] == ['one']
    assert result['known_cost_by_currency'] == {}


def test_attempt_dispatch_ranges_do_not_repeat_cumulative_usage(tmp_path):
    from compute_plane.native_journal import NativeJournal
    direct(tmp_path, 'one')
    journal = NativeJournal(tmp_path/'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'one', 'binding': {}})
    journal.append({'kind': 'model_requested', 'call_id': 'one/model/1'})
    journal.append({'kind': 'model_failed', 'call_id': 'one/model/1', 'retryable': False})
    write(tmp_path/'native-agent.json', {'session_id': 'one', 'invocations': [
        {'id': 'first', 'journal_start': 0, 'journal_end': 3, 'status': 'BLOCKED'},
        {'id': 'inspection', 'journal_start': 3, 'journal_end': 3, 'status': 'BLOCKED'}]})
    report = execution_usage(sessions=[tmp_path])
    attempts = report['recorded_sessions'][0]['attempts']
    assert [a['dispatch_usage']['generation_intents'] for a in attempts] == [1, 0]
    assert report['generation_intents'] == 1
