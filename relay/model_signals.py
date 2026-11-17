"""Public benchmark signals of tare's model lines, and the class (frontier, middling, cheap) they give a route.

Read, the first source that answers: OpenRouter's relay of Artificial Analysis's indices (with the OpenRouter key
already stored, never another), then the LMArena agent dataset (keyless). Left out on purpose: Artificial Analysis's
own API (it needs a key of its own that no one has stored). The reading is shown with its source
and date and only places a model in a class; which route runs is decided elsewhere. Rows are matched to a line by
`vendor_models.parse`, never by a table of models: a name that is not exactly one known line, version and effort, or
that two rows claim, is refused. A model with no line (Kimi, Grok, GLM...) is placed by the known lines of the same
reading, its anchors: the class whose weakest anchor it reaches (owner, 2026-11-16).

Beside it, `relay/task_types.json` is the catalogue of task types (coding, mathematics, web...), each with the boards
(LMArena configs, CSV files of Epoch AI's archive) that say how models do at that type. `refresh_boards` reads them again
with the same rule (once a day, no key, nothing on a machine that declined API use), keeps them in
`serve/task-boards.json`, and a board places a model with the same anchors; a type no board places a model for falls back
to the general class and says so. Nothing routes by them yet: they are shown (`tare models --type`, `--types`).

Only the LMArena agent board carries a cost per task. It is kept beside a reading whose class came from OpenRouter
(`board`), and `task` gives a route's score and cost per task from one row of it.
"""
import csv
import io
import json
import math
import re
import time
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
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
EPOCH = 'https://epoch.ai/data/benchmark_data.zip'  # one archive of CSV files, about 2.3 MB
BOARDS_SCHEMA = 'tare.tools/task-boards/1'
CAP = 8*1024*1024  # the most accepted from the network, and from one member of the archive once opened
ORDER = ('cheap', 'middling', 'frontier')  # ascending: a route reaches a need when its class is not before it


def shown(name):
    """The word a person reads for a class: 'cheap' is the lowest class, enough for simple work, and a model in it may be dear.
    Only for text a person reads; the value stays 'cheap' in plans, receipts, JSON and what a model is told."""
    return 'basic' if name == 'cheap' else name
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


def normalise(name, creator, bare=False):
    """(line, version, effort) of a source's model name ('GPT-6.1 Sol (Max)' by openai is ('gpt-sol', '6.1', 'max')),
    or None. A bracket must hold exactly one known effort and the creator must be the line's vendor. `bare` is for a
    page's display names: a name with no bracket is the model at no stated effort (None), and a creator of None means
    the page names none, so the line's vendor stands."""
    text = name.strip() if isinstance(name, str) else ''
    found = BRACKET.fullmatch(text)
    base, effort = (found[1], found[2].strip().lower()) if found else (text if bare else '', None)
    if not base or found and effort not in EFFORTS:
        return None
    slug = base.lower().replace(' ', '-')
    row = line(slug)
    vendor = row['vendor'] if row and bare and creator is None else creator
    if not row or not isinstance(vendor, str) or vendor.casefold() != row['vendor'].casefold():
        return None
    return row['line'], '.'.join(map(str, parse(slug)[2])), effort


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


def parse_arena(body, column='score', category=None):
    """(rows, as_of, version) of the Hugging Face rows answer; the score (`column`) stands in `intelligence`. Another
    config names its score column otherwise (webdev: rating) and holds several categories (`category` keeps one)."""
    data = body.get('rows') if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    items, others, dates = [], [], []
    for item in data:
        row = item.get('row') if isinstance(item, dict) else None
        if isinstance(row, dict) and (category is None or row.get('category') == category):
            dates.append(str(row.get('leaderboard_publish_date')))
            key = normalise(row.get('model_name'), row.get('organization'))
            if key:
                items.append((key, row.get(column), None, None, row.get('cost_per_task_p50')))
            else:
                others.append((row.get('organization'), row.get('model_name'), None, row.get(column), None, None,
                               row.get('cost_per_task_p50')))
    return (_rows(items) + _others(others), max(dates), None) if dates else None


