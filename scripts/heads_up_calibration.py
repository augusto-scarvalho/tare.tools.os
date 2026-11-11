"""Calibrate the heads-up funnel (relay/heads_up.py) on turns that already happened.

`replay` sends saved TUI and tare serve turns through the funnel with each one forwarded to the writer, so the log
(<storage>/serve/heads-up.jsonl) holds the score beside the writer's verdict for every turn; a turn already replayed
is skipped. It spends writer and judge calls: --limit bounds them.
`notes` lists every note the TUI showed since a date, with its turn, for dogfood review: a label from a person
who reads it, where /heads-up only says a note was sent to the agent.
`report` reads the log: for each score, how many writer calls it saves and how many turns that got notes it would
have dropped, the notes Jev dropped as already said, how many the user labelled useful or not useful, and how many
notes the user really sent to the agent (a request that carries the text) and whether its reply confirmed them.
`claude` uses labels: Claude Code's side agent left the notes the user accepted in the transcripts, so each such
turn should get a note, and the other turns of those sessions may stay quiet. With no --writer it costs nothing:
the score and the cadence on every turn, measured by session, and the weights a refit gives. With --writer it runs
that writer on the noted turns and --quiet quiet ones, and hosted Jev tells whether a note matches the side agent's.
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


def noted(root, since=''):
    """Every note the TUI showed at or after `since` (an ISO time), oldest first, with its session."""
    found = []
    for path in root.glob('*/heads-up.json'):
        try:
            found += [{'session': path.parent.name, **n} for n in json.loads(path.read_text(encoding='utf-8')).get(
                'noted') or [] if n['at'] >= since]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda n: n['at'])


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
            new = heads_up.notes(turn, seen=seen, pace={'since': heads_up.EVERY}, observed={
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
    if name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):  # with what changed, as tare's edits carry it
        changes = [{'search': e.get('old_string', ''), 'replace': e.get('new_string', '')} for e in args.get('edits') or []] or [
            {'search': args.get('old_string', ''), 'replace': args.get('new_string') or args.get('content') or args.get('new_source') or ''}]
        return step('edit', {'path': args.get('file_path') or args.get('notebook_path'), 'changes': changes}, status,
                    {'error': text[:200]})
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


def claude_cases(root, negatives, seed=0):
    """Every turn whose side-agent note the user accepted, and a sample of the other finished turns of the same
    sessions (the side agent was on and said nothing): [(session, turn index, turn, note or None)]. A turn a
    continued session repeats counts once."""
    import hashlib
    import random
    cases, seen = [], set()
    for path in sorted(root.glob('*/*.jsonl')):
        turns = claude_turns(path)
        if any(note for _, note in turns):
            for index, (turn, note) in enumerate(turns):
                key = hashlib.sha256((turn['request'] + '\0' + turn['reply']).encode('utf-8')).digest()
                if turn['reply'] and key not in seen:
                    seen.add(key)
                    cases.append((path.stem, index, turn, note))
    others = [c for c in cases if not c[3]]
    return [c for c in cases if c[3]] + random.Random(seed).sample(others, min(negatives, len(others)))


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


def cross_validated(xs, ys, groups):
    """The AUC of the logistic composite on rows it was not fitted on: each group (a session) is scored by a model
    fitted on the others, since turns of one session look alike and would flatter a model that saw their neighbors."""
    scored = []
    for group in dict.fromkeys(groups):
        train = [i for i, g in enumerate(groups) if g != group]
        test = [i for i, g in enumerate(groups) if g == group]
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
            new, attempts = heads_up.write(heads_up.writer_record(turn), [])
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


def cadence(rows, at, every):
    """(writer calls per turn, share of noted turns some run covers) for rows (session, index, score, noted): the
    writer runs at `at` or on every `every`-th turn since it last ran (None: never), and covers the noted turns of
    the window of turns since its last run (the turn alone without a cadence)."""
    calls, covered, noted = 0, 0, sum(r[3] for r in rows)
    for session in dict.fromkeys(r[0] for r in rows):
        since, pending = 0, []
        for _, index, score, label in sorted((r for r in rows if r[0] == session), key=lambda r: r[1]):
            since += 1
            pending += [index] if label else []
            if score >= at or (every and since >= every):
                calls += 1
                covered += sum(i > index - (since if every else 1) for i in pending)
                since, pending = 0, []
    return calls / len(rows), covered / (noted or 1)


def fit(cases):
    """Code only, no calls: the score and the cadence on the labeled turns, a refit's AUC by session, and the
    FILTER that refit gives (for relay/heads_up.py)."""
    from relay import heads_up
    rows, xs = [], []
    for session, index, turn, note in cases:
        found = heads_up.facts(turn)
        rows.append((session, index, heads_up.score(turn, found), bool(note)))
        xs.append([heads_up.counts(turn, found)[name] for name in heads_up.FILTER])
    ys, groups = [r[3] for r in rows], [r[0] for r in rows]
    noted = [r[2] for r in rows if r[3]]
    print(f'{len(rows)} turns, {len(noted)} noted. Score AUC {auc(noted, [r[2] for r in rows if not r[3]]):.2f}; '
          f'a refit, scored on sessions it never saw: {cross_validated(xs, ys, groups):.2f}.')
    print(f'score at  every  writer calls  noted turns covered   (now {heads_up.FORWARD_AT}, {heads_up.EVERY})')
    for at in sorted({.137, .19, heads_up.FORWARD_AT, .3}):
        for every in (None, 3, heads_up.EVERY, 6):
            calls, covered = cadence(rows, at, every)
            print(f'{at:8.3f}  {every or "-":>5}  {calls:12.0%}  {covered:19.0%}')
    w = logistic(standardize(xs), ys)
    columns = list(zip(*xs))
    print('FILTER = {' + ', '.join(f"'{name}': ({statistics.fmean(c):.3f}, {statistics.pstdev(c) or 1:.3f}, {wi:.3f})"
                                    for name, c, wi in zip(heads_up.FILTER, columns, w)) + '}')
    print(f'FILTER_BIAS = {w[-1]:.3f}')


def report(rows):
    """The log in use: what the score saves and drops among the turns the writer answered (the cadence's sit below
    it), the notes Jev dropped as already said, those the user sent to the agent (/heads-up), and the writer
    routes."""
    from relay.heads_up import CONFIRMED_AT, FORWARD_AT, REDUNDANT_AT
    asked = {r.get('reply_sha256') for r in rows if r.get('event') == 'asked'}
    turns = [r for r in rows if not r.get('event') and r.get('score') is not None]
    ran = [r for r in turns if any(w.get('ok') for w in r.get('writers') or [])]
    noted = [r for r in ran if r['notes'] > 0]
    print(f"{len(turns)} logged turns; the writer answered {len(ran)} "
          f"({sum(r['forwarded'] == 'cadence' for r in ran)} by cadence below the score); {len(noted)} got a note, "
          f"{sum(r['reply_sha256'] in asked for r in noted)} sent to the agent; "
          f"{sum((r.get('redundant') or 0) >= REDUNDANT_AT for r in ran)} dropped as already said.")
    labels = {r.get('reply_sha256'): r.get('value') for r in rows if r.get('event') == 'label'}  # the last one wins
    checked = [r.get('confirmed') for r in rows if r.get('event') == 'checked']
    print(f"{sum(v == 'useful' for v in labels.values())} notes labelled useful, "
          f"{sum(v == 'not_useful' for v in labels.values())} not useful; "
          f"{sum(r.get('event') == 'asked' for r in rows)} sent to the agent "
          f"({sum(r.get('event') == 'asked' and bool(r.get('edited')) for r in rows)} with the note rewritten), "
          f"{sum(c is not None and c >= CONFIRMED_AT for c in checked)} confirmed by its reply, "
          f"{sum(c is not None and c < CONFIRMED_AT for c in checked)} not confirmed, "
          f"{sum(c is None for c in checked)} without an answer.")
    if ran:
        print(f'threshold  writer calls saved  noted turns dropped   (now {FORWARD_AT})')
        for threshold in sorted({.05, .1, .2, FORWARD_AT, .3, .4, .5}):
            dropped = [r for r in ran if r['score'] < threshold]
            print(f'{threshold:9.3f}  {len(dropped):>8}/{len(ran):<9}  {sum(r["notes"] > 0 for r in dropped):>9}/{len(noted)}')
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
    listing = commands.add_parser('notes', help='the notes the TUI showed, with their turns, as JSON lines')
    listing.add_argument('--since', default='', help='ISO time of the oldest note listed')
    history = commands.add_parser('claude', help="the funnel on Claude Code turns against its side agent's notes")
    history.add_argument('--projects', type=Path, default=Path.home()/'.claude'/'projects',
                         help='Claude Code transcripts folder')
    history.add_argument('--writer', action='append',
                         help='run this writer on the noted turns and --quiet quiet ones (spends writer calls)')
    history.add_argument('--quiet', type=int, default=50, help='quiet turns each --writer writes for')
    args = parser.parse_args(argv)
    if args.command == 'claude':
        if args.writer:
            cases = claude_cases(args.projects, args.quiet)
            return writers_report([r for w in args.writer for r in claude_writers(cases, w, args.quiet)])
        return fit(claude_cases(args.projects, 10**9))
    if args.command == 'notes':
        from relay.native_agent import session_root
        for note in noted(session_root(), args.since):
            print(json.dumps(note, ensure_ascii=False))
        return 0
    if args.command == 'replay':
        os.environ.pop('TARE_HEADS_UP', None)  # replaying is asking for it
        print(f'{replay(args.sessions, args.limit, args.writer)} turns replayed.')
    report([r for r in log_rows() if not args.writer or r.get('pinned') == args.writer])


if __name__ == '__main__':
    main()
