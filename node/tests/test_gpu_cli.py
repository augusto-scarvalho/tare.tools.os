"""The `gpu` command: learned task durations, limits, idle watch, visible queue and text giving way."""
import json
import os
import subprocess
import sys
import time

import pytest

from test_gpu_lease_run import CLI_PATH, ROOT, launch_wrapper, requires_linux, serving, SharedGpuLease, wait_until

from tare_node import gpu_lease_run as supervisor  # noqa: E402
from tare_node.gpu_lease import GpuBusy  # noqa: E402

GPU = ROOT/'tare_node/gpu.py'


def test_durations_and_task_names_are_validated():
    assert [supervisor.duration(v) for v in ('90', '90s', '30m', '2h', '1.5h')] == [90, 90, 1800, 7200, 5400]
    for bad in ('', '10d', '-5m', '0'):
        with pytest.raises(Exception):
            supervisor.duration(bad)
    assert supervisor.task_name('cohort-26.v2') == 'cohort-26.v2'
    with pytest.raises(Exception):
        supervisor.task_name('../x')


def test_estimate_is_the_median_of_successful_runs_only(tmp_path):
    log = tmp_path/'runs.jsonl'
    assert supervisor.estimate('train', log) is None
    for seconds, code in ((100, 0), (5, 1), (300, 0), (200, 0), (900, 143)):
        supervisor.record('train', seconds, code, log)
    supervisor.record('other', 1, 0, log)
    log.write_text(log.read_text() + 'not json\n')
    assert supervisor.estimate('train', log) == 200


def watch(samples, window=600):
    clock = [0.0]
    it = iter(samples)
    w = supervisor.IdleWatch(window=window, every=30, sample=lambda: next(it), clock=lambda: clock[0])
    for _ in samples:
        clock[0] += 30
        assert w.tick()
    return w.state()


def test_idle_needs_a_whole_window_under_the_threshold_and_never_guesses():
    assert watch([(40, 0)] * 10)['state'] == 'unknown'  # 5 min of 10: not yet
    assert watch([(40, 0)] * 20) == {'state': 'idle', 'avg_watts': 40, 'utilization': 0}
    assert watch([(340, 97)] * 20)['state'] == 'busy'
    assert watch([(40, 0)] * 19 + [None])['state'] == 'unknown'  # nvidia-smi failed: not idle
    # ponytail ceiling: a 1-minute burst in 10 idle minutes averages 71 W and reads as idle,
    # which is why only an explicit --idle-release acts on it.
    assert watch([(350, 98)] * 2 + [(40, 0)] * 18)['state'] == 'idle'


