"""Heads-up notes: after a completed turn, the one thing the user should know that the final reply left unclear.

A funnel, cheapest stage first:
1. Collect, no model: the turn's recorded steps, what code counts in them, and the facts tare checks itself (a test
   whose last run failed, files changed after the last test ran, an edit refused and never redone).
2. Filter, no model: a logistic score over those counts (FILTER) sends a turn to the writer at FORWARD_AT or more.
   Hosted Jev rates the turn beside it for calibration only: on labeled history its questions added nothing.
3. Write: the first available route of the routing policy's `heads_up` role (/routes changes it) writes at most
   MAX_NOTES note, or none, about consequences: a measurement that may not hold, a cost to come, a changed default,
   a rule that can catch too much, a silent failure.
4. Check: hosted Jev drops a note the reply already says (a Noul, REDUNDANT_AT); without Jev the note stands.

Each decision is logged for calibration (<storage>/serve/heads-up.jsonl: scores, probabilities, routes, times and
sizes, never the texts), SAMPLE of the filtered turns still reach the writer, so the log measures what the filter
drops, and the TUI's /heads-up logs that the user sent a note to the agent (asked()), a label from use.
scripts/heads_up_calibration.py replays history through the funnel and reports the trade. Every frontend calls
notes(); a failure here never fails the turn it reports on. TARE_HEADS_UP=off disables it.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import time

# The filter: a logistic score over what code counts in a turn (counts()), {count: (mean, spread, weight)}. Fitted
# 2026-11-05 on 249 Claude Code turns, labeled by whether the user accepted a side-agent note after them
# (scripts/heads_up_calibration.py claude --claims v2): AUC 0.68 cross-validated, where hosted Jev's questions, asked
# as TypeSafe advises, added nothing (0.66). FORWARD_AT keeps 90% of the noted turns and drops 30% of the quiet ones.
# ponytail: fitted on another agent's transcripts; refit on tare's own log (sampled turns, /heads-up) as it grows
FILTER = {'facts': (.157, .405, .205), 'edits': (2.566, 5.373, .235), 'steps': (24.61, 42.822, .043),
          'tests': (3.426, 7.791, .181), 'failures': (.486, 1.155, .164), 'delegated': (.141, .411, .061),
          'request_kb': (.782, 3.329, -.129), 'reply_kb': (1.461, .999, -.392)}
FILTER_BIAS = -1.517
FORWARD_AT = .137
SAMPLE = 0.1  # filtered turns that still reach the writer: the log's measure of what the filter drops
ALREADY_SAID = 'Does `assistant_final_reply` already tell the user what `note` says?'
REDUNDANT_AT = .7  # TypeSafe's starting threshold for a Noul's yes
MAX_NOTES = 1  # one note, like the side agents users accepted notes from
NOTE_CHARS = 500
WRITER_BYTES = 48000  # the writer reads the conversation: a consequence often rests on what came before
WRITER_EARLIER = .45  # of that, the share for earlier exchanges
WRITER_SECONDS = 180
STEP_CHARS = 300
DELEGATED = ('explore', 'implement', 'delegate_plan', 'demand', 'research')  # tools whose work runs elsewhere
RECORD = 5  # what brief(), record() and facts() show; bump it when that changes, so calibration starts anew
EARLIER_TURNS = 8  # earlier exchanges a turn carries as context, newest kept first when the record must shrink

QUESTION = ('After this coding assistant turn, is there something the user should know that the final reply '
            'does not say, or says inaccurately?')
OPTIONS = {
    'note': 'Yes: the reply says it did, ran or checked something the recorded steps contradict or do not show, or '
            'it leaves out a failure, an unfinished part or a side effect the steps show.',
    'none': 'No: the reply is consistent with the recorded steps. Facts it states may come from context not shown '
            '(project instructions, earlier turns, delegated explorers).',
}
QUIET = 'none'  # the option that means "nothing to tell": p_note is 1 minus its probability
JUDGE_BYTES = 8192  # the choice assessment's request bound (compute_plane.choice_assessment)

# What the side agent looks for. Taken from the kinds of note users of a side agent accepted (scripts/
# heads_up_calibration.py claude): consequences of what was done or said, not process hygiene, which tare's facts
# cover without a model.
SYSTEM = ("You watch a coding agent's conversation from the side, for its user. You get the conversation before "
          "this turn (earlier_in_conversation), the user's request, the tool steps the host recorded (observations, "
          "not claims), facts tare checked itself, and the agent's final reply. Decide whether there is one thing "
          "the user should know before relying on what was just done or said, which the reply does not make clear. "
          "Look for: a measurement, comparison or test whose setup differs from what will really run (another "
          "model or configuration, a stand-in, a single run, numbers from different rounds), so its conclusion may "
          "not hold; a cost the user will pay later (quota, tokens, money, disk, time, history that cannot be "
          "removed); behavior that changes for the user or others (a new default, a removed check, a promise in "
          "docs or UI that the code does not keep); a rule, filter or cleanup that can catch or delete more than "
          "intended; a failure that would stay silent, with nothing to alert anyone; a claim in the reply that "
          "the record contradicts. The agent also saw context that is not shown, so something missing from the "
          "steps is never by itself a reason. Ignore process details that do not change what the user should "
          "believe or do, and tare's facts, which the user sees separately; never give advice, suggestions or "
          "style opinions, and never repeat a note already shown. Most turns need nothing: then answer exactly "
          "NONE. Otherwise answer one line starting with '- ': one or two plain sentences in the language of the "
          "user's messages, saying what holds and why it matters, without the transcript's jargon.")


def funnel():
    """The funnel's version in each log row: calibration compares only rows that ran the same texts and threshold
    (a function, so a calibration variant that replaces QUESTION, OPTIONS or QUIET gets its own)."""
    value = [QUESTION, OPTIONS, QUIET, SYSTEM, FILTER, FILTER_BIAS, FORWARD_AT, ALREADY_SAID, REDUNDANT_AT, RECORD]
    return hashlib.sha256(json.dumps(value).encode('utf-8')).hexdigest()[:12]

ASK = ("tare's heads-up after your last reply:\n{notes}\nCheck whether each point holds, using the tools, "
       "and tell me what you find.")

# ponytail: a command name list, not a parser; a terminal command that matches is a test the "changed after" fact
# compares edits against (named verifiers always are)
TEST = re.compile(r'pytest|unittest|\btests?\b|\btox\b|\bnox\b|cargo (?:test|check|build)|go (?:test|vet|build)|'
                  r'npm (?:run )?test|pnpm test|yarn test|dotnet (?:test|build)|\bmvn\b|gradle|ctest|\btsc\b|ruff|'
                  r'mypy|eslint|check_\w+\.py', re.I)


def store():
    """The machine's choice, for every frontend (the TUI option); TARE_HEADS_UP_STORE overrides the file."""
    from .native_installation import descriptor_path
    return Path(os.environ.get('TARE_HEADS_UP_STORE') or descriptor_path().parent/'heads-up.json')


