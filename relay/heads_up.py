"""Heads-up notes: after a completed turn, the one thing the user should know that the final reply left unclear.

A funnel, cheapest stage first:
1. Collect, no model: the turn's recorded steps, what code counts in them, and the facts tare checks itself (a test
   whose last run failed, files changed after the last test ran, an edit refused and never redone).
2. Pace, no model: a logistic score over those counts (FILTER) sends a turn to the writer at FORWARD_AT or more, and
   otherwise every EVERY-th turn since the writer last ran. A note is about the conversation more often than about
   the turn it follows, and the writer reads the whole window since it last ran (the skipped turns' steps with the
   earlier exchanges), so a pause loses little.
3. Write: the first available route of the routing policy's `heads_up` role (/routes changes it) writes at most
   MAX_NOTES note, or none, about consequences: a measurement that may not hold, a cost to come, a changed default,
   a rule that can catch too much, a silent failure. It reads what the work did, from what tare already recorded
   (each edit's text, a demand's objective and criteria, what explorers were asked and found, delegated reports,
   commands' last output), so it needs no tools and no calls of its own.
4. Check: hosted Jev drops a note the reply already says (a Noul, REDUNDANT_AT). Without Jev the note stands.

Each decision is logged for calibration (<storage>/serve/heads-up.jsonl: scores, probabilities, routes, times and
sizes, never the texts); the turns the cadence sends below the score measure what the score misses, and the TUI's
/heads-up logs that the user sent a note to the agent (asked()), a label from use. The TUI also keeps each note it
showed with its turn (the request's end, the reply's start) in the conversation's heads-up.json, for dogfood review.
scripts/heads_up_calibration.py replays history through the funnel, reports the trade and lists the TUI's notes. Every frontend calls
notes(); a failure here never fails the turn it reports on. TARE_HEADS_UP=off disables it.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

# The filter: a logistic score over what code counts in a turn (counts()), {count: (mean, spread, weight)}. Fitted
# 2026-11-05 on 249 Claude Code turns, labeled by whether the user accepted a side-agent note after them. On all
# 1,408 turns of those sessions its AUC is 0.70, 0.61 for a refit scored on sessions it never saw: no count, and no
# question hosted Jev answered about a turn, tells much about whether a note will follow (scripts/
# heads_up_calibration.py claude). So the score only brings a busy turn forward; the cadence covers the rest.
# On those turns: the writer runs on 32% of them, and 98% of the noted ones fall in a run's window of turns. Few
# notes are lost by it: on 49 noted turns, Sonnet raised the side agent's concern on 2 reading the turn alone and on
# at most 2 reading the 4-turn window (hosted Jev's match, one of them doubtful), so the writer, not the pace, is
# what limits how many such notes are found.
# ponytail: fitted on another agent's transcripts; refit on tare's own log (cadence turns, /heads-up) as it grows
FILTER = {'facts': (.157, .405, .205), 'edits': (2.566, 5.373, .235), 'steps': (24.61, 42.822, .043),
          'tests': (3.426, 7.791, .181), 'failures': (.486, 1.155, .164), 'delegated': (.141, .411, .061),
          'request_kb': (.782, 3.329, -.129), 'reply_kb': (1.461, .999, -.392)}
FILTER_BIAS = -1.517
FORWARD_AT = .25
EVERY = 4  # below the score, the writer runs on every EVERY-th turn since it last ran
HELD_STEPS = 100  # steps of the turns since then that the conversation keeps for it (pace['held'])
ALREADY_SAID = 'Does `assistant_final_reply` already tell the user what `note` says?'
REDUNDANT_AT = .7  # TypeSafe's starting threshold for a Noul's yes
# Jev also judged the written note alone (consequence, process status, a gap in the record) and a regex caught notes
# arguing from what the record does not show. On the user's labels of 18 notes by the consequence writer, that gate
# ranked them worse than chance (AUC 0.41) and would have dropped 6 of the 14 they wanted, so it is gone.
MAX_NOTES = 1  # one note, like the side agents users accepted notes from
NOTE_CHARS = 500
WRITER_BYTES = 48000  # the writer reads the conversation: a consequence often rests on what came before
WRITER_EARLIER = .45  # of that, the share for earlier exchanges
WRITER_SECONDS = 180
STEP_CHARS = 300
DELEGATED = ('explore', 'implement', 'delegate_plan', 'demand', 'research')  # tools whose work runs elsewhere
RECORD = 8  # what record() and facts() show; bump it when that changes, so calibration starts anew
EARLIER_TURNS = 8  # earlier exchanges a turn carries as context, newest kept first when the record must shrink
DETAIL_CHARS = 600  # what a step shows of what it did (_detail)

# What the side agent looks for. Taken from the notes users accepted from Claude Code's side agent (scripts/
# heads_up_calibration.py claude): what follows from what the work changed or the conversation decided, often from
# knowledge beyond the conversation (a license, a platform rule, git history, a quota), never process hygiene.
SYSTEM = ("You watch a coding agent's conversation from the side, for its user, who sees only the agent's final "
          "reply and the names of its steps: not logs, command output, diffs, delegated reports or what audits "
          "found. You get the conversation before this turn (earlier_in_conversation), the user's request, the steps "
          "the host recorded (what each edit changed, what commands printed, what explorers were asked and found, "
          "what delegated work was asked, reported and what its audit found) and the agent's final reply. Decide "
          "whether there is one thing the user should know before relying on what was just done or said, which the "
          "reply does not make clear. Read what the work actually changes and what the conversation decided, then "
          "ask what follows from it, using what you know about the tools, platforms, licenses and costs involved. "
          "Look for: a claim in the reply that the record contradicts (an audit that refused, a test that failed, "
          "numbers that do not add up); a choice the reply offers whose consequence it hides; a measurement, "
          "comparison or test whose setup differs from what will really run (another model or configuration, a "
          "stand-in, a single run, numbers from different rounds, data the agent made itself), so its conclusion "
          "may not hold; a cost the user will pay later (quota, tokens, money, disk, time, history that cannot be "
          "removed, files that will be deleted); behavior that changes for the user or others (a new default, a "
          "removed check, a rule that now applies more widely, a promise in docs or UI that the code does not "
          "keep); a rule, pattern, filter or cleanup that can catch or delete more than intended; a failure that "
          "would stay silent, with nothing to alert anyone; a license, platform or version rule the work runs "
          "into. Never write about the process alone: that something was not run, not tested, not committed, is "
          "still running, or does not appear in the recorded steps, since the agent may have seen more than the "
          "steps show. The one exception is a check the reply claims: when it says something was tested, verified "
          "or passes and the steps show that check failed, never ran, or ran before later edits to what it checks, "
          "say so, unless earlier steps were left out of the record. Never give advice, suggestions or style "
          "opinions, and never repeat a note already shown. Most turns need nothing: then answer exactly NONE. "
          "Otherwise answer one line starting with '- ': one or two plain sentences in the language of the user's "
          "messages, saying what holds and why it matters, without the transcript's jargon.")


def funnel():
    """The funnel's version in each log row: calibration compares only rows that ran the same texts and pace."""
    value = [SYSTEM, FILTER, FILTER_BIAS, FORWARD_AT, EVERY, ALREADY_SAID, REDUNDANT_AT, RECORD]
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


