#!/usr/bin/env python3
"""SPEC-159 Phase 1 (SHADOW) — affected-scenario selection + per-scenario cache key.

MEASURE-ONLY. Nothing here ever skips a scenario. It computes, from the Graphify
code-AST import graph (`graphify-out/graph.json`), which scenarios a set of
changed paths WOULD let a future scenario-level cache skip, and a stable
per-scenario cache key over that scenario's import closure. `spec_test_gate` uses
it in shadow to decorate the gate-perf rows and PROVE (falseSkip==0) that a
scenario-level skip would be safe before Phase 2 ever turns skipping on.

Correctness-first: EVERY uncertainty resolves to AFFECTED (would-run). The
conservative-fallback branches are enumerated in `affected_scenarios` (FB-0..FB-4).

Graph shape used (verified against graphify-out/graph.json, schemaVersion
`graphify-code-ast/v1`): a dict with `nodes` (list of `{"path","id",...}`, id==path,
repo-relative POSIX) and `edges` (list of `{"source","target","type"}` where
`type=="imports"` and source/target are node paths). Only `imports` edges are used.

Pure stdlib; read-only over the graph + git index blobs (passed in by the caller).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib.common import ROOT, now

GRAPH_PATH = ROOT / "graphify-out" / "graph.json"
SCEN_REL = "testing/scenarios"

# Entry points that virtually every CLI-driving scenario reaches through a
# SUBPROCESS (`harness.py ...`), NOT an import edge — so the import graph cannot
# see the dependency. A change to any of these forces GLOBAL affected: the
# conservative floor under the import-graph's subprocess blind spot (see SPEC-159
# ceilings; Phase 2 must model subprocess deps before it can narrow past this).
_GLOBAL_TRIGGERS = {
    "scripts/harness.py",
    "scripts/harness-test.py",
    "scripts/spec_test_gate.py",
    "scripts/harness_lib/cli_registry.py",
    "scripts/harness_lib/cli_catalog.py",
    "scripts/harness_lib/common.py",  # imported by nearly everything; also an import edge
    "testing/scenarios/_lib.py",       # every scenario imports it (belt + braces)
}

# wf-subprocess-edges (SPEC-159 v3, SHADOW): the SHRUNK floor the affectedV2 map
# uses once subprocess/source-read edges model the blind spot. Everything dropped
# from _GLOBAL_TRIGGERS below is now reachable through a static edge and traced by
# the merged closure instead of a global full-run: harness.py via the run()/scrub()
# wrapper edge; cli_registry/cli_catalog/common via harness.py's own import closure;
# _lib.py via the plain import edge every scenario already has. ONLY the two gate
# runners stay global — nothing edges TO them (scenarios do not invoke the gate).
_GLOBAL_TRIGGERS_V2 = {
    "scripts/spec_test_gate.py",
    "scripts/harness-test.py",
}


def _norm(p: Any) -> str:
    return str(p).replace("\\", "/")


# --- graph load + closure ----------------------------------------------------

def load_graph(path: Path = GRAPH_PATH) -> dict | None:
    """Parsed graph, or None when missing / unreadable / wrong shape (→ ALL affected).
    Fail-open: a stale-but-valid graph still loads (staleness surfaces as changed
    paths absent from `nodes`, which the FB-3 attributability check catches)."""
    try:
        g = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(g, dict) or not isinstance(g.get("edges"), list) \
            or not isinstance(g.get("nodes"), list):
        return None
    return g


def _import_adjacency(graph: dict) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in graph["edges"]:
        if not isinstance(e, dict) or e.get("type") != "imports":
            continue
        src, tgt = _norm(e.get("source")), _norm(e.get("target"))
        if src and tgt:
            adj.setdefault(src, set()).add(tgt)
    return adj


def _node_paths(graph: dict) -> set[str]:
    out: set[str] = set()
    for n in graph["nodes"]:
        if isinstance(n, dict):
            p = n.get("path") or n.get("id")
            if p:
                out.add(_norm(p))
    return out


def _closure(start: str, adj: dict[str, set[str]]) -> set[str]:
    """Transitive import targets reachable from `start` (excludes `start` itself)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        for tgt in adj.get(stack.pop(), ()):  # noqa: SIM113
            if tgt not in seen:
                seen.add(tgt)
                stack.append(tgt)
    return seen


def scenario_ids(root: Path) -> list[str]:
    """Authoritative scenario list from the filesystem (not the graph): a scenario
    missing from a stale graph is still enumerated → still classified → affected."""
    d = root / SCEN_REL
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.py") if not p.name.startswith("_"))


def scenario_closures(root: Path, graph: dict | None,
                      sub_edges: dict[str, dict] | None = None) -> dict[str, set[str] | None]:
    """sid → set of closure paths (INCLUDING the scenario file), or None when the
    closure is unknown (no graph, or scenario absent from the graph) → affected.

    wf-subprocess-edges (SPEC-159 v3): when `sub_edges` (the gate_subprocess_edges
    extract) is supplied, the adjacency is the ADDITIVE UNION imports ∪ subprocess
    edges before the closure walk, so a scenario's run()/read_text targets ride into
    its closure. An `unresolved` scenario — or ANY transitively-reached module marked
    unresolved (opaque subprocess program: unknown blast radius) — collapses to None
    → FB-4 → always-affected, never a skip. `sub_edges=None` is byte-identical to the
    imports-only closure (the today's-map path)."""
    sids = scenario_ids(root)
    if graph is None:
        return {s: None for s in sids}
    adj = _import_adjacency(graph)
    unresolved: set[str] = set()
    if sub_edges:
        for src, info in sub_edges.items():
            for tgt in info.get("edges") or ():
                adj.setdefault(_norm(src), set()).add(_norm(tgt))
            if info.get("unresolved"):
                unresolved.add(_norm(src))
    nodes = _node_paths(graph)
    out: dict[str, set[str] | None] = {}
    for sid in sids:
        node = f"{SCEN_REL}/{sid}.py"
        if node not in nodes or node in unresolved:
            out[sid] = None  # unknown closure → affected
            continue
        cl = _closure(node, adj)
        cl.add(node)  # the scenario file's own blob is part of its identity
        out[sid] = None if (cl & unresolved) else cl  # a reached opaque module → unknown radius
    return out


