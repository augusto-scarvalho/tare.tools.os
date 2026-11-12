import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

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
    unapplied = heads_up.step('implement', {}, 'succeeded', {'verdict': 'NOT_REVIEWED', 'files': ['a.py'],
                                                             'files_scope': 'unapplied candidate, not project files',
                                                             'implementation_stop': {'reason': 'NO_PROGRESS'}})
    assert unapplied['summary'] == ('verdict NOT_REVIEWED; files unapplied candidate, not project files; '
                                    'implementer stopped NO_PROGRESS')
    # What a step did reaches the writer, from what tare already recorded: no tools, no calls of its own.
    edited = heads_up.step('edit', {'path': 'a.py', 'changes': [{'search': 'x = 1', 'replace': 'x = 2'}]}, 'succeeded', {})
    assert edited['detail'] == '- x = 1\n+ x = 2' and heads_up._line(edited) == 'edit a.py -> succeeded :: - x = 1\n+ x = 2'
    demanded = heads_up.step('demand', {'objective': 'Reject inverted bounds', 'criteria': ['ValueError if low > high']},
                             'succeeded', {})
    assert demanded['detail'] == 'objective: Reject inverted bounds; criterion: ValueError if low > high'
    explored = heads_up.step('explore', {'investigations': [{'question': 'Where is the cost table?'}]}, 'succeeded',
                             {'investigations': [{'report': {'summary': 'In balance.py, a constant per level.'}}]})
    assert explored['detail'] == 'asked: Where is the cost table?; found: In balance.py, a constant per level.'
    refused = heads_up.step('implement', {'note': 'Drop the evidence files.'}, 'succeeded', {
        'verdict': 'NOT_REVIEWED', 'implementer_report': {'basis': 'claim', 'text': '89 passed.'},
        'audit': {'verdict': 'REVISE', 'findings': [{'claim': 'It commits 3 evidence files.'}]}})
    assert refused['detail'] == ('audit REVISE: It commits 3 evidence files.; asked: Drop the evidence files.; '
                                 'reported: 89 passed.')  # what the audit found, which the user never sees


NOTE = 'The suite now shares one process, so one test can change the next.'


