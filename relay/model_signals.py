"""Public benchmark signals of tare's model lines, and the class (frontier, middling, cheap) they give a route.

Read, the first source that answers: OpenRouter's relay of Artificial Analysis's indices (with the OpenRouter key
already stored, never another), then the LMArena agent dataset (keyless). Left out on purpose: Artificial Analysis's
own API (it needs a key of its own that no one has stored) and Epoch AI (a zip). The reading is shown with its source
and date and only places a model in a class; which route runs is decided elsewhere. Rows are matched to a line by
`vendor_models.parse`, never by a table of models: a name that is not exactly one known line, version and effort, or
that two rows claim, is refused. A model with no line (Kimi, Grok, GLM...) is placed by the known lines of the same
reading, its anchors: the class whose weakest anchor it reaches (owner, 2026-11-16).

Only the LMArena agent board carries a cost per task. It is kept beside a reading whose class came from OpenRouter
(`board`), and `task` gives a route's score and cost per task from one row of it.
"""
import json
import math
import re
import time
from urllib.parse import urlsplit

from .model_choices import EFFORTS
from .provider_prices import _local_json, _when
from .vendor_models import AGY_EFFORT, LINES, line, parse

SCHEMA = 'tare.tools/model-signals/1'
OPENROUTER = 'https://openrouter.ai/api/v1/benchmarks?source=artificial-analysis'
ARENA = ('https://datasets-server.huggingface.co/rows?dataset=lmarena-ai/leaderboard-dataset'
         '&config=agent&split=latest&offset=0&length=100')
# Absolute cut points per source, the one place they are written (never relative to the leader: a new leader must not
# demote the others; a deliberate edit here moves them). A score at or above the first cut is frontier, else at or
# above the second middling, else cheap. 'openrouter' is Artificial Analysis's intelligence index (0 to 100), 'arena'
# the dataset's Arena-style rating (about -0.2 to 0.3, not an index). Both read at one effort per model, so a model's
# row counts only for the route that runs at that effort.
CUTS = {'openrouter': ((56, 'frontier'), (40, 'middling')), 'arena': ((0.14, 'frontier'), (0.05, 'middling'))}
CITATIONS = {'openrouter': 'Source: Artificial Analysis (artificialanalysis.ai) via OpenRouter (openrouter.ai/rankings)',
             'arena': 'Source: LMArena agent leaderboard, CC BY 4.0 (huggingface.co/datasets/lmarena-ai/leaderboard-dataset)'}
ORDER = ('cheap', 'middling', 'frontier')  # ascending: a route reaches a need when its class is not before it
BRACKET = re.compile(r'(.+?)\s*\(([^()]*)\)')


def reading_path():
    from .native_installation import storage_root
    return storage_root()/'serve'/'model-signals.json'


_reading = (None, None)  # (file stamp, reading), as in provider_prices


def reading():
    """The last good reading ({'source', 'as_of', 'version', 'read_at', 'rows', 'previous'}) or None: no reading, and
    every class is the line's own. Read once per change of the file."""
    global _reading
    path = reading_path()
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if _reading[0] != stamp:
        value = _local_json(path)
        ok = isinstance(value, dict) and value.get('schema') == SCHEMA and value.get('source') in CUTS \
            and isinstance(value.get('rows'), list) and _when(value.get('read_at'), 32503680000) is not None
        _reading = (stamp, value if ok else None)
    return _reading[1]


