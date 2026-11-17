"""What each provider reports as left: subscription quota and API credit, read without a model call.

tare quota [--json] [--cached] [--sync] [--set ACCOUNT AMOUNT [--renews DAY [GRANT]]]

Claude, Codex and AGY answer from their own CLI: the plan's windows, each with the models it covers and when it
resets. OpenRouter answers its credit endpoint. A provider that publishes no balance for an ordinary key is listed as
not reported, unless the operator stated its balance (--set): tare then shows that balance less the dollars its own
sessions recorded since (--renews: a credit that returns each month and does not carry over). Under "Local models" it
shows the energy the GPU drew for tare's local calls in the last 7 days, and its cost at the price tare setup holds
(--energy-price); it is never counted against a balance. A window or credit
with more left than the share of its period still to run is marked "in surplus": routing prefers a route whose every window is in surplus to cost until use
catches up with the pace. Every reading names its source and the time it was observed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from pathlib import Path

SCHEMA = 'tare.tools/provider-quota/1'
WINDOWS = {300: '5 h', 10080: 'week', '5h': '5 h', 'weekly': 'week'}
# Credentials whose provider has no balance endpoint for an ordinary key (checked 2026-11-10).
UNREPORTED = {'OPENAI_API_KEY': ('openai-api', 'OpenAI API'), 'ANTHROPIC_API_KEY': ('anthropic-api', 'Claude API'),
              'GEMINI_API_KEY': ('gemini-api', 'Gemini API'), 'AWS_BEARER_TOKEN_BEDROCK': ('bedrock', 'AWS Bedrock'),
              'OPENCODE_API_KEY': ('opencode-api', 'OpenCode API')}
CLAUDE_LINE = re.compile(r'Current ([^:(]+?)(?: \(([^)]+)\))?: (\d+(?:\.\d+)?)% used(?:\W+resets (.+))?$')
CLAUDE_RESET = re.compile(r'(?:([A-Z][a-z]{2}) (\d{1,2}),? (?:at )?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?: \(([^)]+)\))?')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def claude_reset(text, now):
    """'Nov 15, 5:59pm (America/Sao_Paulo)' or '5pm (America/Sao_Paulo)' as an epoch: the next such time."""
    match = CLAUDE_RESET.fullmatch(text.strip())
    if not match or match[1] and match[1] not in MONTHS:
        return None
    zone = datetime.fromtimestamp(now).astimezone().tzinfo  # the CLI prints the machine's zone
    if match[6]:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(match[6])
        except Exception:  # no zone database on this machine
            pass
    current = datetime.fromtimestamp(now, zone)
    hour = int(match[3]) % 12 + (12 if match[5] == 'pm' else 0)
    try:
        if match[1]:
            when = current.replace(month=MONTHS.index(match[1]) + 1, day=int(match[2]), hour=hour,
                                   minute=int(match[4] or 0), second=0, microsecond=0)
            if when < current - timedelta(days=1):
                when = when.replace(year=when.year + 1)
        else:
            when = current.replace(hour=hour, minute=int(match[4] or 0), second=0, microsecond=0)
            if when <= current:
                when += timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


def parse_claude(text, now):
    """The answer of `claude -p /usage`: 'Current week (Fable): 34% used · resets Nov 15, 5:59pm (America/Sao_Paulo)'."""
    buckets = []
    for line in text.splitlines():
        match = CLAUDE_LINE.match(line.strip())
        if not match:
            continue
        window, scope, used, resets = match[1].strip(), match[2], float(match[3]), match[4]
        everything = scope is None or scope.lower() == 'all models'
        buckets.append({'id': window + ('' if everything else ':' + scope.lower()),
                        'label': window + ('' if scope is None else ', ' + scope), 'window': window,
                        'scope': None if everything else scope.lower(),
                        'remaining': round(max(0.0, 1 - used / 100), 4),
                        'resets_at': claude_reset(resets, now) if resets else None, 'resets': resets})
    return buckets


def parse_codex(result):
    """The result of `account/rateLimits/read` from `codex app-server`: one or two windows per limit id."""
    limits = result.get('rateLimitsByLimitId')
    if not isinstance(limits, dict) or not limits:
        limits = {'codex': result.get('rateLimits')}
    buckets, notes, plan = [], [], None
    for name, limit in limits.items():
        if not isinstance(limit, dict):
            continue
        plan = plan or limit.get('planType')
        for key in ('primary', 'secondary'):
            window = limit.get(key)
            if not isinstance(window, dict) or type(window.get('usedPercent')) not in (int, float):
                continue
            minutes = window.get('windowDurationMins')
            span = WINDOWS.get(minutes, f'{minutes} min' if minutes else key)
            general = name == 'codex'
            buckets.append({'id': name + ':' + span, 'label': span + ('' if general else ', ' + name), 'window': span,
                            'scope': None if general else name,
                            'remaining': round(max(0.0, 1 - window['usedPercent'] / 100), 4),
                            'resets_at': window.get('resetsAt') if type(window.get('resetsAt')) in (int, float) else None})
    resets = (result.get('rateLimitResetCredits') or {}).get('availableCount')
    if type(resets) is int and resets:
        notes.append(f'{resets} free limit reset{"" if resets == 1 else "s"} available')
    return buckets, notes, plan


def parse_agy(quota, credits=None):
    """The `command.data` of `agy -p /quota` and `/credits` (--output-format json): model groups and their windows."""
    buckets, notes = [], []
    for group in quota.get('groups') or []:
        for row in group.get('buckets') or []:
            if type(row.get('remaining_fraction')) not in (int, float):
                continue
            span = WINDOWS.get(row.get('window'), row.get('window') or '')
            try:
                resets = datetime.fromisoformat(row['reset_time'].replace('Z', '+00:00')).timestamp()
            except (KeyError, ValueError, AttributeError):
                resets = None
            buckets.append({'id': row.get('id') or group['name'] + ':' + span, 'label': group['name'] + ', ' + span,
                            'window': span, 'scope': group['name'], 'models': group.get('description'),
                            'remaining': round(min(1.0, max(0.0, row['remaining_fraction'])), 4), 'resets_at': resets})
    if isinstance(credits, dict) and type(credits.get('remaining_credits')) in (int, float):
        notes.append(f"AI credits: {credits['remaining_credits']:g}")
    return buckets, notes


def parse_openrouter(value):
    """`GET /credits`: dollars bought and dollars used."""
    data = value.get('data') or {}
    total, used = data.get('total_credits'), data.get('total_usage')
    if type(total) not in (int, float) or type(used) not in (int, float):
        raise ValueError('credits not reported')
    return {'currency': 'USD', 'total': total, 'remaining': round(total - used, 6)}


def openrouter_use(value):
    """`GET /key`: the dollars OpenRouter itself counted for this key in its own day, week and month, as one note
    to set beside tare's own count; '' when it reports none of them."""
    data = value.get('data') if isinstance(value, dict) else None
    used = [(label, data[field]) for label, field in (('today', 'usage_daily'), ('this week', 'usage_weekly'),
                                                     ('this month', 'usage_monthly'))
            if isinstance(data, dict) and type(data.get(field)) in (int, float)]
    return ("OpenRouter's own count for this key: " + ', '.join(f'{label} US$ {amount:.2f}' for label, amount in used)
            + " (OpenRouter's day, week and month)") if used else ''