def test_the_score_or_the_cadence_reaches_the_writer_and_jev_checks_the_note(tmp_path, monkeypatch):
    monkeypatch.delenv('TARE_HEADS_UP')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    written, already = [], {'already_said': .1}
    monkeypatch.setattr(turn_engine, 'judge_many', lambda state, questions, timeout=10: {'yes': dict(already)})
    monkeypatch.setattr(heads_up, 'write', lambda text, seen, known=(): written.append((json.loads(text), seen)) or (
        [NOTE], [{'route': 'sonnet', 'ok': True, 'ms': 5}]))
    explained = {'request': 'Explain the parser', 'reply': 'It is a recursive descent parser. ' * 150, 'steps': [
        heads_up.step('read', {'path': 'src/p.py'}, 'succeeded', {})]}
    worked = heads_up.from_journal([(session(tmp_path), 'inv')])
    assert heads_up.score(explained, []) < heads_up.FORWARD_AT <= heads_up.score(worked, heads_up.facts(worked))
    tested = {**explained, 'request': 'Run the tests', 'steps': [heads_up.step('terminal_read', {}, 'succeeded', {
        'exit_code': 0, 'command': PYTEST, 'output': '4 passed'})]}  # a failed run would be an event, not a quiet turn
    assert heads_up.score(tested, heads_up.facts(tested)) < heads_up.FORWARD_AT
    assert heads_up.events(tested, []) == heads_up.events(explained, []) == []
    pace = {}  # quiet turns reach the writer every EVERY-th turn since it last ran, with the steps of the turns between
    assert [heads_up.notes(t, pace=pace) for t in (tested, explained, explained, explained)] == [[], [], [], [NOTE]]
    assert pace == {'since': 0, 'held': []} and len(written) == 1
    window = written[0][0]['recorded_steps']
    assert window[0] == 'earlier turn Run the tests -> started' and window[1].startswith('terminal_read ' + PYTEST)
    assert window[1].endswith(':: 4 passed') and window[-2:] == ['this turn Explain the parser -> started',
                                                                 'read src/p.py -> succeeded']
    assert heads_up.notes(worked, seen=['old note'], pace=pace) == [NOTE]  # edits and a failed check: events
    assert written[-1][1] == ['old note'] and 'facts_checked_by_tare' not in written[-1][0]  # process facts: never
    assert written[-1][0]['earlier_in_conversation'][0]['assistant'] == 'Measured on the stub model only.'
    already['already_said'] = .9  # the reply already says it: the note goes
    assert heads_up.notes(worked) == []
    rows = [json.loads(line) for line in (tmp_path/'storage'/'serve'/'heads-up.jsonl').read_text(
        encoding='utf-8').splitlines()]
    assert [r['forwarded'] for r in rows] == [None, None, None, 'cadence', 'event', 'event']
    assert [r['events'] for r in rows] == [[], [], [], [], ['edit', 'check'], ['edit', 'check']]
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
                        'to the agent; 1 dropped as already said.')
    assert lines[1] == ('By reason: score 2 forwarded, 1 with a note (0 useful, 0 not useful); '
                        'cadence 2 forwarded, 1 with a note (0 useful, 0 not useful).')
    assert lines[2] == ('0 notes labelled useful, 0 not useful; 1 sent to the agent (0 with the note rewritten), '
                        '0 confirmed by its reply after checking, 0 agreed without checking, 0 not confirmed, '
                        '0 without an answer.')
    assert lines[3].startswith('0 replies flagged as missing a note')  # no /missed yet
    assert lines[5].split() == ['0.050', '0/4', '0/2'] and lines[8].split() == ['0.250', '2/4', '1/2']
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
    told, said, logged, order = [], [], [], []
    monkeypatch.setattr(heads_up, 'notes', lambda turn, seen=(), observed=None, pace=None: order.append('notes') or (
        told.append(list(seen))) or (logged.append(observed)) or pace.update(since=3) or ['The verifier failed.'])
    monkeypatch.setattr(heads_up, 'followed', lambda session, turn, source='tui': order.append('followed') or [])
    console = SimpleNamespace(selection=lambda: SimpleNamespace(local_models=True),
                              call_from_thread=lambda fn, *a, **kw: fn(*a, **kw),
                              notify=lambda text, **kw: said.append(text),
                              show_heads_up=lambda new, source: said.append(new))
    ConversationModels._heads_up(console, str(source), ['inv'], 'Fix it')
    assert said == [['The verifier failed.']] and order == ['followed', 'notes']  # what the request sent comes first
    reply = hashlib.sha256(b'Done: all tests pass.').hexdigest()
    mine = heads_up.shown(source)
    assert mine == {'shown': ['The verifier failed.'], 'last': ['The verifier failed.'], 'turn': reply,
                    'noted': mine['noted'], 'missed': [], 'since': 3, 'held': []}  # the pace is the session's, kept across turns
    assert logged[0]['session'] == source.name  # the log row names its conversation
    from scripts.heads_up_calibration import noted  # dogfood review: each note with its turn
    assert [(n['session'], n['note'], n['request'], n['reply']) for n in noted(source.parent)] == [
        (source.name, 'The verifier failed.', 'Fix it', 'Done: all tests pass.')]
    assert noted(source.parent, '9999') == []
    heads_up.keep(source, [], {'reply': 'a quiet turn'}, {'since': 4})  # a turn without notes keeps the last ones
    assert heads_up.shown(source)['last'] == ['The verifier failed.'] and heads_up.shown(source)['turn'] == reply
    ConversationModels._heads_up(console, str(source), ['inv'], 'Fix it')
    assert told[-1] == ['The verifier failed.']  # the second turn's writer is told what was shown
    assert heads_up.ask(heads_up.shown(source)['last']).startswith("tare's heads-up after your last reply:\n- The")
    heads_up.asked(source)  # without a note: the session's last turn, joined to its row by the reply
    row = json.loads((tmp_path/'storage'/'serve'/'heads-up.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert row['event'] == 'asked' and row['reply_sha256'] == reply


def test_a_message_queued_during_the_turn_reaches_the_heads_up_but_host_messages_do_not(tmp_path):
    folder = tmp_path/'queued'
    folder.mkdir()
    events = [{'event': {'kind': 'user_message', 'message_id': 'a'*32, 'text': 'TUI wrapper...\nAdd the endpoint'}},
              {'event': {'kind': 'user_message', 'message_id': 'b'*32, 'text': 'Also log every refusal'}},
              {'event': {'kind': 'user_message', 'message_id': 'consent-decision-consent-choice-1',
                         'text': 'Host dialog choice: Allow.'}},
              {'event': {'kind': 'user_message', 'message_id': 'c'*32, 'text': 'tare audit: fix the failing test'}},
              {'event': {'kind': 'user_message', 'message_id': 'cli-note', 'text': 'And keep the old route'}},
              {'event': {'kind': 'terminal', 'output': 'Added the endpoint and the refusal log.'}}]
    (folder/'messages').mkdir()  # the operator's inbox keeps what the user queued, whatever its id looks like
    (folder/'messages'/'queue.json').write_text(json.dumps({'session_id': 's', 'items': [
        {'id': 'b'*32, 'text': 'Also log every refusal'}, {'id': 'cli-note', 'text': 'And keep the old route'}]}))
    (folder/'journal.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    (folder/'native-agent.json').write_text(json.dumps({'deployment': {'id': 'cheap-a'}, 'invocations': [
        {'id': 'inv', 'journal_start': 0, 'journal_end': len(events), 'status': 'PROPOSED'}]}), encoding='utf-8')
    turn = heads_up.from_journal([(folder, 'inv')], 'Add the endpoint')
    assert turn['request'] == ('Add the endpoint\n\nLater message from the user:\nAlso log every refusal'
                               '\n\nLater message from the user:\nAnd keep the old route')


def test_a_note_of_another_conversation_is_only_a_notification():
    said = []
    console = SimpleNamespace(audited_open=lambda source: False, notify=lambda text, **kw: said.append((text, kw)))
    ConversationModels.show_heads_up(console, ['The verifier failed.'], 'elsewhere')
    assert said == [('tare heads-up of another conversation: _tare · heads-up: The verifier failed._', {'timeout': 20})]