def _detail(tool, args, output):
    """What a step did, beyond its status: an edit's old and new text, what an explorer was asked and found, a
    demand's objective and criteria, a delegated report, a command's last output."""
    changes = [c for c in args.get('changes') or [] if isinstance(c, dict)]
    new = next((args[k] for k in ('newString', 'new_string', 'content', 'code', 'input') if isinstance(args.get(k), str)), None)
    if changes or new is not None:
        changes = changes or [{'search': args.get('oldString') or args.get('old_string') or '', 'replace': new}]
        text = '\n'.join((f"- {_clip(c.get('search'), 200)}\n" if c.get('search') else '')
                         + f"+ {_clip(c.get('replace'), 400)}" for c in changes)
    elif tool == 'explore':
        asked = [i.get('question') for i in args.get('investigations') or [] if isinstance(i, dict)]
        found = [(i.get('report') or {}).get('summary') for i in output.get('investigations') or [] if isinstance(i, dict)]
        text = '; '.join(['asked: ' + _clip(q, 200) for q in asked if q] + ['found: ' + str(f) for f in found if f])
    elif tool == 'demand':
        text = '; '.join([f"objective: {args.get('objective') or ''}"] + [f'criterion: {c}' for c in args.get('criteria') or []])
    elif tool in DELEGATED + ('Agent', 'Task'):
        # implement's request is its note and its report the implementer's; what its audit found, the user never sees
        asked = str(args.get('description') or args.get('prompt') or args.get('task') or args.get('note') or '')
        report = output.get('output') or output.get('report') or output.get('implementer_report') or ''
        said = _text(report.get('text') if isinstance(report, dict) and 'text' in report else report)
        audit = output.get('audit') if isinstance(output.get('audit'), dict) else {}
        found = ' | '.join(_clip(f.get('claim'), 260) for f in audit.get('findings') or []
                           if isinstance(f, dict) and f.get('claim'))
        text = '; '.join(x for x in (audit and f"audit {audit.get('verdict')}" + (': ' + found if found else ''),
                                     asked and 'asked: ' + _clip(asked, 160), said and 'reported: ' + said) if x)
    elif tool in ('terminal_read', 'verify'):
        text = _tail(_text(output.get('output')).strip(), 400)
    else:
        text = ''
    return _clip(text, DETAIL_CHARS)


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
    detail = _detail(tool, args, output)
    if tool == 'edit':
        from .native_activity import deletion_requested, deletion_detail
        if deletion_requested(args):
            detail = deletion_detail(status, output)
    if detail:
        value['detail'] = detail
    return value


