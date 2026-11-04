import asyncio
import copy
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from tare_tools_agent_runtime import CancellationToken

from relay import console_entry, flow_assessment as flows
from relay.console_backend import Backend, Selection, command


@pytest.fixture
def configured(tmp_path, monkeypatch):
    policy = {'schema': 'tare.tools/flow-assessment-policy/1', 'mode': 'suggest',
              'command': ['fixture-worker'], 'timeout_seconds': 60}
    path = tmp_path/'policy.json'; path.write_text(json.dumps(policy))
    monkeypatch.setenv('TARE_FLOW_POLICY', str(path))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    project = tmp_path/'project'; project.mkdir()
    return project, policy


def answer(choice, request):
    return {'status': 'OBSERVED', 'inference_attempts': 1, 'answer': {
        'choice': choice, 'probabilities': {k: float(k == choice) for k in request['options']},
        'usage': {'input_tokens': 100, 'output_tokens': 1},
        'model_identity': {'method': 'litjev-native-first-token-v1', 'model': 'fixture'}}}


@pytest.mark.parametrize('choice', list(flows.ENTRY_OPTIONS))
def test_entry_uses_local_choice_without_generation_or_execution(configured, tmp_path, monkeypatch, choice):
    calls = []
    def assess(cmd, request, **kw):
        calls.append(request)
        return answer(choice, request)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    def forbidden(*a, **kw): pytest.fail('A second generative interpreter was called')
    selected = Selection(str(configured[0]), prompt='Investigate caching https://example.org/docs')
    result = console_entry.propose(selected, CancellationToken(), directory=tmp_path/'entry', generate=forbidden)
    assert len(calls) == 1 and result['tokens'] == {'input': 100, 'output': 1}
    assert result['status'] == ('UNAVAILABLE' if choice == 'insufficient' else 'NEEDS_INPUT' if choice == 'clarify' else 'PROPOSED')
    if choice == 'explore':
        realized = console_entry.realize(selected, result)
        assert realized.role == 'explorer'
        args = command(realized)
        assert args[args.index('--role')+1] == 'explorer'
    assert not list(configured[0].iterdir())


def test_explicit_and_missing_urls(configured, tmp_path, monkeypatch):
    def forbidden(*a, **kw): pytest.fail('Explicit command called evaluator')
    monkeypatch.setattr(flows, 'run_assessment', forbidden)
    selected = Selection(str(configured[0]), prompt='/explore Read files only')
    assert console_entry.propose(selected, CancellationToken(), directory=tmp_path/'explicit')['generation_calls'] == 0
    monkeypatch.setattr(flows, 'run_assessment', lambda cmd, req, **kw: answer('research', req))
    selected.prompt = 'Research caching approaches'
    result = console_entry.propose(selected, CancellationToken(), directory=tmp_path/'missing')
    assert result['status'] == 'NEEDS_INPUT' and not result['urls']


@pytest.mark.parametrize('fault', ['timeout', 'ram', 'cancel', 'overflow', 'invalid'])
def test_failure_never_falls_back_to_paid_generation(configured, tmp_path, monkeypatch, fault):
    cancellation = CancellationToken()
    def assess(cmd, request, **kw):
        if fault == 'cancel': cancellation.cancel()
        if fault == 'invalid':
            value = answer('session', request); value['answer']['probabilities']['explore'] = 1
            return value
        return {'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT' if fault == 'timeout' else 'ASSESSMENT_INSUFFICIENT_RAM', 'inference_attempts': 1}
    monkeypatch.setattr(flows, 'run_assessment', assess)
    def forbidden(*a, **kw): pytest.fail('Unexpected generation')
    result = console_entry.propose(Selection(str(configured[0]), prompt='x'*9000 if fault == 'overflow' else 'Help'),
        cancellation, directory=tmp_path/'entry', generate=forbidden)
    assert result['status'] == ('CANCELLED' if fault == 'cancel' else 'UNAVAILABLE')
    if fault == 'overflow': assert result['generation_calls'] == 0


