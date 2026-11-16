"""What tare itself spent on each API account, kept across sessions, and the balance the operator declared for it.
For OpenCode's subscription, which charges nothing per call, it keeps the plan allowance each reply said it used.

Most providers publish no balance for an ordinary API key. The operator states the balance once (tare quota --set
ACCOUNT AMOUNT) and tare subtracts the dollars its own sessions record from then on. It counts tare's use only: use
of the same key elsewhere needs a new statement. A credit that renews each month and does not carry over (Anthropic's
Max plan API credit) is stated with --renews DAY [GRANT]: from each renewal the balance is the grant again.
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
    return posted


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
        if name and usd > 0:
            # Valued at a models.dev reference price, not a tariff: it counts down all the same, and the row says
            # which entry priced it and the day that price was read.
            tariff = cost.get('tariff')
            rows.append(json.dumps({**row, 'account': name, 'usd': usd, **({
                'reference': True, 'price_source': tariff['source'], 'price_read': tariff.get('effective_at')}
                if is_reference(tariff) else {})}))
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


def spent(name, since, field='usd', reference=False):
    """Dollars tare recorded for this account from `since` on (field 'allowance': of OpenCode's plan allowance;
    `reference`: only those valued at a models.dev reference price)."""
    total = 0.0
    try:
        with open(folder()/'provider-spend.jsonl', encoding='utf-8') as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                    if row['account'] == name and row['at'] >= since and (not reference or row.get('reference')):
                        total += float(row[field])
                except (ValueError, KeyError, TypeError):
                    continue  # a torn line is not a charge
    except OSError:
        pass
    return round(total, 6)


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


def declare(name, usd, now=None, renews=None, window=None):
    """State the balance of an account as of now; the spend counted against it starts here. `renews`: (day of the
    month, dollars each cycle brings) for a credit that renews and does not carry over. `window`: (start, end, dollars
    granted) for a credit read from the Console, which lapses at `end`; one entry has `renews` or `window`, not both."""
    if name not in DECLARABLE:
        raise ValueError('unknown account ' + repr(name) + '; one of ' + ', '.join(sorted(DECLARABLE)))
    if not 0 <= usd < 1e9:
        raise ValueError('the balance is a dollar amount of zero or more')
    entry = {'usd': float(usd), 'at': time.time() if now is None else now}
    if renews:
        entry['renews'] = {'day': renews[0], 'usd': float(renews[1])}
        if not _renews(entry['renews']):
            raise ValueError('--renews takes a day of the month (1-31) and a grant above zero')
    if window:
        if renews:
            raise ValueError('a credit has renews or a window, not both')
        entry['window'] = dict(zip(('start', 'end', 'usd'), map(float, window)))
        if not _window(entry['window']):
            raise ValueError('a window is a start before its end and a grant above zero')
    from .relay_io import write_json_atomic
    balances = {**declared(), name: entry}
    folder().mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder()/'provider-balance.json', balances)
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
        extra = {'renews_at': window['end'], 'cycle_start': window['start'], 'grant': total, 'console': True}
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
