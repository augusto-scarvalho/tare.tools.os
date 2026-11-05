"""Calibrate the heads-up filter (relay/heads_up.py) on turns that already happened.

`replay` sends saved TUI and tare serve turns through the funnel with each one forwarded to the writer (sample 1),
so the log (<storage>/serve/heads-up.jsonl) holds the judge's probability beside the writer's verdict for every
turn; a turn already replayed is skipped. It spends judge and writer calls: --limit bounds them.
`report` reads the log: for each threshold, how many writer calls the filter saves and how many turns that got
notes it would have dropped (rows whose writer answered count; tare's facts always forward, so they never drop).
`claude` calibrates the judge against labels: Claude Code's side agent ("You should know") left the notes the user
accepted in the transcripts, so each such turn is one the filter should forward, and the other turns of those
sessions are ones it may drop. Each judged turn's clipped record goes to the judge (hosted Jev when it answers).
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def log_rows():
    """The log's rows that ran the current funnel (its texts and threshold); earlier versions measure another one."""
    from relay.heads_up import funnel
    from relay.native_installation import storage_root
    path = storage_root()/'serve'/'heads-up.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    return [r for r in rows if r.get('funnel') == funnel()]


def replay(root, limit, writer=None):
    """`writer` pins one route as the writer for this run, to compare writers on the same turns."""
    from relay import heads_up, turn_engine
    if not heads_up.enabled():
        print('Heads-up notes are off on this machine (the TUI option); turn them on to replay.')
        return 0
    if writer:
        policy = turn_engine.policy
        turn_engine.policy = lambda: {**policy(), 'heads_up': [writer]}
    done = {(r.get('session'), r.get('invocation')) for r in log_rows() if r.get('source') == 'replay'
            and r.get('pinned') == writer}
    count = 0
    for descriptor in sorted(root.glob('**/native-agent.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            saved = json.loads(descriptor.read_text(encoding='utf-8'))
            invocations = saved['invocations']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # Live notes follow conversations only: auditors, planners, explorers and automated dogfood runs are not one.
        if (saved.get('config') or {}).get('role') != 'implementer' or 'dogfood' in descriptor.parts:
            continue
        seen = []
        for row in invocations:
            key = (descriptor.parent.name, row.get('id'))
            if row.get('status') != 'PROPOSED' or key in done:
                continue
            try:
                turn = heads_up.from_journal([(descriptor.parent, row['id'])])
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                continue
            if not turn['reply']:
                continue
            new = heads_up.notes(turn, seen=seen, sample=1, observed={
                'source': 'replay', 'session': key[0], 'invocation': key[1], **({'pinned': writer} if writer else {})})
            seen += new
            count += 1
            print(f'{key[0][:8]} {key[1][:8]} steps={len(turn["steps"])} notes={len(new)}', *new, sep='\n  ')
            if count >= limit:
                return count
    return count


MARK = 'Here is a note offered by a side agent'  # how Claude Code sends an accepted side-agent note to the agent


def _claude_text(content):
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text')


def _claude_step(call, result):
    """A Claude Code tool call as a tare step: edits and shell commands keep what tare's facts read."""
    from relay.heads_up import step
    args, name = call.get('input') or {}, call.get('name')
    content = result.get('content')
    text = content if isinstance(content, str) else '\n'.join(
        b.get('text', '') for b in content or [] if isinstance(b, dict))
    status = 'failed' if result.get('is_error') else 'succeeded'
    if name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
        return step('edit', {'path': args.get('file_path') or args.get('notebook_path')}, status, {'error': text[:200]})
    if name in ('Bash', 'PowerShell'):
        return step('terminal_read', {}, status, {'exit_code': int(status == 'failed'),
                                                  'command': args.get('command', ''), 'output': text})
    return step(name, args, 'returned' if name in ('Agent', 'Task') and status == 'succeeded' else status,
                {'output': text})


def claude_turns(path):
    """[(turn, accepted note or None)] for the finished turns of one Claude Code transcript (.jsonl): a turn ends at
    its turn_duration row, and the side-agent note the user accepted right after it is its label."""
    with open(path, encoding='utf-8', errors='replace') as lines:  # line by line: a long session is gigabytes
        return turns_of(_row(line) for line in lines)


def _row(line):
    try:
        return json.loads(line)
    except ValueError:
        return {}


def turns_of(rows):
    from relay.heads_up import EARLIER_TURNS
    turns, current, calls, history = [], None, {}, []
    for row in rows:
        if row.get('isSidechain'):
            continue
        kind, content = row.get('type'), (row.get('message') or {}).get('content')
        if kind == 'user':
            text = _claude_text(content).strip()
            if text.startswith(MARK):
                if turns and turns[-1][1] is None:
                    turns[-1] = (turns[-1][0], text)
                current = None  # the agent's answer to the note is no case
                continue
            if text and not row.get('isMeta') and not text.startswith('<'):  # not reminders or command output
                if current is None:  # earlier exchanges clipped here: a long session has thousands of turns
                    current = {'request': text, 'steps': [], 'reply': '', 'earlier': [
                        {'user': e['user'][-2000:], 'assistant': e['assistant'][:3000]} for e in history[-EARLIER_TURNS:]]}
                else:
                    current['request'] += '\n\nLater message from the user:\n' + text
            for block in content if isinstance(content, list) and current is not None else []:
                if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') in calls:
                    current['steps'].append(_claude_step(calls.pop(block['tool_use_id']), block))
        elif kind == 'assistant' and current is not None and isinstance(content, list):
            for block in content:
                if block.get('type') == 'tool_use':
                    calls[block.get('id')] = block
                elif block.get('type') == 'text' and block.get('text', '').strip():
                    current['reply'] = block['text']
        elif kind == 'system' and row.get('subtype') == 'turn_duration' and current is not None:
            turns.append((current, None))
            history.append({'user': current['request'], 'assistant': current['reply']})
            current = None
    return turns


# Judge questions tried against the side agent's labels: (question, options, the quiet option). None is the
# module's own (relay/heads_up.py); the one that separates best becomes it.
VARIANTS = {
    'current': None,
    'colleague': (
        'Would a careful colleague who watched this turn stop the user to warn them about something the final reply '
        'does not make clear?',
        {'warn': 'Yes: the user is about to rely on something false, untested, narrower than it sounds, or costly, '
                 'and the reply does not say so.',
         'quiet': 'No: nothing worth interrupting for. Most turns are like this.'}, 'quiet'),
    'belief': (
        'After reading the final reply, will the user believe something about the work that the recorded steps or '
        'the earlier conversation show is false, unverified or only partly true?',
        {'misled': 'Yes: the reply leaves a wrong or overstated impression (done, tested, measured, general) that '
                   'the record does not support.',
         'accurate': 'No: what the user will believe matches the record.'}, 'accurate'),
    'kinds': (
        'What, if anything, should the user be warned about after this turn?',
        {'overstated': 'A claim broader than its evidence: done, tested or measured only on a narrower case, a '
                       'stand-in, another setup, or not at all.',
         'side_effect': 'An effect the user may not expect: behavior changed for others, a lasting cost, a risk, or '
                        'something else broken.',
         'contradiction': 'The reply contradicts the recorded steps or something established earlier in the '
                          'conversation.',
         'nothing': 'Nothing: the reply matches the record and has no surprising effect. Most turns are like this.'},
        'nothing'),
}


# What each judge brief adds to heads_up.brief(): earlier exchanges (user end, assistant start, in UTF-8 bytes),
# earlier user messages alone, and a longer final reply. The one that separates best becomes heads_up.brief().
BRIEFS = {
    'lean': {},
    'last_exchange': {'exchanges': 1, 'user': 600, 'assistant': 1200},
    'two_exchanges': {'exchanges': 2, 'user': 400, 'assistant': 800},
    'user_messages': {'users': 4},
    'last_exchange_long_reply': {'exchanges': 1, 'user': 600, 'assistant': 1200, 'reply': 1.6},
}


def briefed(name):
    """heads_up.brief with BRIEFS[name] added, earlier context first."""
    from relay import heads_up
    lean, extra = heads_up.brief, BRIEFS[name]

    def brief(turn, found, reply=2500):
        earlier, value = turn.get('earlier') or [], {}
        if extra.get('users'):
            value['earlier_user_messages'] = [heads_up._tail(e['user'], 300) for e in earlier[-extra['users']:]]
        if extra.get('exchanges'):
            value['earlier_exchanges'] = [{'user': heads_up._tail(e['user'], extra['user']),
                                           'assistant': heads_up._clip(e['assistant'], extra['assistant'])}
                                          for e in earlier[-extra['exchanges']:]]
        return json.dumps({**value, **json.loads(lean(turn, found, int(reply * extra.get('reply', 1))))},
                          ensure_ascii=False)
    return brief


def claude_cases(root, negatives, seed=0):
    """Every turn whose side-agent note the user accepted, and a sample of the other finished turns of the same
    sessions (the side agent was on and said nothing): [(session, turn index, turn, note or None)]."""
    import random
    cases = []
    for path in sorted(root.glob('*/*.jsonl')):
        turns = claude_turns(path)
        if any(note for _, note in turns):
            cases += [(path.stem, index, turn, note) for index, (turn, note) in enumerate(turns) if turn['reply']]
    others = [c for c in cases if not c[3]]
    return [c for c in cases if c[3]] + random.Random(seed).sample(others, min(negatives, len(others)))


def claude_history(cases, variant='current', brief='lean'):
    """The judge under one VARIANTS question and one BRIEFS brief on the cases; rows go to heads-up-claude.jsonl
    without texts."""
    from relay import heads_up
    from relay.native_installation import storage_root
    from relay.turn_engine import deciding, judges
    saved = heads_up.QUESTION, heads_up.OPTIONS, heads_up.QUIET, heads_up.brief
    if VARIANTS[variant]:
        heads_up.QUESTION, heads_up.OPTIONS, heads_up.QUIET = VARIANTS[variant]
    heads_up.brief = briefed(brief)
    try:
        rows = []
        for session, index, turn, note in cases:
            found = heads_up.facts(turn)
            verdicts = judges(heads_up.assessment(turn, found))
            answer = deciding(verdicts)
            rows.append({'funnel': heads_up.funnel(), 'variant': variant, 'brief': brief, 'session': session, 'turn': index,
                         'noted': bool(note), 'facts': len(found),
                         'p_note': 1 - answer['probabilities'].get(heads_up.QUIET, 0) if answer else None,
                         'failure': next((v['failure'] for v in verdicts.values() if 'failure' in v), None)})
    finally:
        heads_up.QUESTION, heads_up.OPTIONS, heads_up.QUIET, heads_up.brief = saved
    path = storage_root()/'serve'/'heads-up-claude.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as log:
        log.writelines(json.dumps(r) + '\n' for r in rows)
    return rows


# Narrow yes/no questions the judge answers about a turn's brief, in one request (turn_engine.judge_many): what the
# reply claims and what the work touches, which code cannot read; code then weighs them with tare's facts.
CLAIMS = {
    'claims_tested': 'Does the final reply say that tests or checks passed, or that the work was verified?',
    'claims_measured': 'Does the final reply report a measurement, benchmark, cost, count or other number as a result?',
    'claims_done': 'Does the final reply say that something was changed, fixed or delivered?',
    'narrow_evidence': 'Was the evidence for the reply gathered on a narrower case than what it claims: a sample, a '
                       'stand-in, data the assistant made itself, or another model or setup?',
    'affects_others': 'Does the work change behavior for other users, other projects or later runs, or leave a '
                      'lasting cost such as history, storage or quota?',
    'sets_default': 'Does the reply turn something on by default, or recommend a default or a decision, based on '
                    'what was measured or tried?',
    'contradicts': 'Does the final reply contradict the tests, failures or delegated verdicts listed in the record?',
    'buried_caveat': 'Does the reply present the result as complete while a limitation is mentioned only in passing '
                     'or not at all?',
}


def noul(instructions, true, false):
    """A Noul with criteria (TypeSafe's advice: one literal condition, boundary cases in the criteria)."""
    return {'type': 'noul', 'instructions': instructions, 'criteria': {'true': true, 'false': false}}


def ask_nouls(state, questions):
    """Hosted Jev's Noul answers over a structured (JSON object) state: ({name: probability of yes}, None) or
    ({}, failure). The kernel's assess_many sends Nouls without criteria, so this calls the endpoint itself."""
    from compute_plane.jev_choice_worker import BASE_URL, post
    from relay.turn_engine import HOSTED_JEV_MODEL, jev_key
    try:
        response = post({'model': HOSTED_JEV_MODEL, 'state': state, 'questions': questions}, key=jev_key(),
                        base_url=BASE_URL, timeout=25)
        return {k: float(response['answers'][k]['noul']) for k in questions}, None
    except (ValueError, KeyError, TypeError) as exc:
        return {}, str(exc)[:120]


# CLAIMS rewritten after TypeSafe's guide: one literal condition each, the field it judges named in backticks, the
# boundary cases in the criteria, counting left to code. Asked over the brief plus the last earlier exchange.
CLAIMS_V2 = {
    'claims_verified': noul('`assistant_final_reply` says that tests, checks or a verification passed.',
                            'It says tests pass, a check is green, or something was verified, confirmed or works.',
                            'It only describes changes, plans or questions, or it reports failures.'),
    'reports_number': noul('`assistant_final_reply` reports a measured number as a result.',
                           'A count, time, cost, size, score, rate or percentage presented as a measured result.',
                           'No measured number; only file names, line numbers, versions or steps.'),
    'generalizes': noul('`assistant_final_reply` presents a result as general, beyond the cases actually tried.',
                        'It says something works, is fixed or holds in general, or for everyone.',
                        'It limits the result to the cases it tried, or makes no such claim.'),
    'states_limit': noul('`assistant_final_reply` names a limitation, a caveat or something left undone.',
                         'It says what was not checked, not done, only partly true, or still open.',
                         'It names no limitation.'),
    'recommends': noul('`assistant_final_reply` recommends a decision or a default to the user.',
                       'It recommends an option, a default, turning something on, or a next decision.',
                       'It only reports what happened or asks a question.'),
    'lasting_effect': noul('The work in `files_changed` or `assistant_final_reply` affects others or lasts.',
                           'It changes behavior for other users or projects, published history, defaults, storage '
                           'or later runs.',
                           'It stays local to this task, such as a scratch file or a draft.'),
    'stand_in': noul('`assistant_final_reply` rests on a stand-in for the real case.',
                     'A sample, a mock, data the assistant made itself, a fixture, or another model or setup than the '
                     'one in use.',
                     'It rests on the real case, or reports no result.'),
    'contradicts_record': noul('`assistant_final_reply` calls something successful that `tests_run`, `failures` or '
                               '`delegated` show as failed, refused or inconclusive.',
                               'The reply reports success where the record shows a failure or no verdict.',
                               'The reply matches the record, or the record shows nothing to compare.'),
    'asks_measurement': noul('`user_request` asks to measure, compare, benchmark or estimate something.',
                             'It asks for a measurement, comparison, benchmark, cost or estimate.',
                             'It asks for a change, an explanation or something else.'),
    'asks_decision': noul('`user_request` asks the assistant for a recommendation or a decision.',
                          'It asks what to choose, whether to do something, or for an opinion.',
                          'It asks for work to be done or a fact.'),
    'proposes_plan': noul('`assistant_final_reply` mainly proposes a plan or design for the user to approve.',
                          'Most of it is a proposal, options or a plan, not work already done.',
                          'Most of it reports work done or answers a question.'),
    'builds_on_earlier': noul('`assistant_final_reply` relies on a result or measurement from `earlier_exchanges`.',
                              'It reuses a number, a conclusion or a setup established in an earlier exchange.',
                              'It does not rely on earlier results, or there are no earlier exchanges.'),
}


def claude_claims(cases, claims=None):
    """The claim questions on each case's brief (CLAIMS_V2 also see the last earlier exchange); rows (probabilities
    and code features, no texts) go to heads-up-claude-claims.jsonl."""
    from relay import heads_up
    from relay.native_installation import storage_root
    from relay.turn_engine import judge_many
    claims = claims or CLAIMS
    rows = []
    for session, index, turn, note in cases:
        found = heads_up.facts(turn)
        if claims is CLAIMS_V2:  # a JSON object state, as TypeSafe advises, with real Nouls
            yes, failure = ask_nouls(json.loads(briefed('last_exchange')(turn, found)), claims)
        else:
            value = judge_many(heads_up.brief(turn, found), claims, timeout=25)
            yes, failure = value.get('yes') or {}, value.get('failure')
        rows.append({'record': heads_up.RECORD, 'claims': 'v2' if claims is CLAIMS_V2 else 'v1', 'session': session,
                     'turn': index, 'noted': bool(note), 'facts': len(found), 'code': heads_up.counts(turn, found),
                     'yes': yes or None, 'failure': failure})
    path = storage_root()/'serve'/'heads-up-claude-claims.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as log:
        log.writelines(json.dumps(r) + '\n' for r in rows)
    return rows


def claims_report(rows):
    ok = [r for r in rows if r.get('yes')]
    noted, quiet = [r for r in ok if r['noted']], [r for r in ok if not r['noted']]
    print(f'{len(rows)} turns, {len(ok)} answered: {len(noted)} noted, {len(quiet)} quiet. AUC per feature:')
    names = list(ok[0]['yes']) if ok else []
    scores = {name: (lambda r, name=name: r['yes'][name]) for name in names}
    scores.update({name: (lambda r, name=name: (r.get('code') or {}).get(name, 0)) for name in
                   (ok[0].get('code') or {} if ok else {})})
    for name, score in scores.items():
        print(f'  {name:18} {auc([score(r) for r in noted], [score(r) for r in quiet]):.2f}')
    if ok:
        combined = cross_validated([[score(r) for score in scores.values()] for r in ok], [r['noted'] for r in ok])
        print(f'  logistic, all features, 5-fold cross-validated: AUC {combined:.2f}')


def logistic(xs, ys, steps=3000, rate=.1, l2=.05):
    """Weights (bias last) of an L2 logistic regression by gradient descent on standardized features: the composite
    score TypeSafe suggests, learned from labels. Small data, so plain Python is enough."""
    import math
    n, k = len(xs), len(xs[0])
    w = [0.0] * (k + 1)
    for _ in range(steps):
        grad = [0.0] * (k + 1)
        for x, y in zip(xs, ys):
            z = sum(wi * xi for wi, xi in zip(w, x)) + w[-1]
            error = 1 / (1 + math.exp(-max(min(z, 30), -30))) - y
            for i, xi in enumerate(x):
                grad[i] += error * xi
            grad[-1] += error
        w = [wi - rate * (g / n + (l2 * wi if i < k else 0)) for i, (wi, g) in enumerate(zip(w, grad))]
    return w


def standardize(xs, reference=None):
    import statistics
    reference = reference or xs
    columns = list(zip(*reference))
    means = [statistics.fmean(c) for c in columns]
    spreads = [statistics.pstdev(c) or 1 for c in columns]
    return [[(v - m) / s for v, m, s in zip(x, means, spreads)] for x in xs]


def cross_validated(xs, ys, folds=5):
    """The AUC of the logistic composite on rows it was not fitted on (folds interleaved, so each holds noted turns)."""
    scored = []
    for fold in range(folds):
        train = [i for i in range(len(xs)) if i % folds != fold]
        test = [i for i in range(len(xs)) if i % folds == fold]
        reference = [xs[i] for i in train]
        w = logistic(standardize(reference), [ys[i] for i in train])
        for x, i in zip(standardize([xs[i] for i in test], reference), test):
            scored.append((sum(wi * xi for wi, xi in zip(w, x)) + w[-1], ys[i]))
    return auc([s for s, y in scored if y], [s for s, y in scored if not y])


SAME_QUESTION ='Do the candidate notes warn the user about the same problem as the reference note?'
SAME = {'same': 'Yes: one of the candidates points to the same concern about this turn, even in other words.',
        'different': "No: the candidates warn about other things, or none of them raises the reference's concern."}


def headline(note):
    """The side agent's one-line note: the text after '· ' on its first quoted line."""
    line = next((l for l in note.splitlines() if l.startswith('> ') and '·' in l), note)
    return line.split('·', 1)[-1].strip()


def claude_writers(cases, writer, quiet):
    """One writer on every noted case and `quiet` quiet ones: (rows, printed notes). A row says whether it wrote,
    and for a noted case whether hosted Jev finds one of its notes raising the side agent's concern."""
    from relay import heads_up, turn_engine
    policy = turn_engine.policy
    turn_engine.policy = lambda: {**policy(), 'heads_up': [writer]}
    try:
        rows = []
        chosen = [c for c in cases if c[3]] + [c for c in cases if not c[3]][:quiet]
        for session, index, turn, note in chosen:
            found = heads_up.facts(turn)
            new, attempts = heads_up.write(heads_up.writer_record(turn, found), [])
            row = {'writer': writer, 'session': session, 'turn': index, 'noted': bool(note), 'notes': len(new),
                   'ok': any(a['ok'] for a in attempts), 'ms': sum(a['ms'] for a in attempts)}
            if note and new:
                state = json.dumps({'reference_note': headline(note), 'candidate_notes': new}, ensure_ascii=False)
                answer = turn_engine.deciding(turn_engine.judges({'state': state, 'question': SAME_QUESTION,
                                                                   'options': SAME}))
                row['p_same'] = answer['probabilities']['same'] if answer else None
            rows.append(row)
            print(f"{'NOTED' if note else 'quiet'} {session[:8]}#{index}", *(['gold: ' + headline(note)] if note else []),
                  *('note: ' + n for n in new), sep='\n  ', flush=True)
    finally:
        turn_engine.policy = policy
    return rows


def writers_report(rows):
    for writer in dict.fromkeys(r['writer'] for r in rows):
        mine = [r for r in rows if r['writer'] == writer and r['ok']]
        noted, quiet = [r for r in mine if r['noted']], [r for r in mine if not r['noted']]
        same = [r for r in noted if (r.get('p_same') or 0) >= .5]
        print(f"{writer}: wrote for {sum(r['notes'] > 0 for r in noted)}/{len(noted)} noted turns, matching the side "
              f"agent in {len(same)}; wrote for {sum(r['notes'] > 0 for r in quiet)}/{len(quiet)} quiet turns; "
              f"median {statistics.median(r['ms'] for r in mine) / 1000 if mine else 0:.1f} s")


def auc(positive, negative):
    """The chance a noted turn scores above a quiet one (0.5 is a coin)."""
    return sum((p > n) + .5 * (p == n) for p in positive for n in negative) / (len(positive) * len(negative) or 1)


def claude_report(rows):
    judged = [r for r in rows if r['p_note'] is not None]
    noted = [r for r in judged if r['noted']]
    quiet = [r for r in judged if not r['noted']]
    print(f'{len(rows)} turns, {len(judged)} judged: {len(noted)} with an accepted side-agent note, {len(quiet)} '
          f'without; AUC {auc([r["p_note"] for r in noted], [r["p_note"] for r in quiet]):.2f}.')
    print('threshold  noted turns forwarded  quiet turns forwarded (writer calls)')
    for threshold in (.1, .2, .3, .4, .5, .6, .7, .8, .9):
        forwarded = lambda group: sum(r['p_note'] >= threshold or r['facts'] > 0 for r in group)
        print(f'{threshold:9.1f}  {forwarded(noted):>10}/{len(noted):<10}  {forwarded(quiet):>10}/{len(quiet)}')


def report(rows):
    """The log in use: what the filter's threshold saves and drops among the turns the writer answered (sampled ones
    sit below it), the notes Jev dropped as already said, those the user sent to the agent (/heads-up), Jev's shadow
    and the writer routes."""
    from relay.heads_up import FORWARD_AT, REDUNDANT_AT
    asked = {r.get('reply_sha256') for r in rows if r.get('event') == 'asked'}
    turns = [r for r in rows if not r.get('event') and r.get('score') is not None]
    ran = [r for r in turns if any(w.get('ok') for w in r.get('writers') or [])]
    noted = [r for r in ran if r['notes'] > 0]
    print(f"{len(turns)} logged turns; the writer answered {len(ran)} "
          f"({sum(r['forwarded'] == 'sample' for r in ran)} sampled below the threshold); {len(noted)} got a note, "
          f"{sum(r['reply_sha256'] in asked for r in noted)} sent to the agent with /heads-up; "
          f"{sum((r.get('redundant') or 0) >= REDUNDANT_AT for r in ran)} dropped as already said.")
    if ran:
        print(f'threshold  writer calls saved  noted turns dropped   (now {FORWARD_AT})')
        for threshold in (.05, .1, FORWARD_AT, .2, .3, .4, .5):
            dropped = [r for r in ran if r['score'] < threshold]
            print(f'{threshold:9.3f}  {len(dropped):>8}/{len(ran):<9}  {sum(r["notes"] > 0 for r in dropped):>9}/{len(noted)}')
        shadow = [r for r in ran if r.get('p_note') is not None]
        print(f"Jev's shadow against the writer's notes: AUC "
              f"{auc([r['p_note'] for r in shadow if r['notes']], [r['p_note'] for r in shadow if not r['notes']]):.2f}")
    routes = {}
    for row in rows:
        for attempt in row.get('writers') or []:
            routes.setdefault(attempt['route'], []).append(attempt)
    for route, attempts in sorted(routes.items()):
        answered = [a for a in attempts if a.get('ok')]
        print(f'{route}: {len(answered)}/{len(attempts)} answered'
              + (f', median {statistics.median(a["ms"] for a in answered) / 1000:.1f} s' if answered else ''))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    replaying = commands.add_parser('replay', help='send saved turns through the funnel, every one to the writer')
    replaying.add_argument('--sessions', type=Path,
                           default=Path(os.environ.get('LOCALAPPDATA', Path.home()))/'tare.tools',
                           help='folder searched for saved sessions (native-agent.json)')
    replaying.add_argument('--limit', type=int, default=20, help='turns to replay (each spends a writer call)')
    reporting = commands.add_parser('report', help='threshold trade and writer routes from the log')
    for command in (replaying, reporting):
        command.add_argument('--writer', help='one route as the writer (replay), or only its pinned rows (report)')
    history = commands.add_parser('claude', help="judge Claude Code turns against its side agent's accepted notes")
    history.add_argument('--projects', type=Path, default=Path.home()/'.claude'/'projects',
                         help='Claude Code transcripts folder')
    history.add_argument('--negatives', type=int, default=200, help='quiet turns sampled (each spends a judge call)')
    history.add_argument('--variant', action='append', choices=sorted(VARIANTS),
                         help='judge question to try (repeat it; default: the current one)')
    history.add_argument('--writer', action='append',
                         help='instead of the judge, run this writer on the noted turns and --quiet quiet ones')
    history.add_argument('--quiet', type=int, default=50, help='quiet turns each --writer writes for')
    history.add_argument('--claims', choices=('v1', 'v2'),
                         help='measure narrow claim questions instead: v1 over the brief, v2 as TypeSafe advises')
    history.add_argument('--brief', action='append', choices=list(BRIEFS),
                         help='what the judge reads besides the lean brief (repeat it; default: lean)')
    args = parser.parse_args(argv)
    if args.command == 'claude':
        cases = claude_cases(args.projects, args.negatives)
        if args.claims:
            return claims_report(claude_claims(cases, CLAIMS_V2 if args.claims == 'v2' else CLAIMS))
        if args.writer:
            return writers_report([r for w in args.writer for r in claude_writers(cases, w, args.quiet)])
        for variant in args.variant or ['current']:
            for brief in args.brief or ['lean']:
                print(f'== {variant} · {brief}')
                claude_report(claude_history(cases, variant, brief))
        return None
    if args.command == 'replay':
        os.environ.pop('TARE_HEADS_UP', None)  # replaying is asking for it
        print(f'{replay(args.sessions, args.limit, args.writer)} turns replayed.')
    report([r for r in log_rows() if not args.writer or r.get('pinned') == args.writer])


if __name__ == '__main__':
    main()
