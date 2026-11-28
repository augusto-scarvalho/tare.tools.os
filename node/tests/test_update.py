"""tare-node-update's decisions against a fake node: nothing runs git, pip or systemctl."""
from tare_node import update as updater
import pytest

QUIET = {'role': 'qualified-model-gateway', 'gpu_coordination': {'enabled': True, 'held': False, 'queue': []},
         'text_queue': {'running': None, 'waiting': []}}


class FakeNode:
    def __init__(self, state, latest='b' * 40, statuses=None, healthy=True, hook_changed=False, service_changed=True,
                 verified=True):
        self.state, self._latest, self.statuses = dict(state), latest, list(statuses or [QUIET])
        self._healthy, self._hook, self._service, self._verified, self.done = (
            healthy, hook_changed, service_changed, verified, []
        )

    def latest(self):
        return self._latest

    def verified(self, commit):
        return self._verified

    def status(self):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def changed(self, old, new, paths):
        return {updater.HOOK_FILES: self._hook, updater.SERVICE_FILES: self._service}[tuple(paths)]

    def build(self, commit):
        self.done.append(('build', commit[:7]))
        return f'/w/{commit[:7]}.whl'

    def install(self, wheel):
        self.done.append(('install', wheel))

    def restart(self, unit):
        self.done.append(('restart', unit))

    def healthy(self, seconds=60):
        return self._healthy

    def load(self):
        return dict(self.state)

    def save(self, value):
        self.state = value


def test_a_new_main_is_installed_and_the_gateway_restarted_when_the_node_is_quiet():
    node = FakeNode({'commit': 'a' * 40, 'wheel': '/w/aaaaaaa.whl'})
    code, said = updater.update(node)
    assert code == 0 and said == 'installed bbbbbbb'
    assert node.done == [('build', 'bbbbbbb'), ('install', '/w/bbbbbbb.whl'), ('restart', 'llm-inference.service')]
    assert node.state['commit'] == 'b' * 40 and node.state['wheel'] == '/w/bbbbbbb.whl' and not node.state['comfy_pending']


def test_nothing_happens_when_the_node_is_up_to_date():
    node = FakeNode({'commit': 'b' * 40})
    assert updater.update(node) == (0, 'up to date at bbbbbbb') and node.done == []


def test_a_main_that_changes_only_tests_or_docs_restarts_nothing():
    busy = {**QUIET, 'text_queue': {'running': {'model': 'qwen38'}, 'waiting': []}}
    node = FakeNode({'commit': 'a' * 40, 'wheel': '/w/aaaaaaa.whl'}, statuses=[busy], service_changed=False)
    assert updater.update(node) == (0, 'at bbbbbbb; nothing the service runs changed') and node.done == []
    assert node.state == {'commit': 'b' * 40, 'wheel': '/w/aaaaaaa.whl'}


def test_an_update_waits_for_requests_in_flight_and_reservations():
    busy = {**QUIET, 'text_queue': {'running': {'model': 'qwen38'}, 'waiting': []}}
    node = FakeNode({'commit': 'a' * 40}, statuses=[busy])
    assert updater.update(node) == (0, 'bbbbbbb waits: text requests are in flight') and node.done == []
    reserved = {**QUIET, 'gpu_coordination': {'enabled': True, 'reservation': {'name': 'bench'}}}
    node = FakeNode({'commit': 'a' * 40}, statuses=[reserved])
    assert updater.update(node)[1] == 'bbbbbbb waits: a GPU reservation is active' and node.done == []


def test_a_gateway_that_does_not_come_back_gets_the_previous_wheel():
    node = FakeNode({'commit': 'a' * 40, 'wheel': '/w/aaaaaaa.whl'}, healthy=False)
    code, said = updater.update(node)
    assert code == 1 and 'back on aaaaaaa' in said
    assert node.done[-2:] == [('install', '/w/aaaaaaa.whl'), ('restart', 'llm-inference.service')]
    assert node.state['commit'] == 'a' * 40 and node.state['failed'] == 'b' * 40


def test_comfyui_restarts_only_for_a_changed_hook_and_never_under_an_image_job():
    node = FakeNode({'commit': 'a' * 40}, hook_changed=True)
    assert updater.update(node)[1] == 'installed bbbbbbb; ComfyUI restarted'
    assert ('restart', 'comfyui.service') in node.done
    image = {**QUIET, 'gpu_coordination': {'enabled': True, 'held': True, 'owner': {'kind': 'image'}}}
    node = FakeNode({'commit': 'a' * 40}, statuses=[QUIET, image], hook_changed=True)
    assert updater.update(node)[1] == 'installed bbbbbbb; ComfyUI later' and node.state['comfy_pending']
    assert ('restart', 'comfyui.service') not in node.done
    later = FakeNode(node.state)  # the next run, the image job gone
    assert updater.update(later)[1] == 'up to date at bbbbbbb; ComfyUI restarted' and not later.state['comfy_pending']