# --- affected-set (the TIA decision, conservative on every doubt) ------------

def affected_scenarios(root: Path, changed_paths, graph: dict | None = None, *,
                       sub_edges: dict[str, dict] | None = None,
                       triggers: set[str] = _GLOBAL_TRIGGERS) -> dict[str, bool]:
    """sid → affected? True = a change touches this scenario's import closure (or we
    cannot prove it does not) → it WOULD run. Correctness-first; every fallback RUNS.

    FB-0  nothing changed            → nothing affected (a legit no-op, not a doubt).
    FB-1  no/stale/bad graph         → ALL affected (cannot trace anything).
    FB-2  a global-trigger changed   → ALL affected (subprocess blind spot).
    FB-3  a changed path is NOT an import-graph-attributable module → ALL affected
          (a data file, spec, config, tool, or a path absent from the graph — its
          blast radius cannot be bounded by import edges). REFINEMENT
          (fb3-docs-path-refinement): a non-attributable remainder made ONLY of
          docs/**.md paths is bounded by its READERS (extractor docsReads over
          merged closures) — a docs path nobody reads affects nobody; any doubt
          falls back to ALL.
    FB-4  a scenario's closure is unknown → that scenario affected.

    wf-subprocess-edges (SPEC-159 v3, SHADOW): pass `sub_edges` (the subprocess/
    source-read extract) + `triggers=_GLOBAL_TRIGGERS_V2` to compute the second,
    edge-aware map (affectedV2). The floor shrinks (harness.py/common.py/... are no
    longer global — they trace through the merged closure), but every FB-0..FB-4
    doubt still RUNS; `sub_edges=None` + the default floor is the today's map exactly.
    """
    graph = load_graph() if graph is None else graph
    sids = scenario_ids(root)
    changed = {_norm(p) for p in changed_paths}
    if not changed:  # FB-0
        return {s: False for s in sids}
    if graph is None:  # FB-1
        return {s: True for s in sids}
    if changed & triggers:  # FB-2
        return {s: True for s in sids}
    nodes = _node_paths(graph)

    def attributable(p: str) -> bool:
        # Bound-able purely by import edges only if it is a python module node under
        # scripts/ or testing/scenarios/ — the only things a scenario imports.
        return p in nodes and (p.startswith("scripts/") or p.startswith(SCEN_REL + "/"))

    non_attr = [p for p in changed if not attributable(p)]
    docs_map: dict[str, bool] | None = None
    if non_attr:
        # FB-3 docs refinement (fb3-docs-path-refinement, owner 2026-07-22): a
        # remainder made ONLY of docs/**.md paths is bounded by its READERS
        # (merged-closure docsReads) instead of ALL-affected — the backlog/
        # bookkeeping edit that rides every integration commit must not erase
        # every cache skip. ANY other non-attributable path (specs/, data,
        # configs, ui/, ghosts), or any doubt computing readers, keeps FB-3 ALL.
        if all(p.startswith("docs/") and p.endswith(".md") for p in non_attr):
            docs_map = _docs_affected(root, non_attr, graph, sub_edges, sids)
        if docs_map is None:  # FB-3
            return {s: True for s in sids}
        changed -= set(non_attr)
    closures = scenario_closures(root, graph, sub_edges)
    out: dict[str, bool] = {}
    for sid in sids:
        cl = closures.get(sid)
        if cl is None:  # FB-4
            out[sid] = True
            continue
        out[sid] = bool(cl & changed) or bool(docs_map and docs_map.get(sid))
    return out


