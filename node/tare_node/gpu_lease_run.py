#!/usr/bin/env python3
"""Run a Linux training command under the gateway/ComfyUI exclusive GPU lease."""
from __future__ import annotations

import argparse
from collections import deque
import contextlib
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
from uuid import uuid4

from tare_node.comfy_gpu import yield_text_backend
from tare_node.gpu_lease import SharedGpuLease, TRAINING_STATE_DIR

LEASE_CONTEXT = 'TARE_GPU_TRAINING_LEASE'
# Running jobs (active-<pid>.json) and finished task durations (runs.jsonl) live here.
STATE_DIR = TRAINING_STATE_DIR
# Twice the RTX 3090 idle floor measured 2026-09-30 (38-45 W at 0%; generating draws 310-360 W).
IDLE_WATTS = float(os.environ.get('TARE_GPU_IDLE_WATTS') or 80)
IDLE_WINDOW, SAMPLE_EVERY, LATE_FACTOR = 600.0, 30.0, 1.5


def inherited_lease(lock_path=None, gateway=None):
    """Reuse only a live image lease held by an ancestor supervising this job.

    An environment flag alone is not authority. Verify the kernel lock, nonce,
    owner PID and ancestry before allowing a nested command to skip acquisition.
    """
    raw = os.environ.get(LEASE_CONTEXT)
    if raw is None:
        return None
    try:
        if len(raw) > 4096:
            raise ValueError()
        context = json.loads(raw)
        if (not isinstance(context, dict) or set(context) != {'path', 'gateway', 'pid', 'nonce'}
                or type(context['pid']) is not int or context['pid'] <= 1
                or not isinstance(context['path'], str) or not Path(context['path']).is_absolute()
                or not isinstance(context['gateway'], str) or not isinstance(context['nonce'], str)):
            raise ValueError()
        if (lock_path is not None and Path(lock_path).resolve() != Path(context['path']).resolve()
                or gateway is not None and gateway.rstrip('/') != context['gateway'].rstrip('/')):
            raise ValueError()
        parent = os.getppid()
        for _ in range(256):
            if parent == context['pid']:
                break
            if parent <= 1:
                raise ValueError()
            stat = (Path('/proc')/str(parent)/'stat').read_text()
            parent = int(stat.rsplit(')', 1)[1].split()[1])
        else:
            raise ValueError()
        lease = SharedGpuLease(context['path'])
        status = lease.status()
        owner = status.get('owner') or {}
        if (not status.get('held') or owner.get('pid') != context['pid']
                or owner.get('kind') != 'image' or not lease.validate_image_lease(context['nonce'])):
            raise ValueError()
        return context
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('Inherited GPU lease is invalid or no longer held; refusing unprotected execution.') from None


def report(event, **fields):
    try:
        print(json.dumps({'event': event, **fields}), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # A closed diagnostic pipe cannot interrupt process cleanup.


def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be finite and greater than zero')
    return value


def duration(value):
    """Seconds from '90', '90s', '30m' or '2h'."""
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smh]?)', value.strip())
    if not match:
        raise argparse.ArgumentTypeError('use a duration like 90s, 30m or 2h')
    return positive_seconds(float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]])


def task_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise argparse.ArgumentTypeError('use letters, digits, dot, dash or underscore (at most 64)')
    return value


def estimate(task, log=None):
    """Median duration of the task's last ten successful runs, lease wait excluded; None without history."""
    log = log or STATE_DIR/'runs.jsonl'
    seconds = []
    with contextlib.suppress(OSError):
        for line in log.read_text(encoding='utf-8').splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError, KeyError):
                row = json.loads(line)
                if row.get('task') == task and row.get('exit_code') == 0:
                    seconds.append(float(row['seconds']))
    return statistics.median(seconds[-10:]) if seconds else None


