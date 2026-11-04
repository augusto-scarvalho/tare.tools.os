"""Onboarding, prefs, model cards, and project picker for the chat REPL (SPEC-111).

Extracted from chat_operator.py (QA.3 self-lib-file-budget). Owns: preference
persistence and precedence (R9/R10), the setup wizard and model-card selectors
(R11/R17/R18), the local-setup checklist (R12), and the project picker over
governed targets (R25). Exercised by chat_operator's __main__ self-check and
testing/scenarios/ux_repl_onboarding.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import model_routing, prompt_kit
    from . import targets as targets_lib
    from .chat_engines import find_codex
    from .common import read_json, write_json
except ImportError:  # run directly / flat sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import model_routing, prompt_kit
    from harness_lib import targets as targets_lib
    from harness_lib.chat_engines import find_codex
    from harness_lib.common import read_json, write_json

ENGINES = [
    ("claude", "Claude Code CLI — uses your Claude subscription", lambda: bool(shutil.which("claude"))),
    ("codex", "Codex CLI", lambda: bool(find_codex())),
    ("openai", "OpenAI-compatible endpoint — Ollama, LM Studio, vLLM, cloud", lambda: True),
    ("manual", "no agent — you type harness.py subcommands yourself", lambda: True),
]

PREFS_REL = ".harness/runtime/chat-prefs.json"  # gitignored, SPEC-111 R10
CARDS_REL = ".harness/routing/model-cards.json"  # SPEC-111 R18
CUSTOM = "__custom__"
ENGINE_DEFAULT = "__engine_default__"
# panel-chat QoL P8: the modes /approve may switch into after a plan.
POST_PLAN_MODES = ("auto", "accept-edits")


def _list_models(endpoint: str, api_key: str) -> list[str]:
    """Best-effort GET /models so the user picks from what the server offers."""
    try:
        req = urllib.request.Request(f"{endpoint}/models")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


def _load_prefs(root: Path) -> dict[str, Any]:
    try:
        data = read_json(root / PREFS_REL, {}) or {}
    except (json.JSONDecodeError, OSError):  # corrupt prefs never crash chat (R10)
        return {}
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        return {}
    return data


def _post_plan_mode(prefs: dict[str, Any]) -> str:
    """The mode /approve switches into after a plan (panel-chat QoL P8, additive
    pref, validated). Default accept-edits: an approved plan should run its edits."""
    val = prefs.get("postPlanMode")
    return val if val in POST_PLAN_MODES else "accept-edits"


def _save_prefs(root: Path, engine: str, model: str | None, effort: str | None,
                endpoint: str | None, target: str | None = None,
                post_plan_mode: str | None = None) -> None:
    prefs: dict[str, Any] = {"schemaVersion": 1, "engine": engine, "model": model,
                             "effort": effort, "endpoint": endpoint, "target": target}
    # additive (P8): preserve a prior/hand-set postPlanMode across unrelated saves
    # so it round-trips like the other prefs; only a validated value is written.
    ppm = post_plan_mode if post_plan_mode in POST_PLAN_MODES else _load_prefs(root).get("postPlanMode")
    if ppm in POST_PLAN_MODES:
        prefs["postPlanMode"] = ppm
    write_json(root / PREFS_REL, prefs)


def _resolve_config(root: Path, prefs: dict[str, Any], engine: str | None, model: str | None,
                    effort: str | None, endpoint: str | None,
                    target: str | None = None, role: str | None = None) -> dict[str, tuple[Any, str]]:
    """SPEC-111 R9 precedence, extended (SPEC-114 R21):
    flag > env > saved prefs > model-card default > vendor default.
    The card default is passed to the CLI so the panel chip shows what runs."""
    # SPEC-170 rule 6: refuse an ungoverned session role up front — the :126
    # routing rung only fires when no model flag/pref resolved, which would let
    # a flagged session through unenforced. Tmp roots (chat_operator _selfcheck)
    # skip via rule 7.
    model_routing.enforce_spawn(root, role or model_routing.OVERSEER)
    # SPEC-114 R36: the effective session target (flag > saved pref) feeds the
    # routing rung so a perTarget binding actually resolves the overseer model.
    # Read prefs["target"] here, before the engine-switch strip below can drop it.
    target = target or (prefs.get("target") if isinstance(prefs.get("target"), str) else None)
    # saved model/effort/endpoint belong to the saved engine; ignore them when
    # a flag switches engines
    if engine and prefs.get("engine") and engine != prefs["engine"]:
        prefs = {"engine": prefs["engine"]}

    def pick(flag: str | None, env_val: str | None, key: str) -> tuple[Any, str]:
        if flag:
            return flag, "flag"
        if env_val:
            return env_val, "env"
        if prefs.get(key):
            return prefs[key], "saved default"
        return None, "default"

    resolved = {"engine": pick(engine, None, "engine"),
                "model": pick(model, None, "model"),
                "effort": pick(effort, None, "effort"),
                "endpoint": pick(endpoint, os.environ.get("OPENAI_BASE_URL"), "endpoint")}
    if resolved["engine"][0] is None:
        resolved["engine"] = ("claude", "default")
    # SPEC-115: routing rung between saved prefs and the card default — the `role`
    # the chat front runs on (SPEC-144 front-desk `router` by default; `overseer`
    # restores the pre-front-desk operator). For overseer only, a canonical source
    # keeps the rung inert (byte-compat with pre-SPEC-115: fall through to the card
    # default); any other role is an explicit request, so its canonical task-profile
    # spawn cards count as a resolution too (rt-7 pins router == sonnet/high).
    # Chain: flag > env > prefs > routing > card default > vendor default.
    if resolved["model"][0] is None:
        role = role or model_routing.OVERSEER
        res = model_routing.resolve_role(root, role,
                                         executor=resolved["engine"][0], target=target)
        inert = role == model_routing.OVERSEER and res.get("source") == "canonical"
        primary = None if inert else res.get("primary")
        if primary and primary.get("card"):
            resolved["model"] = (primary["card"], "routing")
            if resolved["effort"][0] is None and primary.get("effort"):
                resolved["effort"] = (primary["effort"], "routing")
    # R21: nothing above chose a model — fall through to the engine's card default
    # (claude→fable, codex→gpt-5.5). Cardless engines (openai/manual) keep vendor
    # default. Resolved values reach build_engine, so displayed == executed.
    if resolved["model"][0] is None:
        cards = _model_cards(root, resolved["engine"][0])
        if cards:
            card = next((c for c in cards if c.get("default")), cards[0])
            resolved["model"] = (card["id"], "card default")
            if resolved["effort"][0] is None and card.get("defaultReasoning"):
                resolved["effort"] = (card["defaultReasoning"], "card default")
    return resolved


def _engine_options() -> list[prompt_kit.Option]:
    return [prompt_kit.Option(name, description=desc,
                              disabled="" if avail() else "not found on PATH")
            for name, desc, avail in ENGINES]


def _model_cards(root: Path, engine: str) -> list[dict[str, Any]]:
    """Model cards for one engine; missing/corrupt registry degrades to []."""
    try:
        data = read_json(root / CARDS_REL, {}) or {}
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        return []
    models = ((data.get("engines") or {}).get(engine) or {}).get("models") or []
    return [m for m in models if isinstance(m, dict) and m.get("id")]


def _model_options(cards: list[dict[str, Any]], live: tuple[str, ...] = (),
                   include_engine_default: bool = True,
                   prefer_id: str | None = None) -> tuple[list[prompt_kit.Option], int]:
    """Selector rows per SPEC-111 R17: cards, then endpoint-listed models, then custom.
    Preselect order (SPEC-115 B): `prefer_id` (the overseer primary) > card default."""
    opts: list[prompt_kit.Option] = []
    if include_engine_default:
        opts.append(prompt_kit.Option(ENGINE_DEFAULT, label="(engine default)",
                                      description="use the engine's own configured default"))
    default_idx = 0
    pref_idx: int | None = None
    seen: set[str] = set()
    for card in cards:
        desc = " — ".join(str(x) for x in (card.get("provider"), card.get("description")) if x)
        opts.append(prompt_kit.Option(card["id"], label=card.get("name") or card["id"],
                                      description=desc))
        seen.add(card["id"])
        if card.get("default"):
            default_idx = len(opts) - 1
        if prefer_id and card["id"] == prefer_id:
            pref_idx = len(opts) - 1
    for mid in live:
        if mid not in seen:
            opts.append(prompt_kit.Option(mid, description="listed by the endpoint"))
    opts.append(prompt_kit.Option(CUSTOM, label="custom…", description="type a model id"))
    return opts, (pref_idx if pref_idx is not None else default_idx)


def _select_model(root: Path, engine: str, mode: str,
                  live: tuple[str, ...] = (), required: bool = False) -> str | None:
    cards = _model_cards(root, engine)
    if not cards and not live:  # no enumerable choices: free text is the prompt
        if required:
            return prompt_kit.text("model (could not list from endpoint — type the model id)",
                                   default=None, mode=mode, flag_hint="--model")
        return prompt_kit.text(f"model (Enter = {engine} default)", mode=mode) or None
    prefer = (model_routing.resolve_role(root, model_routing.OVERSEER, executor=engine)
              .get("primary") or {}).get("card")
    opts, default_idx = _model_options(cards, live, include_engine_default=not required, prefer_id=prefer)
    pick = prompt_kit.select("model", opts, default=default_idx, mode=mode, flag_hint="--model")
    if pick == CUSTOM:
        return prompt_kit.text("model id", default=None, mode=mode, flag_hint="--model")
    return None if pick == ENGINE_DEFAULT else pick


def _select_effort(root: Path, engine: str, model: str | None, mode: str) -> str | None:
    """Effort levels come from the chosen model's card (SPEC-111 R18)."""
    cards = _model_cards(root, engine)
    card = next((c for c in cards if c["id"] == model), None)
    levels: list[str] = list((card or {}).get("reasoning") or [])
    if not levels:  # engine default or cardless model: offer the union of known levels
        for c in cards:
            for lvl in c.get("reasoning") or []:
                if lvl not in levels:
                    levels.append(lvl)
    if not levels:
        levels = ["low", "medium", "high"]
    pick = prompt_kit.select("effort (reasoning level)", ["default", *levels], mode=mode)
    return None if pick == "default" else pick


