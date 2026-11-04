# tare.tools node service

The service that runs on a GPU node of tare.tools: the model gateway (`/v1/chat/completions`, `/v1/fleet/status`,
image jobs), the shared GPU lease with its `gpu` command, and the ComfyUI hook that takes the same lease. Text models,
image jobs and training take turns on one GPU; tare reads the node's status and refusals (contract `gpu-lease`,
tare.tools.kernel `docs/GPU_LEASE_CONTRACT.md`) and decides where a request goes
([`docs/guides/SHARED_GPU.md`](../docs/guides/SHARED_GPU.md)).

This folder is a Python package of its own (`tare-tools-node`, standard library only) inside tare.tools.os. `relay/`
talks to a running node over HTTP and never imports it, and nothing here imports `relay/` or a satellite. A node is a
Linux machine (or WSL2 on a Windows workstation) with systemd, an NVIDIA GPU, the inference servers named in the
catalog and, optionally, ComfyUI. The service came from tare.tools.local-labs, which keeps the experiments and the
qualification evidence the catalog cites.

## Layout

- `tare_node/`: `qualified_model_gateway.py`, `gpu_lease.py` (flock lock, queue tickets, text idle grace, ETA),
  `gpu.py` and `gpu_lease_run.py` (`gpu status|run|hold`), `comfy_gpu.py` and `coordinated_comfy.py` (ComfyUI),
  `comfy_routes.py`, `qualified_fleet.py` (our catalog, the owner's fleet), `fleet_count.py`, `install.py`, and
  `config/` (the catalog of qualified models, image routes).
- `ops/`: systemd drop-ins and the Windows firewall rule for Tailscale.
- `docs/SHARED_GPU.md`: operating the shared GPU; `docs/QUALIFIED_MODEL_FLEET.md`: the catalog and the
  owner's fleet (`~/.config/tare-node/fleet.json`: which models the node serves is the owner's choice).

## Develop and test

You need Python 3.10 or newer and Git; the service has no dependencies beyond the standard library. The lease tests
need Linux `flock`, so run them on Linux or in WSL (CI runs them on Ubuntu, workflow `node.yml`); on Windows those
tests fail. The satellites are not needed.

```bash
git clone https://github.com/augusto-scarvalho/tare.tools.os.git
cd tare.tools.os/node
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pytest -e .
python -m pytest -q
```

The tests use disposable state: they download no model, run no inference and touch no installed service.

## Install on the node (with the owner)

```bash
git clone https://github.com/augusto-scarvalho/tare.tools.os.git ~/src/tare.tools.os   # no submodules needed
python3 -m venv ~/.venvs/tare-node && ~/.venvs/tare-node/bin/pip install --no-deps ~/src/tare.tools.os/node
~/.venvs/comfyui/bin/pip install --no-deps ~/src/tare.tools.os/node  # the ComfyUI hook runs in ComfyUI's Python
~/.venvs/tare-node/bin/tare-node-install                             # prints the overrides it would write
sudo ~/.venvs/tare-node/bin/tare-node-install --home "$HOME" --apply  # .bak kept; restarts the gateway
```

Restart ComfyUI by hand once `gpu status` shows no image job. Rollback: restore the `.bak` overrides, `systemctl
daemon-reload`, restart. The lock path does not change, so old and new code exclude each other.

Catalog locations beginning with `~/` resolve under the service user's home when the registry is loaded, and
`@TARE_NODE_PACKAGE@/` inside the installed package (the qwen38 chat template ships there).
For a different model or binary layout, pass an absolute-path catalog with `tare-node-gateway --config`.
The gateway state defaults to `~/.local/state/tare-qualified-models`; `--state-dir` overrides it.

The systemd example in `ops/qualified-model-gateway.conf` is a template. Render and inspect it before installation:

```bash
python3 - "$HOME" <<'PY'
from pathlib import Path
import sys
from tare_node.install import rewrite
print(rewrite(Path("ops/qualified-model-gateway.conf").read_text(), sys.argv[1]), end="")
PY
```

Run this from the checkout with the installed node Python. The printed override uses the chosen service home;
keep the installed service's state and GPU-lock paths when adapting an existing deployment.

## Updates

The node keeps itself on the latest tare.tools.os `main` (which takes only CI-green pull requests), as the installed
tare keeps itself current: `tare-node-update.timer` (root, every 30 minutes) runs `tare-node-update`, which fetches
`~/src/tare.tools.os`, and when `main` changed `node/tare_node/` or `node/pyproject.toml` and no text request or
reservation is in flight builds the wheel, installs it in both venvs, restarts the gateway and checks it answers;
a gateway that does not come back gets the previous wheel again. ComfyUI restarts only when its hook changed and no
image job runs. `journalctl -u tare-node-update` shows each run; state and wheels are in `~/.local/state/tare-node/`.
Set up once with `sudo ~/.venvs/tare-node/bin/tare-node-update --install-units`.

## Development

This folder distributes service code, portable templates, catalog metadata, maintained guides and synthetic tests.
Operator fleet/state files, credentials, model weights and raw experiment receipts remain in private installation or
owner custody. Tests use disposable state and do not require that private data. Changes follow tare.tools.os
[`AGENTS.md`](../AGENTS.md) and its changelog.

## License

The node service code is licensed under [Apache-2.0](../LICENSE). Model weights,
ComfyUI, inference engines and other third-party tools retain their own licenses.
