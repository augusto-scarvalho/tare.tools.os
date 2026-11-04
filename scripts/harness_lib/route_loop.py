"""SPEC-144 Phase 3 -- the /route loop driver + conductor-B commit choreography.

The Phase-1/2 router (route_dispatcher.py) classifies ONE demand. This module is
the non-interactive DRIVER (conductor B): it drains the intake feed, does the live
router triage (G2), and for each dispatchable demand runs the two-stage worker
choreography -- a read-only plan-worker, then a single confined implement-worker --
and commits ONLY through apply-merge + the SPEC-137 stamp gate. Every mechanism it
touches already exists (plan sec1.1); this module wires them, it reimplements none.

Safety (plan sec2, load-bearing):
  * the confined worker NEVER commits; the driver commits ONLY at step 10, ONLY the
    bytes apply-merge returned, ONLY after `validate --staged` stamps them;
  * the four apply-merge gate/rollback flags are HARD-CODED in `_default_merge_stage`;
  * the approval token comes ONLY from the caller (owner-typed `--approve-writes`);
    the driver has NO default token;
  * the driver captures `git status --porcelain` as a BASELINE before Stage I and
    aborts before apply-merge if NEW paths appeared beyond it (S2 delta) -- pre-existing
    runtime dirt is tolerated, never committed (the commit stage stages only apply-merge's
    returned paths);
  * the deterministic footprint gate refuses protected/state/oversize footprints
    independent of any model verdict (S6);
  * the loop is bounded: --max-demands, --max-failures, the stop sentinel, empty feed;
    the heartbeat NEVER spawns -- it returns a resume verdict only.

Seams are INJECTED so the whole driver is hermetically testable with no model, no
git and no real workflow (see `demo()` and rt-5). Self-check:
python scripts/harness_lib/route_loop.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # standalone self-check imports siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes, route_dispatcher  # noqa: E402 -- processes.run_quiet is the mediated spawn seam

STOP_REL = ".harness/state/route-loop.stop"
LOCK_REL = ".harness/state/route-loop.lock"
BRIEFS_REL = ".harness/handoff/route-briefs"
FOOTPRINT_CAP = 20  # driver-side hard cap on a demand's touched-path count (S6)
_TRIAGE_TIMEOUT_S = 180

# Footprints may NEVER touch these prefixes -- driver allowlist, no model can widen it.
_FORBIDDEN_PREFIXES = (".harness/state", ".git", "tools/git-hooks")


class _CommitGateFailed(Exception):
    """`validate --staged` refused the staged bytes -> rollback, no commit."""


# --------------------------------------------------------------------------- #
# G2: the live router triage call (resolves the `router` role, runs it bounded)
# --------------------------------------------------------------------------- #

def _brace_spans(text: str):
    """Yield every balanced {...} substring (handles model output wrapped in
    stream-json envelopes -- the verdict is an inner object)."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]


