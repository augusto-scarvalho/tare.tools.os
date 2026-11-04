#!/usr/bin/env python3
"""Rent the shared GPU: `gpu status`, `gpu run [--task NAME] -- cmd`, `gpu hold --reason R 30m`.

run   supervises a command under the exclusive image lease (the gateway unloads its text model).
      No deadline: --task learns the usual duration and status flags a late run; --kill-after cuts.
hold  opens a shell holding the GPU for a mandatory duration; exit the shell to give it back early.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from tare_node import gpu_lease_run as supervisor  # noqa: E402
from tare_node.gpu_lease_run import LATE_FACTOR, STATE_DIR, SharedGpuLease  # noqa: E402
from tare_node.gpu_lease import eta_seconds  # noqa: E402

LOCK = Path.home()/'.local/state/tare-qualified-models/gpu.lock'
GATEWAY = os.environ.get('TARE_NODE_GATEWAY', 'http://127.0.0.1:8080')


def reservation(gateway=GATEWAY):
    """The gateway's current named reservation (contract gpu-lease/2), or None; it lives in the gateway process."""
    from urllib.request import urlopen
    try:
        with urlopen(gateway + '/v1/fleet/status', timeout=1) as response:
            return (json.load(response).get('gpu_coordination') or {}).get('reservation')
    except (OSError, ValueError):
        return None


def ago(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m' if seconds >= 3600 else \
        f'{seconds // 60}m{seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


def jobs():
    """Supervised jobs that are still running (a crashed supervisor leaves a file behind)."""
    found = []
    for path in sorted(STATE_DIR.glob('active-*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            os.kill(job['pid'], 0)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        elapsed = time.time() - job['started_at']
        job['elapsed_seconds'] = round(elapsed)
        job['late'] = bool(job.get('expected_seconds')) and elapsed > LATE_FACTOR * job['expected_seconds']
        found.append(job)
    return found


def status(lock=LOCK):
    state = SharedGpuLease(lock).status()
    return {**state, 'eta_seconds': eta_seconds(state, state_dir=STATE_DIR), 'jobs': jobs(),
            'reservation': reservation()}


def show(state):
    now = time.time()
    owner = state.get('owner')
    if not state.get('held'):
        print('GPU free')
    elif owner:
        print(f"GPU held by {owner.get('kind')} \"{owner.get('reason') or owner.get('request_id')}\" "
              f"(pid {owner.get('pid')}) for {ago(now - (owner.get('acquired_at') or now))}")
    else:
        print('GPU held (owner unknown)')
    reserved = state.get('reservation')
    if reserved:
        print(f"  reserved for \"{reserved.get('name')}\" by {reserved.get('holder')}: only its requests use the GPU, "
              f"at most {ago(reserved.get('seconds_left') or 0)} more")
    for job in state['jobs']:
        expected = job.get('expected_seconds')
        line = f"  {job['kind']} {job.get('task') or job.get('reason')}: {ago(job['elapsed_seconds'])}"
        line += f", usually {ago(expected)}" if expected else ', no estimate'
        if job['late']:
            line += ' - LATE'
        elif expected and expected > job['elapsed_seconds']:
            line += f", about {ago(expected - job['elapsed_seconds'])} left"
        if job.get('kill_at'):
            line += f", ends in {ago(job['kill_at'] - now)}"
        idle = job.get('idle') or {}
        line += f"; GPU {idle.get('state', 'unknown')}" + (f" ({idle['avg_watts']} W avg)" if idle.get('avg_watts') else '')
        print(line)
    for ticket in state.get('queue') or []:
        print(f"  waiting: {ticket.get('kind')} \"{ticket.get('reason') or ticket.get('request_id')}\" "
              f"for {ago(now - (ticket.get('since') or now))}"
              + (f", usually {ago(ticket['expected_seconds'])}" if ticket.get('expected_seconds') else ''))
    if state.get('eta_seconds'):
        print(f"text gets the GPU in about {ago(state['eta_seconds'])}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command, rest = (argv[0], argv[1:]) if argv else ('status', [])
    if command == 'status':
        state = status()
        print(json.dumps(state)) if '--json' in rest else show(state)
        return 0
    if command == 'run':
        return supervisor.main(rest)
    if command == 'hold':
        if not rest or rest[-1].startswith('-'):
            print('usage: gpu hold [--reason R] DURATION   (e.g. gpu hold --reason "debug flux" 30m)', file=sys.stderr)
            return 2
        if not sys.stdin.isatty():
            print('gpu hold needs a terminal (ssh -t); non-interactive work uses `gpu run`', file=sys.stderr)
            return 2
        print(f'Holding the GPU for {rest[-1]}; exit this shell to release it.', file=sys.stderr)
        reason = [] if '--reason' in rest else ['--reason', 'hold']
        return supervisor.main([*reason, *rest[:-1], '--kill-after', rest[-1], '--interactive', '--',
                                os.environ.get('SHELL') or 'bash', '-i'])
    print(__doc__, file=sys.stderr)
    return 0 if command in ('-h', '--help', 'help') else 2


if __name__ == '__main__':
    raise SystemExit(main())
