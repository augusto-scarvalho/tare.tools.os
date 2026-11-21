"""What tare itself spent on each API account, kept across sessions, and the balance the operator declared for it.
For OpenCode's subscription, which charges nothing per call, it keeps the plan allowance each reply said it used.

Most providers publish no balance for an ordinary API key. The operator states the balance once (tare quota --set
ACCOUNT AMOUNT) and tare subtracts the dollars its own sessions record from then on. It counts tare's use only: use
of the same key elsewhere needs a new statement. A credit that renews each month and does not carry over (Anthropic's
Max plan API credit) is stated with --renews DAY [GRANT]: from each renewal the balance is the grant again. A credit
that lapses on a day and never renews (a cloud's promotional credit) is stated with --expires DATE.
"""
import calendar
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# The account an API credential is billed to. A subscription or a local model has none.
ACCOUNTS = {'OPENAI_API_KEY': 'openai-api', 'ANTHROPIC_API_KEY': 'anthropic-api', 'GEMINI_API_KEY': 'gemini-api',
            'AWS_BEARER_TOKEN_BEDROCK': 'bedrock', 'OPENROUTER_API_KEY': 'openrouter'}
DECLARABLE = frozenset({'openai-api', 'anthropic-api', 'gemini-api', 'bedrock', 'opencode-api'})
# The usage.opencode.billing_source values that name a plan (not 'balance', 'byok' or 'free').
PLANS = ('go', 'go-plus')
# The account of every local model's energy rows (watt-hours, never dollars spent).
LOCAL = 'local'
# The subscriptions tare measures (relay/plan_consumption.py), each with the vendor API whose reference price values a call.
EQUIVALENT_API = {'claude': 'https://api.anthropic.com', 'codex': 'https://api.openai.com',
                  'agy': 'https://generativelanguage.googleapis.com'}


def folder():
    from .native_installation import storage_root
    return storage_root()/'serve'


def account(deployment):
    """The API account this route is billed to, or None."""
    from .opencode import is_route, subscription
    if is_route(deployment):
        return None if subscription(deployment) else 'opencode-api'
    return ACCOUNTS.get(deployment.get('native_credential_env'))


def no_credit(deployment, failed, observations):
    """Whether an API account refused this call for lack of credit: the kernel's model_failed event `failed` is an
    HTTP 4xx whose own reason says so. HTTP 402 is that by definition (OpenRouter's "Insufficient credits", Anthropic's
    billing_error); OpenAI answers 429 with the type insufficient_quota; Anthropic also answers 400 with "Your credit
    balance is too low". A passing rate limit (429 with any other type) and every other refusal are not."""
    if not account(deployment) or not failed:
        return False
    code = str(failed.get('code') or failed.get('reason'))  # a model call says code; a context summary says reason
    if not code.startswith('HTTP_4'):
        return False
    reason = next((call['provider_error'] for row in reversed(observations) if row.get('call_id') == failed.get('call_id')
                   for call in reversed(row.get('transport_calls') or []) if isinstance(call.get('provider_error'), dict)), {})
    return (code == 'HTTP_402' or reason.get('type') in ('insufficient_quota', 'billing_error')
            or 'credit balance is too low' in str(reason.get('message') or '').lower())


def post(deployment, usage, session, now=None, since=None):
    """Add to the ledger the dollars of this session's calls that were not added yet (a session is resumed and its
    usage report is cumulative, so the session keeps how many calls it already posted)."""
    calls = usage.get('calls') or []
    marker = Path(session)/'spend-posted.json'
    try:
        done = int(json.loads(marker.read_text(encoding='utf-8'))['calls'])
    except (OSError, ValueError, KeyError, TypeError):
        done = 0
    posted = record(deployment, calls[done:], now, since=since)
    if len(calls) != done:
        marker.write_text(json.dumps({'calls': len(calls)}), encoding='utf-8')
        if len(calls) > done and type(since) in (int, float):
            try:  # the energy is only shown; whatever goes wrong here never touches the session
                record_energy(deployment, since, time.time() if now is None else now)
            except Exception:
                pass
    return posted