def normalise(name, creator):
    """(line, version, effort) of a source's model name ('GPT-6.1 Sol (Max)' by openai is ('gpt-sol', '6.1', 'max')),
    or None. The bracket must hold exactly one known effort and the creator must be the line's vendor."""
    found = BRACKET.fullmatch(name.strip()) if isinstance(name, str) else None
    if not found or found[2].strip().lower() not in EFFORTS:
        return None
    slug = found[1].lower().replace(' ', '-')
    row = line(slug)
    if not row or not isinstance(creator, str) or creator.casefold() != row['vendor'].casefold():
        return None
    return row['line'], '.'.join(map(str, parse(slug)[2])), found[2].strip().lower()


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _rows(items):
    """Rows keyed by (line, version, effort) from [(key, intelligence, coding, agentic, cost)]; a key two items claim
    is refused with both."""
    seen, rows = {}, []
    for key, *numbers in items:
        seen[key] = seen.get(key, 0) + 1
        rows.append((key, *map(_number, numbers)))
    return [{'line': k[0], 'version': k[1], 'effort': k[2], 'intelligence': i, 'coding': c, 'agentic': a,
             'cost_per_task_usd': p} for k, i, c, a, p in rows if seen[k] == 1 and i is not None]


def _bare(name):
    """(name, effort): a source's name with its one effort bracket taken off; any other bracket (a date, a variant)
    stays in the name, which `_match` refuses."""
    found = BRACKET.fullmatch(name.strip())
    return (found[1], found[2].strip().lower()) if found and found[2].strip().lower() in EFFORTS else (name.strip(), None)


def _others(items):
    """Rows of models with no line, from [(creator, name, permaslug, intelligence, coding, agentic, cost)]. They carry
    the source's own identifiers and `line` None; two rows claiming one name are refused when a route is matched."""
    return [{'line': None, 'creator': c, 'name': (b := _bare(n))[0], 'effort': b[1], 'permaslug': s, 'intelligence': _number(i),
             'coding': _number(g), 'agentic': _number(a), 'cost_per_task_usd': _number(p)}
            for c, n, s, i, g, a, p in items if isinstance(c, str) and isinstance(n, str) and _number(i) is not None]


def parse_openrouter(body):
    """(rows, as_of, version) of OpenRouter's benchmarks answer, or None when it is not shaped as expected."""
    body = body if isinstance(body, dict) else {}
    data, meta = body.get('data'), body.get('meta') if isinstance(body.get('meta'), dict) else {}
    if not isinstance(data, list) or not isinstance(meta.get('as_of'), str):
        return None
    items, others = [], []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get('model_permaslug'), str):
            creator = item['model_permaslug'].split('/')[0]
            numbers = (item.get('intelligence_index'), item.get('coding_index'), item.get('agentic_index'))
            key = normalise(item.get('display_name'), creator)
            if key:
                items.append((key, *numbers, None))
            else:
                others.append((creator, item.get('display_name'), item['model_permaslug'], *numbers, None))
    return _rows(items) + _others(others), meta['as_of'][:10], str(meta.get('version'))


def parse_arena(body):
    """(rows, as_of, version) of the Hugging Face rows answer; the score stands in `intelligence`."""
    data = body.get('rows') if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    items, others, dates = [], [], []
    for item in data:
        row = item.get('row') if isinstance(item, dict) else None
        if isinstance(row, dict):
            dates.append(str(row.get('leaderboard_publish_date')))
            key = normalise(row.get('model_name'), row.get('organization'))
            if key:
                items.append((key, row.get('score'), None, None, row.get('cost_per_task_p50')))
            else:
                others.append((row.get('organization'), row.get('model_name'), None, row.get('score'), None, None,
                               row.get('cost_per_task_p50')))
    return (_rows(items) + _others(others), max(dates), None) if dates else None


def _get(url, headers=None):
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={'Accept': 'application/json', 'User-Agent': 'tare.tools', **(headers or {})}),
                 timeout=30) as reply:
        raw = reply.read(8*1024*1024 + 1)
    if len(raw) > 8*1024*1024:
        raise ValueError('benchmark reading over 8 MiB')
    return json.loads(raw)


def fetch(source, key=None):
    return _get(OPENROUTER, {'Authorization': 'Bearer '+key}) if source == 'openrouter' else _get(ARENA)


