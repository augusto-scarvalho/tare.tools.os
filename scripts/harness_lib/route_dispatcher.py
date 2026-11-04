"""SPEC-144 Phase 1 -- the /route dispatcher (router skeleton, conductor A).

Pure, testable router that classifies ONE demand and produces a compact dispatch
brief. NO --loop, NO live model call, NO writeAllowed execution (Phases 2-3). The
feature is ADDITIVE: absent a `route` invocation every existing path is unchanged
(SPEC-144 invariant 1).

Four seams:
  * `deterministic_floor(root, demand)` -- the GUARDRAIL floor: profile + risk
    flags + covered-doc handles, reusing intake_triage/classify (subprocess, the
    same pattern SPEC-117 uses -- no heavy file reads, invariant 3).
  * `route_decision(demand, floor, triage_fn=None)` -> exactly
    {route, delegate, escalate, reason, workflowProfile?}. `triage_fn` is the
    INJECTABLE semantic-triage seam; the default is a clearly-marked PLACEHOLDER
    heuristic (Phase 2 wires the Sonnet 5 high call). The RISK-FLAG FLOOR is
    enforced HERE: a security/risk flag forces escalate=True regardless of the
    triage verdict -- the model can raise severity, never lower it (invariant 2).
  * `compose_brief(demand, decision, floor)` -> a COMPACT brief (handles only, no
    file-content dumps, invariant 5).
  * `dispatch_command(root, demand, decision, executor=None)` -> the specialized-
    overseer spawn command via `spawn_command` (conductor A: PRODUCED, not
    executed). route == inline -> no spawn.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

# The three-value router decision space (SPEC-144 invariant 3).
_ROUTES = ("dynamic-workflow", "pre-defined-profile", "inline")

# Placeholder-triage signals (Phase 1 only; Phase 2 replaces the whole heuristic).
_CROSS_CUTTING = ("across", "all specs", "all docs", "whole repo", "entire repo",
                  "entire repository", "cross-file", "cross cutting", "cross-cutting",
                  "repository-wide", "repo-wide", "migration")
_INLINE_PROFILES = ("scan", "cheap", "docs", "ui-validation")

# S6 (ref-arch round): Meta's "Rule of Two" — a demand that combines untrusted
# input + sensitive access + external effect is the dangerous class. The floor
# raises `rule-of-two` when 2+ of the three categories are present (any two is
# the tripwire; the classifier's own `security` signal also feeds sensitive).
# Keyword-only, same deterministic style as the security floor — a coarse net
# that can only RAISE escalation (route_decision honors any flag), never lower.
_UNTRUSTED_INPUT = ("http://", "https://", "url", "webhook", "user input",
                    "user-provided", "user provided", "pasted", "upload", "scrape",
                    "scraped", "issue comment", "issue body", "pr body", "external content",
                    "untrusted", "fetched", "third-party", "3rd party", "inbound")
_EXTERNAL_EFFECT = ("deploy", "publish", "send email", "send an email", "post to",
                    "http post", "external api", "push to prod", "production",
                    "charge", "payment", "pay ", "wire transfer", "delete from",
                    "drop table", "merge to main", "release to", "notify")
# sensitive access = the classifier's security signal OR these credential words.
_SENSITIVE_ACCESS = ("secret", "credential", "password", "api key", "api-key",
                     "token", "auth", ".env", "private key", "ssh key", "access key")


def _classify(root: Path, demand: str) -> dict[str, Any]:
    """`harness.classify(demand)` -> {profile, risk, scores}.

    intake_triage subprocesses classify because it runs in the standalone
    UserPromptSubmit hook where a full harness import is too heavy; the `route`
    verb runs INSIDE harness (already imported, and `dispatch_command` imports it
    too), so we take the "import" branch of SPEC-117's "subprocess or import"
    seam -- cheaper and no raw-spawn ratchet. Classify the demand TEXT alone
    (files=[]) so the floor is deterministic and free of the changed-file
    review-glob quirk intake_triage documents. Any failure -> {} (advisory floor;
    a broken classify must not crash the router)."""
    try:
        sys.path.insert(0, str(Path(root).resolve() / "scripts"))
        import harness  # noqa: E402  -- already in sys.modules under the `route` verb
        name, profile, scores = harness.classify(demand, files=[])
        return {"profile": name, "risk": profile.get("risk"), "scores": scores or {}}
    except Exception:
        return {}


def _covered_docs(root: Path, demand: str, limit: int = 3) -> list[str]:
    """Compact covered-doc HANDLES (doc-find + records), reusing intake_triage's
    lookups. Handles only -- never file contents (invariant 5). Never raises."""
    try:
        from harness_lib import intake_triage  # local: same package, avoids import cost at load
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from harness_lib import intake_triage
    terms = intake_triage._terms(demand)
    if not terms:
        return []
    hits: list[str] = []
    for lookup in (intake_triage._doc_hits, intake_triage._record_hits):
        try:
            hits += [h.strip() for h in lookup(root, terms, limit)]
        except Exception:
            pass
    return hits[:limit]


def deterministic_floor(root: Any, demand: str) -> dict[str, Any]:
    """The guardrail floor: profile + risk flags + covered-doc handles.

    Reuses the deterministic classifier (`intake_triage`/`classify`) -- NO heavy
    file reads (the router triages on demand text + deterministic hints, not a
    file fan-out). The security floor: a security-classed profile or any positive
    `security` risk score raises the `security` flag, which `route_decision`
    honors as a hard escalation trigger (invariant 2)."""
    root = Path(root)
    demand = (demand or "").strip()
    info = _classify(root, demand)
    profile = info.get("profile")
    scores = info.get("scores") or {}
    flags: list[str] = []
    security = profile == "security" or int(scores.get("security", 0) or 0) > 0
    if security:
        flags.append("security")
    # S6 Rule of Two: count how many of the three danger categories the demand
    # touches; 2+ is the tripwire (any single category alone is not enough).
    text = demand.lower()
    categories = (
        any(w in text for w in _UNTRUSTED_INPUT),
        security or any(w in text for w in _SENSITIVE_ACCESS),
        any(w in text for w in _EXTERNAL_EFFECT),
    )
    if sum(categories) >= 2:
        flags.append("rule-of-two")
    return {
        "demand": demand,
        "profile": profile,
        "risk": info.get("risk"),
        "riskFlags": flags,
        "coveredDocs": _covered_docs(root, demand),
        "scores": scores,
    }


def _placeholder_triage(demand: str, floor: dict[str, Any]) -> dict[str, Any]:
    # ponytail: PLACEHOLDER heuristic, NOT a model integration. Phase 2 replaces
    # this whole function with the Sonnet 5 high semantic-triage call (fallback
    # gpt-5.6-terra high) via the `router` routing role -- SPEC-144 invariant 2 /
    # plan D-b. Do NOT fabricate a model client here.
    # TODO(phase2): wire model_routing role "router"; return the model's verdict.
    text = (demand or "").lower().strip()
    profile = floor.get("profile") or ""
    if not text:
        return {}  # empty demand -> ambiguous -> route_decision escalates
    if any(w in text for w in _CROSS_CUTTING):
        return {"route": "dynamic-workflow", "delegate": True,
                "reason": "cross-cutting/large wording -> compute a workflow DAG"}
    if profile in _INLINE_PROFILES:
        return {"route": "inline", "delegate": False,
                "reason": f"low-blast profile {profile!r} -> handle inline"}
    return {"route": "pre-defined-profile", "delegate": True,
            "workflowProfile": profile or None,
            "reason": f"profile {profile!r} -> a pre-defined delivery profile"}


def route_decision(demand: str, floor: dict[str, Any] | None,
                   triage_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """One route decision: exactly {route, delegate, escalate, reason, riskTier, workflowProfile?}.

    `triage_fn(demand, floor) -> partial decision` is the injectable semantic seam
    (default: the marked PLACEHOLDER). Two floors are enforced AFTER the triage
    verdict and can only RAISE severity, never lower it:
      * ambiguity floor -- no/invalid route from triage -> escalate, never guess;
      * risk-flag floor -- any deterministic risk flag forces escalate=True
        regardless of the triage verdict (invariant 2)."""
    triage_fn = triage_fn or _placeholder_triage
    floor = floor or {}
    try:
        partial = triage_fn(demand, floor) or {}
    except Exception:
        partial = {}
    route = partial.get("route")
    delegate = bool(partial.get("delegate", False))
    escalate = bool(partial.get("escalate", False))
    reasons = [r for r in [str(partial.get("reason") or "").strip()] if r]

    if route not in _ROUTES:  # ambiguity floor: never silently guess a route
        route, escalate = "inline", True
        reasons.append("ambiguous triage -> escalate for a human route call")

    flags = floor.get("riskFlags") or []
    if flags:  # risk-flag floor (invariant 2): the model can raise, never lower
        escalate = True
        reasons.append(f"deterministic risk flag(s) {sorted(flags)} force escalation")

    try:  # SPEC-148 N5: the canonical R0-R3 tier rides as an additive sibling
        from harness_lib import sandbox_spawn
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from harness_lib import sandbox_spawn
    out: dict[str, Any] = {"route": route, "delegate": delegate,
                           "escalate": escalate, "reason": "; ".join(reasons),
                           "riskTier": sandbox_spawn.risk_tier(write_allowed=False,
                                                               risk_flags=flags)}
    wp = partial.get("workflowProfile")
    if route == "pre-defined-profile" and wp:
        out["workflowProfile"] = wp

    try:
        from harness_lib import unified_routing
        ctx = unified_routing.create_decision_context(
            task=demand,
            task_type="route_dispatcher",
            risk_tier=out.get("riskTier", "medium"),
            extra={"riskFlags": flags},
        )
        dec = unified_routing.create_route_decision(
            component="route_dispatcher",
            input_context=ctx,
            chosen_profile=str(wp or route),
            chosen_model="shadow-pass-through",
            chosen_role="router",
            confidence=1.0 if not escalate else 0.5,
            reasoning_summary="; ".join(reasons),
            applied_policies=flags,
        )
        unified_routing.record_shadow_decision(ctx, dec)
    except Exception:
        pass

    return out



def triage_prompt(demand: str, floor: dict[str, Any] | None) -> str:
    """The compact prompt the SESSION AGENT hands to the Sonnet 5 high router
    (conductor A) to produce the route-decision JSON. The router returns ONLY the
    triage verdict -- the exact key set `route_decision`/`triage_fn` consumes:
    {route, delegate, escalate, reason, workflowProfile?}. The deterministic floor
    is quoted as a GUARDRAIL the router must honor (a risk flag forces escalate=true;
    the model may RAISE severity, never lower it -- invariant 2). NO file dumps: the
    router triages on this text + the floor handles alone (invariant 3), so the
    prompt stays compact."""
    floor = floor or {}
    demand = (demand or "").strip()
    short = demand if len(demand) <= 240 else demand[:237] + "..."
    flags = sorted(floor.get("riskFlags") or [])
    handles = floor.get("coveredDocs") or []
    return "\n".join([
        "[route-triage] You are the /route dispatcher's cheap router (Sonnet 5 high).",
        "Classify ONE demand into exactly one route. Return STRICT JSON, nothing else.",
        "",
        f"demand: {short}",
        f"deterministic floor: profile={floor.get('profile')!r} riskFlags={flags}",
        "covered-doc handles: " + ("; ".join(handles) if handles else "none"),
        "",
        "routes (pick exactly one for `route`):",
        "  dynamic-workflow    - cross-cutting/large; compute a workflow DAG on the fly",
        "  pre-defined-profile - fits an existing workflow profile (name it in workflowProfile)",
        "  inline              - low blast radius; the specialized overseer handles it inline",
        "",
        "guardrail floor (invariant 2 -- RAISE severity only, never lower it):",
        "  * any riskFlag above forces escalate=true regardless of your judgement;",
        "  * ambiguous/unclassifiable -> escalate=true; never silently guess a route;",
        "  * you never author the plan -- the specialized overseer owns inline-vs-workers,",
        "    so `delegate` is an ADVISORY hint, not a command.",
        "",
        "add \"skip\": true ONLY for the SPEC-116 no-behavior-change class (no artifact",
        "warranted -- pure chatter/status/duplicate); the loop discards it, never dispatches.",
        "",
        "return JSON with EXACTLY these keys (workflowProfile only for pre-defined-profile;",
        "the optional skip key is read ONLY by the --loop driver, not by route_decision):",
        '  {"route": "...", "delegate": true|false, "escalate": true|false,',
        '   "reason": "one line", "workflowProfile": "<profile-name>", "skip": true|false}',
    ])


# The specialized overseer's typed verdicts (invariant 5's overseer->router half).
_VERDICTS = ("kept", "rejected")


def consume_verdict(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the specialized overseer's typed return into the router's next-step
    state (invariant 5). Input: {verdict: "kept"|"rejected", committedSha?,
    planDeviations?: [...], ...}. Output: {verdict, committedSha, planDeviations,
    advance, escalate, reason}. DETERMINISTIC -- no model call (the router's return
    half is plumbing, not judgement). Guardrails raise severity only:
      * a malformed/unknown verdict -> advance=false, escalate=true (never silently
        advance the feed on a bad return -- the return-side ambiguity floor);
      * verdict "kept" with NO committedSha -> escalate=true (a kept demand must show
        its commit -- invariant 4). Both kept and rejected otherwise advance the feed
        (the demand's cycle is complete; re-queue is a Phase-3 loop concern)."""
    verdict = verdict or {}
    v = str(verdict.get("verdict") or "").strip().lower()
    sha = verdict.get("committedSha") or None
    devs = verdict.get("planDeviations")
    devs = devs if isinstance(devs, list) else []
    if v not in _VERDICTS:  # return-side ambiguity floor
        return {"verdict": v or None, "committedSha": sha, "planDeviations": devs,
                "advance": False, "escalate": True,
                "reason": f"unknown verdict {v or None!r} -> escalate, do not advance the feed"}
    escalate = False
    reasons: list[str] = []
    if v == "kept" and not sha:
        escalate = True
        reasons.append("kept verdict without committedSha -> escalate (a kept demand must show its commit)")
    reasons.append("overseer rejected its own work -> demand handled, router advances"
                   if v == "rejected" else "overseer kept the committed work -> router advances")
    return {"verdict": v, "committedSha": sha, "planDeviations": devs,
            "advance": True, "escalate": escalate, "reason": "; ".join(reasons)}