def enabled():
    """On unless the machine turned it off (the TUI option) or TARE_HEADS_UP=off."""
    if os.environ.get('TARE_HEADS_UP', '').lower() == 'off':
        return False
    try:
        return json.loads(store().read_text(encoding='utf-8')).get('on') is not False
    except (OSError, ValueError, AttributeError):
        return True


def set_enabled(on):
    path = store()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')  # the TUI and tare serve may write at once
    temporary.write_text(json.dumps({'on': bool(on)}), encoding='utf-8')
    os.replace(temporary, path)


def _loads(text):
    try:
        value = json.loads(text or '{}')
    except ValueError:
        return {'output': text}
    return value if isinstance(value, dict) else {'output': value}


def _text(value):
    """A message content (text or parts), or a terminal read's {'stderr': {'text'}, 'stdout': {'text'}}."""
    if isinstance(value, list):
        return '\n'.join(p.get('text', '') for p in value if isinstance(p, dict))
    if isinstance(value, dict):  # stdout last: its last line is the summary (pytest's "1 failed, 16 passed")
        return '\n'.join(str((value.get(k) or {}).get('text') or '') for k in ('stderr', 'stdout'))
    return '' if value is None else str(value)


def _paths(value, found):
    """Every 'path' a delegated tool's output names, in order."""
    if isinstance(value, dict):
        if isinstance(value.get('path'), str):
            found.append(value['path'])
        for item in value.values():
            _paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _paths(item, found)
    return found


