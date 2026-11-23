"""A reply the output cap cut does not end the session: the host says so and the model goes on."""
import hashlib
import json

from relay import native_agent, output_limit_recovery
from test_conversation_tasks import host  # noqa: F401 (fixture)
from test_native_agent import Client, deployment, response, setup  # noqa: F401 (fixture)

EDIT = ('edit', {'repository': 'repo', 'path': 'a.py', 'search': 'value = 1', 'replace': 'value = 2',
                 'sha256': hashlib.sha256(b'value = 1\n').hexdigest()})
DELIVER = ('tare_deliver', {'summary': 'Set the value to 2.'})


class Cutting(Client):
    """A step 'cut' is a reply the cap ended while the model was still thinking: no text, no tool call."""

    def post(self, path, body, control):
        if path != '/chat/completions':
            return super().post(path, body, control)
        self.calls.append((path, body))
        self.generations += 1
        step = next(self.steps)
        if step != 'cut':
            return response(self.protocol, *step)
        return {'model': 'fixture', 'usage': {'prompt_tokens': 80, 'completion_tokens': 1024, 'total_tokens': 1104},
                'choices': [{'finish_reason': 'length', 'message': {
                    'role': 'assistant', 'content': None, 'reasoning_content': 'still thinking when the cap came'}}]}


def run(setup, steps):
    config, custody, root = setup
    config['verifiers'] = {}
    client = Cutting(steps=steps)
    result = native_agent.run_session(config, deployment(), custody, 'Set the value to 2.', client=client,
                                      local_counter=client, limits={'max_model_calls': 12})
    return result, client, root


def test_a_reply_cut_while_thinking_is_followed_by_the_hosts_message_and_the_work_goes_on(setup):
    result, client, root = run(setup, ['cut', EDIT, DELIVER])
    assert result['status'] == 'PROPOSED' and (root/'a.py').read_bytes() == b'value = 2\n'
    second = [body for path, body in client.calls if path == '/chat/completions'][1]
    told = json.dumps(second['messages'])
    assert 'cut by this route' in told and '1024 tokens' in told and 'no text and no tool call' in told
    assert client.generations == 3 and result['model_calls'] == 3  # the cut call is counted


def test_two_cuts_in_a_row_are_recovered_and_a_third_stands(setup):
    result, client, root = run(setup, ['cut', 'cut', 'cut', EDIT, DELIVER])
    assert result['status'] == 'BLOCKED' and result['reason'] == 'PARTIAL_REPLY'
    assert result['provider_stop_reason'] == 'length' and client.generations == 3
    assert (root/'a.py').read_bytes() == b'value = 1\n'


def test_a_complete_reply_between_two_cuts_starts_the_count_again(setup):
    read = ('read', {'repository': 'repo', 'path': 'a.py'})
    result, client, root = run(setup, ['cut', read, 'cut', read, 'cut', EDIT, DELIVER])
    assert result['status'] == 'PROPOSED' and (root/'a.py').read_bytes() == b'value = 2\n'  # three cuts, none in a row


def test_the_hosts_message_is_never_the_users_answer_to_a_question():
    from relay.native_refresh import user_words
    text = output_limit_recovery.message({'text': ''}, 8192)
    assert text.startswith('[tare host] ') and user_words(text) is None
    assert user_words('yes, go ahead') == 'yes, go ahead'


def test_the_coordinator_is_told_the_limit_and_not_that_the_task_is_too_large():
    from relay.conversation_tasks import delivery_view
    view = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'the implementer changed nothing', 'files': [],
                          'implementation_stop': {'provider_stop_reason': 'length'}})
    assert 'cut by the route\'s output limit' in view['reason']
    assert 'not proof that the task is too large' in view['next'] and 'another implementer route' in view['next']
    plain = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'the implementer changed nothing', 'files': []})
    assert plain['reason'] == 'the implementer changed nothing' and 'output' not in plain.get('next', '')


def test_only_a_cut_by_the_cap_is_recovered():
    class Journal:
        def __init__(self, reason):
            self.event = {'kind': 'model_returned', 'call_id': 's/model/1',
                          'reply': {'metadata_json': json.dumps({'provider_stop_reason': reason})}}

        def snapshot(self):
            row = type('Row', (), {'event': lambda row: self.event})()
            return type('Snapshot', (), {'records': [row]})()
    assert output_limit_recovery.cut(Journal('length')) and output_limit_recovery.cut(Journal('max_tokens'))
    assert output_limit_recovery.cut(Journal('content_filter')) is None  # a refusal is not a full allowance
    assert output_limit_recovery.cut(Journal(None)) is None
    assert 'Nothing in it was acted on' in output_limit_recovery.message({'text': 'half an answer'}, 8192)