def _wizard(root: Path, mode: str, api_key_env: str) -> tuple[str, str | None, str | None, str | None]:
    """Onboarding journey 1 (SPEC-111 R11): engine, then per-engine config."""
    engine = prompt_kit.select("Who drives the harness?", _engine_options(),
                               mode=mode, flag_hint="--engine")
    model = effort = endpoint = None
    if engine == "claude":
        model = _select_model(root, "claude", mode)
        effort = _select_effort(root, "claude", model, mode)
    elif engine == "codex":
        model = _select_model(root, "codex", mode)
    elif engine == "openai":
        model, endpoint = _wizard_openai(root, mode, api_key_env)
    return engine, model, effort, endpoint


def _wizard_openai(root: Path, mode: str, api_key_env: str) -> tuple[str, str]:
    suggested = os.environ.get("OPENAI_BASE_URL") or "http://localhost:11434/v1"
    endpoint = prompt_kit.text("endpoint", default=suggested, mode=mode,
                               flag_hint="--endpoint").rstrip("/")
    live = tuple(_list_models(endpoint, os.environ.get(api_key_env, ""))[:20])
    model = _select_model(root, "openai", mode, live=live, required=True)
    return model, endpoint


def _provider_key_requirements(root: Path) -> list[tuple[str, str]]:
    """(label, env var) for every provider enabled in .harness/project.json."""
    project = read_json(root / ".harness" / "project.json", {}) or {}
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    llm = ((project.get("knowledgeGraph") or {}).get("llmAssisted") or {})
    if llm.get("enabled"):
        for var in llm.get("requiresEnv") or []:
            if var not in seen:
                rows.append(("gemini free-tier discovery", var))
                seen.add(var)
    nvidia = ((project.get("apiAssistedProviders") or {}).get("nvidia") or {})
    if nvidia.get("enabled"):
        for var in nvidia.get("requiresEnv") or []:
            if var not in seen:
                rows.append(("nvidia free-credits fallback", var))
                seen.add(var)
    return rows


