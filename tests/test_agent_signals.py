"""Tests for the host-mediated typed agent signal bus and propose_checks flow (PR 1)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from compute_plane.native_journal import save_json
from compute_plane.native_models import encode
from tare_tools_agent_runtime import EffectRequest, EffectResult, ToolCall

from relay import (
    agent_signals,
    conversation_permissions as permissions,
    conversation_tasks as tasks,
    delegation_tools,
    native_artifacts,
    planning_tools,
    relay_mesh,
    turn_engine,
)


def _control(cancel=None):
    return SimpleNamespace(
        binding='test-binding',
        should_stop=cancel or (lambda: False),
        cancellation=SimpleNamespace(is_cancelled=cancel or (lambda: False)),
        monotonic=time.monotonic,
    )


def _effect(name, args, eid='eff-1'):
    return EffectRequest(
        session_id='sess-1',
        effect_id=eid,
        tool_call=ToolCall(call_id=eid, name=name, arguments_json=encode(args)),
    )


def _base_plan(repo='proj', path='app.py', sha='a' * 64, *, tasks_list=None, signals=None):
    plan = {
        'summary': 'Plan summary.',
        'sources': [{'repository': repo, 'path': path, 'sha256': sha}],
        'remaining': [],
        'steps': ['Update app.py'],
        'criteria': ['app.py works'],
    }
    if tasks_list is not None:
        plan['tasks'] = tasks_list
    if signals is not None:
        plan['signals'] = signals
    return plan


def test_signal_catalog_emitter_allowlists_and_host_stamping():
    propose = {
        'kind': 'propose_checks',
        'summary': 'Add pytest check',
        'reason': 'No checks.json exists yet.',
        'payload': {'checks': {'unit': {'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 60}}},
    }
    drift = {
        'kind': 'goal_drift',
        'summary': 'Target API was removed',
        'reason': 'File uses v2 API instead of v1.',
        'payload': {
            'expected': 'Modify legacy v1 endpoint',
            'discovered': 'v1 endpoint was removed; only v2 exists',
            'proposed_objective': 'Add feature to v2 endpoint',
            'options': ['Update v2 endpoint', 'Restore v1 endpoint'],
        },
    }
    blocker = {
        'kind': 'out_of_scope_blocker',
        'summary': 'Flaky unrelated smoke test',
        'reason': 'tests/smoke.gd fails intermittently on unmodified main.',
        'payload': {
            'paths': ['tests/smoke.gd'],
            'symptom': 'Random physics timeout in unrelated scene',
            'suggested_follow_up': 'Open a separate task to stabilize tests/smoke.gd.',
        },
    }

    # Auditor and plan-auditor can ONLY emit out_of_scope_blocker, never propose_checks or goal_drift.
    for role in ('auditor', 'plan-auditor'):
        with pytest.raises(ValueError, match='agent_signal_emitter_not_allowed'):
            agent_signals.validate_signal(role, propose)
        with pytest.raises(ValueError, match='agent_signal_emitter_not_allowed'):
            agent_signals.validate_signal(role, drift)
        stamped = agent_signals.stamp_signals(role, [blocker], source_id='review-1')
        assert len(stamped) == 1
        assert stamped[0]['from_role'] == role
        assert stamped[0]['authority'] == 'ADVISORY_ONLY'
        assert stamped[0]['policy'] == agent_signals.POLICY_SURFACE_TO_OPERATOR

    # Planner, explorer, implementer can emit all 3 kinds.
    for role in ('planner', 'explorer', 'implementer'):
        stamped = agent_signals.stamp_signals(role, [propose, drift, blocker], source_id='run-1')
        assert [s['from_role'] for s in stamped] == [role, role, role]
        assert all(s['authority'] == 'ADVISORY_ONLY' for s in stamped)
        assert len({s['signal_id'] for s in stamped}) == 3

    # Coordinator can emit propose_checks, not goal_drift.
    assert agent_signals.validate_signal('coordinator', propose)['kind'] == 'propose_checks'
    with pytest.raises(ValueError, match='agent_signal_emitter_not_allowed'):
        agent_signals.validate_signal('coordinator', drift)


def test_signal_caps_and_artifact_digest_preservation():
    # Without signals, validate() pops 'signals' so existing saved artifact digests remain identical.
    raw_plan = _base_plan()
    validated = native_artifacts.validate('planner', raw_plan)
    assert 'signals' not in validated
    assert 'signals' not in json.dumps(validated)

    # Forged authority or from_role inside Signal is rejected by strict extra='forbid'.
    forged = _base_plan(signals=[{
        'kind': 'out_of_scope_blocker',
        'summary': 'Forged',
        'reason': 'Trying to impersonate user.',
        'from_role': 'user',
        'authority': 'APPROVED',
        'payload': {'symptom': 'fake'},
    }])
    with pytest.raises(Exception):
        native_artifacts.validate('planner', forged)

    # More than 3 signals rejected.
    blocker = {
        'kind': 'out_of_scope_blocker',
        'summary': 'Blocker',
        'reason': 'Unrelated failure.',
        'payload': {'symptom': 'fails'},
    }
    with pytest.raises(Exception):
        native_artifacts.validate('planner', _base_plan(signals=[blocker] * 4))

    # Oversized single signal (>8 KiB) rejected.
    big_checks = {f'c{i}': {'argv': ['python', '-c', 'x' * 700]} for i in range(13)}
    oversized = {
        'kind': 'propose_checks',
        'summary': 'Huge checks',
        'reason': 'Too large.',
        'payload': {'checks': big_checks},
    }
    with pytest.raises(ValueError, match='agent_signal_over_budget'):
        agent_signals.validate_signal('planner', oversized)

    # Multiple propose_checks in one run rejected.
    p = {
        'kind': 'propose_checks',
        'summary': 'One',
        'reason': 'Reason.',
        'payload': {'checks': {'unit': {'argv': ['python', '-m', 'pytest']}}},
    }
    with pytest.raises(ValueError, match='agent_signal_multiple_propose_checks'):
        agent_signals.validate_artifact_signals('planner', [p, p])

    # Auditor artifact cannot carry propose_checks.
    p_wire = {
        'kind': 'propose_checks',
        'summary': 'One',
        'reason': 'Reason.',
        'payload': {'checks': [{'name': 'unit', 'argv': ['python', '-m', 'pytest']}]},
    }
    bad_review = {
        'summary': 'Review summary.',
        'sources': [{'repository': 'proj', 'path': 'app.py', 'sha256': 'a' * 64}],
        'remaining': [],
        'verdict': 'ACCEPT',
        'findings': [],
        'signals': [p_wire],
    }
    with pytest.raises(ValueError, match='agent_signal_emitter_not_allowed'):
        native_artifacts.validate('auditor', bad_review)

    # Validate idempotency: validate(role, validate(role, payload)) succeeds for signals with list checks.
    plan_with_signal = _base_plan(signals=[p_wire])
    once = native_artifacts.validate('planner', plan_with_signal)
    assert native_artifacts.validate('planner', once) == once

    # Verify both native_artifacts schemas and conversation_tasks.SPEC['propose_checks'] satisfy strict Codex CLI codec.
    from compute_plane.native_cli import cli_request
    propose_tool = {'function': {'name': 'propose_checks', 'parameters': tasks.SPEC['propose_checks']['parameters']}}
    req = cli_request(dict(model='fixture', messages=[], tools=[propose_tool]), structured=True, native_arguments=True)
    assert 'propose_checks' in req['prompt']


def test_prepare_and_write_checks_proposal_validation_merge_replace_and_toctou(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    root.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)

    # Reject unsafe/invalid proposals strictly.
    for bad in (
        {'checks': {'conversation-diff': {'argv': ['python', '-c', 'pass']}}},
        {'checks': {'tare-backlog-validate': {'argv': ['python', '-c', 'pass']}}},
        {'checks': {'bad': {'argv': ['../evil.py']}}},
        {'checks': {'bad': {'argv': [sys.executable, '-c', 'pass']}}},
        {'checks': {'bad': {'argv': ['python', '-c', 'pass'], 'timeout_seconds': 2}}},
        {'checks': {'bad': {'argv': ['python', '-c', 'pass'], 'timeout_seconds': True}}},
        {'checks': {'bad': {'mode': 'gui', 'argv': ['godot', '-s', 'tests/v.gd']}}},
        {'checks': {'bad': {'argv': ['python', '-c', 'pass'], 'paths': ['tests/a.py']}}},
        {'checks': {'bad': {'argv': ['nonexistent-binary-xyz-999']}}},
        {'checks': {'bad': None}, 'replace': True},
    ):
        with pytest.raises(ValueError):
            agent_signals.prepare_checks_proposal(root, bad)

    # Create initial .tare/checks.json cleanly.
    initial_payload = {
        'checks': {
            'smoke': {'argv': ['godot', '--headless', '--path', '.', '-s', 'tests/smoke.gd'], 'timeout_seconds': 90},
            'visual': {'mode': 'gui', 'description': 'Needs GPU renderer.', 'paths': ['tests/visual.gd']},
        }
    }
    prep1 = agent_signals.prepare_checks_proposal(root, initial_payload)
    assert prep1['existed'] is False
    assert prep1['has_changes'] is True
    assert prep1['added'] == ['smoke', 'visual']
    assert prep1['removed'] == []
    assert prep1['modified'] == []
    assert '[+] ADDED    smoke:' in prep1['diff_text']

    written_path, _, parsed = turn_engine.write_project_checks_doc(
        root, prep1['document'], expected_sha256=prep1['existing_sha256'])
    assert written_path == root / '.tare' / 'checks.json'
    assert set(parsed['verifiers']) == {'smoke'}
    assert parsed['timeouts'] == {'smoke': 90}
    assert set(parsed['manual']) == {'visual'}

    # Merge: modify smoke timeout, remove visual (None), add unit.
    merge_payload = {
        'checks': {
            'smoke': {'argv': ['godot', '--headless', '--path', '.', '-s', 'tests/smoke.gd'], 'timeout_seconds': 120},
            'visual': None,
            'unit': {'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 45},
        }
    }
    prep2 = agent_signals.prepare_checks_proposal(root, merge_payload)
    assert prep2['existed'] is True
    assert prep2['added'] == ['unit']
    assert prep2['modified'] == ['smoke']
    assert prep2['removed'] == ['visual']
    assert '[-] REMOVED  visual:' in prep2['diff_text']

    # TOCTOU: if file changes before write, expected_sha256 guard refuses.
    written_path.write_text(json.dumps({'checks': {'unit': {'argv': ['python', '-m', 'pytest']}}}), encoding='utf-8')
    with pytest.raises(ValueError, match='changed on disk'):
        turn_engine.write_project_checks_doc(root, prep2['document'], expected_sha256=prep2['existing_sha256'])

    # Replace mode replaces existing checks completely.
    prep3 = agent_signals.prepare_checks_proposal(root, {
        'replace': True,
        'checks': {'smoke': {'argv': ['godot', '--headless', '--path', '.', '-s', 'tests/smoke.gd']}},
    })
    assert prep3['added'] == ['smoke']
    assert prep3['removed'] == ['unit']

    # Removing/replacing a malformed existing entry (e.g. non-dict value) must not crash diff formatting.
    written_path.write_text(json.dumps({'checks': {'broken': None, 'bad-num': 42}}), encoding='utf-8')
    prep4 = agent_signals.prepare_checks_proposal(root, {
        'replace': True,
        'checks': {'unit': {'argv': ['python', '-m', 'pytest', '-q']}},
    })
    assert prep4['added'] == ['unit']
    assert prep4['removed'] == ['bad-num', 'broken']
    assert '<invalid entry: None>' in prep4['diff_text']


def test_confirm_checks_refuses_oversized_diff_and_ignores_unchanged_checks_size(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    perm_dir = tmp_path / 'perms'
    perm_dir.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)
    state = {'session_id': 'sess-1', 'config': {'repositories': {'proj': str(project)}, 'verifiers': {}}}

    # Oversized diff (>30 KiB request) must fail closed rather than truncating hidden commands.
    huge = {'checks': {f'c{i}': {'argv': ['python', '-c', 'x' * 950, 'y' * 950]} for i in range(16)}}
    with pytest.raises(ValueError, match='too large to display completely'):
        permissions.confirm_checks(
            session, state, project=project, proposal=huge, from_role='coordinator',
            summary='Huge replace', reason='Testing modal bound.', effect_id='e-big',
            control=_control(), directory=perm_dir)
    assert not (project / '.tare' / 'checks.json').exists()


def test_explore_goal_drift_before_demand_blocks_same_turn_new_demand(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    monkeypatch.setattr(tasks, 'assess_intent', lambda *a, **kw: {'status': 'UNDERSTOOD', 'assessment': {'choice': 'deliver'}})

    exp_state = {
        'session_id': 'sess-1',
        'config': {'role': 'coordinator', 'repositories': {'proj': str(project)}, 'verifiers': {}},
        'limits': {'max_model_calls': None},
        'delegation': {'settings': {}, 'variant': 'default'},
    }
    drift_res = EffectResult('e1', 'succeeded', encode({
        'investigations': [],
        'agent_signals': agent_signals.stamp_signals('explorer', [{
            'kind': 'goal_drift',
            'summary': 'Codebase uses v2',
            'reason': 'v1 is gone.',
            'payload': {'expected': 'Edit v1', 'discovered': 'Only v2 exists'},
        }], source_id='exp-1'),
    }), True)
    inner = SimpleNamespace(
        names=('read', 'explore'),
        tool_specs_json=encode({'read': {}, 'explore': {}}),
        session_id='sess-1',
        binding='test-binding',
        execute=lambda req, ctrl: drift_res,
    )
    conv = tasks.ConversationTools(inner, session, exp_state, 'inv-1', permission_directory=None)
    res = conv.execute(_effect('explore', {'investigations': []}, 'e1'), _control())
    assert res.status == 'succeeded'
    assert 'goal_drift' in json.loads(res.output_json)

    # Opening a new demand in the same turn is blocked until a fresh user message arrives.
    blocked = conv.execute(_effect('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Edit v2', 'criteria': ['done']}, 'd1'), _control())
    assert blocked.status == 'refused'
    assert 'goal drift' in json.loads(blocked.output_json)['reason'].lower()

    conv._host_input_id = 'user-msg-2'
    ok = conv.execute(_effect('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Edit v2', 'criteria': ['done']}, 'd2'), _control())
    assert ok.status == 'succeeded'


def test_confirm_checks_headless_refuses_and_tui_modal_defaults_to_do_not_save(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    perm_dir = tmp_path / 'perms'
    perm_dir.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)

    state = {'session_id': 'sess-1', 'config': {'repositories': {'proj': str(project)}, 'verifiers': {}}}
    proposal = {'checks': {'unit': {'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 45}}}

    # Headless (directory=None) fails closed and never writes .tare/checks.json.
    headless = permissions.confirm_checks(
        session, state, project=project, proposal=proposal, from_role='planner',
        summary='Add unit check', reason='Enable pytest.', effect_id='e1', control=_control(), directory=None)
    assert headless['status'] == 'CONSENT_REQUIRED'
    assert headless['saved'] is False
    assert not (project / '.tare' / 'checks.json').exists()

    # Interactive TUI: verify ExecutionPermissionPanel focuses Do not save by default, and answer() rejects non-once/deny.
    from relay.execution_permission_panel import ExecutionPermissionPanel
    from textual.app import App

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            permissions.confirm_checks, session, state, project=project, proposal=proposal,
            from_role='planner', summary='Add unit check', reason='Enable pytest.',
            effect_id='e2', control=_control(), directory=perm_dir)
        req = None
        for _ in range(100):
            req = permissions.pending(session)
            if req:
                break
            import time
            time.sleep(0.02)
        assert req is not None and req['kind'] == 'checks'

        # Invalid choices (project/change) must be rejected.
        with pytest.raises(ValueError, match='Save .tare/checks.json or do not save it'):
            permissions.answer(session, req['id'], 'project', project=project,
                               directory=perm_dir, control_root=relay_mesh.RELAY_ROOT)

        # Render modal in headless Textual app and verify default focus is #permission-deny.
        class Harness(App):
            pass

        async def check_panel():
            app = Harness()
            answers = []
            async with app.run_test(size=(110, 32)) as pilot:
                panel = ExecutionPermissionPanel(req)
                app.push_screen(panel, answers.append)
                await pilot.pause()
                assert app.focused is not None and app.focused.id == 'permission-deny'
                # Clicking Save .tare/checks.json dismisses with 'once'.
                await pilot.click('#permission-once')
                await pilot.pause()
            return answers

        answers = asyncio.run(check_panel())
        assert answers == ['once']

        permissions.answer(session, req['id'], 'once', project=project,
                           directory=perm_dir, control_root=relay_mesh.RELAY_ROOT)
        outcome = fut.result(timeout=5)

    assert outcome['status'] == 'SAVED'
    assert outcome['saved'] is True
    assert outcome['active_checks'] == ['unit']
    assert (project / '.tare' / 'checks.json').is_file()
    # Immediately visible in detected_checks() in the same conversation!
    detected = tasks.detected_checks(state['config'])
    assert 'unit' in detected
    assert detected['unit']['argv'] == [sys.executable, '-m', 'pytest', '-q']


def test_coordinator_propose_checks_denial_blocks_same_turn_repeat(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    perm_dir = tmp_path / 'perms'
    perm_dir.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)

    state = {'session_id': 'sess-1', 'config': {'repositories': {'proj': str(project)}, 'verifiers': {}}}
    inner = SimpleNamespace(names=('read',), tool_specs_json=encode({'read': {}}),
                            session_id='sess-1', binding='test-binding')
    tools = tasks.ConversationTools(inner, session, state, 'inv-1', permission_directory=perm_dir)
    assert 'propose_checks' in tools.names

    # Verify all schema maxLength constraints remain <= 2000 for local llama.cpp grammars.
    def max_lengths(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get('maxLength'), int):
                yield obj['maxLength']
            for v in obj.values():
                yield from max_lengths(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from max_lengths(v)

    assert all(v <= 2000 for v in max_lengths(json.loads(tools.tool_specs_json)))

    # Deny the first proposal via confirm_checks stub.
    monkeypatch.setattr(permissions, 'confirm_checks', lambda *a, **kw: {
        'status': 'DENIED', 'saved': False, 'path': '.tare/checks.json', 'from_role': 'coordinator'})
    args = {
        'summary': 'Add unit check',
        'reason': 'Run pytest.',
        'checks': [{'name': 'unit', 'argv': ['python', '-m', 'pytest']}],
    }
    first = tools.execute(_effect('propose_checks', args, 'e1'), _control())
    assert first.status == 'succeeded'
    assert json.loads(first.output_json)['status'] == 'DENIED'

    # Repeating in the same user turn is refused to prevent modal spam.
    second = tools.execute(_effect('propose_checks', args, 'e2'), _control())
    assert second.status == 'refused'
    assert 'declined' in json.loads(second.output_json)['reason'].lower()


def test_planner_signals_apply_checks_before_task_plan_and_goal_drift_blocks_same_turn(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    app_file = project / 'app.py'
    app_file.write_text('x = 1\n', encoding='utf-8')
    sha = hashlib.sha256(app_file.read_bytes()).hexdigest()
    session = tmp_path / 'session'
    (session / 'planning' / 'job1' / 'session').mkdir(parents=True)
    perm_dir = tmp_path / 'perms'
    perm_dir.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)

    # 1. Planner emits propose_checks + executable task referencing the newly proposed check 'unit-py'.
    plan_payload = _base_plan(
        sha=sha,
        tasks_list=[{
            'key': 'fix-app',
            'title': 'Fix app',
            'summary': 'Update app.py',
            'cluster': 'core',
            'priority': 'P1',
            'criteria': ['app.py updated'],
            'target_paths': ['app.py'],
            'depends_on': [],
            'verifiers': ['unit-py'],
            'test_paths': [],
        }],
        signals=[{
            'kind': 'propose_checks',
            'summary': 'Register unit-py check',
            'reason': 'Project needs pytest verifier.',
            'payload': {'checks': [{'name': 'unit-py', 'argv': ['python', '-m', 'pytest', '-q']}]},
        }],
    )
    artifact = session / 'planning' / 'job1' / 'session' / 'artifact.json'
    save_json(artifact, {
        'schema': 'tare.tools/native-artifact/1', 'role': 'planner', 'kind': 'plan',
        'authority': 'NONE', 'session_id': 'child-1', 'call_id': 'c1',
        'profile_digest': '0' * 64, 'payload': native_artifacts.validate('planner', plan_payload),
    })
    handoff = planning_tools.handoff(artifact, plan_payload)
    assert handoff['agent_signals'][0]['from_role'] == 'planner'
    assert handoff['agent_signals'][0]['authority'] == 'ADVISORY_ONLY'

    # Simulate user clicking "Save .tare/checks.json" inside confirm_checks.
    def auto_approve(root, state, *, project, proposal, from_role, summary, reason, effect_id, control, directory, task=None):
        prep = agent_signals.prepare_checks_proposal(project, proposal)
        _, _, parsed = turn_engine.write_project_checks_doc(project, prep['document'], expected_sha256=prep['existing_sha256'])
        return {'status': 'SAVED', 'saved': True, 'active_checks': sorted(parsed['verifiers'])}

    monkeypatch.setattr(permissions, 'confirm_checks', auto_approve)
    monkeypatch.setattr(tasks, 'assess_intent', lambda *a, **kw: {'status': 'UNDERSTOOD', 'assessment': {'choice': 'deliver'}})

    state = {
        'session_id': 'sess-1',
        'config': {'repositories': {'proj': str(project)}, 'verifiers': {}},
    }
    inner = SimpleNamespace(
        names=('read', 'delegate_plan', 'explore'),
        tool_specs_json=encode({'read': {}, 'delegate_plan': {}, 'explore': {}}),
        session_id='sess-1',
        binding='test-binding',
        execute=lambda req, ctrl: EffectResult(req.effect_id, 'succeeded', encode(handoff), True),
    )
    tools = tasks.ConversationTools(inner, session, state, 'inv-1', permission_directory=perm_dir)
    assert tools.execute(_effect('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Fix app', 'criteria': ['app.py updated']}, 'd1'), _control()).status == 'succeeded'

    res = tools.execute(_effect('delegate_plan', {'instruction': 'Plan fix'}, 'p1'), _control())
    assert res.status == 'succeeded'
    body = json.loads(res.output_json)
    assert body['checks_proposal_outcome']['saved'] is True
    # Because checks.json was written BEFORE task_plan compiled, 'unit-py' is already admitted in execution_checks!
    assert 'unit-py' in body['execution_checks']['names']

    # 2. Planner emitting goal_drift blocks same-turn implement and revise until a fresh user message arrives.
    drift_plan = _base_plan(
        sha=sha,
        tasks_list=[],
        signals=[{
            'kind': 'goal_drift',
            'summary': 'Objective conflicts with codebase',
            'reason': 'app.py uses v2 schema.',
            'payload': {'expected': 'Edit v1', 'discovered': 'Only v2 exists', 'options': ['Edit v2', 'Abort']},
        }],
    )
    artifact2 = session / 'planning' / 'job1' / 'session' / 'artifact2.json'
    save_json(artifact2, {
        'schema': 'tare.tools/native-artifact/1', 'role': 'planner', 'kind': 'plan',
        'authority': 'NONE', 'session_id': 'child-2', 'call_id': 'c2',
        'profile_digest': '0' * 64, 'payload': native_artifacts.validate('planner', drift_plan),
    })
    drift_handoff = planning_tools.handoff(artifact2, drift_plan)
    inner.execute = lambda req, ctrl: EffectResult(req.effect_id, 'succeeded', encode(drift_handoff), True)

    res2 = tools.execute(_effect('delegate_plan', {'instruction': 'Plan v1'}, 'p2'), _control())
    assert res2.status == 'succeeded'
    body2 = json.loads(res2.output_json)
    assert len(body2['goal_drift']) == 1
    assert 'goal drift' in body2['next'].lower()

    # Same-turn implement and demand(relation=revise|new) are blocked.
    imp_blocked = tools.execute(_effect('implement', {}, 'i1'), _control())
    assert imp_blocked.status == 'refused'
    assert 'goal drift' in json.loads(imp_blocked.output_json)['reason'].lower()

    rev_blocked = tools.execute(_effect('demand', {'intent': 'deliver', 'relation': 'revise', 'objective': 'Edit v2'}, 'd2'), _control())
    assert rev_blocked.status == 'refused'
    assert 'goal drift' in json.loads(rev_blocked.output_json)['reason'].lower()

    new_blocked = tools.execute(_effect('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Edit v2'}, 'd3'), _control())
    assert new_blocked.status == 'refused'
    assert 'goal drift' in json.loads(new_blocked.output_json)['reason'].lower()

    # Fresh operator message lifts the lock cleanly.
    tools._host_input_id = 'user-msg-2'
    rev_ok = tools.execute(_effect('demand', {'intent': 'deliver', 'relation': 'revise', 'objective': 'Edit v2'}, 'd4'), _control())
    assert rev_ok.status == 'succeeded'


def test_explorer_signals_stamped_and_routed_without_side_effects_headless(tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)

    exp_state = {
        'session_id': 'sess-1',
        'config': {'role': 'coordinator', 'repositories': {'proj': str(project)}, 'verifiers': {}},
        'limits': {'max_model_calls': None},
        'delegation': {'settings': {}, 'variant': 'default'},
    }
    base = SimpleNamespace(names=('read',), tool_specs_json=encode({'read': {'description': 'r', 'parameters': {}}}),
                           session_id='sess-1', binding='test-binding')
    exp_tools = delegation_tools.ExplorationTools(base, session, exp_state)
    rows = [{
        'id': 'exp-1',
        'status': 'COMPLETED',
        'profile': 'default',
        'repository_root': str(project),
        'report': {
            'summary': 'Found test layout.',
            'sources': [],
            'remaining': [],
            'signals': [
                {
                    'kind': 'propose_checks',
                    'summary': 'Add unit check',
                    'reason': 'Detected pytest.',
                    'payload': {'checks': {'unit': {'argv': ['python', '-m', 'pytest']}}},
                },
                {
                    'kind': 'out_of_scope_blocker',
                    'summary': 'Pre-existing flake',
                    'reason': 'Unrelated test fails.',
                    'payload': {'paths': ['tests/smoke.gd'], 'symptom': 'Flaky timing'},
                },
            ],
        },
    }]
    exp_result = exp_tools.result(_effect('explore', {'investigations': []}), rows)
    exp_body = json.loads(exp_result.output_json)
    assert len(exp_body['agent_signals']) == 2
    assert all(s['from_role'] == 'explorer' and s['authority'] == 'ADVISORY_ONLY' for s in exp_body['agent_signals'])

    # Routed through ConversationTools headless (permission_directory=None): proposes check fails closed, blocker surfaced.
    inner = SimpleNamespace(
        names=('read', 'explore'),
        tool_specs_json=encode({'read': {}, 'explore': {}}),
        session_id='sess-1',
        binding='test-binding',
        execute=lambda req, ctrl: exp_result,
    )
    conv = tasks.ConversationTools(inner, session, exp_state, 'inv-1', permission_directory=None)
    routed_res = conv.execute(_effect('explore', {'investigations': []}, 'e1'), _control())
    assert routed_res.status == 'succeeded'
    routed_body = json.loads(routed_res.output_json)
    assert routed_body['checks_proposal_outcome']['status'] == 'CONSENT_REQUIRED'
    assert routed_body['checks_proposal_outcome']['saved'] is False
    assert not (project / '.tare' / 'checks.json').exists()
    assert len(routed_body['out_of_scope_blockers']) == 1
    assert routed_body['out_of_scope_blockers'][0]['from_role'] == 'explorer'


def test_signal_tools_schema_caps_isolation_and_custody_persistence(tmp_path):
    from compute_plane.native_cli import cli_request
    from relay import native_agent

    # Strict Codex CLI schema compilation & local llama.cpp <=2000 maxLength check.
    spec = agent_signals.REPORT_SIGNAL_SPEC['report_signal']
    req = cli_request(dict(model='fixture', messages=[], tools=[{'function': {'name': 'report_signal', 'parameters': spec['parameters']}}]),
                      structured=True, native_arguments=True)
    assert 'report_signal' in req['prompt']

    def max_lengths(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get('maxLength'), int):
                yield obj['maxLength']
            for v in obj.values():
                yield from max_lengths(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from max_lengths(v)

    assert all(v <= 2000 for v in max_lengths(spec['parameters']))

    # Only delegated implementers may configure agent_signals=True (never auditors/planners/explorers).
    repo = tmp_path / 'repo'
    repo.mkdir()
    for role in ('auditor', 'plan-auditor', 'planner', 'explorer'):
        with pytest.raises(ValueError, match='agent_signals is admitted only'):
            native_agent.configuration({
                'schema': 'tare.tools/native-agent-config/1', 'role': role,
                'repositories': {'proj': str(repo)}, 'writable': {}, 'verifiers': {},
                'agent_signals': True,
            })

    custody = tmp_path / 'worker-session'
    custody.mkdir()
    inner = SimpleNamespace(names=('read', 'edit'), tool_specs_json=encode({'read': {}, 'edit': {}}),
                            session_id='sess-1', binding='test-binding')
    sig_tools = agent_signals.SignalTools(inner, custody, role='implementer')
    assert 'report_signal' in sig_tools.names

    # Reject cross-kind field mixing (expected with propose_checks).
    bad = sig_tools.execute(_effect('report_signal', {
        'kind': 'propose_checks', 'summary': 'Bad mix', 'reason': 'Mixing fields.',
        'checks': [{'name': 'unit', 'argv': ['python', '-m', 'pytest']}],
        'expected': 'should not be allowed',
    }, 's-bad'), _control())
    assert bad.status == 'refused'
    assert 'not allowed' in json.loads(bad.output_json)['reason'].lower()

    # Record 1 propose_checks, 1 out_of_scope_blocker, 1 goal_drift (cap = 3).
    r1 = sig_tools.execute(_effect('report_signal', {
        'kind': 'propose_checks', 'summary': 'Add smoke check', 'reason': 'No checks.json.',
        'checks': [{'name': 'smoke', 'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 60}],
    }, 's1'), _control())
    assert r1.status == 'succeeded' and json.loads(r1.output_json)['remaining_signals'] == 2

    # Idempotent replay on same effect_id does not consume quota.
    r1_dup = sig_tools.execute(_effect('report_signal', {
        'kind': 'propose_checks', 'summary': 'Add smoke check', 'reason': 'No checks.json.',
        'checks': [{'name': 'smoke', 'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 60}],
    }, 's1'), _control())
    assert r1_dup.status == 'succeeded' and json.loads(r1_dup.output_json)['reused'] is True

    # Second distinct propose_checks in same session refused.
    r1_second = sig_tools.execute(_effect('report_signal', {
        'kind': 'propose_checks', 'summary': 'Another checks', 'reason': 'Duplicate.',
        'checks': [{'name': 'unit', 'argv': ['python', '-m', 'pytest']}],
    }, 's1b'), _control())
    assert r1_second.status == 'refused'

    r2 = sig_tools.execute(_effect('report_signal', {
        'kind': 'out_of_scope_blocker', 'summary': 'Flaky smoke test', 'reason': 'Unrelated scene.',
        'symptom': 'Random timeout in smoke.gd', 'paths': ['tests/smoke.gd'],
        'suggested_follow_up': 'Fix smoke.gd separately.',
    }, 's2'), _control())
    assert r2.status == 'succeeded' and json.loads(r2.output_json)['remaining_signals'] == 1

    r3 = sig_tools.execute(_effect('report_signal', {
        'kind': 'goal_drift', 'summary': 'API v1 removed', 'reason': 'Only v2 exists.',
        'expected': 'Update v1 endpoint', 'discovered': 'v1 was deleted; v2 is active',
        'proposed_objective': 'Update v2 endpoint', 'options': ['Update v2', 'Keep v1'],
    }, 's3'), _control())
    assert r3.status == 'succeeded' and json.loads(r3.output_json)['remaining_signals'] == 0

    # 4th signal rejected (max 3/session).
    r4 = sig_tools.execute(_effect('report_signal', {
        'kind': 'out_of_scope_blocker', 'summary': 'Fourth', 'reason': 'Over cap.',
        'symptom': 'over limit',
    }, 's4'), _control())
    assert r4.status == 'refused'
    assert 'at most 3' in json.loads(r4.output_json)['reason'].lower()

    # Host re-stamps signals cleanly when reading custody store.
    emitted = agent_signals.read_session_signals(custody, role='implementer')
    assert [s['kind'] for s in emitted] == ['propose_checks', 'out_of_scope_blocker', 'goal_drift']
    assert all(s['from_role'] == 'implementer' and s['authority'] == 'ADVISORY_ONLY' for s in emitted)

    # Crash-consistent recover_effect: if signal was saved before journaling EffectResult, recover returns RECORDED.
    from tare_tools_agent_runtime import ConversationLog
    j_saved = ConversationLog()
    j_saved.append({'kind': 'effect_requested', 'session_id': 'sess-1', 'effect_id': 's1',
                    'tool_call': {'call_id': 's1', 'name': 'report_signal', 'arguments_json': '{}'}})
    _, rec_saved = sig_tools.recover_effect(j_saved)
    assert rec_saved.status == 'succeeded' and json.loads(rec_saved.output_json)['recovered'] is True

    j_unsaved = ConversationLog()
    j_unsaved.append({'kind': 'effect_requested', 'session_id': 'sess-1', 'effect_id': 's-missing',
                      'tool_call': {'call_id': 's-missing', 'name': 'report_signal', 'arguments_json': '{}'}})
    _, rec_unsaved = sig_tools.recover_effect(j_unsaved)
    assert rec_unsaved.status == 'refused'


def test_work_unit_implementer_signals_goal_drift_isolation_and_heads_up(tmp_path, monkeypatch):
    import subprocess
    from relay import heads_up, native_agent, work_forms, work_units

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True, capture_output=True)
    (repo / 'app.py').write_text('value = 1\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'app.py'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, check=True, capture_output=True)

    custody = tmp_path / 'custody'
    custody.mkdir()
    fake_dep = {'id': 'fake-imp', 'native_profile': {'model': 'm', 'protocol': 'openai-chat', 'context_window': 64000, 'output_tokens': 4000}}
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('fake-imp', fake_dep)]))
    monkeypatch.setattr(work_units, 'local_slot', lambda *a, **kw: __import__('contextlib').nullcontext())

    config = {
        'schema': 'tare.tools/native-agent-config/1',
        'role': 'implementer',
        'conversation_flow': True,
        'repositories': {'proj': str(repo)},
        'writable': {'proj': ['app.py']},
        'verifiers': {'conversation-diff': {'repository': 'proj', 'argv': [sys.executable, '-c', 'pass']}},
    }
    state = {'session_id': 'sess-1', 'conversation_flow': True, 'config': config,
             'limits': {'max_model_calls': None, 'max_seconds': None},
             'deployment': {'id': 'coord', 'native_profile': {'model': 'coord'}}}
    unit = {'key': 'u1', 'title': 'Update app', 'repository': 'proj', 'target_paths': ['app.py'],
            'criteria': ['value updated'], 'depends_on': [], 'verifiers': ['conversation-diff'], 'test_paths': []}
    work_forms.record_plan(custody, config, {'units': [unit], 'criteria': ['value updated']})

    # 1. Implementer emits goal_drift -> skips auditor & apply, preserves candidate, does NOT trip failure ledger.
    auditor_called = []

    def fake_worker_drift(child, dep, session_path, instruction, **kw):
        assert child.get('agent_signals') is True
        tree = Path(child['repositories']['proj'])
        (tree / 'app.py').write_text('value = 2\n', encoding='utf-8')
        return {
            'status': 'PROPOSED', 'model_calls': 1, 'output': 'Found goal drift.',
            'agent_signals': agent_signals.stamp_signals('implementer', [{
                'kind': 'goal_drift', 'summary': 'v1 missing', 'reason': 'Codebase moved to v2.',
                'payload': {'expected': 'Edit v1', 'discovered': 'Only v2 exists', 'options': ['Edit v2', 'Stop']},
            }], source_id='w1'),
        }

    def spy_review(tree, producer, u, changed, checks):
        auditor_called.append((u, checks))
        return {'verdict': 'ACCEPT', 'summary': 'ok', 'findings': [], 'remaining': [], 'sources': []}

    monkeypatch.setattr(native_agent, 'run_session', fake_worker_drift)
    res_drift = work_units.run_unit(custody, state, {'unit': 'u1'}, lambda: False, review=spy_review)
    assert res_drift['verdict'] == 'INCONCLUSIVE'
    assert res_drift['applied'] is False
    assert res_drift['goal_drift_stop'] is True
    assert res_drift.get('candidate') is not None
    assert auditor_called == []
    assert (repo / 'app.py').read_text(encoding='utf-8') == 'value = 1\n'
    # Must not trip check_failures guard.
    work_units.check_failures(custody, unit, conversation=True)

    # 2. Implementer emits out_of_scope_blocker + auditor emits out_of_scope_blocker; auditor never receives implementer signals.
    def fake_worker_blocker(child, dep, session_path, instruction, **kw):
        tree = Path(child['repositories']['proj'])
        (tree / 'app.py').write_text('value = 2\n', encoding='utf-8')
        return {
            'status': 'PROPOSED', 'model_calls': 1, 'output': 'Updated value.',
            'agent_signals': agent_signals.stamp_signals('implementer', [{
                'kind': 'out_of_scope_blocker', 'summary': 'Unrelated smoke flake', 'reason': 'Fails on main.',
                'payload': {'paths': ['tests/smoke.gd'], 'symptom': 'Flaky timeout'},
            }], source_id='w2'),
        }

    def spy_review_with_signal(tree, producer, u, changed, checks):
        auditor_called.append((u, checks))
        # Strict Implementer <-> Auditor isolation: neither unit nor checks carries implementer agent_signals.
        assert 'agent_signals' not in u and 'agent_signals' not in checks
        return {
            'verdict': 'ACCEPT', 'summary': 'Looks good.', 'findings': [], 'remaining': [], 'sources': [],
            'agent_signals': agent_signals.stamp_signals('auditor', [{
                'kind': 'out_of_scope_blocker', 'summary': 'CI lint warning elsewhere', 'reason': 'Pre-existing.',
                'payload': {'symptom': 'Unused import in unrelated file'},
            }], source_id='aud-1'),
        }

    monkeypatch.setattr(native_agent, 'run_session', fake_worker_blocker)
    res_ok = work_units.run_unit(custody, state, {'unit': 'u1'}, lambda: False, review=spy_review_with_signal)
    assert res_ok['applied'] is True
    assert len(auditor_called) == 1
    assert [s['from_role'] for s in res_ok['agent_signals']] == ['implementer', 'auditor']

    # 3. delivery_view() and heads_up._detail() surface blockers, goal_drift, and checks_proposal_outcome cleanly
    #    without triplicating signals into audit.agent_signals or top-level agent_signals.
    view = tasks.delivery_view({
        **res_drift,
        'audit': {'verdict': 'ACCEPT', 'summary': 'ok', 'findings': [], 'remaining': [],
                  'agent_signals': res_ok['agent_signals']},
        'goal_drift': res_drift['agent_signals'],
        'out_of_scope_blockers': res_ok['agent_signals'],
        'checks_proposal_outcome': {'status': 'SAVED', 'saved': True},
    }, config=config, unit=unit)
    assert 'agent_signals' not in view
    assert 'agent_signals' not in view['audit']
    assert 'goal drift' in view['next'].lower()
    assert 'out_of_scope_blockers' in view['next']
    assert 'recheck_candidate=true' in view['next']

    detail = heads_up._detail('implement', {'note': 'Fix app'}, view)
    assert 'goal_drift (implementer): v1 missing' in detail
    assert 'out_of_scope_blocker (implementer): Unrelated smoke flake' in detail
    assert 'checks_proposal: SAVED' in detail


def test_multirepo_propose_checks_activates_in_catalog_with_disambiguation_and_honest_explicit_override(tmp_path, monkeypatch):
    repo1 = tmp_path / 'repo1'
    repo2 = tmp_path / 'repo2'
    repo1.mkdir()
    repo2.mkdir()
    session = tmp_path / 'session'
    session.mkdir()
    perm_dir = tmp_path / 'perms'
    perm_dir.mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)

    # Approve via wait_for_answer without launching a full TUI or executing any real check command.
    monkeypatch.setattr(permissions, 'wait_for_answer', lambda folder, req, ctrl: {'id': req.get('id', ''), 'choice': 'once'})

    state = {
        'session_id': 'sess-1',
        'config': {'repositories': {'repo1': str(repo1), 'repo2': str(repo2)}, 'verifiers': {}},
    }
    inner = SimpleNamespace(names=('read',), tool_specs_json=encode({'read': {}}),
                            session_id='sess-1', binding='test-binding')
    tools = tasks.ConversationTools(inner, session, state, 'inv-1', permission_directory=perm_dir)

    # 1. Approve a harmless check in repo2; verify catalog immediately exposes it bound to repo2 (no real command run).
    res = tools.execute(_effect('propose_checks', {
        'repository': 'repo2',
        'summary': 'Add repo2 lint check',
        'reason': 'Verify repo2 style.',
        'checks': [{'name': 'repo2-lint', 'argv': ['python', '-c', 'pass'], 'timeout_seconds': 30}],
    }, 'e1'), _control())
    assert res.status == 'succeeded'
    body = json.loads(res.output_json)
    assert body['saved'] is True
    assert body['active_in_conversation'] is True
    assert body['active_checks'] == ['repo2-lint']

    detected = tasks.detected_checks(state['config'])
    assert detected == {'repo2-lint': {'repository': 'repo2', 'argv': [sys.executable, '-c', 'pass']}}
    assert 'repo2-lint' in tasks.product_help(state, 'workflow')['available_checks']

    # Query check catalog via an unknown name (does not execute any command) and confirm repo2-lint is registered on repo2.
    probe = tools.execute(_effect('check', {'names': ['nonexistent']}, 'e2'), _control())
    assert probe.status == 'refused'
    catalog = json.loads(probe.output_json)['available_checks']
    assert catalog['repo2-lint'] == {'repository': 'repo2', 'argv': [sys.executable, '-c', 'pass']}

    # 2. Colliding check names across repo1 and repo2 are disambiguated as <repo>:<name>.
    turn_engine.write_project_checks_doc(repo1, {'checks': {'smoke': {'argv': ['python', '-c', 'print(1)']}}})
    res2 = tools.execute(_effect('propose_checks', {
        'repository': 'repo2',
        'summary': 'Add repo2 smoke check',
        'reason': 'Run repo2 smoke.',
        'checks': [{'name': 'smoke', 'argv': ['python', '-c', 'print(2)']}],
    }, 'e3'), _control())
    assert res2.status == 'succeeded'
    body2 = json.loads(res2.output_json)
    assert body2['active_checks'] == ['repo2-lint', 'repo2:smoke']
    detected2 = tasks.detected_checks(state['config'])
    assert detected2['repo1:smoke']['repository'] == 'repo1'
    assert detected2['repo2:smoke']['repository'] == 'repo2'
    assert 'smoke' not in detected2

    # 3. When explicit scoped verifiers exist, saving .tare/checks.json does NOT claim immediate activation.
    explicit_state = {
        'session_id': 'sess-2',
        'config': {
            'repositories': {'repo1': str(repo1)},
            'verifiers': {'scoped': {'repository': 'repo1', 'argv': [sys.executable, '-c', 'pass']}},
        },
    }
    captured_requests = []

    def capture_approve(folder, req, ctrl):
        captured_requests.append(deepcopy(req))
        return {'id': req.get('id', ''), 'choice': 'once'}

    monkeypatch.setattr(permissions, 'wait_for_answer', capture_approve)
    out = permissions.confirm_checks(
        session, explicit_state, project=repo1,
        proposal={'checks': [{'name': 'extra', 'argv': ['python', '-c', 'pass']}]},
        from_role='coordinator', summary='Add extra check', reason='For future runs.',
        effect_id='e4', control=_control(), directory=perm_dir)
    assert out['saved'] is True
    assert out['active_in_conversation'] is False
    assert out['active_checks'] == []
    assert 'explicit scoped verifiers' in out['next'].lower()
    assert captured_requests[0]['checks_proposal']['active_now'] is False
    assert tasks.detected_checks(explicit_state['config']) == {}


def test_implement_refuses_blind_retry_on_unchanged_out_of_scope_blocker_until_checks_or_file_changes(
        tmp_path, monkeypatch):
    from relay import work_units
    from test_conversation_tasks import effect, host as host_factory, intake, planned, user_reply

    # Build a standard conversation host via pytest helper components.
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'a.py').write_text('value = 1\n', encoding='utf-8')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'visual_review.gd').write_text('var moving_a := 1\nvar moving_a := 2\n', encoding='utf-8')
    session = tmp_path / 'session'; session.mkdir()
    from compute_plane.native_journal import NativeJournal
    journal = NativeJournal(session / 'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'sess-1', 'instruction': 'Update a.py.'})
    state = {
        'session_id': 'sess-1', 'conversation_flow': True,
        'deployment': {'id': 'coord', 'native_profile': {'model': 'coord'}},
        'limits': {'max_model_calls': 30, 'max_seconds': 600}, 'deadline_utc': None,
        'config': {'repositories': {'repo': str(repo)}, 'writable': {'repo': ['a.py']}, 'verifiers': {}},
    }
    inner = SimpleNamespace(names=('read',), tool_specs_json=encode({'read': {}}),
                            session_id='sess-1', binding='test-binding',
                            execute=lambda req, ctrl: None, recover_effect=lambda j: None)
    tools = tasks.ConversationTools(inner, session, state, 'inv-1')
    ctrl = _control()

    # Record demand + saved plan whose only target is a.py (tests/visual_review.gd is out of scope).
    tools.understand({'intent': 'deliver', 'relation': 'new', 'objective': 'Update a.py.', 'criteria': ['value=2']})
    artifact = session / 'planning' / 'p1' / 'session' / 'artifact.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        'schema': 'tare.tools/native-artifact/1', 'role': 'planner', 'payload': {
            'summary': 'Update a.py.', 'steps': ['Edit a.py'], 'criteria': ['value=2'],
            'remaining': [], 'sources': [], 'tasks': [{
                'key': 't1', 'title': 'Update a.py', 'repository': 'repo',
                'target_paths': ['a.py'], 'criteria': ['value=2'], 'depends_on': [],
                'verifiers': [], 'test_paths': [],
            }],
        },
    }), encoding='utf-8')
    val = tasks.load(session); task = tasks.current(val)
    tasks.task_plan(session, state['config'], task, {'artifact': str(artifact)})
    tasks.save(session, val)

    monkeypatch.setattr('relay.conversation_permissions.authorize', lambda *a, **kw: state['config'])
    monkeypatch.setattr(work_units, 'check_failures', lambda *a, **kw: None)
    monkeypatch.setattr(work_units, 'child_limits', lambda *a, **kw: {'max_model_calls': 10, 'max_seconds': 300})
    run_calls = []

    def fake_run_unit(custody, st, args, stop, **kw):
        run_calls.append(args)
        return {
            'verdict': 'NOT_VERIFIED', 'applied': False, 'files': ['a.py'], 'route': 'worker',
            'reason': 'pre-existing check failure', 'already_red': ['godot-tests:tests/visual_review.gd'],
            'agent_signals': agent_signals.stamp_signals('implementer', [{
                'kind': 'out_of_scope_blocker',
                'summary': 'Parse error in tests/visual_review.gd',
                'reason': 'Duplicate var moving_a outside target_paths.',
                'payload': {'paths': ['tests/visual_review.gd'], 'symptom': 'Parse error at line 2'},
            }], source_id='w1'),
        }

    monkeypatch.setattr(work_units, 'run_unit', fake_run_unit)
    first = tools.execute(_effect('implement', {}, 'imp-1'), ctrl)
    assert first.status == 'succeeded', first.output_json
    assert len(run_calls) == 1
    body = json.loads(first.output_json)
    # No contradictory "Never ... open a replacement task" or "use relation=correct and implement" guidance.
    assert 'open a replacement task' not in body['next']
    assert 'out_of_scope_blockers' in body['next']
    assert 'propose_checks' in body['next']
    no_red_view = tasks.delivery_view({
        'verdict': 'NOT_VERIFIED', 'applied': False, 'files': ['a.py'],
        'out_of_scope_blockers': body['out_of_scope_blockers'],
    })
    assert 'use relation=correct and implement' not in no_red_view['next']
    assert 'Do not retry implement unchanged' in no_red_view['next']

    # Fresh user turn: repeating implement() unchanged is refused cheaply with zero run_unit dispatch.
    journal.append({'kind': 'user_message', 'message_id': 'u2', 'text': 'Try again.'})
    tools.invocation_id = 'inv-2'
    tools.understand({'intent': 'deliver', 'relation': 'continue'})
    second = tools.execute(_effect('implement', {}, 'imp-2'), ctrl)
    assert second.status == 'refused' and len(run_calls) == 1
    assert 'tests/visual_review.gd' in second.output_json and 'propose_checks' in second.output_json

    # Once .tare/checks.json is updated (e.g. via propose_checks), the guard clears and implement() dispatches.
    turn_engine.write_project_checks_doc(repo, {
        'checks': {'visual-review': {'mode': 'gui', 'paths': ['tests/visual_review.gd']}}
    })
    third = tools.execute(_effect('implement', {}, 'imp-3'), ctrl)
    assert third.status == 'succeeded' and len(run_calls) == 2

    # Editing the blocked out-of-scope file itself also clears the guard on the next turn.
    journal.append({'kind': 'user_message', 'message_id': 'u3', 'text': 'Fixed visual_review.gd.'})
    tools.invocation_id = 'inv-4'
    tools.understand({'intent': 'deliver', 'relation': 'continue'})
    (repo / 'tests' / 'visual_review.gd').write_text('var moving_a := 1\nvar home_moving_a := 2\n', encoding='utf-8')
    fourth = tools.execute(_effect('implement', {}, 'imp-4'), ctrl)
    assert fourth.status == 'succeeded' and len(run_calls) == 3