def _read(fetch, source, parse_it, key):
    try:  # reading and parsing a third party's answer is one step: whatever fails in it, there is no reading from it
        return parse_it(fetch(source, key))
    except AssertionError:
        raise  # a failed check (a test's, or tare's own) is a bug, never an outage
    except Exception:  # the network, or an answer shaped as nobody foresaw
        return None


def refresh_reading(now=None, fetch=fetch, setup=None):
    """Read the sources at most once a day (after an attempt that read nothing, at most once an hour) and keep the
    reading with the one before it, which the two-reading rule of `klass` needs. A failed, empty or reshaped reading
    keeps the last one. `fetch(source, key)` returns a source's JSON."""
    global _reading
    now = time.time() if now is None else now
    saved = reading()
    if saved and _when(saved['read_at'], now) is not None and now - saved['read_at'] < 86400:
        return saved
    attempt = reading_path().with_name('model-signals-attempt.json')
    record = _local_json(attempt)
    last = _when(record.get('at'), now) if isinstance(record, dict) else None
    if last is not None and now - last < 3600:
        return saved
    from .environment import declined, read
    from .provider_quota import secret
    from .relay_io import write_json_atomic
    setup = read() if setup is None else setup
    api = setup.get('api_providers') or {}
    if 'error' in setup or api.get('use') is False:
        return saved  # the machine's choice (tare setup --no-use api): nothing leaves it for a reading
    key = None if declined((api.get('items') or {}).get('OPENROUTER_API_KEY')) else secret('OPENROUTER_API_KEY')
    attempt.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(attempt, {'at': now})
    for source, parse_it in (('openrouter', parse_openrouter), ('arena', parse_arena)):
        if source == 'openrouter' and not key:
            continue
        found = _read(fetch, source, parse_it, key)
        if found and found[0]:
            rows, as_of, version = found
            value = {'schema': SCHEMA, 'source': source, 'citation': CITATIONS[source], 'as_of': as_of,
                     'version': version, 'read_at': now, 'rows': rows}
            if source == 'openrouter':  # the arena is its own board; OpenRouter has no cost per task, so read it too
                board = _read(fetch, 'arena', parse_arena, None)
                board = {'as_of': board[1], 'rows': board[0]} if board and board[0] else _board(saved)
                if board:
                    value['board'] = board
            if saved:
                value['previous'] = {k: v for k, v in saved.items() if k not in ('previous', 'board')}
            write_json_atomic(reading_path(), value)
            _reading = (None, None)
            return value
    return saved


def _board(saved):
    """{'as_of', 'rows'} of the LMArena agent board kept with a reading: the reading itself when the arena is its
    source, else its `board`; None when it has none."""
    board = saved if (saved or {}).get('source') == 'arena' else (saved or {}).get('board')
    return {'as_of': board.get('as_of'), 'rows': board['rows']} if isinstance(board, dict) and isinstance(board.get('rows'), list) else None


def band(source, score):
    return next((name for cut, name in CUTS[source] if score >= cut), 'cheap')


def _row(saved, key):
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict)
            and (r.get('line'), r.get('version'), r.get('effort')) == key]
    return rows[0] if len(rows) == 1 and _number(rows[0].get('intelligence')) is not None else None


def _slug(deployment):
    """The slug a route's line and version are read from: its runtime model id, or the catalogue's own `model` when
    that id names no line (Bedrock's us.anthropic.claude-opus-4-6-v1 is claude-opus-4.6)."""
    for slug in (deployment['native_profile']['model'], deployment.get('model')):
        if isinstance(slug, str) and line(slug):
            return slug