def test_fresh_continuation_after_output_or_provider_stop_tells_coordinator_to_implement_now(host, monkeypatch):
    from relay import conversation_tasks as tasks, work_units
    from test_conversation_tasks import effect, intake, planned, user_reply

    tools, execution, *_ = host
    intake(host)
    planned(host)
    for stop in ('length', 'max_tokens', 'error'):
        value = tasks.load(tools.root)
        task = tasks.current(value)
        task['delivery'] = {
            'verdict': 'INCONCLUSIVE',
            'reason': 'the implementer changed nothing',
            'files': [],
            'route': 'claude-api-opus-5-5',
            'implementation_stop': {'child_status': 'BLOCKED', 'reason': 'PARTIAL_REPLY', 'provider_stop_reason': stop},
        }
        task['attempt_input'] = tools.input_id()
        task['delivery_invocation'] = tools.invocation_id
        task.pop('permission_denied_input', None)
        task.pop('plan_change', None)
        tasks.save(tools.root, value)

        # Same user turn must keep the immediate-stop explanation (no retry loop inside the same turn).
        same_turn = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='same-' + stop), execution).output_json)
        assert 'Call implement now' not in same_turn['next']
        assert same_turn['next'] == same_turn['delivery']['next']

        # Resume (new invocation, same user input) is not a fresh user authorization.
        saved_inv = tools.invocation_id
        tools.invocation_id = 'resume-' + stop
        resumed = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='resume-' + stop), execution).output_json)
        assert 'Call implement now' not in resumed['next']
        tools.invocation_id = saved_inv

        # Fresh user reply: intent=answer still explains without asking to call implement.
        user_reply(host, 'what happened ' + stop)
        tools.invocation_id = 'fresh-answer-' + stop
        answered = json.loads(tools.execute(
            effect('demand', {'intent': 'answer', 'relation': 'continue'}, key='answer-' + stop), execution).output_json)
        assert 'Call implement now' not in answered['delivery']['next']

        # Fresh user reply with a pending question does not instruct calling implement.
        user_reply(host, 'try again ' + stop)
        tools.invocation_id = 'fresh-ask-' + stop
        asked = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue', 'question': 'Which route?'},
                   key='ask-' + stop), execution).output_json)
        assert asked['next'] == 'Ask the pending question.'
        assert 'Call implement now' not in asked['delivery']['next']

        # Fresh user reply clearing the question and continuing delivery directs immediate implement().
        user_reply(host, 'go ahead ' + stop)
        tools.invocation_id = 'fresh-go-' + stop
        continued = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='go-' + stop), execution).output_json)
        assert 'Call implement now' in continued['next']
        assert continued['next'] == continued['delivery']['next']
        assert 'Tell the user' not in continued['next']
        if stop == 'error':
            assert 'provider error' in continued['next'] and 'do not pin the same failed route unchanged' in continued['next']
        else:
            assert 'output-limit stop' in continued['next'] and 'Do not split the plan' in continued['next']

        # relation=correct on a fresh user reply also directs immediate implement().
        corrected = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'correct'}, key='corr-' + stop), execution).output_json)
        assert 'Call implement now' in corrected['next']

        # If the user denied execution in this same turn, demand(continue) must not re-instruct implement().
        value = tasks.load(tools.root)
        tasks.current(value)['permission_denied_input'] = tools.input_id()
        tasks.save(tools.root, value)
        denied = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='denied-' + stop), execution).output_json)
        assert 'Call implement now' not in denied['next']

        # Same if the user requested a plan change in this same turn.
        value = tasks.load(tools.root)
        cur = tasks.current(value)
        cur.pop('permission_denied_input', None)
        cur['plan_change'] = {'input': tools.input_id(), 'plan': cur['plan']['sha256']}
        tasks.save(tools.root, value)
        plan_change = json.loads(tools.execute(
            effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='plan-chg-' + stop), execution).output_json)
        assert 'Call implement now' not in plan_change['next']

    # End-to-end: after fresh demand(continue), calling implement() actually dispatches run_unit.
    value = tasks.load(tools.root)
    cur = tasks.current(value)
    cur.pop('plan_change', None)
    cur['delivery'] = {
        'verdict': 'INCONCLUSIVE',
        'reason': 'the implementer changed nothing',
        'files': [],
        'route': 'claude-api-opus-5-5',
        'implementation_stop': {'child_status': 'BLOCKED', 'reason': 'PARTIAL_REPLY', 'provider_stop_reason': 'length'},
    }
    cur['attempt_input'] = 'older-input'
    cur['delivery_invocation'] = 'older-inv'
    tasks.save(tools.root, value)
    user_reply(host, 'bora tentar novamente')
    tools.invocation_id = 'fresh-dispatch'
    cont = json.loads(tools.execute(
        effect('demand', {'intent': 'deliver', 'relation': 'continue'}, key='dispatch-demand'), execution).output_json)
    assert 'Call implement now' in cont['next']
    dispatched = []
    monkeypatch.setattr(work_units, 'run_unit', lambda *a, **kw: (
        dispatched.append(True), {'verdict': 'APPLIED', 'applied': True, 'files': ['a.py'], 'checks_after': 'ok'})[1])
    res = tools.execute(effect('implement', {}, key='dispatch-implement'), execution)
    assert res.status == 'succeeded' and dispatched == [True]