def _get(url, headers=None):
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={'Accept': 'application/json', 'User-Agent': 'tare.tools', **(headers or {})}),
                 timeout=30) as reply:
        raw = reply.read(CAP + 1)
    if len(raw) > CAP:
        raise ValueError('benchmark reading over 8 MiB')
    return raw


def fetch(source, key=None):
    """A source's answer: OpenRouter's or an LMArena config's JSON ('arena' is the agent board, 'arena:webdev' another),
    Epoch's archive as bytes ('epoch'), a public page as bytes ('page:<url>')."""
    if source.startswith('page:'):  # a public page the catalogue names (a source of reader 'page' or 'script'): only that URL
        if urlsplit(source[5:]).scheme != 'https':
            raise ValueError('a page is read over https only')
        return _get(source[5:], {'Accept': 'text/html'})
    if source == 'epoch':
        return _get(EPOCH, {'Accept': '*/*'})
    if source == 'openrouter':
        return json.loads(_get(OPENROUTER, {'Authorization': 'Bearer '+key}))
    return json.loads(_get(ARENA.replace('config=agent', 'config='+source[6:]) if source.startswith('arena:') else ARENA))


def _read(fetch, source, parse_it, key):
    try:  # reading and parsing a third party's answer is one step: whatever fails in it, there is no reading from it
        return parse_it(fetch(source, key))
    except AssertionError:
        raise  # a failed check (a test's, or tare's own) is a bug, never an outage
    except Exception:  # the network, or an answer shaped as nobody foresaw
        return None


def refresh_reading(now=None, fetch=fetch, setup=None):
    """The general reading (`_refresh_general`), then the catalogue's boards (`refresh_boards`): neither has a say in
    the other. `fetch(source, key)` returns a source's answer."""
    now = time.time() if now is None else now
    saved = _refresh_general(now, fetch, setup)
    refresh_boards(now, fetch, setup)
    return saved


def _refresh_general(now, fetch, setup):
    """Read the sources at most once a day (after an attempt that read nothing, at most once an hour) and keep the
    reading with the one before it, which the two-reading rule of `klass` needs. A failed, empty or reshaped reading
    keeps the last one."""
    global _reading
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


def _rank(row):
    """The place of a row's effort among EFFORTS; a row with none (a source that does not say) ranks below all."""
    return EFFORTS.index(row['effort']) if row.get('effort') in EFFORTS else -1


def _anchors(saved, current=None):
    """[(score, class, name)], one per known line in the reading: its newest version, at the highest effort listed
    for that version, with the line's declared class. With `current` ({line: version}, `_current`) a line counts only
    if the reading holds that very version: a board that lacks it is behind the others. A line whose newest version the reading names only as a variant
    ("Claude Opus 5.5 (Max, Default Fallback)") gives no anchor: the clean row of an older version is another, weaker
    model, and as an anchor it would lower its class's floor."""
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict)]
    found, newest = {}, {}
    for r in rows:
        if r.get('line') in LINES and _version(r.get('version')):
            name, version = r['line'], _version(r['version'])
            # A row with no effort is an anchor only on a task-type board (`current` given): the general reading's
            # anchors stay the rows that name their effort.
            if (r.get('effort') in EFFORTS or current is not None and r.get('effort') is None) \
                    and _number(r.get('intelligence')) is not None:
                found.setdefault(name, []).append(((version, _rank(r)), r))
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
            if version == newest[name] and version == (current or {}).get(name, version)]


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
        return named(name, r['name'].lower().replace(' ', '-')) and (not vendor or not r['creator'] or house(vendor, r['creator']))
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict) and r.get('line') is None
            and isinstance(r.get('name'), str) and isinstance(r.get('creator'), str)
            and _number(r.get('intelligence')) is not None and same(r)]
    top = [r for r in rows if _rank(r) == max(map(_rank, rows))] if rows else []
    one = len(top) == 1 and len({(r.get('permaslug'), r['creator'], r['name']) for r in rows}) == 1
    return top[0] if one and '(' not in top[0]['name'] else None


MIN_LINES = 8  # a task-type board places models only with this many known lines at their newest version