def _docs_affected(root: Path, docs_paths, graph: dict | None,
                   sub_edges: dict[str, dict] | None, sids) -> dict[str, bool] | None:
    """FB-3 docs refinement: sid → affected? for a docs/**.md-only change set.

    A file READS a docs path when its AST resolves that literal (the extractor's
    `docsReads`, the same ONE collection rule as subprocess edges — fixture
    literals over-collect, which only ever ADDS readers). A scenario is affected
    when any file in its MERGED closure (imports + subprocess/source-read edges)
    reads one of the changed paths, or when its closure is unknown (unresolved
    file inside → unknown reads). Returns None on ANY doubt — the caller then
    falls back to the old FB-3 ALL-affected. The known ceiling, accepted with the
    falseSkip auto-disable as the net: a scenario that consumes a docs-derived
    CLI output without importing the reading module NOR calling run() is
    invisible to the merged closure (same entry-file granularity as SPEC-159 v3).
    """
    try:
        from harness_lib import gate_subprocess_edges as gse
        edges = sub_edges if sub_edges is not None else gse.extract(root, graph)
        wanted = set(docs_paths)

        def reads(entries) -> bool:
            # exact .md read, or a trailing-slash prefix entry (a directory-
            # listing read covers every docs path under it).
            return any(e in wanted or (e.endswith("/") and any(p.startswith(e) for p in wanted))
                       for e in entries)

        reading = {f for f, info in edges.items() if reads(info.get("docsReads") or ())}
        # Membership closure WITHOUT scenario_closures' null-on-unresolved policy
        # (which nulls all 178 closures today — the shadow's narrowing=0 cause).
        # An unresolved (opaque-subprocess) member counts ONLY through its own
        # docs literals, i.e. exactly the `reading` test: an opaque argv in a
        # file with zero matching docs literals (async_runtime engine spawns,
        # processes OS calls) is not a reader of THIS change. Residual ceiling,
        # accepted with the LIVE nets — the falseSkip detector auto-disables
        # skips on the first false green, and serial runs stay exhaustive by
        # design: an opaque program whose OUTPUT depends on a docs file named
        # NOWHERE in reachable sources.
        # fb3-hub-fanin lever (a): the docs BFS follows EXEC edges only — graph
        # imports + subprocess argv/wrapper targets (`execEdges`). Source-read
        # edges (read_text of a .py, docstring literals) are excluded: reading a
        # module's BYTES cannot transport a docs-file change. The V2 shadow and
        # scenario_closures keep consuming the full `edges` union unchanged.
        adj = _import_adjacency(graph)
        for src, info in edges.items():
            for tgt in (info.get("execEdges") if info.get("execEdges") is not None
                        else info.get("edges")) or ():
                adj.setdefault(_norm(src), set()).add(_norm(tgt))
        nodes = _node_paths(graph)
        flagged = reading
        # fb3-verb-level-docs-edges: the wrapper edge scenario->harness.py pulls
        # the WHOLE CLI import universe into every run()-caller's reach (the
        # backlog rider marked ~157/178 through tasks_board alone). When the
        # scenario's called VERBS are all literal and the registry maps them,
        # replace that edge's expansion with: the scenario's own reach WITHOUT
        # the wrapper edge, plus harness.py + cli_registry as bare members
        # (their own code runs for every verb; their imports do not), plus the
        # FULL closure of each called verb's handler modules. Any doubt — no
        # map, unknown verb, opaque call site — keeps the full harness closure.
        wrapper = "scripts/harness.py"
        registry = "scripts/harness_lib/cli_registry.py"
        verb_map = gse.registry_verb_map(root, graph)
        out: dict[str, bool] = {}
        for sid in sids:
            node = f"{SCEN_REL}/{sid}.py"
            if node not in nodes:
                out[sid] = True  # not in the graph → unknown → affected
                continue
            info = edges.get(node) or {}
            verbs = info.get("wrapperVerbs") or set()
            scoped = (verb_map is not None and not info.get("wrapperOpaque")
                      and wrapper in (info.get("edges") or ())
                      and all(v in verb_map for v in verbs))
            if scoped:
                adj2 = dict(adj)
                adj2[node] = set(adj.get(node) or set()) - {wrapper}
                members = _closure(node, adj2) | {node, wrapper, registry}
                for v in verbs:
                    for mod in verb_map[v]:
                        members |= _closure(mod, adj) | {mod}
                out[sid] = bool(members & flagged)
            else:
                cl = _closure(node, adj)
                cl.add(node)
                out[sid] = bool(cl & flagged)
        return out
    except Exception:  # noqa: BLE001 — refinement must never break the gate: doubt = ALL
        return None


# --- per-scenario cache key --------------------------------------------------

def blob_shas(manifest_lines) -> dict[str, str]:
    """path → git index blob sha, parsed from `validation_stamp.staged_manifest`
    lines (`"<mode> <sha>\\t<path>"`). Reuses the stamp's index read; no new git."""
    out: dict[str, str] = {}
    for ln in manifest_lines:
        meta, tab, path = ln.partition("\t")
        if not tab:
            continue
        fields = meta.split()
        if len(fields) >= 2:
            out[_norm(path)] = fields[1]
    return out


def closure_hash(closure_paths, shas: dict[str, str]) -> str:
    """Stable hash of a scenario's closure contents. A missing blob → '?' → the hash
    moves → a cache MISS → the scenario runs (safe)."""
    parts = sorted(f"{_norm(p)}\t{shas.get(_norm(p), '?')}" for p in closure_paths)
    return hashlib.sha256(("\n".join(parts) + "\n").encode("utf-8")).hexdigest()


def scenario_cache_key(sid: str, closure_h: str, gate_version: str) -> str:
    """hash(scenario-id + closure-hash + gate-version) — the Phase-2 skip key."""
    raw = f"{sid}\x00{closure_h}\x00{gate_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- the false-skip decision (pure; the safety core) -------------------------

def shadow_row(affected: bool, cached: dict | None, verdict: str, subprocess_s: float) -> dict:
    """The Phase-1 shadow decision for one scenario, as row fields. PURE + auditable.

      wouldSkip  = a future cache WOULD skip this run: NOT affected AND a cache hit.
      falseSkip  = THE SAFETY VIOLATION: a would-skip scenario whose ACTUAL verdict
                   differs from the cached one — skipping it would ship a false green.
      wouldSaveS = the subprocess time a skip WOULD reclaim (0 unless would-skip).

    Note the double guard: `affected` alone forces a run even on a cache hit, so a
    scenario whose closure changed is never a skip candidate."""
    hit = cached is not None
    would_skip = (not affected) and hit
    false_skip = bool(would_skip and cached and cached.get("verdict") != verdict)
    return {
        "affected": affected,
        "cacheHit": 1 if hit else 0,
        "cacheMiss": 0 if hit else 1,
        "wouldSkip": would_skip,
        "falseSkip": false_skip,
        "wouldSaveS": float(subprocess_s or 0.0) if would_skip else 0.0,
    }


