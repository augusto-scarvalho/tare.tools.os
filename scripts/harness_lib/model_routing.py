"""SPEC-115 model-card management + role-based model routing with fallbacks.

Two canonical, git-diffable files, one derived-live concept:
- **cards** = `.harness/routing/model-cards.json` (already the chat wizard's
  selector source, SPEC-111 R17/R18); this module adds CRUD + a reference guard.
- **routing** = NEW `.harness/routing/model-routing.json`: named profiles, an
  `activeProfile`, and `perTarget` bindings. Each profile maps a *role* (a
  task-profile name, or `overseer` — the harness default engine/model the chat
  REPL and the GUI open with) to a primary card + an ordered fallback chain.
- **canonical** = a RESERVED profile name that is never stored. It is derived
  live from `task-profiles.json` spawn blocks, so "restore canonical" is free
  and byte-compat is preserved when nothing overrides a role.

Grounding (decision -> source): LiteLLM Router fallbacks and OpenRouter provider
routing (the fallback-chain pattern); `executors.json.runtimeLimits` already
classifies rate-limit/quota/auth (detect), the chain completes the loop (fall).

Resolution precedence: perTarget > activeProfile > canonical. Consumption
(`route_spawn`, `fallback_annotation`, `chat_fallbacks`) returns its input
unchanged whenever the effective profile is canonical, so spawn output and the
chat config stay byte-identical to pre-SPEC-115 behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .common import HarnessError, emit, read_json, write_json
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import HarnessError, emit, read_json, write_json

CARDS_REL = ".harness/routing/model-cards.json"
ROUTING_REL = ".harness/routing/model-routing.json"
PROFILES_REL = ".harness/routing/task-profiles.json"
PREFS_REL = ".harness/runtime/chat-prefs.json"

CANONICAL = "canonical"            # reserved profile name, never stored
DEFAULT_EXECUTOR = "claude"        # executor used to derive a canonical role when none given
CONFIGURED = "configured-by-executor"
KNOWN_EFFORTS = ("low", "medium", "high", "xhigh", "max")
OVERSEER = "overseer"              # the harness default engine/model for delegated work; chat/GUI now open on the `router` front-desk (SPEC-144)
LEGACY_OVERSEER = "chat"           # pre-rename role key; migrated transparently on load
# roles beyond the task-profile names (overseer shown first); the two discovery
# roles are SPEC-165 R9's graphify text/image provider chains (discovery.py consumes them).
ROLE_EXTRA = (OVERSEER, "discovery-text", "discovery-image")
OVERSEER_DESC = "harness default model (delegated work; chat/GUI open on the router front-desk, `chat --role overseer` restores it)"
DISCOVERY_TEXT_DESC = "graphify LLM-assisted discovery (text) provider chain, SPEC-165 R9 -- consumed by harness_lib/discovery.py"
DISCOVERY_IMAGE_DESC = "graphify LLM-assisted discovery (image) provider chain, SPEC-165 R9 -- consumed by harness_lib/discovery.py"
ROLE_EXTRA_DESC = {OVERSEER: OVERSEER_DESC,
                   "discovery-text": DISCOVERY_TEXT_DESC, "discovery-image": DISCOVERY_IMAGE_DESC}

# KNOWN_MODELS: verified vendor catalog feeding the GUI dropdowns and `models
# catalog` (CLI parity). contextWindow only where sourced; `note` names the
# source/caveat. Sources (see specs/40-features/model-routing.md decision table):
# verdent.ai gpt-5-codex model names; openai/codex#19319 (400K in-codex window);
# codex.danielvaughan.com (GPT-5.5 1M API); developers.openai.com models; live
# banner `model: gpt-5.5` (2026-07-10, this machine). gpt-5-codex EOL 2026-07-23.
KNOWN_MODELS: dict[str, list[dict[str, Any]]] = {
    "claude": [
        {"id": "fable", "name": "Fable 5", "provider": "Anthropic",
         "reasoning": ["low", "medium", "high", "xhigh", "max"], "defaultReasoning": "high",
         "contextWindow": 1000000, "description": "Mythos-class, most capable; repo default for planning",
         "note": "contextWindow 1M observed in this repo's per-turn telemetry (2026-07-10)"},
        {"id": "claude-opus-5-5", "name": "Opus 5.5", "provider": "Anthropic",
         "reasoning": ["low", "medium", "high", "xhigh", "max"], "defaultReasoning": "high",
         "description": "frontier Opus generation; supports fast mode",
         "note": "exact model id, not the `opus` alias: the overseer guard matches the card "
                 "against the live model by substring, and `opus` matched 4.8 and 5 alike"},
        {"id": "sonnet", "name": "Sonnet 5", "provider": "Anthropic",
         "reasoning": ["low", "medium", "high"], "description": "balanced cost and latency",
         "note": "the claude CLI reports the window in per-turn telemetry"},
        {"id": "haiku", "name": "Haiku 4.5", "provider": "Anthropic",
         "reasoning": ["low", "medium", "high"], "description": "fastest and cheapest",
         "note": "the claude CLI reports the window in per-turn telemetry"},
    ],
    "codex": [
        {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"], "defaultReasoning": "high",
         "default": True,
         "description": "Latest frontier agentic coding model (Codex default flagship)",
         "note": "Active GPT-5.6 frontier flagship observed in codex CLI selector"},
        {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"], "defaultReasoning": "medium",
         "description": "Balanced agentic coding model for everyday work",
         "note": "Active GPT-5.6 balanced model observed in codex CLI selector"},
        {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"], "defaultReasoning": "low",
         "description": "Fast and affordable agentic coding model",
         "note": "Active GPT-5.6 fast model observed in codex CLI selector"},
        {"id": "gpt-5.5", "name": "GPT-5.5 (Deprecated)", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"],
         "description": "Prior GPT-5.5 (deprecated in favor of GPT-5.6 family)",
         "note": "Legacy model fell into disuse; superseded by GPT-5.6 Sol/Terra/Luna"},
        {"id": "gpt-5.4", "name": "GPT-5.4", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"], "defaultReasoning": "medium",
         "description": "prior GPT-5.4 flagship"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"],
         "description": "lighter and cheaper GPT-5.4"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"],
         "description": "coding-tuned Codex model"},
        {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex", "provider": "OpenAI",
         "reasoning": ["low", "medium", "high", "xhigh"],
         "description": "Codex for API-key workflows"},
    ],
}


def catalog_list() -> list[dict[str, Any]]:
    """Flattened KNOWN_MODELS (engine + card fields) — the CLI/GUI dropdown source."""
    return [{"engine": engine, **model} for engine, models in KNOWN_MODELS.items() for model in models]


# -- io (never-crash reads; OneDrive-tolerant writes) ------------------------

def _read(root: Path, rel: str, default: Any) -> Any:
    try:
        data = read_json(Path(root) / rel, default)
    except (json.JSONDecodeError, OSError):
        return default
    return data if data is not None else default


def _write(root: Path, rel: str, data: Any) -> None:
    """Write + one retry for OneDrive-style transient locks."""
    for attempt in (0, 1):
        try:
            write_json(Path(root) / rel, data)
            return
        except OSError:
            if attempt:
                raise
            time.sleep(0.2)


# -- cards -------------------------------------------------------------------

def _load_cards(root: Path) -> dict[str, Any]:
    data = _read(root, CARDS_REL, {})
    if not isinstance(data, dict) or not isinstance(data.get("engines"), dict):
        return {"schemaVersion": 1, "engines": {}}
    return data


def cards_list(root: Path) -> list[dict[str, Any]]:
    """Every card, flattened with its engine — the CLI/GUI grid source."""
    out: list[dict[str, Any]] = []
    for engine, block in (_load_cards(root).get("engines") or {}).items():
        for card in (block or {}).get("models") or []:
            if isinstance(card, dict) and card.get("id"):
                out.append({"engine": engine, **card})
    return out


def _find_card(root: Path, card_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for engine, block in (_load_cards(root).get("engines") or {}).items():
        for card in (block or {}).get("models") or []:
            if isinstance(card, dict) and card.get("id") == card_id:
                return engine, card
    return None, None


def card_exists(root: Path, card_id: str) -> bool:
    return _find_card(root, card_id)[1] is not None


def _engine_default_card(root: Path, engine: str) -> dict[str, Any] | None:
    """The card an engine opens with: the one flagged default, else the first."""
    block = (_load_cards(root).get("engines") or {}).get(engine) or {}
    models = [m for m in (block.get("models") or []) if isinstance(m, dict) and m.get("id")]
    if not models:
        return None
    return next((m for m in models if m.get("default")), models[0])


def _validate_card_fields(reasoning: list[str], default_reasoning: str | None) -> None:
    for level in reasoning:
        if level not in KNOWN_EFFORTS:
            raise HarnessError(f"unknown reasoning level {level!r}; known: {', '.join(KNOWN_EFFORTS)}")
    if default_reasoning and default_reasoning not in (reasoning or list(KNOWN_EFFORTS)):
        raise HarnessError(f"defaultReasoning {default_reasoning!r} not in reasoning {reasoning}")


def cards_add(root: Path, engine: str | None, card_id: str, *, name: str | None = None,
              provider: str | None = None, reasoning: list[str] | None = None,
              default_reasoning: str | None = None, context_window: int | None = None,
              description: str | None = None, default: bool = False) -> dict[str, Any]:
    if not engine or not card_id:
        raise HarnessError("models add requires --engine and an id")
    reasoning = list(reasoning or [])
    _validate_card_fields(reasoning, default_reasoning)
    data = _load_cards(root)
    block = data.setdefault("engines", {}).setdefault(engine, {"models": []})
    models = block.setdefault("models", [])
    if any(isinstance(m, dict) and m.get("id") == card_id for m in models):
        raise HarnessError(f"card {card_id!r} already exists in engine {engine!r}")
    card: dict[str, Any] = {"id": card_id}
    if name:
        card["name"] = name
    if provider:
        card["provider"] = provider
    if reasoning:
        card["reasoning"] = reasoning
    if default_reasoning:
        card["defaultReasoning"] = default_reasoning
    if description:
        card["description"] = description
    if context_window is not None:
        card["contextWindow"] = int(context_window)
    if default:
        for m in models:
            if isinstance(m, dict):
                m.pop("default", None)
        card["default"] = True
    models.append(card)
    _write(root, CARDS_REL, data)
    return {"engine": engine, **card}


def cards_update(root: Path, card_id: str, *, name: str | None = None, provider: str | None = None,
                 reasoning: list[str] | None = None, default_reasoning: str | None = None,
                 context_window: int | None = None, description: str | None = None,
                 default: bool = False) -> dict[str, Any]:
    data = _load_cards(root)
    for engine, block in (data.get("engines") or {}).items():
        models = (block or {}).get("models") or []
        for card in models:
            if not (isinstance(card, dict) and card.get("id") == card_id):
                continue
            new_reasoning = reasoning if reasoning else card.get("reasoning") or []
            _validate_card_fields(list(new_reasoning), default_reasoning or card.get("defaultReasoning"))
            if name is not None:
                card["name"] = name
            if provider is not None:
                card["provider"] = provider
            if reasoning:
                card["reasoning"] = list(reasoning)
            if default_reasoning is not None:
                card["defaultReasoning"] = default_reasoning
            if description is not None:
                card["description"] = description
            if context_window is not None:
                card["contextWindow"] = int(context_window)
            if default:
                for m in models:
                    if isinstance(m, dict):
                        m.pop("default", None)
                card["default"] = True
            _write(root, CARDS_REL, data)
            return {"engine": engine, **card}
    raise HarnessError(f"unknown card {card_id!r}")


def card_references(root: Path, card_id: str) -> list[str]:
    """Where a card is used — routing profiles + the saved chat pref."""
    refs: list[str] = []
    for pname, prof in (_load_routing(root).get("profiles") or {}).items():
        for role, rdef in (prof.get("roles") or {}).items():
            chain = [rdef.get("primary"), *(rdef.get("fallbacks") or [])]
            if any(isinstance(c, dict) and c.get("card") == card_id for c in chain):
                refs.append(f"routing:{pname}/{role}")
    prefs = _read(root, PREFS_REL, {})
    if isinstance(prefs, dict) and prefs.get("model") == card_id:
        refs.append("chat-prefs")
    return refs


def cards_remove(root: Path, card_id: str, force: bool = False) -> dict[str, Any]:
    engine, card = _find_card(root, card_id)
    if card is None:
        raise HarnessError(f"unknown card {card_id!r}")
    refs = card_references(root, card_id)
    if refs and not force:
        raise HarnessError(f"card {card_id!r} is referenced by {', '.join(refs)}; "
                           "pass --force to remove it anyway (leaves those references dangling)")
    data = _load_cards(root)
    block = data["engines"][engine]
    block["models"] = [m for m in block.get("models") or []
                       if not (isinstance(m, dict) and m.get("id") == card_id)]
    _write(root, CARDS_REL, data)
    return {"removed": card_id, "engine": engine, "hadReferences": refs}


def cards_replace(root: Path, payload: str) -> dict[str, Any]:
    """Advanced-mode raw JSON apply for model-cards.json (schema-guarded)."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("engines"), dict):
        raise HarnessError("model-cards JSON must be an object with an 'engines' object")
    for engine, block in data["engines"].items():
        for card in (block or {}).get("models") or []:
            if not (isinstance(card, dict) and card.get("id")):
                raise HarnessError(f"engine {engine!r} has a card without an id")
            _validate_card_fields(list(card.get("reasoning") or []), card.get("defaultReasoning"))
    _write(root, CARDS_REL, data)
    return {"replaced": "model-cards", "engines": sorted(data["engines"])}