_versions = (None, {})  # (the installed routes and the setup's change time, {line: newest version})
def _newest():
    """{line: the newest version of it this machine can use by itself}: a route installed here whose role-table row
    is qualified or has answered once on this machine (tare setup --prove). A version tare merely lists lowers
    nobody: the model in use keeps its class until its successor is proven here."""
    global _versions
    from . import environment, native_routes
    from .native_catalog import offline_deployments
    from .native_profiles import names
    try:
        changed = environment.store().stat().st_mtime_ns
    except OSError:
        changed = None
    stamp = (tuple(sorted(names())), changed)
    if _versions[0] != stamp:
        rows = {row.get('native_catalog', row['id']): row for row in native_routes.configured()['deployments']}
        proofs, found = {}, {}
        for name, value, _ in offline_deployments():
            try:
                row = rows.get(name)
                if row is not None and row.get('qualified') is False and not native_routes.proven(row, proofs):
                    continue  # listed, never answered here
                slug = _slug(value)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if slug:
                family, name, version = parse(AGY_EFFORT.sub('', slug))
                found[family+'-'+name] = max(found.get(family+'-'+name, ()), version)
        _versions = (stamp, found)
    return _versions[1]


def _version(text):
    return tuple(map(int, text.split('.'))) if isinstance(text, str) and re.fullmatch(r'\d+(\.\d+)*', text) else None


def _anchors(saved):
    """[(score, class, name)], one per known line in the reading: its newest version, at the highest effort listed
    for that version, with the line's declared class. A line whose newest version the reading names only as a variant
    ("Claude Opus 5.5 (Max, Default Fallback)") gives no anchor: the clean row of an older version is another, weaker
    model, and as an anchor it would lower its class's floor."""
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict)]
    found, newest = {}, {}
    for r in rows:
        if r.get('line') in LINES and _version(r.get('version')):
            name, version = r['line'], _version(r['version'])
            if r.get('effort') in EFFORTS and _number(r.get('intelligence')) is not None:
                found.setdefault(name, []).append(((version, EFFORTS.index(r['effort'])), r))
        elif isinstance(r.get('name'), str) and isinstance(r.get('creator'), str):
            slug = re.sub(r'\s*\(.*$', '', r['name']).lower().replace(' ', '-')
            known = line(slug)
            if not known or known['vendor'].casefold() != r['creator'].casefold():
                continue
            name, version = known['line'], parse(slug)[2]
        else:
            continue
        newest[name] = max(newest.get(name, version), version)
    return [(r['intelligence'], LINES[name]['class'], LINES[name]['label'].format(version=r['version']))
            for name, listed in found.items() for (version, _), r in [max(listed, key=lambda item: item[0])]
            if version == newest[name]]


def _place(anchors, score):
    """(class, anchor name): the highest class whose weakest anchor the score reaches, else cheap (below the weakest
    middling anchor). A class with no anchor cannot be entered, and with no middling anchor nothing is placed."""
    weakest = {c: min((a[0], a[2]) for a in anchors if a[1] == c) for c in ('frontier', 'middling')
               if any(a[1] == c for a in anchors)}
    if 'middling' not in weakest:
        return None
    for c in ('frontier', 'middling'):
        if c in weakest and score >= weakest[c][0]:
            return c, weakest[c][1]
    return 'cheap', weakest['middling'][1]


RELATIONS = {'quantized': 'a quantized build', 'fine-tune': 'a fine-tuned build', 'variant': 'a variant'}


def measured_as(deployment):
    """(model id, relation) a profile declares in `native_measured_as`: a string is the same weights under another id
    (relation None); {'model', 'relation'} names the base model of a build that is not it (RELATIONS): the reading's
    numbers are then the base model's, and everything that shows or uses them says so. (None, None) without it."""
    value = deployment.get('native_measured_as')
    if isinstance(value, dict) and isinstance(value.get('model'), str) and value.get('relation') in RELATIONS:
        return value['model'], value['relation']
    return (value, None) if isinstance(value, str) else (None, None)