def _place_ranked(anchors, score):
    """(class, anchor name) on a task-type board, where the known lines are a ranking and not their general labels. A
    line that leads in general may be weak at one type of work, and as its class's weakest anchor it would drop that
    class's floor (the general leader, eighth on a competition-mathematics board, put 8 of 11 lines at frontier). The
    lines split 3 : 6 : 3 in general, so on a board frontier is the top quarter of its lines, middling the top three
    quarters, the rest cheap. None with fewer than MIN_LINES lines: a quarter of five lines is one line."""
    ranked = sorted(anchors, key=lambda a: -a[0])
    if len(ranked) < MIN_LINES:
        return None
    bars = [(name, ranked[math.ceil(len(ranked)*share) - 1]) for name, share in (('frontier', .25), ('middling', .75))]
    return next(((name, bar[2]) for name, bar in bars if score >= bar[0]), ('cheap', bars[1][1][2]))


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


def _on_board(board, deployment, bare=False):
    """The one row of a board ({'rows': [...]}) for a route's model, or None. A model of a line is read at the route's
    own effort when the board lists it, else at the highest it lists; a model with no line goes through `_match`.
    `bare` is a task-type board: a row that names no effort counts too, and the model is always read at the highest
    effort listed, as the lines that rank it are. Read at the route's own effort where listed and at the highest where
    not, a route at medium (not listed) outranked the same model's route at high (listed), and a line at its default
    effort fell below lines it leads at theirs."""
    model = _slug(deployment)
    known = model and line(model)
    if not known:
        return _match(board, deployment)
    version, effort = _version_effort(deployment, model)
    listed = [r for r in board['rows'] if isinstance(r, dict) and r.get('line') == known['line']
              and r.get('version') == '.'.join(map(str, version))
              and (r.get('effort') in EFFORTS or bare and r.get('effort') is None)]
    top = max(map(_rank, listed), default=-1)
    at = [r for r in listed if r['effort'] == effort and not bare] or [r for r in listed if _rank(r) == top]
    return at[0] if len(at) == 1 else None


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
    row = _on_board(board, deployment)
    if row is None and not line(_slug(deployment) or '') and (named := _match(saved, deployment)):
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
    'audited', 'accepted', 'applied', per class a unit needed ('needs', class, 'ran' | 'passed'), and per type of work
    a unit named ('kind', id, 'ran' | 'passed' | 'audited' | 'accepted'; a unit of several types counts in each)}}.
    Raw counts, shown and never steering: harder work already goes to stronger models, so they do not compare two."""
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
                counts = dict(ran=1, passed=int(passed), audited=int('audit' in row),
                              accepted=int((row.get('audit') or {}).get('verdict') == 'ACCEPT'))
                tally.update(counts)
                for kind in row.get('kinds') or [row.get('kind')]:
                    if task_type(kind):
                        tally.update({('kind', kind, name): count for name, count in counts.items()})
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
            text += f"; needed {shown(needed)}: {tally['needs', needed, 'passed']} of {tally['needs', needed, 'ran']} passed"
    for kind in TYPES:  # the only measure a type no board covers has (games, art, audio)
        if ran := tally['kind', kind['id'], 'ran']:
            text += (f"; {kind['id']} work: {tally['kind', kind['id'], 'passed']} of {ran} passed, "
                     f"{tally['kind', kind['id'], 'accepted']} of {tally['kind', kind['id'], 'audited']} audits accepted")
    return text


def judged(deployment, kind=None, kept=None):
    """{'class', 'source'[, 'score']} a route is judged by for a unit: its class for the task type `kind` (an id of TYPES) when a
    board places it ('source' is that board's name; `kept`, `boards()`, is read when not given), else its general
    class ('source': 'general'), exactly as with no `kind`. A model with no class (no line and no row of the reading
    to place it by) counts as cheap: nothing known places it higher."""
    found = kind and task_type(kind) and type_class(deployment, task_type(kind), boards() if kept is None else kept)
    if found:  # type_class falls back to the general class itself
        return {'class': found['class'] or 'cheap', 'source': 'general' if found['general'] else found['source'],
                # the board's score ranks a critical unit; its row's effort is what the route is run at (`judged_effort`)
                **({} if found['general'] else {'score': found['score'], 'effort': found.get('effort')})}
    return {'class': (klass(deployment) or {}).get('class') or 'cheap', 'source': 'general'}


# The most a board's effort raises a route to. Boards read models at max, which is too dear to run every unit of a type
# at (owner, 2026-11-16: "max is too much, between high and xhigh"): high for an ordinary unit, xhigh for a critical
# one. A plan's stated `effort` is the user's and planner's own choice and is not capped.
CEILING = {False: 'high', True: 'xhigh'}


def capped(effort, critical=False):
    """A board's effort, no higher than CEILING for the unit; None when the board names none."""
    return min(effort, CEILING[bool(critical)], key=EFFORTS.index) if effort in EFFORTS else None


def judged_effort(deployment, used, critical=False):
    """The effort to run a route at because the board that judged it (`used`, from `judged`) read its model at a
    higher one: the board's, capped (`capped`), or the highest level the route supports below that
    (`supported_level`), when it is higher than the route's own (unknown counts as lowest), else None: never lower."""
    from .execution_team import reasoning
    from .model_choices import supported_level
    model = _slug(deployment)
    own = _version_effort(deployment, model)[1] if model and line(model) else reasoning(deployment)
    level = supported_level(deployment, capped(used.get('effort'), critical))
    return level if level and EFFORTS.index(level) > (EFFORTS.index(own) if own in EFFORTS else -1) else None


def best_first(deployment, kind=None, kept=None):
    """A sort key for a critical unit: the best route first, by the class `judged` gives it, then (with a `kind`) a route
    a board placed before one judged generally, boards in the catalogue's order, then the higher score on that board.
    Ties keep the caller's order, so cost only breaks them. {'class', 'source', 'score'} -> a tuple to sort by."""
    used = judged(deployment, kind, kept)
    names = [s['name'] for s in task_type(kind)['sources']] if kind and task_type(kind) else []
    return (-ORDER.index(used['class']), names.index(used['source']) if used['source'] in names else len(names),
            -used.get('score', 0))


def short(deployment, needs, kind=None, kept=None):
    """How many classes a route's class (`judged`) falls short of the class a unit needs: 0 when it reaches it."""
    return max(0, ORDER.index(needs) - ORDER.index(judged(deployment, kind, kept)['class']))


def reaches(deployment, needs, kind=None, kept=None):
    return short(deployment, needs, kind, kept) == 0


def describe(result):
    """One line of `tare models`: the class, where it came from and the numbers."""
    if not result:
        return 'none (no model line, and no benchmark row to place it by)'
    if result['from'] == 'anchors':
        return (f"{shown(result['class'])}: {'falls below' if result['class'] == 'cheap' else 'reaches'} {result['anchor']} "
                f"on {result['source']}, {result['as_of']}") + _of_base(result)
    text = shown(result['class']) + (' (reading' if result['from'] == 'reading' else ' (line default')
    if result['from'] == 'line' and 'older_than' in result:
        text += f", one class lower: an older version than {result['older_than']}"
    if 'source' in result:
        text += f"; {result['source']} {result['as_of']}: intelligence {result['intelligence']:g}"
        if result['cost_per_task_usd'] is not None:
            text += f", US${result['cost_per_task_usd']:.2f} per task"
        text += '' if result['from'] == 'reading' else '; not confirmed by two readings'
    return text + ')'


# Task types: a catalogue of kinds of work, the boards that say how models do at each, and a model's class per type.
# A model that is the best in general may know nothing of CAD or 3D, so the class is read per type (owner, 2026-11-16).
# Adding a type, or a source of a reader below, is an edit of task_types.json and no code.
TYPES = json.loads(Path(__file__).with_name('task_types.json').read_text(encoding='utf-8'))['types']
READERS = ('arena', 'epoch', 'page', 'script', 'json')


def task_type(type_id):
    return next((t for t in TYPES if t['id'] == type_id), None)


def boards_path():
    return reading_path().with_name('task-boards.json')


def source_key(source):
    return f"{source['reader']}:{source['board']}:{source['column']}"


def boards():
    """{source key: {'read_at', 'as_of', 'rows', maybe 'tried_at'}} as last saved; {} when there is none."""
    value = _local_json(boards_path())
    found = value.get('boards') if isinstance(value, dict) and value.get('schema') == BOARDS_SCHEMA else None
    return {k: v for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}


def parse_epoch(raw, source):
    """(rows, as_of) of one CSV member of Epoch's archive, or None when it holds no scored row. Only the member the
    catalogue names is opened, in memory (never extracted to disk), and only within CAP; a missing member raises. A row's
    'Model version' is '<model>_<effort>' ('unknown' for none); its organisation is the creator, 'Google DeepMind' for
    Google's line. `as_of` is the newest release date on the board: Epoch states no date for a board itself."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(source['board'])
        if info.file_size > CAP:
            raise ValueError('archive member over 8 MiB')
        text = archive.read(info).decode('utf-8-sig')
    items, others, dates = [], [], []
    for r in csv.DictReader(io.StringIO(text)):
        slug, sep, effort = (r.get('Model version') or '').rpartition('_')
        slug, effort = (slug, effort) if sep else (effort, '')
        if sep and effort not in EFFORTS and effort != 'unknown':
            slug, effort = slug+'_'+effort, None  # a mode with no effort of ours (none, minimal): a variant, not the model
        effort = effort if effort in EFFORTS else None
        try:
            score = float(r.get(source['column']))
        except (TypeError, ValueError):
            score = None
        org, known = r.get('Organization'), line(slug)
        dates.append(r.get('Release date') or '')
        if known and isinstance(org, str) and org.casefold().startswith(known['vendor'].casefold()):
            items.append(((known['line'], '.'.join(map(str, parse(slug)[2])), effort), score, None, None, None))
        else:
            others.append((org, slug, None, score, None, None, None))
    rows = _rows(items) + _others(others)
    return (rows, max(filter(None, dates), default=None)) if rows else None


class _Page(HTMLParser):
    """The tables of a page as rows of cells (a cell is the list of its text pieces, in order) and its scripts by id."""
    def __init__(self):
        super().__init__()
        self.tables, self.scripts, self._cell, self._script = [], {}, None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables.append([])
        elif tag == 'tr' and self.tables:
            self.tables[-1].append([])
        elif tag in ('td', 'th') and self.tables and self.tables[-1]:
            self._cell = []
        elif tag == 'script' and dict(attrs).get('id'):
            self._script = dict(attrs)['id']
            self.scripts[self._script] = ''

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            self.tables[-1][-1].append(self._cell)
            self._cell = None
        elif tag == 'script':
            self._script = None

    def handle_data(self, data):
        if self._cell is not None and data.strip():
            self._cell.append(' '.join(data.split()))  # also folds a no-break space
        elif self._script:
            self.scripts[self._script] += data


def _page(raw):
    page = _Page()
    page.feed(raw.decode('utf-8', 'replace'))
    return page


def _score(value):
    """A page's score as a number: 0.754, '0.754', '75.0%'; None for anything else ('coming soon', an estimate '≈0.7')."""
    if isinstance(value, str):
        found = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*%?\s*', value)
        value = float(found[1]) if found else None
    return _number(value)


def _model_rows(found):
    """Rows from [(display name, organisation or None, score)] like any other board's: a known line's row by
    `normalise`, any other model's (creator '' when the page names none) by `_others`."""
    items, others = [], []
    for name, org, score in found:
        key = normalise(name, org, bare=True)
        if key:
            items.append((key, score, None, None, None))
        else:
            others.append((org or '', name, None, score, None, None, None))
    return _rows(items) + _others(others)


def parse_page(raw, source):
    """(rows, None) of the first table of an HTML page whose header row holds the source's `model` and `column`
    headers (and its `organisation` one when the catalogue names it): a model's name is the first text of its cell
    (a row's name may carry a note after it), a sort arrow after a header is ignored, and a row with no number
    ('coming soon', an estimate) is no row. None when no table has the columns or holds a scored row: a failed
    reading, which keeps the saved board. The page states no date, so there is no `as_of`."""
    for table in _page(raw).tables:
        for i, head in enumerate(table):
            heads = [re.sub('[▲▼]', '', ''.join(cell)).strip().casefold() for cell in head]
            at = [heads.index(source[k].casefold()) if source[k].casefold() in heads else None
                  for k in ('model', 'column', 'organisation') if source.get(k)]
            if None in at:
                continue
            found = [(r[at[0]][0], r[at[2]][0] if len(at) > 2 else None, _score(r[at[1]][0]))
                     for r in table[i+1:] if len(r) > max(at) and all(r[j] for j in at)]
            rows = _model_rows(found)
            if rows:
                return rows, None
    return None


def parse_script(raw, source):
    """(rows, as_of) of a JSON `<script id=...>` of a page that has no table (a Next.js page's `__NEXT_DATA__`; reader
    'script'), or of a data file that is JSON itself (reader 'json'): the catalogue gives the script's id (`script`,
    for a page), the dotted path to the list of rows (`rows`) and, as dotted paths in a row, the `model`, the score
    (`column`) and optionally the `organisation`; `as_of`, a dotted path to the file's own date, when it states one.
    A path that is not there raises: a reading that failed, which keeps the saved board."""
    def at(value, path):
        for key in path.split('.'):
            value = value[key]
        return value
    data = json.loads(raw if source['reader'] == 'json' else _page(raw).scripts[source['script']])
    org = source.get('organisation')
    rows = _model_rows([(at(r, source['model']), at(r, org) if org else None, _score(at(r, source['column'])))
                        for r in at(data, source['rows'])])
    date = at(data, source['as_of']) if source.get('as_of') else None
    return (rows, date if isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) else None) if rows else None