# --- shadow ledger (used by spec_test_gate; measure-only) --------------------

class ShadowLedger:
    """SPEC-159 Phase 1 bookkeeping over one scenarios run. Built once; `annotate`
    decorates each scenario's perf row with the shadow fields (and SHOUTS on a
    false-skip); `commit` persists the fresh per-scenario verdicts. It NEVER skips a
    scenario and NEVER raises out — any construction error leaves it idle (ok=False),
    so a broken shadow layer can never affect the gate."""

    def __init__(self, root: Path, subject_pin: str | None = None, base_head: str | None = None,
                 gate_version: str | None = None):
        # `subject_pin` binds the CANDIDATE side of selection to the frozen P (its
        # tree); `base_head` binds the BASELINE side to the frozen H (its tree);
        # `gate_version` (when given) pins the skip-cache version to the FROZEN P so a
        # post-freeze mutation of a validator input cannot move the skip key and skip
        # a scenario belonging to P. All three come from the same FrozenSubject, so
        # neither a post-freeze index move (C2), HEAD move (H2), nor validator-input
        # mutation can change whether a C1 scenario is scheduled. None = incumbent
        # live read (non-candidate: byte-identical).
        self.root = root
        self.subject_pin = subject_pin
        self.base_head = base_head
        self._gate_version_override = gate_version
        self.ok = False
        self.skip_disabled = False  # SPEC-159 Phase-2: latches True on a falseSkip (disarms wants_skip)
        self.graph_fresh = False  # graph-refresh-decoupled rule 12; False until proven (disarms partition_skips)
        self.reason = ""
        self.fresh: dict[str, dict] = {}
        self.subprocess_edges_enabled = False  # wf-subprocess-edges flag (read in _build_v2)
        self.affectedV2: dict[str, bool] | None = None  # SPEC-159 v3 SHADOW map; None = not built/off
        self.closuresV2: dict[str, set[str] | None] | None = None
        try:
            from harness_lib import validation_stamp as vs
            self._vs = vs
            policy = vs.load_policy(root)
            if policy is None:
                self.reason = "precommitValidation not adopted; shadow idle"
                return
            # SELECTION candidate = the FROZEN candidate tree P; BASELINE = the FROZEN
            # base HEAD H (both via ls-tree, same canonical form) — so `changed` is
            # C-vs-H from the freeze, immune to a later index OR HEAD move. Incumbent
            # (non-candidate) reads the live index and symbolic HEAD, unchanged.
            staged = (vs._tree_manifest(root, subject_pin, policy) if subject_pin
                      else vs.staged_manifest(root, policy))
            head = (vs._tree_manifest(root, base_head, policy) if base_head
                    else vs.head_manifest(root, policy))
            self.changed = vs._changed_paths(staged, head)
            self.shas = blob_shas(staged)
            # gate_version: candidate-bound uses the FROZEN override ONLY (may be None
            # -> a key that matches no cached pass, so wants_skip never skips) and NEVER
            # the live value; non-candidate keeps the incumbent live read.
            self.gate_version = (self._gate_version_override if subject_pin
                                 else (self._gate_version_override or vs.validator_version(root)))
            self.graph = load_graph(root / "graphify-out" / "graph.json")
            # Answered HERE, beside the load, because it is a property of the graph
            # this ledger actually read — not of whatever is on disk when some later
            # caller happens to ask.
            self.graph_fresh = _graph_freshness_state(root) == "fresh"
            self.affected = affected_scenarios(root, self.changed, self.graph)
            self.closures = scenario_closures(root, self.graph)
            self.cache = vs.read_scenario_verdicts(root)
            self.ok = True
            self._build_v2(root)  # wf-subprocess-edges SHADOW: own fail-open; never flips ok
        except Exception as exc:  # noqa: BLE001 — measure-only must never break the gate
            self.reason = f"shadow disabled: {exc!r}"

    def _build_v2(self, root: Path) -> None:
        """wf-subprocess-edges (SPEC-159 v3, SHADOW): compute a SECOND affected map
        (`affectedV2`) that merges subprocess/source-read edges into the closure and
        drops the four wrapper-reachable members from the floor (`_GLOBAL_TRIGGERS_V2`).
        MEASURE-ONLY — wants_skip/partition_skips still use `self.affected`, so the
        narrowing flip stays a later owner decision. Gated behind
        `validation.subprocessEdgesEnabled` (project.json; default absent/false =
        byte-identical to today: affectedV2 stays None and annotate stamps no V2
        fields). Fully fail-open: any error leaves affectedV2 None."""
        try:
            from harness_lib.common import read_json
            self.subprocess_edges_enabled = bool(
                read_json(root / ".harness" / "project.json", {})
                .get("validation", {}).get("subprocessEdgesEnabled", False))
            if not self.subprocess_edges_enabled:
                return
            from harness_lib import gate_subprocess_edges as gse
            self.sub_edges = gse.extract(root, self.graph)
            self.affectedV2 = affected_scenarios(root, self.changed, self.graph,
                                                 sub_edges=self.sub_edges,
                                                 triggers=_GLOBAL_TRIGGERS_V2)
            self.closuresV2 = scenario_closures(root, self.graph, self.sub_edges)
        except Exception:  # noqa: BLE001 — a V2 bug must never disable the today's-map shadow
            self.affectedV2 = None
            self.closuresV2 = None

    def _key(self, sid: str):
        cl = self.closures.get(sid)
        if not cl:  # unknown closure → no stable key → never would-skip
            return None, None
        ch = closure_hash(cl, self.shas)
        return scenario_cache_key(sid, ch, self.gate_version), ch

    def wants_skip(self, sid: str) -> dict | None:
        """SPEC-159 Phase-2 (owner flip, EXP-29): the deterministic PRE-RUN skip
        decision. Returns the cached verdict entry to reuse IFF skipping this
        scenario before it runs is provably safe — the ledger is healthy, the
        falseSkip auto-disable has NOT latched, the scenario is NOT in the affected
        set, its cache key is stable (a moved closure blob or gate-version changes
        the key → miss), and the cached verdict is a PASS. ANY doubt → None (runs).
        Pure read: never mutates the store, so a skip can never feed the cache."""
        if not self.ok or self.skip_disabled:
            return None
        try:
            if self.affected.get(sid, True) is not False:  # default/affected → run
                return None
            key, _ch = self._key(sid)
            if not key:  # unknown closure → no stable key → run
                return None
            cached = self.cache.get(key)
            if not isinstance(cached, dict) or cached.get("verdict") != "pass":
                return None  # miss, corrupt entry, or a non-pass verdict → run
            return cached
        except Exception:  # noqa: BLE001 — fail-open: any doubt runs the scenario
            return None

    def annotate(self, row: dict, sid: str, passed: bool) -> None:
        """Stamp the shadow fields onto `row` (a `_PERF_ROWS` entry) in place.
        Fully fail-open like __init__/commit: a shadow bug must decorate nothing,
        never propagate into the gate loop (measure-only contract)."""
        if not self.ok:
            return
        try:
            verdict = "pass" if passed else "fail"
            key, ch = self._key(sid)
            cached = self.cache.get(key) if key else None
            cached = cached if isinstance(cached, dict) else None  # corrupt non-dict entry → treat as MISS (runs), never raise
            affected = self.affected.get(sid, True)  # default: affected
            fields = shadow_row(affected, cached, verdict, float(row.get("subprocessS") or 0.0))
            row.update(fields)
            if fields["falseSkip"]:
                # SPEC-159 Phase-2 auto-disable: a cached PASS that ACTUALLY failed is
                # the exact signature a skip would have shipped as a false green. Disarm
                # wants_skip for the REST of this run (already-emitted skips stay skipped
                # — bounded exposure); the fresh FAIL verdict below invalidates the cache
                # for the next run.
                self.skip_disabled = True
                print(
                    f"!! SPEC-159 FALSE-SKIP DETECTED: scenario '{sid}' is marked would-skip "
                    f"(cached verdict={cached.get('verdict')!r}) but ACTUALLY ran {verdict!r}. "
                    "A scenario-level skip here WOULD SHIP A FALSE GREEN — skips auto-disabled.",
                    file=sys.stderr, flush=True,
                )
            affectedV2 = getattr(self, "affectedV2", None)
            if affectedV2 is not None:  # wf-subprocess-edges SHADOW: a SECOND, subprocess-edge-aware
                # would-skip. Reuses `cached` (today's key): a not-affectedV2 scenario has an
                # unchanged import closure too (merged ⊇ imports), so the today's key still hits.
                # MEASURE-ONLY — never latches skip_disabled, never feeds wants_skip (the flip is later).
                v2 = shadow_row(affectedV2.get(sid, True), cached, verdict, float(row.get("subprocessS") or 0.0))
                row["affectedV2"] = v2["affected"]
                row["wouldSkipV2"] = v2["wouldSkip"]
                row["falseSkipV2"] = v2["falseSkip"]
                row["wouldSaveV2S"] = v2["wouldSaveS"]
                if v2["falseSkip"]:
                    print(
                        f"!! SPEC-159 v3 SHADOW: subprocess-edges V2 would-skip '{sid}' diverged "
                        f"(cached {cached.get('verdict') if cached else None!r} vs actual {verdict!r}) "
                        "— measure-only, no skip taken.",
                        file=sys.stderr, flush=True,
                    )
            if key:  # refresh the store with the real verdict for the next run
                self.fresh[key] = {
                    "scenario": sid, "verdict": verdict, "closureHash": ch,
                    "gateVersion": self.gate_version, "at": now(),
                    "subprocessS": float(row.get("subprocessS") or 0.0),  # Phase-2 savedS source
                }
        except Exception:  # noqa: BLE001 — measure-only must never break the gate
            pass

    def commit(self) -> None:
        # Deliberate no-op when `fresh` is empty (e.g. every scenario skipped):
        # nothing ran, so there is no verdict evidence to persist — the store
        # keeps its previous entries untouched.
        if not (self.ok and self.fresh):
            return
        try:
            self._vs.write_scenario_verdicts(self.root, self.fresh)
        except Exception:  # noqa: BLE001 — persistence failure must not break the gate
            pass


