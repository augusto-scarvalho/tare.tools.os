"""What each provider reports as left: subscription quota and API credit, read without a model call.

tare quota [--json] [--cached] [--set ACCOUNT AMOUNT]

Claude, Codex and AGY answer from their own CLI: the plan's windows, each with the models it covers and when it
resets. OpenRouter answers its credit endpoint. A provider that publishes no balance for an ordinary key is listed as
not reported, unless the operator stated its balance (--set): tare then shows that balance less the dollars its own
sessions recorded since. Every reading names its source and the time it was observed.
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
from datetime import datetime, timedelta
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


def read_claude(executable, now):
    result = json.loads(run(executable, '-p', '/usage', '--output-format', 'json', '--setting-sources', '',
                            '--no-session-persistence'))
    buckets = parse_claude(str(result.get('result') or ''), now)
    if not buckets:
        raise ValueError('no usage window in the answer')
    return {'buckets': buckets, 'source': 'claude -p /usage'}


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
    return {'buckets': buckets, 'notes': notes, 'source': 'agy -p /quota'}


def read_openrouter(key, now):
    from compute_plane.native_models import NativeHttpClient
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    control = ExecutionControl(BindingRef('provider-quota', 'openrouter', 'operator', 1, 'openrouter'),
                               CancellationToken(), time.monotonic() + 20)
    value, _ = NativeHttpClient('https://openrouter.ai/api/v1', credential=lambda: key,
                                timeout_seconds=20).get_observed('/credits', control)
    return {'credit': parse_openrouter(value), 'source': 'GET https://openrouter.ai/api/v1/credits'}


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
    jobs = [(account, name, 'subscription', reader, executable)
            for endpoint, (account, name, reader) in CLIS.items() if (executable := locate(endpoint))]
    if key := secret('OPENROUTER_API_KEY'):
        jobs.append(('openrouter', 'OpenRouter', 'credit', read_openrouter, key))
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        accounts = list(pool.map(lambda job: observe(*job, now), jobs))
    from . import provider_spend
    if secret('OPENCODE_API_KEY'):
        used = [provider_spend.spent('opencode-sub', now - seconds, 'allowance') for seconds in (5 * 3600, 7 * 86400, 30 * 86400)]
        accounts.append({'account': 'opencode-sub', 'name': 'OpenCode subscription', 'kind': 'subscription',
                         'status': 'NOT_REPORTED', 'observed_at': now, 'reason':
                         'OpenCode publishes no remaining allowance; a reply only says what it used.',
                         'allowance_used': dict(zip(('5h', '7d', '30d'), used)), 'notes': [
                             "Allowance tare's own sessions used: US$ {:.2f} in the last 5 h, US$ {:.2f} in 7 days, "
                             'US$ {:.2f} in 30 days.'.format(*used) if used[2] else
                             "tare's own sessions recorded no allowance use in the last 30 days."]})
    for reading in accounts:  # a limit a refused call recorded, which no provider reading shows
        if reading['kind'] == 'subscription' and (until := parked(reading['account'], now)):
            reading['parked_until'] = until
            reading.setdefault('notes', []).insert(0, 'A refused call left this account out until ' + clock(until, now) + '.')
    for variable, (account, name) in UNREPORTED.items():
        credit = provider_spend.remaining(account)
        reading = {'account': account, 'name': name, 'kind': 'credit', 'observed_at': now}
        if credit:
            accounts.append({**reading, 'status': 'DECLARED', 'credit': credit,
                             'source': 'the stated balance less the spend tare recorded since'})
        elif secret(variable) and account != 'opencode-api':  # OpenCode's key is listed as its subscription above
            accounts.append({**reading, 'status': 'NOT_REPORTED', 'reason':
                             'This provider has no balance endpoint for an API key. State the balance with '
                             'tare quota --set ' + account + ' AMOUNT and tare counts down from it.'})
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


def clock(epoch, now):
    when = datetime.fromtimestamp(epoch)
    return when.strftime('%H:%M' if when.date() == datetime.fromtimestamp(now).date() else '%a %d %b %H:%M')


def render(report, now=None):
    now = time.time() if now is None else now
    lines = []
    for kind, title in (('subscription', 'Subscriptions'), ('credit', 'API credit')):
        accounts = [a for a in report['accounts'] if a['kind'] == kind]
        if not accounts:
            continue
        lines += ([''] if lines else []) + [title]
        for account in accounts:
            head = '  ' + account['name'] + (' · ' + account['plan'] if account.get('plan') else '')
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
                lines.append(f"{head}: {left} left of US$ {credit['total']:.2f}{stated}")
                if credit.get('at_reference_prices'):
                    lines.append(f"    US$ {credit['at_reference_prices']:.2f} of that use is valued at models.dev "
                                 'reference prices, not confirmed tariffs: the balance is only as exact as they are.')
                continue
            lines.append(head)
            for row in account.get('buckets', []):
                left = 'none left' if row['remaining'] <= 0 else f"{row['remaining']:.0%} left"
                resets = ('resets ' + clock(row['resets_at'], now) if row.get('resets_at')
                          else 'resets ' + row['resets'] if row.get('resets') else '')
                lines.append(f"    {row['label']:<32}{left:>10}   {resets}".rstrip())
            lines += ['    ' + note for note in account.get('notes', [])]
    lines += ['', 'Read ' + clock(report['observed_at'], now) + ' from each provider; no model call was made.']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare quota', description=__doc__.split('\n\n')[0])
    parser.add_argument('--json', action='store_true', help='print the readings as JSON')
    parser.add_argument('--cached', action='store_true', help='show the last readings without asking the providers')
    parser.add_argument('--set', nargs=2, metavar=('ACCOUNT', 'AMOUNT'),
                        help="state an API account's balance in dollars as of now; tare counts its own spend down from it")
    args = parser.parse_args(argv)
    if args.set:
        from . import provider_spend
        try:
            provider_spend.declare(args.set[0], float(args.set[1]))
        except ValueError as exc:
            parser.error(str(exc))
    report = cached() if args.cached and not args.set else refresh()
    if report is None:
        print('No reading yet. Run tare quota.', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