def _last_line(text):
    return next((line.strip() for line in reversed(_text(text).splitlines()) if line.strip()), '')[:200]


def step(tool, args, status, output):
    """One recorded tool call, reduced to what the funnel reads."""
    subject = next((str(args[k]) for k in ('path', 'name', 'query', 'pattern', 'command', 'url') if args.get(k)), '')
    value = {'tool': tool, 'subject': subject[:200], 'status': status}
    if tool == 'terminal_read' and output.get('exit_code') is not None:
        value.update(subject=str(output.get('command') or '')[-160:], exit_code=output['exit_code'])
    if status in ('failed', 'refused'):
        value['reason'] = str(output.get('reason') or output.get('error') or _last_line(output.get('output')))[:200]
    elif tool in DELEGATED:  # its reads and checks are not this turn's steps, and 'succeeded' is only the call
        said = [f'{key} {output[key]}' for key in ('verdict', 'status', 'reason') if isinstance(output.get(key), str)]
        cited = ', '.join(dict.fromkeys(_paths(output, [])))
        value['summary'] = '; '.join(said + (['cited ' + cited] if cited else []))[:240]
    elif tool in ('verify', 'terminal_read') or status == 'returned':
        value['summary'] = _last_line(output.get('output'))
    return value


def from_journal(runs):
    """A TUI or tare serve turn from its sessions' journals: runs are (session folder, invocation id), oldest first."""
    from .native_agent import read_json
    requests, steps, reply, earlier = [], [], '', None
    for session, invocation_id in runs:
        session = Path(session)
        row = next(r for r in read_json(session/'native-agent.json', 1024*1024)['invocations'] if r['id'] == invocation_id)
        lines = (session/'journal.jsonl').read_text(encoding='utf-8').splitlines()
        if earlier is None:  # the conversation before the turn: its first session's earlier exchanges
            earlier = _exchanges(json.loads(line)['event'] for line in lines[:row.get('journal_start') or 0])
        asked = {}
        for line in lines[row.get('journal_start') or 0:row.get('journal_end')]:
            event = json.loads(line)['event']
            kind = event.get('kind')
            if kind in ('user_message', 'session_started') and (event.get('text') or event.get('instruction')):
                requests.append(event.get('text') or event.get('instruction'))
            elif kind == 'effect_requested':
                asked[event['effect_id']] = event.get('tool_call') or {}
            elif kind == 'effect_returned':
                call, result = asked.get(event['result']['effect_id'], {}), event['result']
                steps.append(step(call.get('name', '?'), _loads(call.get('arguments_json')), result.get('status'),
                                  _loads(result.get('output_json'))))
            elif kind == 'terminal' and event.get('output'):
                reply = event['output']
    return {'request': '\n\nLater message from the user:\n'.join(requests), 'steps': steps, 'reply': reply,
            'earlier': earlier or []}


def _exchanges(events):
    """[{'user', 'assistant'}] of a journal's finished turns, oldest first, at most EARLIER_TURNS."""
    found, user = [], ''
    for event in events:
        if event.get('kind') in ('user_message', 'session_started'):
            user = event.get('text') or event.get('instruction') or user
        elif event.get('kind') == 'terminal' and event.get('output'):
            found.append({'user': user, 'assistant': event['output']})
    return found[-EARLIER_TURNS:]