def _find_route_dict(obj: Any) -> dict[str, Any] | None:
    """First dict carrying a `route` key anywhere in the structure. String leaves
    are re-scanned as JSON -- the claude stream-json envelope wraps the verdict as
    an escaped JSON string in a `text` field."""
    if isinstance(obj, dict):
        if "route" in obj:
            return obj
        for v in obj.values():
            found = _find_route_dict(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_route_dict(v)
            if found is not None:
                return found
    elif isinstance(obj, str):
        for cand in _brace_spans(obj):
            try:
                found = _find_route_dict(json.loads(cand))
            except Exception:
                continue
            if found is not None:
                return found
    return None


def _extract_verdict(text: str) -> dict[str, Any]:
    """The LAST verdict object carrying a `route` key (raw or stream-json-wrapped).
    Garbage -> {} so the ambiguity floor escalates."""
    best: dict[str, Any] = {}
    for cand in _brace_spans(text or ""):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        found = _find_route_dict(obj)
        if isinstance(found, dict):
            best = found
    return best


def _model_triage(root: Any, demand: str, floor: dict[str, Any],
                  executor: str | None = None) -> dict[str, Any]:
    """Resolve the `router` role, render the executor command with the triage
    prompt, run it bounded, parse STRICT JSON. Any failure/garbage -> {} so the
    ambiguity floor escalates (route_dispatcher.py:148-150) -- the driver never
    fabricates a route. The verdict may carry an extra `skip` key (loop-only)."""
    import os
    import harness  # noqa: E402 -- reuse the real executor + routing seams
    from harness_lib import context_diet, model_routing
    try:
        executor = harness.active_executor(executor)
        # SPEC-170 rule 6 (audit dacbdc8 F2): this is a spawn surface too. Inside
        # the try on purpose — a refusal yields {} and the ambiguity floor
        # escalates, honoring "the driver never fabricates a route".
        model_routing.enforce_spawn(Path(root), "router")
        role = model_routing.resolve_role(Path(root), "router", executor)
        primary = role.get("primary") or {}
        spawn = {"profile": "router", "agent": "router",
                 "model": str(primary.get("card", "")),
                 "effort": str(primary.get("effort", "")),
                 "reasoning": str(primary.get("effort", ""))}
        prompt = route_dispatcher.triage_prompt(demand, floor)
        values = {"prompt": prompt, "profileName": "router", "profile": spawn["profile"],
                  "model": spawn["model"], "effort": spawn["effort"],
                  "reasoning": spawn["reasoning"], "agent": spawn["agent"],
                  "python": sys.executable, "root": str(root),
                  "sandbox": "read-only"}  # S3: the triage router never writes
        argv = harness._resolve_argv0(harness.render_template(
            harness.executor_config(executor).get("commandTemplate", []), values))
        # SPEC-118 v5: the router role's contextDiet, spliced in FRONT of the
        # rendered {prompt} tail. keepTools [] makes the playbook's "MUST NOT use
        # tools" structural (zero built-in tool schemas); the worker template
        # itself stays byte-identical (tail:template-diet). tool_flags ONLY: the
        # layer flags are already on the claude template (v4) -- flags_for here
        # would emit them twice.
        trim = context_diet.tool_flags(executor, role.get("contextDiet"))
        if trim:
            argv = argv[:-1] + trim + argv[-1:]  # every template ends with {prompt}
        # The router is a spawned worker: skip the SessionStart canonical-context
        # reinjection so the cheap tier does not pay ~5K tokens of overseer context
        # per triage (honored by tools/hooks/reload_context_after_compact.py).
        # contextDiet's env_for is the same knob -- the explicit set stays as the
        # fail-safe should the role's diet ever be removed.
        proc = processes.run_quiet(argv, timeout=_TRIAGE_TIMEOUT_S, cwd=str(root),
                                   env={**os.environ, "HARNESS_SKIP_REINJECT": "1",
                                        **context_diet.env_for(role.get("contextDiet"))},
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        return _extract_verdict(proc.stdout or "")
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Deterministic driver-side footprint gate (S6) + clean-tree assertion (S2)
# --------------------------------------------------------------------------- #

def _footprint_paths(footprint: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("modify", "create", "delete"):
        for p in footprint.get(key, []) or []:
            out.append(str(p).replace("\\", "/").strip().lstrip("/"))
    return out


def footprint_gate(root: Any, footprint: dict[str, Any], *, cap: int = FOOTPRINT_CAP) -> tuple[bool, str]:
    """Refuse+escalate on protected paths, harness-state/.git/hooks, or oversize.
    Independent of any model verdict (S6). Returns (ok, reason)."""
    from harness_lib import protected_files
    paths = _footprint_paths(footprint)
    if not paths:
        return False, "empty footprint -> nothing to implement (escalate)"
    if len(paths) > cap:
        return False, f"footprint {len(paths)} paths exceeds cap {cap}"
    try:
        protected = {str(p).replace("\\", "/") for p in protected_files.protected_paths(Path(root))}
    except Exception:
        protected = set()
    for p in paths:
        if p in protected:
            return False, f"footprint touches protected file {p!r}"
        if any(p == pre or p.startswith(pre + "/") for pre in _FORBIDDEN_PREFIXES):
            return False, f"footprint touches forbidden path {p!r}"
    return True, "footprint within the driver allowlist"


def _porcelain_set(root: Any) -> frozenset[str] | None:
    """Set of `git status --porcelain` path tokens; None when git itself fails
    (S2 -> abort). Paths, not full lines: a baseline-dirty file whose STATUS
    changes mid-run must not false-trip the delta -- content safety is the scoped
    commit (git add -- <applied>), not this check."""
    try:
        proc = processes.run_quiet(["git", "status", "--porcelain"], timeout=30, cwd=str(root),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return frozenset(line[3:] for line in (proc.stdout or "").splitlines() if len(line) > 3)


# --------------------------------------------------------------------------- #
# The real conductor-B choreography stages (injected in tests)
# --------------------------------------------------------------------------- #

def _extract_footprint(obj: Any) -> dict[str, Any] | None:
    """Pull a {modify,create,delete} footprint out of a plan-worker result
    (top-level or one level nested). None when absent."""
    if isinstance(obj, dict):
        fp = obj.get("footprint")
        if isinstance(fp, dict) and any(k in fp for k in ("modify", "create", "delete")):
            return {"modify": list(fp.get("modify") or []),
                    "create": list(fp.get("create") or []),
                    "delete": list(fp.get("delete") or [])}
        for v in obj.values():
            found = _extract_footprint(v)
            if found is not None:
                return found
    return None


def _extract_branches(obj: Any) -> list[dict[str, Any]] | None:
    """SPEC-144 v3: pull the plan-worker's optional `branches` decomposition
    ([{title, modify, create, delete}], each a slice of the footprint) out of
    its result, shape-validated. None when absent or malformed -- the caller
    falls back to the single-worker choreography, never crashes."""
    if isinstance(obj, dict):
        br = obj.get("branches")
        if isinstance(br, list) and br:
            out: list[dict[str, Any]] = []
            for b in br:
                if not isinstance(b, dict):
                    return None
                out.append({"title": str(b.get("title") or ""),
                            "modify": [str(p) for p in (b.get("modify") or [])],
                            "create": [str(p) for p in (b.get("create") or [])],
                            "delete": [str(p) for p in (b.get("delete") or [])]})
            return out
        for v in obj.values():
            found = _extract_branches(v)
            if found is not None:
                return found
    return None


def _branch_cap(root: Any) -> int:
    """Fan-out cap = feature-delivery's maxWorkers (the profile that executes the
    branches); a missing/broken registry degrades to 4 (the committed value)."""
    try:
        profiles = json.loads((Path(root) / ".harness" / "workflows" / "workflow-profiles.json")
                              .read_text(encoding="utf-8")).get("profiles", {})
        return int(profiles.get("feature-delivery", {}).get("maxWorkers", 4) or 4)
    except Exception:
        return 4


def branch_gate(root: Any, footprint: dict[str, Any],
                branches: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]] | None, str]:
    """Deterministic fan-out gate (S6-style: no model can widen it). Returns
    (branches, reason) when the decomposition is usable, else (None, reason) --
    the caller falls back to the single-worker choreography. Rules: 2..cap
    branches; no empty branch; every branch path within the union footprint;
    ZERO modify overlap between branches (write contention is a refusal)."""
    if not branches:
        return None, "no branch decomposition -> single-worker"
    cap = _branch_cap(root)
    if not 2 <= len(branches) <= cap:
        return None, f"{len(branches)} branches outside 2..{cap} -> single-worker"
    union = {p for k in ("modify", "create", "delete") for p in footprint.get(k) or []}
    seen_modify: set[str] = set()
    for i, b in enumerate(branches):
        paths = [*b.get("modify", []), *b.get("create", []), *b.get("delete", [])]
        if not paths:
            return None, f"branch {i + 1} is empty -> single-worker"
        outside = [p for p in paths if p not in union]
        if outside:
            return None, f"branch {i + 1} paths outside the union footprint {outside[:3]} -> single-worker"
        overlap = seen_modify.intersection(b.get("modify", []))
        if overlap:
            return None, f"modify overlap between branches {sorted(overlap)[:3]} -> single-worker"
        seen_modify.update(b.get("modify", []))
    return branches, f"fan-out {len(branches)} confined branches"


def _default_plan_stage(root: Any, brief_task: str, executor: str | None,
                        route: str | None = None) -> dict[str, Any]:
    """Stage P (read-only): a route-overseer plan-worker returns the footprint in
    its WORKER_RESULT (sec1.3 step 6). Read-only: no --write-allowed. For a
    `dynamic-workflow` route (SPEC-144 v3) it ALSO asks for an optional
    `branches` decomposition -- the overseer tier still owns the final
    inline-vs-workers call (invariant 4): no/invalid branches = single-worker."""
    import harness  # noqa: E402
    ask = (brief_task + " -- write the detailed implementation plan; RETURN in "
           "WORKER_RESULT a `plan` summary and a `footprint` "
           "{modify:[],create:[],delete:[]} of repo-relative paths")
    if route == "dynamic-workflow":
        ask += (". The router classified this demand dynamic-workflow: ALSO return "
                "`branches` [{title,modify:[],create:[],delete:[]}] -- 2..4 disjoint "
                "slices of that footprint (no modify path in two branches), one per "
                "parallel confined worker; omit `branches` if the work does not split")
    branches = [{
        "title": ask,
        "taskProfile": "route-overseer",
        "workerRole": "route-overseer-plan",
    }]
    wf = harness.plan_workflow(brief_task, "fork-join", branches=branches)
    wfid = str(wf["workflowId"])
    harness.workflow_run(wfid, executor)
    base, workflow = harness.load_workflow(wfid)
    worker = (workflow.get("workers") or [{}])[0]
    result = harness.read_json(base / str(worker.get("resultPath", "")), None)
    footprint = _extract_footprint(result)
    if footprint is None:
        raise ValueError("plan-worker returned no parseable footprint")
    out = {"wfid": wfid, **footprint}
    planned = _extract_branches(result)
    if planned is not None:
        out["branches"] = planned
    return out


def _default_implement_stage(root: Any, plan_digest: str, footprint: dict[str, Any],
                             approval_token: str, executor: str | None,
                             branches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Stage I (confined write): feature-delivery worker(s) scoped to the
    footprint, sparse workspace, write overlay (sec1.3 step 8). SPEC-144 v3:
    a branch-gate-approved decomposition fans out N confined workers in ONE
    feature-delivery workflow, each locked to its own slice (worker-00N:path);
    branches=None keeps the single-worker shape byte-identical."""
    import harness  # noqa: E402
    slices = branches or [{"title": plan_digest,
                           "modify": list(footprint.get("modify") or []),
                           "create": list(footprint.get("create") or []),
                           "delete": list(footprint.get("delete") or [])}]
    wf_branches = [{
        "title": (b.get("title") or plan_digest) if branches else plan_digest,
        "taskProfile": "route-overseer",
        "workerRole": "route-overseer-implement",
        "paths": list(b.get("modify") or []),
    } for b in slices]
    wf = harness.plan_workflow(plan_digest, "fork-join", branches=wf_branches,
                               profile_name="feature-delivery",
                               write_allowed=True, approval_token=approval_token)
    wfid = str(wf["workflowId"])
    harness.workflow_prepare_write(
        wfid, approval_token=approval_token,
        create_locks=[f"worker-{i + 1:03d}:{p}" for i, b in enumerate(slices)
                      for p in (b.get("create") or [])],
        delete_locks=[f"worker-{i + 1:03d}:{p}" for i, b in enumerate(slices)
                      for p in (b.get("delete") or [])])
    # SPEC-153: the branch-gate + approval_token already gated this deliberate write
    # fan-out (require_write_approval checked the secret before plan_workflow); that
    # approval IS the spawn-economy acknowledgement, so a multi-branch feature-delivery
    # (route-overseer -> fable/frontier) is not the accidental grunt fan-out the guard
    # blocks. Plan/analysis stages stay unacked -- the read lanes the guard is for.
    harness.workflow_run(wfid, executor, allow_frontier=bool(approval_token))
    reduce_result = harness.workflow_reduce(wfid)
    status = str((reduce_result or {}).get("status", "")).lower()
    review_required = status in ("blocked", "partial", "failed")
    return {"wfid": wfid, "reviewRequired": review_required, "reduce": reduce_result,
            "branches": len(slices) if branches else None}


def _default_merge_stage(root: Any, wfid: str, approval_token: str, *, _apply=None) -> dict[str, Any]:
    """Driver-as-supervisor merge (sec1.3 step 9). The four gate/rollback flags are
    HARD-CODED here -- not configurable, not omittable (plan sec2). `_apply` exists
    only so the self-check can assert those four flags without a live workflow."""
    import harness  # noqa: E402
    apply = _apply or harness.workflow_apply_merge
    return apply(wfid, approval_token=approval_token,
                 validation_gate="scenarios", final_gate="spec-pack",
                 rollback_on_validation_gate_failure=True,
                 rollback_on_final_gate_failure=True)


def _venv_python(root: Any) -> str:
    cand = Path(root) / ".venv" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else sys.executable


def _applied_paths(merge: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for w in merge.get("applied", []) or []:
        for key in ("filesChanged", "createdFiles", "deletedFiles"):
            out += [str(p) for p in (w.get(key) or [])]
        for ren in (w.get("renamedFiles") or []):
            if isinstance(ren, dict) and ren.get("newPath"):
                out.append(str(ren["newPath"]))
    return sorted(set(out))


def _default_commit_stage(root: Any, wfid: str, applied_paths: list[str],
                          entry: dict[str, Any], decision: dict[str, Any]) -> str:
    """Step 10: git add -> validate --staged (stamps) -> git commit, through the
    SPEC-137 hook. On a stamp-gate failure: rollback + unstage -> raise (no commit)."""
    import harness  # noqa: E402
    root = str(root)
    processes.run_quiet(["git", "add", "--", *applied_paths], timeout=60, cwd=root,
                        check=True, capture_output=True)
    v = processes.run_quiet([_venv_python(root), str(Path(root) / "scripts" / "harness.py"),
                             "validate", "--staged"], timeout=600, cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if v.returncode != 0:
        harness.workflow_rollback(wfid)
        processes.run_quiet(["git", "restore", "--staged", "."], timeout=60, cwd=root, capture_output=True)
        raise _CommitGateFailed((v.stdout or "") + (v.stderr or ""))
    # The Tie receipt is not optional for machine-composed commits either: the
    # commit-msg hook blocks a message without one, so omitting it here would have
    # killed this stage outright (caught by rt6 in the gate run of 2026-07-27, which
    # is exactly what that scenario committing for real is FOR). The honest locator
    # is the route entry that authorized the work -- it exists by construction, it is
    # the durable row a reader can follow, and machinery must never assert coverage
    from harness_lib import commit_authority
    msg = (f"route: {entry.get('id')} {_slug(entry.get('ask'))} (WF-{wfid})"
           f"\n\nTie: backlog:{entry.get('id')}")
    res = commit_authority.execute_commit(msg, root=Path(root))
    if res.get("status") in ("refused", "failed"):
        raise _CommitGateFailed(str(res.get("reasons") or res.get("output")))
    return str(res.get("commitSha") or "")



def _stage_failure_fields(exc: BaseException) -> dict[str, Any]:
    """The exception STR drops the child's captured output; the OBJECT carries it.
    Run 4 (2026-07-28) lost the nested `git commit` causal stderr exactly at the
    commit-stage handler — CalledProcessError and TimeoutExpired both hold the
    bytes that explain the rc."""
    fields: dict[str, Any] = {"commitExceptionType": type(exc).__name__}
    if getattr(exc, "returncode", None) is not None:
        fields["commitReturncode"] = exc.returncode  # type: ignore[attr-defined]
    for key in ("stdout", "stderr"):
        raw = getattr(exc, key, None)
        if raw:
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            fields[f"commit{key.capitalize()}"] = text[-2000:]
    return fields


def _default_analysis_stage(root: Any, demand: str, wp: str, executor: str | None) -> dict[str, Any]:
    """SPEC-144 v3 analysis lane: run the NAMED read-only workflow profile for a
    pre-defined-profile route (plan -> run -> reduce; findings, never writes).
    Planning errors (e.g. a changed-files split with no diff) bubble to the
    caller, which escalates with the reason -- the lane never guesses."""
    import harness  # noqa: E402
    profiles = json.loads((Path(root) / ".harness" / "workflows" / "workflow-profiles.json")
                          .read_text(encoding="utf-8")).get("profiles", {})
    ptype = str(profiles.get(wp, {}).get("type") or "fork-join")
    wf = harness.plan_workflow(demand, ptype, profile_name=wp)
    wfid = str(wf["workflowId"])
    harness.workflow_run(wfid, executor)
    reduce_result = harness.workflow_reduce(wfid)
    return {"wfid": wfid, "status": str((reduce_result or {}).get("status", "")).lower()}


def _analysis_profile(root: Any, decision: dict[str, Any]) -> str | None:
    """The workflow profile an analysis-lane dispatch should run, or None when
    the demand belongs to the write choreography: route must be
    pre-defined-profile, the named profile must EXIST and be writeAllowed:false
    (a write profile or an unknown name falls through -- today's behavior)."""
    if decision.get("route") != "pre-defined-profile":
        return None
    wp = decision.get("workflowProfile")
    if not wp:
        return None
    try:
        profiles = json.loads((Path(root) / ".harness" / "workflows" / "workflow-profiles.json")
                              .read_text(encoding="utf-8")).get("profiles", {})
    except Exception:
        return None
    cfg = profiles.get(str(wp))
    if isinstance(cfg, dict) and cfg.get("writeAllowed", False) is False:
        return str(wp)
    return None


def _default_cleanup(root: Any, wfid: str) -> None:
    import harness  # noqa: E402
    try:
        harness.workflow_cleanup_write(wfid, force=True)
    except Exception:
        pass  # keep the workspace on cleanup failure; not fatal to the demand


def _scrub_ephemeral_plan(plan_wfid: str | None) -> None:
    """Scrub the plan-stage workflow once its footprint is captured (write path only).

    The plan workflow is throwaway scaffolding -- its sole output (the footprint) is
    already read into memory, and nothing downstream reads the workflow again (the
    implement workflow is the audit record). Without this, every dispatched demand
    orphans a plan-stage dir in `.harness/workflows/active/`, inflating the active
    count `status` reports. Best-effort + force (the run is finished, so no live
    tree/lock); never fatal to the demand, matching _default_cleanup.

    ponytail: plan-worker usage is NOT folded into EXP-10 telemetry here -- the
    demand event already measures only the implement wfid (route_loop._demand_event).
    If a real-executor EXP-10 later needs per-stage usage, capture it before this scrub.
    """
    if not plan_wfid:
        return
    try:
        import harness  # noqa: E402
        harness.workflow_scrub(plan_wfid, force=True)
    except Exception:
        pass


def _default_usage(root: Any, wfid: str | None) -> dict[str, Any]:
    """Best-effort per-demand telemetry (EXP-10). observedUsage/toolCalls come from
    run_one_worker's persisted worker.run fields; absent -> None placeholders."""
    if not wfid:
        return {"observedUsage": None, "toolCalls": None}
    try:
        import harness  # noqa: E402
        _base, workflow = harness.load_workflow(wfid)
        usage: list[Any] = []
        tools = 0
        for w in workflow.get("workers", []) or []:
            run = w.get("run") or {}
            if run.get("observedUsage") is not None:
                usage.append(run.get("observedUsage"))
            tools += int((run.get("activity") or {}).get("toolCalls", 0) or 0)
        return {"observedUsage": usage or None, "toolCalls": tools or None}
    except Exception:
        return {"observedUsage": None, "toolCalls": None}


def _default_seams() -> dict[str, Callable]:
    return {"plan_stage": _default_plan_stage, "implement_stage": _default_implement_stage,
            "merge_stage": _default_merge_stage, "commit_stage": _default_commit_stage,
            "porcelain": _porcelain_set, "cleanup": _default_cleanup, "usage": _default_usage,
            "analysis_stage": _default_analysis_stage}


def _slug(text: Any, n: int = 48) -> str:
    words = "".join(c if c.isalnum() or c in " -_" else " " for c in str(text or "")).split()
    slug = "-".join(words).lower()
    return slug[:n] or "demand"


# --------------------------------------------------------------------------- #
# Conductor-B dispatch: steps 6-11 for ONE demand
# --------------------------------------------------------------------------- #

def conductor_b_dispatch(root: Any, entry: dict[str, Any], decision: dict[str, Any],
                         brief: str, floor: dict[str, Any], *, approval_token: str | None,
                         dry_run: bool, executor: str | None = None,
                         seams: dict[str, Callable] | None = None) -> dict[str, Any]:
    """One demand through sec1.3 steps 6-11. Returns an outcome dict:
    {outcome, entryId, wfid?, committedSha?, footprint?, reason}. outcome in
    {committed, skipped-here, dry-run, planned-read-only, escalated, rolled_back}."""
    seams = {**_default_seams(), **(seams or {})}
    eid = entry.get("id")
    brief_task = brief or route_dispatcher.compose_brief(entry.get("ask", ""), decision, floor)

    if dry_run:  # read-only, no model/workflow/git: prove the loop plumbing only
        return {"outcome": "dry-run", "entryId": eid, "reason": "dry-run: no plan/implement/merge/commit"}

    # 6. Stage P -- read-only plan-worker -> footprint (+ optional branches, v3).
    try:
        plan = seams["plan_stage"](root, brief_task, executor, route=decision.get("route"))
    except Exception as exc:
        return {"outcome": "escalated", "entryId": eid, "reason": f"plan stage failed: {exc}"}
    plan_wfid = plan.get("wfid")
    footprint = {"modify": plan.get("modify") or [], "create": plan.get("create") or [],
                 "delete": plan.get("delete") or []}

    # 7. Deterministic footprint gate (S6).
    ok, reason = footprint_gate(root, footprint)
    if not ok:
        return {"outcome": "escalated", "entryId": eid, "wfid": plan_wfid,
                "footprint": footprint, "reason": f"footprint gate: {reason}"}

    # 7b. Deterministic branch gate (SPEC-144 v3): a dynamic-workflow decomposition
    # fans out ONLY when it passes; anything else degrades to single-worker.
    branches, branch_reason = branch_gate(
        root, footprint,
        plan.get("branches") if decision.get("route") == "dynamic-workflow" else None)

    if not approval_token:  # no-token = read-only: plan produced, nothing written
        return {"outcome": "planned-read-only", "entryId": eid, "wfid": plan_wfid,
                "footprint": footprint, "reason": "no approval token -> read-only (plan only)"}

    # Write path: the footprint is captured, so the ephemeral plan workflow is dead
    # weight from here on (all downstream exits run after this). Scrub it now so a
    # long loop does not accumulate orphan plan-stage dirs in workflows/active.
    _scrub_ephemeral_plan(plan_wfid)

    # S2 (delta baseline): pre-existing runtime dirt is tolerated -- it is captured
    # here and never committed (the commit stage scopes `git add` to apply-merge's
    # returned paths). Abort only when git itself cannot report status.
    baseline = seams["porcelain"](root)
    if baseline is None:
        return {"outcome": "escalated", "entryId": eid, "footprint": footprint,
                "reason": "git status failed before Stage I -> abort (S2)"}

    # 8. Stage I -- confined write (fan-out when the branch gate approved one).
    plan_digest = f"{brief_task}\n\n[plan footprint] {json.dumps(footprint)}"
    try:
        impl = seams["implement_stage"](root, plan_digest, footprint, approval_token, executor,
                                        branches=branches)
    except Exception as exc:
        return {"outcome": "escalated", "entryId": eid, "footprint": footprint,
                "reason": f"implement stage failed: {exc}"}
    wfid = impl.get("wfid")
    if impl.get("reviewRequired"):
        return {"outcome": "escalated", "entryId": eid, "wfid": wfid, "footprint": footprint,
                "reason": "reviewPolicy tripped -> review-merge, not auto-merged"}

    # S2 again: NEW paths beyond the baseline = a worker escaped confinement.
    now = seams["porcelain"](root)
    new_dirt = None if now is None else sorted(now - baseline)
    if now is None or new_dirt:
        return {"outcome": "escalated", "entryId": eid, "wfid": wfid, "footprint": footprint,
                "newDirt": new_dirt,
                "reason": "new dirt beyond baseline before apply-merge -> abort (S2)"}

    # 9. Merge + gates (four flags HARD-CODED in the merge seam).
    try:
        merge = seams["merge_stage"](root, wfid, approval_token)
    except Exception as exc:
        return {"outcome": "escalated", "entryId": eid, "wfid": wfid, "footprint": footprint,
                "reason": f"apply-merge raised: {exc}"}
    status = str((merge or {}).get("status", ""))
    final_ok = bool(((merge or {}).get("finalValidation") or {}).get("passed"))
    if status != "applied" or not final_ok:
        # rolled_back / blocked / validation_blocked -> NO commit.
        after = seams["porcelain"](root)
        clean_after = after is not None and not (after - baseline)
        return {"outcome": "rolled_back" if status == "rolled_back" else "escalated",
                "entryId": eid, "wfid": wfid, "footprint": footprint,
                "cleanAfterRollback": clean_after,
                "reason": f"merge status {status!r} (finalValidation ok={final_ok}) -> no commit"}

    # 10. Commit (driver only, through SPEC-137).
    applied = _applied_paths(merge)
    try:
        sha = seams["commit_stage"](root, wfid, applied, entry, decision)
    except _CommitGateFailed as exc:
        return {"outcome": "rolled_back", "entryId": eid, "wfid": wfid, "footprint": footprint,
                "reason": f"validate --staged refused the commit -> rolled back: {exc}"}
    except Exception as exc:
        fields = _stage_failure_fields(exc)
        tail = (fields.get("commitStderr") or fields.get("commitStdout") or "")[-300:]
        return {"outcome": "escalated", "entryId": eid, "wfid": wfid, "footprint": footprint,
                **fields,
                "reason": f"commit stage failed: {exc}" + (f" | tail: {tail}" if tail else "")}

    # 11. Verdict + cleanup (success only).
    verdict = route_dispatcher.consume_verdict({"verdict": "kept", "committedSha": sha})
    seams["cleanup"](root, wfid)
    return {"outcome": "committed", "entryId": eid, "wfid": wfid, "footprint": footprint,
            "branches": len(branches) if branches else None,
            "committedSha": sha, "advance": verdict.get("advance"),
            "reason": f"committed via apply-merge + SPEC-137 stamp ({branch_reason})"}


# --------------------------------------------------------------------------- #
# The bounded loop (steps 1-5 + control) and the heartbeat
# --------------------------------------------------------------------------- #

def _stop_requested(root: Any) -> bool:
    return (Path(root) / STOP_REL).exists()


def _write_brief(root: Any, entry_id: str, brief: str) -> None:
    d = Path(root) / BRIEFS_REL
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{entry_id}.md").write_text(brief + "\n", encoding="utf-8", newline="\n")


def _emit(root: Any, event: str, payload: dict[str, Any]) -> None:
    try:
        import harness  # noqa: E402
        harness.append_event(event, payload)
    except Exception:
        pass  # telemetry is never load-bearing for the loop's control flow


def run_loop(root: Any, *, approval_token: str | None = None, executor: str | None = None,
             max_demands: int = 5, max_failures: int = 2, dry_run: bool = False,
             triage_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
             dispatch_fn: Callable[..., dict[str, Any]] | None = None,
             pull_fn: Callable[[Any], list[dict[str, Any]]] | None = None,
             seams: dict[str, Callable] | None = None,
             only: str | None = None) -> dict[str, Any]:
    """Drain the intake feed one demand at a time (sec1.3 steps 1-11), bounded by
    --max-demands / --max-failures / the stop sentinel / an empty feed.

    Injected seams keep it hermetic: `triage_fn` (default: live `_model_triage`, or
    the placeholder under dry_run), `dispatch_fn` (default: `conductor_b_dispatch`),
    `pull_fn` (default: `intake_queue.pending`). `dry_run` (or a missing token)
    makes the whole run read-only: no live model, no writes, no queue mutation.
    `only` (v3) restricts the pull to ONE entry id -- controlled/targeted runs
    against a long feed; the pull is oldest-first otherwise."""
    root = Path(root)
    from harness_lib import intake_queue
    from harness_lib.common import gate_holding
    # route_loop conductor-B has no business running under a gate-hold: it dispatches
    # workflows, commits, calls intake_queue.decide(), AND emits per-demand telemetry
    # (_emit -> append_event, unconditional) -- every one of those writes lands on the tree
    # the hold swapped to materialized HEAD and is discarded on release, while the loop would
    # report committed/analyzed/skipped and leave the entry pending, then re-pull it next run.
    # dry_run is NOT exempt: it still emits those events and reads a stale HEAD tree, so a
    # preview under a hold is meaningless. Refuse the whole run -- a visible refusal, mirroring
    # R1's decide() gate-hold guard, never a false success.
    if gate_holding(root):
        return {"demands": 0, "stopReason": "gate-hold", "dryRun": dry_run,
                "byOutcome": {}, "results": [], "held": True,
                "note": ("gate-hold: route --loop refused -- a gate holds .harness; its "
                         "dispatches/commits/decides/telemetry would land on the swapped HEAD "
                         "tree and be lost. Re-run after the gate settles.")}
    base_pull = pull_fn or intake_queue.pending
    pull = (base_pull if not only
            else (lambda r: [e for e in (base_pull(r) or []) if e.get("id") == only]))
    if triage_fn is None:
        triage_fn = (route_dispatcher._placeholder_triage if dry_run
                     else lambda d, f: _model_triage(root, d, f, executor))
    dispatch = dispatch_fn or conductor_b_dispatch

    lock = root / LOCK_REL
    results: list[dict[str, Any]] = []
    failures = 0
    stop_reason = "max-demands"
    seen: set[str] = set()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        lock.write_text(str(_pid()), encoding="utf-8")
    try:
        for _ in range(max(0, int(max_demands))):
            if _stop_requested(root):
                stop_reason = "stop-sentinel"
                break
            pend = [e for e in (pull(root) or []) if e.get("id") not in seen]
            if not pend:
                stop_reason = "empty-feed"
                break
            entry = pend[0]  # oldest actionable
            eid = str(entry.get("id"))
            seen.add(eid)
            demand = str(entry.get("ask") or "")
            floor = route_dispatcher.deterministic_floor(root, demand)
            try:
                verdict = triage_fn(demand, floor) or {}
            except Exception:
                verdict = {}

            # Skip class (D-a/M1): honored ONLY here; never when a risk flag is set.
            if verdict.get("skip") and not (floor.get("riskFlags") or []):
                if not dry_run:
                    intake_queue.decide(root, eid, "discard",
                                        note="route: no behavior change -> no artifact (model triage)")
                out = {"outcome": "skipped", "entryId": eid, "reason": "model triage skip -> discard"}
                results.append(out)
                _demand_event(root, eid, None, "inline", out, seams, floor)
                failures = 0
                continue

            decision = route_dispatcher.route_decision(demand, floor, triage_fn=lambda d, f: verdict)
            brief = route_dispatcher.compose_brief(demand, decision, floor)

            # Step 4: escalate -> record, leave pending, advance (never dispatch).
            if decision.get("escalate"):
                out = {"outcome": "escalated", "entryId": eid, "route": decision.get("route"),
                       "reason": decision.get("reason")}
                _emit(root, "route_escalation", {"entryId": eid, "route": decision.get("route"),
                                                 "riskFlags": floor.get("riskFlags", []),
                                                 "reason": decision.get("reason")})
                results.append(out)
                _demand_event(root, eid, None, decision.get("route"), out, seams, floor)
                continue

            if not dry_run:
                _write_brief(root, eid, brief)

            # SPEC-144 v3 analysis lane: a pre-defined-profile route naming a
            # read-only profile runs THAT workflow (findings) -- never the write
            # chain. Unknown/write profiles fall through to the choreography.
            wp = _analysis_profile(root, decision)
            if wp:
                if dry_run:
                    out = {"outcome": "dry-run", "entryId": eid, "workflowProfile": wp,
                           "reason": f"dry-run: analysis lane ({wp}) not executed"}
                else:
                    try:
                        ares = ({**_default_seams(), **(seams or {})})["analysis_stage"](
                            root, demand, wp, executor)
                    except Exception as exc:
                        ares = {"status": "", "error": str(exc)}
                    if ares.get("status") == "done":
                        out = {"outcome": "analyzed", "entryId": eid, "wfid": ares.get("wfid"),
                               "workflowProfile": wp,
                               "reason": f"analysis profile {wp} reduced done"}
                        intake_queue.decide(root, eid, "done",
                                            note=f"route analysis: {wp} -> {ares.get('wfid')}")
                    else:
                        out = {"outcome": "escalated", "entryId": eid, "wfid": ares.get("wfid"),
                               "workflowProfile": wp,
                               "reason": (f"analysis profile {wp} status "
                                          f"{(ares.get('status') or ares.get('error'))!r} -> owner review")}
                results.append(out)
                if out.get("outcome") == "escalated":
                    failures += 1
                else:
                    failures = 0
                _demand_event(root, eid, out.get("wfid"), decision.get("route"), out, seams, floor)
                if failures >= max(1, int(max_failures)):
                    stop_reason = "max-failures"
                    break
                continue

            out = dispatch(root, entry, decision, brief, floor, approval_token=approval_token,
                           dry_run=dry_run, executor=executor, seams=seams)
            results.append(out)

            outcome = out.get("outcome")
            if outcome == "committed" and not dry_run:
                intake_queue.decide(root, eid, "done",
                                    note=f"route: {out.get('committedSha')}")
                failures = 0
            elif outcome in ("rolled_back", "escalated"):
                failures += 1
            else:  # dry-run / planned-read-only / skipped-here: progress, not a failure
                failures = 0
            _demand_event(root, eid, out.get("wfid"), decision.get("route"), out, seams, floor)

            if failures >= max(1, int(max_failures)):
                stop_reason = "max-failures"
                break
    finally:
        if not dry_run:
            lock.unlink(missing_ok=True)

    tally: dict[str, int] = {}
    for r in results:
        tally[r.get("outcome", "unknown")] = tally.get(r.get("outcome", "unknown"), 0) + 1
    return {"demands": len(results), "stopReason": stop_reason, "dryRun": dry_run,
            "byOutcome": tally, "results": results}


def _demand_event(root: Any, eid: str, wfid: str | None, route: str | None,
                  out: dict[str, Any], seams: dict[str, Callable] | None,
                  floor: dict[str, Any] | None = None) -> None:
    """EXP-10 per-demand measurement hook (secD5)."""
    usage = {"observedUsage": None, "toolCalls": None}
    try:
        usage = ({**_default_seams(), **(seams or {})})["usage"](root, wfid)
    except Exception:
        pass
    _emit(root, "route_demand_done", {"entryId": eid, "wfid": wfid, "route": route,
                                      "outcome": out.get("outcome"),
                                      "committedSha": out.get("committedSha"),
                                      "observedUsage": usage.get("observedUsage"),
                                      "toolCalls": usage.get("toolCalls")})
    # SPEC-144 v8 durable ledger: the done twin of the transient event. Only entryId
    # is in scope here (the raw demand text -- from which demand_id derives -- is not
    # threaded into this EXP-10 hook), so the done row is keyed by entryId; the
    # triage/dispatched rows carry demandId. record() never raises (swallows).
    # PLAN E-routescores: the done row also carries the classifier `scores` and the R4
    # `predictedP` = score(chosenProfile)/sum(scores) (null on sum=0, never a zero-divide).
    # The loop's done row is entryId-keyed and not join-able to the demandId triage rows,
    # so persisting predictedP HERE keeps prediction + realized outcome in ONE row for the
    # ECE probe. Additive only; a bad/absent floor degrades to empty scores + null predictedP.
    try:
        from harness_lib import route_ledger  # noqa: E402
        scores = (floor or {}).get("scores") or {}
        route_ledger.record(root, "done", {"entryId": eid, "wfid": wfid, "route": route,
                                           "outcome": out.get("outcome"),
                                           "committedSha": out.get("committedSha"),
                                           "scores": scores,
                                           "predictedP": route_ledger.normalized_predicted_p(
                                               scores, (floor or {}).get("profile"))})
    except Exception:
        pass


def _pid() -> int:
    import os
    return os.getpid()


def heartbeat(root: Any, *, pull_fn: Callable[[Any], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Re-entry check (M3). NEVER spawns -- returns a resume verdict only. Resume
    ONLY when the feed has actionable pending entries AND no stop sentinel AND no
    live loop lock. The caller (session agent) does the single re-invoke."""
    root = Path(root)
    from harness_lib import intake_queue
    pull = pull_fn or intake_queue.pending
    pending_actionable = len(pull(root) or [])
    stop_requested = _stop_requested(root)
    # ponytail: lock-file presence == running. A crashed loop leaves a stale lock
    # that blocks resume until manually removed; upgrade to a pid-liveness probe if
    # crash-frequency ever warrants it. Normal exits clear it in run_loop's finally.
    loop_running = (root / LOCK_REL).exists()
    should_resume = pending_actionable > 0 and not stop_requested and not loop_running
    return {"shouldResume": should_resume, "pendingActionable": pending_actionable,
            "stopRequested": stop_requested, "loopRunning": loop_running}


# --------------------------------------------------------------------------- #
# Hermetic self-check (injected seams -- no model, no git, no real workflow)
# --------------------------------------------------------------------------- #

def demo() -> None:
    import tempfile

    # footprint gate: protected / forbidden / oversize all refuse; a clean small one passes.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ok, _ = footprint_gate(root, {"modify": ["docs/x.md"]})
        assert ok, "clean small footprint should pass"
        bad, reason = footprint_gate(root, {"modify": [".harness/state/queue.json"]})
        assert not bad and "forbidden" in reason, reason
        bad, reason = footprint_gate(root, {"create": [f"f{i}.txt" for i in range(FOOTPRINT_CAP + 1)]})
        assert not bad and "cap" in reason, reason
        assert not footprint_gate(root, {})[0], "empty footprint escalates"

    # _extract_verdict handles raw JSON and stream-json-wrapped output.
    assert _extract_verdict('noise {"route":"inline","skip":true} tail')["skip"] is True
    assert _extract_verdict('{"type":"result","text":"{\\"route\\":\\"inline\\"}"}')["route"] == "inline"
    assert _extract_verdict("no json here") == {}

    # _extract_footprint finds a nested footprint.
    assert _extract_footprint({"a": {"footprint": {"modify": ["x"]}}}) == {"modify": ["x"], "create": [], "delete": []}
    assert _extract_footprint({"nope": 1}) is None

    # merge seam HARD-CODES the four gate/rollback flags.
    captured: dict[str, Any] = {}
    def fake_apply(wfid, **kw):
        captured.update(kw); return {"status": "applied", "finalValidation": {"passed": True}, "applied": []}
    _default_merge_stage("r", "WF-x", "TOK", _apply=fake_apply)
    assert captured["validation_gate"] == "scenarios" and captured["final_gate"] == "spec-pack"
    assert captured["rollback_on_validation_gate_failure"] is True
    assert captured["rollback_on_final_gate_failure"] is True, captured

    # Injected loop seams: a full run with stubs, no model / git / workflow.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        feed = [{"id": "d1", "ask": "add a docs note"}, {"id": "d2", "ask": "chatter"}]
        pull = lambda r: [e for e in feed]
        # dispatch stub reports committed for d1; loop should decide 'done' via intake.
        committed = {"outcome": "committed", "entryId": "d1", "wfid": "WF-1", "committedSha": "abc1234"}
        seen_dispatch: list[str] = []
        def disp(r, entry, decision, brief, floor, **kw):
            seen_dispatch.append(entry["id"]); return {**committed, "entryId": entry["id"]}
        benign = lambda d, f: {"route": "inline", "delegate": False, "escalate": False, "reason": "ok"}
        # d1 commits (feed drains it), then d2 commits, then empty -> stop.
        from harness_lib import intake_queue
        intake_queue.add(root, "add a docs note"); intake_queue.add(root, "chatter here now")
        real_pending = intake_queue.pending(root)
        out = run_loop(root, approval_token="TOK", max_demands=5, triage_fn=benign,
                       dispatch_fn=lambda r, e, d, b, f, **kw: {"outcome": "committed", "entryId": e["id"],
                                                                "wfid": "WF", "committedSha": "s"},
                       )
        assert out["byOutcome"].get("committed") == len(real_pending), out
        assert out["stopReason"] == "empty-feed", out
        assert intake_queue.pending(root) == [], "committed demands become done"

    # Skip class: skip:true + no risk flag -> discard, dispatch never called.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        from harness_lib import intake_queue
        intake_queue.add(root, "please just status update no change")
        skip = lambda d, f: {"route": "inline", "skip": True, "escalate": False, "reason": "no change"}
        called = []
        out = run_loop(root, approval_token="TOK", triage_fn=skip,
                       dispatch_fn=lambda *a, **k: called.append(1) or {"outcome": "committed"})
        assert out["byOutcome"].get("skipped") == 1 and not called, out
        assert intake_queue.pending(root) == [], "skipped -> discarded"

    # Stop sentinel halts before demand 2.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / STOP_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / STOP_REL).write_text("stop", encoding="utf-8")
        from harness_lib import intake_queue
        intake_queue.add(root, "some demand here now please")
        out = run_loop(root, approval_token="TOK",
                       triage_fn=lambda d, f: {"route": "inline", "escalate": False, "reason": "x"},
                       dispatch_fn=lambda *a, **k: {"outcome": "committed"})
        assert out["demands"] == 0 and out["stopReason"] == "stop-sentinel", out

    # max-demands bound honored.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # pull_fn always returns 5 distinct never-decided entries; bound = 3.
        pool = [{"id": f"x{i}", "ask": f"demand {i} here"} for i in range(5)]
        out = run_loop(root, approval_token="TOK", max_demands=3,
                       pull_fn=lambda r: pool,
                       triage_fn=lambda d, f: {"route": "inline", "escalate": False, "reason": "x"},
                       dispatch_fn=lambda r, e, d, b, f, **kw: {"outcome": "planned-read-only", "entryId": e["id"]})
        assert out["demands"] == 3 and out["stopReason"] == "max-demands", out

    # Gate-failure rollback: merge seam returns rolled_back -> NO commit, escalated.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        commit_calls = []
        seams = {"plan_stage": lambda r, t, ex, **kw: {"wfid": "WF-9", "modify": ["docs/x.md"], "create": [], "delete": []},
                 "porcelain": lambda r: frozenset(),
                 "implement_stage": lambda r, pd, fp, tok, ex, **kw: {"wfid": "WF-9", "reviewRequired": False},
                 "merge_stage": lambda r, wfid, tok: {"status": "rolled_back", "applied": [],
                                                      "finalValidation": None, "rollbackResult": {}},
                 "commit_stage": lambda *a, **k: commit_calls.append(1) or "SHA",
                 "cleanup": lambda r, wfid: None,
                 "usage": lambda r, wfid: {"observedUsage": None, "toolCalls": None}}
        entry = {"id": "g1", "ask": "touch docs"}
        floor = {"riskFlags": []}
        decision = {"route": "inline", "escalate": False, "reason": "ok"}
        out = conductor_b_dispatch(root, entry, decision, "brief", floor,
                                   approval_token="TOK", dry_run=False, seams=seams)
        assert out["outcome"] == "rolled_back" and not commit_calls, out

    # S2 delta: pre-existing dirt tolerated; NEW dirt or git failure aborts.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        happy = {"plan_stage": lambda r, t, ex, **kw: {"wfid": "WF-8", "modify": ["docs/x.md"], "create": [], "delete": []},
                 "implement_stage": lambda r, pd, fp, tok, ex, **kw: {"wfid": "WF-8", "reviewRequired": False},
                 "merge_stage": lambda r, wfid, tok: {"status": "applied", "finalValidation": {"passed": True},
                                                      "applied": [{"filesChanged": ["docs/x.md"]}]},
                 "commit_stage": lambda *a, **k: "SHA8", "cleanup": lambda r, wfid: None,
                 "usage": lambda r, wfid: {"observedUsage": None, "toolCalls": None}}
        entry = {"id": "s2", "ask": "touch docs"}
        dec = {"route": "inline", "escalate": False, "reason": "ok"}
        # constant pre-existing dirt -> tolerated, commits.
        out = conductor_b_dispatch(root, entry, dec, "b", {"riskFlags": []}, approval_token="TOK",
                                   dry_run=False, seams={**happy, "porcelain": lambda r: frozenset({".harness/state/worklog.json"})})
        assert out["outcome"] == "committed", out
        # new dirt appears after Stage I -> S2 abort, no merge/commit.
        calls = iter([frozenset(), frozenset({"evil.txt"})])
        out = conductor_b_dispatch(root, entry, dec, "b", {"riskFlags": []}, approval_token="TOK",
                                   dry_run=False, seams={**happy, "porcelain": lambda r: next(calls)})
        assert out["outcome"] == "escalated" and out.get("newDirt") == ["evil.txt"], out
        # git failure -> abort at the baseline.
        out = conductor_b_dispatch(root, entry, dec, "b", {"riskFlags": []}, approval_token="TOK",
                                   dry_run=False, seams={**happy, "porcelain": lambda r: None})
        assert out["outcome"] == "escalated" and "S2" in out["reason"], out

    # dry-run dispatch: no plan/implement/merge/commit, no queue mutation.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        touched = []
        seams = {k: (lambda *a, **kw: touched.append(k)) for k in _default_seams()}
        out = conductor_b_dispatch(root, {"id": "z", "ask": "x"}, {"route": "inline"}, "b",
                                   {}, approval_token="TOK", dry_run=True, seams=seams)
        assert out["outcome"] == "dry-run" and not touched, out

    # heartbeat NEVER resumes past a stop condition; resumes on an actionable feed.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hb = heartbeat(root, pull_fn=lambda r: [])
        assert hb["shouldResume"] is False and hb["pendingActionable"] == 0, hb
        hb = heartbeat(root, pull_fn=lambda r: [{"id": "a", "ask": "x"}])
        assert hb["shouldResume"] is True, hb
        (root / STOP_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / STOP_REL).write_text("s", encoding="utf-8")
        hb = heartbeat(root, pull_fn=lambda r: [{"id": "a", "ask": "x"}])
        assert hb["shouldResume"] is False and hb["stopRequested"] is True, hb
        (root / STOP_REL).unlink()
        (root / LOCK_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / LOCK_REL).write_text("123", encoding="utf-8")
        hb = heartbeat(root, pull_fn=lambda r: [{"id": "a", "ask": "x"}])
        assert hb["shouldResume"] is False and hb["loopRunning"] is True, hb

    # ---- SPEC-144 v3: branch gate, fan-out dispatch, analysis lane, --only ----
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fp = {"modify": ["a.md", "b.md"], "create": [], "delete": []}
        ok_br = [{"title": "a", "modify": ["a.md"], "create": [], "delete": []},
                 {"title": "b", "modify": ["b.md"], "create": [], "delete": []}]
        got, why = branch_gate(root, fp, ok_br)
        assert got == ok_br and "fan-out 2" in why, why
        assert branch_gate(root, fp, None)[0] is None
        assert branch_gate(root, fp, ok_br[:1])[0] is None, "1 branch -> single-worker"
        assert branch_gate(root, fp, ok_br * 3)[0] is None, "6 branches > cap 4"
        assert branch_gate(root, fp, [{"title": "a", "modify": ["a.md"]},
                                      {"title": "z", "modify": ["z.md"]}])[0] is None, "outside union"
        assert branch_gate(root, fp, [{"title": "a", "modify": ["a.md"]},
                                      {"title": "b", "modify": ["a.md", "b.md"]}])[0] is None, "modify overlap"
        assert branch_gate(root, fp, [{"title": "a", "modify": ["a.md"]}, {"title": "e"}])[0] is None, "empty branch"
        assert _extract_branches({"x": {"branches": [{"title": "t", "modify": ["p"]}]}})[0]["modify"] == ["p"]
        assert _extract_branches({"branches": ["bad"]}) is None

        # fan-out dispatch: a dynamic-workflow plan with branches reaches implement;
        # an inline route NEVER fans out even when the plan result carries branches.
        cap_impl: dict[str, Any] = {}
        seams = {"plan_stage": lambda r, t, ex, **kw: {"wfid": "WF-D", "modify": ["a.md", "b.md"],
                                                       "create": [], "delete": [], "branches": ok_br},
                 "porcelain": lambda r: frozenset(),
                 "implement_stage": lambda r, pd, fp2, tok, ex, **kw:
                     cap_impl.update(kw) or {"wfid": "WF-D", "reviewRequired": False},
                 "merge_stage": lambda r, wfid, tok: {"status": "applied", "finalValidation": {"passed": True},
                                                      "applied": [{"filesChanged": ["a.md", "b.md"]}]},
                 "commit_stage": lambda *a, **k: "SHAD", "cleanup": lambda r, wfid: None,
                 "usage": lambda r, wfid: {"observedUsage": None, "toolCalls": None}}
        outd = conductor_b_dispatch(root, {"id": "d9", "ask": "cross-cutting docs"},
                                    {"route": "dynamic-workflow", "escalate": False}, "b",
                                    {"riskFlags": []}, approval_token="TOK", dry_run=False, seams=seams)
        assert outd["outcome"] == "committed" and outd["branches"] == 2 and cap_impl.get("branches") == ok_br, outd
        cap_impl.clear()
        outi = conductor_b_dispatch(root, {"id": "d10", "ask": "small fix"},
                                    {"route": "inline", "escalate": False}, "b",
                                    {"riskFlags": []}, approval_token="TOK", dry_run=False, seams=seams)
        assert outi["outcome"] == "committed" and outi.get("branches") is None and cap_impl.get("branches") is None, outi

    # analysis lane: a pre-defined-profile route naming a read-only profile runs the
    # analysis seam and NEVER the write chain; blocked reduce escalates + stays pending.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        from harness_lib import intake_queue
        wdir = root / ".harness" / "workflows"
        wdir.mkdir(parents=True)
        (wdir / "workflow-profiles.json").write_text(json.dumps({"profiles": {
            "diff-review": {"type": "map-reduce", "writeAllowed": False},
            "feature-delivery": {"type": "fork-join", "writeAllowed": True, "maxWorkers": 4}}}),
            encoding="utf-8")
        intake_queue.add(root, "review the latest diff for regressions")
        never_write = lambda *a, **k: (_ for _ in ()).throw(AssertionError("write chain must not run"))
        ran: list[str] = []
        out = run_loop(root, approval_token="TOK",
                       triage_fn=lambda d, f: {"route": "pre-defined-profile", "escalate": False,
                                               "workflowProfile": "diff-review", "reason": "x"},
                       dispatch_fn=never_write,
                       seams={"analysis_stage": lambda r, d, wp, ex: ran.append(wp) or {"wfid": "WF-A", "status": "done"}})
        assert out["byOutcome"].get("analyzed") == 1 and ran == ["diff-review"], out
        assert intake_queue.pending(root) == [], "analyzed done -> queue advanced"
        intake_queue.add(root, "another diff review please now")
        out = run_loop(root, approval_token="TOK",
                       triage_fn=lambda d, f: {"route": "pre-defined-profile", "escalate": False,
                                               "workflowProfile": "diff-review", "reason": "x"},
                       dispatch_fn=never_write,
                       seams={"analysis_stage": lambda r, d, wp, ex: {"wfid": "WF-B", "status": "blocked"}})
        assert out["byOutcome"].get("escalated") == 1 and len(intake_queue.pending(root)) == 1, out
        # a WRITE profile named in the verdict falls through to the write choreography
        wp_route = lambda d, f: {"route": "pre-defined-profile", "escalate": False,
                                 "workflowProfile": "feature-delivery", "reason": "x"}
        hit: list[str] = []
        run_loop(root, approval_token="TOK", triage_fn=wp_route,
                 dispatch_fn=lambda r, e, *a, **k: hit.append(e["id"]) or {"outcome": "planned-read-only", "entryId": e["id"]})
        assert hit, "write profile -> choreography, not the analysis lane"

    # --only: the pull is restricted to ONE entry id.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pool = [{"id": "x1", "ask": "demand one here"}, {"id": "x2", "ask": "demand two here"}]
        seen_only: list[str] = []
        out = run_loop(root, approval_token="TOK", pull_fn=lambda r: pool, only="x2",
                       triage_fn=lambda d, f: {"route": "inline", "escalate": False, "reason": "x"},
                       dispatch_fn=lambda r, e, *a, **k: seen_only.append(e["id"]) or {"outcome": "planned-read-only", "entryId": e["id"]})
        assert seen_only == ["x2"] and out["demands"] == 1, (seen_only, out)

    print("route_loop self-check OK")


if __name__ == "__main__":
    demo()
