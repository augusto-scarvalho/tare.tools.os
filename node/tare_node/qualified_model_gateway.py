#!/usr/bin/env python3
"""OpenAI-compatible on-demand gateway for the qualified single-GPU fleet.

The public gateway owns port 8080. Exactly one llama-server child owns a private
loopback port. The JSON ``model`` field selects a frozen qualified card; switching
stops the old child before starting the new one, so two large models never overlap
in VRAM. Different cards may use different qualified llama-server builds.
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import ipaddress
import itertools
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import request as urllib_request

PACKAGE_ROOT = Path(__file__).resolve().parent

from tare_node.qualified_fleet import (  # noqa: E402
    CATALOG,
    FLEET_FILE,
    FleetConfigError,
    arrived,
    compose,
    owner_fleet,
    save_fleet,
    turned_off,
    backend_kind,
    build_backend_command,
    host_mode,
    load_registry,
    ninfer_chat_request,
    public_card,
    resolve_model,
)
from tare_node.fleet_count import (  # noqa: E402
    BINDING_FIELD, BindingMismatch, check_binding, count_request, effective_profile,
)
from tare_node.gpu_lease import GpuBusy, SharedGpuLease, eta_seconds  # noqa: E402
from tare_node.comfy_routes import (  # noqa: E402
    ComfyBackendError,
    ComfyRouteError,
    CoordinationError,
    ImageValidationError,
    OutputNotFoundError,
    UncertainSubmissionError,
    cancel_image_job,
    get_image_job_status,
    get_image_model_catalog,
    get_image_public_cards,
    get_job_output_image,
    load_image_routes,
    submit_image_job,
    validate_comfy_url,
)

DEFAULT_IMAGE_ROUTES = PACKAGE_ROOT / "config" / "comfy_image_routes.json"

LOG = logging.getLogger("qualified-model-gateway")
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
ALLOWED_POST = {
    "/v1/chat/completions", "/v1/completions", "/completion", "/infill",
}
FLEET_POST = {'/v1/fleet/count', '/v1/fleet/profile'}
INTERNAL_POST = {'/internal/gpu/yield'}
TEXT_YIELDS_TO_IMAGE_SECONDS = 5.0
# Text keeps the GPU this long after a request, so an agent's back-to-back calls are never split by an
# image or training job (--text-idle-grace; 0 releases at once). The grace ends early, after the running
# request, once an image job has waited IMAGE_MAX_WAIT_SECONDS (--image-max-wait).
TEXT_IDLE_GRACE_SECONDS = 20.0
IMAGE_MAX_WAIT_SECONDS = 300.0


class FleetRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        backend_host: str,
        backend_port: int,
        state_dir: Path,
        load_timeout: float,
        stop_timeout: float,
        gpu_lock: Path | str | None = None,
        route_timeout: float = 60.0,
        gpu_lease: SharedGpuLease | None = None,
        comfy_url: str | None = None,
        image_routes_path: Path | str | None = None,
        text_idle_grace: float = TEXT_IDLE_GRACE_SECONDS,
        image_max_wait: float = IMAGE_MAX_WAIT_SECONDS,
    ) -> None:
        self.config = config
        self.text_idle_grace = float(text_idle_grace)
        self.image_max_wait = float(image_max_wait)
        if not 0 <= self.text_idle_grace < float("inf"):
            raise ValueError("text idle grace must be finite and non-negative")
        if not 0 < self.image_max_wait < float("inf"):
            raise ValueError("image max wait must be finite and positive")
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.state_dir = state_dir
        self.load_timeout = load_timeout
        self.stop_timeout = stop_timeout
        self.route_timeout = route_timeout
        if gpu_lease is not None:
            self.gpu_lease = gpu_lease
        elif gpu_lock is not None:
            self.gpu_lease = SharedGpuLease(Path(gpu_lock), timeout=route_timeout)
        else:
            self.gpu_lease = SharedGpuLease(None, timeout=route_timeout)
        self.request_lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.model_id: str | None = None
        self.requested_name: str | None = None
        self.last_switch_seconds: float | None = None
        self.last_error: str | None = None
        self.last_used = time.monotonic()
        if comfy_url is not None:
            self.comfy_url: str | None = validate_comfy_url(comfy_url)
            routes_file = Path(image_routes_path) if image_routes_path else DEFAULT_IMAGE_ROUTES
            self.image_routes: dict[str, Any] | None = load_image_routes(routes_file)
        else:
            self.comfy_url = None
            self.image_routes = None

    def backend_url(self, path: str) -> str:
        return f"http://{self.backend_host}:{self.backend_port}{path}"

    def backend_health(self, timeout: float = 2.0) -> bool:
        process = self.process
        if process is None or process.poll() is not None:
            return False
        try:
            with urllib_request.urlopen(self.backend_url("/health"), timeout=timeout) as response:
                body = json.load(response)
            return response.status == 200 and body.get("status") == "ok"
        except Exception:
            return False

    def backend_load(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Admitted/running/waiting requests from a NInfer backend's lock-free /v1/load, else None.

        Lets a client ask the resident model a short question only while it is idle.
        """
        card = self.config["models"].get(self.model_id) if self.model_id else None
        process = self.process
        if card is None or backend_kind(card) != "ninfer" or process is None or process.poll() is not None:
            return None
        try:
            with urllib_request.urlopen(self.backend_url("/v1/load"), timeout=timeout) as response:
                requests = json.load(response)["requests"]
            return {key: requests[key] for key in ("admitted", "running", "waiting") if key in requests}
        except Exception:
            return None

    def _verify_identity(self, model_id: str, card: dict[str, Any]) -> bool:
        try:
            if backend_kind(card) in ("ninfer", "strata"):
                # No /props: the backend serves what the gateway started; NInfer under --model-id,
                # Strata under the model_name its config declares.
                expected = model_id if backend_kind(card) == "ninfer" else card["runtime"]["model_name"]
                with urllib_request.urlopen(self.backend_url("/v1/models"), timeout=3) as response:
                    return [m.get("id") for m in json.load(response).get("data", [])] == [expected]
            with urllib_request.urlopen(self.backend_url("/props"), timeout=3) as response:
                props = json.load(response)
            return props.get("model_path") == card["artifact"]["path"]
        except Exception:
            return False

    def stop_backend(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            self.model_id = None
            self.requested_name = None
            return
        LOG.info("stopping backend pid=%s", process.pid)
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            with contextlib.suppress(Exception):
                process.terminate()
        deadline = time.monotonic() + self.stop_timeout
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            LOG.warning("backend pid=%s exceeded graceful timeout; killing", process.pid)
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                with contextlib.suppress(Exception):
                    process.kill()
            kill_deadline = time.monotonic() + 5.0
            while process.poll() is None and time.monotonic() < kill_deadline:
                time.sleep(0.1)
        if process.poll() is None:
            self.last_error = f"failed to stop backend pid={process.pid}"
            raise RuntimeError(f"backend process {process.pid} could not be stopped")
        self.process = None
        self.model_id = None
        self.requested_name = None

    def unload_if_idle(self) -> bool:
        """Stop a resident backend whose card sets idle_unload_seconds once it has been idle that long."""
        card = self.config["models"].get(self.model_id) if self.model_id else None
        limit = card and card["runtime"].get("idle_unload_seconds")
        if not limit or self.process is None or time.monotonic() - self.last_used < limit:
            return False
        if not self.request_lock.acquire(blocking=False):   # a request is running: not idle
            return False
        try:
            idle = time.monotonic() - self.last_used
            if self.model_id is None or idle < limit:
                return False
            LOG.info("unloading %s after %.0fs idle (limit %ss)", self.model_id, idle, limit)
            self.stop_backend()
            return True
        finally:
            self.request_lock.release()

    def ensure_model(self, requested: str | None) -> tuple[str, float]:
        model_id, card = resolve_model(self.config, requested)
        requested_name = requested or model_id
        if self.model_id == model_id and self.backend_health():
            self.requested_name = requested_name
            return model_id, 0.0

        self.stop_backend()
        wait_until_bindable(self.backend_host, self.backend_port,
                            reuse_address=backend_kind(card) in {'ninfer', 'strata'})
        self.state_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.state_dir / f"{model_id}.log"
        command = build_backend_command(
            model_id, card, host=self.backend_host, port=self.backend_port
        )
        environment = os.environ.copy()
        environment.update({str(k): str(v) for k, v in card["runtime"]["environment"].items()})
        environment.pop("GGML_CUDA_REGISTER_HOST", None)
        environment.pop("GGML_SCHED_PREFETCH_EXPERTS", None)
        LOG.info("loading %s: %s", model_id, " ".join(command))
        started = time.monotonic()
        log_handle = log_path.open("ab", buffering=0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
        log_handle.close()
        self.process = process
        self.last_used = time.monotonic()
        self.model_id = model_id
        self.requested_name = requested_name

        deadline = started + self.load_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.process = None
                self.model_id = None
                tail = ""
                with contextlib.suppress(Exception):
                    tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
                raise RuntimeError(f"{model_id} exited while loading ({process.returncode})\n{tail}")
            if self.backend_health(timeout=2) and self._verify_identity(model_id, card):
                elapsed = time.monotonic() - started
                self.last_switch_seconds = elapsed
                self.last_error = None
                LOG.info("model %s ready in %.2fs", model_id, elapsed)
                return model_id, elapsed
            time.sleep(0.5)

        self.stop_backend()
        raise TimeoutError(f"{model_id} did not become healthy in {self.load_timeout:.0f}s")

    def status(self) -> dict[str, Any]:
        lease = getattr(self, "gpu_lease", None)
        coord = lease.status() if lease is not None else {"enabled": False}
        if coord.get("enabled"):
            coord["eta_seconds"] = eta_seconds(coord)  # contract gpu-lease/1: until text gets the GPU
        return {
            "status": "ok",
            "role": "qualified-model-gateway",
            "current_model": self.model_id,
            "requested_name": self.requested_name,
            "backend_healthy": self.backend_health(),
            "backend_load": self.backend_load(),
            "backend_pid": self.process.pid if self.process and self.process.poll() is None else None,
            "backend_port": self.backend_port,
            "last_switch_seconds": self.last_switch_seconds,
            "last_error": self.last_error,
            "max_resident_models": 1,
            "available_models": sorted(self.config["models"]),
            "default_model": self.config["fleet"]["default_model"],
            "fleet_missing": getattr(self, "fleet_missing", []),
            "gpu_coordination": coord,
            "resident_readout": "pid-bound-v1",
            "text_queue": TEXT_QUEUE.status(),
        }


class TextQueue:
    """Text requests run one at a time, the smallest waiting first, and each is logged with its caller.

    Waiters used to race for the GPU lease every 50 ms, so a few-kB interactive turn could wait behind a batch
    client's ~121k-token prompts until route_timeout sent it to another route. A running request is never
    interrupted, and the wait counts against the caller's route_timeout as before.
    ponytail: smallest-first can starve a large request under steady small traffic; it then times out and its
    caller escalates, as for a busy GPU. Age the key by waiting time if that ever happens.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._order = itertools.count()
        self._waiting: dict[tuple[int, int], dict[str, Any]] = {}
        self._running: dict[str, Any] | None = None

    @contextlib.contextmanager
    def turn(self, size: int, timeout: float, caller: dict[str, Any]):
        """Wait for this request's turn; yields the seconds it waited. TimeoutError past timeout."""
        ticket, deadline = (size, next(self._order)), time.monotonic() + timeout
        with self._cond:
            self._waiting[ticket] = {**caller, "bytes": size, "since": time.monotonic()}
            try:
                while self._running is not None or min(self._waiting) != ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"timed out after {timeout:.0f}s waiting behind other text requests")
                    self._cond.wait(remaining)
            except BaseException:
                del self._waiting[ticket]
                self._cond.notify_all()
                raise
            self._running = {**self._waiting.pop(ticket), "started": time.monotonic()}
            waited = self._running["started"] - self._running["since"]
        try:
            yield waited
        finally:
            with self._cond:
                running, self._running = self._running, None
                self._cond.notify_all()
            LOG.info("text %s from %s (%s): %d bytes, waited %.1fs, ran %.1fs", running.get("model"),
                     running["address"], running["client"], size, waited, time.monotonic() - running["started"])

    def status(self) -> dict[str, Any]:
        now = time.monotonic()

        def view(item: dict[str, Any], since: str) -> dict[str, Any]:
            return {"address": item["address"], "client": item["client"], "model": item.get("model"),
                    "bytes": item["bytes"], since + "_seconds": round(now - item[since], 1)}
        with self._cond:
            return {"running": view(self._running, "started") if self._running else None,
                    "waiting": [view(item, "since") for _, item in sorted(self._waiting.items())]}


TEXT_QUEUE = TextQueue()
RUNTIME: FleetRuntime


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any, headers: dict | None = None) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(raw)