def from_journal(runs, request=None):
    """A TUI or tare serve turn from its sessions' journals: runs are (session folder, invocation id), oldest first.
    request, the user's own words for the turn, replaces only its opening message: messages the user queued while
    the turn ran stay (a side agent without them called a requirement the user had asked for invented)."""
    from .native_agent import read_json
    from .native_messages import operator_ids
    requests, steps, reply, earlier = [], [], '', None
    for session, invocation_id in runs:
        session = Path(session)
        row = next(r for r in read_json(session/'native-agent.json', 1024*1024)['invocations'] if r['id'] == invocation_id)
        lines = (session/'journal.jsonl').read_text(encoding='utf-8').splitlines()
        if earlier is None:  # the conversation before the turn: its first session's earlier exchanges
            earlier = _exchanges(json.loads(line)['event'] for line in lines[:row.get('journal_start') or 0])
        asked, queued = {}, operator_ids(session)
        for line in lines[row.get('journal_start') or 0:row.get('journal_end')]:
            event = json.loads(line)['event']
            kind = event.get('kind')
            if kind in ('user_message', 'session_started') and (event.get('text') or event.get('instruction')):
                if requests and event.get('message_id') not in queued:
                    continue  # not from the operator's inbox: the host's own (consent choices, recoveries)
                requests.append(event.get('text') or event.get('instruction'))
            elif kind == 'effect_requested':
                asked[event['effect_id']] = event.get('tool_call') or {}
            elif kind == 'effect_returned':
                call, result = asked.get(event['result']['effect_id'], {}), event['result']
                steps.append(step(call.get('name', '?'), _loads(call.get('arguments_json')), result.get('status'),
                                  _loads(result.get('output_json'))))
            elif kind == 'terminal' and event.get('output'):
                reply = event['output']
    if request:
        requests[:1] = [request]
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
    line = _clip(' '.join(parts + [s.get('reason') or s.get('summary') or '']).strip(), STEP_CHARS)
    return line + (' :: ' + s['detail'] if s.get('detail') else '')


def _tail(text, limit):
    raw = (text or '').encode('utf-8')
    return raw[len(raw) - int(limit):].decode('utf-8', 'ignore') if len(raw) > limit else text or ''


def writer_record(turn):
    return record(turn, WRITER_BYTES, WRITER_EARLIER)


def record(turn, budget, earlier_share=.25):
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
                            'recorded_steps': (['... earlier steps left out'] if len(shown) < len(lines) else []) + shown,
                            'assistant_final_reply': _clip(turn['reply'], budget * .35 * share)}, ensure_ascii=False)
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) <= budget:  # as the judge request embeds it
            break
    return value


def _sentences(note):
    """A note longer than NOTE_CHARS, cut after its last sentence that fits, so it never stops mid-word."""
    whole = len(note) > NOTE_CHARS and re.match(r'(?s).*[.!?](?=\s)', note[:NOTE_CHARS + 1])
    return whole.group() if whole else note[:NOTE_CHARS]


