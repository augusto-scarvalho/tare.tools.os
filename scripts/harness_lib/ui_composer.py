"""SPEC-120 flow composer backend (derived DAG + validate-only compile).

MF split out of ui_panel.py (self-lib-file-budget finding, 2026-07-13):
the N2 composer domain — derived profile DAG views and the
in-process compile that gates composer-create (the trust boundary).
ui_panel re-exports every public name, so callers and tests are unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from .common import now, read_json
except ImportError:  # run directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import now, read_json

# -- N2 flow composer (K1): derived DAG read view + in-process validate-only compile --

def _import_harness() -> Any:
    """Lazy import of the CLI module (heavy) so the panel only loads it when the
    Compose view is opened / a compile runs. scripts/ is on sys.path in every real
    context (harness_ui inserts it, the scenarios insert it); the fallback covers a
    direct `python ui_panel.py` run (mirrors this module's own import block)."""
    try:
        import harness  # type: ignore
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import harness  # type: ignore
    return harness


def _dag_for(view: dict[str, Any]) -> dict[str, Any]:
    """Derive nodes+edges for one profile view — pure, recomputed at read, NO layout
    state stored (a fully derived view cannot drift). source → one node per branch →
    reduce; the source→branch edges carry the seed flag when the profile shares a
    plan-time context digest (F1). map-reduce has no enumerable branches (shards are
    computed at compile), so it renders a single derived `map` node."""
    seeded = bool(view.get("seeded"))
    nodes: list[dict[str, Any]] = [{"id": "source", "kind": "source",
                                    "label": str(view.get("type") or "workflow")}]
    edges: list[dict[str, Any]] = []
    branches = view.get("branches") or []
    if branches:
        for i, b in enumerate(branches):
            nid = f"b{i}"
            label = " · ".join(x for x in (b.get("workerRole"), b.get("taskProfile")) if x)
            nodes.append({"id": nid, "kind": "branch", "label": label or (b.get("title") or nid),
                          "taskProfile": b.get("taskProfile"), "workerRole": b.get("workerRole"),
                          "title": b.get("title")})
            edges.append({"from": "source", "to": nid, "kind": "seed" if seeded else "fanout"})
            edges.append({"from": nid, "to": "reduce", "kind": "reduce"})
    else:
        nodes.append({"id": "map", "kind": "branch",
                      "label": f"map · {view.get('splitStrategy') or 'roots'} · ≤{view.get('maxWorkers')}"})
        edges.append({"from": "source", "to": "map", "kind": "seed" if seeded else "fanout"})
        edges.append({"from": "map", "to": "reduce", "kind": "reduce"})
    nodes.append({"id": "reduce", "kind": "reduce", "label": "reduce"})
    return {"nodes": nodes, "edges": edges, "seeded": seeded}


def _normalize_profile(harness: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """One profile → the composer's normalized DAG spec. Branch objects are resolved
    with the compiler's OWN `default_fork_branches` (string→keyword profile, object
    passthrough) so the rendered view matches what would compile — no second resolver."""
    ptype = str(cfg.get("type") or "")
    branches: list[dict[str, Any]] = []
    if ptype == "fork-join":
        raw = cfg.get("branches") if isinstance(cfg.get("branches"), list) else None
        try:
            resolved = harness.default_fork_branches(int(cfg.get("maxWorkers") or 5), raw)
        except Exception:
            resolved = []
        branches = [{"title": b.get("title"), "taskProfile": b.get("taskProfile"),
                     "workerRole": b.get("workerRole")} for b in resolved]
    view = {
        "type": ptype,
        "maxWorkers": cfg.get("maxWorkers"),
        "splitStrategy": cfg.get("splitStrategy"),
        "branches": branches,
        "awaitPolicy": cfg.get("awaitPolicy") or {},
        "joinPolicy": cfg.get("joinPolicy") or {},
        "tokenBudget": cfg.get("tokenBudget") or {},
        "seeded": bool(cfg.get("sharedContextDigest")),
    }
    view["dag"] = _dag_for(view)
    return view


def composer_snapshot(root: Path) -> dict[str, Any]:
    """N2 read collector: the CLOSED vocabularies for the composer forms + a
    normalized DAG view of every workflow profile. Pure derivation, writes NOTHING;
    degrades to {"error": …} like every collector. Layout is never persisted — the
    front-end recomputes the DAG from form state on every edit (the no-drift proof)."""
    root = Path(root)
    try:
        harness = _import_harness()
        profiles_cfg = harness.load_workflow_profiles().get("profiles", {})
        profiles = {name: _normalize_profile(harness, cfg)
                    for name, cfg in profiles_cfg.items() if isinstance(cfg, dict)}
        task_profiles = sorted(harness.load_profiles().get("profiles", {}))
        execs = read_json(root / ".harness" / "routing" / "executors.json", {}) or {}
        executors = sorted(execs.get("executors") or {})
        return {"profiles": profiles, "executors": executors,
                "taskProfiles": task_profiles, "generatedAt": now()}
    except Exception as exc:  # one bad source must never sink the view
        return {"error": f"{type(exc).__name__}: {exc}"}


def _compile_reject(profile: str, error: str) -> dict[str, Any]:
    return {"valid": False, "validateOnly": True, "profile": profile, "workers": [],
            "tokenAudit": None, "errors": [error], "warnings": []}


def compile_candidate(root: Path, profile: Any, task: Any, branches: Any) -> dict[str, Any]:
    """N2 /api/compile core — the K1 trust boundary. Validate the browser-composed
    candidate against the CLOSED vocabularies (known profiles, known taskProfiles)
    BEFORE compiling, then run the N1 zero-materialization compile
    (`validate_workflow_plan`) IN-PROCESS. Read-shaped like /api/records: it writes
    nothing and constructs NO subcommand argv, so browser input can never escape into
    one. A profile outside the closed set is rejected before any compile runs."""
    profile = str(profile or "")
    task = str(task or "")
    try:
        harness = _import_harness()
        known_profiles = set(harness.load_workflow_profiles().get("profiles", {}))
        if profile not in known_profiles:
            return _compile_reject(profile,
                f"unknown profile {profile!r} — not in the closed profile set "
                f"({', '.join(sorted(known_profiles))})")
        known_tp = set(harness.load_profiles().get("profiles", {}))
        norm: list[dict[str, Any]] | None = None
        if isinstance(branches, list) and branches:
            norm = []
            for b in branches:
                if not isinstance(b, dict):
                    continue
                tp = b.get("taskProfile")
                if tp not in (None, "") and str(tp) not in known_tp:
                    return _compile_reject(profile,
                        f"unknown taskProfile {tp!r} — not in the closed task-profile set "
                        f"({', '.join(sorted(known_tp))})")
                entry: dict[str, Any] = {"title": str(b.get("title") or "").strip()}
                if tp:
                    entry["taskProfile"] = str(tp)
                if b.get("workerRole"):
                    entry["workerRole"] = str(b.get("workerRole"))
                norm.append(entry)
        # In-process N1 call: validate_workflow_plan itself re-enforces the closed
        # vocabularies + secret-scans `task`/branch content and writes no WF dir.
        return harness.validate_workflow_plan(task, None, branches=norm, profile_name=profile)
    except Exception as exc:
        return _compile_reject(profile, f"{type(exc).__name__}: {exc}")