def from_messages(messages, reply):
    """A tare-copilot turn: the client's tool calls and results since the user's last message (VS Code runs them)."""
    start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=-1)
    earlier = []
    for message in messages[:max(start, 0)]:
        if message.get('role') == 'user':
            earlier.append({'user': _text(message.get('content')), 'assistant': ''})
        elif message.get('role') == 'assistant' and earlier and _text(message.get('content')).strip():
            earlier[-1]['assistant'] = _text(message.get('content'))  # the last text of that exchange
    calls, steps = {}, []
    for message in messages[start + 1:]:
        for call in message.get('tool_calls') or []:
            calls[call.get('id')] = call.get('function') or {}
        if message.get('role') == 'tool':
            function = calls.get(message.get('tool_call_id'), {})
            steps.append(step(function.get('name', '?'), _loads(function.get('arguments')), 'returned',
                              {'output': _text(message.get('content'))}))
    return {'request': _text(messages[start].get('content')) if start >= 0 else '', 'steps': steps, 'reply': reply or '',
            'earlier': earlier[-EARLIER_TURNS:]}


def _check(s):
    if s['tool'] == 'verify':
        return 'verify ' + s['subject'], s['status'] == 'succeeded', True
    if s.get('exit_code') is not None:
        return '`' + s['subject'] + '`', s['exit_code'] == 0, bool(TEST.search(s['subject']))
    return None, None, None


def facts(turn):
    """What tare tells from the recorded steps alone, no model: tests and verifiers whose last run failed, files
    changed after the last test ran, edits whose last attempt was refused."""
    found, last, tested, edits = [], {}, None, {}
    for index, s in enumerate(turn['steps']):
        key, passed, test = _check(s)
        if test:  # a failed grep or ls says nothing; a failed test or verifier does
            last[key] = (passed, s)
            tested = (index, key)
        if s['tool'] == 'edit':
            edits.setdefault(s['subject'], []).append((index, s['status'], s.get('reason')))
    for key, (passed, s) in last.items():
        if not passed:
            found.append(f"The last run of {key} failed" + (f" (exit {s['exit_code']})" if 'exit_code' in s else '')
                         + (f": {s['reason']}" if s.get('reason') else '') + '.')
    if tested:
        late = sorted(path for path, tries in edits.items()
                      if any(i > tested[0] and status == 'succeeded' for i, status, _ in tries))
        if late:
            found.append(f"{', '.join(late[:5])}{' and others' if len(late) > 5 else ''} changed after the last "
                         f"test ({tested[1]}) ran; the final version was not tested.")
    for path, tries in edits.items():
        if tries[-1][1] in ('failed', 'refused'):
            found.append(f"The last edit of {path} was {tries[-1][1]}"
                         + (f" ({tries[-1][2]})" if tries[-1][2] else '') + ' and not redone.')
    return found


def _clip(text, limit):
    return (text or '').encode('utf-8')[:max(int(limit), 0)].decode('utf-8', 'ignore')


def _line(s):
    parts = [s['tool'], s['subject'], '->', str(s['status'])]
    if 'exit_code' in s:
        parts.append(f"(exit {s['exit_code']})")
    return _clip(' '.join(parts + [s.get('reason') or s.get('summary') or '']).strip(), STEP_CHARS)


def _tail(text, limit):
    raw = (text or '').encode('utf-8')
    return raw[len(raw) - int(limit):].decode('utf-8', 'ignore') if len(raw) > limit else text or ''


def writer_record(turn, found):
    return record(turn, found, WRITER_BYTES, WRITER_EARLIER)


def record(turn, found, budget, earlier_share=.25):
    """The turn as the writer reads it, within `budget` UTF-8 bytes: the newest steps and the newest earlier
    exchanges are kept. An earlier user message keeps its end, where a frontend's wrapper puts their words."""
    lines = [_line(s) for s in turn['steps']]
    earlier = turn.get('earlier') or []
    for keep, share in ((len(lines), 1), (60, 1), (30, .6), (12, .4), (4, .25), (0, .15)):
        shown = lines[max(len(lines) - keep, 0):]
        context, room = [], budget * earlier_share * share
        each = room / 5  # escaped twice when embedded, so the newest exchange fits even when dense with quotes
        for item in reversed(earlier):
            exchange = {'user': _tail(item['user'], each * .4), 'assistant': _clip(item['assistant'], each * .6)}
            room -= len(json.dumps(exchange, ensure_ascii=False).encode('utf-8')) * 2  # escaped again when embedded
            if room < 0:
                break
            context.insert(0, exchange)
        value = json.dumps({'earlier_in_conversation': context,
                            'user_request': _clip(turn['request'], budget * .15 * share),
                            'facts_checked_by_tare': found,
                            'recorded_steps': (['... earlier steps left out'] if len(shown) < len(lines) else []) + shown,
                            'assistant_final_reply': _clip(turn['reply'], budget * .35 * share)}, ensure_ascii=False)
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) <= budget:  # as the judge request embeds it
            break
    return value