@pytest.fixture
def log(tmp_path, monkeypatch):
    """The heads-up log's rows so far, with logging on and storage in the test's folder."""
    monkeypatch.delenv('TARE_HEADS_UP', raising=False)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    path = tmp_path/'storage'/'serve'/'heads-up.jsonl'
    return lambda: [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def kept(tmp_path, notes):
    """A session folder whose heads-up.json holds each (note, reply) as a note of its own turn."""
    folder = tmp_path/'kept'
    folder.mkdir(exist_ok=True)
    for note, reply in notes:
        heads_up.keep(folder, [note], {'request': 'Request', 'reply': reply}, {})
    return folder


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def test_a_label_is_kept_on_its_note_and_logged_without_the_text(tmp_path, log):
    folder = kept(tmp_path, [('First note.', 'reply one'), ('Second note.', 'reply two')])
    first, second = heads_up.shown(folder)['noted']
    assert second['turn'] == digest('reply two') and second['funnel'] == heads_up.funnel()
    assert heads_up.label(folder, 'Second note.', 'useful') is True
    first, second = heads_up.shown(folder)['noted']
    assert second['label'] == 'useful' and 'label' not in first
    [row] = log()
    assert (row['event'], row['value'], row['source'], row['funnel']) == ('label', 'useful', 'tui', heads_up.funnel())
    assert row['reply_sha256'] == digest('reply two') and 'Second note' not in json.dumps(row)
    assert heads_up.label(folder, 'Unknown note.', 'useful') is False and len(log()) == 1  # nothing written or logged
    with pytest.raises(ValueError):
        heads_up.label(folder, 'Second note.', 'maybe')
    assert heads_up.label(folder, 'Second note.', 'not_useful') is True  # the last one wins
    assert heads_up.shown(folder)['noted'][1]['label'] == 'not_useful' and [r['value'] for r in log()] == [
        'useful', 'not_useful']
    heads_up.keep(folder, ['Third note.'], {'request': 'r', 'reply': 'reply three'}, {'since': 2})
    state = heads_up.shown(folder)
    assert [n.get('label') for n in state['noted']] == [None, 'not_useful', None] and state['since'] == 2


def test_a_note_counts_as_asked_only_when_a_request_carries_it(tmp_path, log, monkeypatch):
    folder = kept(tmp_path, [('First note.', 'reply one'), ('Second note.\nOn two lines.', 'reply two')])
    judged = []
    monkeypatch.setattr(heads_up, 'confirms', lambda turn, note: judged.append(note) or .9)
    assert heads_up.followed(folder, {'request': 'Something else', 'reply': 'x'}) == [] and log() == []
    # filling the composer logs nothing; the ask text sent in a request does, once, with the note's own turn
    ran = [{'tool': 'read', 'status': 'succeeded'}, {'tool': 'terminal_read', 'status': 'returned'},
           {'tool': 'read', 'status': 'refused'}]  # a refused step checked nothing
    assert heads_up.followed(folder, {'request': heads_up.ask(['First note.']), 'reply': 'Confirmed.',
                                      'steps': ran}) == ['First note.']
    asked_row, checked = log()
    assert (asked_row['event'], asked_row['reply_sha256'], asked_row['funnel']) == ('asked', digest('reply one'),
                                                                                     heads_up.funnel())
    assert (checked['event'], checked['confirmed'], checked['reply_sha256'], checked['answer_sha256']) == (
        'checked', .9, digest('reply one'), digest('Confirmed.'))
    assert checked['steps'] == 2  # what the agent ran before answering: agreement alone is not a confirmation
    assert 'First note' not in json.dumps(log())
    first, second = heads_up.shown(folder)['noted']
    assert first['confirmed'] == .9 and first['steps'] == 2 and first['asked'] and 'asked' not in second
    assert heads_up.followed(folder, {'request': heads_up.ask(['First note.']), 'reply': 'Again.'}) == []
    assert len(log()) == 2 and judged == ['First note.']  # an entry is followed at most once
    # whitespace differs (the user wrapped the lines): still the same note
    assert heads_up.followed(folder, {'request': 'Check: Second note. On  two\n lines. Thanks', 'reply': 'r'}) == [
        'Second note.\nOn two lines.']
    assert log()[-2]['reply_sha256'] == digest('reply two')
    assert log()[-1]['steps'] == 0  # this reply agreed without running anything
    heads_up.keep(folder, [], {'reply': 'quiet'}, {'since': 1})
    assert [(bool(n['asked']), n['confirmed'], n['steps']) for n in heads_up.shown(folder)['noted']] == [
        (True, .9, 2), (True, .9, 0)]


def test_a_message_that_rewrote_the_note_still_counts_while_it_keeps_the_first_line(tmp_path, log, monkeypatch):
    folder = kept(tmp_path, [('First note.', 'reply one'), ('Second note.', 'reply two')])
    judged = []
    monkeypatch.setattr(heads_up, 'confirms', lambda turn, note: judged.append(note) or .8)
    rewritten = heads_up.ASK_HEAD + '\n- My own words about the second point, with more detail.\nCheck it.'
    # the first line alone names no note: nothing was put in the composer yet
    assert heads_up.followed(folder, {'request': rewritten, 'reply': 'r'}) == [] and log() == []
    heads_up.loaded(folder, ['First note.'])
    heads_up.loaded(folder, ['Second note.'])  # the newest one put in the composer is the one a rewrite is about
    assert log() == [] and [bool(n.get('loaded')) for n in heads_up.shown(folder)['noted']] == [True, True]
    # rewritten without the first line: nothing ties the message to a note
    assert heads_up.followed(folder, {'request': 'My own words about the second point.', 'reply': 'r'}) == []
    assert heads_up.followed(folder, {'request': rewritten, 'reply': 'It holds.'}) == ['Second note.']
    first, second = heads_up.shown(folder)['noted']
    assert second['edited'] is True and second['sent'] == rewritten and second['confirmed'] == .8
    assert 'asked' not in first and judged == ['Second note.']  # the judge reads the note as it was written
    asked_row, checked = log()
    assert (asked_row['event'], asked_row['edited'], asked_row['reply_sha256']) == ('asked', True, digest('reply two'))
    assert (checked['event'], checked['edited'], checked['confirmed']) == ('checked', True, .8)
    assert 'second point' not in json.dumps(log()) and 'Second note' not in json.dumps(log())
    # a message that carries a note's own text is about that note, whatever was loaded
    heads_up.loaded(folder, ['First note.'])
    assert heads_up.followed(folder, {'request': heads_up.ask(['First note.']), 'reply': 'r'}) == ['First note.']
    assert log()[-2]['edited'] is False and 'edited' not in heads_up.shown(folder)['noted'][0]


@pytest.mark.parametrize('fails', [False, True])
def test_a_judge_without_an_answer_still_logs_the_note_as_asked(tmp_path, log, monkeypatch, fails):
    def judge(*args, **kwargs):
        if fails:
            raise RuntimeError('Jev is down')
        return {}
    monkeypatch.setattr(turn_engine, 'judge_many', judge)
    folder = kept(tmp_path, [('First note.', 'reply one')])
    assert heads_up.followed(folder, {'request': heads_up.ask(['First note.']), 'reply': 'r'}) == ['First note.']
    assert [r['event'] for r in log()] == ['asked', 'checked'] and log()[1]['confirmed'] is None
    assert heads_up.shown(folder)['noted'][0]['confirmed'] is None


def test_the_calibration_report_sums_the_labels_and_what_use_confirmed(capsys):
    from scripts.heads_up_calibration import report
    report([{'event': 'label', 'value': 'useful', 'reply_sha256': 'a'},
            {'event': 'label', 'value': 'not_useful', 'reply_sha256': 'a'},  # the last label of a note wins
            {'event': 'label', 'value': 'useful', 'reply_sha256': 'b'},
            {'event': 'label', 'value': 'useful', 'reply_sha256': 'c'},
            {'event': 'label', 'value': 'not_useful', 'reply_sha256': 'd'},
            {'event': 'asked', 'reply_sha256': 'a'}, {'event': 'asked', 'reply_sha256': 'b', 'edited': True},
            {'event': 'asked', 'reply_sha256': 'c'},
            {'event': 'checked', 'confirmed': .9, 'steps': 3}, {'event': 'checked', 'confirmed': .93, 'steps': 0},
            {'event': 'checked', 'confirmed': .95},  # a row from before steps were counted: not a confirmation
            {'event': 'checked', 'confirmed': .2, 'steps': 4}, {'event': 'checked', 'confirmed': None, 'steps': 1}])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith('0 logged turns;')  # no event row is a turn
    assert lines[1] == ('2 notes labelled useful, 2 not useful; 3 sent to the agent (1 with the note rewritten), '
                        '1 confirmed by its reply after checking, 2 agreed without checking, '
                        '1 not confirmed, 1 without an answer.')


def test_a_miss_is_kept_with_the_users_words_and_logged_without_any_text(tmp_path, log):
    folder = session(tmp_path, reply='Done: all tests pass. ' + 'x' * 2000)
    reply = heads_up.from_journal([(folder, 'inv')])['reply']
    assert heads_up.missed(folder, '  The reply never said the verifier failed.  ') is True
    [entry] = heads_up.shown(folder)['missed']
    assert entry['text'] == 'The reply never said the verifier failed.' and entry['request'] == 'Fix the parser and run the tests'
    assert entry['reply'] == reply[:1600] and entry['funnel'] == heads_up.funnel() and entry['at']
    assert entry['turn'] == heads_up._digest(reply)  # the digest the turn's own log row carries as reply_sha256
    [row] = log()
    assert (row['event'], row['said'], row['source'], row['reply_sha256'], row['funnel']) == (
        'miss', True, 'tui', entry['turn'], heads_up.funnel())
    assert 'verifier' not in json.dumps(row) and 'Done' not in json.dumps(row) and 'parser' not in json.dumps(row)
    assert heads_up.missed(folder, source='serve') is True  # a miss without words still counts
    assert log()[-1]['said'] is False and log()[-1]['source'] == 'serve' and len(heads_up.shown(folder)['missed']) == 2
    wrapped = tmp_path/'wrapped'  # a TUI turn's request is wrapped: the end, the user's words, is kept
    wrapped.mkdir()
    (wrapped/'journal.jsonl').write_text('\n'.join(json.dumps({'event': e}) for e in [
        {'kind': 'session_started', 'instruction': 'w' * 2000 + '\n\nOperator message:\nMeasure it'},
        {'kind': 'terminal', 'output': 'Measured.'}]), encoding='utf-8')
    assert heads_up.missed(wrapped) is True
    assert heads_up.shown(wrapped)['missed'][0]['request'].endswith('Operator message:\nMeasure it')
    assert len(heads_up.shown(wrapped)['missed'][0]['request'].encode('utf-8')) <= 900


def test_a_miss_needs_a_finished_exchange(tmp_path, log):
    empty = tmp_path/'empty'
    empty.mkdir()
    (empty/'journal.jsonl').write_text('', encoding='utf-8')
    unfinished = tmp_path/'unfinished'
    unfinished.mkdir()
    (unfinished/'journal.jsonl').write_text(json.dumps({'event': {'kind': 'user_message', 'text': 'Still working'}}),
                                            encoding='utf-8')
    for folder in (empty, unfinished, tmp_path/'missing'):
        assert heads_up.missed(folder, 'x') is False
    assert not list(tmp_path.glob('*/heads-up.json')) and log() == []


def test_a_miss_survives_what_rewrites_the_state(tmp_path, log, monkeypatch):
    monkeypatch.setattr(heads_up, 'confirms', lambda turn, note: .9)
    folder = session(tmp_path)
    assert heads_up.missed(folder, 'No warning.') is True
    heads_up.keep(folder, ['A note.'], {'request': 'r', 'reply': 'reply'}, {'since': 1})
    assert heads_up.label(folder, 'A note.', 'useful') is True
    heads_up.loaded(folder, ['A note.'])
    assert heads_up.followed(folder, {'request': heads_up.ask(['A note.']), 'reply': 'r'}) == ['A note.']
    heads_up.keep(folder, [], {'reply': 'quiet'}, {'since': 2})
    assert [m['text'] for m in heads_up.shown(folder)['missed']] == ['No warning.']


def test_the_calibration_report_counts_the_flagged_replies_and_never_as_turns(capsys):
    from scripts.heads_up_calibration import report
    writer = [{'route': 'sonnet', 'ok': True, 'ms': 2000}]
    report([{'score': .3, 'forwarded': 'score', 'notes': 0, 'writers': writer, 'reply_sha256': 'a'},  # wrote none
            {'score': .3, 'forwarded': 'score', 'notes': 1, 'writers': writer, 'reply_sha256': 'b'},  # a note anyway
            {'score': .02, 'forwarded': None, 'notes': 0, 'writers': [], 'reply_sha256': 'c'},  # not run
            {'event': 'miss', 'reply_sha256': 'a'}, {'event': 'miss', 'reply_sha256': 'a'},  # one reply, flagged twice
            {'event': 'miss', 'reply_sha256': 'b'}, {'event': 'miss', 'reply_sha256': 'c'},
            {'event': 'miss', 'reply_sha256': 'z'}])  # a turn this log does not hold
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith('3 logged turns; the writer answered 2')
    assert lines[3] == ('4 replies flagged as missing a note: 1 where the writer ran and wrote none, '
                        '1 where a note was shown anyway, 1 where the writer did not run, 1 not in this log.')


def test_the_misses_are_listed_beside_the_notes(tmp_path, log):
    from scripts.heads_up_calibration import missed
    folder = session(tmp_path)
    assert heads_up.missed(folder, 'No warning.') is True
    [found] = missed(folder.parent)
    assert found['session'] == folder.name and found['text'] == 'No warning.' and found['turn']
    assert missed(folder.parent, '9999') == []


def test_the_writer_uses_the_owners_heads_up_effort(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'heads_up': ['opus'], 'heads_up_effort': 'high'})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'id': name, 'native_profile': {'model': name}}, None))
    monkeypatch.setattr(turn_engine, 'with_effort', lambda deployment, effort: {**deployment, 'effort': effort})
    seen = []
    monkeypatch.setattr('relay.native_generation.generate_text',
                        lambda deployment, *a, **kw: seen.append(deployment.get('effort')) or (True, 'NONE', {}))
    heads_up.write('{}', [])
    assert seen == ['high']
    # A change of routes keeps the owner's reasoning levels.
    monkeypatch.undo()  # the real policy and routes
    monkeypatch.setattr(turn_engine, 'policy_path', lambda: tmp_path/'routing.json')
    (tmp_path/'routing.json').write_text(json.dumps({'heads_up': ['claude-sonnet-5-5-cli'], 'heads_up_effort': 'high'}))
    saved = turn_engine.save_policy({'heads_up': [['codex-cli-gpt-6-1-sol']]})
    assert saved['heads_up'] == ['codex-cli-gpt-6-1-sol'] and saved['heads_up_effort'] == 'high'