def node_watts(deployment, began, ended):
    """(average watts, seconds they cover) the model's node measured for its GPU between two times, or None (an old
    node, no nvidia-smi, unreachable, no reading in the span). The node's record is finite and a restart empties it:
    the average is of the readings it still holds, which may start after `began`, and then covers only from its first
    reading on. One request, 2 s."""
    from urllib.request import urlopen
    base = deployment['native_profile']['endpoint'].rstrip('/').rsplit('/v1', 1)[0]
    try:
        with urlopen(f'{base}/v1/gpu/power?since={began}&until={ended}', timeout=2) as reply:
            found = json.load(reply)
        watts = float(found['watts'])
        # A node from before it said where its readings start kept one hour of them.
        first = float(found['from']) if 'from' in found else max(began, ended - 3600)
    except Exception:  # any failure is no reading, never an error in the call's result
        return None
    if not 0 < watts < 10000 or not first <= ended:  # also drops nan and the null of "no reading"
        return None
    # The first reading of a span the record covers comes within a sampling period of its start (5 s; 30 allows for
    # the two clocks): then the whole span counts. Later than that, the start is not measured.
    return watts, ended - began if first - began <= 30 else ended - first


def record_energy(deployment, began, ended):
    """Add the energy a local route's GPU drew between two times to the ledger, as an 'energy_wh' row for the
    account 'local' (watt-hours, not dollars: the price is read when it is shown). Never money spent: it has no 'usd',
    so spent(), a declared balance and a spending cap do not see it. The watts are the node's measurement; failing
    that, the ones `tare setup --local-watts` declared; failing both, nothing. When the node's readings cover only
    the end of the span (a session longer than its record, or a node restarted meanwhile), only that part is counted
    and the row says how many seconds were not ('unmeasured_seconds'): the average of the end is never stretched over
    the start (an hour at 50 W then an hour at 300 W is 350 Wh, not 600). Measured only once the user set a price
    or watts: until then no local endpoint gets a request it did not ask for (a loopback server that is not a tare
    node would answer 404, or cost its timeout, after every invocation). Rows added."""
    from .challenger import local
    seconds = ended - began
    if not (settings := local_energy()) or not local(deployment) or not 0 < seconds < 7 * 86400:
        return 0
    measured = node_watts(deployment, began, ended)
    watts, covered, source = (*measured, 'measured') if measured else (settings.get('watts'), seconds, 'declared')
    if watts is None:
        return 0
    folder().mkdir(parents=True, exist_ok=True)
    with open(folder()/'provider-spend.jsonl', 'a', encoding='utf-8') as ledger:
        ledger.write(json.dumps({'at': ended, 'account': LOCAL, 'deployment': deployment.get('id'),
                                 'model': str(deployment['native_profile'].get('model') or '').lower(),
                                 'energy_wh': watts*covered/3600, 'watts': watts, 'seconds': covered,
                                 **({'unmeasured_seconds': seconds - covered} if covered < seconds else {}),
                                 'source': source}) + '\n')
    return 1


def local_energy():
    """What `tare setup` holds for local energy: {'price_kwh': US$ per kWh, 'watts': declared load}, each when set."""
    from .environment import read
    section = read().get('local_energy')
    return {k: v for k, v in section.items() if k in ('price_kwh', 'watts') and type(v) in (int, float) and v > 0
            } if isinstance(section, dict) else {}


def energy_report(now, days=7):
    """The local account's energy of the last `days` days for `tare quota`, or None without a row: kWh (and how much
    of it at declared watts), the hours of sessions no reading covered (not in the kWh) and, when a price is set, the
    cost at that price."""
    since = now - days*86400
    measured, declared = (spent(LOCAL, since, 'energy_wh', source=s) / 1000 for s in ('measured', 'declared'))
    if not measured + declared:
        return None
    price = local_energy().get('price_kwh')
    kwh = measured + declared
    return {'days': days, 'kwh': round(kwh, 4), 'declared_kwh': round(declared, 4),
            'unmeasured_hours': round(spent(LOCAL, since, 'unmeasured_seconds') / 3600, 2), 'price_kwh': price,
            'usd': round(kwh*price, 4) if price else None, 'scope': 'GPU only'}


def equivalent(deployment, call):
    """What a call on a subscription would have cost on the model's own vendor API: its tokens at that model's
    models.dev reference price (cache reads and writes at their own rates), else 0 (no price, or no token counts)."""
    from compute_plane.native_usage import UsageTariff, price_usage
    from .provider_prices import reference
    from .turn_engine import usage_account
    profile = deployment.get('native_profile') or {}
    try:
        tariff = reference({**profile, 'endpoint': EQUIVALENT_API[usage_account(deployment)]})
        return float(price_usage(call['normalized'], UsageTariff(**tariff))['known_amount']) if tariff else 0.0
    except (KeyError, TypeError, ValueError):  # a call with no token counts (an older CLI) is unvalued, never guessed
        return 0.0