def test_decision_replay_interruption_and_changed_binding(configured, tmp_path):
    req = {'state': 'A task', 'question': 'Which?', 'options': {'a': 'A', 'b': 'B'}}
    calls = []
    def assess(cmd, request, **kw): calls.append(1); return answer('a', request)
    root = tmp_path/'decision'
    first = flows.decide(req, root, CancellationToken(), configured[1], assess=assess)
    assert first == flows.decide(req, root, CancellationToken(), configured[1], assess=assess)
    assert len(calls) == 1
    with pytest.raises(ValueError, match='INPUT_CHANGED'):
        flows.decide({**req, 'state': 'Changed'}, root, CancellationToken(), configured[1], assess=assess)
    saved = json.loads((root/'decision.json').read_text()); saved.update(status='UNKNOWN', choice=None)
    (root/'decision.json').write_text(json.dumps(saved))
    assert flows.decide(req, root, CancellationToken(), configured[1], assess=assess)['status'] == 'UNKNOWN'
    assert len(calls) == 1


def test_worker_timeout_diagnosis_reaches_entry_and_saved_receipt(configured, tmp_path, monkeypatch):
    calls = []
    def assess(*args, **kwargs):
        calls.append(1)
        return {'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT', 'worker_exit_code': 124,
                'worker_stage': 'CPU_SCORING', 'elapsed_ms': 52000, 'inference_attempts': 1}
    monkeypatch.setattr(flows, 'run_assessment', assess)
    selected = Selection(str(configured[0]), prompt='Fix the recovery message')
    def forbidden(*args, **kwargs): pytest.fail('Unexpected paid inference fallback')
    result = console_entry.propose(selected, CancellationToken(), directory=tmp_path/'entry-timeout', generate=forbidden)
    assert result['status'] == 'UNAVAILABLE'
    assert 'time limit' in result['reason'] and 'scoring on CPU' in result['reason']
    assert 'Worker exit: 124' in result['reason'] and '/session' in result['reason']
    receipt = result['flow_assessment']
    assert receipt['diagnostics']['worker_exit_code'] == 124
    saved = json.loads(Path(receipt['receipt']).read_text())
    assert saved['assessment']['worker_stage'] == 'CPU_SCORING'
    assert len(calls) == 1 and selected.prompt == 'Fix the recovery message'


@pytest.fixture
def findings(configured, monkeypatch):
    from relay import planning_findings
    root = str(configured[0].resolve())
    selected = asdict(Selection(root, mode='research', research='fixture-owner', prompt='Understand the cache design.'))
    value = {'id': 'a'*32, 'root': root, 'control_root': root+'-control', 'events': [
        {'kind': 'execution_requested', 'selection': selected}]}
    step = {'action': 'REVIEW_FINDINGS', 'reason': 'Review findings', 'mode': 'research',
            'owner_field': 'research', 'owner': 'fixture-owner'}
    view = {'root': root, 'sha256': 'a'*64, 'omitted': [], 'reports': [
        {'question': 'Cache design?', 'artifact': {'payload': {'summary': 'Design inspected.',
                    'remaining': ['Check invalidation'], 'sources': []}}}]}
    monkeypatch.setattr(planning_findings, 'preview', lambda *a: copy.deepcopy(view))
    return value, step, view


@pytest.mark.parametrize('choice,action', [('plan', 'REVIEW_FINDINGS'), ('explore', 'SUGGEST_EXPLORATION'),
    ('review_result', 'REVIEW_DEMAND_RESULT'), ('insufficient', 'REVIEW_FINDINGS')])
def test_completed_boundary_maps_only_to_existing_controls(configured, findings, monkeypatch, choice, action):
    value, step, _ = findings; calls = []
    def assess(cmd, req, **kw): calls.append(req); return answer(choice, req)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    result = flows.after_findings(value, step, CancellationToken())
    assert result['action'] == action
    assert flows.after_findings(value, step, CancellationToken()) == result
    assert len(calls) == 1
    if choice == 'explore': assert 'Read only' in result['prompt']


def test_interactive_worker_override_does_not_change_findings_admission(configured, findings, tmp_path, monkeypatch):
    config = {**configured[1], 'entry_command': ['resident-only-worker']}
    monkeypatch.setattr(flows, 'policy', lambda: config)
    calls = []
    def assess(cmd, request, **kwargs):
        calls.append(cmd)
        return answer('explore', request)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    flows.entry(Selection(str(configured[0])), ['Inspect the design'], [], tmp_path/'entry', CancellationToken())
    value, step, _ = findings
    flows.after_findings(value, step, CancellationToken())
    assert calls == [['resident-only-worker'], ['fixture-worker']]
    assert config['command'] == ['fixture-worker']
    saved = json.loads((tmp_path/'entry/flow-assessment/decision.json').read_text())
    assert saved['binding']['policy'] == flows.digest({**config, 'command': config['entry_command']})


