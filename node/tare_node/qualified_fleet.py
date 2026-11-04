"""The node's model fleet: our catalog, the owner's choice.

The catalog (config/qualified_model_fleet.json) is the models tare.tools qualified, with their evidence and how
to run them; it ships with the package and updates with it. The fleet, ~/.config/tare-node/fleet.json, is the
owner's: which catalog models this node serves, its default, and the owner's own models; updates never touch it.
The gateway serves `compose(catalog, fleet)`: the owner's catalog picks first (their cards stay current), then
their own models. The registry checks only what keeps the GPU safe and the gateway in charge.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
PACKAGE_PREFIX = "@TARE_NODE_PACKAGE@/"
CATALOG = DEFAULT_REGISTRY = PACKAGE_ROOT / "config" / "qualified_model_fleet.json"
FLEET_FILE = Path.home() / ".config" / "tare-node" / "fleet.json"
FLEET_SHAPE = '{"default": id, "models": [catalog ids], "custom": {id: card}, "off": [catalog ids turned off]}'
BACKEND_BINARIES = {"llama": "/llama-server", "ninfer": "/ninfer-serve", "strata": "/python"}
# NInfer 0.6.1 rejects these inside chat_template_kwargs but reads them at top level.
NINFER_TOP_LEVEL_TEMPLATE_KWARGS = ("enable_thinking", "reasoning_effort", "preserve_thinking")


def backend_kind(card: dict[str, Any]) -> str:
    return card["runtime"].get("kind", "llama")


def ninfer_chat_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Move thinking controls from chat_template_kwargs to the top level NInfer accepts."""
    kwargs = payload.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        return payload
    result, rest = dict(payload), dict(kwargs)
    for key in NINFER_TOP_LEVEL_TEMPLATE_KWARGS:
        if key in rest:
            value = rest.pop(key)
            result.setdefault(key, value)
    if rest:
        result["chat_template_kwargs"] = rest
    else:
        result.pop("chat_template_kwargs")
    return result


class FleetConfigError(ValueError):
    """The fleet registry is unsafe or internally inconsistent."""


def first_fleet(catalog: dict[str, Any]) -> dict[str, Any]:
    """A new node's fleet: the catalog models whose weights are on it, and the catalog's default."""
    here = [m for m, card in catalog["models"].items() if Path(card["artifact"]["path"]).exists()]
    default = catalog["fleet"]["default_model"]
    return {"default": default if default in here or not here else here[0], "models": here or [default],
            "custom": {}, "off": []}


def arrived(catalog: dict[str, Any], fleet: dict[str, Any]) -> list[str]:
    """Catalog models whose weights are on this node now and that the fleet neither serves nor has turned off: a
    qualified model the owner downloads joins the fleet by itself."""
    taken = set(fleet.get("models") or []) | set(fleet.get("off") or [])
    return [m for m, card in catalog["models"].items() if m not in taken and Path(card["artifact"]["path"]).exists()]


def turned_off(catalog: dict[str, Any], before: dict[str, Any], models: list[str]) -> list[str]:
    """The catalog models the owner keeps off after an edit: those already off, and those the edit took out of the
    fleet, unless the edit serves them again. These never join by themselves."""
    dropped = set(before.get("off") or []) | (set(before.get("models") or []) - set(models))
    return [m for m in catalog["models"] if m in dropped and m not in models]


