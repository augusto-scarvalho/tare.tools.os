from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import json
import time
from pathlib import Path

import pytest

from relay.execution_team import actor, observe_session, reasoning, saved_rows
from relay.native_activity import activity_view, saved_history
from test_model_choices import profile


def state(role='coordinator', dep=None, identity='parent'):
    return {'session_id': identity, 'conversation_role': role, 'instruction': 'Review the requested change.',
            'config': {'role': 'implementer'}, 'deployment': dep or profile('cli://codex', 'gpt-6.1-sol', 'high')}


def write_state(path, project, value, status='PROPOSED'):
    path.mkdir(parents=True, exist_ok=True)
    value = {**deepcopy(value), 'config': {**value['config'], 'repositories': {'repo': str(project)}},
             'invocations': [{'id': 'attempt', 'status': status}]}
    (path/'native-agent.json').write_text(json.dumps(value), encoding='utf-8')


def test_local_reasoning_is_not_replaced_by_role_or_metadata_default():
    value = state(dep={'id': 'qwen', 'native_credential_env': 'HIDDEN_KEY',
        'native_model_choice': {'effort': 'high'}, 'native_profile': {'model': 'qwen38-gsq',
        'protocol': 'openai-chat', 'endpoint': 'http://secret:password@host?key=secret',
        'parameters_json': json.dumps({'chat_template_kwargs': {'enable_thinking': True, 'reasoning_effort': 'low'},
                                      'api_key': 'secret'})}})
    result = actor(value)
    assert result['role'] == 'coordinator' and result['reasoning'] == 'low'
    assert result['model'] == 'qwen38-gsq' and 'secret' not in json.dumps(result)
    assert reasoning({'native_profile': {'parameters_json': '{broken'}}) is None
    assert actor(state(dep={'native_profile': {}}))['reasoning'] == 'Not recorded'


def test_nested_children_inherit_observation_without_forwarding_private_reasoning_or_leaking_scope():
    events = []
    parent = state(); original = deepcopy(parent)
    with observe_session(parent, {'id': 'p'}, events.append) as result:
        def child():
            with observe_session(state('planner', identity='child'), {'id': 'c'}) as done:
                done.update(status='PROPOSED', model_calls=3)
        with ThreadPoolExecutor(1) as pool:
            pool.submit(copy_context().run, child).result()
        result.update(status='CANCELLED', reason='Operator stopped execution.')
    assert len(events) == 4
    assert [e['activity']['actor']['role'] for e in events] == ['coordinator', 'planner', 'planner', 'coordinator']
    assert events[2]['activity']['state'] == 'succeeded'
    assert 'not evidence' in events[2]['activity']['detail']
    assert events[-1]['activity']['state'] == 'failed' and parent == original
    with observe_session(state(identity='unrelated'), {'id': 'u'}) as done:
        done.update(status='PROPOSED')
    assert len(events) == 4  # observation cannot escape the originating invocation


def test_host_recovery_is_not_presented_as_a_model_call():
    events=[]
    with observe_session(state(),{'id':'host'},events.append,host_action='RECHECK_CANDIDATE') as done:
        done.update(status='PROPOSED',model_calls=3)
    assert all(e['activity']['actor']['role']=='host' for e in events)
    assert all(e['activity']['actor']['reasoning']=='No coordinator call' for e in events)
    assert 'Model execution started' not in events[0]['activity']['detail']
    with observe_session(state(identity='unrelated'), {'id': 'u'}) as done:
        done.update(status='PROPOSED')
    assert len(events) == 2  # observation cannot escape the originating invocation


def test_observer_failure_or_exception_never_retries_a_model_or_claims_a_proposal():
    events = []
    with pytest.raises(RuntimeError):
        with observe_session(state(), {'id': 'broken'}, events.append):
            raise RuntimeError('model failed')
    assert events[-1]['activity']['state'] == 'unconfirmed'
    def broken(_):
        raise OSError('UI closed')
    with observe_session(state(), {'id': 'safe'}, broken) as done:
        done.update(status='PROPOSED')


