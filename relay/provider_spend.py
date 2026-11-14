"""What tare itself spent on each API account, kept across sessions, and the balance the operator declared for it.
For OpenCode's subscription, which charges nothing per call, it keeps the plan allowance each reply said it used.

Most providers publish no balance for an ordinary API key. The operator states the balance once (tare quota --set
ACCOUNT AMOUNT) and tare subtracts the dollars its own sessions record from then on. It counts tare's use only: use
of the same key elsewhere needs a new statement.
"""
import json
import time
from pathlib import Path

# The account an API credential is billed to. A subscription or a local model has none.
ACCOUNTS = {'OPENAI_API_KEY': 'openai-api', 'ANTHROPIC_API_KEY': 'anthropic-api', 'GEMINI_API_KEY': 'gemini-api',
            'AWS_BEARER_TOKEN_BEDROCK': 'bedrock', 'OPENROUTER_API_KEY': 'openrouter'}
DECLARABLE = frozenset({'openai-api', 'anthropic-api', 'gemini-api', 'bedrock', 'opencode-api'})


def folder():
    from .native_installation import storage_root
    return storage_root()/'serve'


def account(deployment):
    """The API account this route is billed to, or None."""
    from .opencode import is_route, subscription
    if is_route(deployment):
        return None if subscription(deployment) else 'opencode-api'
    return ACCOUNTS.get(deployment.get('native_credential_env'))


def post(deployment, usage, session, now=None):
    """Add to the ledger the dollars of this session's calls that were not added yet (a session is resumed and its
    usage report is cumulative, so the session keeps how many calls it already posted)."""
    calls = usage.get('calls') or []
    marker = Path(session)/'spend-posted.json'
    try:
        done = int(json.loads(marker.read_text(encoding='utf-8'))['calls'])
    except (OSError, ValueError, KeyError, TypeError):
        done = 0
    posted = record(deployment, calls[done:], now)
    if len(calls) != done:
        marker.write_text(json.dumps({'calls': len(calls)}), encoding='utf-8')
    return posted


def record(deployment, calls, now=None):
    """Add to the ledger the dollars of these calls ({'call_id', 'cost'}), with no marker: the caller knows they are
    new (a side call with no session of its own, like the heads-up writer's). The number of rows added."""
    from .opencode import subscription
    from .provider_prices import is_reference
    name, rows = account(deployment), []
    plan = 'opencode-sub' if subscription(deployment) else None
    now = time.time() if now is None else now

    def amount(value):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0
    for call in calls if name or plan else []:
        cost = call.get('cost') or {}
        row = {'at': now, 'deployment': deployment.get('id'), 'call': call.get('call_id')}
        usd = amount(cost.get('known_amount')) if cost.get('currency') == 'USD' else 0.0
        # OpenCode's subscription charges nothing; each reply says how much of the plan's allowance it used.
        allowance = amount((cost.get('opencode') or {}).get('allowance')) if plan else 0.0
        if name and usd > 0:
            # Valued at a models.dev reference price, not a tariff: it counts down all the same, and the row says
            # which entry priced it and the day that price was read.
            tariff = cost.get('tariff')
            rows.append(json.dumps({**row, 'account': name, 'usd': usd, **({
                'reference': True, 'price_source': tariff['source'], 'price_read': tariff.get('effective_at')}
                if is_reference(tariff) else {})}))
        elif allowance > 0:
            rows.append(json.dumps({**row, 'account': plan, 'allowance': allowance}))
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


def declared():
    """{account: {'usd': balance, 'at': when it was stated}}."""
    try:
        value = json.loads((folder()/'provider-balance.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, dict) and type(v.get('usd')) in (int, float)
            and type(v.get('at')) in (int, float)} if isinstance(value, dict) else {}


def declare(name, usd, now=None):
    """State the balance of an account as of now; the spend counted against it starts here."""
    if name not in DECLARABLE:
        raise ValueError('unknown account ' + repr(name) + '; one of ' + ', '.join(sorted(DECLARABLE)))
    if not 0 <= usd < 1e9:
        raise ValueError('the balance is a dollar amount of zero or more')
    from .relay_io import write_json_atomic
    balances = {**declared(), name: {'usd': float(usd), 'at': time.time() if now is None else now}}
    folder().mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder()/'provider-balance.json', balances)
    return balances[name]


def remaining(name):
    """The declared balance less what tare recorded since, or None when none was declared."""
    balance = declared().get(name)
    if balance is None:
        return None
    reference = spent(name, balance['at'], reference=True)
    return {'currency': 'USD', 'total': balance['usd'], 'declared_at': balance['at'],
            'remaining': round(balance['usd'] - spent(name, balance['at']), 6),
            **({'at_reference_prices': reference} if reference else {})}