MAX_RETRY_AFTER = 24 * 3600  # the kernel's ceiling too (compute_plane/native_http_worker.py)


def gpu_refusal(lease, kind: str, message: str, model: Any, eta: float | None = None) -> tuple[dict, dict]:
    """A text request refused for the GPU (contract gpu-lease/1): the body and the Retry-After header, both from the
    node's estimate of when text gets the GPU (or `eta`, a reservation's time left), as it is (5 s to 24 h: a 12-hour
    training run says so, it does not claim 10 minutes), 60 s without one."""
    if eta is None:
        eta = eta_seconds(lease.status()) if lease is not None and lease.is_enabled else None
    seconds = min(MAX_RETRY_AFTER, max(5, round(eta))) if eta else 60
    return ({"error": {"message": message, "type": kind, "model": model, "retry_after_seconds": seconds}},
            {"Retry-After": str(seconds)})


# Named reservations (contract gpu-lease/2): one at a time, holding the GPU for text up to MAX_RESERVATION seconds
# while its holder renews it every ttl; only requests carrying its secret in RESERVATION_HEADER use the GPU, the others
# get 503 gpu_reserved with the time left. The node's API token (--gpu-api-token-file) authorizes them; without one
# they are off. State lives in this process (a restart ends the reservation); the fence number survives in a file.
RESERVATION_HEADER = "X-Tare-GPU-Reservation"
MAX_RESERVATION, TTL_RANGE, MAX_RESERVE_WAIT = 4 * 3600, (15, 600), 120
RESERVATION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GPU_API_TOKEN: str | None = None
RESERVATION: dict[str, Any] | None = None
RESERVATION_LOCK = threading.Lock()
RESERVING = threading.Lock()  # one reservation being acquired at a time