def implemented(files=None, verdict=None, status='succeeded', reason=None):
    """An `implement` step as the journal records it: files_scope says whether the change reached the project."""
    output = {**({'verdict': verdict} if verdict else {}), **({'reason': reason} if reason else {}),
              **({'files': ['a.py'], 'files_scope': files} if files else {})}
    return heads_up.step('implement', {'note': 'Do it.'}, status, output)


UNAPPLIED = 'unapplied candidate, not project files'


def test_an_implement_step_keeps_its_verdict_and_where_its_files_went():
    step = implemented(UNAPPLIED, 'NOT_REVIEWED')
    assert (step['verdict'], step['files']) == ('NOT_REVIEWED', UNAPPLIED)
    assert step['summary'] == f'verdict NOT_REVIEWED; files {UNAPPLIED}'  # unchanged
    assert implemented('project')['files'] == 'project' and 'verdict' not in implemented('project')
    assert 'files' not in heads_up.step('implement', {}, 'succeeded', {'verdict': 'PASS', 'files_scope': 'project'})
    other = heads_up.step('demand', {}, 'succeeded', {'verdict': 'PASS', 'files': ['a.py'], 'files_scope': 'project'})
    assert 'verdict' not in other and 'files' not in other and other['summary'] == 'verdict PASS; files project'
    refused = implemented(status='refused', reason='no budget')
    assert 'verdict' not in refused and refused['reason'] == 'no budget'