def _match(saved, deployment):
    """The row of a route's model among those with no line, at the highest effort the reading lists for it, or None.
    Exact or nothing: an OpenRouter route's model id is the row's permaslug, with or without its `-YYYYMMDD`; any
    other route's id (its `vendor/` checked against the creator when it has one, the same house in either spelling) is
    the row's name lowercased and hyphenated, with or without the row's snapshot date. A name or permaslug two rows
    claim (a variant or another date of the model counts), two rows at the top effort, and a name with a bracket left
    (a variant) match nothing."""
    profile = deployment['native_profile']
    # A profile may say which measured model it serves when its own id is a variant of it with the same weights
    # (meta/muse-spark-1.3-contributor is Muse Spark 1.3 under a data-sharing term).
    model = measured_as(deployment)[0] or profile.get('model')
    if not isinstance(model, str):
        return None
    openrouter = urlsplit(profile.get('endpoint') or '').hostname == 'openrouter.ai'
    vendor, _, name = model.rpartition('/')

    def house(a, b):  # one creator spelled by two catalogues: z-ai and zai, moonshotai and moonshot (measured 2026-11-16)
        a, b = (re.sub(r'[^a-z0-9]', '', text.lower()) for text in (a, b))
        return a == b or a == b + 'ai' or b == a + 'ai'

    def named(mine, row):  # the row may carry the snapshot date the route's id leaves out (-20261017) or shortens (-1017)
        dated = re.fullmatch(r'(.+)-(\d{8}|\d{4})', row)
        short = re.fullmatch(r'(.+)-(\d{4})', mine)
        return mine == row or bool(dated) and (mine == dated[1] or bool(short) and short[1] == dated[1] and dated[2].endswith(short[2]))

    def same(r):
        if openrouter and isinstance(r.get('permaslug'), str):
            return named(model, r['permaslug'])
        return named(name, r['name'].lower().replace(' ', '-')) and (not vendor or house(vendor, r['creator']))
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict) and r.get('line') is None
            and isinstance(r.get('name'), str) and isinstance(r.get('creator'), str)
            and _number(r.get('intelligence')) is not None and same(r)]
    rank = lambda r: EFFORTS.index(r['effort']) if r.get('effort') in EFFORTS else -1
    top = [r for r in rows if rank(r) == max(map(rank, rows))] if rows else []
    one = len(top) == 1 and len({(r.get('permaslug'), r['creator'], r['name']) for r in rows}) == 1
    return top[0] if one and '(' not in top[0]['name'] else None


def _placed(saved, deployment):
    row = _match(saved, deployment)
    placed = row and _place(_anchors(saved), row['intelligence'])
    return (*placed, row) if placed else None


def _by_anchors(deployment, saved):
    """The class of a model with no line, from the known lines of the reading, only when the two latest readings of
    the same source and index version agree; otherwise None."""
    saved = reading() if saved is None else saved
    now = _placed(saved, deployment) if saved else None
    before = (saved or {}).get('previous')
    if now and isinstance(before, dict) and (before.get('source'), before.get('version')) == (saved['source'], saved.get('version')):
        before = _placed(before, deployment)
        if before and before[0] == now[0]:
            relation = measured_as(deployment)[1]
            return {'class': now[0], 'from': 'anchors', 'source': saved['source'], 'as_of': saved.get('as_of'),
                    'intelligence': now[2]['intelligence'], 'cost_per_task_usd': now[2].get('cost_per_task_usd'),
                    'anchor': now[1], **({'base': now[2]['name'], 'relation': relation} if relation else {})}


def _version_effort(deployment, model):
    from .execution_team import reasoning
    suffix = AGY_EFFORT.search(deployment['native_profile']['model'])
    return parse(AGY_EFFORT.sub('', model))[2], suffix[1] if suffix else reasoning(deployment)