def parse(answer, seen):
    """The writer's notes: none for NONE; at most MAX_NOTES '- ' lines, never one already shown."""
    if answer.strip().upper().startswith('NONE'):
        return []
    known = {s.casefold() for s in seen}
    notes = [_sentences(line.strip()[2:].strip()) for line in answer.splitlines() if line.strip()[:2] in ('- ', '* ')]
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


def _mark(label, turn):
    return {'tool': label, 'subject': _clip(turn['request'], 120), 'status': 'started'}


def hold(held, turn):
    """The steps kept for the writer after a turn it skipped: the turn's, marked with its request, newest kept."""
    return (held + [_mark('earlier turn', turn)] + turn['steps'])[-HELD_STEPS:]


def window(turn, held):
    """The turn as the writer reads it after skipping some: the skipped turns' steps first, so their tests, edits
    and failures reach it along with the earlier exchanges."""
    return {**turn, 'steps': held + [_mark('this turn', turn)] + turn['steps']} if held else turn


def _digest(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def notes(turn, *, seen=(), observed=None, pace=None):
    """At most MAX_NOTES new notes for a completed `turn` (from_journal or from_messages), never one in `seen`.
    `pace` is the conversation's dict that counts the turns since the writer last ran ('since') and keeps their
    steps ('held')."""
    if not enabled():
        return []
    from .turn_engine import judge_log
    pace = {} if pace is None else pace
    started, row = time.monotonic(), {}
    try:
        found = facts(turn)
        p = score(turn, found)
        since, held = pace.get('since', 0) + 1, list(pace.get('held') or [])
        why = 'score' if p >= FORWARD_AT else 'cadence' if since >= EVERY else None
        pace.update(since=0 if why else since, held=[] if why else hold(held, turn))
        new, attempts = write(writer_record(window(turn, held)), list(seen)) if why else ([], [])
        redundant = said_already(turn, new[0]) if new else None
        if redundant is not None and redundant >= REDUNDANT_AT:
            new = []
        row = {'facts': len(found), 'score': round(p, 4), 'counts': counts(turn, found), 'since': since,
               'held': len(held), 'forwarded': why, 'writers': attempts, 'redundant': redundant, 'notes': len(new)}
    except Exception as exc:  # ponytail: a heads-up never fails the turn it reports on; the reason is logged
        new, row = [], {'failure': f'{type(exc).__name__}: {str(exc)[:120]}'}
    judge_log('heads-up', {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'funnel': funnel(),
                           **(observed or {}),
                           'request_sha256': _digest(turn.get('request')), 'reply_sha256': _digest(turn.get('reply')),
                           'steps': len(turn.get('steps') or []), 'reply_chars': len(turn.get('reply') or ''),
                           **row, 'ms': round((time.monotonic() - started) * 1000)})
    return new


def shown(session):
    """A session's heads-up state: {'shown': every note its user saw, 'last': the latest turn's notes, 'turn': that
    turn's reply digest, 'noted': each note with its time and turn, and the pace notes() keeps: 'since' and 'held'}."""
    try:
        value = json.loads((Path(session)/'heads-up.json').read_text(encoding='utf-8'))
        return {'shown': list(value['shown']), 'last': list(value['last']), 'turn': value.get('turn'),
                'noted': list(value.get('noted') or []), 'since': int(value.get('since', 0)),
                'held': list(value.get('held') or [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {'shown': [], 'last': [], 'turn': None, 'noted': [], 'since': 0, 'held': []}


def keep(session, new, turn=None, pace=None):
    """After every turn: its new notes, if any, become the last ones, each also kept with the end of the turn's
    request and the start of its reply for dogfood review; the pace is kept either way."""
    value, turn = shown(session), turn or {}
    if new:
        at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        value.update(shown=value['shown'] + new, last=new, turn=_digest(turn.get('reply')), noted=value['noted'] + [
            {'at': at, 'note': note, 'request': _tail(turn.get('request'), 900).strip(),
             'reply': _clip(turn.get('reply'), 1600).strip()} for note in new])
    value.update(since=(pace or {}).get('since', 0), held=(pace or {}).get('held', []))
    path = Path(session)/'heads-up.json'
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
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