def _checklist_rows(root: Path) -> list[tuple[str, bool, str | None, str]]:
    """(label, ok, fixable env var or None, manual fix) — SPEC-111 R12."""
    rows: list[tuple[str, bool, str | None, str]] = []
    venv_rel = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    rows.append(("project venv (.venv)", (root / venv_rel).exists(), None,
                 "create it with: uv venv --python 3.13"))
    for label, var in _provider_key_requirements(root):
        rows.append((f"{label} ({var})", bool(os.environ.get(var)), var,
                     f"store it in the OS vault with: harness.py keys set {var}"))
    return rows


def _project_checklist(root: Path, mode: str) -> None:
    """Onboarding journey 2: local-setup status + optional fixes. Never blocks."""
    rows = _checklist_rows(root)
    missing = [r for r in rows if not r[1]]
    if not missing:
        return
    print("\nlocal setup checklist:", file=sys.stderr)
    for label, ok, _var, _fix in rows:
        print(f"  [{'ok' if ok else '--'}] {label}", file=sys.stderr)
    fixable = [r for r in missing if r[2]]
    fixed: set[str] = set()
    if fixable and mode != "no-input":
        picks = prompt_kit.multi_select(
            "Fix now? (keys are entered hidden and stored in the OS vault)",
            [prompt_kit.Option(var, label) for label, _ok, var, _fix in fixable],
            mode=mode)
        from harness_lib import keys_vault
        for var in picks:
            value = prompt_kit.text(f"{var} value", default=None, secret=True, mode=mode)
            # keys-keyring v2: this used to append the secret to a plaintext .env.
            # It goes to the OS vault now — same prompt, no file on disk holding it.
            if prompt_kit.confirm(f"store {var} in the OS vault?", mode=mode):
                try:
                    # broad: keyring backends raise their own exception types too
                    keys_vault.set_key(root, var, value)
                except Exception as exc:
                    print(f"  {var} NOT stored: {exc}", file=sys.stderr)
                    continue
                os.environ.setdefault(var, value)
                fixed.add(var)
                print(f"  {var} stored in the OS vault (value not echoed)", file=sys.stderr)
    for label, _ok, var, fix in missing:
        if var not in fixed:
            print(f"  to fix '{label}': {fix}", file=sys.stderr)