def reservation_view(reservation: dict[str, Any], now: float) -> dict[str, Any]:
    """What status shows of a reservation: never its secret."""
    left = max(0, round(min(reservation["expires"], reservation["hard"]) - now))
    return {"id": reservation["id"], "name": reservation["name"], "holder": reservation["holder"],
            "fence": reservation["fence"], "expires_at": time.time() + left,
            "hard_until": reservation["hard_until"], "seconds_left": max(0, round(reservation["hard"] - now))}


def active_reservation() -> dict[str, Any] | None:
    """The current reservation, ending it first when its holder stopped renewing or its maximum time ran out."""
    with RESERVATION_LOCK:
        current = RESERVATION
    if current is not None and time.monotonic() >= min(current["expires"], current["hard"]):
        end_reservation(current, "expired")
        return None
    return current


def fleet_view() -> dict[str, Any]:
    """GET /v1/fleet: the catalog (each card and whether its weights are on this node), the owner's fleet, what is
    served (catalog picks first) and the default."""
    catalog = RUNTIME.catalog
    return {
        "catalog": [{**public_card(m, card), "on_node": Path(card["artifact"]["path"]).exists()}
                    for m, card in catalog["models"].items()],
        "fleet": json.loads(RUNTIME.fleet_path.read_text(encoding="utf-8")),
        "served": list(RUNTIME.config["models"]),
        "default": RUNTIME.config["fleet"]["default_model"],
        "missing": RUNTIME.fleet_missing,
    }


def serve_fleet(fleet: dict[str, Any], config: dict[str, Any], missing: list[str], wait: float) -> bool:
    """Save the fleet and serve it at once, stopping a resident model it dropped; False when a text request kept
    the model switch lock for `wait` seconds."""
    if not RUNTIME.request_lock.acquire(timeout=wait):
        return False
    try:
        save_fleet(fleet, RUNTIME.fleet_path)
        RUNTIME.config, RUNTIME.fleet_missing = config, missing
        if RUNTIME.model_id and RUNTIME.model_id not in config["models"]:
            RUNTIME.stop_backend()
    finally:
        RUNTIME.request_lock.release()
    LOG.info("fleet now %s, default %s", ", ".join(config["models"]), config["fleet"]["default_model"])
    return True


