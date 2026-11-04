"""tare gpu status: who holds the shared local GPU, who waits for it and, when the node says, how long until text
gets it (contract gpu-lease/1, docs/guides/SHARED_GPU.md). tare gpu report: what tare spent on other routes because
the GPU was taken. tare gpu hold|run: a named reservation of the node's GPU for text (contract gpu-lease/2), for a
bench or a long run from this machine; the node keeps the lock."""
import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

TOKEN = 'TARE_GPU_TOKEN'  # the node's API token: environment, else the vault (tare setup --credential TARE_GPU_TOKEN)


def status(origin):
    with urlopen(origin + '/v1/fleet/status', timeout=5) as response:
        return json.load(response)


def describe(value):
    lease = value.get('gpu_coordination') or {}
    if not lease.get('enabled'):
        return 'GPU coordination is off on this gateway.'
    owner = lease.get('owner') or {}
    lines = [f"GPU {'held by ' + str(owner.get('kind') or 'unknown') if lease.get('held') else 'free'}"
             + (f" ({owner['reason']})" if owner.get('reason') else '')
             + (f"; model loaded: {value['current_model']}" if value.get('current_model') else '')]
    reserved = lease.get('reservation')
    if isinstance(reserved, dict):
        lines.append(f"  reserved for {reserved.get('name')} by {reserved.get('holder')}: only its requests use it")
    for ticket in lease.get('queue') or []:
        lines.append(f"  waiting: {ticket.get('kind')}" + (f" ({ticket['reason']})" if ticket.get('reason') else ''))
    eta = lease.get('eta_seconds')
    if type(eta) in (int, float) and eta >= 0:
        lines.append(f'  text gets it in about {round(eta / 60) or 1} min')
    return '\n'.join(lines)