def klass(deployment, saved=None):
    """{'class', 'from': 'reading' | 'line', plus 'source', 'as_of', 'intelligence', 'cost_per_task_usd' when the
    reading has the route's model at its default effort} for a deployment. A model with no line gets {'class', 'from':
    'anchors', 'source', 'as_of', 'intelligence', 'cost_per_task_usd', 'anchor'} (`_by_anchors`), else None. The
    class is the reading's (absolute cuts of CUTS) only when the two latest readings of the same source and index
    version put the model in the same class; otherwise the line's own, which no reading overrides on one word. The
    line's class is its newest version's: an older version gets one class lower and says so ('older_than')."""
    model = _slug(deployment)
    known = model and line(model)
    if not known:
        return _by_anchors(deployment, saved)
    version, effort = _version_effort(deployment, model)
    key = (known['line'], '.'.join(map(str, version)), effort)
    # The line's class is the class of its newest version usable here. An older version is another model (Opus 4.6
    # on Bedrock is not the Opus 5.5 of the CLI): one class lower, since nothing measured places it higher, until a
    # reading of its own.
    newest = _newest().get(known['line'], version)
    own = {'class': known['class'], 'from': 'line'}
    if version < newest:
        own = {'class': ORDER[max(0, ORDER.index(known['class']) - 1)], 'from': 'line',
               'older_than': '.'.join(map(str, newest))}
    saved = reading() if saved is None else saved
    now = _row(saved, key)
    if not now:
        return own
    numbers = {'source': saved['source'], 'as_of': saved.get('as_of'), 'intelligence': now['intelligence'],
               'cost_per_task_usd': now.get('cost_per_task_usd')}
    before = saved.get('previous')
    before = _row(before, key) if isinstance(before, dict) and \
        (before.get('source'), before.get('version')) == (saved['source'], saved.get('version')) else None
    chosen = band(saved['source'], now['intelligence'])
    if before and band(saved['source'], before['intelligence']) == chosen:
        return {'class': chosen, 'from': 'reading', **numbers}
    return {**own, **numbers}


def task(deployment, saved=None):
    """{'score', 'cost_per_task_usd', 'value', 'source', 'as_of', 'effort'} of a route's model from ONE row of the
    LMArena agent board, or None when the board has no single row of it or the row lacks the score or the cost. A model
    of a line is read at the route's own effort when the board lists it, else at the highest it lists ('effort' says
    which); a model with no line goes through `_match`. The value is the score above the board's lowest score, per
    dollar of a task: Arena ratings go below zero, so the lowest listed model is the zero of the scale, a rating alone
    cannot be divided by a cost."""
    saved = reading() if saved is None else saved
    board = _board(saved)
    if not board:
        return None
    model = _slug(deployment)
    known = model and line(model)
    if known:
        version, effort = _version_effort(deployment, model)
        listed = [r for r in board['rows'] if isinstance(r, dict) and r.get('line') == known['line']
                  and r.get('version') == '.'.join(map(str, version)) and r.get('effort') in EFFORTS]
        top = max((EFFORTS.index(r['effort']) for r in listed), default=-1)
        at = [r for r in listed if r['effort'] == effort] or [r for r in listed if EFFORTS.index(r['effort']) == top]
        row = at[0] if len(at) == 1 else None
    else:
        row = _match(board, deployment)
        if row is None and (named := _match(saved, deployment)):
            # The class source knows the route by its permaslug and names the model as its creator does (Mistral Large
            # 4 for mistralai/mistral-large-4-0): that name finds the board's row where the route's id does not.
            row = _match(board, {'native_profile': {'model': named['creator'] + '/' + named['name'].lower().replace(' ', '-')}})
    score, cost = row and _number(row.get('intelligence')), row and _number(row.get('cost_per_task_usd'))
    if score is None or cost is None or cost <= 0:
        return None
    relation = measured_as(deployment)[1]
    if relation:
        from .challenger_variants import accounting
        if accounting(deployment) == 'local':
            return None  # the board's cost is what the full model costs by an API: a local build costs its energy
    lowest = min(s for r in board['rows'] if isinstance(r, dict) and (s := _number(r.get('intelligence'))) is not None)
    return {'score': score, 'cost_per_task_usd': cost, 'value': (score - lowest)/cost, 'source': 'arena',
            'as_of': board['as_of'], 'effort': row.get('effort'),
            **({'base': row['name'], 'relation': relation} if relation else {})}


