"""Compact-context implementer defaults and automatic compaction-loop route escalation."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from compute_plane.native_journal import NativeJournal
from relay import conversation_tasks as tasks, execution_scope, native_agent, work_units
from test_conversation_tasks import host, intake, planned  # noqa: F401
from test_implement_unit import repository
from test_native_agent import deployment, setup  # noqa: F401


def _cli_deployment(tmp_path, *, dep_id, endpoint='cli://agy', model='gemini-4-hydrogen-high',
                    window=200000, output_tokens=16384, projection=None):
    value = {
        'id': dep_id,
        'runner': 'native-http',
        'native_cli': {'executable': str(tmp_path / 'cli.exe'), 'version': '1.2.7' if endpoint == 'cli://agy' else '2.1.280'},
        'native_counter_protocol': 'utf8-envelope-estimate-v1',
        'native_profile': {
            'protocol': 'vendor-cli',
            'endpoint': endpoint,
            'model': model,
            'context_window': window,
            'output_tokens': output_tokens,
            'parameters_json': '{"effort":"high"}',
            'source': 'test-fixture-not-a-qualified-model',
        },
    }
    if projection is not None:
        value['native_context_projection'] = projection
    return value


class _VendorStubClient:
    def __init__(self, endpoint, model, actions_per_turn):
        self.endpoint = endpoint
        self.model = model
        self.actions = list(actions_per_turn)
        self.calls = []

    def post_observed(self, path, body, control):
        self.calls.append((path, body))
        step = self.actions.pop(0)
        tool_calls = [
            {'id': f'cli-action-{i}', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(args)}}
            for i, (name, args) in enumerate(step, 1)
        ]
        return ({
            'model': self.model,
            'usage': {'input_tokens': 4000, 'cache_read_tokens': 0, 'output_tokens': 60, 'total_tokens': 4060},
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': tool_calls}}],
        }, {'transport': 'official_vendor_cli', 'started': True, 'cleanup_verified': True})


@pytest.mark.parametrize('explicit_elide', [None, False])
def test_agy_editing_implementer_gets_compact_guidance_and_elides_edited_reads_by_default(
        setup, tmp_path, explicit_elide):
    config, custody, root = setup
    config['verifiers'] = {}
    config['context_policy'] = {}
    dep = _cli_deployment(
        tmp_path, dep_id='agy-gemini-4-hydrogen-cli', endpoint='cli://agy',
        projection=({'elide_edited_reads': explicit_elide} if explicit_elide is not None else None),
    )
    client = _VendorStubClient('cli://agy', 'gemini-4-hydrogen-high', [
        [('read', {'repository': 'repo', 'path': 'a.py', 'start': 1, 'end': 10})],
        [('edit', {'repository': 'repo', 'path': 'a.py',
                   'sha256': hashlib.sha256(b'value = 1\n').hexdigest(),
                   'search': 'value = 1', 'replace': 'value = 2'})],
        [('tare_deliver', {'summary': 'Updated a.py.'})],
    ])
    result = native_agent.run_session(config, dep, custody, 'Change a.py.', client=client)
    assert result['status'] == 'PROPOSED' and len(client.calls) == 3
    assert (root / 'a.py').read_text(encoding='utf-8') == 'value = 2\n'
    assert native_agent.COMPACT_CONTEXT_GUIDANCE in client.calls[0][1]['system']
    prompt_after_edit = client.calls[2][1]['prompt']
    expected_elide = explicit_elide is None
    assert ('text_omitted' in prompt_after_edit) is expected_elide
    assert ('1|value = 1' in prompt_after_edit) is (not expected_elide)
    saved = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved.get('compact_context_guidance') is True
    assert (saved.get('elide_edited_reads') is True) is expected_elide


def test_large_window_and_readonly_roles_do_not_receive_compact_implementer_defaults(setup, tmp_path):
    config, custody, _ = setup
    config['verifiers'] = {}
    config['context_policy'] = {}
    claude_dep = _cli_deployment(
        tmp_path, dep_id='claude-opus-5-5-cli', endpoint='cli://claude', model='claude-opus-5-5',
    )
    claude_client = _VendorStubClient('cli://claude', 'claude-opus-5-5', [
        [('tare_deliver', {'summary': 'Done.'})],
    ])
    result = native_agent.run_session(config, claude_dep, custody, 'Inspect a.py.', client=claude_client)
    assert result['status'] == 'PROPOSED'
    assert native_agent.COMPACT_CONTEXT_GUIDANCE not in claude_client.calls[0][1]['system']
    saved = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert not saved.get('compact_context_guidance') and not saved.get('elide_edited_reads')

    ro_custody = tmp_path / 'ro-custody'
    ro_config = {**config, 'role': 'explorer', 'writable': {}, 'capabilities': ['read', 'list', 'search']}
    agy_dep = _cli_deployment(tmp_path, dep_id='agy-gemini-4-hydrogen-cli', endpoint='cli://agy')
    agy_client = _VendorStubClient('cli://agy', 'gemini-4-hydrogen-high', [
        [('tare_deliver', {'summary': 'Read only.'})],
    ])
    ro_result = native_agent.run_session(ro_config, agy_dep, ro_custody, 'Inspect a.py.', client=agy_client)
    assert ro_result['status'] == 'PROPOSED'
    assert native_agent.COMPACT_CONTEXT_GUIDANCE not in agy_client.calls[0][1]['system']
    ro_saved = json.loads((ro_custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert not ro_saved.get('compact_context_guidance') and not ro_saved.get('elide_edited_reads')


def _write_compaction_loop_journal(session_path, *, compactions=3, edited=False):
    Path(session_path).mkdir(parents=True, exist_ok=True)
    journal = NativeJournal(Path(session_path) / 'journal.jsonl')
    journal.append({'kind': 'effect_requested', 'effect_id': 's/effect/1', 'tool_call': {
        'call_id': 'c1', 'name': 'read', 'arguments_json': '{"path":"a.py"}'}})
    journal.append({'kind': 'effect_returned', 'result': {
        'effect_id': 's/effect/1', 'status': 'succeeded', 'output_json': '{}', 'progressed': True}})
    for n in range(1, compactions + 1):
        journal.append({'kind': 'context_activated', 'call_id': f's/summary/{n}', 'summary': 'summary'})
    if edited:
        journal.append({'kind': 'effect_requested', 'effect_id': 's/effect/2', 'tool_call': {
            'call_id': 'c2', 'name': 'edit', 'arguments_json': '{"path":"a.py"}'}})
        journal.append({'kind': 'effect_returned', 'result': {
            'effect_id': 's/effect/2', 'status': 'succeeded', 'output_json': '{}', 'progressed': True}})


def _unit_state(setup):
    config, custody, root = setup
    repository(root)
    config = {**config, 'conversation_flow': True, 'writable': {'repo': ['a.py']}}
    state = {'config': config, 'deployment': deployment(), 'conversation_flow': True,
             'limits': {'max_model_calls': 60, 'max_seconds': 3600}}
    unit = {'title': 'Update a.py', 'target_paths': ['a.py'], 'criteria': ['Set value = 2'],
            'verifiers': ['unit'], 'test_paths': []}
    return state, custody, root, unit


def test_conversation_flow_escalates_zero_edit_compaction_loop_past_same_window_routes(
        setup, tmp_path, monkeypatch):
    state, custody, root, unit = _unit_state(setup)
    agy1 = _cli_deployment(tmp_path, dep_id='agy-hydrogen', endpoint='cli://agy')
    agy2 = _cli_deployment(tmp_path, dep_id='agy-argon', endpoint='cli://agy', model='gemini-4-argon-high')
    claude = _cli_deployment(tmp_path, dep_id='claude-large', endpoint='cli://claude', model='claude-opus-5-5')
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([
        ('agy-hydrogen', agy1), ('agy-argon', agy2), ('claude-large', claude),
    ]))
    dispatched = []

    def fake_run_session(child, dep, session_path, instruction, **kwargs):
        dispatched.append(dep['id'])
        if dep['id'] == 'agy-hydrogen':
            _write_compaction_loop_journal(session_path, compactions=3)
            return {'status': 'BLOCKED', 'reason': 'NO_PROGRESS', 'model_calls': 12}
        target = Path(child['repositories']['repo']) / 'a.py'
        target.write_text('value = 2\n', encoding='utf-8')
        return {'status': 'PROPOSED', 'model_calls': 4, 'output': 'Applied change.'}

    monkeypatch.setattr(native_agent, 'run_session', fake_run_session)
    token = work_units.CHECK_PORT.set(lambda argv, **kw: SimpleNamespace(returncode=0, stdout='ok', stderr=''))
    try:
        result = work_units.run_unit(custody, state, unit, lambda: False)
    finally:
        work_units.CHECK_PORT.reset(token)

    assert dispatched == ['agy-hydrogen', 'claude-large']
    assert result['verdict'] == 'VERIFIED' and result['applied'] is True
    assert result['route'] == 'claude-large' and result['rungs'] == 2
    assert (root / 'a.py').read_text(encoding='utf-8') == 'value = 2\n'
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
    assert len(receipt['rungs']) == 2
    skipped = receipt['rungs'][1]['executed_decision']['fallback']['skipped']
    assert {'deployment': 'agy-hydrogen', 'reason': 'COMPACTION_LOOP_ESCALATED'} in skipped
    assert {'deployment': 'agy-argon', 'reason': 'CONTEXT_WINDOW_NOT_LARGER'} in skipped


def test_compaction_loop_escalation_is_bounded_to_one_retry_and_respects_explicit_route_and_pins(
        setup, tmp_path, monkeypatch):
    state, custody, root, unit = _unit_state(setup)
    agy1 = _cli_deployment(tmp_path, dep_id='agy-hydrogen', endpoint='cli://agy')
    claude = _cli_deployment(tmp_path, dep_id='claude-large', endpoint='cli://claude', model='claude-opus-5-5')
    codex_xl = _cli_deployment(tmp_path, dep_id='codex-xl', endpoint='cli://codex', model='gpt-6-1-sol',
                               window=272000, output_tokens=32768)
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([
        ('agy-hydrogen', agy1), ('claude-large', claude), ('codex-xl', codex_xl),
    ]))
    dispatched = []

    def always_loop(child, dep, session_path, instruction, **kwargs):
        dispatched.append(dep['id'])
        _write_compaction_loop_journal(session_path, compactions=3)
        return {'status': 'BLOCKED', 'reason': 'NO_PROGRESS', 'model_calls': 10}

    monkeypatch.setattr(native_agent, 'run_session', always_loop)
    token = work_units.CHECK_PORT.set(lambda argv, **kw: SimpleNamespace(returncode=0, stdout='ok', stderr=''))
    try:
        # At most 1 automatic escalation: agy-hydrogen -> claude-large, never codex-xl.
        result = work_units.run_unit(custody, state, unit, lambda: False)
        assert dispatched == ['agy-hydrogen', 'claude-large']
        assert result['verdict'] == 'INCONCLUSIVE' and result['rungs'] == 2
        assert result['implementation_stop']['compaction_loop'] == 3

        # Explicit args['route'] never escalates automatically.
        dispatched.clear()
        pinned_arg = work_units.run_unit(custody, state, {**unit, 'route': 'agy-hydrogen'}, lambda: False)
        assert dispatched == ['agy-hydrogen'] and pinned_arg['rungs'] == 1

        # Temporary implementer pin never escalates automatically.
        dispatched.clear()
        scope = execution_scope.Scope(pins=(('implementer', 'google/agy-hydrogen:high'),))
        monkeypatch.setattr(execution_scope, 'current', lambda: scope)
        monkeypatch.setattr(execution_scope, 'pin_matches', lambda raw, dep: dep['id'] == 'agy-hydrogen')
        pinned_scope = work_units.run_unit(custody, state, unit, lambda: False)
        assert dispatched == ['agy-hydrogen'] and pinned_scope['rungs'] == 1
    finally:
        work_units.CHECK_PORT.reset(token)


def test_compaction_loop_preserves_original_diagnostic_when_escalation_cannot_start(
        setup, tmp_path, monkeypatch):
    state, custody, root, unit = _unit_state(setup)
    agy1 = _cli_deployment(tmp_path, dep_id='agy-hydrogen', endpoint='cli://agy')
    claude = _cli_deployment(tmp_path, dep_id='claude-large', endpoint='cli://claude', model='claude-opus-5-5')
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([
        ('agy-hydrogen', agy1), ('claude-large', claude),
    ]))

    def fake_limits(custody_, state_, share=1, deployment=None):
        if deployment and deployment['id'] == 'claude-large':
            raise ValueError('The session has too little left for a child (0 calls; R5).')
        return {'max_model_calls': 20, 'max_seconds': 600}

    def first_loop(child, dep, session_path, instruction, **kwargs):
        _write_compaction_loop_journal(session_path, compactions=3)
        return {'status': 'BLOCKED', 'reason': 'NO_PROGRESS', 'model_calls': 10}

    monkeypatch.setattr(work_units, 'child_limits', fake_limits)
    monkeypatch.setattr(native_agent, 'run_session', first_loop)
    token = work_units.CHECK_PORT.set(lambda argv, **kw: SimpleNamespace(returncode=0, stdout='ok', stderr=''))
    try:
        result = work_units.run_unit(custody, state, unit, lambda: False)
    finally:
        work_units.CHECK_PORT.reset(token)

    assert result['verdict'] == 'INCONCLUSIVE'
    assert result['route'] == 'agy-hydrogen'
    assert result['implementation_stop']['compaction_loop'] == 3


def test_fresh_user_continuation_after_compaction_loop_authorizes_retry_of_saved_plan(host):
    from test_conversation_tasks import effect, user_reply
    intake(host)
    planned(host)
    tools, execution, *_ = host
    value = tasks.load(tools.root)
    task = tasks.current(value)
    task['delivery'] = {
        'verdict': 'INCONCLUSIVE',
        'applied': False,
        'files': [],
        'route': 'agy-gemini-4-hydrogen-cli',
        'reason': 'the implementer changed nothing',
        'implementation_stop': {
            'child_status': 'BLOCKED',
            'reason': 'NO_PROGRESS',
            'model_calls': 18,
            'compaction_loop': 3,
        },
    }
    task['attempt_input'] = 'earlier-input'
    task['delivery_invocation'] = 'earlier-invocation'
    tasks.save(tools.root, value)

    user_reply(host, 'Retry on another route.')
    res = tools.execute(effect('demand', {
        'objective': task['objective'], 'intent': 'deliver', 'relation': 'continue',
    }), execution)
    assert res.status == 'succeeded'
    body = json.loads(res.output_json)
    assert 'Call implement now with the saved plan' in body['delivery']['next']
    assert 'larger context window' in body['delivery']['next']


@pytest.mark.parametrize('explicit_dup', [None, False])
def test_agy_compact_planner_gets_planner_guidance_duplicate_read_elision_and_mechanical_read_bounds(
        setup, tmp_path, explicit_dup):
    config, custody, root = setup
    (root / 'big.py').write_text(''.join(f'line_{i} = {i}\n' for i in range(1, 161)), encoding='utf-8')
    planner_cfg = {**config, 'role': 'planner', 'writable': {}, 'verifiers': {},
                   'capabilities': ['read', 'list', 'search'], 'context_policy': {}}
    dep = _cli_deployment(
        tmp_path, dep_id='agy-gemini-4-carbon-cli', endpoint='cli://agy', model='gemini-4-carbon-high',
        projection=({'elide_duplicate_reads': explicit_dup} if explicit_dup is not None else None),
    )
    client = _VendorStubClient('cli://agy', 'gemini-4-carbon-high', [
        [('read', {'repository': 'repo', 'path': 'big.py', 'start': 1, 'end': 160})],
        [('read', {'repository': 'repo', 'path': 'big.py', 'start': 1, 'end': 120})],
        [('tare_deliver', {'summary': 'Planned big.py.'})],
    ])
    result = native_agent.run_session(planner_cfg, dep, custody, 'Plan changes to big.py.', client=client)
    assert result['status'] == 'PROPOSED' and len(client.calls) == 3
    assert native_agent.COMPACT_PLANNER_GUIDANCE in client.calls[0][1]['system']
    assert native_agent.COMPACT_CONTEXT_GUIDANCE not in client.calls[0][1]['system']
    # Mechanical read cap clamps the 1..160 request to 120 lines.
    prompt_t2 = client.calls[1][1]['prompt']
    assert '120|line_120 = 120' in prompt_t2 and '121|line_121 = 121' not in prompt_t2
    # After re-reading 1..120 on turn 2, turn 3 elides the earlier duplicate read by default.
    prompt_t3 = client.calls[2][1]['prompt']
    expected_elide = explicit_dup is None
    assert ('Covered verbatim by a later read' in prompt_t3) is expected_elide
    saved = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved.get('compact_context_guidance') is True
    assert saved.get('read_max_lines') == native_agent.COMPACT_READ_MAX_LINES
    assert saved.get('read_max_bytes') == native_agent.COMPACT_READ_MAX_BYTES
    assert (saved.get('elide_duplicate_reads') is True) is expected_elide


def test_delegate_plan_escalates_once_on_compaction_loop_and_fair_quota_skips_cancelled_and_invalid(
        tmp_path, monkeypatch):
    from relay import planning_tools, turn_engine
    from test_planning_tools import PLAN, execution, request, wrap

    parent = tmp_path / 'parent'; parent.mkdir()
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'a.py').write_text('value = 1\n', encoding='utf-8')
    agy1 = _cli_deployment(tmp_path, dep_id='agy-carbon', endpoint='cli://agy', model='gemini-4-carbon-high')
    agy2 = _cli_deployment(tmp_path, dep_id='agy-argon', endpoint='cli://agy', model='gemini-4-argon-high')
    claude = _cli_deployment(tmp_path, dep_id='claude-large', endpoint='cli://claude', model='claude-opus-5-5')
    by_id = {'agy-carbon': agy1, 'agy-argon': agy2, 'claude-large': claude}
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (by_id.get(name), None))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'planner': [['agy-carbon'], ['agy-argon'], ['claude-large']]})
    state = {
        'deployment': claude, 'conversation_flow': False,
        'planning': {'routes': dict(by_id), 'route_order': ['agy-carbon', 'agy-argon', 'claude-large']},
        'limits': {'max_model_calls': 30}, 'deadline_utc': None,
        'config': {'repositories': {'repo': str(repo)}, 'writable': {'repo': ['a.py']}, 'verifiers': {}},
    }
    dispatched = []

    def fake_run_session(child, dep, session_path, instruction, **kwargs):
        dispatched.append(dep['id'])
        if dep['id'] == 'agy-carbon':
            _write_compaction_loop_journal(session_path, compactions=3)
            return {'status': 'BLOCKED', 'reason': 'NO_PROGRESS', 'model_calls': 5}
        Path(session_path).mkdir(parents=True, exist_ok=True)
        (Path(session_path) / 'artifact.json').write_text(json.dumps({
            'schema': 'tare.tools/native-artifact/1', 'role': 'planner', 'payload': PLAN,
        }), encoding='utf-8')
        return {'status': 'PROPOSED', 'model_calls': 3}

    monkeypatch.setattr(native_agent, 'run_session', fake_run_session)
    tool = wrap(parent, state, execution())
    # 1. Unpinned compaction loop skips equal-window agy-argon and escalates once to claude-large.
    res = tool.execute(request({'instruction': 'Plan a.py.'}, effect='esc-1'), execution())
    assert res.status == 'succeeded', res.output_json
    body = json.loads(res.output_json)
    assert body['status'] == 'PLANNED' and body['route'] == 'claude-large'
    assert dispatched == ['agy-carbon', 'claude-large']
    receipt = json.loads(next((parent / 'planning').glob('*/receipt.json')).read_text(encoding='utf-8'))
    assert receipt['escalated_from'] == 'agy-carbon' and len(receipt['attempts']) == 2
    skipped = receipt['executed_decision']['fallback']['skipped']
    assert {'deployment': 'agy-carbon', 'reason': 'COMPACTION_LOOP_ESCALATED'} in skipped
    assert {'deployment': 'agy-argon', 'reason': 'CONTEXT_WINDOW_NOT_LARGER'} in skipped

    # 2. Explicit route pin never escalates automatically.
    dispatched.clear()
    res_pinned = tool.execute(request({'instruction': 'Plan pinned.', 'route': 'agy-carbon'}, effect='esc-pin'), execution())
    assert res_pinned.status == 'succeeded'
    assert json.loads(res_pinned.output_json)['status'] == 'BLOCKED'
    assert dispatched == ['agy-carbon']

    # 3. CANCELLED and pre-artifact INVALID_DELIVERY do not consume MAX_REQUESTS=3,
    #    while MAX_TOTAL_ATTEMPTS=6 still enforces a hard cap.
    parent2 = tmp_path / 'parent2'; parent2.mkdir()
    for i, res_obj in enumerate([
        {'status': 'CANCELLED'},
        {'status': 'BLOCKED', 'failure_code': 'INVALID_DELIVERY', 'delivery_validation': {'issues': ['bad']}},
        {'status': 'CANCELLED'},
        {'status': 'BLOCKED', 'delivery_validation': {'issues': ['missing']}},
    ]):
        folder = parent2 / 'planning' / f'r{i}'; folder.mkdir(parents=True)
        (folder / 'receipt.json').write_text(json.dumps({
            'demand_id': None, 'planning_revision': 0, 'status': 'FINISHED',
            'calls_reserved': 6, 'calls_used': 1, 'result': res_obj,
        }), encoding='utf-8')
    dispatched.clear()
    tool2 = wrap(parent2, state, execution())
    # 4 non-quota attempts -> 5th call is still admitted!
    res_fifth = tool2.execute(request({'instruction': 'Fifth attempt.'}, effect='fair-5'), execution())
    assert res_fifth.status == 'succeeded' and json.loads(res_fifth.output_json)['status'] == 'PLANNED'
    # Add 1 more cancelled receipt to reach MAX_TOTAL_ATTEMPTS=6 -> 7th call is refused by hard cap.
    folder = parent2 / 'planning' / 'r_extra'; folder.mkdir(parents=True)
    (folder / 'receipt.json').write_text(json.dumps({
        'demand_id': None, 'planning_revision': 0, 'status': 'FINISHED',
        'calls_reserved': 6, 'calls_used': 1, 'result': {'status': 'CANCELLED'},
    }), encoding='utf-8')
    dispatched.clear()
    res_cap = tool2.execute(request({'instruction': 'Seventh attempt.'}, effect='fair-7'), execution())
    assert res_cap.status == 'refused' and 'maximum of 6 planning attempts' in res_cap.output_json
    assert dispatched == []
