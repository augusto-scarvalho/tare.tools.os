"""The Anthropic monthly API credit, read from the Console's Billing page (opt-in; tare quota --sync).

Anthropic publishes no API for the balance or expiry of a Max/Ultra plan's API credit; only the Console's Billing page
shows them. One non-interactive Claude Code call (Haiku, read-only browser tools, the Claude in Chrome extension in the
operator's own signed-in browser) returns that page's text; only the credits section is parsed and only the stated
numbers are saved (relay/provider_spend.py keeps them, with the credit's exact window).
"""
import json
import re
import subprocess
import time
from datetime import datetime, timezone

ACCOUNT = 'anthropic-api'
BILLING = 'https://platform.claude.com/settings/billing'
EVERY = 12 * 3600  # a reading opens a tab in the browser and spends a model call: not at each demand
AFTER_FAILURE = 3600  # a failed one (the browser was closed) is tried again at a later start, not half a day on
TOOLS = ','.join('mcp__claude-in-chrome__' + name for name in
                 ('tabs_context_mcp', 'tabs_create_mcp', 'navigate', 'get_page_text', 'tabs_close_mcp'))
PROMPT = f"""Read the Claude Console's Billing page. Do not click anything and do not type anything.
1. Call tabs_context_mcp with createIfEmpty and use the tab it gives you; create no other tab.
2. Navigate that tab to {BILLING} and read the page text with get_page_text. Read again (at most 5 times in all) while
   the text lacks the line "Promotional credits" or says Loading.
3. Close every tab of your own group.
4. Reply with only the page text from the line "Credit balance" up to, not including, "Payment details": no other word.
If the page asks to sign in, reply only SIGN_IN_REQUIRED."""
ROW = re.compile(r'\$([\d,]+(?:\.\d+)?) of \$([\d,]+(?:\.\d+)?)')
DATES = re.compile(r'([A-Z][a-z]{2}) (\d{1,2}), (\d{4})(?:, (\d{2}):(\d{2}))? UTC\s+([A-Z][a-z]{2}) (\d{1,2}), (\d{4})')


def _epoch(month, day, year, hour=0, minute=0):
    from .provider_quota import MONTHS
    return datetime(int(year), MONTHS.index(month) + 1, int(day), int(hour or 0), int(minute or 0),
                    tzinfo=timezone.utc).timestamp()


def parse(text):
    """The promotional credits of the Billing page's text: [{'name', 'remaining', 'granted', 'expires_at',
    'granted_at', 'applies_to'}], dollars and UTC epochs. ValueError when the text is not a finished Billing page."""
    if 'SIGN_IN_REQUIRED' in text:
        raise ValueError('sign in to the Console in the browser')
    if 'Credit balance' not in text:
        raise ValueError('not the Billing page')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if 'Promotional credits' not in lines:
        if any('Loading' in line for line in lines):
            raise ValueError('the page had not finished loading')
        return []
    lines = lines[lines.index('Promotional credits') + 1:]
    credits = []
    for i, line in enumerate(lines):
        row = ROW.fullmatch(line)
        if not row or i == 0 or i + 2 >= len(lines) or not (dates := DATES.search(lines[i + 2])):
            continue
        try:
            credits.append({'name': lines[i - 1], 'remaining': float(row[1].replace(',', '')),
                            'granted': float(row[2].replace(',', '')),
                            'expires_at': _epoch(*dates.group(1, 2, 3, 4, 5)), 'granted_at': _epoch(*dates.group(6, 7, 8)),
                            'applies_to': [name.strip() for name in lines[i + 1].split(',')]})
        except ValueError:  # a month name this parser does not know
            continue
    return credits


def read(executable, timeout=120):
    """The reply of one Claude Code call that reads the Billing page through the browser, or ValueError."""
    from .provider_quota import run
    try:
        done = json.loads(run(executable, '-p', PROMPT, '--chrome', '--model', 'haiku', '--setting-sources', '',
                              '--output-format', 'json', '--no-session-persistence', '--allowedTools', TOOLS,
                              timeout=timeout))
    except subprocess.TimeoutExpired:
        raise ValueError(f'the reading took over {timeout} s') from None
    except (OSError, ValueError):
        raise ValueError('Claude Code gave no answer (is it signed in, and the Chrome extension connected?)') from None
    if not isinstance(done, dict) or done.get('is_error') or not isinstance(done.get('result'), str):
        raise ValueError('the call failed: ' + str(done.get('subtype') or 'no result')[:60] if isinstance(done, dict)
                         else 'the call failed')
    return done['result']


def path():
    from .provider_spend import folder
    return folder()/'console-credit.json'


def last():
    """The record of the last attempt, or None."""
    try:
        record = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and type(record.get('at')) in (int, float) else None


def sync(now=None):
    """Read the Billing page and state the API credit that expires first and has something left (none has: the latest,
    at zero) for the account anthropic-api. Returns the attempt's record, which it also keeps; a failure changes nothing
    that was stated before."""
    from . import provider_spend
    from .cli_discovery import locate
    from .relay_io import write_json_atomic
    now = time.time() if now is None else now
    record = {'at': now, 'ok': False}
    try:
        if not (executable := locate('cli://claude')):
            raise ValueError('Claude Code is not installed')
        api = [c for c in parse(read(executable)) if 'API' in c['applies_to']]
        if not api:
            raise ValueError('no promotional credit of the Console applies to the API')
        left = [c for c in api if c['remaining'] > 0 and c['expires_at'] > now]
        credit = min(left, key=lambda c: c['expires_at']) if left else max(api, key=lambda c: c['expires_at'])
        usd = credit['remaining'] if left else 0.0
        provider_spend.declare(ACCOUNT, usd, now=now, window=(credit['granted_at'], credit['expires_at'], credit['granted']))
        record.update(ok=True, stated={'usd': usd, 'granted': credit['granted'], 'expires_at': credit['expires_at']})
    except (ValueError, OSError) as exc:
        record['reason'] = str(exc)[:160]
    path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path(), record)
    return record


def enabled():
    """Whether the operator turned the automatic reading on (tare setup --console-credit on); off until then."""
    from . import environment
    return (environment.read().get('console_credit') or {}).get('use') is True


def sync_if_due(now=None):
    """What the TUI does at start: sync() when the choice is on and the last attempt is over 12 hours old (one hour when
    it failed), else None."""
    now = time.time() if now is None else now
    record = last() or {}
    if not enabled() or now - record.get('at', 0) < (EVERY if record.get('ok') else AFTER_FAILURE):
        return None
    return sync(now)


def note(now):
    """One line for tare quota when the last attempt failed, else None."""
    from .provider_quota import clock
    if (record := last()) and not record.get('ok'):
        return (f"The last reading from the Console failed ({record.get('reason') or 'no reason'}, "
                f"{clock(record['at'], now)}); the earlier statement stays.")
    return None


def describe(record):
    """What tare quota --sync prints about an attempt."""
    if stated := record.get('stated'):
        return (f"Read from the Console: US$ {stated['usd']:.2f} left of US$ {stated['granted']:.2f}, lapses "
                + datetime.fromtimestamp(stated['expires_at'], timezone.utc).strftime('%a %d %b') + '.')
    return f"Console credit not read: {record.get('reason') or 'no reason'}; the earlier statement stays."