# --- Phase-2 call-site pre-filter (extracted whole; keeps the gate file thin) -

def _graph_freshness_state(root: Path | None) -> str:
    """`fresh` only when the ONE freshness computation says so; any other state and
    any failure read as not-fresh, because the caller's fallback (run everything) is
    the safe direction. Never raises into the gate."""
    if root is None:
        return "unknown"
    try:
        from harness_lib import graphify_code_ast
        return str(graphify_code_ast.freshness(Path(root))["state"])
    except Exception:  # noqa: BLE001 — a freshness we cannot compute is not a fresh one
        return "unknown"


def partition_skips(ledger: "ShadowLedger", order, *, result_fn, enabled: bool, serial: bool):
    """SPEC-159 Phase-2 pre-filter (owner flip 2026-07-22, EXP-29), extracted whole
    from spec_test_gate so the gate file stays under the gs-7 line ratchet. Split
    `order` into `(skip_rows, skip_perf, remainder)`: `skip_rows` are ready-to-append
    `skip` result rows (built via `result_fn` — a SKIP is NEVER a pass and never feeds
    the verdict cache), `skip_perf` the matching `{skipped, savedS}` perf rows, and
    `remainder` the paths to shard. Skips apply ONLY when `enabled and not serial` —
    otherwise returns `([], [], list(order))` so the caller shards the full order and
    the serial loop / parallel fallback stays EXHAUSTIVE. `wants_skip` is a pure read
    that returns None on any doubt, so a broken ledger skips nothing (fail-open).

    A NON-FRESH graph is one of those doubts, and it is the sharpest one, because it
    is invisible to every other guard here. The skip key hashes the closure
    (`scenario_cache_key` = sid + closure_h + gate_version), and a stale graph hands
    back a scenario's OLD closure: one that just gained an import keeps its old key,
    still reads "unaffected", and gets skipped. No detector catches it either — the
    false-skip detector only sees scenarios that RUN. `load_graph`'s fail-open note
    covers the file ABSENT from the graph (FB-3 attributability), not the file whose
    EDGES moved.

    The gate does rebuild the graph before it partitions, but that ordering is not the
    safety property — it is two unrelated flags (`knowledgeGraph.enabled`,
    `graphifyAst.enabled`) happening to be on, while `validation.scenarioSkipEnabled`
    is a third. Turn graphify off and the skips would silently ride a frozen graph. So
    the ledger answers this for itself when it loads the graph (`graph_fresh`), and a
    missing attribute reads as untrusted — inheriting someone else's refresh is exactly
    the coupling this removes."""
    if not enabled or serial:
        return [], [], list(order)
    if not getattr(ledger, "graph_fresh", False):  # default False: absent == untrusted
        return [], [], list(order)
    skip_rows: list = []
    skip_perf: list = []
    remainder: list = []
    for path in order:
        cached = ledger.wants_skip(path.stem)
        if cached is None:
            remainder.append(path)
        else:
            skip_rows.append(result_fn(f"scenarios:{path.stem}", "skip",
                f"cache-skip (SPEC-159): closure unchanged, cached pass {cached.get('at', '?')}"))
            skip_perf.append({"skipped": 1, "savedS": float(cached.get("subprocessS") or 0.0)})
    return skip_rows, skip_perf, remainder


