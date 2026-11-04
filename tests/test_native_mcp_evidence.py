"""MCP evidence uses real native receipts, without asking a model to validate itself."""
import json
import sys

import pytest

pytest.importorskip('mcp')
from compute_plane.native_journal import custody_lock
from relay import native_agent
from relay.native_mcp import Service
from test_native_agent import Client, steps
from test_native_mcp import anyio_backend, setup


@pytest.fixture
def execution(setup, monkeypatch):
    root, _ = setup
    client = Client(steps=[*steps(), ('tare_deliver', {'summary': 'Follow-up complete.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient', lambda *a, **k: client)
    service = Service(root, ['fixture'], write_paths=['a.py'], context=False,
                      verifiers={'unit': [sys.executable, '-B', '-c',
                          "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]})
    return service, root, client


@pytest.mark.anyio
async def test_review_actual_edits_checks_staleness_and_scoped_diff(execution):
    service, root, client = execution
    result = await service.start('edit', 'Change a.py and verify', 'fixture')
    report = service.review('edit')
    assert report['authority'] == 'NONE' and report['acceptance'] == 'unknown'
    evidence = report['report']
    assert evidence['edits'][0]['diff'] is None
    check = evidence['checks'][0]
    assert check['returncode'] == 0 and check['freshness'] == 'OBSERVED_FILES_MATCH'
    effect = evidence['edits'][0]['effect_id']
    diff = service.review('edit', invocation_id=result['invocation_id'], effect_id=effect)
    assert '+value = 2' in diff['report']['edits'][0]['diff']
    assert '-value = 1' in diff['report']['edits'][0]['diff']
    with pytest.raises(ValueError, match='No recorded edit'):
        service.review('edit', effect_id='nonexistent')
    with pytest.raises(ValueError, match='no recorded review boundaries'):
        service.review('edit', invocation_id='nonexistent')
    (root/'a.py').write_bytes(b'value = 3\n')
    stale = service.review('edit')['report']
    assert stale['checks'][0]['freshness'] == 'STALE'
    assert stale['changed_since_observation'] == [{'repository': 'repo', 'path': 'a.py'}]
    assert client.generations == 4


@pytest.mark.anyio
async def test_usage_attribution_repeated_reads_and_unknown_acceptance(execution):
    service, root, client = execution
    await service.start('edit', 'Change a.py and verify', 'fixture')
    await service.resume('edit', 'followup', 'Confirm completion')
    response = service.usage('edit')
    report = response['report']
    assert report['generation_intents'] == 5
    assert report['acceptance'] == response['acceptance'] == 'unknown'
    assert report['known_cost_by_currency'] == {}
    assert report['unpriced_or_partial_calls'] == 5
    attempts = report['recorded_sessions'][0]['attempts']
    assert [a['dispatch_usage']['generation_intents'] for a in attempts] == [4, 1]
    assert service.usage('edit') == response
    service.review('edit')
    assert client.generations == 5


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['review', 'usage'])
async def test_evidence_refuses_changed_scope_and_live_writers(execution, method):
    service, root, client = execution
    await service.start('edit', 'Change a.py and verify', 'fixture')
    readonly = Service(root, ['fixture'], context=False)
    with pytest.raises(ValueError, match='scope/profile changed'):
        getattr(readonly, method)('edit')
    service.active['edit'] = object()
    with pytest.raises(ValueError, match='active_writer'):
        getattr(service, method)('edit')
    service.active.clear()
    with custody_lock(service.path('edit')):
        with pytest.raises(ValueError, match='active_writer'):
            getattr(service, method)('edit')
    service.profiles['fixture'] = dict(service.profiles['fixture'], id='changed')
    with pytest.raises(ValueError, match='scope/profile changed'):
        getattr(service, method)('edit')
    assert client.generations == 4


@pytest.mark.anyio
@pytest.mark.parametrize('method,owner', [
    ('review', 'relay.native_review.review'),
    ('usage', 'relay.execution_usage_sources.execution_usage')])
async def test_oversized_reports_are_explicit_not_silently_incomplete(execution, monkeypatch, method, owner):
    service, root, client = execution
    await service.start('edit', 'Change a.py and verify', 'fixture')
    # Non-ASCII escaping and JSON indentation count toward the transport budget.
    monkeypatch.setattr(owner, lambda *a, **kw: {'large': 'é'*17000})
    report = getattr(service, method)('edit')
    assert report['error'] == 'response_too_large'
    assert 'report' not in report
    assert report['session'] == str(service.path('edit'))
    assert report['acceptance'] == 'unknown'
    assert len(json.dumps(report, indent=2).encode('utf-8')) <= 32768
    assert client.generations == 4


@pytest.mark.anyio
async def test_failed_verifier_is_reported_without_acceptance(execution):
    service, root, client = execution
    service.config['verifiers']['unit']['argv'] = [sys.executable, '-B', '-c', 'raise SystemExit(1)']
    await service.start('edit', 'Change a.py and verify', 'fixture')
    response = service.review('edit')
    assert response['report']['checks'][0]['returncode'] == 1
    assert response['acceptance'] == 'unknown'


@pytest.mark.anyio
async def test_missing_usage_and_corrupt_diff_remain_explicit(execution):
    from compute_plane.native_review import receipt_path
    service, root, client = execution
    await service.start('edit', 'Change a.py and verify', 'fixture')
    path = service.path('edit')
    (path/'usage.json').unlink()
    assert len(service.usage('edit')['report']['unknown_session_ids']) == 1
    effect = service.review('edit')['report']['edits'][0]['effect_id']
    receipt_path(path/'review', effect).write_text('{}', encoding='utf-8')
    edit = service.review('edit', effect_id=effect)['report']['edits'][0]
    assert edit['diff'] is None and 'mismatch' in edit['diff_unavailable']
    assert client.generations == 4