def owner_fleet(catalog: dict[str, Any], path: Path = FLEET_FILE) -> dict[str, Any]:
    """The owner's fleet file; the first start writes `first_fleet` there."""
    if not path.exists():
        save_fleet(first_fleet(catalog), path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FleetConfigError(f"cannot read the fleet {path}: {exc}") from exc


def save_fleet(fleet: dict[str, Any], path: Path = FLEET_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(fleet, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)


def compose(catalog: dict[str, Any], fleet: dict[str, Any], strict: bool = False) -> tuple[dict[str, Any], list[str]]:
    """(the registry the gateway serves, the fleet's catalog ids the catalog no longer has).

    A model the catalog dropped is skipped, so an update never stops the gateway; `strict` (an edit) refuses it,
    and a default that is not served. An own model may leave out runtime.kind (llama), runtime.binary (the
    catalog's for that kind) and runtime.args.
    """
    if (not isinstance(fleet, dict) or not isinstance(fleet.get("models", []), list)
            or not isinstance(fleet.get("custom", {}), dict) or not isinstance(fleet.get("off", []), list)):
        raise FleetConfigError(f"the fleet is {FLEET_SHAPE}")
    picked = [m for m in fleet.get("models", []) if m in catalog["models"]]
    missing = [m for m in fleet.get("models", []) if m not in catalog["models"]]
    custom = fleet.get("custom", {})
    if strict and missing:
        raise FleetConfigError(f"not in the catalog: {', '.join(missing)} (an own model goes under custom)")
    if set(custom) & set(catalog["models"]):
        raise FleetConfigError(f"own models need their own names: {', '.join(sorted(set(custom) & set(catalog['models'])))}")
    binaries = {}
    for card in catalog["models"].values():
        binaries.setdefault(backend_kind(card), card["runtime"]["binary"])
    own = {}
    for model_id, card in custom.items():
        if not isinstance(card, dict) or not isinstance(card.get("runtime", {}), dict):
            raise FleetConfigError(f"{model_id}: an own model is a card with artifact.path")
        runtime = {"kind": "llama", "args": [], **card.get("runtime", {})}
        if runtime["kind"] in binaries:
            runtime.setdefault("binary", binaries[runtime["kind"]])
        own[model_id] = {**card, "runtime": runtime}
    models = {**{m: catalog["models"][m] for m in picked}, **own}
    if not models:
        raise FleetConfigError("the fleet needs at least one model")
    default = fleet.get("default")
    if strict and default not in models:
        raise FleetConfigError(f"the default {default!r} is not in the fleet")
    default = default if default in models else next(iter(models))
    section = {**catalog["fleet"], "default_model": default}
    modes = section.get("host_modes")
    if modes and not (modes["shared"] in models and modes["solo"] in models):
        section.pop("host_modes")
    aliases = {a: t for a, t in catalog["aliases"].items() if t in models and a not in models}
    aliases["default"] = default
    registry = {**catalog, "fleet": section, "models": models, "aliases": aliases}
    validate_registry(registry)
    return registry, missing


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetConfigError(f"cannot read fleet registry {config_path}: {exc}") from exc
    _resolve_local_paths(data)
    validate_registry(data)
    return data


def _resolve_local_paths(data: dict[str, Any]) -> None:
    """Resolve catalog locations before validation: `~/` under the service user's home, `@TARE_NODE_PACKAGE@/` in
    this installed package (files the catalog ships, such as a chat template)."""
    home = Path.home()
    def location(value):
        if isinstance(value, str) and value.startswith("~/"):
            return (home / value[2:]).as_posix()
        if isinstance(value, str) and value.startswith(PACKAGE_PREFIX):
            return (PACKAGE_ROOT / value[len(PACKAGE_PREFIX):]).as_posix()
        return value
    models = data.get("models", {})
    if not isinstance(models, dict):
        return
    for card in models.values():
        if not isinstance(card, dict):
            continue
        artifact, runtime = card.get("artifact", {}), card.get("runtime", {})
        if isinstance(artifact, dict) and "path" in artifact:
            artifact["path"] = location(artifact["path"])
        if not isinstance(runtime, dict):
            continue
        for key in ("binary", "server", "config"):
            if key in runtime:
                runtime[key] = location(runtime[key])
        if isinstance(runtime.get("args"), list):
            runtime["args"] = [location(value) for value in runtime["args"]]
        if isinstance(runtime.get("environment"), dict):
            runtime["environment"] = {key: location(value) for key, value in runtime["environment"].items()}


def validate_registry(data: dict[str, Any]) -> None:
    """Safety and consistency only: a card needs a runtime and an artifact path; display fields are optional."""
    if data.get("schema_version") != 1:
        raise FleetConfigError("schema_version must be 1")
    fleet = data.get("fleet")
    models = data.get("models")
    aliases = data.get("aliases")
    if not isinstance(fleet, dict) or not isinstance(models, dict) or not models:
        raise FleetConfigError("fleet and at least one model are required")
    if not isinstance(aliases, dict):
        raise FleetConfigError("aliases must be an object")
    if fleet.get("max_resident_models") != 1:
        raise FleetConfigError("this RTX 3090 fleet must fail closed at max_resident_models=1")
    if fleet.get("default_model") not in models:
        raise FleetConfigError("fleet.default_model must name a registered model")

    modes = fleet.get("host_modes")
    if modes is not None and not (
        isinstance(modes, dict) and modes.get("shared") in models and modes.get("solo") in models
        and str(modes.get("status_path", "")).endswith(".json")
        and type(modes.get("max_status_age_seconds")) is int and modes["max_status_age_seconds"] > 0
        and type(modes.get("solo_host_ram_gb")) in (int, float) and modes["solo_host_ram_gb"] > 0
        and type(modes.get("ram_margin_gb")) in (int, float) and modes["ram_margin_gb"] >= 0
    ):
        raise FleetConfigError("fleet.host_modes needs shared/solo models, status_path, max_status_age_seconds, "
                               "solo_host_ram_gb, ram_margin_gb")

    for alias, target in aliases.items():
        if not alias or target not in models:
            raise FleetConfigError(f"alias {alias!r} points to unknown model {target!r}")

    for model_id, card in models.items():
        if not model_id or not isinstance(card, dict):
            raise FleetConfigError("model ids must be non-empty objects")
        for key in ("qualified_for", "not_for", "modalities", "evidence"):
            if not isinstance(card.get(key, []), list):
                raise FleetConfigError(f"{model_id}: {key} must be a list")
        artifact = card.get("artifact", {})
        runtime = card.get("runtime", {})
        if not isinstance(artifact, dict) or not (str(artifact.get("path", "")).startswith("/")
                                                  or Path(str(artifact.get("path", ""))).is_absolute()):
            raise FleetConfigError(f"{model_id}: artifact.path must be an absolute path")
        digest = artifact.get("sha256")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
            raise FleetConfigError(f"{model_id}: artifact.sha256, when given, has 64 characters")
        kind = runtime.get("kind", "llama")
        if kind not in BACKEND_BINARIES:
            raise FleetConfigError(f"{model_id}: runtime.kind must be one of {sorted(BACKEND_BINARIES)}")
        if not str(runtime.get("binary", "")).endswith(BACKEND_BINARIES[kind]):
            raise FleetConfigError(f"{model_id}: runtime binary must be {BACKEND_BINARIES[kind].lstrip('/')}")
        if not isinstance(runtime.get("args"), list):
            raise FleetConfigError(f"{model_id}: runtime.args must be a list")
        if "example_overrides" in card and not isinstance(card["example_overrides"], dict):
            raise FleetConfigError(f"{model_id}: example_overrides must be an object")
        if any(token in runtime["args"] for token in ("--host", "--port", "--alias", "-m", "--model", "--model-id",
                                                      "--config", "--engine")):
            raise FleetConfigError(f"{model_id}: gateway-owned flags found in runtime.args")
        if kind == "strata" and not (str(runtime.get("server", "")).endswith("/serve/server.py")
                                     and str(runtime.get("config", "")).endswith(".json")
                                     and isinstance(runtime.get("model_name"), str) and runtime["model_name"]):
            raise FleetConfigError(f"{model_id}: strata needs runtime.server, runtime.config and runtime.model_name")
        idle = runtime.get("idle_unload_seconds")
        if idle is not None and (type(idle) is not int or idle < 60):
            raise FleetConfigError(f"{model_id}: runtime.idle_unload_seconds must be an integer >= 60")


def resolve_model(data: dict[str, Any], requested: str | None) -> tuple[str, dict[str, Any]]:
    name = requested or data["fleet"]["default_model"]
    seen: set[str] = set()
    while name in data.get("aliases", {}):
        if name in seen:
            raise FleetConfigError(f"alias cycle at {name}")
        seen.add(name)
        name = data["aliases"][name]
    try:
        return name, data["models"][name]
    except KeyError as exc:
        raise KeyError(requested) from exc


def recommend(data: dict[str, Any], role: str) -> tuple[str, dict[str, Any]]:
    normalized = role.strip().lower()
    if normalized in data.get("aliases", {}):
        return resolve_model(data, normalized)
    candidates = [
        (model_id, card)
        for model_id, card in data["models"].items()
        if normalized in {item.lower() for item in card.get("qualified_for", [])}
    ]
    if not candidates:
        raise KeyError(role)
    default_model = data["fleet"]["default_model"]
    candidates.sort(key=lambda pair: (
        pair[1].get("qualification") != "promoted",
        pair[0] != default_model,
        pair[0],
    ))
    return candidates[0]


def host_mode(data: dict[str, Any], status: dict[str, Any] | None, now: float,
              resident: str | None = None) -> dict[str, Any]:
    """Pick the solo model when it fits what the host has free right now; otherwise the shared one.

    Solo is "nobody else needs this memory": the solo model's host RAM plus a margin must be free on
    Windows. A resident solo model already holds its share, so it keeps fitting. Missing or stale host
    status does not fit: the shared model is the safe side. The GPU itself is the shared lease's job.
    """
    modes = data["fleet"]["host_modes"]
    needed = modes["solo_host_ram_gb"] + modes["ram_margin_gb"]
    free = status.get("windows_free_gb") if isinstance(status, dict) else None
    if not isinstance(status, dict) or not isinstance(status.get("ts"), (int, float)):
        reason = "host_status_missing"
    elif now - status["ts"] > modes["max_status_age_seconds"]:
        reason = "host_status_stale"
    elif not isinstance(free, (int, float)):
        reason = "host_status_missing"
    elif free + (modes["solo_host_ram_gb"] if resident == modes["solo"] else 0) >= needed:
        reason = "solo_fits"
    else:
        reason = "host_ram_short"
    mode = "solo" if reason == "solo_fits" else "shared"
    return {"mode": mode, "model": modes[mode], "reason": reason, "needed_free_gb": needed,
            "resident": resident, "host": status if isinstance(status, dict) else None}


def build_backend_command(
    model_id: str,
    card: dict[str, Any],
    *,
    host: str,
    port: int,
) -> list[str]:
    runtime = card["runtime"]
    if backend_kind(card) == "ninfer":
        return [runtime["binary"], card["artifact"]["path"], "--host", host, "--port", str(port),
                "--model-id", model_id, *[str(token) for token in runtime["args"]]]
    if backend_kind(card) == "strata":
        # The Strata config (engine args, pack, shards) is written by its setup; the gateway owns the address.
        return [runtime["binary"], runtime["server"], "--engine", "strata", "--config", runtime["config"],
                "--host", host, "--port", str(port), *[str(token) for token in runtime["args"]]]
    return [
        card["runtime"]["binary"],
        "-m",
        card["artifact"]["path"],
        "--alias",
        model_id,
        "--host",
        host,
        "--port",
        str(port),
        *[str(token) for token in card["runtime"]["args"]],
    ]


def public_card(model_id: str, card: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": model_id,
        "display_name": card.get("display_name", model_id),
        "qualification": card.get("qualification", "owner_choice"),
        "qualified_for": card.get("qualified_for", []),
        "not_for": card.get("not_for", []),
        "modalities": card.get("modalities", ["text"]),
        "summary": card.get("summary", ""),
        "limits": card.get("limits", ""),
        "quant": card["artifact"].get("quant"),
        "evidence": card.get("evidence", []),
    }
    if card.get("example_overrides"):
        result["example_overrides"] = card["example_overrides"]
    return result
