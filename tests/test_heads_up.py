import hashlib
import json
from types import SimpleNamespace

from relay import heads_up, turn_engine
from relay.console_models import ConversationModels

PYTEST = 'python -m pytest -q tests/test_p.py'


def call(effect, name, args, status, output):
    return [{'event': {'kind': 'effect_requested', 'effect_id': effect,
                       'tool_call': {'name': name, 'arguments_json': json.dumps(args)}}},
            {'event': {'kind': 'effect_returned', 'result': {'effect_id': effect, 'status': status,
                                                              'output_json': json.dumps(output)}}}]


def session(tmp_path, reply='Done: all tests pass.'):
    """An earlier exchange, then a finished turn: a failing test fixed and re-run, a failing verifier, then one more
    edit and a refused one."""
    folder = tmp_path/'session'
    folder.mkdir()
    events = [{'event': {'kind': 'session_started', 'instruction': 'TUI wrapper...\n\nOperator message:\nMeasure it'}},
              {'event': {'kind': 'terminal', 'output': 'Measured on the stub model only.'}}]
    start = len(events)
    events += [{'event': {'kind': 'user_message', 'text': 'Fix the parser and run the tests'}}]
    events += call('e1', 'edit', {'path': 'src/p.py'}, 'succeeded', {'effects': 1})
    events += call('e2', 'terminal_read', {'handle': 'h'}, 'failed',
                   {'exit_code': 1, 'command': PYTEST, 'output': {'stdout': {'text': 'F\n1 failed, 3 passed'}}})
    events += call('e3', 'edit', {'path': 'src/p.py'}, 'succeeded', {'effects': 1})
    events += call('e4', 'terminal_read', {'handle': 'h'}, 'succeeded',
                   {'exit_code': 0, 'command': PYTEST, 'output': {'stdout': {'text': '4 passed'}}})
    events += call('e5', 'verify', {'name': 'check'}, 'failed', {'output': 'collected 9\n2 failed, 7 passed'})
    events += call('e6', 'edit', {'path': 'src/q.py'}, 'succeeded', {'effects': 1})
    events += call('e7', 'edit', {'path': 'README.md'}, 'refused', {'reason': 'exact_unique_search_required'})
    events.append({'event': {'kind': 'terminal', 'output': reply}})
    (folder/'journal.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    (folder/'native-agent.json').write_text(json.dumps({'deployment': {'id': 'cheap-a'}, 'invocations': [
        {'id': 'old', 'journal_start': 0, 'journal_end': start, 'status': 'PROPOSED'},
        {'id': 'inv', 'journal_start': start, 'journal_end': len(events), 'status': 'PROPOSED'}]}), encoding='utf-8')
    return folder


def test_tare_states_the_facts_it_can_check_without_a_model(tmp_path):
    turn = heads_up.from_journal([(session(tmp_path), 'inv')])
    assert turn['request'] == 'Fix the parser and run the tests' and turn['reply'] == 'Done: all tests pass.'
    assert turn['earlier'] == [{'user': 'TUI wrapper...\n\nOperator message:\nMeasure it',
                                'assistant': 'Measured on the stub model only.'}]
    assert [s['tool'] for s in turn['steps']] == ['edit', 'terminal_read', 'edit', 'terminal_read', 'verify', 'edit',
                                                  'edit']
    # The failing pytest run was fixed and re-run, so it is not a fact; the verifier's last run failed.
    assert heads_up.facts(turn) == [
        'The last run of verify check failed: 2 failed, 7 passed.',
        'src/q.py changed after the last test (verify check) ran; the final version was not tested.',
        'The last edit of README.md was refused (exact_unique_search_required) and not redone.']
    # An ordinary command is not a test: its failure, or edits after it, are no fact.
    plain = {'request': 'x', 'reply': 'y', 'steps': [
        heads_up.step('terminal_read', {}, 'succeeded', {'exit_code': 0, 'command': 'git status'}),
        heads_up.step('terminal_read', {}, 'failed', {'exit_code': 1, 'command': 'grep -r missing src'}),
        heads_up.step('edit', {'path': 'a.py'}, 'succeeded', {})]}
    assert heads_up.facts(plain) == []
    explored = heads_up.step('explore', {'investigations': []}, 'succeeded', {'investigations': [{'report': {
        'locations': [{'path': 'docs/a.md', 'line': 69}, {'path': 'run.gd'}, {'path': 'docs/a.md'}]}}]})
    assert explored['summary'] == 'cited docs/a.md, run.gd'  # what explorers read is not a step of the turn
    implemented = heads_up.step('implement', {}, 'succeeded', {'verdict': 'INCONCLUSIVE', 'reason': 'check refused'})
    assert implemented['summary'] == 'verdict INCONCLUSIVE; reason check refused'  # the call succeeded, not the work
    # What a step did reaches the writer, from what tare already recorded: no tools, no calls of its own.
    edited = heads_up.step('edit', {'path': 'a.py', 'changes': [{'search': 'x = 1', 'replace': 'x = 2'}]}, 'succeeded', {})
    assert edited['detail'] == '- x = 1\n+ x = 2' and heads_up._line(edited) == 'edit a.py -> succeeded :: - x = 1\n+ x = 2'
    demanded = heads_up.step('demand', {'objective': 'Reject inverted bounds', 'criteria': ['ValueError if low > high']},
                             'succeeded', {})
    assert demanded['detail'] == 'objective: Reject inverted bounds; criterion: ValueError if low > high'
    explored = heads_up.step('explore', {'investigations': [{'question': 'Where is the cost table?'}]}, 'succeeded',
                             {'investigations': [{'report': {'summary': 'In balance.py, a constant per level.'}}]})
    assert explored['detail'] == 'asked: Where is the cost table?; found: In balance.py, a constant per level.'