def record(deployment, calls, now=None, since=None):
    """Add to the ledger the dollars of these calls ({'call_id', 'cost', and 'normalized' for a subscription's}), with
    no marker: the caller knows they are new (a side call with no session of its own, like the heads-up writer's). A
    call on a subscription adds an 'equivalent' row instead (no 'usd': it is never money spent). Rows added."""
    from .opencode import subscription
    from .provider_prices import is_reference
    from .turn_engine import usage_account
    name, rows = account(deployment), []
    plan = 'opencode-sub' if subscription(deployment) else None
    sub = usage_account(deployment)
    sub = sub if sub in EQUIVALENT_API else None
    now = time.time() if now is None else now

    def amount(value):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0
    for call in calls if name or plan or sub else []:
        cost = call.get('cost') or {}
        row = {'at': now, 'deployment': deployment.get('id'), 'call': call.get('call_id')}
        usd = amount(cost.get('known_amount')) if cost.get('currency') == 'USD' else 0.0
        # OpenCode's subscription charges nothing; each reply says how much of the plan's allowance it used.
        allowance = amount((cost.get('opencode') or {}).get('allowance')) if plan else 0.0
        source = (cost.get('opencode') or {}).get('billing_source')
        tokens = {k: v for k, key in (('in', 'input_tokens'), ('out', 'output_tokens'), ('cache_read', 'cache_read_tokens'),
                                      ('cache_write', 'cache_write_tokens'))
                  if type(v := (((call.get('normalized') or {}).get('tokens')) or {}).get(key)) is int}
        if name and usd > 0:
            # Valued at a models.dev reference price, not a tariff: it counts down all the same, and the row says
            # which entry priced it and the day that price was read. 'basis' says how the dollars were known.
            tariff = cost.get('tariff')
            rows.append(json.dumps({**row, 'account': name, 'usd': usd, **({'tokens': tokens} if tokens else {}),
                'basis': 'charged' if cost.get('basis') == 'provider_reported' else 'reference' if is_reference(tariff) else 'tariff',
                **({'reference': True, 'price_source': tariff['source'], 'price_read': tariff.get('effective_at')}
                   if is_reference(tariff) else {})}))
        elif name and (tokens.get('in', 0) > 0 or tokens.get('out', 0) > 0):
            # Tokens were spent and tare could not value them: the row adds no dollars (spent() and remaining() are
            # unchanged) but own_use() counts it, so a balance that counts down too slowly is said to. A call the
            # provider itself charged nothing for (a free model on OpenRouter) is valued, at zero: not one of those.
            free = cost.get('basis') == 'provider_reported' and cost.get('currency') == 'USD'
            rows.append(json.dumps({**row, 'account': name, 'usd': 0, 'tokens': tokens,
                                    **({'basis': 'charged'} if free else {'unpriced': True})}))
        elif plan and (allowance > 0 or source in PLANS):  # a reply inside the plan's limits still names its plan
            rows.append(json.dumps({**row, 'account': plan, 'allowance': allowance, **({'plan': source} if source in PLANS else {})}))
        elif sub and (value := equivalent(deployment, call)) > 0:
            # API-equivalent use of a plan, for relay/plan_consumption.py only: the key is 'equivalent', not 'usd', so
            # spent(), a declared balance and a spending cap (which reads the session journal) never see it.
            model = str(deployment['native_profile'].get('model') or '').lower()
            # 'since': the earliest these calls can have run, as the caller knows it (the start of the invocation
            # that made them). plan_consumption counts a call only in a span that began before it; with no known start
            # it counts in none, which only loosens the bound. A file's time is never used: it is not the call's.
            rows.append(json.dumps({**row, 'account': sub, 'equivalent': value, 'model': model,
                                    **({'since': since} if type(since) in (int, float) else {})}))
    if rows:
        folder().mkdir(parents=True, exist_ok=True)
        with open(folder()/'provider-spend.jsonl', 'a', encoding='utf-8') as ledger:
            ledger.write('\n'.join(rows) + '\n')
    return len(rows)