# -- routing registry --------------------------------------------------------

def _load_routing(root: Path) -> dict[str, Any]:
    data = _read(root, ROUTING_REL, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schemaVersion", 1)
    data.setdefault("activeProfile", CANONICAL)
    data.setdefault("profiles", {})
    data.setdefault("perTarget", {})
    # Tolerate legacy `chat` role keys — rename to `overseer` on read (registry
    # ships empty, so this only ever fires for a pre-rename hand-edited file).
    for prof in (data.get("profiles") or {}).values():
        roles = prof.get("roles") if isinstance(prof, dict) else None
        if isinstance(roles, dict) and LEGACY_OVERSEER in roles and OVERSEER not in roles:
            roles[OVERSEER] = roles.pop(LEGACY_OVERSEER)
    return data


def _save_routing(root: Path, data: dict[str, Any]) -> None:
    _write(root, ROUTING_REL, data)


def _task_profiles(root: Path) -> dict[str, Any]:
    data = _read(root, PROFILES_REL, {})
    return (data or {}).get("profiles") or {} if isinstance(data, dict) else {}


def _role_names(root: Path) -> list[str]:
    return [*ROLE_EXTRA, *sorted(_task_profiles(root))]  # overseer first


def role_risk(root: Path, role: str) -> str | None:
    """Risk tone of a role (from its task-profile), for the GUI matrix."""
    prof = _task_profiles(root).get(role)
    return prof.get("risk") if isinstance(prof, dict) else None


def role_description(root: Path, role: str) -> str | None:
    """Human blurb for the GUI role dialog: task-profile description, or an extra role's own."""
    if role in ROLE_EXTRA_DESC:
        return ROLE_EXTRA_DESC[role]
    prof = _task_profiles(root).get(role)
    return prof.get("description") if isinstance(prof, dict) else None


def _canonical_role(root: Path, role: str, executor: str | None) -> dict[str, Any] | None:
    """The canonical routing entry for (role, executor): a task-profile spawn
    block, or — for the roleless `overseer` — the engine's default card (R21)."""
    prof = _task_profiles(root).get(role)
    if isinstance(prof, dict):
        spawn = (prof.get("spawn") or {}).get(executor or DEFAULT_EXECUTOR)
        if not isinstance(spawn, dict):
            return None
        model = spawn.get("model")
        if not model or model == CONFIGURED:
            return None
        effort = spawn.get("effort") or spawn.get("reasoning")
        entry = {"executor": executor or DEFAULT_EXECUTOR, "card": model}
        if effort:
            entry["effort"] = effort
        return entry
    if role == OVERSEER:  # harness default = engine's default card + its defaultReasoning
        engine = executor or DEFAULT_EXECUTOR
        card = _engine_default_card(root, engine)
        if not card:
            return None
        entry = {"executor": engine, "card": card["id"]}
        if card.get("defaultReasoning"):
            entry["effort"] = card["defaultReasoning"]
        return entry
    return None


def _effective_profile_name(routing: dict[str, Any], target: str | None) -> str:
    if target and (routing.get("perTarget") or {}).get(target):
        return str(routing["perTarget"][target])
    return str(routing.get("activeProfile") or CANONICAL)


def resolve_role(root: Path, role: str, executor: str | None = None,
                 target: str | None = None) -> dict[str, Any]:
    """Resolve a role to {profile, source, primary, fallbacks[, contextDiet]}.

    Precedence perTarget > activeProfile > canonical. A non-canonical profile
    that does not define this role falls through to canonical (byte-compat).
    `contextDiet` (SPEC-118 v5) rides along verbatim when the role declares it —
    the vendor-neutral session-diet intent consumed by context_diet.py."""
    routing = _load_routing(root)
    name = _effective_profile_name(routing, target)
    if name != CANONICAL:
        rdef = ((routing.get("profiles") or {}).get(name) or {}).get("roles", {}).get(role)
        if isinstance(rdef, dict) and rdef.get("primary"):
            chain = [rdef["primary"], *(rdef.get("fallbacks") or [])]
            head = 0
            if executor:  # the PRIMARY must match the caller's engine — an openai-
                # engine REPL must never receive a claude card (incident 2026-07-10:
                # the missing-model fail-closed stopped firing). Fallbacks stay
                # cross-engine: that is what "fall back on unavailability" means.
                head = next((i for i, e in enumerate(chain) if isinstance(e, dict)
                             and e.get("executor") in (None, executor)), -1)
            if head >= 0:
                source = ("perTarget" if target and (routing.get("perTarget") or {}).get(target)
                          else "activeProfile")
                res = {"profile": name, "source": source, "primary": chain[head],
                       "fallbacks": chain[:head] + chain[head + 1:]}
                if isinstance(rdef.get("contextDiet"), dict):
                    res["contextDiet"] = rdef["contextDiet"]
                if rdef.get("strategy") == "gas":  # SPEC-165 R13: opt-in rides along like contextDiet
                    res["strategy"] = "gas"
                return _enforce_playbook(root, role, res)
    return _enforce_playbook(root, role, {"profile": CANONICAL, "source": "canonical",
            "primary": _canonical_role(root, role, executor), "fallbacks": []})


def _governance_absent(root: Path) -> bool:
    """Rule 7 probe (audit dacbdc8 F1): "registry absent" means NO governance
    source at all — canonical metamodel OR legacy projection. Probing only the
    legacy file would turn enforcement OFF everywhere the day that frozen view
    is retired, while playbook_registry.load() kept resolving roles from the
    metamodel."""
    try:
        from . import playbook_registry, role_metamodel
    except ImportError:  # __main__ self-check runs the module directly
        from harness_lib import playbook_registry, role_metamodel
    return not any((Path(root) / rel).exists()
                   for rel in (role_metamodel.METAMODEL_REL, playbook_registry.REGISTRY_REL))


def _enforce_playbook(root: Path, role: str, res: dict[str, Any]) -> dict[str, Any]:
    """SPEC-170 provenance stamp (N1-C3 simplified) — TOLERANT by design.

    resolve_role is a QUERY api (GUI matrix, routing show, scenarios with
    fixture roles); rule-6 teeth live at SPAWN surfaces via enforce_spawn()
    (gate catch 2026-07-23: in-resolve refusal broke fixture-role scenarios).
    Registry absent (rule 7) or role unregistered -> `res` unchanged; on a
    resolvable chain stamp `playbookChain` + `chainHash` (sha256 first 12,
    LF-normalized) — provenance the existing consumers log for free."""
    try:
        from . import playbook_registry
    except ImportError:  # __main__ self-check runs the module directly
        from harness_lib import playbook_registry
    if _governance_absent(root):
        return res  # rule 7: no governance source yet -> additive, resolve unchanged
    try:
        chain = playbook_registry.resolve(role, playbook_registry.load(root))
    except HarnessError:
        return res  # unregistered/fixture role: query tolerates; spawn refuses
    digest = hashlib.sha256()
    for rel in chain:
        try:
            digest.update((Path(root) / rel).read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            pass  # dangling chain file: verify() grades it; hash stays stable
    res["playbookChain"] = chain
    res["chainHash"] = digest.hexdigest()[:12]
    return res


def enforce_spawn(root: Path, role: str) -> None:
    """SPEC-170 rule 6 teeth (owner Q8, HARD — no escape hatch, S1-C4 rejected):
    a SPAWN surface given a role absent from the playbook registry (or an
    unresolvable chain) refuses with the typed roster error. Registry file
    absent -> silent skip (rule 7, additive). Call sites: spawn-command,
    workflow worker start (sync + async), chat engine session launch."""
    try:
        from . import playbook_registry
    except ImportError:
        from harness_lib import playbook_registry
    if _governance_absent(root):
        return
    playbook_registry.resolve(role, playbook_registry.load(root))  # raises typed


def routing_show(root: Path, target: str | None = None) -> dict[str, Any]:
    routing = _load_routing(root)
    roles: dict[str, Any] = {}
    for role in _role_names(root):
        res = resolve_role(root, role, target=target)
        roles[role] = {"profile": res["profile"], "risk": role_risk(root, role),
                       "description": role_description(root, role),
                       "primary": res.get("primary"), "fallbacks": res.get("fallbacks"),
                       "contextDiet": res.get("contextDiet")}
    return {"activeProfile": routing.get("activeProfile", CANONICAL),
            "effectiveProfile": _effective_profile_name(routing, target),
            "profiles": sorted(routing.get("profiles") or {}),
            "perTarget": routing.get("perTarget") or {},
            "roles": roles}


def _validate_entry(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Validate a routing entry and default its executor to the card's engine."""
    if not isinstance(entry, dict) or not entry.get("card"):
        raise HarnessError("a routing entry needs a card")
    card_id = entry["card"]
    engine, card = _find_card(root, card_id)
    if card is None:
        raise HarnessError(f"unknown card {card_id!r} (add it with `models add` first)")
    effort = entry.get("effort")
    supported = card.get("reasoning") or []
    if effort and supported and effort not in supported:
        raise HarnessError(f"effort {effort!r} not supported by card {card_id!r} (supports {supported})")
    entry.setdefault("executor", engine)
    if entry["executor"] != engine:
        # SPEC-165 R2: an executor can only be routed through its OWN cards — a hop
        # naming an executor with no cards can never validate via another engine's card.
        raise HarnessError(f"card {card_id!r} belongs to executor {engine!r}, not "
                           f"{entry['executor']!r}\nfix: use a {entry['executor']!r} card, "
                           "or add one with `models add` first")
    return {k: v for k, v in entry.items() if v is not None}


def set_role(root: Path, profile: str, role: str, primary: dict[str, Any],
             fallbacks: list[dict[str, Any]], strategy: str | None = None) -> dict[str, Any]:
    if profile == CANONICAL:
        raise HarnessError("'canonical' is reserved and derived live; save a named profile first")
    if strategy not in (None, "gas", "quality"):
        raise HarnessError(f"unknown strategy {strategy!r} (gas | quality)")
    primary = _validate_entry(root, primary)
    fallbacks = [_validate_entry(root, fb) for fb in fallbacks]
    routing = _load_routing(root)
    prof = routing.setdefault("profiles", {}).setdefault(profile, {"roles": {}})
    rdef = {"primary": primary, "fallbacks": fallbacks}
    prior = prof.setdefault("roles", {}).get(role)
    if isinstance(prior, dict) and isinstance(prior.get("contextDiet"), dict):
        rdef["contextDiet"] = prior["contextDiet"]  # a model edit never drops the diet
    # R13 strategy: None preserves the prior choice (same rule as the diet);
    # "gas" opts in; "quality" is the default and stores NOTHING (no dead config).
    if strategy is None and isinstance(prior, dict) and prior.get("strategy") == "gas":
        rdef["strategy"] = "gas"
    elif strategy == "gas":
        rdef["strategy"] = "gas"
    prof["roles"][role] = rdef
    _save_routing(root, routing)
    out = {"profile": profile, "role": role, "primary": primary, "fallbacks": fallbacks}
    if rdef.get("strategy"):
        out["strategy"] = rdef["strategy"]
    return out


def set_role_diet(root: Path, profile: str, role: str,
                  diet: dict[str, Any] | None) -> dict[str, Any]:
    """Write (or clear, diet=None) a role's SPEC-118 contextDiet in a NAMED
    profile. Canonical is derived and read-only, same rule as set_role. The
    role must already exist in the profile (set-role first) so a diet never
    creates a half-defined role."""
    if profile == CANONICAL:
        raise HarnessError("'canonical' is reserved and derived live; save a named profile first")
    if diet is not None:
        keep = diet.get("keepTools")
        if keep is not None:
            try:
                from . import context_diet
            except ImportError:
                from harness_lib import context_diet
            bad = sorted(set(keep) - set(context_diet.CLAUDE_CAPABILITY_TOOLS))
            if bad:
                raise HarnessError(f"unknown capabilities {bad}; known: "
                                   f"{sorted(context_diet.CLAUDE_CAPABILITY_TOOLS)}")
    routing = _load_routing(root)
    prof = (routing.get("profiles") or {}).get(profile)
    if not isinstance(prof, dict):
        raise HarnessError(f"profile {profile!r} not found\nfix: routing profile save {profile}")
    rdef = (prof.get("roles") or {}).get(role)
    if not isinstance(rdef, dict) or not rdef.get("primary"):
        raise HarnessError(f"role {role!r} not defined in profile {profile!r}\nfix: routing set-role --profile {profile} --role {role} --primary <spec>")
    if diet is None:
        rdef.pop("contextDiet", None)
    else:
        rdef["contextDiet"] = diet
    _save_routing(root, routing)
    return {"profile": profile, "role": role, "contextDiet": rdef.get("contextDiet")}


def profile_save(root: Path, name: str) -> dict[str, Any]:
    """Create a named profile seeded from the CURRENT effective resolution."""
    if name == CANONICAL:
        raise HarnessError("'canonical' is reserved and cannot be overwritten")
    roles: dict[str, Any] = {}
    for role in _role_names(root):
        res = resolve_role(root, role)
        if res.get("primary"):
            roles[role] = {"primary": res["primary"], "fallbacks": res.get("fallbacks") or []}
            if isinstance(res.get("contextDiet"), dict):
                roles[role]["contextDiet"] = res["contextDiet"]  # diets ride profile saves
    routing = _load_routing(root)
    routing.setdefault("profiles", {})[name] = {"roles": roles}
    _save_routing(root, routing)
    return {"saved": name, "roles": sorted(roles)}


def profile_use(root: Path, name: str) -> dict[str, Any]:
    routing = _load_routing(root)
    if name != CANONICAL and name not in (routing.get("profiles") or {}):
        raise HarnessError(f"unknown profile {name!r}")
    routing["activeProfile"] = name
    _save_routing(root, routing)
    # cm-1 fix (chat-mined 2026-07-13): a saved chat model/effort pref outranks
    # the routing rung (R9/R21 precedence), so a profile switch could never
    # resolve its own overseer (R38) — the panel selector looked dead and the
    # owner's choice was silently vetoed forever. Switching profiles is the
    # NEWER user intent: demote the stale model/effort prefs (engine, target and
    # postPlanMode survive) so the new profile's overseer rung breathes again.
    return {"activeProfile": name, "chatPrefsDemoted": _demote_chat_model_prefs(root)}


def _demote_chat_model_prefs(root: Path) -> bool:
    path = root / PREFS_REL
    try:
        prefs = read_json(path, None)
    except Exception:
        return False  # corrupt prefs never block a profile switch (R10 idiom)
    if not isinstance(prefs, dict) or not (prefs.get("model") or prefs.get("effort")):
        return False
    prefs["model"] = None
    prefs["effort"] = None
    write_json(path, prefs)
    return True


def profile_delete(root: Path, name: str) -> dict[str, Any]:
    if name == CANONICAL:
        raise HarnessError("cannot delete the reserved 'canonical' profile")
    routing = _load_routing(root)
    if name not in (routing.get("profiles") or {}):
        raise HarnessError(f"unknown profile {name!r}")
    del routing["profiles"][name]
    if routing.get("activeProfile") == name:
        routing["activeProfile"] = CANONICAL
    routing["perTarget"] = {t: p for t, p in (routing.get("perTarget") or {}).items() if p != name}
    _save_routing(root, routing)
    return {"deleted": name, "activeProfile": routing["activeProfile"]}


def target_assign(root: Path, target: str, profile: str) -> dict[str, Any]:
    routing = _load_routing(root)
    if profile != CANONICAL and profile not in (routing.get("profiles") or {}):
        raise HarnessError(f"unknown profile {profile!r}")
    routing.setdefault("perTarget", {})[target] = profile
    _save_routing(root, routing)
    return {"target": target, "profile": profile}


def target_clear(root: Path, target: str) -> dict[str, Any]:
    routing = _load_routing(root)
    routing.setdefault("perTarget", {}).pop(target, None)
    _save_routing(root, routing)
    return {"cleared": target}


def routing_replace(root: Path, payload: str) -> dict[str, Any]:
    """Advanced-mode raw JSON apply for model-routing.json (schema-guarded)."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise HarnessError("model-routing JSON must be an object with a 'profiles' object")
    for pname, prof in data["profiles"].items():
        for role, rdef in (prof.get("roles") or {}).items():
            for c in [rdef.get("primary"), *(rdef.get("fallbacks") or [])]:
                if isinstance(c, dict) and c.get("card") and not card_exists(root, c["card"]):
                    raise HarnessError(f"{pname}/{role} references unknown card {c['card']!r}")
    data.setdefault("schemaVersion", 1)
    data.setdefault("activeProfile", CANONICAL)
    data.setdefault("perTarget", {})
    _save_routing(root, data)
    return {"replaced": "model-routing", "profiles": sorted(data["profiles"])}


# -- consumption (byte-identical when the effective profile is canonical) ----

def route_spawn(root: Path, role: str, executor: str, spawn: dict[str, Any] | None,
                target: str | None = None,
                override: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Override a task-profiles spawn block with the active routing profile's
    entry for this executor. Returns `spawn` unchanged for canonical / no match.

    `override` (audit-seat-sizing v1) is an explicit SIZING pin — the audit leg's
    seat class — applied LAST so it outranks the routing card. Only model/effort
    survive (a sizing knob, not a spawn escape hatch); `effort` derives
    `reasoning` because the codex/nvidia templates read that key. None/{} keeps
    every path byte-identical."""
    res = resolve_role(root, role, executor, target)
    routed = spawn
    if res["profile"] != CANONICAL:
        chain = [res.get("primary"), *(res.get("fallbacks") or [])]
        match = next((c for c in chain
                      if isinstance(c, dict) and c.get("executor") == executor and c.get("card")), None)
        if match:
            routed = dict(spawn or {})
            routed["model"] = match["card"]
            if match.get("effort"):
                routed["effort"] = match["effort"]
                routed["reasoning"] = match["effort"]
    if not override:
        final_spawn = routed
    else:
        sized = {k: v for k, v in override.items() if k in ("model", "effort")}
        if "effort" in sized:
            sized["reasoning"] = sized["effort"]
        final_spawn = {**(routed or {}), **sized}

    try:
        from harness_lib import unified_routing
        ctx = unified_routing.create_decision_context(
            task=role,
            task_type="model_routing",
            requested_role=role,
        )
        dec = unified_routing.create_route_decision(
            component="model_routing",
            input_context=ctx,
            chosen_profile=res.get("profile", CANONICAL),
            chosen_model=str((final_spawn or {}).get("model") or "default"),
            chosen_role=role,
            confidence=1.0,
            reasoning_summary=f"route_spawn resolved profile={res.get('profile')}",
            fallback_chain=[f.get("card") for f in (res.get("fallbacks") or []) if isinstance(f, dict)],
        )
        unified_routing.record_shadow_decision(ctx, dec)
    except Exception:
        pass

    return final_spawn



def apply_resume(argv: list[str], executor: dict[str, Any] | None,
                 session_id: str | None) -> list[str]:
    """Splice `resume <id>` into an ALREADY-RENDERED spawn argv so a resumed lane
    carries the IDENTICAL launch-seat pins (--model / model_reasoning_effort / --sandbox)
    the fresh spawn rendered from the routing profile — the resume can never drift onto
    the codex CLI default (measured gpt-5.5/medium), the exact seat-drop the launch guard
    denies (SPEC v4.3 / D1.5). The tokens land AFTER all the exec flags and BEFORE the
    trailing prompt positional, because codex `exec resume` accepts those global seat
    flags ONLY before `resume` (a flag after `resume` aborts clap with `unexpected
    argument`; audit 2026-08-01, confirmed against the codex CLI reference and the repo's
    own agent_spawn_economy guard "pin flags before the subcommand"). No session_id ->
    argv unchanged (byte-identical, every existing spawn untouched). The tokens are
    declared per-executor (`resume.tokens`, `{sessionId}` substituted; the literal `last`
    maps to codex's `--last`); an executor with no `resume` recipe REFUSES rather than
    emit an unpinnable command."""
    if not session_id:
        return argv
    recipe = (executor or {}).get("resume")
    if not isinstance(recipe, dict) or not recipe.get("tokens"):
        raise HarnessError("this executor declares no `resume` recipe — cannot build a "
                           "seat-pinned resume command (codex is the only resume today)")
    sid = "--last" if session_id == "last" else session_id
    tokens = [sid if t == "{sessionId}" else t for t in recipe["tokens"]]
    at = len(argv) - 1  # before the trailing prompt; every seat flag stays before `resume`
    return argv[:at] + tokens + argv[at:]


def fallback_annotation(root: Path, role: str, executor: str | None = None,
                        target: str | None = None) -> str:
    """`fable·high → opus·high → gpt-5.5·high` when a chain exists, else ''."""
    res = resolve_role(root, role, executor, target)
    if res["profile"] == CANONICAL:
        return ""
    chain = [res.get("primary"), *(res.get("fallbacks") or [])]
    parts = [f"{c['card']}·{c['effort']}" if c.get("effort") else str(c["card"])
             for c in chain if isinstance(c, dict) and c.get("card")]
    return " → ".join(parts) if len(parts) >= 2 else ""


def chat_fallbacks(root: Path, target: str | None = None,
                   role: str | None = None) -> list[tuple[str, str, str | None]]:
    """(engine, card, effort) hops for build_engine to walk on construction
    failure. Empty for canonical, so build_engine behavior is unchanged.
    `target` (SPEC-114 R36) picks up a perTarget overseer binding when present.
    `role` (SPEC-144 front-desk) walks that role's chain instead of overseer's."""
    res = resolve_role(root, role or OVERSEER, target=target)
    hops: list[tuple[str, str, str | None]] = []
    if res["profile"] == CANONICAL:
        return hops
    for fb in res.get("fallbacks") or []:
        if isinstance(fb, dict) and fb.get("card"):
            hops.append((fb.get("executor") or DEFAULT_EXECUTOR, fb["card"], fb.get("effort")))
    return hops


def spawn_chain(root: Path, role: str, executor: str | None = None,
                target: str | None = None) -> list[dict[str, Any]]:
    """Ordered [primary, *fallbacks] {executor, card, effort} hops for a worker
    spawn walk (SPEC-165 R7). Empty under canonical -> single-attempt spawn,
    byte-identical to pre-SPEC-165 (SPEC-115 invariant).

    `executor` mirrors the async failover precedent (workflow_next_failover
    resolves against the origin executor): resolve_role filters the PRIMARY to the
    caller's engine, so a chosen executor that the active profile does NOT route
    (e.g. local-llama vs a claude/codex chain) falls through to canonical -> []
    -> the operator's --executor is honored with a single attempt, never hijacked
    onto the chain's engines."""
    res = resolve_role(root, role, executor=executor, target=target)
    if res["profile"] == CANONICAL:
        return []
    hops = [{"executor": c.get("executor") or DEFAULT_EXECUTOR, "card": c["card"], "effort": c.get("effort")}
            for c in [res.get("primary"), *(res.get("fallbacks") or [])]
            if isinstance(c, dict) and c.get("card")]
    # SPEC-165 R13 gas balancer (opt-in): env wins both ways over the role field;
    # default stays byte-identical quality order (SPEC-115 invariant).
    env_mode = os.environ.get("HARNESS_SPAWN_BALANCE", "").strip().lower()
    mode = env_mode if env_mode in ("gas", "quality") else ("gas" if res.get("strategy") == "gas" else "quality")
    if mode == "gas" and len(hops) > 1:
        ordered = [hops[0], *gas_balance(hops[1:], _gas_pcts(root))]
        if ordered != hops:
            sys.stderr.write("[routing] gas balance (" + (f"env" if env_mode == "gas" else "role opt-in")
                             + "): fallbacks reordered by fuel: "
                             + " -> ".join(f"{h['executor']}·{h['card']}" for h in ordered[1:]) + "\n")
        hops = ordered
    return hops


def _gas_pcts(root: Path) -> dict[str, float]:
    """Fresh numeric gas readings per vendor from the cached fuel state
    (vendor_fuel.show annotates effectiveStatus). Stale/unavailable/pct-null
    rows are ABSENT on purpose — the 54h-rollout lesson (2026-07-27): an
    expired number routes worse than no number."""
    try:
        from . import vendor_fuel
    except ImportError:  # __main__ self-check runs the module directly
        from harness_lib import vendor_fuel
    out: dict[str, float] = {}
    try:
        vendors = (vendor_fuel.show(Path(root)) or {}).get("vendors") or {}
    except Exception:  # noqa: BLE001 — no fuel state is a valid state, never a spawn blocker
        return out
    for name, vs in vendors.items():
        if not isinstance(vs, dict) or vs.get("effectiveStatus") != "fresh":
            continue
        pct = (vs.get("summary") or {}).get("pct")
        if isinstance(pct, (int, float)):
            out[str(name)] = float(pct)
    return out


def gas_balance(fallbacks: list[dict[str, Any]], pcts: dict[str, float]) -> list[dict[str, Any]]:
    """R13 ordering: measured hops first, most gas first (stable sort — a tie
    keeps the chain's quality order); unmeasured hops after, in chain order.
    The PRIMARY is never part of this list (owner decision 2026-07-28: the
    chain's head stays the quality choice; gas mode only re-ranks fallbacks)."""
    measured = [h for h in fallbacks if h.get("executor") in pcts]
    rest = [h for h in fallbacks if h.get("executor") not in pcts]
    measured.sort(key=lambda h: -pcts[h["executor"]])
    return measured + rest


# -- CLI handlers (thin dispatch target for harness.py) ----------------------

def _emit(value: Any, *, tsv: bool = False) -> None:
    emit(value, tsv=tsv)  # TE.5: compact/TSV under HARNESS_AGENT_OUTPUT, else indent=2


def _parse_spec(text: str) -> dict[str, Any]:
    """`executor:card:effort` | `card:effort` | `card` -> a routing entry.
    The executor is filled from the card's engine by _validate_entry when omitted."""
    parts = [p.strip() for p in str(text).split(":")]
    if len(parts) >= 3:
        entry = {"executor": parts[0], "card": parts[1], "effort": parts[2]}
    elif len(parts) == 2:
        entry = {"card": parts[0], "effort": parts[1]}
    else:
        entry = {"card": parts[0]}
    return {k: v for k, v in entry.items() if v}


def _add_card_args(parser: argparse.ArgumentParser, with_engine: bool) -> None:
    parser.add_argument("id")
    if with_engine:
        parser.add_argument("--engine")
    parser.add_argument("--name")
    parser.add_argument("--provider")
    parser.add_argument("--reasoning", action="append", default=[],
                        help="repeatable effort level the card supports")
    parser.add_argument("--default-reasoning")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--description")
    parser.add_argument("--default", action="store_true")


def cmd_models(root: Path, argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="harness.py models",
                                 description="model-card CRUD over model-cards.json (SPEC-115)")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="list every card, grouped by engine")
    sub.add_parser("catalog", help="list the built-in KNOWN_MODELS catalog (parity with the GUI dropdowns)")
    sub.add_parser("show", help="show one card by id").add_argument("id")
    _add_card_args(sub.add_parser("add", help="add a card to an engine"), with_engine=True)
    _add_card_args(sub.add_parser("set", help="update fields of an existing card"), with_engine=False)
    rm = sub.add_parser("remove", help="remove a card (refused if referenced, unless --force)")
    rm.add_argument("id")
    rm.add_argument("--force", action="store_true")
    sub.add_parser("replace", help="advanced: replace the whole file from --json").add_argument(
        "--json", dest="json_payload", required=True)
    args = ap.parse_args(argv)
    if args.action == "list":
        _emit(cards_list(root), tsv=True)
    elif args.action == "catalog":
        _emit(catalog_list(), tsv=True)
    elif args.action == "show":
        engine, card = _find_card(root, args.id)
        if card is None:
            raise HarnessError(f"unknown card {args.id!r}")
        _emit({"engine": engine, **card})
    elif args.action == "add":
        _emit(cards_add(root, args.engine, args.id, name=args.name, provider=args.provider,
                        reasoning=args.reasoning, default_reasoning=args.default_reasoning,
                        context_window=args.context_window, description=args.description,
                        default=args.default))
    elif args.action == "set":
        _emit(cards_update(root, args.id, name=args.name, provider=args.provider,
                           reasoning=args.reasoning or None, default_reasoning=args.default_reasoning,
                           context_window=args.context_window, description=args.description,
                           default=args.default))
    elif args.action == "remove":
        _emit(cards_remove(root, args.id, force=args.force))
    elif args.action == "replace":
        _emit(cards_replace(root, args.json_payload))


def cmd_routing(root: Path, argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="harness.py routing",
                                 description="role-based model routing profiles (SPEC-115)")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="show resolved roles for the active/target profile").add_argument("--target")
    sr = sub.add_parser("set-role", help="set a role's primary + fallback chain in a profile")
    sr.add_argument("--profile", required=True)
    sr.add_argument("--role", required=True)
    sr.add_argument("--primary", required=True, help="executor:card:effort | card:effort | card")
    sr.add_argument("--fallback", action="append", default=[], help="repeatable, same syntax as --primary")
    sr.add_argument("--strategy", choices=["gas", "quality"], default=None,
                    help="R13 fallback ordering: 'gas' re-ranks fallbacks by fresh fuel pct at spawn; "
                         "'quality' clears it (default); omitted preserves the prior choice")
    dt = sub.add_parser("diet", help="show/set/clear a role's contextDiet (SPEC-118) in a named profile")
    dt.add_argument("--profile", required=True)
    dt.add_argument("--role", required=True)
    dt.add_argument("--keep", help="comma-separated kept capabilities (exec,todo,read,edit,web,agents,skills,plan); the rest are denied. 'off' removes the tool trim; '' denies everything")
    dt.add_argument("--user-layer", choices=["on", "off"], help="off = strip user-scope config/plugins from the session")
    dt.add_argument("--reinject", choices=["on", "off"], help="off = skip the canonical-pack reinjection")
    dt.add_argument("--clear", action="store_true", help="remove the role's contextDiet entirely")
    pf = sub.add_parser("profile", help="save|use|delete a named profile")
    pf.add_argument("op", choices=["save", "use", "delete"])
    pf.add_argument("name")
    tg = sub.add_parser("target", help="assign|clear a per-target profile binding")
    tg.add_argument("op", choices=["assign", "clear"])
    tg.add_argument("name")
    tg.add_argument("profile", nargs="?")
    sub.add_parser("restore-canonical", help="reset the active profile to canonical")
    sub.add_parser("replace", help="advanced: replace the whole file from --json").add_argument(
        "--json", dest="json_payload", required=True)
    bo = sub.add_parser("bake-off", help="RF.1 phase 1: observe-only (model, effort) bake-off "
                                         "for a role — recommends, never routes")
    bo.add_argument("role")
    bo.add_argument("--models", required=True, help="comma-separated card ids")
    bo.add_argument("--efforts", required=True, help="comma-separated effort levels")
    bo.add_argument("--task-cmd", dest="task_cmd", required=True,
                    help="command template run per cell; {model}/{effort} placeholders")
    bo.add_argument("--execute", action="store_true",
                    help="actually run the cells (operator-only: real token spend); default dry-run")
    args = ap.parse_args(argv)
    if args.action == "show":
        _emit(routing_show(root, args.target))
    elif args.action == "set-role":
        _emit(set_role(root, args.profile, args.role, _parse_spec(args.primary),
                       [_parse_spec(f) for f in args.fallback], strategy=args.strategy))
    elif args.action == "diet":
        if args.clear:
            _emit(set_role_diet(root, args.profile, args.role, None))
        elif args.keep is None and args.user_layer is None and args.reinject is None:
            # no flags = show the role's stored diet (named profile, raw file view)
            prof = (_load_routing(root).get("profiles") or {}).get(args.profile) or {}
            rdef = (prof.get("roles") or {}).get(args.role) or {}
            _emit({"profile": args.profile, "role": args.role,
                   "contextDiet": rdef.get("contextDiet")})
        else:
            # merge flags over the existing diet; on = back to vendor default (drop key)
            prof = (_load_routing(root).get("profiles") or {}).get(args.profile) or {}
            diet = dict(((prof.get("roles") or {}).get(args.role) or {}).get("contextDiet") or {})
            if args.keep is not None:
                if args.keep.strip() == "off":  # tool trim off = drop the key
                    diet.pop("keepTools", None)
                else:
                    diet["keepTools"] = [c for c in (s.strip() for s in args.keep.split(",")) if c]
            if args.user_layer is not None:
                diet.pop("vendorUserLayer", None)
                if args.user_layer == "off":
                    diet["vendorUserLayer"] = False
            if args.reinject is not None:
                diet.pop("canonicalReinject", None)
                if args.reinject == "off":
                    diet["canonicalReinject"] = False
            _emit(set_role_diet(root, args.profile, args.role, diet or None))
    elif args.action == "profile":
        _emit({"save": profile_save, "use": profile_use, "delete": profile_delete}[args.op](root, args.name))
    elif args.action == "target":
        if args.op == "assign":
            if not args.profile:
                raise HarnessError("routing target assign needs <name> <profile>")
            _emit(target_assign(root, args.name, args.profile))
        else:
            _emit(target_clear(root, args.name))
    elif args.action == "restore-canonical":
        _emit(profile_use(root, CANONICAL))
    elif args.action == "replace":
        _emit(routing_replace(root, args.json_payload))
    elif args.action == "bake-off":  # RF.1 phase 1: whole behavior lives in role_calibration
        try:
            from . import role_calibration
        except ImportError:
            from harness_lib import role_calibration
        role_calibration.run(root, args.role,
                             role_calibration.plan_cells(args.role, args.models.split(","),
                                                         args.efforts.split(","), cards_list(root)),
                             args.task_cmd, execute=args.execute)


# -- deterministic self-check (no LLM, tmp root) -----------------------------

def _self_check() -> None:
    import tempfile
    root = Path(tempfile.mkdtemp())
    (root / ".harness" / "routing").mkdir(parents=True)

    # seed task-profiles (canonical source) + model-cards
    write_json(root / PROFILES_REL, {"profiles": {
        "plan": {"risk": "medium", "spawn": {
            "claude": {"agent": "planner", "model": "fable", "effort": "high"},
            "codex": {"profile": "plan", "model": "gpt-5.5", "reasoning": "high"}}},
        "implementation": {"risk": "medium", "spawn": {
            "claude": {"agent": "implementer", "model": "opus", "effort": "xhigh"}}},
        "generic-only": {"spawn": {"generic": {"model": CONFIGURED}}}}})
    write_json(root / CARDS_REL, {"schemaVersion": 1, "engines": {
        "claude": {"models": [{"id": "fable", "reasoning": ["high", "max"],
                               "defaultReasoning": "high", "default": True},
                              {"id": "opus", "reasoning": ["high", "xhigh"]},
                              {"id": "sonnet", "reasoning": ["low", "high"]}]},
        "codex": {"models": [{"id": "gpt-5.5", "reasoning": ["low", "high"]}]}}})

    # cards CRUD round-trip
    cards_add(root, "claude", "haiku", name="Haiku", reasoning=["low", "high"], default_reasoning="low")
    assert card_exists(root, "haiku")
    assert any(c["id"] == "haiku" for c in cards_list(root))
    cards_update(root, "haiku", description="fast")
    assert _find_card(root, "haiku")[1]["description"] == "fast"
    try:
        cards_add(root, "claude", "haiku")
        raise AssertionError("duplicate id must raise")
    except HarnessError:
        pass
    try:
        cards_add(root, "claude", "z", reasoning=["nonsense"])
        raise AssertionError("bad reasoning must raise")
    except HarnessError:
        pass
    cards_remove(root, "haiku")
    assert not card_exists(root, "haiku")

    # canonical == task-profiles spawn blocks
    res = resolve_role(root, "plan", executor="claude")
    assert res["profile"] == CANONICAL and res["primary"] == {"executor": "claude", "card": "fable", "effort": "high"}, res
    assert resolve_role(root, "plan", executor="codex")["primary"]["card"] == "gpt-5.5"
    # overseer has no task-profile: canonical = the engine's default card + defaultReasoning (R21)
    ov = resolve_role(root, OVERSEER, executor="claude")
    assert ov["profile"] == CANONICAL and ov["primary"] == {"executor": "claude", "card": "fable", "effort": "high"}, ov
    assert resolve_role(root, OVERSEER, executor="codex")["primary"] == {"executor": "codex", "card": "gpt-5.5"}
    assert _canonical_role(root, "generic-only", "generic") is None  # configured-by-executor filtered
    assert _role_names(root)[0] == OVERSEER  # overseer first in the matrix
    assert catalog_list()[0]["id"] == "fable" and any(m["id"] == "gpt-5.5" for m in catalog_list())

    # route_spawn / fallback_annotation are inert under canonical (byte-compat)
    base = {"agent": "planner", "model": "fable", "effort": "high"}
    assert route_spawn(root, "plan", "claude", dict(base)) == base
    assert fallback_annotation(root, "plan", "claude") == ""
    assert chat_fallbacks(root) == []
    assert spawn_chain(root, "plan") == []                          # SPEC-165: canonical -> single-attempt spawn (empty chain)
    assert resolve_role(root, "discovery-text")["primary"] is None  # extra role w/o task-profile: empty chain, no crash
    assert spawn_chain(root, "discovery-text") == []

    # set-role + reference guard on removal
    set_role(root, "econ", "plan", _parse_spec("opus:high"),
             [_parse_spec("claude:sonnet:high"), _parse_spec("gpt-5.5:high")])
    assert card_references(root, "opus") == ["routing:econ/plan"]
    try:
        cards_remove(root, "opus")
        raise AssertionError("referenced card must not be removable without --force")
    except HarnessError:
        pass
    removed = cards_remove(root, "opus", force=True)
    assert removed["hadReferences"] == ["routing:econ/plan"]
    cards_add(root, "claude", "opus", reasoning=["high", "xhigh"])  # restore for the rest

    # effort validated against the card's reasoning list
    try:
        set_role(root, "econ", "implementation", _parse_spec("sonnet:xhigh"), [])
        raise AssertionError("unsupported effort must raise")
    except HarnessError:
        pass

    # precedence: target > activeProfile > canonical
    set_role(root, "econ", "plan", _parse_spec("opus:high"), [_parse_spec("gpt-5.5:high")])
    set_role(root, "aggr", "plan", _parse_spec("sonnet:high"), [])
    profile_use(root, "econ")
    assert resolve_role(root, "plan")["primary"]["card"] == "opus"          # activeProfile
    target_assign(root, "repo-x", "aggr")
    assert resolve_role(root, "plan", target="repo-x")["primary"]["card"] == "sonnet"  # perTarget wins
    # non-canonical profile that lacks a role falls through to canonical
    assert resolve_role(root, "implementation")["profile"] == CANONICAL

    # route_spawn now overrides for the active profile; annotation shows the chain
    routed = route_spawn(root, "plan", "claude", dict(base))
    assert routed["model"] == "opus" and routed["effort"] == "high" and routed["agent"] == "planner", routed
    assert fallback_annotation(root, "plan", "claude") == "opus·high → gpt-5.5·high"
    # SPEC-165: the active profile's ordered [primary, *fallbacks] hops for the spawn walk
    assert spawn_chain(root, "plan") == [{"executor": "claude", "card": "opus", "effort": "high"},
                                         {"executor": "codex", "card": "gpt-5.5", "effort": "high"}], spawn_chain(root, "plan")

    # overseer fallbacks feed build_engine
    set_role(root, "econ", OVERSEER, _parse_spec("claude:fable:high"), [_parse_spec("claude:sonnet:high")])
    assert chat_fallbacks(root) == [("claude", "sonnet", "high")]

    # profile lifecycle: save (seeds from active), delete restores canonical
    profile_save(root, "copy-of-active")
    assert "plan" in _load_routing(root)["profiles"]["copy-of-active"]["roles"]
    target_assign(root, "repo-e", "econ")
    profile_delete(root, "econ")
    assert _load_routing(root)["activeProfile"] == CANONICAL           # active profile deleted -> canonical
    assert "repo-e" not in _load_routing(root)["perTarget"]            # dangling binding cleared
    assert _load_routing(root)["perTarget"].get("repo-x") == "aggr"    # unrelated binding untouched
    # restore-canonical is the explicit path too
    profile_use(root, "aggr")
    profile_use(root, CANONICAL)
    assert route_spawn(root, "plan", "claude", dict(base)) == base     # back to byte-identical

    # advanced JSON apply, with schema guard
    routing_replace(root, json.dumps({"profiles": {"raw": {"roles": {
        "plan": {"primary": {"executor": "claude", "card": "fable", "effort": "high"}, "fallbacks": []}}}},
        "activeProfile": "raw"}))
    assert resolve_role(root, "plan")["primary"]["card"] == "fable"
    try:
        routing_replace(root, json.dumps({"profiles": {"bad": {"roles": {
            "plan": {"primary": {"card": "ghost"}}}}}}))
        raise AssertionError("unknown card ref must raise")
    except HarnessError:
        pass
    cards_replace(root, json.dumps({"schemaVersion": 1, "engines": {"claude": {"models": [{"id": "fable"}]}}}))
    assert [c["id"] for c in cards_list(root)] == ["fable"]

    # apply_resume (D1.5): the resume reuses the fresh argv's pins by construction, and
    # `resume <id>` lands AFTER every seat flag and before the trailing prompt — codex
    # `exec resume` rejects --model/--sandbox placed AFTER resume, so the fixture MUST
    # carry those flags (the first draft omitted --sandbox and hid the parse break).
    codex = {"resume": {"tokens": ["resume", "{sessionId}"]}}
    fresh = ["codex", "exec", "--model", "gpt-5.6", "-c", "model_reasoning_effort=high",
             "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "--skip-git-repo-check",
             "--sandbox", "workspace-write", "PROMPT"]
    assert apply_resume(fresh, codex, None) == fresh, "no session id -> byte-identical"
    resumed = apply_resume(fresh, codex, "01ABC")
    assert resumed[-3:] == ["resume", "01ABC", "PROMPT"], resumed         # id before the prompt
    assert resumed[:resumed.index("resume")] == fresh[:-1], "every seat flag kept, in order, BEFORE resume"
    assert resumed.index("resume") > resumed.index("--sandbox"), "flags precede resume (codex parse)"
    assert apply_resume(fresh, codex, "last")[-3:-1] == ["resume", "--last"], "last -> --last"
    for bad in ({}, {"resume": {}}, None):  # no recipe -> refuse, never an unpinnable command
        try:
            apply_resume(fresh, bad, "01ABC"); raise AssertionError("must refuse without a recipe")
        except HarnessError:
            pass
    print("model_routing self-check ok")


if __name__ == "__main__":
    _self_check()