def test_certain_says_what_the_last_implementation_did_then_what_tare_checked():
    def turn(*steps):
        return {'request': 'x', 'reply': 'y', 'steps': list(steps)}
    failed = heads_up.step('terminal_read', {}, 'failed', {'exit_code': 1, 'command': PYTEST, 'output': '1 failed'})
    assert heads_up.certain(turn(heads_up.step('read', {'path': 'a'}, 'succeeded', {}))) == []
    assert heads_up.certain(turn(failed)) == heads_up.facts(turn(failed)) == [
        'The last run of `' + PYTEST + '` failed (exit 1): 1 failed.']
    assert heads_up.certain(turn(implemented(status='refused', reason='no budget'))) == [
        'The last implementation attempt did not run (refused: no budget); it changed nothing.']
    assert heads_up.certain(turn(implemented(status='failed'))) == [
        'The last implementation attempt did not run (failed); it changed nothing.']
    assert heads_up.certain(turn(implemented(UNAPPLIED, 'NOT_REVIEWED'))) == [
        'The last implementation attempt is a kept candidate (verdict NOT_REVIEWED): its changes are NOT in the '
        'project files.']
    assert heads_up.certain(turn(implemented(UNAPPLIED))) == [
        'The last implementation attempt is a kept candidate: its changes are NOT in the project files.']
    assert heads_up.certain(turn(implemented('project', 'PASS'))) == [
        'The last implementation attempt was applied to the project files (verdict PASS).']
    assert heads_up.certain(turn(implemented('project'))) == [
        'The last implementation attempt was applied to the project files.']
    assert heads_up.certain(turn(implemented())) == []  # no outcome recorded: nothing is certain
    # the last implement step wins, and the delivery sentence comes before the facts
    both = turn(implemented('project', 'PASS'), failed, implemented(UNAPPLIED, 'REVISE'))
    assert heads_up.certain(both) == [
        'The last implementation attempt is a kept candidate (verdict REVISE): its changes are NOT in the project '
        'files.', 'The last run of `' + PYTEST + '` failed (exit 1): 1 failed.']