def report(days=7, now=None):
    """{route: {'sessions', 'tokens', 'cost'}}: tare serve sessions started on another route because the local GPU
    was taken (an image job or someone else's reservation: their reason, or a local route skipped for it), in the
    last `days`, with what they used."""
    from decimal import Decimal, InvalidOperation
    from pathlib import Path
    from .native_installation import storage_root
    from .turn_engine import gpu_taken
    cutoff, rows = (now or time.time()) - days * 86400, {}
    for chat in (storage_root()/'serve'/'chats').glob('*.json'):
        try:
            entries = json.loads(chat.read_text(encoding='utf-8')).get('sessions') or []
        except (OSError, ValueError, AttributeError):
            continue
        for entry in entries:
            skipped = [s for s in entry.get('skipped') or [] if isinstance(s, dict)]
            reason = str(entry.get('reason') or '').split(',')[0]  # without ", back in about ..."
            if not (gpu_taken(reason) or any(gpu_taken(s.get('reason')) for s in skipped)):
                continue
            session = Path(str(entry.get('session')))
            try:
                if session.stat().st_mtime < cutoff:
                    continue
                usage = json.loads((session/'usage.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                usage = {}
            row = rows.setdefault(entry.get('route') or 'unknown', {'sessions': 0, 'tokens': 0, 'cost': {}})
            row['sessions'] += 1
            row['tokens'] += ((usage.get('tokens') or {}).get('total_tokens') or {}).get('known_sum') or 0
            for currency, amount in (usage.get('known_cost_by_currency') or {}).items():
                try:
                    row['cost'][currency] = str(Decimal(row['cost'].get(currency, '0')) + Decimal(str(amount)))
                except InvalidOperation:
                    pass
    return rows


def report_text(rows, days):
    if not rows:
        return f'Last {days} days: no tare serve turn left the local GPU because it was taken.'
    total = sum(r['sessions'] for r in rows.values())
    lines = [f'Last {days} days: {total} tare serve session(s) went to another route because the local GPU was '
             'taken.']
    for route, row in sorted(rows.items(), key=lambda item: -item[1]['sessions']):
        cost = ', '.join(f'{c} {a}' for c, a in sorted(row['cost'].items()))
        lines.append(f"  {route}: {row['sessions']} session(s), {row['tokens']:,} tokens" + (f', {cost}' if cost else ''))
    lines.append("Quota routes show tokens only; a Work unit's route is in its own receipt.")
    return '\n'.join(lines)


def seconds(text):
    """'2h', '90m', '45s' or a number of seconds."""
    found = re.fullmatch(r'(\d+(?:\.\d+)?)([hms]?)', str(text).strip())
    if not found:
        raise argparse.ArgumentTypeError('a duration such as 2h, 90m or 45s')
    return round(float(found.group(1)) * {'h': 3600, 'm': 60, 's': 1, '': 1}[found.group(2)])


def node_token():
    from .native_generation import vault_secret
    token = os.environ.get(TOKEN) or vault_secret(TOKEN)
    if not token:
        raise SystemExit(f"No node token: save the node's API token with `tare setup --credential {TOKEN}` "
                         '(from ~/.local/state/tare-qualified-models/api-token on the node).')
    return token


def reserve(stack, name, duration, wait, *, sleep=time.sleep):
    """Enter the node's reservation in `stack`, trying again while the GPU is taken until `wait` seconds pass."""
    from compute_plane import gpu_lease_client as lease
    from .native_catalog import local_gateway
    origin, token, deadline = local_gateway(), node_token(), time.monotonic() + wait
    lost = lambda exc: print(f'tare gpu: the reservation was lost ({exc}); the GPU is shared again.', file=sys.stderr)
    while True:
        try:
            return stack.enter_context(lease.hold(origin, token, name, duration, holder=socket.gethostname(),
                                                  wait=min(30, max(0, round(wait))), on_lost=lost))
        except lease.ReservationRefused as exc:
            left = deadline - time.monotonic()
            if exc.kind not in ('gpu_busy', 'gpu_reserved') or left <= 0:
                raise SystemExit(f'tare gpu: no reservation ({exc}).')
            print(f'tare gpu: the GPU is taken ({exc.kind}); trying again.', file=sys.stderr)
            sleep(min(exc.retry_after or 30, 60, max(1, left)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare gpu', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    show = sub.add_parser('status', help="the node's GPU: holder, queue, reservation and estimated wait")
    show.add_argument('--json', action='store_true', help='print the gateway status as it came')
    spent = sub.add_parser('report', help='what tare spent elsewhere because the local GPU was taken')
    spent.add_argument('--days', type=int, default=7)
    for action, text in (('hold', 'reserve the GPU for a name until the time ends or Ctrl-C'),
                         ('run', 'run a command whose tare sessions use the reserved GPU, then release it')):
        command = sub.add_parser(action, help=text)
        command.add_argument('name')
        command.add_argument('duration', type=seconds, help='at most 4h, e.g. 2h or 90m')
        command.add_argument('--wait', type=seconds, default=0, help='keep trying this long while the GPU is taken')
        if action == 'run':
            command.add_argument('command', nargs=argparse.REMAINDER, help='-- the command to run')
    args = parser.parse_args(argv)
    if args.action == 'report':
        print(report_text(report(args.days), args.days))
        return 0
    if args.action in ('hold', 'run'):
        command = [c for c in getattr(args, 'command', []) if c != '--'] if args.action == 'run' else None
        if args.action == 'run' and not command:
            parser.error('run needs a command after --')
        with contextlib.ExitStack() as stack:
            reservation = reserve(stack, args.name, args.duration, args.wait)
            until = time.strftime('%H:%M', time.localtime(reservation.get('hard_until') or time.time()))
            if args.action == 'hold':
                print(f'GPU reserved for {args.name} until {until}; Ctrl-C releases it.')
                with contextlib.suppress(KeyboardInterrupt):
                    time.sleep(max(0, args.duration))
                return 0
            print(f'GPU reserved for {args.name} until {until}; running {command[0]}.', file=sys.stderr)
            env = {**os.environ, 'TARE_GPU_RESERVATION': reservation['secret'],
                   'TARE_GPU_RESERVATION_ID': reservation['id']}
            return subprocess.call(command, env=env)
    from .native_catalog import local_gateway
    origin = local_gateway()
    try:
        value = status(origin)
    except (OSError, ValueError) as exc:
        print(f'The model gateway at {origin} did not answer: {type(exc).__name__}.')
        return 1
    print(json.dumps(value.get('gpu_coordination') or {}, indent=1) if args.json else describe(value))
    return 0