def _current(kept):
    """{line: its newest version on any saved board}. The anchors of a board must be these versions: a board that stopped
    at an older one (an aged source) would place a new model against models it is no longer measured beside."""
    best = {}
    for board in kept.values():
        for r in board.get('rows') or () if isinstance(board, dict) else ():
            if isinstance(r, dict) and r.get('line') in LINES and (v := _version(r.get('version'))):
                best[r['line']] = max(best.get(r['line'], v), v)
    return best


def _due(entry, now, wait=3600):
    """True when a saved board is older than a day and no try was made in the last `wait` seconds (an hour; a day for
    a public page: a page that changed its layout must not be asked every hour by every machine)."""
    read, tried = (_when((entry or {}).get(k), now) for k in ('read_at', 'tried_at'))
    return not (read is not None and now - read < 86400 or tried is not None and now - tried < wait)


def refresh_boards(now, fetch, setup=None):
    """Read the catalogue's boards that are due: Epoch's archive once for all its boards, each LMArena config once. A
    board that fails keeps its saved rows and never stops another; a machine that declined API use reads nothing."""
    from .environment import read
    from .relay_io import write_json_atomic
    setup = read() if setup is None else setup
    if 'error' in setup or (setup.get('api_providers') or {}).get('use') is False:
        return
    saved = boards()
    sources = {source_key(s): s for t in TYPES for s in t['sources']}
    due = {k: s for k, s in sources.items()
           if _due(saved.get(k), now, 86400 if s['reader'] in ('page', 'script', 'json') else 3600)}
    if not due:
        return
    kept, archive = {k: v for k, v in saved.items() if k in sources}, []
    for key, s in due.items():
        if s['reader'] == 'epoch':
            if not archive:  # one GET for every Epoch board; a failed one is not asked again in this pass
                archive.append(_read(fetch, 'epoch', lambda raw: raw, None))
            found = _read(lambda source, k: archive[0], 'epoch', lambda raw: parse_epoch(raw, s), None)
        elif s['reader'] == 'arena':
            found = _read(fetch, 'arena:'+s['board'], lambda body: parse_arena(body, s['column'], s.get('category')), None)
        else:  # a public page: one GET of the URL the catalogue names, nothing else of the site
            read_it = parse_page if s['reader'] == 'page' else parse_script
            found = _read(fetch, 'page:'+s['board'], lambda raw: read_it(raw, s), None)
        if found and found[0]:
            sign = 1 if s['higher_is_better'] else -1  # one direction for _place: a higher number is better
            kept[key] = {'read_at': now, 'as_of': found[1] or datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
                         'rows': [{**r, 'intelligence': sign*r['intelligence']} for r in found[0]]}
        else:
            kept[key] = {**kept.get(key, {}), 'tried_at': now}
    boards_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(boards_path(), {'schema': BOARDS_SCHEMA, 'boards': kept})