def anthropic_days(body):
    """`GET /v1/organizations/cost_report`: [(start of a UTC day, dollars)] from its daily buckets, whose amounts are
    decimal strings in cents."""
    return [(datetime.fromisoformat(bucket['starting_at'].replace('Z', '+00:00')).timestamp(),
             sum(float(row['amount']) for row in bucket.get('results') or [] if str(row.get('currency')).upper() == 'USD') / 100)
            for bucket in body['data']]


def openai_days(body):
    """`GET /v1/organization/costs`: [(start of a UTC day, dollars)] from its daily buckets."""
    return [(float(bucket['start_time']),
             sum(float(row['amount']['value']) for row in bucket.get('results') or []
                 if str(row['amount'].get('currency')).lower() == 'usd'))
            for bucket in body['data']]


# What a provider itself counted for the whole organization, where an ordinary API key may not ask (probed
# 2026-11-17: Anthropic answers it HTTP 401, OpenAI HTTP 403). It takes an admin key, a different kind of key that
# manages the organization: the owner creates it and stores it under its own name (tare setup --credential NAME).
# tare sends it to that provider's cost report and nowhere else: no route names these variables, so no model call can
# carry one. account: (the variable, the provider's name, the endpoint, the path for a first day, extra headers,
# the parser).
ADMIN_REPORTS = {
    'anthropic-api': ('ANTHROPIC_ADMIN_KEY', 'Anthropic', 'https://api.anthropic.com/v1',
                      lambda start: '/organizations/cost_report?limit=31&starting_at='
                      + datetime.fromtimestamp(start, timezone.utc).strftime('%Y-%m-%dT00:00:00Z'),
                      {'anthropic-version': '2026-10-21'}, anthropic_days),
    'openai-api': ('OPENAI_ADMIN_KEY', 'OpenAI', 'https://api.openai.com/v1',
                   lambda start: f'/organization/costs?bucket_width=1d&limit=31&start_time={int(start)}', None, openai_days),
}