NOTE = 'The suite now shares one process, so one test can change the next.'


def test_the_score_or_the_cadence_reaches_the_writer_and_jev_checks_the_note(tmp_path, monkeypatch):
    monkeypatch.delenv('TARE_HEADS_UP')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    written, already = [], {'already_said': .1}
    monkeypatch.setattr(turn_engine, 'judge_many', lambda state, questions, timeout=10: {'yes': dict(already)})
    monkeypatch.setattr(heads_up, 'write', lambda text, seen: written.append((json.loads(text), seen)) or (
        [NOTE], [{'route': 'sonnet', 'ok': True, 'ms': 5}]))
    explained = {'request': 'Explain the parser', 'reply': 'It is a recursive descent parser. ' * 150, 'steps': [
        heads_up.step('read', {'path': 'src/p.py'}, 'succeeded', {})]}
    worked = heads_up.from_journal([(session(tmp_path), 'inv')])
    assert heads_up.score(explained, []) < heads_up.FORWARD_AT <= heads_up.score(worked, heads_up.facts(worked))
    tested = {**explained, 'request': 'Run the tests', 'steps': [heads_up.step('terminal_read', {}, 'failed', {
        'exit_code': 1, 'command': PYTEST, 'output': '1 failed'})]}
    assert heads_up.score(tested, heads_up.facts(tested)) < heads_up.FORWARD_AT
    pace = {}  # quiet turns reach the writer every EVERY-th turn since it last ran, with the steps of the turns between
    assert [heads_up.notes(t, pace=pace) for t in (tested, explained, explained, explained)] == [[], [], [], [NOTE]]
    assert pace == {'since': 0, 'held': []} and len(written) == 1
    window = written[0][0]['recorded_steps']
    assert window[0] == 'earlier turn Run the tests -> started' and window[1].startswith('terminal_read ' + PYTEST)
    assert window[1].endswith(':: 1 failed') and window[-2:] == ['this turn Explain the parser -> started',
                                                                 'read src/p.py -> succeeded']
    assert heads_up.notes(worked, seen=['old note'], pace=pace) == [NOTE]  # a busy turn: the score
    assert written[-1][1] == ['old note'] and 'facts_checked_by_tare' not in written[-1][0]  # process facts: never
    assert written[-1][0]['earlier_in_conversation'][0]['assistant'] == 'Measured on the stub model only.'
    already['already_said'] = .9  # the reply already says it: the note goes
    assert heads_up.notes(worked) == []
    rows = [json.loads(line) for line in (tmp_path/'storage'/'serve'/'heads-up.jsonl').read_text(
        encoding='utf-8').splitlines()]
    assert [r['forwarded'] for r in rows] == [None, None, None, 'cadence', 'score', 'score']
    assert [r['since'] for r in rows[:5]] == [1, 2, 3, 4, 1] and [r['held'] for r in rows[:4]] == [0, 2, 4, 6]
    assert rows[4]['writers'][0]['route'] == 'sonnet' and rows[4]['counts']['edits'] == 4
    assert rows[5]['redundant'] == .9 and rows[5]['notes'] == 0
    assert 'parser' not in json.dumps(rows) and 'one process' not in json.dumps(rows)  # never the texts
    heads_up.set_enabled(False)
    assert heads_up.notes(worked) == [] and len(written) == 3


