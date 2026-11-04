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
