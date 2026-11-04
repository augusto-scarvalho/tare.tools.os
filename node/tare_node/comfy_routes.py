"""Image route orchestration and ComfyUI integration for the model gateway.

This module handles:
1. Validating loopback ComfyUI configuration and routes (preserving IPv6 brackets).
2. Building bounded SDXL core workflows for configured presets.
3. Verifying GPU coordinator status (lock path match, explicit cleanup_blocked False, explicit gpu.enabled True).
4. Asynchronously enqueueing image jobs via ComfyUI /prompt with strict uncertainty and no raw data leaks.
5. Tracking real job queue and history status without leaking prompts, requiring explicit completion proof.
6. Retrieving bounded image output artifacts safely without relabeling mismatched MIME.
7. Targeted single-job cancellation without impacting unrelated tasks.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid

LOG = logging.getLogger("comfy-routes")

MAX_IMAGE_BYTES = 32 * 1024 * 1024  # 32 MiB
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class ComfyRouteError(Exception):
    """Base exception for ComfyUI routing failures."""


class UncertainSubmissionError(ComfyRouteError):
    """Transport failure occurred during submission; state of job is uncertain."""

    def __init__(self, job_id: str, message: str) -> None:
        super().__init__(f"Uncertain submission transport for job {job_id}: {message}")
        self.job_id = job_id


class CoordinationError(ComfyRouteError):
    """GPU lease coordination or coordinator health check failed."""


class ComfyBackendError(ComfyRouteError):
    """ComfyUI backend returned an error or is unreachable."""


class ImageValidationError(ValueError, ComfyRouteError):
    """Request envelope or parameters failed validation."""


class OutputNotFoundError(KeyError, ComfyRouteError):
    """Requested job output was not found."""


def validate_comfy_url(url: str) -> str:
    """Validate that comfy_url is a loopback HTTP URL with explicit port, preserving IPv6 brackets."""
    if not isinstance(url, str) or not url.strip():
        raise ImageValidationError("comfy_url must be a non-empty string")
    parsed = urllib_parse.urlsplit(url.strip())
    if parsed.scheme != "http":
        raise ImageValidationError(f"comfy_url scheme must be 'http', got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ImageValidationError("comfy_url must not contain user credentials")
    if parsed.query or parsed.fragment:
        raise ImageValidationError("comfy_url must not contain query parameters or fragments")
    if parsed.path not in ("", "/"):
        raise ImageValidationError(f"comfy_url must not contain a path prefix, got {parsed.path!r}")

    host = parsed.hostname
    if not host:
        raise ImageValidationError("comfy_url must contain a host")

    is_loopback = False
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not is_loopback:
        raise ImageValidationError(f"comfy_url host must be loopback, got {host!r}")

    if parsed.port is None or not (1 <= parsed.port <= 65535):
        raise ImageValidationError(f"comfy_url must specify a valid port (1-65535), got {parsed.port!r}")

    host_part = f"[{host}]" if ":" in host else host
    return f"http://{host_part}:{parsed.port}"


def validate_job_id(job_id: Any) -> str:
    """Validate exact canonical lowercase UUID string format with no normalization."""
    if type(job_id) is not str:
        raise ImageValidationError("Job ID must be a string")
    try:
        parsed = uuid.UUID(job_id)
    except ValueError as exc:
        raise ImageValidationError(f"Invalid UUID string: {job_id!r}") from exc
    canonical = str(parsed)
    if job_id != canonical:
        raise ImageValidationError(f"Job ID must be exact canonical lowercase UUID without normalization: {job_id!r}")
    return canonical


def load_image_routes(path: Path | str) -> dict[str, Any]:
    """Load and validate the image routes JSON configuration."""
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Image routes config not found: {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageValidationError(f"Failed to read image routes config {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ImageValidationError("Image routes config must be a JSON object")
    if data.get("schema_version") != 1:
        raise ImageValidationError("schema_version must be 1")
    if data.get("engine") != "comfyui":
        raise ImageValidationError("engine must be 'comfyui'")
    presets = data.get("presets")
    if not isinstance(presets, dict) or not presets:
        raise ImageValidationError("presets must be a non-empty object")

    for preset_id, preset in presets.items():
        if not isinstance(preset_id, str) or not preset_id.strip():
            raise ImageValidationError("Preset ID must be a non-empty string")
        if not isinstance(preset, dict):
            raise ImageValidationError(f"Preset {preset_id} must be an object")
        if "checkpoint" not in preset or not isinstance(preset["checkpoint"], str):
            raise ImageValidationError(f"Preset {preset_id} missing required string 'checkpoint'")

    return data


def build_sdxl_workflow(
    preset_checkpoint: str,
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    steps: int = 20,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build bounded SDXL core workflow dict (ComfyUI API format).

    CheckpointLoaderSimple -> two CLIPTextEncode -> EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageValidationError("prompt must be a non-empty string")
    if len(prompt) > 4096:
        raise ImageValidationError(f"prompt length ({len(prompt)}) exceeds maximum allowed (4096)")

    if not isinstance(negative_prompt, str):
        raise ImageValidationError("negative_prompt must be a string")
    if len(negative_prompt) > 4096:
        raise ImageValidationError(f"negative_prompt length ({len(negative_prompt)}) exceeds maximum allowed (4096)")

    if type(width) is not int or isinstance(width, bool) or width <= 0 or width % 64 != 0:
        raise ImageValidationError(f"width must be a positive integer divisible by 64, got {width!r}")

    if type(height) is not int or isinstance(height, bool) or height <= 0 or height % 64 != 0:
        raise ImageValidationError(f"height must be a positive integer divisible by 64, got {height!r}")

    if (width * height) > (1024 * 1024):
        raise ImageValidationError(
            f"Total pixels ({width * height}) exceeds maximum budget of 1024x1024 (1048576)"
        )

    if type(steps) is not int or isinstance(steps, bool) or not (1 <= steps <= 50):
        raise ImageValidationError(f"steps must be an integer between 1 and 50, got {steps!r}")

    if seed is None:
        seed = secrets.randbelow(2**63)
    elif type(seed) is not int or isinstance(seed, bool) or not (0 <= seed <= (2**63 - 1)):
        raise ImageValidationError(f"seed must be an integer between 0 and 2^63-1, got {seed!r}")

    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": preset_checkpoint,
            },
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt,
                "clip": ["1", 1],
            },
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt,
                "clip": ["1", 1],
            },
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": 1,
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["5", 0],
                "vae": ["1", 2],
            },
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "tare",
                "images": ["6", 0],
            },
        },
    }


def verify_coordinator(comfy_url: str, gpu_lease: Any, timeout: float = 3.0) -> None:
    """Verify managed ComfyUI coordinator status, shared lock path, and quarantine."""
    if gpu_lease is None or not getattr(gpu_lease, "is_enabled", False):
        raise CoordinationError("GPU lease coordination is disabled on gateway")

    gateway_path = Path(gpu_lease.path).resolve()
    req = urllib_request.Request(f"{comfy_url}/tare/gpu/status", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise CoordinationError(f"ComfyUI coordinator returned HTTP {resp.status}")
            raw = resp.read(8193)
            if len(raw) > 8192:
                raise CoordinationError("Coordinator status response exceeded size limit")
            data = json.loads(raw.decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise CoordinationError(f"Failed to query managed ComfyUI coordinator: {exc}") from exc

    if not isinstance(data, dict):
        raise CoordinationError("Coordinator status response must be a JSON object")

    if data.get("role") != "tare-comfy-gpu-coordinator":
        raise CoordinationError(
            f"Unexpected coordinator role: {data.get('role')!r}; managed ComfyUI required"
        )

    # Must explicitly be False; missing or truthy values refuse
    if data.get("cleanup_blocked") is not False:
        raise CoordinationError("ComfyUI coordinator cleanup_blocked is not explicitly False")

    gpu_info = data.get("gpu")
    if not isinstance(gpu_info, dict):
        raise CoordinationError("ComfyUI coordinator missing gpu dictionary")

    # Must explicitly be True; missing or falsy values refuse
    if gpu_info.get("enabled") is not True:
        raise CoordinationError("ComfyUI coordinator reports GPU coordination is not explicitly enabled")

    coord_path_str = gpu_info.get("path")
    if not coord_path_str or not isinstance(coord_path_str, str):
        raise CoordinationError("ComfyUI coordinator reports empty or non-string GPU lock path")

    coord_path = Path(coord_path_str).resolve()
    if coord_path != gateway_path:
        raise CoordinationError(
            f"GPU lock path mismatch: gateway has {gateway_path}, coordinator has {coord_path}"
        )


def submit_image_job(
    comfy_url: str,
    gpu_lease: Any,
    route_config: dict[str, Any],
    payload: dict[str, Any],
    timeout: float = 15.0,
) -> tuple[str, str]:
    """Validate request, verify coordinator, and forward asynchronous job to ComfyUI /prompt.

    Image HTTP submission itself must NOT acquire GPU lease (worker owns it).
    """
    if not isinstance(payload, dict):
        raise ImageValidationError("body must be a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ImageValidationError("Field 'model' must be a non-empty string")

    presets = route_config.get("presets", {})
    if model == "comfyui":
        unknown_keys = set(payload.keys()) - {"model", "workflow"}
        if unknown_keys:
            raise ImageValidationError(f"Unknown fields for model='comfyui': {sorted(unknown_keys)}")
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict) or not workflow:
            raise ImageValidationError("model='comfyui' requires a non-empty 'workflow' dictionary")
    elif model in presets:
        if "workflow" in payload:
            raise ImageValidationError("Explicit 'workflow' is not permitted for preset models")
        unknown_keys = set(payload.keys()) - {"model", "prompt", "negative_prompt", "width", "height", "steps", "seed"}
        if unknown_keys:
            raise ImageValidationError(f"Unknown parameter(s) for preset model: {sorted(unknown_keys)}")
        preset = presets[model]
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise ImageValidationError("Field 'prompt' is required and must be a string")
        negative_prompt = payload.get("negative_prompt", "")
        width = payload.get("width", preset.get("default_width", 512))
        height = payload.get("height", preset.get("default_height", 512))
        steps = payload.get("steps", preset.get("default_steps", 20))
        seed = payload.get("seed")
        workflow = build_sdxl_workflow(
            preset["checkpoint"],
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            seed=seed,
        )
    else:
        raise ImageValidationError(f"Unknown or unsupported image model: {model}")

    verify_coordinator(comfy_url, gpu_lease)

    job_id = str(uuid.uuid4()).lower()
    prompt_body = json.dumps({"prompt_id": job_id, "prompt": workflow}).encode("utf-8")
    req = urllib_request.Request(
        f"{comfy_url}/prompt",
        data=prompt_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            raw = resp.read(1024 * 1024 + 1)
    except urllib_error.HTTPError as exc:
        # Bounded drain to avoid resource leak, without exposing raw error body
        with exc:
            exc.read(1025)
        if 400 <= exc.code < 500:
            # Definitive 4xx client validation rejection by ComfyUI: emit generic validation error
            raise ImageValidationError(f"ComfyUI rejected prompt validation (HTTP {exc.code})") from exc
        # 5xx or server-side failure is uncertain
        raise UncertainSubmissionError(job_id, f"ComfyUI server error HTTP {exc.code}") from exc
    except (urllib_error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise UncertainSubmissionError(job_id, str(exc)) from exc

    # Parse and validate response with full transport uncertainty guards
    if len(raw) > 1024 * 1024:
        raise UncertainSubmissionError(job_id, "ComfyUI prompt acknowledgement exceeded size limit")

    if status_code != 200:
        raise UncertainSubmissionError(job_id, f"ComfyUI returned unexpected HTTP {status_code}")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise UncertainSubmissionError(job_id, f"Malformed response from ComfyUI: {exc}") from exc

    if not isinstance(data, dict):
        raise UncertainSubmissionError(job_id, f"ComfyUI returned non-object response: {type(data).__name__}")

    confirmed_id = data.get("prompt_id")
    if confirmed_id != job_id:
        raise UncertainSubmissionError(
            job_id, f"Backend returned prompt_id {confirmed_id!r}, expected {job_id!r}"
        )

    return job_id, "QUEUED"


def extract_job_outputs(job_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract strictly recorded output images with stable total node ordering."""
    outputs: list[dict[str, Any]] = []
    raw_outputs = entry.get("outputs")
    if not isinstance(raw_outputs, dict):
        return outputs

    def node_sort_key(k: Any) -> tuple[int, Any]:
        s = str(k)
        if s.isdigit():
            return (0, int(s))
        return (1, s)

    for node_id in sorted(raw_outputs.keys(), key=node_sort_key):
        node_data = raw_outputs[node_id]
        if not isinstance(node_data, dict):
            continue
        images = node_data.get("images")
        if not isinstance(images, list):
            continue
        for img in images:
            # Only explicitly recorded type == 'output' (missing type is not proof)
            if isinstance(img, dict) and img.get("type") == "output":
                filename = img.get("filename")
                if not isinstance(filename, str) or not filename.strip():
                    continue
                subfolder = img.get("subfolder", "")
                if not isinstance(subfolder, str):
                    subfolder = ""
                idx = len(outputs)
                outputs.append({
                    "index": idx,
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": "output",
                    "url": f"/v1/images/jobs/{job_id}/outputs/{idx}",
                })
    return outputs