def test_events_name_the_points_where_the_writer_runs():
    def kinds(*steps):
        turn = {'request': 'x', 'reply': 'y', 'steps': list(steps)}
        return heads_up.events(turn, heads_up.facts(turn))
    edit = heads_up.step('edit', {'path': 'a.py'}, 'succeeded', {})
    failed = heads_up.step('terminal_read', {}, 'failed', {'exit_code': 1, 'command': PYTEST, 'output': '1 failed'})
    assert kinds(implemented(status='refused')) == ['delivery']  # whatever its outcome
    assert kinds(edit) == ['edit'] and kinds(failed) == ['check']
    assert kinds(failed, implemented('project'), edit) == ['delivery', 'edit', 'check']
    for status in ('failed', 'refused'):  # not an edit event; tare's fact that it was not redone is a check
        assert kinds(heads_up.step('edit', {'path': 'a.py'}, status, {'reason': 'no'})) == ['check']
    assert kinds(heads_up.step('read', {'path': 'a.py'}, 'succeeded', {}),
                 heads_up.step('delegate_plan', {}, 'succeeded', {'verdict': 'PASS'})) == []


def test_an_event_runs_the_writer_below_the_score_and_the_cadence_and_tells_it_the_facts(tmp_path, monkeypatch, log):
    told = []  # heads_up.write is stubbed: this test is about what notes() hands it
    monkeypatch.setattr(heads_up, 'write', lambda text, seen, known=(): told.append((json.loads(text), list(known))) or (
        [], [{'route': 'sonnet', 'ok': True, 'ms': 5}]))
    delivered = {'request': 'Add the flag', 'reply': 'Done.', 'steps': [implemented(UNAPPLIED, 'NOT_REVIEWED')]}
    quiet = {'request': 'Explain', 'reply': 'It is a parser.', 'steps': [heads_up.step('read', {'path': 'a'}, 'succeeded', {})]}
    assert heads_up.score(delivered, []) < heads_up.FORWARD_AT
    pace = {}
    heads_up.notes(delivered, pace=pace)  # since is 1: the cadence is not due
    assert len(told) == 1 and pace == {'since': 0, 'held': []}
    assert told[0][1] == ['The last implementation attempt is a kept candidate (verdict NOT_REVIEWED): its changes are '
                          'NOT in the project files.']
    heads_up.notes(quiet, pace=pace)  # no event, a low score, the cadence not due: the writer does not run
    assert len(told) == 1 and pace['since'] == 1
    held = {'request': 'Earlier', 'reply': 'r', 'steps': [implemented('project', 'PASS')]}
    heads_up.notes(quiet, pace={'since': 0, 'held': heads_up.hold([], held)})  # a held turn's delivery is not this turn's
    assert len(told) == 1
    rows = log()
    assert [(r['forwarded'], r['events']) for r in rows] == [('event', ['delivery']), (None, []), (None, [])]
    assert 'known' not in json.dumps(rows) and 'NOT in the project' not in json.dumps(rows)  # never the texts


