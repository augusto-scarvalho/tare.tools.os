import json
from types import SimpleNamespace

import pytest

from relay.native_activity import ActivityJournal, activity_view, full_output
from relay.native_agent import run_session
from test_native_agent import Client, deployment, setup, steps


def test_activity_follows_real_recorded_effects_and_keeps_reasoning_private(setup):
    config, custody, root = setup
    events = []
    client = Client(steps=steps())
    result = run_session(config, deployment(), custody, 'Change the value and verify.',
                         client=client, local_counter=client, on_activity=events.append)
    assert result['status'] == 'PROPOSED', result
    tools = [event['activity'] for event in events if event['activity']['operation'] != 'agent_run']
    assert [event['state'] for event in tools] == ['running', 'succeeded']*3
    assert events[0]['activity']['operation'] == events[-1]['activity']['operation'] == 'agent_run'
    assert events[-1]['activity']['actor']['role'] == 'implementer'
    rows = result['tool_activity']['items']
    assert [row['operation'] for row in rows] == ['read', 'edit', 'verify']
    assert all(row['duration_ms'] >= 0 and row['receipt'] for row in rows)
    assert 'reasoning retained' not in json.dumps(events)
    assert (root/'a.py').read_bytes() == b'value = 2\n'
    assert json.loads(full_output(custody, root, rows[0]))['text'] == '1|value = 1'
    with pytest.raises(ValueError, match='another project'):
        full_output(custody, root.parent, rows[0])
    with pytest.raises(ValueError, match='could not be verified'):
        full_output(custody, root, {**rows[0], 'receipt': 'wrong'})


def test_broken_activity_sink_does_not_retry_or_change_effects(setup):
    config, custody, root = setup
    client = Client(steps=steps())
    def fail(event):
        raise OSError('Presentation pipe closed')
    result = run_session(config, deployment(), custody, 'Change and verify.', client=client, local_counter=client, on_activity=fail)
    assert result['status'] == 'PROPOSED' and client.generations == 4
    assert result['tool_activity']['observation_failures'] == 6
    assert len(result['tool_activity']['items']) == 3
    assert (root/'a.py').read_bytes() == b'value = 2\n'


class Journal:
    def __init__(self):
        self.events = []; self.fail = False
    def append(self, event):
        if self.fail: raise OSError('Journal append failed')
        self.events.append(event)
        return SimpleNamespace(event=lambda: event, sequence=len(self.events)-1, sha256='a'*64)


def request(identity='tool/1', name='read', **arguments):
    return {'kind': 'effect_requested', 'effect_id': identity,
            'tool_call': {'name': name, 'arguments_json': json.dumps(arguments)}}


def test_projection_bounds_failed_unknown_and_missing_results():
    events = []; journal = Journal(); ticks = iter([1.0, 3.5, 4.0])
    observer = ActivityJournal(journal, events.append, clock=lambda: next(ticks))
    observer.append(request(path='[red]file.py', env={'SECRET': 'do not show'}))
    observer.append({'kind': 'effect_returned', 'result': {'effect_id': 'tool/1', 'status': 'failed',
                     'output_json': json.dumps({'output': 'x'*9000+'\x1b[31m'})}})
    row = observer.presentation()['items'][0]
    assert row['state'] == 'failed' and row['duration_ms'] == 2500
    assert row['truncated'] and len(row['detail']) <= 8000
    assert 'SECRET' not in json.dumps(events) and '\x1b' not in row['detail']
    observer.append(request('tool/2', name='edit', path='file.py'))
    observer.finish()
    assert observer.presentation()['items'][-1]['state'] == 'unconfirmed'
    journal.fail = True
    before = len(events)
    with pytest.raises(OSError): observer.append(request('tool/3'))
    assert len(events) == before  # No activity claims a write that never committed.
    for duration in [-1, float('inf'), float('nan'), True]:
        with pytest.raises(ValueError): activity_view({**row, 'duration_ms': duration})


def test_projection_has_bounded_identity_retention_and_ignores_model_events():
    observer = ActivityJournal(Journal())
    for number in range(120): observer.append(request(str(number), path='file.py'))
    observer.append({'kind': 'model_returned', 'text': 'private reasoning'})
    assert len(observer.rows) == len(observer.started) == 100
    assert observer.omitted == 20 and 'private reasoning' not in json.dumps(observer.presentation())


@pytest.mark.parametrize('kind,state', [('invocation_not_started', 'not_started'), ('invocation_interrupted', 'unknown')])
def test_known_dispatch_state_is_not_replaced_by_generic_missing_result(kind, state):
    observer = ActivityJournal(Journal())
    observer.append(request())
    observer.append({'kind': kind, 'invocation_id': 'tool/1'})
    observer.finish()
    assert observer.presentation()['items'][0]['state'] == state
    observer.append({'kind': kind, 'invocation_id': 'unrelated/model/1'})
    assert len(observer.rows) == 1