def brief(turn, found, reply=2500):
    """What the judge needs, not the whole record: the request's start, the reply (where the claims are), tare's
    facts, the files changed, the tests and verifiers run, delegated work and the last failures, in a few KiB."""
    steps = turn['steps']
    return json.dumps({
        'user_request': _clip(turn['request'], 600),
        'facts_checked_by_tare': found,
        'files_changed': sorted({s['subject'] for s in steps if s['tool'] == 'edit' and s['status'] == 'succeeded'})[:12],
        'tests_run': [_clip(_line(s), 200) for s in steps if _check(s)[2]][-4:],
        'delegated': [_clip(_line(s), 240) for s in steps if s['tool'] in DELEGATED][-3:],
        'failures': [_clip(_line(s), 200) for s in steps if s['status'] in ('failed', 'refused')][-3:],
        'assistant_final_reply': _clip(turn['reply'], reply)}, ensure_ascii=False)


def assessment(turn, found):
    """The judge's choice request for a turn: its brief, within the assessment bound (a reply dense with quotes
    doubles when escaped, so it shortens until the request fits)."""
    for reply in (2500, 1500, 800, 300):
        request = {'state': brief(turn, found, reply), 'question': QUESTION, 'options': OPTIONS}
        if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) <= JUDGE_BYTES:
            break
    return request


def parse(answer, seen):
    """The writer's notes: none for NONE; at most MAX_NOTES '- ' lines, never one already shown."""
    if answer.strip().upper().startswith('NONE'):
        return []
    known = {s.casefold() for s in seen}
    notes = [line.strip()[2:].strip()[:NOTE_CHARS] for line in answer.splitlines() if line.strip()[:2] in ('- ', '* ')]
    return [n for n in dict.fromkeys(notes) if n and n.casefold() not in known][:MAX_NOTES]


def write(text, seen):
    """(notes, attempts): the first available `heads_up` route that answers writes them; one handover on failure."""
    from .native_generation import generate_text, vault_secret
    from .turn_engine import DEFAULT_POLICY, available, policy
    prompt = ('Notes already shown to the user (never repeat these):\n' + ('\n'.join('- ' + s for s in seen) or 'none')
              + '\n\nTurn record (JSON):\n' + text)
    attempts = []
    for name in policy().get('heads_up') or DEFAULT_POLICY['heads_up']:
        deployment, _ = available(name)
        if deployment is None:
            continue
        started = time.monotonic()
        try:
            ok, answer, receipt = generate_text(deployment, deployment['native_profile']['model'], prompt,
                                                WRITER_SECONDS, system=SYSTEM, credential_resolver=vault_secret)
        except Exception as exc:  # ponytail: a writer never fails the turn; the next route answers once
            ok, answer, receipt = False, '', {'failure_class': type(exc).__name__}
        attempts.append({'route': name, 'ok': ok, 'ms': round((time.monotonic() - started) * 1000),
                         **({} if ok else {'failure': str(receipt.get('failure_class'))[:80]})})
        if ok:
            return parse(answer or '', seen), attempts
        if len(attempts) == 2:
            break
    return [], attempts