def test_the_writer_prompt_carries_the_facts_between_the_notes_shown_and_the_record(monkeypatch):
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'heads_up': ['sonnet']})
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'id': name, 'native_profile': {'model': name}}, None))
    prompts = []
    monkeypatch.setattr('relay.native_generation.generate_text',
                        lambda deployment, model, prompt, timeout, **kw: prompts.append(prompt) or (True, 'NONE', {}))
    heads_up.write('{}', ['Old note.'], ['A is true.', 'B is true.'])
    heads_up.write('{}', ['Old note.'])
    assert prompts[0] == ('Notes already shown to the user (never repeat these):\n- Old note.\n\n'
                          'Facts the host is certain of (never contradict them):\n- A is true.\n- B is true.'
                          '\n\nTurn record (JSON):\n{}')
    assert 'Facts the host' not in prompts[1]  # nothing known: no block


def test_the_writer_is_told_to_trust_the_facts_and_not_to_predict():
    assert 'never contradict them, and say a change is in the project only when they say it was applied' in heads_up.SYSTEM
    assert 'Never predict what the host or the agent will do next' in heads_up.SYSTEM


def test_the_report_counts_each_reason_the_writer_ran(capsys):
    from scripts.heads_up_calibration import report
    writer = [{'route': 'sonnet', 'ok': True, 'ms': 2000}]

    def row(reason, notes, sha):
        return {'score': .1, 'forwarded': reason, 'notes': notes, 'writers': writer, 'reply_sha256': sha}
    report([row('event', 1, 'a'), row('event', 1, 'b'), row('event', 0, 'c'), row('cadence', 1, 'd'),
            row('score', 0, 'e'), {'score': .1, 'forwarded': None, 'notes': 0, 'writers': [], 'reply_sha256': 'f'},
            {'event': 'label', 'value': 'not_useful', 'reply_sha256': 'a'},
            {'event': 'label', 'value': 'useful', 'reply_sha256': 'a'},  # the last label of a reply wins
            {'event': 'label', 'value': 'not_useful', 'reply_sha256': 'b'},
            {'event': 'label', 'value': 'useful', 'reply_sha256': 'd'}])
    assert capsys.readouterr().out.splitlines()[1] == (
        'By reason: event 3 forwarded, 2 with a note (1 useful, 1 not useful); score 1 forwarded, 0 with a note '
        '(0 useful, 0 not useful); cadence 1 forwarded, 1 with a note (1 useful, 0 not useful).')


def test_the_default_writer_is_a_mid_to_high_model_at_high_effort_and_no_small_model_stands_in():
    default = turn_engine.DEFAULT_POLICY
    assert default['heads_up'] == ['claude-sonnet-5-5-cli', 'codex-cli-gpt-6-1-sol']
    assert default['heads_up_effort'] == 'high'
    assert not any('haiku' in name or 'luna' in name or 'local' in name for name in default['heads_up'])


CONTRACT = {'layout': 'chat-inclusive', 'source': 'test'}
RECEIPT = {'attempt_id': 'a1', 'usage': {'prompt_tokens': 1200, 'completion_tokens': 80},
           'cost': {'currency': 'USD', 'known_amount': '0.0123', 'complete': True}}


@pytest.fixture
def writer(monkeypatch, tmp_path):
    """One writer route; the process-wide stop and the machine storage belong to this test alone."""
    monkeypatch.setattr(heads_up, '_token', None)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'heads_up': ['w']})

    def route(**deployment):
        monkeypatch.setattr(turn_engine, 'available', lambda name: (
            {'id': 'w', 'native_profile': {'model': 'm'}, 'native_usage_contract': CONTRACT, **deployment}, None))
    route()
    route.ledger = tmp_path/'storage'/'serve'/'provider-spend.jsonl'
    return route


def answers(monkeypatch, receipt, ok=True):
    monkeypatch.setattr('relay.native_generation.generate_text',
                        lambda *a, **kw: (ok, '- The verifier failed.' if ok else '', receipt))