def validate_filename_and_subfolder(filename: Any, subfolder: Any) -> tuple[str, str]:
    """Validate filename and subfolder for path traversal, control chars, colons, and extensions."""
    if type(filename) is not str or not filename:
        raise ImageValidationError("Recorded output image filename must be a non-empty string")

    # No slash, backslash, colon, or control characters
    if any(c in filename for c in ("/", "\\", ":")):
        raise ImageValidationError(f"Prohibited separator or colon in image filename: {filename!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise ImageValidationError("Control character detected in image filename")
    if ".." in filename or os.path.basename(filename) != filename:
        raise ImageValidationError(f"Path traversal detected in image filename: {filename!r}")

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ImageValidationError(f"Disallowed image extension: {ext!r}")

    if subfolder is None:
        subfolder = ""
    if type(subfolder) is not str:
        raise ImageValidationError("Recorded output image subfolder must be a string")

    if subfolder:
        if ":" in subfolder:
            raise ImageValidationError("Prohibited colon in image subfolder")
        if any(ord(c) < 32 or ord(c) == 127 for c in subfolder):
            raise ImageValidationError("Control character detected in image subfolder")
        if subfolder.startswith(("/", "\\")):
            raise ImageValidationError("Absolute subfolder paths are prohibited")

        components = [p for p in subfolder.replace("\\", "/").split("/") if p]
        for comp in components:
            if comp in (".", ".."):
                raise ImageValidationError(f"Path traversal component in subfolder: {comp!r}")

    return filename, subfolder


def get_image_job_status(comfy_url: str, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Query truthful job state from ComfyUI queue and history without leaking raw inputs."""
    canonical_id = validate_job_id(job_id)

    # 1. Check queue (running / pending)
    q_req = urllib_request.Request(f"{comfy_url}/queue", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(q_req, timeout=timeout) as resp:
            raw_q = resp.read(2 * 1024 * 1024 + 1)
            if len(raw_q) > 2 * 1024 * 1024:
                raise ComfyBackendError("Queue response exceeded size limit")
            q_data = json.loads(raw_q.decode("utf-8"))
    except Exception as exc:
        raise ComfyBackendError(f"Failed to query ComfyUI queue: {exc}") from exc

    if isinstance(q_data, dict):
        running = q_data.get("queue_running", [])
        if isinstance(running, list):
            for item in running:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == canonical_id:
                    return {"id": canonical_id, "status": "RUNNING", "outputs": []}

        pending = q_data.get("queue_pending", [])
        if isinstance(pending, list):
            for item in pending:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == canonical_id:
                    return {"id": canonical_id, "status": "QUEUED", "outputs": []}

    # 2. Check history
    h_req = urllib_request.Request(
        f"{comfy_url}/history/{canonical_id}", headers={"Accept": "application/json"}
    )
    try:
        with urllib_request.urlopen(h_req, timeout=timeout) as resp:
            raw_h = resp.read(2 * 1024 * 1024 + 1)
            if len(raw_h) > 2 * 1024 * 1024:
                raise ComfyBackendError("History response exceeded size limit")
            h_data = json.loads(raw_h.decode("utf-8"))
    except Exception as exc:
        raise ComfyBackendError(f"Failed to query ComfyUI history: {exc}") from exc

    if not isinstance(h_data, dict) or canonical_id not in h_data:
        # Backend restart or missing history must be UNKNOWN, not completed or cancelled
        return {"id": canonical_id, "status": "UNKNOWN", "outputs": []}

    entry = h_data[canonical_id]
    if not isinstance(entry, dict):
        return {"id": canonical_id, "status": "UNKNOWN", "outputs": []}

    outputs = extract_job_outputs(canonical_id, entry)

    status_info = entry.get("status")
    status_str = status_info.get("status_str") if isinstance(status_info, dict) else ""
    completed = status_info.get("completed") if isinstance(status_info, dict) else False
    messages = status_info.get("messages", []) if isinstance(status_info, dict) else []

    has_interrupted = status_str in ("interrupted", "cancelled") or any(
        isinstance(m, (list, tuple)) and len(m) > 0 and m[0] == "execution_interrupted"
        for m in messages
    )
    has_error = status_str == "error" or any(
        isinstance(m, (list, tuple)) and len(m) > 0 and m[0] == "execution_error"
        for m in messages
    )

    if has_interrupted:
        job_status = "CANCELLED"
    elif has_error:
        job_status = "FAILED"
    elif completed is True and status_str == "success" and not has_error and not has_interrupted:
        # Require explicit completed is True AND status_str == "success"
        job_status = "COMPLETED"
    else:
        # Partial outputs or incomplete success remain retrievable, but status is UNKNOWN
        job_status = "UNKNOWN"

    result: dict[str, Any] = {"id": canonical_id, "status": job_status, "outputs": outputs}
    if job_status == "FAILED":
        result["error"] = {"message": "Execution failed on image backend"}
    return result


def get_job_output_image(
    comfy_url: str,
    job_id: str,
    index: int,
    timeout: float = 10.0,
) -> tuple[bytes, str]:
    """Retrieve recorded output image by index via ComfyUI /view with strict MIME and bounds checks."""
    canonical_id = validate_job_id(job_id)
    if type(index) is not int or isinstance(index, bool) or index < 0:
        raise ImageValidationError("Output index must be a non-negative integer")

    h_req = urllib_request.Request(
        f"{comfy_url}/history/{canonical_id}", headers={"Accept": "application/json"}
    )
    try:
        with urllib_request.urlopen(h_req, timeout=timeout) as resp:
            raw_h = resp.read(2 * 1024 * 1024 + 1)
            if len(raw_h) > 2 * 1024 * 1024:
                raise ComfyBackendError("History response exceeded size limit")
            h_data = json.loads(raw_h.decode("utf-8"))
    except Exception as exc:
        raise ComfyBackendError(f"Failed to query ComfyUI history for job {canonical_id}: {exc}") from exc

    if not isinstance(h_data, dict) or canonical_id not in h_data:
        raise OutputNotFoundError(f"Job {canonical_id} not found in history")

    entry = h_data[canonical_id]
    if not isinstance(entry, dict):
        raise OutputNotFoundError(f"Job {canonical_id} history entry is invalid")

    valid_images = extract_job_outputs(canonical_id, entry)

    if index >= len(valid_images):
        raise OutputNotFoundError(
            f"Output index {index} out of range ({len(valid_images)} outputs available)"
        )

    target_img = valid_images[index]
    filename, subfolder = validate_filename_and_subfolder(
        target_img.get("filename"), target_img.get("subfolder")
    )

    query = urllib_parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    view_req = urllib_request.Request(f"{comfy_url}/view?{query}")

    try:
        with urllib_request.urlopen(view_req, timeout=timeout) as resp:
            if resp.status != 200:
                raise ComfyBackendError(f"ComfyUI /view returned HTTP {resp.status}")
            content = resp.read(MAX_IMAGE_BYTES + 1)
            if len(content) > MAX_IMAGE_BYTES:
                raise ComfyBackendError(f"Output image exceeded maximum allowed size of {MAX_IMAGE_BYTES} bytes")
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise ComfyBackendError(f"Failed to fetch image via ComfyUI /view: {exc}") from exc

    ext = os.path.splitext(filename)[1].lower()
    expected_mime = MIME_MAP.get(ext, "image/png")

    # REJECT unsupported or non-matching MIME; never relabel
    if content_type != expected_mime:
        raise ComfyBackendError(
            f"Backend returned unsupported or non-matching MIME {content_type!r}, expected {expected_mime!r}"
        )

    return content, content_type


def cancel_image_job(comfy_url: str, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Targeted cancellation of a single job via ComfyUI /api/jobs/{uuid}/cancel."""
    canonical_id = validate_job_id(job_id)
    req = urllib_request.Request(
        f"{comfy_url}/api/jobs/{canonical_id}/cancel",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4097)
            if len(raw) > 4096:
                raise ComfyBackendError("Cancellation response exceeded size limit")
            data = json.loads(raw.decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ComfyBackendError(f"Failed to dispatch cancellation to ComfyUI: {exc}") from exc

    cancelled = bool(data.get("cancelled", False)) if isinstance(data, dict) else False
    return {"id": canonical_id, "cancellation_requested": cancelled}


def get_image_model_catalog(
    comfy_url: str | None,
    route_config: dict[str, Any],
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Return image model catalog with configured routes and installed assets."""
    installed_assets: dict[str, Any] = {
        "checkpoints": [],
        "diffusion_models": [],
        "vae": [],
        "text_encoders": [],
    }

    if comfy_url:
        for folder in ("checkpoints", "diffusion_models", "vae", "text_encoders"):
            req = urllib_request.Request(f"{comfy_url}/models/{folder}", headers={"Accept": "application/json"})
            try:
                with urllib_request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        raw = resp.read(1024 * 1024 + 1)
                        if len(raw) <= 1024 * 1024:
                            file_list = json.loads(raw.decode("utf-8"))
                            if isinstance(file_list, list):
                                installed_assets[folder] = file_list
            except Exception:
                LOG.debug("Could not query ComfyUI /models/%s", folder)
                installed_assets[folder] = []

    return {
        "object": "list",
        "engine": route_config.get("engine", "comfyui"),
        "presets": route_config.get("presets", {}),
        "installed_assets": installed_assets,
    }


def get_image_public_cards(route_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return public model cards for image engine and presets using capabilities and intended_roles."""
    cards = [
        {
            "id": "comfyui",
            "object": "model",
            "owned_by": "tare.tools.local-labs",
            "display_name": "ComfyUI Workflow Engine",
            "qualification": "unassessed",
            "capabilities": ["workflow_execution"],
            "intended_roles": ["image_generation_workflows"],
            "modalities": ["image"],
            "summary": "Direct ComfyUI API workflow execution engine.",
        }
    ]
    for preset_id, preset in route_config.get("presets", {}).items():
        cards.append({
            "id": preset_id,
            "object": "model",
            "owned_by": "tare.tools.local-labs",
            "display_name": preset.get("display_name", preset_id),
            "qualification": "unassessed",
            "capabilities": ["text_to_image"],
            "intended_roles": ["image_generation"],
            "modalities": ["image"],
            "summary": preset.get("summary", f"{preset_id} SDXL preset routed to ComfyUI."),
            "checkpoint": preset.get("checkpoint"),
        })
    return cards
