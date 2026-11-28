from tare_node.install import rewrite


def test_gateway_template_renders_service_home_with_spaces():
    import shlex
    from pathlib import Path
    template = (Path(__file__).resolve().parents[1] / "ops/qualified-model-gateway.conf").read_text(encoding="utf-8")
    home = "/srv/service user"
    rendered = rewrite(template, home)
    command = next(line for line in rendered.splitlines() if line.startswith("ExecStart=") and line != "ExecStart=")
    argv = shlex.split(command[len("ExecStart="):])
    assert argv[0] == home + "/.venvs/tare-node/bin/tare-node-gateway"
    assert argv[argv.index("--state-dir") + 1] == home + "/.local/state/tare-qualified-models"
    # The lock `gpu` uses; without it GPU coordination is off and tare-node-update rolls every update back.
    assert argv[argv.index("--gpu-lock") + 1] == home + "/.local/state/tare-qualified-models/gpu.lock"
    assert "--preload" not in argv  # the shared-GPU gateway listens without holding the GPU for a model
    assert "@TARE_NODE_HOME@" not in rendered
    assert rewrite(rendered, home) == rendered

GATEWAY = """[Service]
Environment=PYTHONUNBUFFERED=1
ExecStart=
ExecStart=/usr/bin/python3 /opt/tare.tools.local-labs/tools/serving/qualified_model_gateway.py --config /opt/tare.tools.local-labs/config/qualified_model_fleet.json --host 0.0.0.0 --port 8080 --gpu-lock /home/operator/.local/state/tare-qualified-models/gpu.lock --route-timeout 600 --image-routes /opt/tare.tools.local-labs/config/comfy_image_routes.json --preload qwen38
"""
COMFY = """[Service]
Environment=VIRTUAL_ENV=/home/operator/.venvs/comfyui
ExecStart=
ExecStart=/home/operator/.venvs/comfyui/bin/python /opt/tare.tools.local-labs/tools/serving/coordinated_comfy.py --comfy-root '/home/operator/Comfy UI' --gpu-lock /home/operator/.local/state/tare-qualified-models/gpu.lock -- --listen 0.0.0.0 --port 8188
"""


def test_the_node_services_move_to_the_installed_version_keeping_their_arguments():
    gateway = rewrite(GATEWAY, '/home/operator')
    assert ('ExecStart=/home/operator/.venvs/tare-node/bin/tare-node-gateway --host 0.0.0.0 --port 8080 --gpu-lock '
            '/home/operator/.local/state/tare-qualified-models/gpu.lock --route-timeout 600 --preload qwen38') in gateway
    assert 'local-labs' not in gateway and 'ExecStart=\n' in gateway and 'PYTHONUNBUFFERED=1' in gateway
    comfy = rewrite(COMFY, '/home/operator')
    assert ("ExecStart=/home/operator/.venvs/comfyui/bin/tare-node-comfy --comfy-root '/home/operator/Comfy UI' "
            '--gpu-lock /home/operator/.local/state/tare-qualified-models/gpu.lock -- --listen 0.0.0.0 --port 8188') in comfy
    assert rewrite(gateway, '/home/operator') == gateway and rewrite(comfy, '/home/operator') == comfy  # twice is once
    other = '[Service]\nExecStart=/usr/bin/something --flag\n'
    assert rewrite(other, '/home/operator') == other


def test_the_node_token_is_created_once_and_only_its_owner_reads_it(tmp_path):
    import os
    import stat
    from tare_node.install import ensure_token
    token = tmp_path/'state'/'api-token'
    assert ensure_token(token) is True
    first = token.read_text()
    assert len(first.strip()) >= 40 and ensure_token(token) is False and token.read_text() == first
    if os.name == 'posix':
        assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_install_refuses_symlinks_in_token_and_override_bak(tmp_path):
    import pytest
    from tare_node.install import ensure_token, _write_no_symlink
    outside = tmp_path/'outside.txt'
    outside.write_text('keep')
    bad_token = tmp_path/'api-token'
    bad_token.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        ensure_token(bad_token)
    bad_bak = tmp_path/'override.conf.bak'
    bad_bak.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        _write_no_symlink(bad_bak, 'overwrite')
    assert outside.read_text() == 'keep'