def compose_brief(demand: str, decision: dict[str, Any], floor: dict[str, Any] | None) -> str:
    """A COMPACT router->overseer brief: demand + chosen route + covered-doc
    HANDLES + inline hint. NO file-content dumps (invariant 5)."""
    floor = floor or {}
    demand = (demand or "").strip()
    short = demand if len(demand) <= 240 else demand[:237] + "..."
    lines = [
        "[route-brief]",
        f"demand: {short}",
        f"route: {decision.get('route')} (delegate={decision.get('delegate')}, "
        f"escalate={decision.get('escalate')})",
    ]
    if decision.get("workflowProfile"):
        lines.append(f"workflowProfile: {decision['workflowProfile']}")
    if decision.get("reason"):
        lines.append(f"reason: {decision['reason']}")
    handles = floor.get("coveredDocs") or []
    lines.append("covered-doc handles: " + ("; ".join(handles) if handles else "none"))
    lines.append("inline hint: overseer decides inline-vs-workers (router hint is advisory)")
    return "\n".join(lines)


def dispatch_command(root: Any, demand: str, decision: dict[str, Any],
                     executor: str | None = None,
                     profile_name: str | None = None,
                     prompt_file: Any = None) -> tuple[list[str], str | None, str | None, dict[str, str]]:
    """The specialized-overseer spawn argv PLUS the RESOLVED PROFILE, the resolved
    executor, AND the packet-economy spawn env (conductor A: PRODUCED, not executed).
    route == inline -> ([], None, None, {}) (no spawn; the overseer/session handles it
    inline). The resolved executor lets the caller REFUSE an honest dispatch when it is
    the `generic` echo stub. The resolved profile (session-seat-unwired follow-up) lets
    the launcher build the detached lane's HARNESS_SESSION_* seat from the SAME
    resolution the command used, WITHOUT polluting the packet-economy env that
    pes_packet_economy pins. `profile_name` (SPEC-144 v5) threads the ROUTED profile
    straight into `spawn_command` so the packet is NOT re-classified off the dirty
    working tree (the review-glob quirk that hijacked simple UI changes).
    `prompt_file` (SPEC-144 v6, the --dispatch launch path): write the packet there
    and point the argv at it -- cmd.exe cannot carry a multi-line prompt argv
    through the claude.CMD npm shim on Windows."""
    if decision.get("route") == "inline":
        return [], None, None, {}
    sys.path.insert(0, str(Path(root).resolve() / "scripts"))
    import harness  # noqa: E402  -- reuse the real spawn seam; Phase 2 pins Fable via feature-delivery
    argv, resolved_profile, resolved_executor, env = harness.spawn_command(demand, executor, profile_name, prompt_file)
    return argv, resolved_profile, resolved_executor, env