def test_the_writer_keeps_tokens_and_dollars_and_no_text(monkeypatch, writer):
    answers(monkeypatch, RECEIPT)
    notes, [attempt] = heads_up.write('{}', [])
    assert notes == ['The verifier failed.']
    assert attempt['tokens']['input_tokens'] == 1200 and attempt['tokens']['output_tokens'] == 80
    assert attempt['usd'] == 0.0123 and 'verifier' not in json.dumps(attempt)
    # Missing usage is unknown, not zero: the keys stay out.
    answers(monkeypatch, {'usage': None, 'cost': {'currency': 'USD', 'known_amount': None}})
    [attempt] = heads_up.write('{}', [])[1]
    assert 'tokens' not in attempt and 'usd' not in attempt
    answers(monkeypatch, {})
    assert heads_up.write('{}', [])[1][0].keys() == {'route', 'ok', 'ms'}
    # A failed call may carry its usage too.
    answers(monkeypatch, {**RECEIPT, 'failure_class': 'RESPONSE_INCOMPLETE'}, ok=False)
    [attempt] = heads_up.write('{}', [])[1]
    assert attempt['failure'] == 'RESPONSE_INCOMPLETE' and attempt['tokens']['input_tokens'] == 1200


def test_a_paid_route_posts_to_the_ledger_and_a_subscription_posts_nothing(monkeypatch, writer):
    answers(monkeypatch, RECEIPT)
    writer(native_credential_env='OPENAI_API_KEY')
    heads_up.write('{}', [])
    [row] = [json.loads(line) for line in writer.ledger.read_text(encoding='utf-8').splitlines()]
    assert (row['account'], row['usd'], row['call'], row['deployment']) == ('openai-api', 0.0123, 'a1', 'w')
    writer.ledger.unlink()
    writer()  # a subscription or local route: no API account
    heads_up.write('{}', [])
    assert not writer.ledger.exists()


def test_a_ledger_failure_does_not_lose_the_note(monkeypatch, writer):
    answers(monkeypatch, RECEIPT)

    def refuse(*a, **kw):
        raise OSError('disk full')
    monkeypatch.setattr('relay.provider_spend.record', refuse)
    notes, [attempt] = heads_up.write('{}', [])
    assert notes == ['The verifier failed.'] and attempt['usd'] == 0.0123


def test_provider_spend_record_posts_a_call_without_a_session_marker(tmp_path, monkeypatch):
    from relay import provider_spend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    deployment = {'id': 'w', 'native_credential_env': 'ANTHROPIC_API_KEY'}
    call = {'call_id': 'c', 'cost': {'currency': 'USD', 'known_amount': '0.5'}}
    assert provider_spend.record(deployment, [call, {'call_id': 'free', 'cost': None}], now=7) == 1
    assert provider_spend.spent('anthropic-api', 0) == .5 and not list(tmp_path.glob('**/spend-posted.json'))


def test_stop_cancels_a_call_in_flight_and_starts_no_new_one(monkeypatch, writer):
    started, seen = threading.Event(), []

    def generate_text(deployment, model, prompt, timeout, **kw):
        seen.append(kw['control'])
        started.set()
        end = time.monotonic() + 5
        while not kw['control'].cancellation.is_cancelled() and time.monotonic() < end:
            time.sleep(.01)
        return False, '', {'failure_class': 'CONTROL_STOPPED'}
    monkeypatch.setattr('relay.native_generation.generate_text', generate_text)
    result = []
    worker = threading.Thread(target=lambda: result.append(heads_up.write('{}', [])))
    worker.start()
    assert started.wait(5)
    heads_up.stop()
    worker.join(5)
    assert not worker.is_alive() and seen[0].cancellation.is_cancelled()
    assert result[0][0] == [] and [a['failure'] for a in result[0][1]] == ['CONTROL_STOPPED']
    assert heads_up.write('{}', []) == ([], []) and len(seen) == 1  # stopped for good: no second call


def test_usage_totals_one_conversation_and_counts_older_rows_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    new = {'route': 'w', 'ok': True, 'ms': 2000, 'tokens': {'input_tokens': 1200, 'output_tokens': 80}, 'usd': 0.0123}
    old = {'route': 'w', 'ok': False, 'ms': 500, 'failure': 'CLI_USAGE_LIMIT'}
    rows = [{'session': 's1', 'writers': [old, new]}, {'session': 's1', 'writers': [new]},
            {'session': 's1', 'writers': []}, {'session': 's1'}, {'session': 's2', 'writers': [new]}]
    log = tmp_path/'serve'/'heads-up.jsonl'
    log.parent.mkdir(parents=True)
    log.write_text(''.join(json.dumps(r) + chr(10) for r in rows) + '{torn' + chr(10), encoding='utf-8')
    assert heads_up.usage(tmp_path/'s1') == {
        'turns': 2, 'calls': 3, 'unknown': 1, 'input_tokens': 2400, 'output_tokens': 160, 'ms': 4500,
        'usd': 0.0246, 'usd_unknown': 1}
    assert heads_up.usage(tmp_path/'s3')['calls'] == 0