def type_class(deployment, kind, kept):
    """A route's class for a task type (`kind`, a row of TYPES; `kept`, `boards()`): from the first source of the type
    whose board places the model, by its rank among the known lines there (`_place_ranked`) ({'class', 'type', 'label',
    'general': False, 'source', 'board', 'kind', 'read_at', 'effort'}); else the general class, marked general
    ({'class' or None, 'type', 'label', 'general': True}). A board without the newest versions of the known lines
    places nobody: an aged source drops out by itself."""
    current = _current(kept)
    for source in kind['sources']:
        board = kept.get(source_key(source))
        rows = board.get('rows') if isinstance(board, dict) else None
        row = isinstance(rows, list) and _on_board(board, deployment, bare=True)
        placed = row and _place_ranked(_anchors(board, current), row['intelligence'])
        if placed:
            relation = measured_as(deployment)[1]
            return {'class': placed[0], 'type': kind['id'], 'label': kind['label'], 'general': False,
                    'source': source['name'], 'board': source['board'], 'kind': source['kind'],
                    'read_at': board.get('read_at'), 'effort': row.get('effort'), 'score': row['intelligence'],
                    **({'base': row['name'], 'relation': relation} if relation and 'name' in row else {})}
    general = klass(deployment)
    return {'class': general and general['class'], 'type': kind['id'], 'label': kind['label'], 'general': True}