def _selfcheck() -> None:
    root = Path(__file__).resolve().parents[2]

    # Stubs never call a real model (hermetic).
    benign = lambda d, f: {"route": "inline", "delegate": False, "escalate": False, "reason": "benign"}
    benign_escalate_false = lambda d, f: {"route": "pre-defined-profile", "delegate": True,
                                          "escalate": False, "workflowProfile": "x", "reason": "benign"}
    model_raises = lambda d, f: {"route": "inline", "delegate": False, "escalate": True, "reason": "model raised"}
    ambiguous = lambda d, f: {}

    # Exact key set (invariant 3): no extra keys, workflowProfile only when routed there.
    d = route_decision("x", {}, benign)
    assert set(d) == {"route", "delegate", "escalate", "reason", "riskTier"}, d
    assert d["route"] == "inline" and d["escalate"] is False and d["riskTier"] == "R0", d

    # Risk-flag floor forces escalation even against a benign verdict (invariant 2, rt-1).
    d = route_decision("x", {"riskFlags": ["security"]}, benign)
    assert d["escalate"] is True and "security" in d["reason"], d

    # Floor is a floor: severity rises, never falls (rt-2).
    #  - floor flag raises a model "escalate: False"
    d = route_decision("x", {"riskFlags": ["security"]}, benign_escalate_false)
    assert d["escalate"] is True, d
    #  - the model itself may raise with no floor flag; the floor never lowers it
    d = route_decision("x", {}, model_raises)
    assert d["escalate"] is True, d

    # Ambiguity floor: empty triage escalates, never silently guesses (edge case).
    d = route_decision("x", {}, ambiguous)
    assert d["route"] == "inline" and d["escalate"] is True and "ambiguous" in d["reason"], d

    # workflowProfile surfaces only for the pre-defined-profile route.
    d = route_decision("x", {}, benign_escalate_false)
    assert d.get("workflowProfile") == "x", d

    # Compact brief: handles only, no file-content dump, bounded (invariant 5, rt-3).
    floor = {"coveredDocs": ["doc: specs/40-features/route-dispatcher.md", "rec/decision: SPEC-144"]}
    brief = compose_brief("add a dark-mode toggle to the panel", route_decision("x", floor, benign), floor)
    assert "[route-brief]" in brief and "route:" in brief, brief
    assert "specs/40-features/route-dispatcher.md" in brief, brief
    assert len(brief) < 900 and brief.count("\n") < 12, ("brief not compact", len(brief))

    # Real deterministic-floor path (subprocess): a security-term demand raises the flag.
    sec = deterministic_floor(root, "add oauth jwt authentication with a secret credential store")
    assert "security" in sec.get("riskFlags", []), sec
    dsec = route_decision(sec["demand"], sec, benign)
    assert dsec["escalate"] is True, dsec  # the floor overrides the benign stub end to end

    # triage_prompt: names the demand + floor flags + the exact key set; handles only,
    # never dumps files, stays compact (invariant 3/5). Phase 2 plumbing.
    tp = triage_prompt("add oauth login", {"profile": "security", "riskFlags": ["security"],
                                           "coveredDocs": ["doc: specs/40-features/route-dispatcher.md"]})
    assert "[route-triage]" in tp and "oauth login" in tp and "security" in tp, tp
    for key in ("route", "delegate", "escalate", "reason", "workflowProfile"):
        assert key in tp, (key, tp)
    assert "def " not in tp and "import " not in tp and len(tp) < 1600, ("triage prompt not compact", len(tp))

    # consume_verdict: the overseer->router return half (invariant 5). Deterministic.
    kept = consume_verdict({"verdict": "kept", "committedSha": "abc1234", "planDeviations": []})
    assert kept == {"verdict": "kept", "committedSha": "abc1234", "planDeviations": [],
                    "advance": True, "escalate": False, "reason": kept["reason"]}, kept
    rej = consume_verdict({"verdict": "rejected",
                           "planDeviations": [{"planSaid": "x", "codeIs": "y", "action": "revert"}]})
    assert rej["advance"] is True and rej["escalate"] is False and len(rej["planDeviations"]) == 1, rej
    no_sha = consume_verdict({"verdict": "kept"})  # kept must show its commit
    assert no_sha["advance"] is True and no_sha["escalate"] is True and no_sha["committedSha"] is None, no_sha
    bad = consume_verdict({"verdict": "???"})  # return-side ambiguity floor
    assert bad["advance"] is False and bad["escalate"] is True, bad

    # dispatch_command inline shape: no spawn, no resolved executor (honest-dispatch
    # plumbing -- cmd_route refuses when the resolved executor is the generic stub).
    assert dispatch_command(root, "x", {"route": "inline"}) == ([], None, None, {})

    print("route_dispatcher self-check OK")


if __name__ == "__main__":
    _selfcheck()
