"""Offline descriptive pilot fixtures; never evidence of real Work qualification."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from relay.flow_usage import completed_work_usage_rows as saved_work_rows
from scripts.round_table import report_round_table_telemetry as telemetry
from test_completed_work_review import settled
from test_completed_work_usage import owner
from test_inference_telemetry import save
from test_native_work_telemetry import native_call, usage, work
from test_work_call_attribution_report import claim


def pilot(tmp_path):
    return telemetry.provider_pilot_review(
        telemetry.collect([tmp_path], include_work_metadata=True))


def assert_unknown(result):
    for key in ('qualification', 'representativeness', 'provider_equivalence',
                'provider', 'code_quality', 'independent_audit',
                'model_preference', 'authorization'):
        assert result[key] is None


def test_six_legacy_settlements_are_not_qualification(tmp_path):
    for day in range(1, 7):
        save(tmp_path/str(day)/'run.json', settled(
            str(day), day, work_id='work-'+str(day), train_id='train'))
    result = pilot(tmp_path)
    assert result['selected_count'] == 5
    assert result['distinct_work_count_in_window'] == 6
    assert_unknown(result)
    for counts in result['metadata_counts'].values():
        assert counts == dict(known_attempts=0, missing_attempts=5, conflicting_works=0)
    assert all(execution['full_cost_usd'] is None
               for item in result['rows'] for execution in item['executions'])


def test_repeat_executions_and_resumes_do_not_fill_five(tmp_path):
    for day in range(1, 7):
        save(tmp_path/str(day)/'run.json', settled(
            'root-'+str(day), day, work_id='same', train_id='train'))
    save(tmp_path/'resume/run.json', settled(
        'resume', 7, resumed_from='root-1', work_id='same', train_id='train'))
    result = pilot(tmp_path)
    assert result['selected_count'] == 1 and result['remaining'] == 4
    item, = result['rows']
    assert len(item['executions']) == 6
    assert sum(len(row['attempts']) for row in item['executions']) == 7
    assert_unknown(result)


def test_five_distinct_works_show_only_recorded_facts(tmp_path):
    for day in range(1, 6):
        save(tmp_path/str(day)/'run.json', settled(
            str(day), day, work_id='work-'+str(day), train_id='train',
            repository_root='fixture-project', task_class='bugfix',
            runtime_identity='explicit-runtime', recorded_audit_outcome='ACCEPT'))
    result = pilot(tmp_path)
    assert result['selected_count'] == 5 and result['remaining'] == 0
    assert_unknown(result)
    assert all(row['runtime_identity'] == 'explicit-runtime' for row in result['rows'])
    assert all(counts == dict(known_attempts=5, missing_attempts=0, conflicting_works=0)
               for counts in result['metadata_counts'].values())
    assert 'not a proven representative selection' in result['notes'][0]


def test_window_is_ten_executions_and_identity_includes_train(tmp_path):
    save(tmp_path/'old/run.json', settled('old', 1, work_id='old', train_id='old'))
    for day in range(2, 12):
        save(tmp_path/str(day)/'run.json', settled(
            str(day), day, work_id='same', train_id='one' if day % 2 else 'two'))
    result = pilot(tmp_path)
    assert result['window_limit'] == result['window_execution_count'] == 10
    assert result['selected_count'] == 2
    assert {item['train_id'] for item in result['rows']} == {'one', 'two'}


def test_conflicting_runtime_verdicts_and_linked_failures_keep_base_evidence(tmp_path):
    save(tmp_path/'origin/run.json', work(
        'origin', work_id='same', train_id='train', runtime_identity='recorded-A',
        recorded_audit_outcome='REVISE', inference_calls=[claim()]))
    save(tmp_path/'resume/run.json', settled(
        'resume', 22, resumed_from='origin', work_id='same', train_id='train',
        runtime_identity='recorded-B', recorded_audit_outcome='ACCEPT'))
    save(tmp_path/'usage.json', usage(native_call(
        observation={'status': 'failed', 'error': 'recorded_failure'})))
    report = telemetry.collect([tmp_path], include_work_metadata=True)
    before = deepcopy(report)
    base = telemetry.completed_work_review(report)
    result = telemetry.provider_pilot_review(report)
    assert report == before
    item, = result['rows']
    assert item['executions'] == base['rows']
    assert item['runtime_identity'] is item['recorded_audit_outcome'] is None
    assert result['metadata_counts']['runtime_identity']['conflicting_works'] == 1
    assert {attempt['status'] for attempt in item['executions'][0]['attempts']} == {'FAILED', 'SETTLED'}
    assert item['executions'][0]['attributed_calls'][0]['status'] == 'ERROR'
    assert_unknown(result)


def test_cli_requires_source_before_reading_defaults(monkeypatch, capsys):
    monkeypatch.setattr(telemetry, 'collect', lambda *a, **k: pytest.fail('unexpected read'))
    with pytest.raises(SystemExit) as error:
        telemetry.main(['--review-provider-pilot'])
    assert error.value.code == 2
    assert '--review-provider-pilot requires explicit --source' in capsys.readouterr().err


def test_cli_opt_in_keeps_default_and_flow05(tmp_path, capsys):
    source, output = tmp_path/'run.json', tmp_path/'report.json'
    save(source, settled('done', work_id='fixture', train_id='train'))
    args = ['--source', str(source), '--json', str(output)]
    assert telemetry.main(args) == 0
    assert 'provider_pilot_review' not in json.loads(output.read_text(encoding='utf-8'))
    assert 'Provider pilot' not in capsys.readouterr().out
    assert telemetry.main(args + ['--review-provider-pilot', '--review-completed-works']) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['completed_work_review']['selected_count'] == 1
    assert report['provider_pilot_review']['selected_count'] == 1
    assert_unknown(report['provider_pilot_review'])
    text = capsys.readouterr().out
    assert 'Provider pilot: 1/5' in text and 'Completed Work review: 1/10' in text


def test_usage_reuses_reads_offline_without_writes(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    directory = custody/'done'
    save(directory/'run.json', settled('done', inference_calls=[claim()]))
    snapshot = directory/'implement/scratch/usage.json'
    save(snapshot, usage(native_call()))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    opened, collections = [], []
    original_open, original_collect = Path.open, telemetry.collect
    def guarded_open(path, mode='r', *args, **kwargs):
        assert not any(flag in mode for flag in ('w', 'a', 'x', '+'))
        opened.append(path)
        return original_open(path, mode, *args, **kwargs)
    def collect(sources, **kwargs):
        collections.append(list(sources))
        return original_collect(sources, **kwargs)
    def forbidden(*args, **kwargs):
        pytest.fail('optional projection attempted a write or network call')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded_open)
        patch.setattr(Path, 'write_text', forbidden)
        patch.setattr(Path, 'write_bytes', forbidden)
        patch.setattr(socket, 'create_connection', forbidden)
        patch.setattr(socket, 'socket', forbidden)
        patch.setattr(telemetry, 'collect', collect)
        legacy = saved_work_rows(control)
        baseline_reads, baseline_collections = opened[:], collections[:]
        opened.clear(); collections.clear()
        rows = saved_work_rows(control, include_provider_pilot=True)
        assert opened == baseline_reads and collections == baseline_collections
        assert rows[0] == legacy[0]
        assert snapshot in opened
        assert rows[1]['label'] == 'Provider pilot'
        assert 'Requested runtime: Unknown' in rows[1]['body']
        assert 'why five comparable Works are representative' in rows[1]['body']
        def failed_projection(*args, **kwargs):
            raise ValueError('optional fixture failure')
        patch.setattr(telemetry, 'provider_pilot_review', failed_projection)
        failed = saved_work_rows(control, include_provider_pilot=True)
        assert failed[0] == legacy[0]
        assert failed[1]['label'] == 'Provider pilot · unavailable'
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