def record(task, seconds, code, log=None):
    log = log or STATE_DIR/'runs.jsonl'
    with contextlib.suppress(OSError):
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'task': task, 'seconds': round(seconds, 1), 'exit_code': code,
                                  'finished_at': time.time()}) + '\n')


def gpu_sample():
    """(watts, utilization %) from nvidia-smi, or None when it cannot tell."""
    try:
        out = subprocess.run(['nvidia-smi', '--query-gpu=power.draw,utilization.gpu', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        watts, util = (float(x) for x in out.splitlines()[0].split(','))
        return watts, util
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class IdleWatch:
    """Average board power over a window. A job is idle only after a whole window of readings under
    IDLE_WATTS; any failed reading in the window makes it unknown, never idle. Informational unless
    the job asked for --idle-release. ponytail: 30 s samples miss sub-second bursts, so short recurring
    calls can read as idle; that is why release needs an explicit opt-in."""

    def __init__(self, window=IDLE_WINDOW, every=SAMPLE_EVERY, sample=gpu_sample, clock=time.monotonic):
        self.window, self.every, self.sample, self.clock = window, every, sample, clock
        self.started, self.readings = clock(), deque()

    def tick(self):
        """Take a reading when one is due; True when it did."""
        now = self.clock()
        last = self.readings[-1][0] if self.readings else self.started
        if now - last < self.every:
            return False
        self.readings.append((now, self.sample()))
        while now - self.readings[0][0] > self.window:
            self.readings.popleft()
        return True

    def state(self):
        values = [r for _, r in self.readings]
        latest = values[-1][1] if values and values[-1] else None
        if not values or None in values or self.readings[-1][0] - self.started < self.window:
            return {'state': 'unknown', 'avg_watts': None, 'utilization': latest}
        avg = sum(w for w, _ in values) / len(values)
        return {'state': 'idle' if avg < IDLE_WATTS else 'busy', 'avg_watts': round(avg), 'utilization': latest}


def write_state(path, fields):
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(fields), encoding='utf-8')
        os.replace(tmp, path)


def foreground():
    """Popen options giving an interactive child its own process group in front of the terminal."""
    tty = sys.stdin.fileno()
    def claim():
        os.setpgid(0, 0)
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(tty, os.getpgrp())
        signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    return {'preexec_fn': claim}


def take_back_terminal():
    with contextlib.suppress(OSError):
        previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())
        signal.signal(signal.SIGTTOU, previous)


