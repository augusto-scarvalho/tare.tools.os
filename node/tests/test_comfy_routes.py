"""Tests for gateway ComfyUI image routing, coordination, lifecycle, and safety boundaries."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from threading import Event, RLock, Thread
import time
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tare_node.comfy_routes import (
    ComfyBackendError,
    ComfyRouteError,
    CoordinationError,
    ImageValidationError,
    OutputNotFoundError,
    UncertainSubmissionError,
    build_sdxl_workflow,
    extract_job_outputs,
    get_image_public_cards,
    load_image_routes,
    validate_comfy_url,
    validate_filename_and_subfolder,
    validate_job_id,
)
from tare_node.gpu_lease import SharedGpuLease

spec = importlib.util.spec_from_file_location(
    "gateway_module_for_image_tests", ROOT / "tare_node/qualified_model_gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


# ---------------------------------------------------------------------------
# Unit tests: Workflow builder, validation, and extraction helpers
# ---------------------------------------------------------------------------

def test_sdxl_workflow_builder_defaults():
    checkpoint = "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"
    wf = build_sdxl_workflow(
        preset_checkpoint=checkpoint,
        prompt="a photo of an astronaut on mars",
    )
    assert wf["1"]["class_type"] == "CheckpointLoaderSimple"
    assert wf["1"]["inputs"]["ckpt_name"] == checkpoint

    assert wf["2"]["class_type"] == "CLIPTextEncode"
    assert wf["2"]["inputs"]["text"] == "a photo of an astronaut on mars"
    assert wf["2"]["inputs"]["clip"] == ["1", 1]

    assert wf["3"]["class_type"] == "CLIPTextEncode"
    assert wf["3"]["inputs"]["text"] == ""
    assert wf["3"]["inputs"]["clip"] == ["1", 1]

    assert wf["4"]["class_type"] == "EmptyLatentImage"
    assert wf["4"]["inputs"]["width"] == 512
    assert wf["4"]["inputs"]["height"] == 512
    assert wf["4"]["inputs"]["batch_size"] == 1

    assert wf["5"]["class_type"] == "KSampler"
    assert wf["5"]["inputs"]["steps"] == 20
    assert wf["5"]["inputs"]["cfg"] == 7.0
    assert wf["5"]["inputs"]["sampler_name"] == "euler"
    assert wf["5"]["inputs"]["scheduler"] == "normal"
    assert wf["5"]["inputs"]["denoise"] == 1.0
    assert wf["5"]["inputs"]["model"] == ["1", 0]
    assert wf["5"]["inputs"]["positive"] == ["2", 0]
    assert wf["5"]["inputs"]["negative"] == ["3", 0]
    assert wf["5"]["inputs"]["latent_image"] == ["4", 0]
    assert 0 <= wf["5"]["inputs"]["seed"] <= (2**63 - 1)

    assert wf["6"]["class_type"] == "VAEDecode"
    assert wf["6"]["inputs"]["samples"] == ["5", 0]
    assert wf["6"]["inputs"]["vae"] == ["1", 2]

    assert wf["7"]["class_type"] == "SaveImage"
    assert wf["7"]["inputs"]["filename_prefix"] == "tare"
    assert wf["7"]["inputs"]["images"] == ["6", 0]


def test_sdxl_workflow_builder_validation_errors():
    ckpt = "Illustrious-XL-v0.1.safetensors"

    # Reject empty prompt
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "")
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "   ")
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, None)  # type: ignore

    # Reject prompt too long
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "a" * 4097)

    # Reject negative_prompt too long
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", negative_prompt="b" * 4097)

    # Reject non-divisible by 64 dimensions
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", width=500, height=512)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", width=512, height=500)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", width=0, height=512)

    # Reject total pixels over budget (1024x1024 = 1048576)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", width=1024, height=1088)

    # Reject invalid steps
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", steps=0)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", steps=51)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", steps=True)  # type: ignore (reject bool)

    # Reject invalid seed
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", seed=-1)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", seed=2**63)
    with pytest.raises(ImageValidationError):
        build_sdxl_workflow(ckpt, "prompt", seed=True)  # type: ignore (reject bool)


def test_validate_comfy_url():
    assert validate_comfy_url("http://127.0.0.1:8188") == "http://127.0.0.1:8188"
    assert validate_comfy_url("http://localhost:8188/") == "http://localhost:8188"
    # Preserves brackets for IPv6 loopback
    assert validate_comfy_url("http://[::1]:8188") == "http://[::1]:8188"

    # Must be loopback HTTP
    with pytest.raises(ImageValidationError):
        validate_comfy_url("https://127.0.0.1:8188")
    with pytest.raises(ImageValidationError):
        validate_comfy_url("http://192.168.1.50:8188")
    with pytest.raises(ImageValidationError):
        validate_comfy_url("http://user:pass@127.0.0.1:8188")
    with pytest.raises(ImageValidationError):
        validate_comfy_url("http://127.0.0.1:8188/api")
    with pytest.raises(ImageValidationError):
        validate_comfy_url("http://127.0.0.1")  # missing port


def test_validate_job_id_strict_canonical():
    test_uuid = str(uuid.uuid4()).lower()
    assert validate_job_id(test_uuid) == test_uuid

    # No uppercase normalization allowed
    with pytest.raises(ImageValidationError):
        validate_job_id(test_uuid.upper())

    # No whitespace normalization allowed
    with pytest.raises(ImageValidationError):
        validate_job_id(f" {test_uuid} ")

    # No non-hyphenated hex format
    with pytest.raises(ImageValidationError):
        validate_job_id(uuid.uuid4().hex)

    with pytest.raises(ImageValidationError):
        validate_job_id("not-a-uuid")
    with pytest.raises(ImageValidationError):
        validate_job_id("")
    with pytest.raises(ImageValidationError):
        validate_job_id(12345)


def test_extract_job_outputs_mixed_nodes_and_missing_type():
    job_id = str(uuid.uuid4()).lower()
    entry = {
        "outputs": {
            "save_node": {
                "images": [
                    {"filename": "save.png", "subfolder": "", "type": "output"},
                ]
            },
            "7": {
                "images": [
                    {"filename": "out7.png", "subfolder": "sub", "type": "output"},
                    {"filename": "missing_type.png", "subfolder": ""},  # Missing type => excluded
                    {"filename": "temp.png", "subfolder": "", "type": "temp"},  # Non-output => excluded
                ]
            },
            "2": {
                "images": [
                    {"filename": "out2.png", "subfolder": "", "type": "output"},
                ]
            },
            "alpha": {
                "images": [
                    {"filename": "alpha.png", "subfolder": "", "type": "output"},
                ]
            },
        }
    }

    # Stable total ordering: numeric IDs first (2, 7), then string IDs ('alpha', 'save_node')
    outputs = extract_job_outputs(job_id, entry)
    assert len(outputs) == 4
    filenames = [o["filename"] for o in outputs]
    assert filenames == ["out2.png", "out7.png", "alpha.png", "save.png"]
    assert outputs[0]["index"] == 0
    assert outputs[0]["url"] == f"/v1/images/jobs/{job_id}/outputs/0"


def test_validate_filename_and_subfolder():
    # Valid
    f, s = validate_filename_and_subfolder("image.png", "sub/folder")
    assert f == "image.png"
    assert s == "sub/folder"

    # Colon in filename
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("image:1.png", "")

    # Slashes in filename
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("sub/image.png", "")
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("sub\\image.png", "")

    # Control characters in filename
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("image\x00.png", "")

    # Colon in subfolder
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("image.png", "C:/sub")

    # Traversal in subfolder
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("image.png", "../secret")
    with pytest.raises(ImageValidationError):
        validate_filename_and_subfolder("image.png", "sub/../secret")


def test_public_cards_no_qualified_for():
    routes = {
        "engine": "comfyui",
        "presets": {
            "juggernaut-xl": {
                "display_name": "Juggernaut XL",
                "summary": "SDXL",
                "checkpoint": "jugg.safetensors",
            }
        },
    }
    cards = get_image_public_cards(routes)
    for card in cards:
        assert card["qualification"] == "unassessed"
        assert "capabilities" in card
        assert "intended_roles" in card
        # Must not claim qualified_for while unassessed
        assert "qualified_for" not in card


# ---------------------------------------------------------------------------
# Integration fixtures: Mock ComfyUI and Gateway HTTP servers
# ---------------------------------------------------------------------------

class MockComfyUI(BaseHTTPRequestHandler):
    """Mock ComfyUI server simulating /tare/gpu/status, /prompt, /queue, /history, /view, /models, /cancel."""

    coordinator_role = "tare-comfy-gpu-coordinator"
    cleanup_blocked: Any = False
    gpu_enabled: Any = True
    gpu_lock_path: str = ""
    prompt_fail_code: int = 0
    prompt_fail_body: dict[str, Any] = {}
    prompt_timeout = False
    view_mime = "image/png"

    queue_running: list[list[Any]] = []
    queue_pending: list[list[Any]] = []
    history: dict[str, Any] = {}
    cancelled_jobs: list[str] = []
    submitted_prompts: dict[str, Any] = {}

    def log_message(self, *args):
        pass

    def reply_json(self, status: int, data: Any):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/tare/gpu/status":
            resp: dict[str, Any] = {
                "role": self.coordinator_role,
            }
            if self.cleanup_blocked is not None:
                resp["cleanup_blocked"] = self.cleanup_blocked
            if self.gpu_enabled is not None:
                resp["gpu"] = {
                    "enabled": self.gpu_enabled,
                    "path": self.gpu_lock_path,
                }
            return self.reply_json(200, resp)

        if path == "/queue":
            return self.reply_json(200, {
                "queue_running": self.queue_running,
                "queue_pending": self.queue_pending,
            })

        if path.startswith("/history/"):
            prompt_id = path.split("/history/", 1)[1]
            if prompt_id in self.history:
                return self.reply_json(200, {prompt_id: self.history[prompt_id]})
            return self.reply_json(200, {})

        if path == "/view":
            query = urllib_parse.parse_qs(urllib_parse.urlsplit(self.path).query)
            filename = query.get("filename", [""])[0]

            if "huge" in filename:
                # Exceeds 32MiB budget
                huge_data = b"0" * (33 * 1024 * 1024)
                self.send_response(200)
                self.send_header("Content-Type", self.view_mime)
                self.send_header("Content-Length", str(len(huge_data)))
                self.end_headers()
                self.wfile.write(huge_data)
                return

            # Normal small 1x1 png image
            img_bytes = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
                b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            self.send_response(200)
            self.send_header("Content-Type", self.view_mime)
            self.send_header("Content-Length", str(len(img_bytes)))
            self.end_headers()
            self.wfile.write(img_bytes)
            return

        if path == "/models/checkpoints":
            return self.reply_json(200, [
                "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
                "Illustrious-XL-v0.1.safetensors",
            ])
        if path == "/models/diffusion_models":
            return self.reply_json(200, ["z-image.safetensors"])
        if path == "/models/vae":
            return self.reply_json(200, ["sdxl_vae.safetensors"])
        if path == "/models/text_encoders":
            return self.reply_json(200, ["clip_g.safetensors"])

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/prompt":
            if self.prompt_timeout:
                time.sleep(2)
                return
            if self.prompt_fail_code > 0:
                return self.reply_json(self.prompt_fail_code, self.prompt_fail_body)

            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            prompt_id = data.get("prompt_id")
            self.submitted_prompts[prompt_id] = data.get("prompt")
            return self.reply_json(200, {"prompt_id": prompt_id, "number": 1, "node_errors": {}})

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/api/jobs/", 1)[1][: -len("/cancel")].strip("/")
            self.cancelled_jobs.append(job_id)
            return self.reply_json(200, {"cancelled": True})

        self.send_response(404)
        self.end_headers()


@contextmanager
def running_test_env(tmp_path: Path, *, enable_comfy: bool = True):
    lock_file = tmp_path / "shared_gpu.lock"
    routes_file = tmp_path / "comfy_image_routes.json"
    routes_file.write_text(json.dumps({
        "schema_version": 1,
        "engine": "comfyui",
        "presets": {
            "juggernaut-xl": {
                "display_name": "Juggernaut XL v9",
                "checkpoint": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
                "summary": "SDXL photo checkpoint preset routed to ComfyUI.",
                "default_width": 512,
                "default_height": 512,
                "default_steps": 20,
            },
            "illustrious-xl": {
                "display_name": "Illustrious XL v0.1",
                "checkpoint": "Illustrious-XL-v0.1.safetensors",
                "summary": "Illustrious XL checkpoint preset routed to ComfyUI.",
                "default_width": 512,
                "default_height": 512,
                "default_steps": 20,
            },
        }
    }))

    # Reset mock comfy state
    MockComfyUI.coordinator_role = "tare-comfy-gpu-coordinator"
    MockComfyUI.cleanup_blocked = False
    MockComfyUI.gpu_enabled = True
    MockComfyUI.gpu_lock_path = str(lock_file)
    MockComfyUI.prompt_fail_code = 0
    MockComfyUI.prompt_fail_body = {}
    MockComfyUI.prompt_timeout = False
    MockComfyUI.view_mime = "image/png"
    MockComfyUI.queue_running = []
    MockComfyUI.queue_pending = []
    MockComfyUI.history = {}
    MockComfyUI.cancelled_jobs = []
    MockComfyUI.submitted_prompts = {}

    comfy_server = ThreadingHTTPServer(("127.0.0.1", 0), MockComfyUI)
    comfy_port = comfy_server.server_port
    comfy_thread = Thread(target=comfy_server.serve_forever, daemon=True)
    comfy_thread.start()

    comfy_url = f"http://127.0.0.1:{comfy_port}" if enable_comfy else None

    # Minimal fleet config
    config = {
        "fleet": {"default_model": "test-model", "max_resident_models": 1},
        "aliases": {},
        "models": {
            "test-model": {
                "display_name": "Test Text Model",
                "qualification": "promoted",
                "qualified_for": ["general"],
                "not_for": ["code"],
                "modalities": ["text"],
                "summary": "A mock text model.",
                "limits": {"context_window": 4096},
                "artifact": {"path": "/models/test-model", "sha256": "0" * 64, "quant": "Q4"},
                "runtime": {"binary": "/bin/llama-server", "args": [], "environment": {}},
                "evidence": [],
            }
        },
    }

    # Initialize runtime
    runtime = gateway.FleetRuntime(
        config,
        backend_host="127.0.0.1",
        backend_port=19999,
        state_dir=tmp_path / "state",
        load_timeout=10.0,
        stop_timeout=5.0,
        gpu_lock=lock_file,
        route_timeout=5.0,
        comfy_url=comfy_url,
        image_routes_path=routes_file if enable_comfy else None,
    )

    old_runtime = getattr(gateway, "RUNTIME", None)
    gateway.RUNTIME = runtime

    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    gateway_port = gateway_server.server_port
    gateway_thread = Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()

    base_url = f"http://127.0.0.1:{gateway_port}"

    try:
        yield {
            "base_url": base_url,
            "comfy_url": comfy_url,
            "lock_file": lock_file,
            "runtime": runtime,
            "mock_comfy": MockComfyUI,
        }
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        comfy_server.shutdown()
        comfy_server.server_close()
        gateway_thread.join(timeout=2)
        comfy_thread.join(timeout=2)
        gateway.RUNTIME = old_runtime


def http_get(base_url: str, path: str) -> tuple[int, Any, dict[str, str]]:
    req = urllib_request.Request(f"{base_url}{path}")
    try:
        with urllib_request.urlopen(req, timeout=5) as resp:
            headers = dict(resp.headers)
            body = resp.read()
            if headers.get("Content-Type", "").startswith("application/json"):
                return resp.status, json.loads(body.decode("utf-8")), headers
            return resp.status, body, headers
    except urllib_error.HTTPError as exc:
        headers = dict(exc.headers)
        body = exc.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = body
        return exc.code, parsed, headers


def http_post(base_url: str, path: str, payload: Any = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib_request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = body.decode("utf-8", errors="replace")
        return exc.code, parsed


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_discovery_disabled_without_comfy(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=False) as env:
        # /v1/models only has text models
        status, data, _ = http_get(env["base_url"], "/v1/models")
        assert status == 200
        ids = [m["id"] for m in data["data"]]
        assert ids == ["test-model"]

        # /v1/images/models returns 404
        status, err, _ = http_get(env["base_url"], "/v1/images/models")
        assert status == 404

        # Image job submission returns 404
        status, err = http_post(env["base_url"], "/v1/images/jobs", {"model": "comfyui"})
        assert status == 404


def test_discovery_with_comfy(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        status, data, _ = http_get(env["base_url"], "/v1/models")
        assert status == 200
        models_by_id = {m["id"]: m for m in data["data"]}

        assert "test-model" in models_by_id
        assert "comfyui" in models_by_id
        assert "juggernaut-xl" in models_by_id
        assert "illustrious-xl" in models_by_id

        # Verify image cards explicitly output modalities image and qualification unassessed
        for img_id in ("comfyui", "juggernaut-xl", "illustrious-xl"):
            card = models_by_id[img_id]
            assert card["modalities"] == ["image"]
            assert card["qualification"] == "unassessed"
            assert "capabilities" in card
            assert "intended_roles" in card
            assert "qualified_for" not in card
            assert card["owned_by"] == "tare.tools.local-labs"

        # Separate image model catalog /v1/images/models
        status, catalog, _ = http_get(env["base_url"], "/v1/images/models")
        assert status == 200
        assert catalog["engine"] == "comfyui"
        assert "juggernaut-xl" in catalog["presets"]
        assert "illustrious-xl" in catalog["presets"]
        assert "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors" in catalog["installed_assets"]["checkpoints"]
        assert "z-image.safetensors" in catalog["installed_assets"]["diffusion_models"]
        assert "sdxl_vae.safetensors" in catalog["installed_assets"]["vae"]
        assert "clip_g.safetensors" in catalog["installed_assets"]["text_encoders"]


def test_explicit_workflow_submission(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "custom.safetensors"}},
            "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out", "images": ["1", 0]}},
        }
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "comfyui",
            "workflow": workflow,
        })
        assert status == 200
        assert resp["status"] == "QUEUED"
        assert resp["model"] == "comfyui"
        assert resp["backend"] == "comfyui"

        job_id = resp["id"]
        uuid_obj = uuid.UUID(job_id)
        assert str(uuid_obj) == job_id

        assert job_id in env["mock_comfy"].submitted_prompts
        assert env["mock_comfy"].submitted_prompts[job_id] == workflow


def test_preset_workflow_submission(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "a beautiful forest at sunrise",
            "negative_prompt": "fog, clouds",
            "width": 768,
            "height": 768,
            "steps": 25,
            "seed": 12345678,
        })
        assert status == 200
        assert resp["status"] == "QUEUED"
        assert resp["model"] == "juggernaut-xl"
        assert resp["backend"] == "comfyui"

        job_id = resp["id"]
        wf = env["mock_comfy"].submitted_prompts[job_id]
        assert wf["1"]["inputs"]["ckpt_name"] == "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"
        assert wf["2"]["inputs"]["text"] == "a beautiful forest at sunrise"
        assert wf["3"]["inputs"]["text"] == "fog, clouds"
        assert wf["4"]["inputs"]["width"] == 768
        assert wf["4"]["inputs"]["height"] == 768
        assert wf["5"]["inputs"]["steps"] == 25
        assert wf["5"]["inputs"]["seed"] == 12345678


def test_malformed_and_unknown_model_refusal(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        # Unknown model
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "krea-v1",
            "prompt": "test",
        })
        assert status == 400
        assert "unknown" in str(resp).lower()

        # Missing model
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "prompt": "test",
        })
        assert status == 400

        # Preset model with explicit workflow
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "workflow": {},
        })
        assert status == 400

        # model='comfyui' with unknown parameters
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "comfyui",
            "workflow": {"1": {}},
            "prompt": "test",
        })
        assert status == 400

        # Malformed JSON
        req = urllib_request.Request(
            f"{env['base_url']}/v1/images/jobs",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib_error.HTTPError) as exc_info:
            urllib_request.urlopen(req)
        assert exc_info.value.code == 400


def test_submit_leak_prevention_on_comfy_4xx(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        # ComfyUI emits 400 with sensitive prompt leak
        env["mock_comfy"].prompt_fail_code = 400
        env["mock_comfy"].prompt_fail_body = {
            "error": "node_error",
            "secret_workflow_data": "secret_user_prompt_leak_12345",
        }

        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "secret prompt",
        })
        assert status == 400
        # Check that sensitive raw error content is NOT leaked
        assert "secret_user_prompt_leak_12345" not in str(resp)
        assert "secret_workflow_data" not in str(resp)


def test_coordination_validation_checks_strict(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        # 1. Unmanaged role
        env["mock_comfy"].coordinator_role = "unmanaged-role"
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        env["mock_comfy"].coordinator_role = "tare-comfy-gpu-coordinator"

        # 2. Cleanup quarantine (cleanup_blocked = True)
        env["mock_comfy"].cleanup_blocked = True
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        env["mock_comfy"].cleanup_blocked = False

        # 3. Missing cleanup_blocked field (not explicitly False)
        env["mock_comfy"].cleanup_blocked = None
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        env["mock_comfy"].cleanup_blocked = False

        # 4. GPU coordination not enabled (gpu.enabled = False or missing)
        env["mock_comfy"].gpu_enabled = False
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        env["mock_comfy"].gpu_enabled = True

        # 5. Lock path mismatch
        env["mock_comfy"].gpu_lock_path = "/tmp/different_gpu.lock"
        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        env["mock_comfy"].gpu_lock_path = str(env["lock_file"])


def test_uncertain_submission_transport(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        env["mock_comfy"].prompt_timeout = True
        env["runtime"].route_timeout = 0.5

        status, resp = http_post(env["base_url"], "/v1/images/jobs", {
            "model": "juggernaut-xl",
            "prompt": "test",
        })
        assert status == 503
        assert resp["error"]["type"] == "uncertain_submission"
        assert "job_id" in resp["error"]
        assert resp["error"]["job_id"]


def test_job_status_outputs_only_and_incomplete_success(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        job_id = str(uuid.uuid4()).lower()

        # 1. Outputs present, but status_str is success and completed is False
        env["mock_comfy"].history[job_id] = {
            "status": {"status_str": "success", "completed": False},
            "outputs": {
                "7": {
                    "images": [
                        {"filename": "partial.png", "subfolder": "", "type": "output"},
                    ]
                }
            },
        }
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        # Incomplete success must be UNKNOWN, but outputs remain retrievable
        assert resp["status"] == "UNKNOWN"
        assert len(resp["outputs"]) == 1
        assert resp["outputs"][0]["filename"] == "partial.png"

        # 2. Outputs present, completed is True but status_str is 'running' (not success)
        env["mock_comfy"].history[job_id]["status"] = {"status_str": "running", "completed": True}
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "UNKNOWN"

        # 3. Outputs present, but no status dict at all (outputs-only)
        env["mock_comfy"].history[job_id]["status"] = {}
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "UNKNOWN"
        assert len(resp["outputs"]) == 1

        # 4. Strict COMPLETED: completed is True AND status_str == 'success'
        env["mock_comfy"].history[job_id]["status"] = {"status_str": "success", "completed": True}
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "COMPLETED"


def test_job_status_lifecycle_and_history(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        job_id = str(uuid.uuid4()).lower()

        # 1. QUEUED in queue_pending
        env["mock_comfy"].queue_pending = [[1, job_id, {}, {}, []]]
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "QUEUED"
        assert resp["outputs"] == []

        # 2. RUNNING in queue_running
        env["mock_comfy"].queue_pending = []
        env["mock_comfy"].queue_running = [[1, job_id, {}, {}, []]]
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "RUNNING"
        assert resp["outputs"] == []

        # 3. CANCELLED in history
        env["mock_comfy"].queue_running = []
        env["mock_comfy"].history[job_id] = {
            "status": {
                "status_str": "interrupted",
                "completed": False,
                "messages": [["execution_interrupted", {"prompt_id": job_id}]],
            },
            "outputs": {},
        }
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "CANCELLED"

        # 4. FAILED in history (no raw server tracebacks leaked)
        env["mock_comfy"].history[job_id]["status"] = {
            "status_str": "error",
            "completed": False,
            "messages": [["execution_error", {"exception_message": "CUDA OOM", "traceback": ["line 10"]}]],
        }
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "FAILED"
        assert "line 10" not in str(resp)

        # 5. UNKNOWN for absent history
        del env["mock_comfy"].history[job_id]
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}")
        assert status == 200
        assert resp["status"] == "UNKNOWN"
        assert resp["outputs"] == []


def test_targeted_job_cancellation(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        job_id = str(uuid.uuid4()).lower()
        status, resp = http_post(env["base_url"], f"/v1/images/jobs/{job_id}/cancel", {})
        assert status == 200
        assert resp["id"] == job_id
        assert resp["cancellation_requested"] is True
        assert env["mock_comfy"].cancelled_jobs == [job_id]


def test_output_retrieval_and_bounds_checks(tmp_path: Path):
    with running_test_env(tmp_path, enable_comfy=True) as env:
        job_id = str(uuid.uuid4()).lower()
        env["mock_comfy"].history[job_id] = {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "7": {
                    "images": [
                        {"filename": "out_0001.png", "subfolder": "", "type": "output"},
                    ]
                }
            }
        }

        # Valid retrieval
        status, content, headers = http_get(env["base_url"], f"/v1/images/jobs/{job_id}/outputs/0")
        assert status == 200
        assert headers.get("Content-Type") == "image/png"
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert content.startswith(b"\x89PNG")

        # Index out of bounds
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}/outputs/1")
        assert status == 404

        # Non-matching/unsupported MIME: ComfyUI serves SVG/HTML for png
        env["mock_comfy"].view_mime = "image/svg+xml"
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}/outputs/0")
        # Gateway rejects with 503 backend error and never relabels to image/png
        assert status == 503
        env["mock_comfy"].view_mime = "image/png"

        # Path traversal in history filename rejected
        env["mock_comfy"].history[job_id]["outputs"]["7"]["images"] = [
            {"filename": "../../etc/shadow", "subfolder": "", "type": "output"}
        ]
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}/outputs/0")
        assert status == 400

        # Oversized file (> 32MiB) rejected
        env["mock_comfy"].history[job_id]["outputs"]["7"]["images"] = [
            {"filename": "huge.png", "subfolder": "", "type": "output"}
        ]
        status, resp, _ = http_get(env["base_url"], f"/v1/images/jobs/{job_id}/outputs/0")
        assert status == 503
