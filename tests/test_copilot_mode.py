from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

import pytest

from relay import copilot_mode, native_serve, turn_engine


def text(content, finish='stop'):
    return [{'choices': [{'delta': {'content': content}}]}, {'choices': [{'delta': {}, 'finish_reason': finish}]}]


def tool_call(name):
    return [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                                                    'function': {'name': name, 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}]


@pytest.fixture
def copilot(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)  # no live judge calls from tests
    monkeypatch.setattr(turn_engine, 'local_judge', lambda: None)  # nor the node's gateway
    project = tmp_path/'project'
    project.mkdir()
    script, seen = [], []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            for chunk in script.pop(0):
                self.wfile.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    routes = {'local': {'id': 'local', 'native_profile': {'protocol': 'openai-chat', 'model': 'm',
              'endpoint': f'http://127.0.0.1:{upstream.server_port}/v1'}},
              'cli': {'id': 'cli', 'native_profile': {'protocol': 'vendor-cli', 'model': 'x', 'endpoint': 'cli://x'}}}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['cli', 'local']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (routes[name], None))
    audits = []

    def audit_turn(files, root, request, implementer, emit, checks=None):
        audits.append((sorted(f['path'] for f in files), request, implementer['id']))
        emit('\n\n**REVISE** — off by one\n')
        return {'verdict': 'REVISE'}, turn_engine.FIX_PREFIX + 'sol) asks: fix the off by one'
    monkeypatch.setattr(turn_engine, 'audit_turn', audit_turn)
    server = ThreadingHTTPServer(('127.0.0.1', 0), native_serve.handler({}, None))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def send(*extra, **fields):
        messages = [{'role': 'user', 'content': f'<workspace_info>\n- {project} \n</workspace_info>'},
                    {'role': 'user', 'content': '<userRequest>add paging</userRequest>'}, *extra]
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                         json.dumps({'model': 'tare-copilot', 'stream': True, 'messages': messages,
                                                     'tools': [{'type': 'function'}], **fields}).encode(),
                                         {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as reply:
            chunks = [json.loads(line[6:]) for line in reply.read().decode().splitlines() if line.startswith('data: {')]
        deltas = [c['choices'][0] for c in chunks if c.get('choices')]
        return (''.join(d['delta'].get('content') or '' for d in deltas),
                [d['finish_reason'] for d in deltas if d.get('finish_reason')])
    yield project, script, seen, audits, send
    server.shutdown()
    upstream.shutdown()


def test_copilot_runs_the_tools_while_tare_routes_audits_and_asks_one_fix(copilot):
    project, script, seen, audits, send = copilot
    script.append(tool_call('create_file'))
    content, finishes = send()
    assert finishes == ['tool_calls'] and seen[0]['tools'] == [{'type': 'function'}]  # client tools, OpenAI-chat route
    (project/'paging.py').write_text('pages\n', encoding='utf-8')  # VS Code applied the tool call
    script += [text('Done.'), tool_call('replace_string_in_file')]
    content, finishes = send({'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'})
    assert audits == [(['paging.py'], 'add paging', 'local')]
    assert 'Done.' in content and 'REVISE' in content and finishes == ['tool_calls']  # the fix round goes on
    injected = seen[2]['messages'][-2:]
    assert injected[0]['role'] == 'assistant' and 'REVISE' in injected[0]['content']
    assert injected[1] == {'role': 'user', 'content': turn_engine.FIX_PREFIX + 'sol) asks: fix the off by one'}
    (project/'paging.py').write_text('pages fixed\n', encoding='utf-8')
    script.append(text('Fixed.'))
    content, finishes = send({'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
                             {'role': 'assistant', 'content': 'Done.'}, {'role': 'tool', 'tool_call_id': 'c2', 'content': 'ok'})
    assert audits[1] == (['paging.py'], turn_engine.FIX_PREFIX + 'sol) asks: fix the off by one', 'local')
    assert finishes == ['stop'] and len(seen) == 4  # the fix is audited; no second round


def test_an_answer_that_changed_nothing_is_not_audited(copilot):
    project, script, seen, audits, send = copilot
    script.append(text('It pages by ten.'))
    content, finishes = send()
    assert content == 'It pages by ten.' and finishes == ['stop'] and audits == []


def test_the_audit_gets_the_commands_vs_code_ran_for_this_message_with_their_output():
    messages = [{'role': 'user', 'content': '<userRequest>earlier</userRequest>'},
                {'role': 'assistant', 'tool_calls': [{'id': 'old', 'function': {'name': 'run_in_terminal',
                                                                                 'arguments': '{"command": "ls"}'}}]},
                {'role': 'user', 'content': '<userRequest>fix the tests</userRequest>'},
                {'role': 'assistant', 'tool_calls': [
                    {'id': 'a', 'function': {'name': 'read_file', 'arguments': '{"filePath": "x.py"}'}},
                    {'id': 'b', 'function': {'name': 'run_in_terminal', 'arguments': '{"command": "pytest -q"}'}}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': 'print(1)'},
                {'role': 'tool', 'tool_call_id': 'b', 'content': '5 passed in 0.3s'}]
    rows = copilot_mode.client_evidence(messages)
    assert rows == [{'argv': ['pytest -q'], 'status': 'ran in VS Code', 'exit_code': None, 'output': '5 passed in 0.3s'}]
    text = turn_engine.verification_of(rows)
    assert '- pytest -q -> ran in VS Code' in text and '  5 passed in 0.3s' in text


def test_a_route_that_cannot_see_gets_descriptions_and_linked_pages_instead_of_image_parts():
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,UE5H'}}
    body = {'messages': [{'role': 'system', 'content': 'You are Copilot.'},
                         {'role': 'user', 'content': [{'type': 'text', 'text': '<userRequest>why red?</userRequest>'},
                                                      image]}]}
    context = '\n\nAttached image(s), described by luna: a red button'
    turn = {'chat': {'floor': 0, 'contexts': {copilot_mode.request_key('why red?'): context}}}
    blind = copilot_mode.with_context(body, turn, {'native_profile': {}})['messages'][1]['content']
    assert image not in blind and blind[-1] == {'type': 'text', 'text': context}
    seeing = copilot_mode.with_context(body, turn, {'native_image_media_types': ['image/png']})['messages'][1]['content']
    assert image in seeing and seeing[-1]['text'] == context
    assert body['messages'][1]['content'][-1] is image  # the client's request is not mutated


def test_the_history_a_route_gets_only_grows_at_its_end_so_the_prompt_cache_holds():
    chat = {'floor': 0, 'contexts': {copilot_mode.request_key('read https://example.com/a'): '\n\nPAGE A'}}
    seeing = {'native_image_media_types': ['image/png']}
    first = [{'role': 'system', 'content': 'You are Copilot.'},
             {'role': 'user', 'content': '<userRequest>read https://example.com/a</userRequest>'}]
    second = first + [{'role': 'assistant', 'content': 'done'},
                      {'role': 'user', 'content': '<userRequest>now b</userRequest>'}]
    shaped1 = copilot_mode.with_context({'messages': first}, {'chat': chat}, seeing)['messages']
    shaped2 = copilot_mode.with_context({'messages': second}, {'chat': chat}, seeing)['messages']
    assert shaped1[1]['content'].endswith('PAGE A') and shaped2[:len(shaped1)] == shaped1


def test_a_failing_route_hands_the_call_up_and_the_chat_stays_up(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    strong = {'id': 'strong', 'native_profile': {'protocol': 'openai-chat', 'model': 's',
                                                 'endpoint': 'http://unused.invalid/v1'}}
    local = turn_engine.available('local')[0]
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local'], ['strong']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'local': local, 'strong': strong}[name], None))
    real, used = copilot_mode.upstream_lines, []

    def upstream(deployment, request):
        used.append(deployment['id'])
        if deployment['id'] == 'local':
            raise ValueError('route answered HTTP 500')
        return real(local, request)  # the fixture's upstream answers for the strong route too
    monkeypatch.setattr(copilot_mode, 'upstream_lines', upstream)
    script.append(text('Answer.'))
    content, finishes = send()
    assert used == ['local', 'strong'] and 'local failed' in content and 'continuing on strong' in content
    assert content.endswith('Answer.') and finishes == ['stop']
    script.append(text('Again.'))
    content, _ = send({'role': 'assistant', 'content': content}, {'role': 'user', 'content': '<userRequest>more</userRequest>'})
    assert used[2:] == ['strong']  # the next message starts where the chat went: no flip back, no cache thrown away


def test_a_fix_that_fails_its_audit_again_moves_the_next_message_up(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    local = turn_engine.available('local')[0]
    strong = {**local, 'id': 'strong'}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local'], ['strong']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'local': local, 'strong': strong}[name], None))
    script.append(tool_call('create_file'))
    send()
    (project/'paging.py').write_text('pages\n', encoding='utf-8')
    script += [text('Done.'), tool_call('replace_string_in_file')]
    send({'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'})
    (project/'paging.py').write_text('still wrong\n', encoding='utf-8')
    script.append(text('Fixed.'))
    content, _ = send({'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}, {'role': 'assistant', 'content': 'Done.'},
                      {'role': 'tool', 'tool_call_id': 'c2', 'content': 'ok'})
    assert 'the next message starts on a stronger route' in content
    script.append(text('Better.'))
    send({'role': 'assistant', 'content': content}, {'role': 'user', 'content': '<userRequest>try again</userRequest>'})
    rows = [json.loads(line) for line in (project.parent/'storage'/'serve'/'usage.jsonl').read_text().splitlines()]
    assert [row['route'] for row in rows][-2:] == ['local', 'strong']  # the fix round on local, then one tier up


def test_the_runtimes_no_progress_rule_counts_rounds_that_repeat_what_was_seen():
    looping = [[('read_file', '{"filePath": "a.py"}', 'x = 1')]] * 4
    assert turn_engine.round_progress(looping) == [True, False, False, False]
    assert turn_engine.stagnant_rounds(turn_engine.round_progress(looping)) == 3 == turn_engine.MAX_STAGNANT_ROUNDS
    working = [[('read_file', '{"filePath": "a.py"}', 'x = 1')], [('edit', '{"a": 1}', 'ok')],
               [('read_file', '{"filePath": "a.py"}', 'x = 2')]]  # the same read, but the file changed
    assert turn_engine.stagnant_rounds(turn_engine.round_progress(working)) == 0


def stuck_history(rounds):
    history = []
    for index in range(rounds):
        history += [{'role': 'assistant', 'tool_calls': [{'id': f'c{index}', 'function': {
                        'name': 'read_file', 'arguments': '{"filePath": "a.py"}'}}]},
                    {'role': 'tool', 'tool_call_id': f'c{index}', 'content': 'x = 1'}]
    return history


def test_a_stuck_message_moves_one_tier_up_and_stuck_at_the_top_stops_the_loop(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    local = turn_engine.available('local')[0]
    strong = {**local, 'id': 'strong'}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local'], ['strong']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'local': local, 'strong': strong}[name], None))
    script.append(tool_call('read_file'))
    send()
    script.append(text('Found it.'))
    content, finishes = send(*stuck_history(4))  # three rounds repeat the first read and its result
    assert 'no progress in 3 rounds on local' in content and 'continuing on strong' in content
    assert content.endswith('Found it.') and len(seen) == 2
    calls = len(seen)
    content, finishes = send(*stuck_history(8))  # stuck again, now on the strongest route
    assert 'stopping here instead of looping' in content and finishes == ['stop'] and len(seen) == calls


def test_a_reply_that_only_announced_the_work_moves_the_chat_one_tier_up_once(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    local = turn_engine.available('local')[0]
    strong = {**local, 'id': 'strong'}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local'], ['strong']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'local': local, 'strong': strong}[name], None))
    announce = 'Vou rastrear a regra que define IsDeleted e o fluxo usado para propostas antigas...'
    judged = []

    def unanswered(request, reply, observed=None):
        judged.append((request, reply, (observed or {}).get('route')))
        return {'model': 'qwen38-gsq', 'probabilities': {'announced': .95}} if reply == announce else None
    monkeypatch.setattr(turn_engine, 'unanswered', unanswered)
    script += [text(announce), text('IsDeleted vem do soft delete.')]
    content, finishes = send()
    assert judged == [('add paging', announce, 'local')]  # once per message: the stronger reply is not judged again
    assert 'only announced the work' in content and 'continuing on strong' in content
    assert content.endswith('IsDeleted vem do soft delete.') and finishes == ['stop'] and len(seen) == 2
    tail = seen[1]['messages'][-2:]  # the history is the continuity, as in the fix round
    assert tail[0].get('role') == 'assistant' and tail[0].get('content') == announce
    assert tail[1] == {'role': 'user', 'content': turn_engine.UNANSWERED_CONTINUE}
    assert audits == []  # nothing changed: no audit
    script.append(text('Mais.'))
    send({'role': 'assistant', 'content': content}, {'role': 'user', 'content': '<userRequest>more</userRequest>'})
    assert len(judged) == 1  # the chat stays on the strongest route: nowhere to go, no judge asked
    rows = [json.loads(line) for line in (project.parent/'storage'/'serve'/'usage.jsonl').read_text().splitlines()]
    assert [row['route'] for row in rows][-2:] == ['strong', 'strong']


def test_local_gpu_is_answered_by_tare_and_kept_for_that_chat_only(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    content, finishes = send({'role': 'assistant', 'content': 'Paged.'},
                             {'role': 'user', 'content': '<userRequest>/local-gpu off</userRequest>'})
    assert 'this chat uses no local models' in content and finishes == ['stop'] and not seen
    assert not turn_engine.leaves_gpu_to_others() and not turn_engine.avoid_local_gpu()  # this chat only
    real = turn_engine.available
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'no local models in this chat') if (
        name == 'local' and turn_engine.avoid_local_gpu()) else real(name))
    with pytest.raises(urllib.error.HTTPError) as refused:  # its only tool-carrying route is the local one
        send({'role': 'assistant', 'content': 'Paged.'}, {'role': 'user', 'content': '<userRequest>/local-gpu off</userRequest>'},
             {'role': 'assistant', 'content': 'ok'}, {'role': 'user', 'content': '<userRequest>add tests</userRequest>'})
    assert refused.value.code == 503 and not seen


def test_a_request_rules_out_local_models_itself_and_a_chat_choice_outlives_a_restart(copilot, monkeypatch):
    project, script, seen, audits, send = copilot
    real = turn_engine.available
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'no local models in this chat') if (
        name == 'local' and turn_engine.avoid_local_gpu()) else real(name))
    with pytest.raises(urllib.error.HTTPError) as refused:  # the policy rides on the inference request itself
        send(tare_local_models=False)
    assert refused.value.code == 503 and not seen  # its only tool-carrying route is the local one
    send({'role': 'assistant', 'content': 'Paged.'}, {'role': 'user', 'content': '<userRequest>/local-gpu off</userRequest>'})
    copilot_mode._chats.clear()  # tare serve restarted, as it does after each update
    with pytest.raises(urllib.error.HTTPError) as after:
        send({'role': 'assistant', 'content': 'Paged.'}, {'role': 'user', 'content': '<userRequest>/local-gpu off</userRequest>'},
             {'role': 'assistant', 'content': 'ok'}, {'role': 'user', 'content': '<userRequest>add tests</userRequest>'})
    assert after.value.code == 503 and not seen  # the chat's choice was read back from disk