def provider_count(account, now):
    """One note of what the provider counted for the organization in the last 7 and 30 UTC days, read with the stored
    admin key; a note of the failure when the key is refused (an ordinary key stored under the admin name is); None
    when no admin key is stored. The note never holds the key or the provider's message."""
    variable, name, endpoint, path, headers, parse = ADMIN_REPORTS[account]
    key = secret(variable)
    if not key:
        return None
    from .web_search import NoRedirect
    day = 86400
    first = (now // day - 29) * day  # 30 UTC days, today included
    try:
        # ponytail: the kernel's HTTP client takes no query string and both reports need one, so this is a plain GET:
        # redirects refused (the key belongs to this one endpoint), bounded in time and size, nothing of the answer
        # or the error kept but its numbers and its HTTP status. Move it to the kernel's client when that takes a query.
        request = Request(endpoint + path(first), headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                                                           'User-Agent': 'tare', **(headers or {})})
        with build_opener(NoRedirect()).open(request, timeout=20) as reply:
            body = json.loads(reply.read(4 * 1024 * 1024))
        days = parse(body)
        week = sum(usd for start, usd in days if start >= (now // day - 6) * day)
        more = ' or more: the report has further pages' if body.get('has_more') else ''
        return (f"{name}'s own count for the organization: 7 days US$ {week:.2f}, 30 days US$ "
                f"{sum(usd for _, usd in days):.2f}{more} (UTC days, every key of the organization)")
    except Exception as exc:  # the reading of the account must not fail for a report
        why = f'HTTP {exc.code}' if isinstance(exc, HTTPError) else type(exc).__name__
        return (f"{variable} did not read {name}'s cost report: {why}. It takes an admin key of the organization, "
                'not an ordinary API key.')


def run(executable, *arguments, timeout=90):
    from .turn_engine import cli_env
    done = subprocess.run([executable, *arguments], capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, stdin=subprocess.DEVNULL, env=cli_env(),
                          cwd=str(Path(executable).absolute().parent))
    return done.stdout


def codex_limits(executable, timeout=45):
    """One JSON-RPC exchange with `codex app-server` over stdio; the server is stopped afterwards."""
    from .turn_engine import cli_env
    server = subprocess.Popen([executable, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', env=cli_env(),
                              cwd=str(Path(executable).absolute().parent))
    watchdog = threading.Timer(timeout, server.kill)
    watchdog.start()

    def send(message):
        server.stdin.write(json.dumps(message) + '\n')
        server.stdin.flush()
    try:
        send({'id': 0, 'method': 'initialize', 'params': {'clientInfo': {'name': 'tare', 'title': 'tare', 'version': '1'}}})
        for line in server.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get('id') == 0:
                send({'method': 'initialized'})
                send({'id': 1, 'method': 'account/rateLimits/read'})
            elif message.get('id') == 1:
                if not isinstance(message.get('result'), dict):
                    raise ValueError(str((message.get('error') or {}).get('message') or 'no result')[:120])
                return message['result']
        raise ValueError('codex app-server closed without an answer')
    finally:
        watchdog.cancel()
        server.kill()


def claude_plan(executable):
    """planTier (else subscriptionType) of `claude auth status`, a local answer. It also holds the account's email and
    organisation: only the plan is taken. A failure is no plan, never no windows."""
    try:
        answer = json.loads(run(executable, 'auth', 'status', timeout=20))
        plan = answer.get('planTier') or answer.get('subscriptionType')
        return plan if isinstance(plan, str) else None
    except Exception:
        return None


def read_claude(executable, now):
    result = json.loads(run(executable, '-p', '/usage', '--output-format', 'json', '--setting-sources', '',
                            '--no-session-persistence'))
    buckets = parse_claude(str(result.get('result') or ''), now)
    if not buckets:
        raise ValueError('no usage window in the answer')
    return {'buckets': buckets, 'plan': claude_plan(executable), 'source': 'claude -p /usage'}


def read_codex(executable, now):
    buckets, notes, plan = parse_codex(codex_limits(executable))
    return {'buckets': buckets, 'notes': notes, 'plan': plan, 'source': 'codex app-server account/rateLimits/read'}


def read_agy(executable, now):
    def command(name):
        data = (json.loads(run(executable, '-p', '/' + name, '--output-format', 'json')).get('command') or {}).get('data')
        if not isinstance(data, dict):  # a signed-out CLI, or one older than the read-only slash commands
            raise ValueError('the CLI gave no answer to /' + name)
        return data
    try:
        credits = command('credits')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        credits = None  # the quota still stands without it
    buckets, notes = parse_agy(command('quota'), credits)
    plan_read = agy_plan(executable, now)
    return {'buckets': buckets, 'notes': notes, 'source': 'agy -p /quota', 'plan': plan_read and plan_read['plan'],
            'plan_read': plan_read}


PLAN_LINE = re.compile(r'\((Google AI [A-Za-z ]+)\)')  # the header: '<account email> (Google AI Pro)'


def agy_screen_plan(executable, folder, timeout=12):
    """The plan on the first screen of AGY's TUI, which no AGY command reports. Nothing is sent: no key, no text, not even
    an answer to the terminal's queries, so no model call can result and the trust question of a folder AGY does not know
    is never answered (that screen gives no plan). The header also holds the account's email: only the plan leaves here."""
    from compute_plane.terminal_sessions import TerminalSessions
    from .terminal_display import TerminalDisplay
    from .turn_engine import cli_env
    display, cursor, end = TerminalDisplay(120, 40), 0, time.monotonic() + timeout
    with TerminalSessions() as sessions:  # leaving it ends AGY and its children
        handle = sessions.start(owner='plan', request_id='plan', argv=[executable], cwd=folder, env=cli_env(),
                                timeout=timeout + 15, mode='pty', columns=120, rows=40)['handle']
        while time.monotonic() < end:
            page = sessions.read(owner='plan', handle=handle, cursors={'stdout': cursor, 'stderr': 0})['output']['stdout']
            cursor = page['cursor']
            display.feed(page['text'], truncated=page['truncated'])
            screen = '\n'.join(display.active.display)
            if 'do you trust' in screen.lower():
                return None
            if match := PLAN_LINE.search(screen):
                return match[1].strip()
            time.sleep(0.2)
    return None


def agy_plan(executable, now):
    """{'fingerprint', 'at', 'plan'}: the screen is read once per executable file (about seven seconds) and kept in the
    quota cache; a miss (no plan on the screen) is tried again after a day. A failure leaves the windows alone."""
    from .cli_discovery import fingerprint
    try:
        stamp = fingerprint(executable)
        held = next((row.get('plan_read') for row in (cached() or {}).get('accounts') or []
                     if row.get('account') == 'agy'), None)
        if held and held['fingerprint'] == stamp and (held['plan'] or now - held['at'] < 86400):
            return held
        return {'fingerprint': stamp, 'at': now, 'plan': agy_screen_plan(executable, os.getcwd())}
    except Exception:
        return None


def read_openrouter(key, now):
    from compute_plane.native_models import NativeHttpClient
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    client = NativeHttpClient('https://openrouter.ai/api/v1', credential=lambda: key, timeout_seconds=20)

    def get(path):  # each request on its own 20 s: one that times out must not leave the other no time
        return client.get_observed(path, ExecutionControl(BindingRef('provider-quota', 'openrouter', 'operator', 1, 'openrouter'),
                                                          CancellationToken(), time.monotonic() + 20))[0]
    # Two readings that stand apart: the account's balance (`/credits`, which OpenRouter refuses to a key that may
    # not manage the account) and what this key itself spent (`/key`, which any key may ask). Neither loses the other.
    reading, refused = {'source': 'GET https://openrouter.ai/api/v1/credits'}, None
    try:
        reading['credit'] = parse_openrouter(get('/credits'))
    except Exception as exc:
        refused = exc
    try:
        if note := openrouter_use(get('/key')):
            reading['notes'] = [note]
    except Exception:
        pass
    if refused is not None:
        if 'notes' not in reading:
            raise refused  # neither answered: no reading, as before
        code = getattr(refused, 'code', None)
        why = code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9_]{1,40}', code) else type(refused).__name__
        reading['notes'].insert(0, f"OpenRouter did not report the account's balance to this key ({why}): the balance "
                                   'is unknown, and unknown is not zero. What the key itself spent:')
        reading['source'] = 'GET https://openrouter.ai/api/v1/key'
    return reading


CLIS = {'cli://claude': ('claude', 'Claude subscription', read_claude),
        'cli://codex': ('codex', 'Codex subscription', read_codex),
        'cli://agy': ('agy', 'AGY subscription', read_agy)}


def observe(account, name, kind, reader, argument, now):
    reading = {'account': account, 'name': name, 'kind': kind, 'observed_at': now}
    try:
        return {**reading, 'status': 'OBSERVED', **reader(argument, now)}
    except subprocess.TimeoutExpired:
        return {**reading, 'status': 'UNAVAILABLE', 'reason': 'the provider did not answer in time'}
    except Exception as exc:  # one provider's failure must not hide the others
        return {**reading, 'status': 'UNAVAILABLE', 'reason': (type(exc).__name__ + ': ' + str(exc))[:160]}


def secret(name):
    from .native_generation import vault_secret
    return os.environ.get(name) or vault_secret(name)


def refresh(now=None):
    """Ask every configured provider now, in parallel, and keep the answers for `cached()`."""
    from .cli_discovery import locate
    now = time.time() if now is None else now
    began = time.monotonic()
    jobs = [(account, name, 'subscription', reader, executable)
            for endpoint, (account, name, reader) in CLIS.items() if (executable := locate(endpoint))]
    if key := secret('OPENROUTER_API_KEY'):
        jobs.append(('openrouter', 'OpenRouter', 'credit', read_openrouter, key))
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        accounts = list(pool.map(lambda job: observe(*job, now), jobs))
    # A quota reading that failed this once must not forget AGY's plan: its TUI would be opened again for nothing.
    held = next((row.get('plan_read') for row in (cached() or {}).get('accounts') or [] if row.get('account') == 'agy'), None)
    for reading in accounts:
        if reading['account'] == 'agy' and held and not reading.get('plan_read'):
            reading['plan_read'] = held
    from . import provider_spend
    if secret('OPENCODE_API_KEY'):
        plan = provider_spend.last_plan('opencode-sub')  # the billing source of the last reply of the subscription route
        used = [provider_spend.spent('opencode-sub', now - seconds, 'allowance') for seconds in (5 * 3600, 7 * 86400, 30 * 86400)]
        accounts.append({'account': 'opencode-sub', 'name': 'OpenCode subscription', 'kind': 'subscription',
                         'status': 'NOT_REPORTED', 'observed_at': now, **({'plan': plan} if plan else {}), 'reason':
                         'OpenCode publishes no remaining allowance; a reply only says what it used.',
                         'allowance_used': dict(zip(('5h', '7d', '30d'), used)), 'notes': [
                             "Allowance tare's own sessions used: US$ {:.2f} in the last 5 h, US$ {:.2f} in 7 days, "
                             'US$ {:.2f} in 30 days.'.format(*used) if used[2] else
                             "tare's own sessions recorded no allowance use in the last 30 days."]})
    for variable, (account, name) in UNREPORTED.items():
        credit = provider_spend.remaining(account)
        reading = {'account': account, 'name': name, 'kind': 'credit', 'observed_at': now}
        if credit:
            accounts.append({**reading, 'status': 'DECLARED', 'credit': credit,
                             'source': 'the stated balance less the spend tare recorded since'})
        elif (secret(variable) or account in ADMIN_REPORTS and secret(ADMIN_REPORTS[account][0])) and account != 'opencode-api':
            # OpenCode's key is listed as its subscription above; an admin key alone still has its report to show
            accounts.append({**reading, 'status': 'NOT_REPORTED', 'reason':
                             'This provider has no balance endpoint for an API key. State the balance with '
                             'tare quota --set ' + account + " AMOUNT, or in the console's /quota, and tare counts "
                             'down from it.'})
    for reading in accounts:  # the provider's own count, where the owner stored an admin key for it
        if reading['account'] in ADMIN_REPORTS and (note := provider_count(reading['account'], now)):
            reading.setdefault('notes', []).append(note)
    for reading in accounts:  # tare's own use of each API account, from the ledger (kept apart from what the provider says)
        if reading['kind'] == 'credit' and (use := provider_spend.own_use(reading['account'], now)):
            reading['own_use'] = use
            reading.setdefault('notes', []).append(own_use_line(use))
    for reading in accounts:  # a limit a refused call recorded (a spent plan, an API account with no credit), which no reading shows
        if until := parked(reading['account'], now):
            reading['parked_until'] = until
            reading.setdefault('notes', []).insert(0, 'A refused call left this account out until ' + clock(until, now) + '.')
    if energy := provider_spend.energy_report(now):  # the local models' GPU energy (never money spent)
        accounts.append({'account': provider_spend.LOCAL, 'name': 'Local models', 'kind': 'local', 'status': 'MEASURED',
                         'observed_at': now, 'energy': energy})
    from .console_credit import ACCOUNT, note
    if text := note(now):  # a failed Console reading, under the Claude API account
        for reading in accounts:
            if reading['account'] == ACCOUNT:
                reading.setdefault('notes', []).append(text)
    from . import plan_consumption
    # This reading joins the history the estimate is taken from, with the time the sampling took.
    plan_consumption.keep(accounts, now, done=now + (time.monotonic() - began))
    plan_consumption.annotate(accounts, now)
    report = {'schema': SCHEMA, 'observed_at': now, 'accounts': accounts}
    from .relay_io import write_json_atomic
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, report)
    return report


def parked(account, now):
    """Until when a refused call left this whole account out (turn_engine.note_usage_limit), else None."""
    from .turn_engine import usage_limits_path
    try:
        entry = json.loads(usage_limits_path().read_text(encoding='utf-8')).get(account)
        until = entry.get('until')
    except (OSError, ValueError, AttributeError):
        return None
    return until if type(until) in (int, float) and now < until <= now + 7 * 86400 else None


def cache_path():
    from .native_installation import storage_root
    return storage_root()/'serve'/'provider-quota.json'


def cached():
    """The last answers `refresh()` kept, or None."""
    try:
        report = json.loads(cache_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and report.get('schema') == SCHEMA else None


def covers(account, bucket, model):
    """Whether a window limits this model. A window tare cannot place limits nothing: it is shown, never acted on."""
    scope = bucket.get('scope')
    if scope is None:
        return True  # the account's general window
    if account == 'agy':  # AGY's groups by their ids: Argon's own quota, the third-party models, the other Gemini models
        key = str(bucket.get('id') or '')
        if key.startswith('gemini-argon'):
            return 'argon' in model
        if key.startswith('3p-'):
            return not model.startswith('gemini')
        return key.startswith('gemini-') and model.startswith('gemini') and 'argon' not in model
    return scope in model  # Claude's "Fable" window; a Codex limit named after a model


def spent_until(deployment, now=None):
    """Until when the saved readings say this route's quota is spent, else None. Only a window read at zero counts,
    until its own reset (half an hour when the provider gave none); a window with anything left leaves the route."""
    from .turn_engine import USAGE_LIMIT_DEFAULT_SECONDS, usage_account
    account, report = usage_account(deployment), cached()
    if account is None or not report:
        return None
    now = time.time() if now is None else now
    model = str((deployment.get('native_profile') or {}).get('model') or '').lower()
    found = []
    try:
        for reading in report['accounts']:
            if reading.get('account') != account or reading.get('status') != 'OBSERVED':
                continue
            for bucket in reading.get('buckets') or []:
                if bucket['remaining'] > 0 or not covers(account, bucket, model):
                    continue
                until = bucket.get('resets_at') or reading['observed_at'] + USAGE_LIMIT_DEFAULT_SECONDS
                if now < until <= now + 7 * 86400:  # the bound of a limit a failed call records
                    found.append(until)
    except (KeyError, TypeError):
        return None  # an unreadable record is unknown, not a limit
    return max(found) if found else None


def balance_spent(deployment, now=None):
    """Until when an API route is left out because its account's known balance is spent, else None (owner,
    2026-11-16: the balance is a guard). Known: a stated balance tare counted down to zero, or OpenRouter's own reading
    at zero while that reading is fresh. Until the credit renews when it does, else half an hour: it is asked again at
    each use, so a new statement or reading lifts it at once. No known balance leaves nothing out: unknown is not zero.
    ponytail: `remaining` scans the spend ledger at each ask; cache it per ranking as `surplus` does if rankings slow."""
    from . import provider_spend
    from .turn_engine import USAGE_LIMIT_DEFAULT_SECONDS
    name = provider_spend.account(deployment)
    if not name:
        return None
    now = time.time() if now is None else now
    credit = provider_spend.remaining(name, now)
    if credit is None and name == 'openrouter':
        credit = next((a.get('credit') for a in (cached() or {}).get('accounts') or []
                       if a.get('account') == name and a.get('status') == 'OBSERVED'
                       and type(a.get('observed_at')) in (int, float) and now - FRESH <= a['observed_at'] <= now), None)
    try:
        if not credit or credit['remaining'] > 0:
            return None
    except (KeyError, TypeError):
        return None  # an unreadable record is unknown, not a limit
    renews = credit.get('renews_at')
    return renews if type(renews) in (int, float) and now < renews <= now + 7 * 86400 else now + USAGE_LIMIT_DEFAULT_SECONDS


# The windows tare can place in time. 'session' is Claude's own name for its 5 h window (`Current session: ...`).
LENGTHS = {'5 h': 5 * 3600, 'session': 5 * 3600, 'week': 7 * 86400}
LEAD = 0.01  # a smaller lead is no surplus: a window that has just renewed is not one
# A reading older than this says too little of what is left now: on 2026-11-14 the saved one was two days old and still
# showed 23% of a week the day's work had used. The TUI rereads at each demand; an older reading promotes nothing.
FRESH = 3600


def credit_pace(credit, now):
    """(remaining fraction, time-left fraction) of a renewing declared credit in its cycle, else None."""
    if not credit.get('renews_at') or credit['remaining'] <= 0 or credit['renews_at'] <= now:
        return None
    return min(1.0, credit['remaining'] / credit['grant']), (credit['renews_at'] - now) / (credit['renews_at'] - credit['cycle_start'])


def surplus(deployment, now=None, cache=None):
    """The one signal that a route's allowance lapses unused: None, or {'lead', 'remaining', 'time_left', 'resets_at',
    'allowance'} while what is left exceeds the share of its window still to run (use first what would be lost, and
    stop once use catches the pace). A subscription route needs every window that limits its model in surplus; an API
    route needs a declared credit that renews or lapses on a stated day. Nothing else (OpenRouter, a plain balance, a
    local model) has one.
    `cache`: shared by one ranking so the files are read once."""
    from . import provider_spend
    from .turn_engine import usage_account
    now = time.time() if now is None else now
    cache = {} if cache is None else cache
    if account := usage_account(deployment):
        if 'report' not in cache:
            cache['report'] = cached() or {}
        model = str((deployment.get('native_profile') or {}).get('model') or '').lower()
        found = []
        try:
            for reading in cache['report'].get('accounts') or []:
                if (reading.get('account') != account or reading.get('status') != 'OBSERVED'
                        or not now - FRESH <= reading['observed_at'] <= now):
                    continue
                for bucket in reading.get('buckets') or []:
                    length, end = LENGTHS.get(bucket.get('window')), bucket.get('resets_at')
                    if not length or type(end) not in (int, float) or end <= now or not covers(account, bucket, model):
                        continue  # a window tare cannot place, or one whose reading says nothing of the new window
                    time_left = (end - now) / length
                    found.append((bucket['remaining'] - time_left, bucket, time_left))
        except (KeyError, TypeError, AttributeError):
            return None
        # ponytail: the reading is the last finished one; a refresh started by a TUI demand lands for the next one (tare serve and
        # tare agent refresh only where they already do)
        if not found or (worst := min(found, key=lambda row: row[0]))[0] < LEAD:
            return None
        return {'lead': worst[0], 'remaining': worst[1]['remaining'], 'time_left': worst[2],
                'resets_at': worst[1]['resets_at'], 'allowance': account + ' ' + worst[1]['label']}
    if not (name := provider_spend.account(deployment)):
        return None
    if ('credit', name) not in cache:
        cache[('credit', name)] = provider_spend.remaining(name, now)
    credit = cache[('credit', name)]
    if not credit or not (pace := credit_pace(credit, now)) or pace[0] - pace[1] < LEAD:
        return None
    return {'lead': pace[0] - pace[1], 'remaining': pace[0], 'time_left': pace[1], 'resets_at': credit['renews_at'],
            'allowance': name + ' credit'}


def lapse_order(signal):
    """Sort key for routes by `surplus`: one whose allowance lapses in surplus first, the sooner lapse first (what is
    lost first is used first: a week ending today before a credit ending next week, whatever their leads), the larger
    lead between two that lapse together; a route with nothing lapsing after them."""
    return (0, signal['resets_at'], -round(signal['lead'], 2)) if signal else (1, 0, 0)


def used_first(remaining, time_left, what='window'):
    """The mark `tare quota` puts on a line in surplus, or ''."""
    return (f" · in surplus: {remaining:.0%} left, {time_left:.0%} of the {what} left: used first"
            if remaining - time_left >= LEAD else '')


def day_end(text):
    """The epoch the UTC day YYYY-MM-DD ends at: a credit that lapses on a day is good through it."""
    return (datetime.strptime(text, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp()


def clock(epoch, now):
    when = datetime.fromtimestamp(epoch)
    return when.strftime('%H:%M' if when.date() == datetime.fromtimestamp(now).date() else '%a %d %b %H:%M')


def own_use_line(use):
    """What tare's own calls on an API account came to, as one note: dollars, calls and tokens over 7 and 30 days, how
    the dollars were known, and the calls it could not value (a stated balance counts down too slowly for those)."""
    def amount(n):
        return str(n) if n < 1000 else f'{n/1000:.0f}k' if n < 999500 else f'{n/1e6:.1f}M'

    def span(days, v):
        calls = v['calls']
        return (f"{days} days US$ {v['usd']:.2f} in {calls} call{'s' * (calls != 1)}"
                + (f" ({amount(v['tokens'])} tokens)" if v['tokens'] is not None else ''))
    week, month = use['7d'], use['30d']
    basis = month['basis']
    ending = ('charged by the provider' if basis == ['charged'] else "valued at the provider's tariff" if basis == ['tariff']
              else 'valued at models.dev reference prices, not a confirmed tariff' if basis == ['reference']
              else 'valued partly at a tariff or a reference price' if basis else '')
    n = month['unpriced']
    return ("tare's own use: " + span(7, week) + '; ' + span(30, month) + (' · ' + ending if ending else '')
            + (f" · {n} call{'s' * (n != 1)} tare could not value: their tokens are counted, their cost is not, "
               'so a stated balance counts down too slowly' if n else ''))


def energy_line(energy):
    """The local account's energy as `tare quota` says it. 'GPU only': nvidia-smi measures the card, not the machine."""
    text = f"{energy['kwh']:.2f} kWh in {energy['days']} days"
    if energy.get('usd') is not None:
        text += f", about US$ {energy['usd']:.2f} at US$ {energy['price_kwh']:g}/kWh (GPU only)"
    else:
        text += ' (GPU only); tare setup --energy-price US$_PER_KWH shows it in dollars'
    if energy.get('declared_kwh'):
        text += f", {energy['declared_kwh']:.2f} kWh of it at the watts you declared (the node reported none: an upper bound)"
    if energy.get('unmeasured_hours'):
        text += (f"; {energy['unmeasured_hours']:g} h of sessions not counted (the node no longer held their readings: "
                 'a partial total)')
    return text


def render(report, now=None):
    from . import plan_consumption
    now = time.time() if now is None else now
    lines = []
    for kind, title in (('subscription', 'Subscriptions'), ('credit', 'API credit'), ('local', 'Local models')):
        accounts = [a for a in report['accounts'] if a['kind'] == kind]
        if not accounts:
            continue
        lines += ([''] if lines else []) + [title]
        for account in accounts:
            if kind == 'local':
                lines.append('  local: ' + energy_line(account['energy']))
                continue
            head = '  ' + account['name'] + (' · plan ' + account['plan'] if account.get('plan') else '')
            if account['status'] not in ('OBSERVED', 'DECLARED'):
                lines.append(head + ': ' + ('not reported. ' if account['status'] == 'NOT_REPORTED' else 'no reading. ')
                             + account['reason'])
                lines += ['    ' + note for note in account.get('notes', [])]
                continue
            credit = account.get('credit')
            if credit:
                left = 'none' if credit['remaining'] <= 0 else f"US$ {credit['remaining']:.2f}"
                stated = (f", stated {clock(credit['declared_at'], now)}, less tare's own use since"
                          if account['status'] == 'DECLARED' else '')
                if credit.get('console'):  # a window read from the Console: it says where it came from
                    stated = (', lapses ' + datetime.fromtimestamp(credit['renews_at'], timezone.utc).strftime('%a %d %b')
                              + ' (read from the Console ' + datetime.fromtimestamp(credit['declared_at']).strftime('%d %b %H:%M') + ')')
                elif credit.get('stated'):  # an expiring credit the owner stated: it never renews
                    # the day the owner stated: the window ends as that day does
                    stated = (', lapses ' + datetime.fromtimestamp(credit['renews_at'] - 1, timezone.utc).strftime('%a %d %b %Y')
                              + stated)
                elif credit.get('renews_at'):
                    stated = ' a cycle, renews ' + datetime.fromtimestamp(credit['renews_at'], timezone.utc).strftime('%a %d %b')
                pace = credit_pace(credit, now)
                lines.append(f"{head}: {left} left of US$ {credit['total']:.2f}{stated}"
                             + (used_first(*pace, 'time to its lapse' if credit.get('stated') else 'cycle') if pace else '')
                             + (' · its routes are left out while nothing is left' if credit['remaining'] <= 0 else ''))
                if credit.get('at_reference_prices'):
                    lines.append(f"    US$ {credit['at_reference_prices']:.2f} of that use is valued at models.dev "
                                 'reference prices, not confirmed tariffs: the balance is only as exact as they are.')
                lines += ['    ' + note for note in account.get('notes', [])]
                continue
            lines.append(head)
            for row in account.get('buckets', []):
                left = 'none left' if row['remaining'] <= 0 else f"{row['remaining']:.0%} left"
                resets = ('resets ' + clock(row['resets_at'], now) if row.get('resets_at')
                          else 'resets ' + row['resets'] if row.get('resets') else '')
                pace = (row.get('window') in LENGTHS and type(row.get('resets_at')) in (int, float) and row['resets_at'] > now
                        and now - FRESH <= account['observed_at'] <= now)
                mark = used_first(row['remaining'], (row['resets_at'] - now) / LENGTHS[row['window']]) if pace else ''
                out = ' · its routes are left out until then' if row['remaining'] <= 0 and row.get('resets_at') else ''  # what spent_until acts on
                lines.append(f"    {row['label']:<32}{left:>10}   {resets}{mark}{out}".rstrip())
                lines.append('      ' + plan_consumption.line(row.get('consumption')))
            lines += ['    ' + note for note in account.get('notes', [])]
    lines += ['', 'Read ' + clock(report['observed_at'], now) + ' from each provider; no model call was made.']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare quota', description=__doc__.split('\n\n')[0])
    parser.add_argument('--json', action='store_true', help='print the readings as JSON')
    parser.add_argument('--cached', action='store_true', help='show the last readings without asking the providers')
    parser.add_argument('--sync', action='store_true', help=(
        "read the Claude API credit (balance and expiry) from the Console's Billing page through the Claude in Chrome "
        'extension, in a browser signed in to the Console: one Haiku call on the Claude subscription, about half a minute, '
        'a tab that opens and closes. Running it is the consent for that one reading'))
    parser.add_argument('--set', nargs=2, metavar=('ACCOUNT', 'AMOUNT'),
                        help="state an API account's balance in dollars as of now; tare counts its own spend down from it")
    parser.add_argument('--renews', nargs='+', metavar=('DAY', 'GRANT'), help=(
        'with --set: the credit renews on this day of the month (1-31) and does not carry over; GRANT is the dollars '
        'each cycle brings (default: the amount stated). While more is left than the share of the cycle still to run, '
        'tare uses the account before cheaper routes'))
    parser.add_argument('--expires', metavar='DATE', help=(
        'with --set: the credit lapses at the end of this day (YYYY-MM-DD, UTC) and never renews, as a promotional '
        'cloud credit does. While more is left than the share of the time to that day still to run, tare uses '
        'the account before cheaper routes'))
    args = parser.parse_args(argv)
    if args.renews and (not args.set or len(args.renews) > 2):
        parser.error('--renews DAY [GRANT] goes with --set ACCOUNT AMOUNT')
    if args.expires and (not args.set or args.renews):
        parser.error('--expires DATE goes with --set ACCOUNT AMOUNT, without --renews')
    if args.set:
        from . import provider_spend
        try:
            amount, renews = float(args.set[1]), None
            if args.renews:
                renews = (int(args.renews[0]), float(args.renews[-1]) if len(args.renews) > 1 else amount)
            expires = args.expires and day_end(args.expires)
            provider_spend.declare(args.set[0], amount, renews=renews, expires=expires or None)
        except ValueError as exc:
            parser.error(str(exc))
    synced = None
    if args.sync:
        from . import console_credit
        synced = console_credit.sync()
        print(console_credit.describe(synced), file=sys.stderr)
    report = cached() if args.cached and not args.set and not args.sync else refresh()
    if report is None:
        print('No reading yet. Run tare quota.', file=sys.stderr)
        return 1
    shown = json.dumps(report, indent=2) if args.json else render(report)
    if args.sync:  # the footer speaks of the providers' readings; the Console reading is one model call
        shown = shown.replace('no model call was made.', 'the Console reading made one model call (Haiku).')
    print(shown)
    return 0 if synced is None or synced['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