@pytest.mark.parametrize('invalid', [None, [], 'worker', [''], [0], ['bad\x00argv']])
def test_invalid_entry_command_rejected(configured, tmp_path, invalid):
    (tmp_path/'policy.json').write_text(json.dumps({**configured[1], 'entry_command': invalid}))
    with pytest.raises(ValueError, match='FLOW_ENTRY_COMMAND_INVALID'):
        flows.policy()


def test_changed_findings_discard_suggestion(configured, findings, monkeypatch):
    value, step, view = findings
    def assess(cmd, req, **kw): view['sha256'] = 'b'*64; return answer('review_result', req)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    result = flows.after_findings(value, step, CancellationToken())
    assert result['action'] == 'REVIEW_FINDINGS'
    assert result['flow_assessment']['reason'] == 'FLOW_BOUNDARY_CHANGED'


def test_resume_draft_does_not_rewrite_original_demand(configured, findings, monkeypatch):
    value, step, _ = findings
    resume = copy.deepcopy(value['events'][0])
    resume['selection']['prompt'] = 'Different draft, never dispatched to this owner'
    value['events'].append(resume)
    def assess(cmd, request, **kw):
        assert json.loads(request['state'])['operator_demand'] == 'Understand the cache design.'
        return answer('plan', request)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    assert flows.after_findings(value, step, CancellationToken())['flow_assessment']['status'] == 'SUGGESTED'


def test_tui_next_stage_opens_read_only_without_dispatch(configured, findings, monkeypatch):
    from relay import demand_conduction, relay_mesh
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from textual.widgets import Select, TextArea
    _, step, _ = findings
    monkeypatch.setattr(demand_conduction, 'next_step', lambda value: step)
    monkeypatch.setattr(flows, 'run_assessment', lambda cmd, req, **kw: answer('explore', req))
    async def scenario():
        backend = FakeBackend(); app = WorkConsole(configured[0], backend=backend)
        async with app.run_test(size=(130,46)) as pilot:
            await pilot.pause()
            app.restore_selection(asdict(Selection(str(configured[0]), mode='research',
                research='fixture-owner', prompt='Understand the cache design.')))
            app.checkpoint('execution_requested')
            app.checkpoint('execution_observed', {'status': 'COMPLETED'})
            await app.continue_demand().wait()
            assert app.selection().mode == 'session' and app.selection().role == 'explorer'
            assert not app.selection().session and not app.selection().terminal
            assert not backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['CHECK_RECOVERY', 'BLOCKED', 'REVIEW_PLAN', 'RESUME_WORK', 'HUMAN_DECISION', 'START_WORK', 'RESUME_FLOW'])
def test_protected_boundaries_never_infer(configured, monkeypatch, action):
    monkeypatch.setattr(flows, 'policy', lambda: pytest.fail('Protected boundary attempted evaluation'))
    baseline = {'action': action}
    assert flows.after_findings({}, baseline, CancellationToken()) is baseline


def test_tui_chooses_and_reopens_without_reinference(configured, monkeypatch):
    from textual.widgets import TextArea, Select
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    calls = []
    def assess(cmd, req, **kw): calls.append(1); return answer('explore', req)
    monkeypatch.setattr(flows, 'run_assessment', assess)
    class EntryBackend(FakeBackend):
        propose_entry = Backend.propose_entry
    async def scenario():
        backend = EntryBackend()
        app = WorkConsole(configured[0], backend=backend, initial_mode='conversation')
        async with app.run_test(size=(130,46)) as pilot:
            await pilot.pause()
            app.query_one('#prompt', TextArea).load_text('Explain the cache design without editing.')
            await pilot.click('#start')
            for _ in range(1500):  # the interpretation worker may not have started when the click returns
                if app.entry_proposal and not app.busy: break
                await pilot.pause(.02)
            assert app.entry_proposal[1]['flow'] == 'explore' and not backend.launches
            identity = app.demand_id
        app = WorkConsole(configured[0], backend=backend, initial_mode='conversation')
        async with app.run_test(size=(130,46)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete()
            assert app.entry_proposal[1]['flow'] == 'explore' and len(calls) == 1
            await pilot.click('#start-suggested'); await pilot.pause()
            assert len(backend.launches) == 1
            args = backend.launches[0]
            assert args[args.index('--role')+1] == 'explorer'
            app.action_cancel()
            while app.busy: await pilot.pause(.02)
    asyncio.run(scenario())
