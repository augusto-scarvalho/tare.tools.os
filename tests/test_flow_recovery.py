import asyncio
from dataclasses import asdict
import json

import pytest
from textual.widgets import Select, TextArea
from compute_plane.native_journal import custody_lock, save_json
from relay import console_demand, console_recovery, native_plan, relay_mesh, work_run
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_native_plan import proposal, pending_review
from test_work_run import os_root, evidence, _git
from test_console_demand import Backend

IDENTITY = 'a'*32


def request(root, mode, owner, identity=IDENTITY):
    selection = asdict(Selection(str(root), mode=mode, **{mode: str(owner)}))
    return {'root': str(root), 'control_root': str(relay_mesh.RELAY_ROOT),
        'events': [{'kind': 'execution_requested', 'selection': selection, 'detail': {'invocation_id': identity}}],
        'snapshot': {'selection': selection, 'entry_history': [], 'entry_questions': [],
            'entry_proposal': None, 'entry_selection': None, 'last_result': None}}


def bind_plan(proposal, monkeypatch):
    directory, graph, value, _ = proposal
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', graph.parent)
    monkeypatch.setattr(relay_mesh, 'WORK_GRAPH_FILE', graph)
    return directory, graph, value['roots']['project']


def test_plan_keeps_each_result_and_recovery_never_reaudits_or_applies(proposal, monkeypatch):
    directory, graph, root = bind_plan(proposal, monkeypatch)
    before = graph.read_bytes()
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **kw: pytest.fail('Accepted PLAN must not resample'))
    first = native_plan.retry_review(directory, invocation_id=IDENTITY)
    native_plan.retry_review(directory, invocation_id='b'*32)
    recovered = console_recovery.observe(request(root, 'plan', directory))
    assert recovered['state'] == 'RECOVERED' and recovered['result'] == first
    assert graph.read_bytes() == before
    with pytest.raises(ValueError, match='ALREADY_RECORDED'):
        native_plan.retry_review(directory, invocation_id=IDENTITY)
    with custody_lock(directory):
        assert console_recovery.observe(request(root, 'plan', directory))['state'] == 'BUSY'


def test_interrupted_plan_attempt_cannot_reuse_id_before_child_custody_exists(proposal, monkeypatch):
    directory, _, root = bind_plan(proposal, monkeypatch)
    pending_review(proposal)
    def crash(*args, **kwargs): raise OSError('died before session creation')
    monkeypatch.setattr('relay.native_agent.run_session', crash)
    with pytest.raises(OSError): native_plan.retry_review(directory, invocation_id=IDENTITY)
    assert not (directory/('review-'+IDENTITY)).exists()
    assert console_recovery.observe(request(root, 'plan', directory))['state'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='ALREADY_RECORDED'):
        native_plan.retry_review(directory, invocation_id=IDENTITY)


@pytest.mark.parametrize('fault', ['identity', 'project', 'graph', 'end', 'corrupt'])
def test_mismatched_plan_receipts_stay_unknown(proposal, monkeypatch, fault):
    directory, graph, root = bind_plan(proposal, monkeypatch)
    result = native_plan.retry_review(directory, invocation_id=IDENTITY)
    path = native_plan.result_path(directory, IDENTITY)
    if fault == 'identity': result['invocation_id'] = 'b'*32
    elif fault == 'project': result['repository_root'] = str(graph.parent)
    elif fault == 'graph': result['graph_path'] = str(graph.parent/'different.json')
    elif fault == 'end': result.pop('invocation_ended_at')
    save_json(path, result)
    if fault == 'corrupt': path.write_text('{', encoding='utf-8')
    assert console_recovery.observe(request(root, 'plan', directory))['state'] == 'UNKNOWN'


def test_work_recovery_uses_exact_run_despite_a_later_failure_and_never_lands_again(os_root):
    control, root = os_root
    def implement(worktree, descriptor):
        (worktree/'src/value.txt').write_text('strict\n', encoding='utf-8')
        return {'status': 'SUCCEEDED'}
    first = work_run.run_work('work-one', run_id=IDENTITY, repository_root=root,
        plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []}, implementer=implement, evidence_factory=evidence)
    assert first['status'] == 'SETTLED'
    second = work_run.run_work('work-one', run_id='b'*32, repository_root=root)
    assert second['status'] == 'FAILED'
    head = _git('rev-parse', 'HEAD', cwd=root); graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    recovered = console_recovery.observe(request(root, 'work', 'work-one'))
    assert recovered['state'] == 'RECOVERED' and recovered['result'] == first
    assert _git('rev-parse', 'HEAD', cwd=root) == head and relay_mesh.WORK_GRAPH_FILE.read_bytes() == graph
    with pytest.raises(FileExistsError): work_run.run_work('work-one', run_id=IDENTITY, repository_root=root)
    with custody_lock(work_run.lock_root_for('work-one')):
        assert console_recovery.observe(request(root, 'work', 'work-one'))['state'] == 'BUSY'
    for changed in ({'work_id': 'wrong'}, {'repository_root': str(control)}, {'ended_at': None}, {'status': 'RUNNING'}):
        save_json(work_run.receipt_path(IDENTITY), {**first, **changed})
        assert console_recovery.observe(request(root, 'work', 'work-one'))['state'] == 'UNKNOWN'


def test_reopen_plan_restores_historical_result_but_no_apply_approval(proposal, monkeypatch):
    directory, graph, root = bind_plan(proposal, monkeypatch)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(graph.parent/'storage'))
    result = native_plan.retry_review(directory, invocation_id=IDENTITY)
    value = request(root, 'plan', directory)
    saved = console_demand.save(None, 0, root, relay_mesh.RELAY_ROOT, 'execution_requested',
        value['snapshot'], value['events'][0]['detail'])
    async def scenario():
        backend = Backend(); app = WorkConsole(root, backend=backend)
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved['id']; app.open_demand()
            await app.workers.wait_for_complete()
            assert app.last_result == result and app.reviewed_plan is None
            assert app.demand_state['events'][-1]['kind'] == 'flow_reconciled'
            assert not backend.calls and not app.process
    asyncio.run(scenario())
