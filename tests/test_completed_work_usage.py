"""Offline optional /usage Work projection with explicit temporary owner custody."""
import asyncio
import json
from pathlib import Path

import pytest

from relay import flow_usage
from relay.flow_usage import completed_work_usage_rows as saved_work_rows
from relay.console_workbench import WorkbenchPanel
from relay.native_console import WorkConsole
from scripts.round_table import report_round_table_telemetry as telemetry
from test_completed_work_review import settled
from test_inference_telemetry import save
from test_native_console import FakeBackend
from test_work_call_attribution_report import claim


def owner(tmp_path):
    control = tmp_path/'control'; control.mkdir()
    return control, tmp_path/'.reconciliation'/'work-runs'


def test_partial_unknown_resume_scope_and_read_only(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    for day in range(1, 7):
        save(custody/str(day)/'run.json', settled('root-'+str(day), day,
            work_id='work-'+str(day), repository_root='C:/side-projects/kanban',
            inference_calls=[claim()] if day == 1 else []))
    save(custody/'resume/run.json', settled('resume', 7, resumed_from='root-1', work_id='work-1'))
    save(custody/'resume-copy/run.json', settled('resume', 7, resumed_from='root-1', work_id='work-1'))
    # Adjacent experiments and nested session receipts must never be discovered.
    save(custody.parent/'experiment/run.json', settled('outside', 8))
    save(custody/'1/session/run.json', settled('nested', 9))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def no_write(*args, **kwargs):
        pytest.fail('review attempted a receipt write')
    monkeypatch.setattr(Path, 'write_text', no_write)
    monkeypatch.setattr(Path, 'write_bytes', no_write)
    row, = saved_work_rows(control)
    assert 'partial' in row['label']
    body = row['body']
    for text in ('6/10', 'Remaining: 4', 'excluded records: 0', 'can span projects',
                 'C:/side-projects/kanban', 'Attempts: 2', 'SETTLED: 2',
                 'Attributed calls: 1', 'coverage: partial', 'Reported USD: Unknown',
                 'full cost: Unknown', 'independent audit: Unknown'):
        assert text in body
    assert 'outside' not in body and 'nested' not in body
    assert all('Work work-'+str(day) in body for day in range(1, 7))
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault', ['receipt', 'projection', 'non_object', 'wrong_schema'])
def test_bad_optional_view_is_unavailable(tmp_path, monkeypatch, fault):
    control, custody = owner(tmp_path)
    if fault != 'projection':
        path = custody/'bad/run.json'; path.parent.mkdir(parents=True)
        invalid = {'receipt': '{bad', 'non_object': '[]', 'wrong_schema': '{}'}
        path.write_text(invalid[fault], encoding='utf-8')
    else:
        save(custody/'done/run.json', settled('done'))
        def fail(*args, **kwargs):
            raise ValueError('PRIVATE diagnostic')
        monkeypatch.setattr(telemetry, 'completed_work_review', fail)
    row, = saved_work_rows(control)
    assert 'unavailable' in row['label']
    assert 'PRIVATE' not in json.dumps(row)


def test_usage_without_saved_session_keeps_flow_rows_and_never_launches(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    save(custody/'done/run.json', settled('done'))
    monkeypatch.setattr('relay.relay_mesh.RELAY_ROOT', control)
    seen = []
    def view(selected_control, *, include_provider_pilot=False, include_behavioral_profiles=False, baseline=None):
        assert Path(selected_control) == control
        assert include_provider_pilot is True
        assert include_behavioral_profiles is True
        seen.append(selected_control)
        return saved_work_rows(selected_control, include_provider_pilot=include_provider_pilot,
                               include_behavioral_profiles=include_behavioral_profiles, baseline=baseline)
    monkeypatch.setattr(flow_usage, 'completed_work_usage_rows', view)
    monkeypatch.setattr(flow_usage, 'flow_usage_rows', lambda *a, **k: [
        {'id': 'flow-fixture', 'label': 'Flow selection', 'body': 'Recorded flow usage'}])
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            assert not app.assistant_session
            app.action_usage(); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            assert [r['id'] for r in app.screen.rows] == [  # the provider readings come first
                'quota', 'flow-fixture', 'completed-work-review', 'provider-pilot-review', 'behavioral-profiles']
            assert '1/10' in app.screen.rows[-3]['body']
            assert '1/5' in app.screen.rows[-2]['body']
            assert 'Completed Work executions: 1/10' in app.screen.rows[-1]['body']
            assert not app.assistant_session and not backend.launches
            await pilot.press('escape'); await pilot.pause()
            app.controls(True)
            seen.clear()
            app.action_usage(); await pilot.pause()
            assert not seen
            assert [r['id'] for r in app.screen.rows] == ['quota', 'unavailable']
            assert not backend.launches
            app.controls(False)
    asyncio.run(scenario())


def test_optional_failure_keeps_other_usage_rows(tmp_path, monkeypatch):
    control, custody = owner(tmp_path)
    path = custody/'bad/run.json'; path.parent.mkdir(parents=True)
    path.write_text('invalid', encoding='utf-8')
    monkeypatch.setattr('relay.relay_mesh.RELAY_ROOT', control)
    monkeypatch.setattr(flow_usage, 'completed_work_usage_rows', saved_work_rows)
    monkeypatch.setattr(flow_usage, 'flow_usage_rows', lambda *a, **k: [])
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.action_usage(); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            assert [r['id'] for r in app.screen.rows[:2]] == ['quota', 'usage']
            work_row = next(r for r in app.screen.rows if r['id'] == 'completed-work-review')
            assert 'unavailable' in work_row['label']
            audit_row = app.screen.rows[-1]
            assert audit_row['id'] == 'behavioral-profiles'
            assert 'controlled_replay' in audit_row['body']
            assert 'confirmed_corrected' in audit_row['body']
            assert not backend.launches
    asyncio.run(scenario())


def test_complete_cohort_and_known_model_cost_summary(tmp_path, monkeypatch):
    from test_native_work_telemetry import native_call, usage
    control, custody = owner(tmp_path)
    for day in range(1, 11):
        save(custody/str(day)/'run.json', settled('root-'+str(day), day,
            inference_calls=[claim()] if day == 1 else []))
    # An explicit offline projection exercises rendering independently of discovery.
    usage_path = tmp_path/'fixture-usage.json'
    save(usage_path, usage(native_call()))
    report = telemetry.collect([custody/str(day)/'run.json' for day in range(1, 11)] + [usage_path])
    record, = report['rows']
    record['reported_cost_usd'] = '0.10'
    record['estimated_cost_usd'] = '0.90'
    def collect_explicit(sources, *, include_work_metadata=False):
        assert include_work_metadata is True
        assert set(sources) == {custody/str(day)/'run.json' for day in range(1, 11)}
        return report
    monkeypatch.setattr(telemetry, 'collect', collect_explicit)
    row, = saved_work_rows(control)
    assert row['label'].endswith('complete')
    for text in ('10/10', 'Remaining: 0', 'observed model: observed',
                 'requested model: requested', 'coverage: recorded',
                 'Reported USD: 0.10', 'additional estimated USD (not billed): Unknown',
                 'full cost: Unknown'):
        assert text in row['body']


@pytest.mark.parametrize('exists', [False, True])
def test_empty_ordinary_store_preserves_existing_flow_rows(tmp_path, exists):
    control, custody = owner(tmp_path)
    if exists:
        custody.mkdir(parents=True)
    assert saved_work_rows(control) == []


def test_closed_snapshot_exact_join_and_optional_failure_are_read_only(tmp_path, monkeypatch):
    from test_native_work_telemetry import native_call, usage
    control, custody = owner(tmp_path)
    directory = custody/'done'
    save(directory/'run.json', settled('done', inference_calls=[claim()]))
    attempt = directory/'implement'
    save(attempt/'attempt.json', {'attempts': [
        {'status': 'PROPOSED', 'ended_at': 123.0}]})
    snapshot = attempt/'scratch/usage.json'
    save(snapshot, usage(native_call(), native_call('unowned/model/2')))
    bad = custody/'bad/run.json'
    bad.parent.mkdir(parents=True)
    bad.write_text('{bad', encoding='utf-8')
    forbidden = [attempt/'journal.jsonl', attempt/'native-agent.json',
                 directory/'plan-audit/scratch/usage.json',
                 directory/'workspaces/foreign/usage.json',
                 custody.parent/'experiments/usage.json']
    for path in forbidden:
        save(path, usage(native_call('foreign/model/1')))
    save(directory/'plan-audit/attempt.json', {'attempts': [
        {'status': 'PREPARING', 'ended_at': None}]})
    forbidden.extend([attempt/'attempt.json', directory/'plan-audit/attempt.json'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    original = Path.open
    opened = []
    def guarded(path, *args, **kwargs):
        assert path not in forbidden
        opened.append(path)
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        row, = saved_work_rows(control)
    assert snapshot in opened
    assert '1/10' in row['body']
    assert 'Attributed calls: 1' in row['body']
    assert 'missing usage receipts: 0' in row['body']
    assert 'observed model: observed' in row['body']
    assert 'requested model: requested' in row['body']
    assert 'Optional source warnings: 1' in row['body']
    assert 'full call count: Unknown' in row['body']
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('live', [False, True])
def test_real_reconciled_lineage_native_layout(tmp_path, monkeypatch, live):
    from test_native_work_telemetry import native_call, usage
    control, custody = owner(tmp_path)
    origin = '755c169636f7496f9dae4aefa374c215'
    directory = custody/origin
    implement = [f'session/model/{n}' for n in range(1, 39)]
    audit = [f'session/model/{n}' for n in range(39, 48)]
    save(directory/'run.json', settled(origin,
        status='IMPLEMENTATION_RECONCILIATION_REQUIRED', landing=None,
        inference_calls=[claim(identity) for identity in implement]
                        + [claim(identity, 'plan-audit') for identity in audit]))
    save(custody/'resume/run.json', settled('resume', 2, resumed_from=origin))
    snapshots = [directory/'implement/scratch/usage.json', directory/'plan-audit/usage.json']
    save(snapshots[0], usage(*(native_call(identity) for identity in implement)))
    save(snapshots[1], usage(*(native_call(identity) for identity in audit)))
    if live:
        save(custody/'active/run.json', settled('active', 3, resumed_from=origin,
            status='RUNNING', ended_at=None, landing=None))
    forbidden = [directory/'implement/attempt.json',
                 directory/'implement/native-agent.json',
                 directory/'implement/host.json',
                 directory/'implement/journal.jsonl',
                 directory/'plan-audit/scratch/usage.json',
                 directory/'workspaces/foreign/usage.json',
                 custody.parent/'foreign/usage.json']
    for path in forbidden:
        save(path, usage(native_call('foreign/model/1')))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    original = Path.open
    opened = []
    def guarded(path, *args, **kwargs):
        assert path not in forbidden
        if live:
            assert path not in snapshots
        opened.append(path)
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        row, = saved_work_rows(control)
    body = row['body']
    assert 'IMPLEMENTATION_RECONCILIATION_REQUIRED: 1' in body
    assert 'SETTLED: 1' in body
    assert 'Attributed calls: 47' in body
    if live:
        assert 'missing usage receipts: 47' in body
        assert 'observed model: Unknown' in body
        assert 'Recorded input tokens: Unknown' in body
    else:
        assert all(path in opened for path in snapshots)
        assert 'missing usage receipts: 0' in body
        assert 'observed model: observed' in body
        assert 'requested model: requested' in body
        assert 'Recorded input tokens: 4700' in body
        assert 'recorded output tokens: 940' in body
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