def fleet_edit(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """PUT /v1/fleet: the owner's new fleet, served at once. A catalog model it takes out stays off: it does not
    join again when its weights are on the node."""
    models = body.get("models", [])
    try:
        before = json.loads(RUNTIME.fleet_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    fleet = {"default": body.get("default"), "models": models, "custom": body.get("custom", {}),
             "off": turned_off(RUNTIME.catalog, before, models if isinstance(models, list) else [])}
    try:
        config, _ = compose(RUNTIME.catalog, fleet, strict=True)
    except FleetConfigError as exc:
        return 400, {"error": {"type": "invalid_fleet", "message": str(exc)}}
    if not serve_fleet(fleet, config, [], wait=30):
        return 409, {"error": {"type": "busy", "message": "a text request is switching models; try again"}}
    return 200, fleet_view()


def join_arrivals() -> list[str]:
    """Serve the catalog models whose weights reached the node since (the owner downloaded them), unless the owner
    turned them off; the fleet watch runs it every minute. A busy moment just waits for the next one."""
    try:
        fleet = json.loads(RUNTIME.fleet_path.read_text(encoding="utf-8"))
        new = arrived(RUNTIME.catalog, fleet)
        if not new:
            return []
        fleet = {**fleet, "models": [*fleet.get("models", []), *new]}
        config, missing = compose(RUNTIME.catalog, fleet)
    except (OSError, ValueError) as exc:  # FleetConfigError too: a hand edit the owner is still making
        LOG.warning("fleet watch: %s", exc)
        return []
    return new if serve_fleet(fleet, config, missing, wait=5) else []


def with_reservation(value: dict[str, Any]) -> dict[str, Any]:
    """/v1/fleet/status with the current reservation (contract gpu-lease/2): what it is and how long it lasts, as
    the estimate of when text gets the GPU."""
    current, coord = active_reservation(), value.get("gpu_coordination")
    if current is not None and isinstance(coord, dict):
        coord["reservation"] = reservation_view(current, time.monotonic())
        coord["eta_seconds"] = coord["reservation"]["seconds_left"]
    return value


def end_reservation(reservation: dict[str, Any], why: str) -> None:
    global RESERVATION
    with RESERVATION_LOCK:
        if RESERVATION is not reservation:
            return
        RESERVATION = None
    lease = getattr(RUNTIME, "gpu_lease", None)
    if lease is not None:
        lease.end_reservation(reservation["id"])
    LOG.info("GPU reservation %s for %s ended: %s", reservation["name"], reservation["holder"], why)


FENCE = 0


def next_fence() -> int:
    """A number that grows with every reservation, across restarts when the runtime has a state directory."""
    global FENCE
    state_dir = getattr(RUNTIME, "state_dir", None)
    path = Path(state_dir) / "gpu-fence" if state_dir else None
    candidate = FENCE + 1
    if path is not None:
        try:
            stored = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            stored = "0"  # A new state directory has no counter yet.
        if re.fullmatch(r"[0-9]+", stored) is None:
            raise ValueError("invalid fence counter")
        candidate = max(FENCE, int(stored)) + 1
        temporary = None
        try:
            # Never truncate the recoverable counter, even if writing is interrupted.
            with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent,
                                             prefix=".gpu-fence-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(str(candidate))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink()
    FENCE = candidate
    return FENCE


def reserve_request(body: dict[str, Any], client: str) -> tuple[int, dict, dict]:
    """POST /v1/gpu/reservations: (status, body, headers)."""
    global RESERVATION
    name, holder = body.get("name"), str(body.get("holder") or client)[:120]
    max_seconds, ttl, wait = body.get("max_seconds"), body.get("ttl_seconds", 60), body.get("wait_seconds", 30)
    if (not isinstance(name, str) or not RESERVATION_NAME.fullmatch(name)
            or type(max_seconds) is not int or not 0 < max_seconds <= MAX_RESERVATION
            or type(ttl) is not int or not TTL_RANGE[0] <= ttl <= TTL_RANGE[1]
            or type(wait) not in (int, float) or not 0 <= wait <= MAX_RESERVE_WAIT):
        return 400, {"error": {"type": "invalid_reservation", "message": (
            f"name [A-Za-z0-9._-]{{1,64}}, max_seconds 1..{MAX_RESERVATION}, ttl_seconds {TTL_RANGE[0]}.."
            f"{TTL_RANGE[1]}, wait_seconds 0..{MAX_RESERVE_WAIT}")}}, {}
    lease = getattr(RUNTIME, "gpu_lease", None)
    if lease is None or not lease.is_enabled:
        return 400, {"error": {"type": "gpu_coordination_disabled", "message": "GPU coordination is not enabled"}}, {}
    current = active_reservation()
    if current is not None or not RESERVING.acquire(blocking=False):
        left = (current["hard"] - time.monotonic()) if current else None
        body_, headers = gpu_refusal(lease, "gpu_reserved", "the GPU is reserved", None, eta=left)
        return 409, body_, headers
    try:
        # The pre-lock observation may be stale: another admission can finish before
        # we acquire RESERVING. Recheck while serialized before touching the lease.
        current = active_reservation()
        if current is not None:
            left = current["hard"] - time.monotonic()
            body_, headers = gpu_refusal(lease, "gpu_reserved", "the GPU is reserved", None, eta=left)
            return 409, body_, headers
        # Persist before taking the GPU: a storage failure must not leave a lease.
        try:
            fence = next_fence()
        except (OSError, ValueError) as exc:
            reason = "fence_state_invalid" if isinstance(exc, ValueError) else "fence_state_unavailable"
            return 503, {"error": {"type": reason, "message": "GPU reservation counter could not be persisted"}}, {}
        reservation_id = secrets.token_hex(8)
        try:
            lease.reserve(reservation_id, max_seconds, wait=wait, reason=name)
        except TimeoutError:
            body_, headers = gpu_refusal(lease, "gpu_busy", "the GPU stayed taken; no reservation", None)
            return 503, body_, headers
        now, wall = time.monotonic(), time.time()
        reservation = {"id": reservation_id, "secret": secrets.token_urlsafe(32), "fence": fence,
                       "name": name, "holder": holder, "ttl": ttl, "expires": now + ttl, "hard": now + max_seconds,
                       "hard_until": wall + max_seconds}
        with RESERVATION_LOCK:
            RESERVATION = reservation
        LOG.info("GPU reserved for %s by %s for up to %ss (fence %s)", name, holder, max_seconds, reservation["fence"])
        return 201, {"id": reservation_id, "secret": reservation["secret"], "fence": reservation["fence"],
                     "expires_at": wall + ttl, "hard_until": reservation["hard_until"]}, {}
    finally:
        RESERVING.release()


def held_reservation(reservation_id: str, body: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple | None]:
    """The reservation a renew or release names, or the refusal: 404 when it is gone, 409 for a stale fence or a
    wrong secret (an old client never touches a newer reservation)."""
    current = active_reservation()
    if current is None or current["id"] != reservation_id:
        return None, (404, {"error": {"type": "reservation_not_found", "message": "no such reservation"}}, {})
    secret = body.get("secret")
    if (not isinstance(secret, str) or not hmac.compare_digest(secret.encode(), current["secret"].encode())
            or body.get("fence") != current["fence"]):
        return None, (409, {"error": {"type": "stale_fence", "message": "not the current reservation"}}, {})
    return current, None


def renew_request(reservation_id: str, body: dict[str, Any]) -> tuple[int, dict, dict]:
    current, refusal = held_reservation(reservation_id, body)
    if refusal:
        return refusal
    current["expires"] = min(time.monotonic() + current["ttl"], current["hard"])
    return 200, {"expires_at": time.time() + (current["expires"] - time.monotonic()), "fence": current["fence"]}, {}


def release_request(reservation_id: str, body: dict[str, Any]) -> tuple[int, dict, dict]:
    current, refusal = held_reservation(reservation_id, body)
    if refusal:
        return refusal
    end_reservation(current, "released")
    return 200, {"released": reservation_id}, {}


def proxy_request(handler: BaseHTTPRequestHandler, body: bytes, *, fleet_observation=None, timeout=3600) -> None:
    connection = http.client.HTTPConnection(
        RUNTIME.backend_host, RUNTIME.backend_port, timeout=timeout
    )
    headers = {
        key: value for key, value in handler.headers.items()
        if key.lower() not in HOP_HEADERS and key.lower() not in {"host", "content-length"}
    }
    headers["Content-Type"] = handler.headers.get("Content-Type", "application/json")
    headers["Content-Length"] = str(len(body))
    connection.request(handler.command, handler.path, body=body, headers=headers)
    response = connection.getresponse()
    streaming = response.getheader('Content-Type','').split(';',1)[0].strip() == 'text/event-stream'
    if fleet_observation is not None and not streaming:
        try:
            raw = response.read(8*1024*1024+1)
            if len(raw) > 8*1024*1024:
                raise ValueError('fleet_generation_response_over_budget')
            payload = json.loads(raw)
            payload['tare_fleet_observation'] = fleet_observation
            send_json(handler, response.status, payload)
            return
        finally:
            connection.close()
    handler.send_response(response.status)
    for key, value in response.getheaders():
        if key.lower() not in HOP_HEADERS and key.lower() != "content-length":
            handler.send_header(key, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        if fleet_observation is not None:
            # The same request lock covers binding verification and forwarding.
            # This is route evidence, not completion or token usage.
            handler.wfile.write(b'data: '+json.dumps({'choices':[],
                'tare_fleet_observation':fleet_observation}).encode()+b'\n\n')
            handler.wfile.flush()
        while True:
            chunk = response.read1(4096)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in {"/health", "/v1/health", "/fleet/status", "/v1/fleet/status"}:
            send_json(self, 200, with_reservation(RUNTIME.status()))
            return
        if path == "/v1/fleet/recommend":
            if "host_modes" not in RUNTIME.config["fleet"]:
                send_json(self, 404, {"error": {"message": "host modes not configured", "type": "not_found"}})
                return
            try:
                status = json.loads(Path(RUNTIME.config["fleet"]["host_modes"]["status_path"]).read_text())
            except (OSError, ValueError):
                status = None
            send_json(self, 200, host_mode(RUNTIME.config, status, time.time(), RUNTIME.model_id))
            return
        if path == "/v1/fleet" and getattr(RUNTIME, "catalog", None) is not None:
            send_json(self, 200, fleet_view())
            return
        if path in {"/models", "/v1/models"}:
            payload = []
            for model_id, card in RUNTIME.config["models"].items():  # catalog picks first, then the owner's own
                item = public_card(model_id, card)
                item.update({"object": "model", "owned_by": "tare.tools.local-labs"})
                payload.append(item)
            image_routes = getattr(RUNTIME, "image_routes", None)
            if image_routes is not None:
                payload.extend(get_image_public_cards(image_routes))
            send_json(self, 200, {"object": "list", "data": payload})
            return
        if path == "/props":
            status = RUNTIME.status()
            if RUNTIME.model_id:
                _, card = resolve_model(RUNTIME.config, RUNTIME.model_id)
                status["model"] = public_card(RUNTIME.model_id, card)
            send_json(self, 200, status)
            return
        if path in {"/images/models", "/v1/images/models"}:
            comfy_url = getattr(RUNTIME, "comfy_url", None)
            image_routes = getattr(RUNTIME, "image_routes", None)
            if not comfy_url or not image_routes:
                send_json(self, 404, {"error": {"message": "image routing not configured", "type": "not_found"}})
                return
            catalog = get_image_model_catalog(comfy_url, image_routes)
            send_json(self, 200, catalog)
            return
        if path.startswith("/v1/images/jobs/") or path.startswith("/images/jobs/"):
            comfy_url = getattr(RUNTIME, "comfy_url", None)
            if not comfy_url:
                send_json(self, 404, {"error": {"message": "image routing not configured", "type": "not_found"}})
                return
            rel = path.split("/images/jobs/", 1)[1].strip("/")
            if "/outputs/" in rel:
                parts = rel.split("/outputs/", 1)
                job_id, index_str = parts[0], parts[1]
                try:
                    index = int(index_str)
                    content, mime_type = get_job_output_image(comfy_url, job_id, index)
                except OutputNotFoundError as exc:
                    send_json(self, 404, {"error": {"message": str(exc), "type": "output_not_found"}})
                    return
                except (ImageValidationError, ValueError) as exc:
                    send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_request"}})
                    return
                except (CoordinationError, ComfyBackendError, TimeoutError, ConnectionError, OSError):
                    LOG.exception("failed to get output for job %s index %s", job_id, index_str)
                    send_json(self, 503, {"error": {"message": "Image retrieval from backend failed", "type": "backend_error"}})
                    return
                except Exception:
                    LOG.exception("unexpected error retrieving output for job %s index %s", job_id, index_str)
                    send_json(self, 503, {"error": {"message": "Unexpected error retrieving output", "type": "internal_error"}})
                    return

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", mime_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(content)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
                return
            else:
                job_id = rel
                try:
                    status_info = get_image_job_status(comfy_url, job_id)
                    send_json(self, 200, status_info)
                except (ImageValidationError, ValueError) as exc:
                    send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_request"}})
                except (CoordinationError, ComfyBackendError, TimeoutError, ConnectionError, OSError):
                    LOG.exception("failed to get status for job %s", job_id)
                    send_json(self, 503, {"error": {"message": "Job status check failed on backend", "type": "backend_error"}})
                except Exception:
                    LOG.exception("unexpected error getting status for job %s", job_id)
                    send_json(self, 503, {"error": {"message": "Unexpected error checking job status", "type": "internal_error"}})
                return
        send_json(self, 404, {"error": {"message": "not found", "type": "not_found"}})

    def authorized_body(self, disabled: str) -> dict[str, Any] | None:
        """The JSON object a holder of the node token sent; None once the refusal (404 `disabled`, 401, 400) is sent."""
        if not GPU_API_TOKEN:
            send_json(self, 404, {"error": {"type": disabled, "message": "this node has no API token"}})
            return None
        given = (self.headers.get("Authorization") or "").encode()
        if not hmac.compare_digest(given, ("Bearer " + GPU_API_TOKEN).encode()):
            send_json(self, 401, {"error": {"type": "unauthorized", "message": "Authorization: Bearer <node token>"}})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            send_json(self, 400, {"error": {"type": "invalid_json", "message": "body must be a JSON object"}})
            return None
        return body

    def reservation_api(self, path: str, method: str) -> None:
        """POST /v1/gpu/reservations, POST /v1/gpu/reservations/<id>/renew, DELETE /v1/gpu/reservations/<id>."""
        body = self.authorized_body("reservations_disabled")
        if body is None:
            return
        parts = path.split("/")  # '', 'v1', 'gpu', 'reservations', <id>, 'renew'
        if method == "POST" and len(parts) == 4:
            status, body, headers = reserve_request(body, self.client_address[0])
        elif method == "POST" and len(parts) == 6 and parts[5] == "renew":
            status, body, headers = renew_request(parts[4], body)
        elif method == "DELETE" and len(parts) == 5:
            status, body, headers = release_request(parts[4], body)
        else:
            status, body, headers = 404, {"error": {"type": "not_found", "message": path}}, {}
        send_json(self, status, body, headers)

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/v1/fleet" or getattr(RUNTIME, "catalog", None) is None:
            send_json(self, 404, {"error": {"type": "not_found", "message": path}})
            return
        body = self.authorized_body("fleet_edit_disabled")
        if body is not None:
            send_json(self, *fleet_edit(body))

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path.startswith("/v1/gpu/reservations/"):
            self.reservation_api(path, "DELETE")
            return
        send_json(self, 404, {"error": {"type": "not_found", "message": path}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/v1/gpu/reservations" or path.startswith("/v1/gpu/reservations/"):
            self.reservation_api(path, "POST")
            return
        if path == "/internal/gpu/yield":
            client_ip = self.client_address[0]
            is_loopback = False
            try:
                is_loopback = ipaddress.ip_address(client_ip).is_loopback
            except ValueError:
                is_loopback = client_ip in {"127.0.0.1", "::1", "localhost"}
            if not is_loopback:
                send_json(self, 403, {"error": {"message": "forbidden: loopback client required", "type": "forbidden"}})
                return

            lease = getattr(RUNTIME, "gpu_lease", None)
            if lease is None or not lease.is_enabled:
                send_json(self, 400, {"error": {"message": "GPU coordination is not enabled", "type": "gpu_coordination_disabled"}})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 4096:
                send_json(self, 400, {"error": {"message": "invalid body length"}})
                return

            try:
                payload = json.loads(self.rfile.read(length))
            except Exception:
                send_json(self, 400, {"error": {"message": "body must be a JSON object"}})
                return

            if (not isinstance(payload, dict) or set(payload.keys()) != {"nonce"}
                    or not isinstance(payload.get("nonce"), str) or not payload["nonce"]):
                send_json(self, 400, {"error": {"message": "yield request must contain strictly {'nonce': '<string>'}", "type": "invalid_request"}})
                return

            if not lease.validate_image_lease(payload["nonce"]):
                send_json(self, 403, {"error": {"message": "invalid or unheld image lease proof", "type": "invalid_lease_proof"}})
                return

            with RUNTIME.request_lock:
                try:
                    # The holder may have exited while this handler waited.
                    if not lease.validate_image_lease(payload["nonce"]):
                        send_json(self, 403, {"error": {"message": "image lease is no longer held"}})
                        return
                    RUNTIME.stop_backend()
                    send_json(self, 200, {"status": "released", "backend_pid": None})
                except Exception as exc:
                    RUNTIME.last_error = str(exc)
                    send_json(self, 503, {"error": {"message": str(exc), "type": "backend_stop_failed"}})
            return

        if path in {"/v1/images/jobs", "/images/jobs"}:
            reserved = active_reservation()
            if reserved is not None:
                send_json(self, 503, *gpu_refusal(None, "gpu_reserved", f"the GPU is reserved for {reserved['name']}",
                                                  None, eta=reserved["hard"] - time.monotonic()))
                return
            comfy_url = getattr(RUNTIME, "comfy_url", None)
            image_routes = getattr(RUNTIME, "image_routes", None)
            if not comfy_url or not image_routes:
                send_json(self, 404, {"error": {"message": "image routing not configured", "type": "not_found"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 4 * 1024 * 1024:
                send_json(self, 400, {"error": {"message": "invalid body length", "type": "invalid_request"}})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except Exception as exc:
                send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_json"}})
                return

            lease = getattr(RUNTIME, "gpu_lease", None)
            try:
                job_id, status = submit_image_job(comfy_url, lease, image_routes, payload)
                send_json(self, 200, {
                    "id": job_id,
                    "status": status,
                    "model": payload.get("model"),
                    "backend": "comfyui",
                })
            except UncertainSubmissionError as exc:
                LOG.warning("uncertain image job submission: %s", exc)
                send_json(self, 503, {
                    "error": {
                        "message": str(exc),
                        "type": "uncertain_submission",
                        "job_id": exc.job_id,
                    }
                })
            except (ImageValidationError, ValueError) as exc:
                send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_image_request"}})
            except CoordinationError as exc:
                LOG.warning("GPU coordinator check failed: %s", exc)
                send_json(self, 503, {"error": {"message": str(exc), "type": "gpu_coordination_error"}})
            except (ComfyBackendError, TimeoutError, ConnectionError, OSError):
                LOG.warning("ComfyUI backend submission error")
                send_json(self, 503, {"error": {"message": "Image generation backend unavailable", "type": "backend_error"}})
            except Exception:
                LOG.exception("unexpected error submitting image job")
                send_json(self, 503, {"error": {"message": "Unexpected error submitting image job", "type": "internal_error"}})
            return

        if (path.startswith("/v1/images/jobs/") or path.startswith("/images/jobs/")) and path.endswith("/cancel"):
            comfy_url = getattr(RUNTIME, "comfy_url", None)
            if not comfy_url:
                send_json(self, 404, {"error": {"message": "image routing not configured", "type": "not_found"}})
                return
            rel = path.split("/images/jobs/", 1)[1]
            job_id = rel[: -len("/cancel")].strip("/")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    self.rfile.read(min(length, 4096))
            except Exception:
                pass
            try:
                res = cancel_image_job(comfy_url, job_id)
                send_json(self, 200, res)
            except (ImageValidationError, ValueError) as exc:
                send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_request"}})
            except (CoordinationError, ComfyBackendError, TimeoutError, ConnectionError, OSError):
                send_json(self, 503, {"error": {"message": "Cancellation failed on backend", "type": "cancel_failed"}})
            except Exception:
                send_json(self, 503, {"error": {"message": "Unexpected error cancelling job", "type": "internal_error"}})
            return

        if path not in ALLOWED_POST | FLEET_POST:
            send_json(self, 404, {"error": {"message": "not found", "type": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > (1024*1024 if path in FLEET_POST else 128*1024*1024):
            send_json(self, 400, {"error": {"message": "invalid body length"}})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            requested = payload.get("model") or RUNTIME.config["fleet"]["default_model"]
            if path in FLEET_POST and (not isinstance(payload.get('model'), str) or not payload['model']):
                raise ValueError('explicit_fleet_model_required')
            if not isinstance(requested, str):
                raise ValueError('model_must_be_string')
            model_id, card = resolve_model(RUNTIME.config, requested)
            if backend_kind(card) != 'llama' and (path in FLEET_POST or BINDING_FIELD in payload):
                # Fleet counting/binding read llama.cpp /props, /apply-template and /tokenize.
                raise ValueError(f'fleet_count_unavailable_for_{backend_kind(card)}_backend')
            if backend_kind(card) == 'ninfer' and path == '/v1/chat/completions':
                payload = ninfer_chat_request(payload)
        except (ValueError, AttributeError) as exc:
            send_json(self, 400, {"error": {"message": str(exc) if str(exc) else "body must be a JSON object"}})
            return
        except KeyError:
            send_json(self, 404, {"error": {
                "message": f"unknown or unqualified model: {requested}",
                "available_models": sorted(RUNTIME.config["models"]),
            }})
            return

        binding = payload.pop(BINDING_FIELD, None)
        resident_pid = payload.pop('_tare_resident_backend_pid', None)
        if resident_pid is not None and (path != '/completion' or binding is not None
                or type(resident_pid) is not int or resident_pid <= 0
                or payload.get('n_predict') != 1 or payload.get('stream', False) is not False):
            send_json(self, 400, {'error': {'type': 'invalid_resident_readout', 'message': 'invalid resident-only request'}})
            return
        lease = getattr(RUNTIME, "gpu_lease", None) or SharedGpuLease(None)
        route_timeout = .15 if resident_pid is not None else getattr(RUNTIME, "route_timeout", 60.0)
        request_id = str(secrets.token_hex(8))
        # A reservation's holder shows its secret; everyone else waits for it to end (contract gpu-lease/2).
        reserved = active_reservation()
        holder = reserved is not None and hmac.compare_digest(
            (self.headers.get(RESERVATION_HEADER) or "").encode(), reserved["secret"].encode())
        if reserved is not None and not holder:
            send_json(self, 503, *gpu_refusal(lease, "gpu_reserved", f"the GPU is reserved for {reserved['name']}",
                                              requested, eta=reserved["hard"] - time.monotonic()))
            return

        try:
            # Text gives way to an image job within seconds; callers escalate instead of queueing.
            # The idle grace keeps the GPU between the calls of one turn (0 when the runtime sets none;
            # resident-only probes never hold it).
            grace = 0.0 if resident_pid is not None else getattr(RUNTIME, "text_idle_grace", 0.0)
            caller = {"address": self.client_address[0], "model": model_id,
                      "client": (self.headers.get("User-Agent") or "unknown")[:120]}
            with TEXT_QUEUE.turn(0 if resident_pid is not None else length, route_timeout, caller) as waited, \
                    lease.hold("text", request_id, timeout=max(.001, route_timeout - waited), reason=model_id,
                               yield_to_image=None if holder else TEXT_YIELDS_TO_IMAGE_SECONDS, linger=grace,
                               image_max_wait=None if holder else getattr(RUNTIME, "image_max_wait",
                                                                          IMAGE_MAX_WAIT_SECONDS)):
                with RUNTIME.request_lock:
                    try:
                        if resident_pid is not None and (not lease.is_enabled
                                or RUNTIME.model_id != model_id or RUNTIME.process is None
                                or RUNTIME.process.pid != resident_pid or RUNTIME.process.poll() is not None
                                or not RUNTIME.backend_health()):
                            send_json(self, 409, {'error': {'type': 'resident_backend_unavailable',
                                'message': 'resident backend changed; no model was loaded'}})
                            return
                        operations = []
                        if path == '/v1/fleet/count':
                            if set(payload) != {'model', 'request'} or binding is not None:
                                raise ValueError('fleet_count_envelope_invalid')
                            value = count_request(RUNTIME, requested, payload['request'], operations)
                            send_json(self, 200, value)
                            return
                        if path == '/v1/fleet/profile':
                            if set(payload) != {'model'} or binding is not None:
                                raise ValueError('fleet_profile_envelope_invalid')
                            RUNTIME.ensure_model(requested)
                            send_json(self, 200, {'profile': effective_profile(RUNTIME, model_id, operations),
                                                  'backend_operations': operations})
                            return
                        if binding is not None:
                            if path != '/v1/chat/completions' or type(payload.get('stream',False)) is not bool:
                                raise ValueError('fleet_binding_requires_chat_and_boolean_stream')
                            profile = check_binding(RUNTIME, requested, payload, binding, operations)
                            observed = {'binding': binding, 'profile': profile, 'backend_operations': operations,
                                'backend_operation_coverage': 'binding_checks_only_excludes_health_loading_and_generation'}
                        elif resident_pid is None:
                            RUNTIME.ensure_model(str(requested))
                            observed = None
                        else:
                            observed = None
                        payload['model'] = model_id
                        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                        try:
                            if resident_pid is not None:
                                proxy_request(self, body, timeout=15)
                            else:
                                proxy_request(self, body, fleet_observation=observed)
                            RUNTIME.last_used = time.monotonic()
                        except (ConnectionError, http.client.HTTPException, socket.error, OSError) as exc:
                            if lease.is_enabled:
                                LOG.warning("transport error during proxy; stopping backend under GPU lease: %s", exc)
                                RUNTIME.stop_backend()
                            raise
                    except BindingMismatch as exc:
                        send_json(self, 409, {'error': {'message': str(exc), 'type': 'fleet_binding_mismatch'},
                                             'backend_operations': operations})
                    except ValueError as exc:
                        send_json(self, 400, {'error': {'message': str(exc), 'type': 'invalid_fleet_request'},
                                             'backend_operations': operations})
                    except Exception as exc:
                        RUNTIME.last_error = str(exc)
                        LOG.exception("request failed for model=%s", requested)
                        send_json(self, 503, {"error": {
                            "message": str(exc),
                            "type": "qualified_model_gateway_error",
                            "model": requested,
                        }})
        except GpuBusy:
            LOG.info("GPU busy with an image job; text request for model=%s refused", requested)
            send_json(self, 503, *gpu_refusal(lease, "gpu_busy", "GPU busy with an image job; retry elsewhere or later",
                                              requested))
        except TimeoutError as exc:
            LOG.warning("GPU lease timeout for model=%s: %s", requested, exc)
            send_json(self, 503, *gpu_refusal(lease, "gpu_lease_timeout",
                                              f"timed out waiting for GPU lease ({route_timeout:.0f}s)", requested))
        except InterruptedError as exc:
            LOG.warning("GPU lease cancelled for model=%s: %s", requested, exc)
            send_json(self, 503, *gpu_refusal(lease, "gpu_lease_cancelled", "GPU lease acquisition cancelled", requested))


def port_is_free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex((host, port)) != 0


def port_is_bindable(host: str, port: int, *, reuse_address: bool = False) -> bool:
    """Probe the next server's bind policy; Windows keeps the conservative probe."""
    with socket.socket() as sock:
        try:
            if reuse_address and os.name != 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return True
        except OSError:
            return False


def wait_until_bindable(host: str, port: int, timeout: float = 90.0, *, reuse_address: bool = False) -> None:
    # NInfer can leave server-side TIME_WAIT sockets. NInfer and Strata reuse
    # those addresses on POSIX; llama-server still needs the exclusive probe.
    deadline = time.monotonic() + timeout
    while not port_is_bindable(host, port, reuse_address=reuse_address):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"backend port {port} still unavailable after {timeout:.0f}s")
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CATALOG), help="the catalog of qualified models (shipped)")
    parser.add_argument("--fleet", default=str(FLEET_FILE),
                        help="the owner's fleet: which catalog models this node serves, its default, own models "
                             "(written on the first start; edited with PUT /v1/fleet, `tare fleet` or by hand)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=18080)
    parser.add_argument("--state-dir", default=str(Path.home() / ".local/state/tare-qualified-models"))
    parser.add_argument("--preload", default=None, help="model or alias to load before listening")
    parser.add_argument("--load-timeout", type=float, default=600)
    parser.add_argument("--stop-timeout", type=float, default=90)
    parser.add_argument("--gpu-lock", default=None, help="path to cross-process shared GPU lock file")
    parser.add_argument("--route-timeout", type=float, default=60.0, help="seconds to wait for GPU lease")
    parser.add_argument("--text-idle-grace", type=float, default=TEXT_IDLE_GRACE_SECONDS,
                        help="seconds text keeps the GPU after a request, so back-to-back calls are never split "
                             "by an image or training job (0 releases at once)")
    parser.add_argument("--image-max-wait", type=float, default=IMAGE_MAX_WAIT_SECONDS,
                        help="seconds an image or training job may wait before text stops renewing its idle grace")
    parser.add_argument("--comfy-url", default=None, help="loopback HTTP URL for managed ComfyUI instance")
    parser.add_argument("--image-routes", default=None, help="path to image routes JSON configuration")
    parser.add_argument("--gpu-api-token-file", default=None,
                        help="the node's API token for GPU reservations (default <state-dir>/api-token); none: off")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    catalog, fleet_path = load_registry(args.config), Path(args.fleet)
    config, missing = compose(catalog, owner_fleet(catalog, fleet_path))
    if missing:
        LOG.warning("the fleet names models the catalog no longer has: %s", ", ".join(missing))
    if not port_is_free(args.backend_host, args.backend_port):
        raise SystemExit(f"backend port {args.backend_port} is already occupied")

    global RUNTIME
    RUNTIME = FleetRuntime(
        config,
        backend_host=args.backend_host,
        backend_port=args.backend_port,
        state_dir=Path(args.state_dir),
        load_timeout=args.load_timeout,
        stop_timeout=args.stop_timeout,
        gpu_lock=args.gpu_lock,
        route_timeout=args.route_timeout,
        comfy_url=args.comfy_url,
        image_routes_path=args.image_routes,
        text_idle_grace=args.text_idle_grace,
        image_max_wait=args.image_max_wait,
    )
    RUNTIME.catalog, RUNTIME.fleet_path, RUNTIME.fleet_missing = catalog, fleet_path, missing
    if args.preload:
        preload = args.preload if args.preload in config["models"] or args.preload in config["aliases"] else None
        if preload is None:
            LOG.warning("--preload %s is not in the fleet; preloading the default", args.preload)
        with RUNTIME.gpu_lease.hold("text", "preload", timeout=args.load_timeout):
            with RUNTIME.request_lock:
                RUNTIME.ensure_model(preload)

    server = ThreadingHTTPServer((args.host, args.port), Handler)

    def idle_watch() -> None:
        while True:
            time.sleep(30)
            with contextlib.suppress(Exception):
                RUNTIME.unload_if_idle()

    threading.Thread(target=idle_watch, name="idle-unload", daemon=True).start()

    global GPU_API_TOKEN
    token_file = Path(args.gpu_api_token_file or Path(args.state_dir) / "api-token")
    with contextlib.suppress(OSError):
        GPU_API_TOKEN = token_file.read_text(encoding="utf-8").strip() or None
    LOG.info("GPU reservations %s", "on" if GPU_API_TOKEN else f"off (no token in {token_file})")

    def reservation_watch() -> None:
        while True:  # a holder that stopped renewing frees the GPU within a second of its ttl
            time.sleep(1)
            with contextlib.suppress(Exception):
                active_reservation()

    threading.Thread(target=reservation_watch, name="gpu-reservation", daemon=True).start()

    def fleet_watch() -> None:
        while True:  # a qualified model downloaded to the node joins the fleet within a minute
            with contextlib.suppress(Exception):
                joined = join_arrivals()
                if joined:
                    LOG.info("joined the fleet (weights now on the node): %s", ", ".join(joined))
            time.sleep(60)

    threading.Thread(target=fleet_watch, name="fleet-watch", daemon=True).start()

    def shutdown(signum: int, _frame: Any) -> None:
        LOG.info("received signal %s", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("gateway listening on %s:%s; backend=%s:%s; gpu_lock=%s; comfy_url=%s", args.host, args.port,
             args.backend_host, args.backend_port, args.gpu_lock, args.comfy_url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        with RUNTIME.request_lock:
            RUNTIME.stop_backend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