def record_receipt(deployment, receipt):
    """Add to the ledger one direct call made outside a session (native_generation.generate_text's receipt): a setup
    proof, an explorer's or a researcher's side call. These spent the account's credit and left no row (found
    2026-11-17, setting OpenRouter's own count beside the ledger). What the provider says it charged goes before a
    tariff, as in a session's usage report. Rows added; it never raises: a ledger that misses a call loses no result."""
    try:
        from compute_plane.native_usage import UsageContract, deployment_contract, normalize_usage, reported_cost
        contract = deployment_contract(deployment)
        contract = UsageContract(**contract) if contract else None
        usage = receipt.get('usage')
        return record(deployment, [{'call_id': receipt.get('attempt_id'), 'normalized': normalize_usage(usage, contract),
                                    'cost': reported_cost(usage, contract) or receipt.get('cost')}])
    except Exception:
        return 0


def spent(name, since, field='usd', reference=False, source=None):
    """Dollars tare recorded for this account from `since` on (field 'allowance': of OpenCode's plan allowance, 'energy_wh':
    of the local account's energy; `reference`: only those valued at a models.dev reference price; `source`: only the
    energy rows measured or declared)."""
    total = 0.0
    try:
        with open(folder()/'provider-spend.jsonl', encoding='utf-8') as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                    if row['account'] == name and row['at'] >= since and (not reference or row.get('reference')) \
                            and (source is None or row.get('source') == source):
                        total += float(row[field])
                except (ValueError, KeyError, TypeError):
                    continue  # a torn line is not a charge
    except OSError:
        pass
    return round(total, 6)


def own_use(name, now):
    """tare's own use of an API account over the last 7 and 30 days, from one pass over the ledger, or None without a
    row in 30 days: {'7d': {...}, '30d': {...}}, each {'usd', 'calls', 'tokens' (in + out over the rows that kept them,
    else None), 'unpriced' (calls tare could not value), 'basis' (sorted: how the rows with dollars were known; a row
    from before 'basis' was kept counts as 'reference' when it says so, else it names none)}."""
    spans = {'7d': now - 7*86400, '30d': now - 30*86400}
    found = {k: {'usd': 0.0, 'calls': 0, 'tokens': None, 'unpriced': 0, 'basis': set()} for k in spans}
    try:
        with open(folder()/'provider-spend.jsonl', encoding='utf-8') as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                    if row['account'] != name:
                        continue
                    at, usd = float(row['at']), float(row['usd'])
                except (ValueError, KeyError, TypeError):
                    continue  # a torn line is not a call; an energy or allowance row has no dollars
                kept = row.get('tokens') if isinstance(row.get('tokens'), dict) else {}
                counted = [kept[k] for k in ('in', 'out') if type(kept.get(k)) is int]
                basis = row.get('basis') or ('reference' if row.get('reference') else None)
                for span, start in spans.items():
                    if at >= start:
                        use = found[span]
                        use['usd'] += usd
                        use['calls'] += 1
                        use['unpriced'] += bool(row.get('unpriced'))
                        if counted:
                            use['tokens'] = (use['tokens'] or 0) + sum(counted)
                        if usd > 0 and basis:
                            use['basis'].add(basis)
    except OSError:
        pass
    if not found['30d']['calls']:
        return None
    return {k: {**v, 'usd': round(v['usd'], 6), 'basis': sorted(v['basis'])} for k, v in found.items()}


def equivalents(name, since):
    """[(since or None, at, dollars, model)] of tare's own calls on subscription `name` recorded from `since` on, at
    the vendor API's reference price: what the plan's use would have cost, not what was paid."""
    found = []
    try:
        with open(folder()/'provider-spend.jsonl', encoding='utf-8') as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                    if row['account'] == name and row['at'] >= since and row.get('equivalent'):
                        began = row.get('since')
                        found.append((began if type(began) in (int, float) else None, row['at'],
                                      float(row['equivalent']), str(row.get('model') or '')))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return found


def last_plan(name):
    """The plan (billing source) of the last reply the ledger holds for this account, or None."""
    found = None
    try:
        with open(folder()/'provider-spend.jsonl', encoding='utf-8') as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                    if row['account'] == name and row.get('plan') in PLANS:
                        found = row['plan']
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return found


def cycle(day, now):
    """(start, end) epochs, UTC, of the monthly cycle holding `now` when the credit renews on `day` of the month (a
    day past the month's end is its last day): start <= now < end."""
    when = datetime.fromtimestamp(now, timezone.utc)

    def renewal(shift):
        year, month = divmod(when.year*12 + when.month - 1 + shift, 12)
        return datetime(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]), tzinfo=timezone.utc).timestamp()
    return (renewal(0), renewal(1)) if now >= renewal(0) else (renewal(-1), renewal(0))