@requires_linux
def test_waiting_tickets_are_visible_and_text_gives_way_to_image(tmp_path):
    lock = tmp_path/'gpu.lock'
    ready, done = tmp_path/'ready', tmp_path/'done'
    holder = subprocess.Popen([sys.executable, '-c', f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tare_node.gpu_lease import SharedGpuLease
with SharedGpuLease({str(lock)!r}).hold('image', 'train-1', reason='cohort-26'):
    Path({str(ready)!r}).write_text('1')
    while not Path({str(done)!r}).exists():
        time.sleep(.05)
"""])
    try:
        wait_until(ready.exists)
        state = SharedGpuLease(lock).status()
        assert state['owner']['reason'] == 'cohort-26' and state['queue'] == []
        started = time.monotonic()
        with pytest.raises(GpuBusy):
            with SharedGpuLease(lock).hold('text', 'chat-1', timeout=30, yield_to_image=.5):
                pass
        assert time.monotonic() - started < 5
        assert not list((tmp_path/'gpu.lock.queue').iterdir())  # the text withdrew its ticket
        with pytest.raises(TimeoutError) as plain:  # without yield_to_image text keeps waiting
            with SharedGpuLease(lock).hold('text', 'chat-2', timeout=.3):
                pass
        assert not isinstance(plain.value, GpuBusy)
    finally:
        done.write_text('1')
        holder.wait(timeout=5)


@requires_linux
def test_text_behind_text_keeps_its_place(tmp_path):
    lock = tmp_path/'gpu.lock'
    lease = SharedGpuLease(lock)
    with lease.hold('text', 'first', reason='qwen38-gsq'):
        other = SharedGpuLease(lock)
        with pytest.raises(TimeoutError) as waited:
            with other.hold('text', 'second', timeout=.4, yield_to_image=.1):
                pass
        assert not isinstance(waited.value, GpuBusy)


@requires_linux
def test_a_task_nested_in_a_job_keeps_its_supervision_and_learns_its_duration(tmp_path):
    lock = tmp_path/'gpu.lock'
    env = {**os.environ, 'TARE_GPU_STATE': str(tmp_path/'state')}
    with serving(lock_path=lock) as rt:
        with launch_wrapper(['--gateway', rt.endpoint, '--gpu-lock', str(lock), '--reason', 'outer',
                             sys.executable, str(CLI_PATH), '--task', 'inner-job',
                             sys.executable, '-c', 'import time; time.sleep(.3)'], env=env) as wrapper:
            assert wrapper.wait() == 0
    events = [row['event'] for row in wrapper.events]
    assert events.count('acquired') == 1 and events.count('lease_reused') == 1
    assert events.count('started') == 2 and events.count('processes_reaped') == 2
    rows = [json.loads(line) for line in (tmp_path/'state/runs.jsonl').read_text().splitlines()]
    assert [(r['task'], r['exit_code']) for r in rows] == [('inner-job', 0)] and rows[0]['seconds'] >= .3
    assert not list((tmp_path/'state').glob('active-*.json'))


@requires_linux
def test_hold_gives_a_terminal_shell_that_ends_at_the_deadline(tmp_path):
    import pty
    home = tmp_path
    lock = home/'.local/state/tare-qualified-models/gpu.lock'
    lock.parent.mkdir(parents=True)
    with serving(lock_path=lock) as rt:
        # `gpu hold` uses the default gateway; point the supervisor at the fixture through a shim.
        shim = tmp_path/'gpu_shim.py'
        shim.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
                        f"from tare_node import gpu_lease_run, gpu\n"
                        f"real = gpu_lease_run.main\n"
                        f"gpu.supervisor.main = lambda a: real(['--gateway', {rt.endpoint!r}, *a])\n"
                        f"raise SystemExit(gpu.main())\n")
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(HOME=str(home), TARE_GPU_STATE=str(tmp_path/'state'), SHELL='/bin/bash', PS1='$ ')
            os.execv(sys.executable, [sys.executable, str(shim), 'hold', '--reason', 'debug', '3s'])
        out = b''
        def read_until(marker, timeout=10):
            nonlocal out
            deadline = time.monotonic() + timeout
            while marker not in out and time.monotonic() < deadline:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    break
            return marker in out
        assert read_until(b'"started"')
        os.write(fd, b'tty >/dev/null && echo TTY$((1+1)) $(ps -o stat= -p $$) END\n')
        assert read_until(b' END\r'), out  # the shell owns the terminal (foreground: '+' in stat)
        assert b'+' in out.split(b'TTY2 ')[-1].split(b' END')[0], out
        assert SharedGpuLease(lock).status()['owner']['reason'] == 'debug'
        assert read_until(b'"kill_after"'), out
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert not SharedGpuLease(lock).status()['held']


@requires_linux
def test_status_shows_the_running_job_and_kill_after_stops_it(tmp_path):
    env = {**os.environ, 'TARE_GPU_STATE': str(tmp_path/'state'), 'HOME': str(tmp_path)}
    (tmp_path/'.local/state/tare-qualified-models').mkdir(parents=True)
    lock = tmp_path/'.local/state/tare-qualified-models/gpu.lock'
    with serving(lock_path=lock) as rt:
        with launch_wrapper(['--gateway', rt.endpoint, '--gpu-lock', str(lock), '--task', 'sleepy',
                             '--kill-after', '2s', sys.executable, '-c', 'import time; time.sleep(60)'],
                            env=env) as wrapper:
            wrapper.wait_for_event('started')
            shown = json.loads(subprocess.run([sys.executable, str(GPU), 'status', '--json'], env=env,
                                              capture_output=True, text=True, timeout=10, check=True).stdout)
            assert shown['held'] and shown['owner']['kind'] == 'image' and shown['owner']['reason'] == 'sleepy'
            [job] = shown['jobs']
            assert job['task'] == 'sleepy' and job['kill_at'] and job['idle']['state'] == 'unknown'
            assert wrapper.wait(timeout=10) == 143
            assert wrapper.wait_for_event('kill_after')
    assert not SharedGpuLease(lock).status()['held']
    assert supervisor.estimate('sleepy', tmp_path/'state/runs.jsonl') is None  # a killed run teaches nothing


def test_text_gets_an_estimate_only_when_every_image_job_ahead_has_one(tmp_path, capsys):
    # Contract gpu-lease/1: tare decides to wait or go elsewhere from it, so no estimate beats a made-up one.
    from tare_node.gpu_lease import eta_seconds
    from tare_node import gpu
    (tmp_path/'active-41.json').write_text(json.dumps({'started_at': 100, 'expected_seconds': 300}), encoding='utf-8')
    held = {'held': True, 'owner': {'kind': 'image', 'pid': 41}, 'queue': [
        {'kind': 'text'}, {'kind': 'image', 'expected_seconds': 120}]}
    assert eta_seconds(held, now=200, state_dir=tmp_path) == 200 + 120
    assert eta_seconds(held, now=500, state_dir=tmp_path) is None  # the run is past its estimate
    comfy = {'held': True, 'owner': {'kind': 'image', 'pid': 99}, 'queue': []}  # no job description
    assert eta_seconds(comfy, now=200, state_dir=tmp_path) is None
    unknown = {'held': False, 'owner': None, 'queue': [{'kind': 'image', 'expected_seconds': None}]}
    assert eta_seconds(unknown, now=200, state_dir=tmp_path) is None
    assert eta_seconds({'held': True, 'owner': {'kind': 'text'}, 'queue': []}, state_dir=tmp_path) is None
    gpu.show({'held': True, 'owner': {'kind': 'image', 'reason': 'lora'}, 'eta_seconds': 320,
              'jobs': [{'kind': 'run', 'task': 'lora', 'elapsed_seconds': 100, 'expected_seconds': 300, 'late': False}],
              'queue': [{'kind': 'image', 'reason': 'batch', 'since': time.time(), 'expected_seconds': 120}]})
    shown = capsys.readouterr().out
    assert 'about 3m20s left' in shown and 'usually 2m00s' in shown and 'text gets the GPU in about 5m20s' in shown