def describe_task(found):
    """One phrase for `tare models` and the matrix: what a task costs on the board, with its source and date; '' when
    the board has no figure. A public figure of the board, never tare's own measurement."""
    return f"about US$ {found['cost_per_task_usd']:.2f} per task" + (f" at {found['effort']} effort" if found['effort'] else '')         + f" on the LMArena agent board, {found['as_of']}" + _of_base(found) if found else ''


def _of_base(found):
    """' (of its base model X; this route runs a quantized build)' when the numbers are a base model's, else ''."""
    return f" (of its base model {found['base']}; this route runs {RELATIONS[found['relation']]})" if found.get('relation') else ''


def own_record(storage=None):
    """How each route did as implementer on this machine, from the unit receipts: {route: {'ran', 'passed',
    'audited', 'accepted', 'applied', and per class a unit needed ('needs', class): [ran, passed]}}. Raw counts,
    shown and never steering: harder work already goes to stronger models, so they do not compare two."""
    from collections import Counter, defaultdict
    if storage is None:
        from .native_installation import storage_root
        storage = storage_root()
    found = defaultdict(Counter)
    for path in storage.glob('agent-sessions/*/units/*/receipt.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            for row in value['rungs']:
                tally, passed = found[row['route']], row.get('verdict') == 'VERIFIED'
                # int: an empty Counter keeps a first bool as it is
                tally.update(ran=1, passed=int(passed), audited=int('audit' in row),
                             accepted=int((row.get('audit') or {}).get('verdict') == 'ACCEPT'))
                if row.get('needs') in ORDER:
                    tally.update({('needs', row['needs'], 'ran'): 1, ('needs', row['needs'], 'passed'): int(passed)})
            if value['result'].get('applied'):
                found[value['result']['route']]['applied'] += 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # a receipt still being written, or of another shape, is not a record
    return found


def describe_record(tally):
    """One line of `tare models`: a route's own record as implementer, or None when it never ran here."""
    if not tally or not tally['ran']:
        return None
    text = (f"{tally['ran']} runs; {tally['passed']} passed the checks; "
            f"{tally['accepted']} of {tally['audited']} audits accepted; {tally['applied']} applied")
    for needed in ORDER:
        if tally['needs', needed, 'ran']:
            text += f"; needed {needed}: {tally['needs', needed, 'passed']} of {tally['needs', needed, 'ran']} passed"
    return text


def short(deployment, needs):
    """How many classes a route's class falls short of the class a unit needs: 0 when it reaches it. A model with
    no class (no line and no row of the reading to place it by) counts as cheap: nothing known places it higher."""
    found = klass(deployment) or {}
    return max(0, ORDER.index(needs) - ORDER.index(found.get('class') or 'cheap'))


def reaches(deployment, needs):
    return short(deployment, needs) == 0


def describe(result):
    """One line of `tare models`: the class, where it came from and the numbers."""
    if not result:
        return 'none (no model line, and no benchmark row to place it by)'
    if result['from'] == 'anchors':
        return (f"{result['class']}: {'falls below' if result['class'] == 'cheap' else 'reaches'} {result['anchor']} "
                f"on {result['source']}, {result['as_of']}") + _of_base(result)
    text = result['class'] + (' (reading' if result['from'] == 'reading' else ' (line default')
    if result['from'] == 'line' and 'older_than' in result:
        text += f", one class lower: an older version than {result['older_than']}"
    if 'source' in result:
        text += f"; {result['source']} {result['as_of']}: intelligence {result['intelligence']:g}"
        if result['cost_per_task_usd'] is not None:
            text += f", US${result['cost_per_task_usd']:.2f} per task"
        text += '' if result['from'] == 'reading' else '; not confirmed by two readings'
    return text + ')'