def subreaper(enabled=None):
    """Adopt orphaned grandchildren, including workers that call setsid()."""
    libc = ctypes.CDLL(None, use_errno=True)
    if enabled is None:
        value = ctypes.c_int()
        result = libc.prctl(37, ctypes.byref(value), 0, 0, 0)  # PR_GET_CHILD_SUBREAPER
    else:
        result = libc.prctl(36, int(enabled), 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    if result != 0:
        raise OSError(ctypes.get_errno(), 'Cannot establish training process supervision')
    return value.value if enabled is None else None


def descendants():
    """Read this supervisor's descendants, across every thread's child list."""
    seen, pending = set(), [os.getpid()]
    while pending:
        parent = pending.pop()
        try:
            tasks = list((Path('/proc')/str(parent)/'task').iterdir())
        except FileNotFoundError:
            if parent == os.getpid():
                raise  # Missing procfs is not proof that workers exited.
            continue
        for task in tasks:
            try:
                children = (task/'children').read_text().split()
            except FileNotFoundError:
                continue
            for raw in children:
                pid = int(raw)
                if pid not in seen:
                    seen.add(pid)
                    pending.append(pid)
    return seen


def reap(process):
    """Wait for the root before reaping adopted workers, preserving its exit code."""
    if process.poll() is None:
        return
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return


def signal_tree(process, signum):
    # The process group covers forks racing with enumeration. Adopted/setsid
    # workers are also signalled individually and rescanned during cleanup.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    for pid in descendants():
        try:
            if os.getpgid(pid) != process.pid:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass


def drain(process, signum, grace):
    """Retain ownership until all children are observed exited and reaped.

    The grace period controls escalation, never lease release. Unobservable or
    unkillable children quarantine the supervisor with the lease still held.
    """
    deadline = time.monotonic() + grace
    warned, sent = False, set()
    while True:
        try:
            reap(process)
            live = descendants()
            if process.returncode is not None and not live:
                return
            force = time.monotonic() >= deadline
            current_signal = signal.SIGKILL if force else signum
            if force or live - sent:
                signal_tree(process, current_signal)
                sent |= live
            if force and not warned:
                report('cleanup_wait', message='Grace elapsed; sent SIGKILL. Lease retained until process exit is confirmed.')
                warned = True
        except (OSError, ValueError) as exc:
            if not warned:
                report('cleanup_unconfirmed', error=str(exc), message='Lease retained; process cleanup needs attention.')
                warned = True
        time.sleep(.1)


def supervise(command, pending_signals, grace, *, env=None, job=None, watch=None):
    """Run command until it exits, a signal arrives, or the job's --kill-after/--idle-release fires.

    job: task, reason, kill_after, idle_release, interactive. While it runs, the job is described in
    STATE_DIR/active-<pid>.json for `gpu status`.
    """
    job = job or {}
    kill_after, idle_release, interactive = job.get('kill_after'), job.get('idle_release'), job.get('interactive')
    stop = signal.SIGHUP if interactive else signal.SIGTERM  # an interactive bash ignores SIGTERM
    watch = watch or IdleWatch(window=idle_release or IDLE_WINDOW)
    state = STATE_DIR/f'active-{os.getpid()}.json'
    started, started_at = time.monotonic(), time.time()
    describe = {'pid': os.getpid(), 'task': job.get('task'), 'reason': job.get('reason'),
                'kind': 'hold' if interactive else 'run', 'started_at': started_at,
                'expected_seconds': estimate(job['task']) if job.get('task') else None,
                'kill_at': started_at + kill_after if kill_after else None}
    process, warned = None, False
    try:
        process = subprocess.Popen(command, env=env, **(foreground() if interactive else {'start_new_session': True}))
        report('started', pid=process.pid)
        write_state(state, {**describe, 'idle': watch.state()})
        while process.poll() is None:
            if pending_signals:
                drain(process, pending_signals[0], grace)
                break
            left = kill_after - (time.monotonic() - started) if kill_after else None
            if left is not None and left <= 0:
                report('kill_after', seconds=kill_after)
                drain(process, stop, grace)
                break
            if interactive and left is not None and left <= 300 and not warned:
                warned = True
                with contextlib.suppress(OSError):
                    print(f'\n[gpu] hold ends in {math.ceil(left / 60)} min; save your work.', file=sys.stderr, flush=True)
            if watch.tick():
                idle = watch.state()
                write_state(state, {**describe, 'idle': idle})
                if idle_release and idle['state'] == 'idle':
                    report('idle_release', avg_watts=idle['avg_watts'], window_seconds=idle_release)
                    drain(process, stop, grace)
                    break
            time.sleep(.1)
    finally:
        if process is not None:
            drain(process, pending_signals[0] if pending_signals else stop, grace)
        if interactive:
            take_back_terminal()
        with contextlib.suppress(OSError):
            state.unlink()
    code = process.returncode
    report('processes_reaped', child_exit_code=code)
    code = code if code >= 0 else 128 - code
    if job.get('task'):
        record(job['task'], time.monotonic() - started, code)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', help='gateway URL (default: local serving gateway, or verified parent lease)')
    parser.add_argument('--gpu-lock', type=Path, help='lock file (default: serving lock, or verified parent lease)')
    parser.add_argument('--check-inherited', action='store_true',
                        help='check a supervising parent lease without acquiring the GPU; exit 1 when absent')
    parser.add_argument('--gpu-wait', type=positive_seconds, default=3600,
                        help='maximum acquisition wait in seconds (default: 3600; no runtime limit)')
    parser.add_argument('--stop-grace', type=positive_seconds, default=30,
                        help='seconds after a stop request before SIGKILL; never authorizes lease release')
    parser.add_argument('--task', type=task_name,
                        help='name that learns its usual duration; `gpu status` flags a run that is late')
    parser.add_argument('--reason', default='', help='shown to others in `gpu status`')
    parser.add_argument('--kill-after', type=duration, help='stop the command after this long (30m, 2h)')
    parser.add_argument('--idle-release', type=duration,
                        help='stop the command once the GPU averaged idle power for this long')
    parser.add_argument('--interactive', action='store_true', help=argparse.SUPPRESS)  # `gpu hold`
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.check_inherited and command:
        parser.error('--check-inherited does not execute a command')
    if not args.check_inherited and not command:
        parser.error('supply a training script or command (optionally after --)')
    if command and command[0].endswith('.sh'):
        command = ['bash', *command]
    if not sys.platform.startswith('linux'):
        parser.error('run on the GPU host in Linux/WSL, as the serving user')

    pending_signals = []
    def interrupted(signum, _frame):
        pending_signals.append(signum)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    was_subreaper = None
    request_id = 'training-' + uuid4().hex
    shown = command[1] if command[:1] == ['bash'] and len(command) > 1 else command[0] if command else ''
    reason = (args.reason or args.task or Path(shown).name)[:200]
    job = {'task': args.task, 'reason': reason, 'kill_after': args.kill_after,
           'idle_release': args.idle_release, 'interactive': args.interactive}
    try:
        inherited = inherited_lease(args.gpu_lock, args.gateway)
        if args.check_inherited:
            return 0 if inherited else 1
        if inherited:
            report('lease_reused', supervisor_pid=inherited['pid'])
            if not (args.task or args.kill_after or args.idle_release or args.interactive):
                os.execvpe(command[0], command, os.environ)
            # Nested in a hold or another job: keep this job's name, limits and log under the held lease.
            was_subreaper = subreaper()
            subreaper(True)
            return supervise(command, pending_signals, args.stop_grace, job=job)
        args.gateway = args.gateway or 'http://127.0.0.1:8080'
        args.gpu_lock = args.gpu_lock or Path.home()/'.local/state/tare-qualified-models/gpu.lock'
        was_subreaper = subreaper()
        subreaper(True)
        lease = SharedGpuLease(args.gpu_lock, timeout=args.gpu_wait)
        report('waiting', request_id=request_id, timeout_seconds=args.gpu_wait, lock=str(args.gpu_lock))
        with lease.hold('image', request_id, cancelled=lambda: bool(pending_signals), reason=reason,
                        expected_seconds=estimate(job['task']) if job.get('task') else None) as receipt:
            report('acquired', request_id=request_id)
            yield_text_backend(args.gateway, receipt['nonce'])
            report('text_backend_released', request_id=request_id)
            if pending_signals:
                return 128 + pending_signals[0]
            context = {'path': str(args.gpu_lock.resolve()), 'gateway': args.gateway,
                       'pid': os.getpid(), 'nonce': receipt['nonce']}
            code = supervise(command, pending_signals, args.stop_grace,
                             env={**os.environ, LEASE_CONTEXT: json.dumps(context)}, job=job)
        report('lease_released', request_id=request_id, exit_code=code)
        return code
    except InterruptedError:
        report('cancelled_while_waiting', request_id=request_id)
        return 128 + pending_signals[0] if pending_signals else 130
    except TimeoutError as exc:
        report('acquisition_timeout', request_id=request_id, error=str(exc))
        return 124
    except Exception as exc:
        report('failed', request_id=request_id, error=str(exc))
        return 127 if isinstance(exc, OSError) and exc.errno == errno.ENOENT else 125
    finally:
        if was_subreaper is not None:
            subreaper(was_subreaper)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