def test_an_unverified_commit_never_builds_or_installs():
    node = FakeNode({'commit': 'a' * 40, 'wheel': '/w/aaaaaaa.whl'}, verified=False)
    code, said = updater.update(node)
    assert code == 0 and 'CI check not yet verified' in said
    assert node.done == []
    assert node.state['commit'] == 'a' * 40


def test_the_timer_runs_every_half_hour_as_the_service_user(tmp_path, monkeypatch):
    import os
    import stat
    ran = []
    monkeypatch.setattr(updater.subprocess, 'run', lambda argv, check: ran.append(argv))
    systemd, sudoers = tmp_path/'systemd', tmp_path/'sudoers.d'
    updater.install_units('/home/operator', 'operator', systemd=systemd, sudoers=sudoers)
    service = (systemd/'tare-node-update.service').read_text()
    assert 'User=operator\n' in service
    assert 'ExecStart=/home/operator/.venvs/tare-node/bin/tare-node-update --home /home/operator --user operator' in service
    assert 'OnUnitActiveSec=30min' in (systemd/'tare-node-update.timer').read_text()
    rule = sudoers/'tare-node-update'
    rule_text = rule.read_text()
    assert rule_text.startswith('operator ALL=(root) NOPASSWD: ')
    assert 'llm-inference.service' in rule_text and 'comfyui.service' in rule_text
    if os.name == 'posix':
        assert stat.S_IMODE(rule.stat().st_mode) == 0o440
    assert ran[-1] == ['systemctl', 'enable', '--now', 'tare-node-update.timer']


def test_update_refuses_running_as_root_and_rejects_bad_user_or_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0, raising=False)
    assert updater.main(['--home', str(tmp_path), '--user', 'operator']) == 1
    with pytest.raises(ValueError, match='invalid service user'):
        updater.install_units(tmp_path, 'operator; rm -rf /', systemd=tmp_path/'s', sudoers=tmp_path/'d')
    node = updater.Node(tmp_path, 'operator')
    with pytest.raises(ValueError, match='unauthorized systemd unit'):
        node.restart('ssh.service')


def test_node_save_rejects_symlink_target(tmp_path):
    node = updater.Node(tmp_path, 'operator')
    node.state_dir.mkdir(parents=True)
    outside = tmp_path/'outside.txt'
    outside.write_text('keep')
    (node.state_dir/'installed.json').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        node.save({'commit': 'a' * 40})
    assert outside.read_text() == 'keep'


def test_node_verified_requires_completed_success_and_rejects_failures(monkeypatch):
    import io

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def serve_runs(runs):
        payload = updater.json.dumps({'check_runs': runs}).encode('utf-8')
        monkeypatch.setattr(updater, 'urlopen', lambda req, timeout=15: FakeResp(payload))

    node = updater.Node('/tmp/home', 'operator')
    commit = 'a' * 40
    serve_runs([{'name': updater.REQUIRED_CHECK, 'status': 'completed', 'conclusion': 'success'}])
    assert node.verified(commit) is True
    serve_runs([
        {'name': updater.REQUIRED_CHECK, 'status': 'completed', 'conclusion': 'success'},
        {'name': 'Tests (windows)', 'status': 'completed', 'conclusion': 'failure'},
    ])
    assert node.verified(commit) is False
    serve_runs([{'name': updater.REQUIRED_CHECK, 'status': 'in_progress', 'conclusion': None}])
    assert node.verified(commit) is False
    assert node.verified('not-a-sha') is False


@pytest.mark.parametrize(('sudo_user', 'user', 'expected'), [
    ('service-owner', 'root', 'service-owner'),
    (None, 'service-user', 'service-user'),
    (None, None, 'detected-user'),
])
def test_update_cli_uses_the_detected_service_user(monkeypatch, sudo_user, user, expected):
    for key, value in (('SUDO_USER', sudo_user), ('USER', user)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(updater.getpass, 'getuser', lambda: 'detected-user')
    installed = []
    monkeypatch.setattr(updater, 'install_units', lambda home, owner: installed.append((home, owner)))
    assert updater.main(['--install-units', '--home', '/srv/service']) == 0
    assert installed == [('/srv/service', expected)]