def _project_candidates(root: Path) -> list[str]:
    """Pure part of the project picker: labels for fuzzy_select (SPEC-111 R25)."""
    names = sorted(targets_lib.load_targets())
    return [f"(this repo) {root.name}",
            *[f"target: {name}" for name in names],
            "browse… — register a new repo"]


def _sibling_git_repos(root: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for entry in sorted(root.parent.iterdir()):
            if entry.is_dir() and entry != root and (entry / ".git").exists():
                out.append(entry)
            if len(out) >= 50:
                break
    except OSError:
        pass
    return out


def _register_target(root: Path, name: str, path: Path) -> None:
    """Minimal governed-target profile; delegates to the single write path (R32)."""
    targets_lib.register(root, name, path)


def _pick_project(root: Path, mode: str) -> str | None:
    """Fuzzy project picker: harness repo, registered targets, or browse+register.
    Returns the chosen target name, or None for the harness repo / skip."""
    pick = prompt_kit.fuzzy_select("which project?", _project_candidates(root), mode=mode)
    if not pick or pick.startswith("(this repo)"):
        return None
    if pick.startswith("target: "):
        return pick[len("target: "):]
    repos = _sibling_git_repos(root)  # browse…
    if not repos:
        print("no sibling git repos found next to the harness repo", file=sys.stderr)
        return None
    choice = prompt_kit.fuzzy_select("register which repo?", [str(p) for p in repos], mode=mode)
    if not choice:
        return None
    path = Path(choice)
    if not prompt_kit.confirm(f"register '{path.name}' as a governed target ({path})?",
                              default=True, mode=mode):
        return None
    _register_target(root, path.name, path)
    print(f"registered target '{path.name}' at .harness/targets/{path.name}/target.json",
          file=sys.stderr)
    return path.name