def test_the_writer_keeps_the_newest_context_of_a_long_turn():
    steps = [heads_up.step('edit', {'path': f'src/"quoted"\nfile{i}.py'}, 'succeeded', {}) for i in range(400)]
    steps += [heads_up.step('terminal_read', {}, 'failed', {'exit_code': 1, 'command': f'pytest "{i}"' * 30,
                                                           'output': '"x"\n' * 900}) for i in range(10)]
    turn = {'request': 'pedido ' * 3000, 'reply': 'résumé "x"\n' * 5000, 'steps': steps,
            'earlier': [{'user': f'antes "{i}"\n' * 400, 'assistant': 'resposta "x"\n' * 600} for i in range(6)]}
    state = json.loads(heads_up.record(turn, 8000))  # what the writer reads
    assert len(json.dumps(state, ensure_ascii=False).encode('utf-8')) <= 8000 * 1.1
    assert state['recorded_steps'][0] == '... earlier steps left out' and 'pytest "9"' in state['recorded_steps'][-1]
    # The newest earlier exchange is kept, and it keeps the end of the user's message.
    assert state['earlier_in_conversation'][-1]['user'].endswith('antes "5"\n')


def test_the_writer_takes_the_first_available_route_and_hands_over_once(monkeypatch):
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'heads_up': ['local-default', 'luna', 'haiku', 'sonnet']})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'GPU busy') if name == 'local-default' else (
        {'id': name, 'native_profile': {'model': name}}, None))
    calls = []

    def generate_text(deployment, model, prompt, timeout, **kwargs):
        calls.append((model, kwargs['system']))
        if model == 'luna':
            return False, '', {'failure_class': 'CLI_USAGE_LIMIT'}
        return True, '- Old one\n- The verifier failed.\n- A third', {}
    monkeypatch.setattr('relay.native_generation.generate_text', generate_text)
    notes, attempts = heads_up.write('{}', ['old one'])
    assert [c[0] for c in calls] == ['luna', 'haiku'] and calls[0][1] == heads_up.SYSTEM
    assert notes == ['The verifier failed.']  # an already-shown note goes; one at most
    assert [a['route'] for a in attempts] == ['luna', 'haiku'] and attempts[0]['failure'] == 'CLI_USAGE_LIMIT'
    assert heads_up.parse('NONE', []) == [] and heads_up.parse('none.', []) == []
    sentence = 'It may cost more. '  # a long note ends at its last whole sentence, not mid-word
    assert heads_up.parse('- ' + sentence * 40, []) == [(sentence * (heads_up.NOTE_CHARS // len(sentence))).strip()]
    assert heads_up.parse('- ' + 'x' * 600, []) == ['x' * heads_up.NOTE_CHARS]


def test_calibration_reports_what_each_threshold_saves_and_drops(capsys):
    from scripts.heads_up_calibration import report
    writer = [{'route': 'sonnet', 'ok': True, 'ms': 2000}]
    report([{'score': .05, 'forwarded': 'cadence', 'notes': 0, 'writers': writer, 'reply_sha256': 'a'},
            {'score': .1, 'forwarded': 'cadence', 'notes': 1, 'writers': writer, 'reply_sha256': 'b'},
            {'score': .3, 'forwarded': 'score', 'notes': 1, 'writers': writer, 'reply_sha256': 'c'},
            {'score': .5, 'forwarded': 'score', 'notes': 0, 'writers': writer, 'reply_sha256': 'd',
             'redundant': .95},  # written, then dropped as already said
            {'score': .02, 'forwarded': None, 'notes': 0, 'writers': [], 'reply_sha256': 'e'},  # never written
            {'event': 'asked', 'reply_sha256': 'c'}])  # the user sent c's note to the agent
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == ('5 logged turns; the writer answered 4 (2 by cadence below the score); 2 got a note, 1 sent '
                        'to the agent with /heads-up; 1 dropped as already said.')
    assert lines[2].split() == ['0.050', '0/4', '0/2'] and lines[5].split() == ['0.250', '2/4', '1/2']
    assert lines[-1] == 'sonnet: 4/4 answered, median 2.0 s'


def test_claude_code_turns_carry_the_side_agent_note_the_user_accepted(tmp_path):
    from scripts.heads_up_calibration import MARK, claude_turns, headline
    assert headline(MARK + ':\n\n> Heads up · Only measured on Sol.\n>\n> **Title**\n> - detail') == 'Only measured on Sol.'

    def user(content, **extra):
        return {'type': 'user', 'message': {'role': 'user', 'content': content}, **extra}

    def assistant(*blocks):
        return {'type': 'assistant', 'message': {'role': 'assistant', 'content': list(blocks)}}
    done = {'type': 'system', 'subtype': 'turn_duration'}
    rows = [user('Fix the parser'), assistant({'type': 'tool_use', 'id': 'a', 'name': 'Edit',
                                               'input': {'file_path': 'src/p.py'}}),
            user([{'type': 'tool_result', 'tool_use_id': 'a', 'content': 'ok'}]),
            assistant({'type': 'tool_use', 'id': 'b', 'name': 'Bash', 'input': {'command': 'pytest -q'}}),
            user([{'type': 'tool_result', 'tool_use_id': 'b', 'is_error': True, 'content': 'Exit code 1\n1 failed'}]),
            assistant({'type': 'text', 'text': 'Fixed, tests pass.'}), done,
            user(MARK + ':\n\n> Heads up · The tests failed.'), assistant({'type': 'text', 'text': 'Right.'}), done,
            user('<command-name>/clear</command-name>'), user('Thanks'), assistant({'type': 'text', 'text': 'Ok'}),
            {**done, 'isSidechain': True}, done]
    path = tmp_path/'s.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    (first, note), (second, quiet) = claude_turns(path)
    assert first['request'] == 'Fix the parser' and first['reply'] == 'Fixed, tests pass.' and first['earlier'] == []
    assert note.endswith('The tests failed.') and quiet is None and second['request'] == 'Thanks'
    assert second['earlier'] == [{'user': 'Fix the parser', 'assistant': 'Fixed, tests pass.'}]
    assert heads_up.facts(first) == ['The last run of `pytest -q` failed (exit 1): 1 failed.']


def test_a_copilot_turn_is_read_from_the_client_messages():
    messages = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'ok'},
                {'role': 'user', 'content': [{'type': 'text', 'text': 'Run the tests'}]},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 't1', 'function': {'name': 'run_in_terminal', 'arguments': '{"command": "pytest -q"}'}}]},
                {'role': 'tool', 'tool_call_id': 't1', 'content': 'F.\n1 failed, 1 passed'}]
    turn = heads_up.from_messages(messages, 'All green.')
    assert turn['request'] == 'Run the tests' and turn['reply'] == 'All green.'
    assert turn['earlier'] == [{'user': 'old', 'assistant': 'ok'}]
    assert turn['steps'] == [{'tool': 'run_in_terminal', 'subject': 'pytest -q', 'status': 'returned',
                              'summary': '1 failed, 1 passed'}]