# --- self-check --------------------------------------------------------------

def _self_check() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scen = root / SCEN_REL
        scen.mkdir(parents=True)
        # Three scenarios: alpha imports mod_a; beta imports mod_b; gamma imports _lib only.
        for name in ("alpha", "beta", "gamma"):
            (scen / f"{name}.py").write_text("x=1\n", encoding="utf-8")
        (scen / "_lib.py").write_text("x=1\n", encoding="utf-8")  # not enumerated

        graph = {
            "schemaVersion": "graphify-code-ast/v1",
            "nodes": [{"path": p, "id": p} for p in [
                "testing/scenarios/alpha.py", "testing/scenarios/beta.py",
                "testing/scenarios/gamma.py", "testing/scenarios/_lib.py",
                "scripts/harness_lib/mod_a.py", "scripts/harness_lib/mod_b.py",
                "scripts/harness_lib/shared.py",
            ]],
            "edges": [
                {"source": "testing/scenarios/alpha.py", "target": "scripts/harness_lib/mod_a.py", "type": "imports"},
                {"source": "testing/scenarios/beta.py", "target": "scripts/harness_lib/mod_b.py", "type": "imports"},
                {"source": "scripts/harness_lib/mod_a.py", "target": "scripts/harness_lib/shared.py", "type": "imports"},
                {"source": "testing/scenarios/gamma.py", "target": "testing/scenarios/_lib.py", "type": "imports"},
            ],
        }

        # closure: alpha -> {mod_a, shared, alpha}; transitive edge captured.
        cl = scenario_closures(root, graph)
        assert cl["alpha"] == {"scripts/harness_lib/mod_a.py", "scripts/harness_lib/shared.py",
                               "testing/scenarios/alpha.py"}, cl["alpha"]
        assert cl["beta"] == {"scripts/harness_lib/mod_b.py", "testing/scenarios/beta.py"}, cl["beta"]

        # 1) changed module marks only its dependents affected (import-closure intersection).
        aff = affected_scenarios(root, ["scripts/harness_lib/mod_a.py"], graph)
        assert aff == {"alpha": True, "beta": False, "gamma": False}, aff
        # transitive: shared is in alpha's closure via mod_a.
        aff = affected_scenarios(root, ["scripts/harness_lib/shared.py"], graph)
        assert aff["alpha"] is True and aff["beta"] is False, aff
        # a changed scenario file marks itself (and only itself) affected.
        aff = affected_scenarios(root, ["testing/scenarios/beta.py"], graph)
        assert aff == {"alpha": False, "beta": True, "gamma": False}, aff

        # 2) FB-3: a data/spec/unknown change → ALL affected (conservative). Includes
        # ui/ paths (wf-ui-surface-gate-coverage): a ui/src or ui/dist change is not an
        # import-graph-attributable python module, so FB-3 runs the WHOLE battery — the
        # ui scenarios (ui_e2e/pw_ui_smoke/ds/rh_gui/m5) included — even once Phase-2 skip
        # is enabled. So adding ui/ to the surface never opens a false-skip on ui.
        for p in ("specs/40-features/x.md", "some/data.json", "scripts/harness_lib/ghost.py",
                  "ui/src/api/experiments.ts", "ui/dist/assets/index-abc.js"):
            aff = affected_scenarios(root, [p], graph)
            assert all(aff.values()), (p, aff)

        # 2b) FB-3 docs refinement (fb3-docs-path-refinement): a docs/**.md-only
        # change is bounded by its READERS; anything else in case 2 stayed ALL.
        (root / "scripts" / "harness_lib").mkdir(parents=True, exist_ok=True)
        for m in ("mod_a", "shared"):
            (root / f"scripts/harness_lib/{m}.py").write_text("x=1\n", encoding="utf-8")
        (root / "scripts/harness_lib/mod_b.py").write_text(          # module reader
            "from pathlib import Path\nROOT = Path('.')\n"
            "txt = (ROOT / 'docs' / 'methods.md').read_text()\n", encoding="utf-8")
        (scen / "gamma.py").write_text(                              # direct scenario reader
            "from pathlib import Path\nt = Path('docs/guide.md')\n", encoding="utf-8")
        # no reader anywhere -> NOBODY affected (the skip the backlog rider needs)
        aff = affected_scenarios(root, ["docs/lonely.md"], graph)
        assert not any(aff.values()), aff
        # direct scenario reader -> only gamma
        aff = affected_scenarios(root, ["docs/guide.md"], graph)
        assert aff == {"alpha": False, "beta": False, "gamma": True}, aff
        # module reader via import closure -> only beta (beta imports mod_b)
        aff = affected_scenarios(root, ["docs/methods.md"], graph)
        assert aff == {"alpha": False, "beta": True, "gamma": False}, aff
        # mixed docs+code commit: closure trace OR docs readers — the integration-
        # commit shape (code + backlog strike) finally narrows instead of ALL.
        aff = affected_scenarios(root, ["docs/guide.md", "scripts/harness_lib/mod_a.py"], graph)
        assert aff == {"alpha": True, "beta": False, "gamma": True}, aff
        # docs + a NON-docs unattributable path -> old FB-3 ALL (no refinement).
        aff = affected_scenarios(root, ["docs/guide.md", "some/data.json"], graph)
        assert all(aff.values()), aff

        # 2c) fb3-verb-level: the wrapper edge stops pulling the whole CLI
        # universe when the called verbs are literal and registry-mapped. Graph:
        # harness.py imports mod_b (the docs/methods.md reader); delta calls
        # ONLY verb va (-> mod_a), epsilon calls vb (-> mod_b).
        (root / "scripts" / "harness_lib" / "cli_registry.py").write_text(
            "def register(sub, h):\n"
            "    p = sub.add_parser('va'); p.set_defaults(func=mod_a.cmd_va)\n"
            "    p = sub.add_parser('vb'); p.set_defaults(func=mod_b.cmd_vb)\n",
            encoding="utf-8")
        (root / "scripts" / "harness.py").write_text("x = 1\n", encoding="utf-8")
        (scen / "delta.py").write_text("from _lib import run\nrun('va')\n", encoding="utf-8")
        (scen / "epsilon.py").write_text("from _lib import run\nrun('vb')\n", encoding="utf-8")
        graph3 = {
            "schemaVersion": "graphify-code-ast/v1",
            "nodes": graph["nodes"] + [{"path": p, "id": p} for p in [
                "scripts/harness.py", "scripts/harness_lib/cli_registry.py",
                "testing/scenarios/delta.py", "testing/scenarios/epsilon.py"]],
            "edges": graph["edges"] + [
                {"source": "scripts/harness.py", "target": "scripts/harness_lib/cli_registry.py", "type": "imports"},
                {"source": "scripts/harness_lib/cli_registry.py", "target": "scripts/harness_lib/mod_b.py", "type": "imports"},
            ],
        }
        aff = affected_scenarios(root, ["docs/methods.md"], graph3)
        assert aff.get("delta") is False, aff      # va-only: mod_b never reached
        assert aff.get("epsilon") is True, aff     # vb: closure(mod_b) reads it
        assert aff.get("beta") is True, aff        # import path unaffected by scoping
        # an OPAQUE call site keeps the full harness closure -> affected again.
        (scen / "delta.py").write_text(
            "from _lib import run\nv = 'va'\nrun(v)\n", encoding="utf-8")
        aff = affected_scenarios(root, ["docs/methods.md"], graph3)
        assert aff.get("delta") is True, aff
        # leftover 2c scenarios would leak into the later exact-dict cases
        # (scenario_ids reads the filesystem) — remove them.
        (scen / "delta.py").unlink()
        (scen / "epsilon.py").unlink()

        # 3) FB-2: a global-trigger change → ALL affected.
        aff = affected_scenarios(root, ["scripts/harness.py"], graph)
        assert all(aff.values()), aff

        # 4) FB-1: no graph → ALL affected.
        assert all(affected_scenarios(root, ["scripts/harness_lib/mod_a.py"], None).values())

        # 5) FB-0: nothing changed → nothing affected.
        assert not any(affected_scenarios(root, [], graph).values())

        # 6) cache key stability: same closure blobs + gate-version → same key;
        #    a changed blob → different key (→ cache miss → runs).
        shas = {"scripts/harness_lib/mod_a.py": "aaa", "scripts/harness_lib/shared.py": "bbb",
                "testing/scenarios/alpha.py": "ccc"}
        h1 = closure_hash(cl["alpha"], shas)
        k1 = scenario_cache_key("alpha", h1, "gv1")
        assert k1 == scenario_cache_key("alpha", closure_hash(cl["alpha"], dict(shas)), "gv1")
        shas2 = dict(shas, **{"scripts/harness_lib/shared.py": "changed"})
        assert closure_hash(cl["alpha"], shas2) != h1, "changed blob must move the hash"
        assert scenario_cache_key("alpha", h1, "gv2") != k1, "gate-version must move the key"
        # missing blob → '?' sentinel → different hash than present.
        assert closure_hash(cl["alpha"], {}) != h1

        # 7) wants_skip + skip_disabled (Phase-2). Fabricate a ledger's state (bypass
        #    __init__, which needs a git repo) and exercise the pre-run skip decision.
        led = ShadowLedger.__new__(ShadowLedger)
        led.ok = True
        led.skip_disabled = False
        led.graph_fresh = True  # rule 12: __init__ derives this; fabrication must state it
        led.fresh = {}
        led.closures = {"alpha": cl["alpha"], "beta": cl["beta"]}  # gamma omitted → unknown closure
        led.shas = {p: "s" for p in (cl["alpha"] | cl["beta"])}
        led.gate_version = "gv1"
        led.affected = {"alpha": True, "beta": False, "gamma": True}
        beta_key = led._key("beta")[0]
        led.cache = {beta_key: {"scenario": "beta", "verdict": "pass", "subprocessS": 2.0, "at": "t"}}
        assert led.wants_skip("beta") == led.cache[beta_key], "unaffected + cached pass → skip the pass"
        assert led.wants_skip("alpha") is None, "affected overrides the cache hit → run"
        assert led.wants_skip("gamma") is None, "unknown closure → no stable key → run"
        # partition_skips: beta → a skip row + savedS perf; alpha/gamma → remainder.
        rf = lambda name, status, detail="": {"name": name, "status": status, "detail": detail}
        order = [Path("testing/scenarios/beta.py"), Path("testing/scenarios/alpha.py"),
                 Path("testing/scenarios/gamma.py")]
        rows, perf, remainder = partition_skips(led, order, result_fn=rf, enabled=True, serial=False)
        assert [r["name"] for r in rows] == ["scenarios:beta"] and rows[0]["status"] == "skip", rows
        assert perf == [{"skipped": 1, "savedS": 2.0}], perf
        assert [p.stem for p in remainder] == ["alpha", "gamma"], remainder
        assert partition_skips(led, order, result_fn=rf, enabled=False, serial=False) == ([], [], order)
        assert partition_skips(led, order, result_fn=rf, enabled=True, serial=True) == ([], [], order)
        # rule 12: a graph we cannot vouch for disarms the pre-filter entirely.
        led.graph_fresh = False
        assert partition_skips(led, order, result_fn=rf, enabled=True, serial=False) == ([], [], order)
        del led.graph_fresh  # and an ABSENT verdict is untrusted, never a permissive default
        assert partition_skips(led, order, result_fn=rf, enabled=True, serial=False) == ([], [], order)
        led.graph_fresh = True
        led.cache[beta_key] = {"verdict": "fail"}
        assert led.wants_skip("beta") is None, "a non-pass cached verdict → run"
        led.cache[beta_key] = {"scenario": "beta", "verdict": "pass", "subprocessS": 2.0, "at": "t"}
        # falseSkip auto-disable: beta is cached-pass + unaffected but ACTUALLY fails.
        led.annotate({"subprocessS": 2.0}, "beta", passed=False)
        assert led.skip_disabled is True, "a falseSkip must latch skip_disabled"
        assert led.wants_skip("beta") is None, "auto-disabled → no more skips this run"
        # ok=False (a broken ledger) never skips.
        led.ok, led.skip_disabled = False, False
        assert led.wants_skip("beta") is None, "a broken ledger skips nothing (fail-open)"

        # 8) wf-subprocess-edges SHADOW: sub_edges merge into the closure (additive
        # union); an unresolved node collapses to None (→ always affected); the V2
        # floor keeps only the gate runners global. The floor NARROWING with a real
        # run()-wrapper edge to a dropped member is proven in gac_gate_affected.py.
        sub_edges = {
            "testing/scenarios/alpha.py": {"edges": {"scripts/harness_lib/mod_b.py"}, "unresolved": False},
            "testing/scenarios/gamma.py": {"edges": set(), "unresolved": True},
        }
        cl2 = scenario_closures(root, graph, sub_edges)
        assert "scripts/harness_lib/mod_b.py" in cl2["alpha"], cl2["alpha"]   # subprocess edge merged
        assert cl2["gamma"] is None, cl2["gamma"]                             # unresolved → None
        assert scenario_closures(root, graph, None) == scenario_closures(root, graph)  # byte-identical off
        # a mod_b change now marks alpha (subprocess edge) AND beta (import) affected under V2;
        # the imports-only map marks ONLY beta — proving the merge is additive, never subtractive.
        affV2 = affected_scenarios(root, ["scripts/harness_lib/mod_b.py"], graph,
                                   sub_edges=sub_edges, triggers=_GLOBAL_TRIGGERS_V2)
        assert affV2 == {"alpha": True, "beta": True, "gamma": True}, affV2
        assert affected_scenarios(root, ["scripts/harness_lib/mod_b.py"], graph) == \
            {"alpha": False, "beta": True, "gamma": False}
        # the V2 floor keeps the gate runner global (FB-2 still fires for it under V2).
        assert all(affected_scenarios(root, ["scripts/spec_test_gate.py"], graph,
                                      triggers=_GLOBAL_TRIGGERS_V2).values())

    print("gate_affected self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