def _day(stamp):
    day = datetime.fromtimestamp(stamp, timezone.utc) if type(stamp) in (int, float) else None
    return f'{day.day} {day:%b}' if day else 'never'


def describe_type(found):
    """One phrase for `tare models --type`: 'middling for mathematics (Epoch FrontierMath, read 16 Nov)', or the general
    class and why: 'frontier (general: no board places it for CAD)'."""
    if found['general']:
        return (f"{shown(found['class'])} (general: " if found['class'] else 'none (no general class either: ') \
            + f"no board places it for {found['label']})"
    votes = {'preference': ', votes', 'reported': ', as each lab reported it'}.get(found['kind'], '')
    return f"{shown(found['class'])} for {found['label']} ({found['source']}{votes}, read {_day(found['read_at'])})" + _of_base(found)


def types_report(kept):
    """The catalogue with what each source holds now: [{'id', 'label', 'description', 'sources': [{'name', 'reader',
    'board', 'kind', 'citation', 'read_at', 'as_of', 'anchors', 'known_lines', 'places'}]}]. `anchors` counts the
    known lines whose newest version the board holds; with fewer than MIN_LINES it places no model."""
    report, current = [], _current(kept)
    for kind in TYPES:
        rows = []
        for s in kind['sources']:
            board = kept.get(source_key(s)) or {}
            anchors = _anchors(board, current) if isinstance(board.get('rows'), list) else []
            rows.append({**{k: s[k] for k in ('name', 'reader', 'board', 'kind', 'citation')},
                         'read_at': board.get('read_at'), 'as_of': board.get('as_of'), 'anchors': len(anchors),
                         'known_lines': len(LINES), 'places': len(anchors) >= MIN_LINES})
        report.append({'id': kind['id'], 'label': kind['label'], 'description': kind['description'], 'sources': rows})
    return report


def describe_source(row):
    text = f"{row['name']} ({row['kind']}): " + (f"read {_day(row['read_at'])}, " if row['read_at'] else 'not read yet, ')
    text += f"{row['anchors']} of {row['known_lines']} known lines"
    return text + ('' if row['places'] else f'; fewer than {MIN_LINES}: places no model')