def test_the_tui_shows_new_notes_once_and_keeps_them_for_the_composer(tmp_path, monkeypatch):
    monkeypatch.delenv('TARE_HEADS_UP')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    source = session(tmp_path)
    told, said = [], []
    monkeypatch.setattr(heads_up, 'notes', lambda turn, seen=(), observed=None, pace=None: told.append(list(seen)) or (
        pace.update(since=3)) or ['The verifier failed.'])
    console = SimpleNamespace(selection=lambda: SimpleNamespace(local_models=True),
                              call_from_thread=lambda fn, *a, **kw: fn(*a, **kw),
                              notify=lambda text, **kw: said.append(text),
                              audit_say=lambda text, source, label: said.append(text))
    ConversationModels._heads_up(console, str(source), ['inv'], 'Fix it')
    assert 'tare · heads-up: The verifier failed.' in said[0] and '/heads-up' in said[0]
    reply = hashlib.sha256(b'Done: all tests pass.').hexdigest()
    assert heads_up.shown(source) == {'shown': ['The verifier failed.'], 'last': ['The verifier failed.'], 'turn': reply,
                                      'since': 3, 'held': []}  # the pace is the session's, kept across turns
    heads_up.keep(source, [], 'a quiet turn', {'since': 4})  # a turn without notes keeps the last ones for /heads-up
    assert heads_up.shown(source)['last'] == ['The verifier failed.'] and heads_up.shown(source)['turn'] == reply
    ConversationModels._heads_up(console, str(source), ['inv'], 'Fix it')
    assert told[-1] == ['The verifier failed.']  # the second turn's writer is told what was shown
    assert heads_up.ask(heads_up.shown(source)['last']).startswith("tare's heads-up after your last reply:\n- The")
    heads_up.asked(source)  # /heads-up: the operator sent it to the agent, a label joined to the turn by its reply
    row = json.loads((tmp_path/'storage'/'serve'/'heads-up.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert row['event'] == 'asked' and row['reply_sha256'] == reply
