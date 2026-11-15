"""A reply the output cap cut does not end the session: the host says so and the model goes on."""
import hashlib
import json

from relay import native_agent, output_limit_recovery
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