def test_parallel_children_serialize_the_public_activity_stream():
    active, overlaps = [], []
    def sink(event):
        active.append(event)
        if len(active) > 1:
            overlaps.append(True)
        time.sleep(.005)
        active.pop()
    def child(number):
        with observe_session(state('explorer', identity=str(number)), {'id': 'child'}) as done:
            done.update(status='PROPOSED')
    with observe_session(state(), {'id': 'parent'}, sink) as done:
        with ThreadPoolExecutor(4) as pool:
            for future in [pool.submit(copy_context().run, child, n) for n in range(4)]:
                future.result()
        done.update(status='PROPOSED')
    assert not overlaps and not active


def test_actor_projection_is_bounded_and_survives_old_checkpoint_categories():
    events = []
    with observe_session(state(), {'id': 'a'}, events.append) as done:
        done.update(status='PROPOSED')
    row = events[-1]['activity']
    saved = saved_history([row])[0]
    assert saved['operation'] == 'read'
    assert activity_view(saved) == row
    with pytest.raises(ValueError, match='actor'):
        activity_view({**row, 'actor': {**row['actor'], 'credential': 'secret'}})


def test_legacy_team_reads_actual_children_not_current_preferences_and_does_not_rewrite(tmp_path, monkeypatch):
    root = tmp_path/'session'; project = tmp_path/'project'; project.mkdir()
    write_state(root, project, state())
    planner = root/'planning'/'job'/'session'
    old = state('planner', profile('cli://codex', 'gpt-6.1-sol', 'low'), 'planner')
    write_state(planner, project, old)
    review = root/'reviews'/'review'/'session'
    write_state(review, project, state('auditor', profile('cli://agy', 'gemini-4-argon-high', 'high'), 'audit'), 'BLOCKED')
    (review.parent/'receipt.json').write_text(json.dumps({'result': {'verdict': 'INCONCLUSIVE', 'reason': 'NO_PROGRESS'}}))
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **k: pytest.fail('Current preference is not execution evidence'))
    rows = saved_rows(root, project)
    assert len(rows) == 4
    assert any('Planner' in r['label'] and 'low' in r['label'] and 'Below minimum high' in r['label'] for r in rows)
    assert any('INCONCLUSIVE' in r['body'] and 'AGY CLI' in r['label'] for r in rows)
    assert all('Explorer' not in r['label'] for r in rows)
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert 'another project' in saved_rows(root, tmp_path/'other')[0]['body']


def test_unreadable_child_is_visible_and_stale_live_rows_cannot_override_finished_custody(tmp_path):
    project = tmp_path/'project'; project.mkdir(); root = tmp_path/'session'
    write_state(root, project, state(), 'CANCELLED')
    events = []
    with observe_session(state(), {'id': 'attempt'}, events.append):
        pass
    broken = root/'planning'/'broken'/'session'; broken.mkdir(parents=True)
    (broken/'native-agent.json').write_text('{broken')
    rows = saved_rows(root, project, [events[0]['activity']])
    assert len(rows) == 2 and 'CANCELLED' in rows[-1]['label']
    assert 'could not be read' in rows[0]['body']


def test_child_redirect_cannot_expose_another_project(tmp_path):
    root = tmp_path/'session'; project = tmp_path/'project'; project.mkdir()
    write_state(root, project, state())
    external = tmp_path/'external'; write_state(external, project, state('planner', identity='secret-model'))
    link = root/'planning'/'escape'/'session'; link.parent.mkdir(parents=True)
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('Symlinks unavailable')
    text = json.dumps(saved_rows(root, project))
    assert 'secret-model' not in text and 'could not be read' in text


@pytest.mark.parametrize('operation,target', [('explore', 'Map how tasks are saved'), ('delegate_plan', ''),
                                              ('implement', ''), ('folder_read', 'docs: a.md')])
def test_a_reopened_conversation_keeps_the_richer_activity_labels(operation, target):
    row = activity_view({'kind': 'tool-activity', 'id': 'e/1', 'operation': operation, 'state': 'succeeded',
                         'target': target, 'duration_ms': 1.0})
    saved = saved_history([row])[0]
    assert saved['operation'] in {'read', 'edit', 'search', 'list'}  # readable by the previous release
    assert activity_view(saved) == row  # not "Edit implement:" or "Search explore: ..." on reopen