def test_gateway_perimeter_enforces_require_token_all_and_allowed_cidrs(monkeypatch):
    import http.client
    import ipaddress
    import threading
    from http.server import ThreadingHTTPServer
    from tare_node import qualified_model_gateway as qmg

    monkeypatch.setattr(qmg, 'GPU_API_TOKEN', 'secret-node-token')
    monkeypatch.setattr(qmg, 'REQUIRE_TOKEN_ALL', True)
    monkeypatch.setattr(qmg, 'ALLOWED_NETWORKS', (ipaddress.ip_network('100.64.0.0/10'),))
    server = ThreadingHTTPServer(('127.0.0.1', 0), qmg.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        # Loopback not in 100.64.0.0/10 and no token -> 403
        conn.request('GET', '/v1/gpu/power?since=0&until=1')
        assert conn.getresponse().status == 403
        conn.close()
        # With valid token -> bypasses CIDR block and satisfies REQUIRE_TOKEN_ALL -> 200
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request('GET', '/v1/gpu/power?since=0&until=1', headers={'Authorization': 'Bearer secret-node-token'})
        assert conn.getresponse().status == 200
        conn.close()
        # Now allow loopback CIDR, keep REQUIRE_TOKEN_ALL -> missing token still gets 401
        monkeypatch.setattr(qmg, 'ALLOWED_NETWORKS', (ipaddress.ip_network('127.0.0.0/8'),))
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request('GET', '/v1/gpu/power?since=0&until=1')
        assert conn.getresponse().status == 401
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_tare_node_install_apply_sets_up_unprivileged_auto_updates_in_one_command(tmp_path, monkeypatch):
    import os
    import stat
    from tare_node import install, update

    home = tmp_path/'operator'
    home.mkdir()
    systemd = tmp_path/'systemd'
    sudoers = tmp_path/'sudoers.d'
    override_dir = systemd/'llm-inference.service.d'
    override_dir.mkdir(parents=True)
    override = override_dir/'zzz-shared-gpu.conf'
    override.write_text(GATEWAY, encoding='utf-8')

    ran = []
    monkeypatch.setattr(install.subprocess, 'run', lambda argv, check: ran.append(argv))
    monkeypatch.setattr(update.subprocess, 'run', lambda argv, check: ran.append(argv))

    assert install.main([
        '--home', str(home), '--user', 'operator',
        '--systemd', str(systemd), '--sudoers', str(sudoers), '--apply',
    ]) == 0

    assert (override_dir/'zzz-shared-gpu.conf.bak').read_text(encoding='utf-8') == GATEWAY
    assert 'tare-node-gateway' in override.read_text(encoding='utf-8')
    assert (home/'.local/state/tare-qualified-models/api-token').is_file()
    assert (home/'.local/bin/gpu').is_symlink()
    assert (home/'.local/bin/gpu-run').is_symlink()

    service = (systemd/'tare-node-update.service').read_text(encoding='utf-8')
    assert 'User=operator\n' in service
    assert (systemd/'tare-node-update.timer').is_file()
    rule = sudoers/'tare-node-update'
    assert rule.read_text(encoding='utf-8').startswith('operator ALL=(root) NOPASSWD: ')
    if os.name == 'posix':
        assert stat.S_IMODE(rule.stat().st_mode) == 0o440
    assert ['systemctl', 'enable', '--now', 'tare-node-update.timer'] in ran
    assert ran[-1] == ['systemctl', 'restart', 'llm-inference.service']


def test_tare_node_install_no_auto_update_and_rejects_root_user_before_mutating(tmp_path, monkeypatch):
    import pytest
    from tare_node import install

    home = tmp_path/'operator'
    home.mkdir()
    systemd = tmp_path/'systemd'
    sudoers = tmp_path/'sudoers.d'
    override_dir = systemd/'llm-inference.service.d'
    override_dir.mkdir(parents=True)
    override = override_dir/'zzz-shared-gpu.conf'
    override.write_text(GATEWAY, encoding='utf-8')

    # Rejects --user root BEFORE rewriting overrides or creating api-token
    with pytest.raises(ValueError, match='root'):
        install.main([
            '--home', str(home), '--user', 'root',
            '--systemd', str(systemd), '--sudoers', str(sudoers), '--apply',
        ])
    assert override.read_text(encoding='utf-8') == GATEWAY
    assert not (home/'.local/state/tare-qualified-models/api-token').exists()

    # --no-auto-update skips updater units and sudoers
    ran = []
    monkeypatch.setattr(install.subprocess, 'run', lambda argv, check: ran.append(argv))
    assert install.main([
        '--home', str(home), '--user', 'operator',
        '--systemd', str(systemd), '--sudoers', str(sudoers), '--no-auto-update', '--apply',
    ]) == 0
    assert not (systemd/'tare-node-update.service').exists()
    assert not (sudoers/'tare-node-update').exists()
    assert ran == [
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'restart', 'llm-inference.service'],
    ]
