"""tare-node-update: keep the GPU node on the latest tare.tools.os main, as Tare-Update keeps the installed tare.

tare-node-update.timer runs it as root every 30 minutes. It fetches main into ~/src/tare.tools.os, a checkout the
service user owns (main takes only CI-green pull requests; the satellites are not needed), and when main changed what
the service runs (node/tare_node/, node/pyproject.toml; anything else only moves the recorded commit) and the node is
quiet (no text request in flight, no reservation) it builds the wheel, installs it in ~/.venvs/tare-node and
~/.venvs/comfyui, restarts the gateway and waits for it to answer; a gateway that does not come back gets the
previous wheel again. ComfyUI restarts only when its hook changed, and only with no image job (else at a later run).
State, wheels and the last outcome live in ~/.local/state/tare-node/. `--install-units` writes and enables the timer.
"""
import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

GATEWAY = 'http://127.0.0.1:8080'
HOOK_FILES = ('node/tare_node/comfy_gpu.py', 'node/tare_node/coordinated_comfy.py', 'node/tare_node/gpu_lease.py')
SERVICE_FILES = ('node/tare_node', 'node/pyproject.toml')  # what the installed wheel carries; nothing else restarts
SERVICE = """[Unit]
Description=Keep the node on the latest tare.tools.os main (tare-node-update)
After=network-online.target llm-inference.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={home}/.venvs/tare-node/bin/tare-node-update --home {home} --user {user}
"""
TIMER = """[Unit]
Description=Run tare-node-update every 30 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
"""


def quiet(status):
    """(True, None) when restarting the gateway breaks nothing in flight, else (False, why)."""
    coordination = status.get('gpu_coordination') or {}
    if coordination.get('reservation'):
        return False, 'a GPU reservation is active'
    queue = status.get('text_queue') or {}
    if queue.get('running') or queue.get('waiting'):
        return False, 'text requests are in flight'
    return True, None


def image_busy(status):
    """An image or training job holds or waits for the GPU: ComfyUI must not restart now."""
    coordination = status.get('gpu_coordination') or {}
    owner = coordination.get('owner') or {}
    return owner.get('kind') == 'image' or any(
        isinstance(t, dict) and t.get('kind') == 'image' for t in coordination.get('queue') or [])


class Node:
    """The node's side effects, one place to fake in tests."""

    def __init__(self, home, user):
        self.home, self.user = Path(home), user
        self.src, self.state_dir = self.home/'src/tare.tools.os', self.home/'.local/state/tare-node'

    def run(self, *argv, as_user=False):
        if as_user and os.geteuid() == 0:
            argv = ('runuser', '-u', self.user, '--', *argv)
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=600).stdout.strip()

    def latest(self):
        self.run('git', '-C', str(self.src), 'fetch', '--quiet', 'origin', 'main', as_user=True)
        return self.run('git', '-C', str(self.src), 'rev-parse', 'origin/main', as_user=True)

    def changed(self, old, new, paths):
        try:
            diff = self.run('git', '-C', str(self.src), 'diff', '--name-only', old, new, '--', *paths, as_user=True)
        except subprocess.CalledProcessError:  # the recorded commit is not in this checkout (moved, rewritten): rebuild
            return True
        return bool(diff)

    def build(self, commit):
        self.run('git', '-C', str(self.src), 'checkout', '--quiet', '--detach', commit, as_user=True)
        out = self.state_dir/'wheels'/commit[:12]
        self.run(str(self.home/'.venvs/tare-node/bin/pip'), 'wheel', '--quiet', '--no-deps', '-w', str(out),
                 str(self.src/'node'), as_user=True)
        return str(next(out.glob('*.whl')))

    def install(self, wheel):
        for venv in ('tare-node', 'comfyui'):
            self.run(str(self.home/'.venvs'/venv/'bin/pip'), 'install', '--quiet', '--no-deps', '--force-reinstall', wheel,
                     as_user=True)

    def restart(self, unit):
        self.run('systemctl', 'restart', unit)

    def status(self):
        with urlopen(GATEWAY + '/v1/fleet/status', timeout=5) as response:
            return json.load(response)

    def healthy(self, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = self.status()
                if value.get('role') == 'qualified-model-gateway' and (value.get('gpu_coordination') or {}).get('enabled'):
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(1)
        return False

    def load(self):
        try:
            return json.loads((self.state_dir/'installed.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def save(self, value):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir/'installed.json'
        path.write_text(json.dumps(value, indent=1), encoding='utf-8')
        if os.geteuid() == 0:
            self.run('chown', '-R', f'{self.user}:{self.user}', str(self.state_dir))


def update(node):
    """One run: (exit code, what happened)."""
    state = node.load()
    latest = node.latest()
    if latest == state.get('commit'):
        if state.get('comfy_pending') and not image_busy(node.status()):
            node.restart('comfyui.service')
            node.save({**state, 'comfy_pending': False})
            return 0, f'up to date at {latest[:7]}; ComfyUI restarted'
        return 0, f'up to date at {latest[:7]}'
    if state.get('commit') and not node.changed(state['commit'], latest, SERVICE_FILES):
        node.save({**state, 'commit': latest})
        return 0, f'at {latest[:7]}; nothing the service runs changed'
    ok, why = quiet(node.status())
    if not ok:
        return 0, f'{latest[:7]} waits: {why}'
    wheel = node.build(latest)
    node.install(wheel)
    node.restart('llm-inference.service')
    if not node.healthy():
        if state.get('wheel'):
            node.install(state['wheel'])
            node.restart('llm-inference.service')
        node.save({**state, 'failed': latest, 'failed_at': time.time()})
        return 1, f'{latest[:7]} did not come up; back on {str(state.get("commit"))[:7]}'
    comfy = not state.get('commit') or node.changed(state['commit'], latest, HOOK_FILES)
    restarted = comfy and not image_busy(node.status())
    if restarted:
        node.restart('comfyui.service')
    node.save({'commit': latest, 'wheel': wheel, 'installed_at': time.time(), 'comfy_pending': comfy and not restarted})
    return 0, f'installed {latest[:7]}' + ('; ComfyUI restarted' if restarted else '; ComfyUI later' if comfy else '')


def install_units(home, user, systemd=Path('/etc/systemd/system')):
    (systemd/'tare-node-update.service').write_text(SERVICE.format(home=home, user=user), encoding='utf-8')
    (systemd/'tare-node-update.timer').write_text(TIMER, encoding='utf-8')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'tare-node-update.timer'], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare-node-update', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', default=str(Path.home()))
    parser.add_argument('--user', default=os.environ.get('SUDO_USER') or os.environ.get('USER') or getpass.getuser())
    parser.add_argument('--install-units', action='store_true', help='write and enable tare-node-update.timer (root)')
    args = parser.parse_args(argv)
    if args.install_units:
        install_units(args.home, args.user)
        print('tare-node-update.timer enabled: every 30 minutes')
        return 0
    try:
        code, outcome = update(Node(args.home, args.user))
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration) as exc:
        code, outcome = 1, f'failed: {type(exc).__name__}: {getattr(exc, "stderr", None) or exc}'
    print(outcome)
    return code


if __name__ == '__main__':
    sys.exit(main())