def _renews(value):
    return (isinstance(value, dict) and type(value.get('day')) is int and 1 <= value['day'] <= 31
            and type(value.get('usd')) in (int, float) and 0 < value['usd'] < 1e9)


def _window(value):
    return (isinstance(value, dict) and all(type(value.get(k)) in (int, float) for k in ('start', 'end', 'usd'))
            and value['start'] < value['end'] and 0 < value['usd'] < 1e9)


def declared():
    """{account: {'usd': balance, 'at': when it was stated, 'renews': {'day', 'usd'} when the credit renews, or
    'window': {'start', 'end', 'usd'} for a credit read from the Console with its exact window}}."""
    try:
        value = json.loads((folder()/'provider-balance.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: {a: b for a, b in v.items() if a not in ('renews', 'window') or (_renews if a == 'renews' else _window)(b)}
            for k, v in value.items()
            if isinstance(v, dict) and type(v.get('usd')) in (int, float) and type(v.get('at')) in (int, float)
            } if isinstance(value, dict) else {}


def declare(name, usd, now=None, renews=None, window=None, expires=None, dry=False):
    """State the balance of an account as of now; the spend counted against it starts here. `renews`: (day of the
    month, dollars each cycle brings) for a credit that renews and does not carry over. `window`: (start, end, dollars
    granted) for a credit read from the Console, which lapses at `end`; one entry has `renews` or `window`, not both.
    `expires`: the epoch a credit stated by hand lapses at, never renewing: a window from now holding the balance.
    `dry`: check it all and write nothing (the /quota panel validates every account before it saves any)."""
    if name not in DECLARABLE:
        raise ValueError('unknown account ' + repr(name) + '; one of ' + ', '.join(sorted(DECLARABLE)))
    if not 0 <= usd < 1e9:
        raise ValueError('the balance is a dollar amount of zero or more')
    entry = {'usd': float(usd), 'at': time.time() if now is None else now}
    if expires is not None:
        if window or not (usd > 0 and entry['at'] < expires):
            raise ValueError('a credit lapses on a day still to come and holds a balance above zero')
        window = (entry['at'], expires, usd)
    if renews:
        entry['renews'] = {'day': renews[0], 'usd': float(renews[1])}
        if not _renews(entry['renews']):
            raise ValueError('a credit renews on a day of the month (1-31) with a grant above zero')
    if window:
        if renews:
            raise ValueError('a credit has renews or a window, not both')
        entry['window'] = dict(zip(('start', 'end', 'usd'), map(float, window)))
        if not _window(entry['window']):
            raise ValueError('a window is a start before its end and a grant above zero')
        if expires is not None:
            entry['window']['stated'] = True  # by the owner, not read from the Console
    if dry:
        return entry
    from .relay_io import write_json_atomic
    balances = {**declared(), name: entry}
    folder().mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder()/'provider-balance.json', balances)
    if usd > 0:
        from .turn_engine import clear_usage_limits
        at = entry['at']
        clear_usage_limits(
            lambda key, item: key == name
            and (type(item.get('recorded_at')) not in (int, float) or at >= item['recorded_at']),
            now=at,
        )
    return entry


def remaining(name, now=None):
    """The declared balance less what tare recorded since, or None when none was declared. A renewing credit is the
    stated balance until its first renewal and the grant again from each renewal on, never what was left before."""
    balance = declared().get(name)
    if balance is None:
        return None
    since, total, extra = balance['at'], balance['usd'], {}
    if window := balance.get('window'):
        if (time.time() if now is None else now) >= window['end']:
            return None  # the credit lapsed; the next Console reading states the new one
        total = window['usd']
        extra = {'renews_at': window['end'], 'cycle_start': window['start'], 'grant': total,
                 'stated' if window.get('stated') else 'console': True}
    elif renews := balance.get('renews'):
        start, end = cycle(renews['day'], time.time() if now is None else now)
        total = renews['usd']
        extra = {'renews_at': end, 'cycle_start': start, 'grant': total}
        if since < start:  # renewed since it was stated
            balance = {**balance, 'usd': total}
            since = start
    reference = spent(name, since, reference=True)
    return {'currency': 'USD', 'total': total, 'declared_at': balance['at'],
            'remaining': round(balance['usd'] - spent(name, since), 6), **extra,
            **({'at_reference_prices': reference} if reference else {})}