def test_command_review_status_and_known_unknown_usage_are_visible_without_inventing_zero():
    journal=Journal();observer=ActivityJournal(journal)
    observer.append(request(identity='command/1',name='command',repository='project',argv=['python','check.py']))
    output={'output':'HOST RESULT','returncode':0,'process':{'command_protection':{
        'review':'both','review_status':'approved','reserved_calls':3,'max_reviews':6,
        'usage':{'classifier':{'input_tokens':12},'codex':None}}}}
    observer.append({'kind':'effect_returned','result':{'effect_id':'command/1','status':'succeeded',
                    'output_json':json.dumps(output)}})
    detail=observer.presentation()['items'][0]['detail']
    assert 'approved' in detail and '3/6' in detail and 'HOST RESULT' in detail
    assert '12' in detail and 'codex usage: not reported' in detail
    assert 'codex usage: 0' not in detail


def test_the_coordinator_progress_sentence_is_narrated_but_not_its_reasoning_or_final_answer():
    events = []
    observer = ActivityJournal(Journal(), events.append, narrate=True)
    def returned(text, *names, delivery=False):
        observer.append({'kind': 'model_returned', 'call_id': 's/model/1', 'reply': {
            'text': text, 'proposes_delivery': delivery, 'complete': True,
            'metadata_json': json.dumps({'assistant': {'reasoning_content': 'PRIVATE'}}),
            'tool_calls': [{'name': name, 'call_id': 'c', 'arguments_json': '{}'} for name in names]}})
    returned('Vou mapear como as tarefas são salvas.', 'explore')
    returned('Pronto.', 'tare_deliver')  # the final answer arrives as the result
    returned('', 'read')
    returned('A plain answer without tools.')
    assert events == [{'channel': 'narration', 'call_id': 's/model/1', 'text': 'Vou mapear como as tarefas são salvas.'}]
    quiet = []
    ActivityJournal(Journal(), quiet.append).append({'kind': 'model_returned', 'call_id': 'x', 'reply': {
        'text': 'Leaf sessions stay quiet.', 'tool_calls': [{'name': 'read'}]}})
    assert quiet == []


@pytest.mark.parametrize('delete', [True, 'true', False, 'false'])
@pytest.mark.parametrize('status', ['succeeded', 'refused', 'failed', 'unknown'])
def test_deletion_activity_never_claims_unconfirmed_success(delete, status):
    observer = ActivityJournal(Journal())
    observer.append(request(name='edit', repository='repo', path='a.py', sha256='a'*64,
        delete=delete, changes=[]))
    assert 'Deleted file.' not in observer.presentation()['items'][0]['detail']
    observer.append({'kind': 'effect_returned', 'result': {'effect_id': 'tool/1',
        'status': status, 'output_json': json.dumps({'sha256': None})}})
    detail = observer.presentation()['items'][0]['detail']
    assert ('Deleted file.' in detail) is (delete in (True, 'true') and status == 'succeeded')


def test_a_refused_row_carries_its_reason():
    observer = ActivityJournal(Journal())
    for identity, status in (('tool/1', 'refused'), ('tool/2', 'succeeded')):
        observer.append(request(identity, path='a.py'))
        observer.append({'kind': 'effect_returned', 'result': {'effect_id': identity, 'status': status,
            'output_json': json.dumps({'reason': 'COORDINATOR_ROUTING_REQUIRED', 'route': 'plan'})}})
    refused, returned = observer.presentation()['items']
    assert refused['reason'] == 'COORDINATOR_ROUTING_REQUIRED' and 'reason' not in returned
    assert '"route": "plan"' in refused['detail']  # the detail stays the exact preview
    assert activity_view({**refused, 'reason': '\x1b[31mred\x1b[0m  and\nsplit'})['reason'] == 'red and split'
    assert 'reason' not in activity_view({**refused, 'state': 'failed'})
    assert activity_view({**refused, 'reason': 'x'*300})['reason'] == 'x'*256
    assert 'reason' not in activity_view({**refused, 'reason': '\x1b[31m\x1b[0m '})
    observer.append(request('tool/3', path='a.py'))
    observer.append({'kind': 'effect_returned', 'result': {'effect_id': 'tool/3', 'status': 'refused',
        'output_json': json.dumps({'error_type': 'TypeError'})}})
    assert observer.presentation()['items'][-1]['reason'] == 'TypeError'


def test_delegated_rows_have_their_own_history_budget():
    from relay.native_activity import retained_history
    own = [{'kind': 'tool-activity', 'id': f'o/{i}'} for i in range(120)]
    delegated = [{'kind': 'tool-activity', 'id': f'd/{i}', 'source': {'mode': 'conversation'}} for i in range(150)]
    kept = retained_history(own[:60] + delegated + own[60:] + ['last words'])
    assert sum('source' in row for row in kept[:-1]) == 100 and sum('source' not in row for row in kept[:-1]) == 100
    assert kept[-1] == 'last words' and [row['id'] for row in kept if 'source' in row][0] == 'd/50'
    assert [row['id'] for row in kept[:-1] if 'source' not in row][0] == 'o/20'


def test_the_session_that_owns_the_callback_is_not_prefixed(setup, tmp_path):
    from relay import flow_activity
    config, custody, root = setup
    events, client = [], Client(steps=steps())
    with flow_activity.stream('conversation', tmp_path, root, lambda event: None):
        run_session(config, deployment(), custody, 'Change and verify.', client=client, local_counter=client,
                    on_activity=events.append)
    assert events and all('source' not in event['activity'] for event in events)