def counts(turn, found):
    """What code counts in a turn, the filter's input (TypeSafe: never ask the model to count)."""
    steps = turn['steps']
    return {'facts': len(found), 'edits': sum(s['tool'] == 'edit' for s in steps), 'steps': len(steps),
            'tests': sum(bool(_check(s)[2]) for s in steps),
            'failures': sum(s['status'] in ('failed', 'refused') for s in steps),
            'delegated': sum(s['tool'] in DELEGATED + ('Agent', 'Task') for s in steps),
            'request_kb': len(turn['request'].encode('utf-8')) / 1024,
            'reply_kb': len(turn['reply'].encode('utf-8')) / 1024}


def score(turn, found):
    """The filter's probability that a turn deserves a note (FILTER)."""
    values = counts(turn, found)
    z = FILTER_BIAS + sum(weight * (values[name] - mean) / spread for name, (mean, spread, weight) in FILTER.items())
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def said_already(turn, note):
    """Hosted Jev's probability that the reply already says what the note says, or None without an answer."""
    from .turn_engine import judge_many
    state = json.dumps({'assistant_final_reply': _clip(turn['reply'], 6000), 'note': note}, ensure_ascii=False)
    return (judge_many(state, {'already_said': ALREADY_SAID}, timeout=10).get('yes') or {}).get('already_said')


def _digest(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def notes(turn, *, seen=(), observed=None, sample=SAMPLE):
    """At most MAX_NOTES new notes for a completed `turn` (from_journal or from_messages), never one in `seen`."""
    if not enabled():
        return []
    from .turn_engine import deciding, judge_log, judges
    started, row = time.monotonic(), {}
    try:
        found = facts(turn)
        p = score(turn, found)
        why = 'score' if p >= FORWARD_AT else 'sample' if random.random() < sample else None
        verdicts = judges(assessment(turn, found))  # calibration only: the filter decides
        answer = deciding(verdicts)
        new, attempts = write(writer_record(turn, found), list(seen)) if why else ([], [])
        redundant = said_already(turn, new[0]) if new else None
        if redundant is not None and redundant >= REDUNDANT_AT:
            new = []
        row = {'facts': len(found), 'score': round(p, 4), 'counts': counts(turn, found),
               'p_note': 1 - answer['probabilities'].get(QUIET, 0) if answer else None, **verdicts,
               'forwarded': why, 'writers': attempts, 'redundant': redundant, 'notes': len(new)}
    except Exception as exc:  # ponytail: a heads-up never fails the turn it reports on; the reason is logged
        new, row = [], {'failure': f'{type(exc).__name__}: {str(exc)[:120]}'}
    judge_log('heads-up', {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'funnel': funnel(),
                           **(observed or {}),
                           'request_sha256': _digest(turn.get('request')), 'reply_sha256': _digest(turn.get('reply')),
                           'steps': len(turn.get('steps') or []), 'reply_chars': len(turn.get('reply') or ''),
                           **row, 'ms': round((time.monotonic() - started) * 1000)})
    return new


def shown(session):
    """The notes a session's user saw: {'shown': all, 'last': the latest turn's, 'turn': that turn's reply digest}."""
    try:
        value = json.loads((Path(session)/'heads-up.json').read_text(encoding='utf-8'))
        return {'shown': list(value['shown']), 'last': list(value['last']), 'turn': value.get('turn')}
    except (OSError, ValueError, KeyError, TypeError):
        return {'shown': [], 'last': [], 'turn': None}


def keep(session, new, reply=''):
    value = shown(session)
    path = Path(session)/'heads-up.json'
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps({'shown': value['shown'] + new, 'last': new, 'turn': _digest(reply)},
                                    ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def asked(session, source='tui'):
    """The user sent the last notes to the agent: a label from use, joined to its turn's row by the reply digest."""
    from .turn_engine import judge_log
    judge_log('heads-up', {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'funnel': funnel(),
                           'event': 'asked', 'source': source, 'reply_sha256': shown(session)['turn']})


def shown_text(new):
    return ''.join(f'\n_tare · heads-up: {note}_\n' for note in new)


def ask(new):
    """The composer text that asks the agent about the notes (the TUI's /heads-up)."""
    return ASK.format(notes='\n'.join('- ' + n for n in new))
