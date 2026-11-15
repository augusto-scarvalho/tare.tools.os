"""Public benchmark signals of tare's model lines, and the class (frontier, middling, cheap) they give a route.

Read, the first source that answers: OpenRouter's relay of Artificial Analysis's indices (with the OpenRouter key
already stored, never another), then the LMArena agent dataset (keyless). Left out on purpose: Artificial Analysis's
own API (it needs a key of its own that no one has stored) and Epoch AI (a zip). The reading is shown with its source
and date and only places a model in a class; which route runs is decided elsewhere. Rows are matched to a line by
`vendor_models.parse`, never by a table of models: a name that is not exactly one known line, version and effort, or
that two rows claim, is refused.
"""
import json
import math
import re
import time

from .model_choices import EFFORTS
from .provider_prices import _local_json, _when
from .vendor_models import AGY_EFFORT, line, parse

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


def parse_openrouter(body):
    """(rows, as_of, version) of OpenRouter's benchmarks answer, or None when it is not shaped as expected."""
    body = body if isinstance(body, dict) else {}
    data, meta = body.get('data'), body.get('meta') if isinstance(body.get('meta'), dict) else {}
    if not isinstance(data, list) or not isinstance(meta.get('as_of'), str):
        return None
    items = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get('model_permaslug'), str):
            key = normalise(item.get('display_name'), item['model_permaslug'].split('/')[0])
            if key:
                items.append((key, item.get('intelligence_index'), item.get('coding_index'),
                              item.get('agentic_index'), None))
    return _rows(items), meta['as_of'][:10], str(meta.get('version'))


def parse_arena(body):
    """(rows, as_of, version) of the Hugging Face rows answer; the score stands in `intelligence`."""
    data = body.get('rows') if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    items, dates = [], []
    for item in data:
        row = item.get('row') if isinstance(item, dict) else None
        if isinstance(row, dict):
            dates.append(str(row.get('leaderboard_publish_date')))
            key = normalise(row.get('model_name'), row.get('organization'))
            if key:
                items.append((key, row.get('score'), None, None, row.get('cost_per_task_p50')))
    return (_rows(items), max(dates), None) if dates else None


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
        try:  # reading and parsing a third party's answer is one step: whatever fails in it, the next source is tried
            found = parse_it(fetch(source, key))
        except AssertionError:
            raise  # a failed check (a test's, or tare's own) is a bug, never an outage
        except Exception:  # the network, or an answer shaped as nobody foresaw: no reading from this source
            continue
        if found and found[0]:
            rows, as_of, version = found
            value = {'schema': SCHEMA, 'source': source, 'citation': CITATIONS[source], 'as_of': as_of,
                     'version': version, 'read_at': now, 'rows': rows}
            if saved:
                value['previous'] = {k: v for k, v in saved.items() if k != 'previous'}
            write_json_atomic(reading_path(), value)
            _reading = (None, None)
            return value
    return saved


def band(source, score):
    return next((name for cut, name in CUTS[source] if score >= cut), 'cheap')


def _row(saved, key):
    rows = [r for r in (saved or {}).get('rows', ()) if isinstance(r, dict)
            and (r.get('line'), r.get('version'), r.get('effort')) == key]
    return rows[0] if len(rows) == 1 and _number(rows[0].get('intelligence')) is not None else None


def klass(deployment, saved=None):
    """{'class', 'from': 'reading' | 'line', plus 'source', 'as_of', 'intelligence', 'cost_per_task_usd' when the
    reading has the route's model at its default effort} for a deployment, or None for a model with no line. The
    class is the reading's (absolute cuts of CUTS) only when the two latest readings of the same source and index
    version put the model in the same class; otherwise the line's own, which no reading overrides on one word."""
    model = deployment['native_profile']['model']
    known = line(model)
    if not known:
        return None
    from .execution_team import reasoning
    suffix = AGY_EFFORT.search(model)
    effort = suffix[1] if suffix else reasoning(deployment)
    key = (known['line'], '.'.join(map(str, parse(AGY_EFFORT.sub('', model))[2])), effort)
    saved = reading() if saved is None else saved
    now = _row(saved, key)
    if not now:
        return {'class': known['class'], 'from': 'line'}
    numbers = {'source': saved['source'], 'as_of': saved.get('as_of'), 'intelligence': now['intelligence'],
               'cost_per_task_usd': now.get('cost_per_task_usd')}
    before = saved.get('previous')
    before = _row(before, key) if isinstance(before, dict) and \
        (before.get('source'), before.get('version')) == (saved['source'], saved.get('version')) else None
    chosen = band(saved['source'], now['intelligence'])
    if before and band(saved['source'], before['intelligence']) == chosen:
        return {'class': chosen, 'from': 'reading', **numbers}
    return {'class': known['class'], 'from': 'line', **numbers}


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
    no line (a local, reseller or Bedrock id) counts as cheap: nothing known places it higher."""
    found = klass(deployment) or {}
    return max(0, ORDER.index(needs) - ORDER.index(found.get('class') or 'cheap'))


def reaches(deployment, needs):
    return short(deployment, needs) == 0


def describe(result):
    """One line of `tare models`: the class, where it came from and the numbers."""
    if not result:
        return 'none (no model line)'
    text = result['class'] + (' (reading' if result['from'] == 'reading' else ' (line default')
    if 'source' in result:
        text += f"; {result['source']} {result['as_of']}: intelligence {result['intelligence']:g}"
        if result['cost_per_task_usd'] is not None:
            text += f", US${result['cost_per_task_usd']:.2f} per task"
        text += '' if result['from'] == 'reading' else '; not confirmed by two readings'
    return text + ')'
