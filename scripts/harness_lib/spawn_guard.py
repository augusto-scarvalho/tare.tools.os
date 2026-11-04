#!/usr/bin/env python3
"""SPEC-153 spawn-economy guard core -- vendor-independent, pure, stdlib-only.

The single source of truth for one decision: may this spawn land grunt/fan-out
work on a FRONTIER model and burn the owner's credits? Generalizes D032 (the
`workflow run` Fable footgun -- default executor `claude` + `plan` profile ->
`fable` xhigh across a 5-way fork-join) and the AGENTS.md spawn-economy incident
(2026-07-15, a frontier session fanning read-only workers onto its own price).

Consumed by BOTH spawn funnels so the rule is enforced once, not remembered:
- the `workflow run` TEETH in scripts/harness.py (vendor-independent -- it is the
  harness's own code, runs before any subprocess spawns);
- the ADVISORY hook tools/hooks/agent_spawn_economy.py (seed on both vendors).

Failure discipline (see guard_spawn):
- fail-CLOSED on under-specification (no resolvable card/profile could default
  onto frontier -> deny);
- fail-OPEN on any internal error (this guard must never break legit tooling).

Config knob: .harness/project.json -> spawnEconomy.guard = "block"|"warn"|"off"
(default "block") and spawnEconomy.frontierCards (default DEFAULT_FRONTIER).
Owner-tunable without touching code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROJECT_REL = ".harness/project.json"

# Frontier = the premium cards that must never take default/grunt fan-out work
# without an explicit acknowledgement. Config overrides via spawnEconomy.frontierCards.
DEFAULT_FRONTIER = {"fable", "opus", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"}
# Bare (non-namespaced) cheap cards. Every nvidia-compat free card is namespaced
# "provider/model" (caught by the "/" heuristic); gemini's flash-lite is the one
# bare cheap card, listed explicitly so its tier label is accurate.
CHEAP_CARDS = {"haiku", "sonnet", "gpt-5.4-mini", "gpt-5.6-luna", "gemini-2.5-flash-lite",
               "qwen36-fast"}  # SPEC-165: the self-hosted local card runs at zero token cost
FANOUT_KINDS = {"fork-join", "map-reduce"}


def _load_project(root: Path | str) -> dict[str, Any]:
    try:
        return json.loads((Path(root) / PROJECT_REL).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _spawn_economy(root: Path | str) -> dict[str, Any]:
    cfg = _load_project(root).get("spawnEconomy")
    return cfg if isinstance(cfg, dict) else {}


def guard_mode(root: Path | str = ROOT) -> str:
    """block (default) | warn | off | block-unpinned, from spawnEconomy.guard.
    block-unpinned adds Rule 1b teeth: an unpinned frontier spawn is refused even at
    width 1 (block/warn/off leave Rule 1b as an allow+warn)."""
    mode = str(_spawn_economy(root).get("guard", "block"))
    return mode if mode in ("block", "warn", "off", "block-unpinned") else "block"


def frontier_cards(root: Path | str = ROOT) -> set[str]:
    cards = _spawn_economy(root).get("frontierCards")
    return set(str(c) for c in cards) if isinstance(cards, list) and cards else set(DEFAULT_FRONTIER)


def card_tier(root: Path | str, card_id: Any) -> str:
    """frontier | mid | cheap. Only "frontier" is load-bearing for guard_spawn;
    cheap/mid are labels. ponytail: the "/" heuristic classifies every current
    nvidia-compat/gemini-compat card (all "provider/model"); a bare cheap card
    shipping later just needs adding to CHEAP_CARDS -- no file read required."""
    card = str(card_id or "").strip()
    # "default" is the claude executor's defaultSpawn placeholder (executors.json);
    # the claude CLI resolves --model default to the engine default (fable, frontier).
    # Treat it as frontier by RULE, config-independent, so a future profile lacking a
    # claude spawn entry can never silently under-block (reckon LOW, dead path today).
    if card == "default" or card in frontier_cards(root):
        return "frontier"
    if "/" in card or card in CHEAP_CARDS:
        return "cheap"
    return "mid"


def guard_spawn(spec: dict[str, Any], root: Path | str = ROOT) -> dict[str, Any]:
    """Decide one spawn. spec = {kind, workers, card, profile, hasExplicitAck}.

    Rules (fail-CLOSED on under-spec, fail-OPEN on internal error):
      1. brawn fan-out on frontier: kind in fork-join/map-reduce AND workers>1 AND
         card is frontier AND no ack -> DENY (D032 generalized). A single
         deliberate frontier spawn (workers==1) is NOT brawn fan-out -> allow.
      2. under-specified: no profile, or no resolvable card -> DENY (a default can
         land on frontier).
      3. else ALLOW.
    """
    try:
        kind = spec.get("kind")
        workers = int(spec.get("workers") or 0)
        card = spec.get("card")
        profile = spec.get("profile")
        ack = bool(spec.get("hasExplicitAck"))
        # Rule 2 -- fail-closed under-specification.
        if not profile:
            return {"allow": False, "reason":
                    "spawn without a task-profile -- an unresolved profile can default onto a "
                    "frontier card; name the profile or use --executor nvidia-compat"}
        if not card:
            return {"allow": False, "reason":
                    f"spawn with no resolvable card for profile {profile!r} -- the default can "
                    "land on frontier; pin a card or use --executor nvidia-compat"}
        # Rule 1 -- brawn fan-out on frontier without acknowledgement.
        if kind in FANOUT_KINDS and workers > 1 and card_tier(root, card) == "frontier" and not ack:
            return {"allow": False, "reason":
                    f"{kind} fan-out of {workers} workers on frontier card {card!r} without "
                    "acknowledgement -- this burns owner credits running grunt/fan-out work on a "
                    "frontier model (D032). Pass --allow-frontier (or HARNESS_ALLOW_FRONTIER=1) to "
                    "acknowledge, or --executor nvidia-compat to run cheap. Choosing --executor "
                    "claude is NOT an acknowledgement."}
        # Rule 1b -- unpinned frontier spawn, even at width 1 (D032 generalized: an
        # omitted --executor defaults grunt work onto a frontier card). TRI-STATE on
        # explicitPin: absent/None -> rule SKIPPED (every legacy caller byte-identical,
        # absent must NEVER coerce to False); False -> unpinned; True -> a deliberate
        # pin (explicit --executor or a routing role) satisfies the rule. Frontier +
        # unpinned + no ack: block-unpinned mode DENIES; block/warn/off allow with a
        # "warn" the funnel surfaces.
        if spec.get("explicitPin") is False and card_tier(root, card) == "frontier" and not ack:
            fixes = ("Pin it with --executor nvidia-compat (cheap), a routing role for the "
                     f"profile {profile!r}, or --allow-frontier to acknowledge.")
            if guard_mode(root) == "block-unpinned":
                return {"allow": False, "reason":
                        f"unpinned frontier spawn on card {card!r} refused under "
                        f"spawnEconomy.guard=block-unpinned -- an omitted --executor defaults "
                        f"grunt work onto a frontier card even at width 1. {fixes}"}
            return {"allow": True, "reason": "ok", "warn":
                    f"unpinned frontier spawn on card {card!r} -- an omitted --executor defaults "
                    f"grunt work onto a frontier card even at width 1. {fixes} "
                    "(spawnEconomy.guard=block-unpinned refuses instead of warning.)"}
        return {"allow": True, "reason": "ok"}
    except Exception as exc:  # fail-OPEN: an internal error must never block legit tooling
        return {"allow": True, "reason": f"guard error (fail-open): {exc}"}


def first_denied_worker(root: Path | str, kind: Any, workers: int, runnable: list[dict[str, Any]],
                        executor: Any, card_resolver: Any, ack: bool,
                        pins: dict | None = None, spawn_override: dict | None = None,
                        worker_executors: dict | None = None,
                        branch_spawn_pins: dict | None = None) -> dict[str, Any]:
    """Shared spawn-funnel gate (SPEC-153 v3): walk `runnable` and return the FIRST
    worker whose real card `guard_spawn` denies, else {"denied": False, "warning": ...}.
    BOTH spawn funnels route through this -- the blocking `workflow run` teeth and the
    async `workflow start` teeth -- so the D032 refusal is enforced once, not remembered.

    `card_resolver(taskProfile, executor)` is the INJECTED seam that resolves the
    real card (executor_profile_spawn in harness.py); injecting it keeps this module
    leaf (it never imports the router). A resolver error -> card=None -> fail-CLOSED
    via guard_spawn Rule 2. `workers` is the fan-out width fed to guard_spawn.

    `pins` (workerId -> explicitPin bool) feeds Rule 1b per worker; pins=None keeps
    every explicitPin None (rule skipped -> the async funnel stays byte-identical).
    The FIRST non-denied warn verdict rides back as "warning" for the caller to surface.

    `spawn_override` (DD-mechanization P1) is the operator's uniform `--model/--effort`
    pin from `workflow run/start`; when it sets `model`, the verdict judges the PINNED
    card for EVERY worker (the card each will actually spawn), not the profile default.
    spawn_override=None is falsy -> byte-identical to the pre-pin gate (tri-state, `pins`).

    `worker_executors` and `branch_spawn_pins` are the P6 per-worker seat inputs.
    Both default to None so legacy blocking/async callers remain byte-identical.
    """
    warning = None
    for worker in runnable:
        profile = worker.get("taskProfile", "scan")
        wid = worker.get("workerId")
        try:
            card = (card_resolver(profile, (worker_executors or {}).get(wid, executor)) or {}).get("model")
        except Exception:
            card = None
        branch_pin = (branch_spawn_pins or {}).get(wid) or {}
        if branch_pin.get("model") is not None:
            card = str(branch_pin["model"])
        # DD-mechanization P1: a uniform operator `--model` pin overrides the routed
        # profile card, so the guard judges the PINNED model (the card the worker will
        # actually spawn) instead of the profile default; the refusal then names the
        # pin. `--model` absent -> spawn_override falsy -> byte-identical (tri-state,
        # like `pins`), so every legacy caller is untouched.
        if spawn_override and spawn_override.get("model") is not None:
            card = str(spawn_override["model"])
        decision = guard_spawn(
            {"kind": kind, "workers": workers, "card": card, "profile": profile,
             "hasExplicitAck": ack, "explicitPin": pins.get(wid) if pins else None}, root)
        if not decision["allow"]:
            return {"denied": True, "reason": decision["reason"], "card": card,
                    "profile": profile, "workerId": wid}
        if warning is None and decision.get("warn"):
            warning = {"reason": decision["warn"], "card": card, "profile": profile, "workerId": wid}
    return {"denied": False, "warning": warning}


def _self_check() -> int:
    root = ROOT

    def d(**spec: Any) -> bool:
        return bool(guard_spawn(spec, root)["allow"])

    # D032 literal: 5-way fork-join of planners on fable, no ack -> deny; ack -> allow.
    assert d(kind="fork-join", workers=5, card="fable", profile="plan", hasExplicitAck=False) is False
    assert d(kind="fork-join", workers=5, card="fable", profile="plan", hasExplicitAck=True) is True
    # single deliberate frontier spawn is not brawn fan-out -> allow.
    assert d(kind="fork-join", workers=1, card="fable", profile="plan", hasExplicitAck=False) is True
    # map-reduce brawn on frontier likewise denied.
    assert d(kind="map-reduce", workers=8, card="opus", profile="scan", hasExplicitAck=False) is False
    # nvidia/cheap fan-out passes at any width (not frontier).
    assert d(kind="fork-join", workers=5, card="z-ai/glm-5.2", profile="plan", hasExplicitAck=False) is True
    assert d(kind="map-reduce", workers=8, card="sonnet", profile="scan", hasExplicitAck=False) is True
    # under-specified -> fail-closed deny (card absent OR profile absent).
    assert d(kind="fork-join", workers=5, card=None, profile="plan", hasExplicitAck=False) is False
    assert d(kind="fork-join", workers=5, card="fable", profile=None, hasExplicitAck=False) is False
    # a resolvable non-frontier default (generic executor) is never blocked.
    assert d(kind="fork-join", workers=5, card="configured-by-executor", profile="plan", hasExplicitAck=False) is True
    # card_tier classification.
    assert card_tier(root, "fable") == "frontier" and card_tier(root, "opus") == "frontier"
    assert card_tier(root, "sonnet") == "cheap" and card_tier(root, "haiku") == "cheap"
    assert card_tier(root, "z-ai/glm-5.2") == "cheap"
    assert card_tier(root, "gemini-2.5-flash-lite") == "cheap"
    assert card_tier(root, "default") == "frontier"  # claude placeholder, conservative
    assert card_tier(root, "configured-by-executor") == "mid"
    # config knob resolves to a known mode; default is block on the shipped tree.
    assert guard_mode(root) in ("block", "warn", "off", "block-unpinned")

    # Rule 1b tri-state (default block mode on the shipped tree -> allow + warn, never
    # deny). explicitPin False -> allow + a "warn" naming the footgun; True (deliberate
    # pin) -> allow, no warn; absent -> allow, no warn (byte-compat: absent != False).
    def g(**spec: Any) -> dict[str, Any]:
        return guard_spawn(spec, root)
    unpinned = g(kind="fork-join", workers=1, card="fable", profile="plan", hasExplicitAck=False, explicitPin=False)
    pinned = g(kind="fork-join", workers=1, card="fable", profile="plan", hasExplicitAck=False, explicitPin=True)
    legacy = g(kind="fork-join", workers=1, card="fable", profile="plan", hasExplicitAck=False)
    assert unpinned["allow"] is True and unpinned.get("warn")
    assert pinned["allow"] is True and not pinned.get("warn")
    assert legacy["allow"] is True and not legacy.get("warn")  # absent -> Rule 1b skipped
    # a cheap card unpinned is never a Rule 1b warn; an acked frontier likewise silent.
    assert not g(kind="fork-join", workers=1, card="sonnet", profile="scan", hasExplicitAck=False, explicitPin=False).get("warn")
    assert not g(kind="fork-join", workers=1, card="fable", profile="plan", hasExplicitAck=True, explicitPin=False).get("warn")

    # first_denied_worker: the shared funnel gate over a runnable list, real card
    # resolved via an injected resolver stub. fable/plan fork-join fan-out, no ack ->
    # the first worker is denied; the same acked -> not denied; a cheap resolver ->
    # not denied; a resolver that raises -> card=None -> fail-closed deny.
    runnable = [{"workerId": "W1", "taskProfile": "plan"}, {"workerId": "W2", "taskProfile": "plan"}]

    def _fable(_profile: Any, _executor: Any) -> dict[str, Any]:
        return {"model": "fable"}

    def _cheap(_profile: Any, _executor: Any) -> dict[str, Any]:
        return {"model": "z-ai/glm-5.2"}

    def _boom(_profile: Any, _executor: Any) -> dict[str, Any]:
        raise RuntimeError("resolver down")

    deny = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, False)
    acked = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, True)
    cheap = first_denied_worker(root, "fork-join", len(runnable), runnable, "nvidia-compat", _cheap, False)
    err = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _boom, False)
    assert deny["denied"] is True and deny["workerId"] == "W1" and deny["card"] == "fable"
    assert acked["denied"] is False
    assert cheap["denied"] is False
    assert err["denied"] is True  # resolver raised -> card=None -> fail-closed

    # first_denied_worker + pins: a width-1 unpinned frontier worker surfaces a warning
    # (default block mode allows); a pinned worker and the pins=None legacy call do not.
    one = [{"workerId": "W1", "taskProfile": "plan"}]
    warned = first_denied_worker(root, "fork-join", 1, one, "claude", _fable, False, pins={"W1": False})
    pinned_w = first_denied_worker(root, "fork-join", 1, one, "claude", _fable, False, pins={"W1": True})
    legacy_w = first_denied_worker(root, "fork-join", 1, one, "claude", _fable, False)  # pins=None
    assert warned["denied"] is False and warned["warning"] and warned["warning"]["workerId"] == "W1"
    assert pinned_w["denied"] is False and pinned_w.get("warning") is None
    assert legacy_w["denied"] is False and legacy_w.get("warning") is None  # byte-identical

    # first_denied_worker + spawn_override (DD-mechanization P1): the guard judges the
    # PINNED model, not the profile default. A frontier pin over a CHEAP-default profile
    # at width>1 is denied naming the pin; a cheap pin over a FRONTIER-default profile is
    # allowed; spawn_override=None leaves the verdict byte-identical to the legacy call.
    pin_front = first_denied_worker(root, "fork-join", len(runnable), runnable, "nvidia-compat", _cheap, False, spawn_override={"model": "fable"})
    pin_cheap = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, False, spawn_override={"model": "sonnet"})
    pin_none = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, False, spawn_override=None)
    assert pin_front["denied"] is True and pin_front["card"] == "fable"  # judges the pin, not glm-5.2
    assert pin_cheap["denied"] is False  # cheap pin over a fable default is allowed
    assert pin_none == deny  # None override -> byte-identical to the no-override deny

    # P6 Slice 1: each worker is judged on its resolved executor and branch model;
    # the uniform run pin still wins, and None params preserve the legacy verdict.
    branch_front = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _cheap, False,
                                       worker_executors={"W1": "nvidia-compat", "W2": "claude"},
                                       branch_spawn_pins={"W1": {"model": "fable"}})
    branch_overridden = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, False,
                                            spawn_override={"model": "sonnet"},
                                            branch_spawn_pins={"W1": {"model": "fable"}})
    branch_none = first_denied_worker(root, "fork-join", len(runnable), runnable, "claude", _fable, False,
                                      worker_executors=None, branch_spawn_pins=None)
    assert branch_front["denied"] is True and branch_front["workerId"] == "W1" and branch_front["card"] == "fable"
    assert branch_overridden["denied"] is False
    assert branch_none == deny

    print("spawn_guard self-check: OK")
    return 0


def main() -> int:
    if "--self-check" in sys.argv:
        return _self_check()
    print("spawn_guard is a library; run with --self-check to self-test", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
