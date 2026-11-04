"""SPEC-107 cheap-discovery routing and MF.4 doc-find (extracted from harness.py, MF.1).

Shared harness helpers/constants arrive via bind(env) at import time —
the same idiom as harness_lib.workflow_reduce and the gate_fixtures modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import secret_scan  # SEC.1: reuse anchored patterns, do not re-implement


def bind(env: dict[str, Any]) -> None:
    """Receive shared runtime helpers/constants from scripts/harness.py."""
    globals().update(env)


DISCOVER_TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".xml", ".ini", ".cfg", ".log"}

DISCOVER_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

DISCOVER_CODE_EXTENSIONS = {".py"}

# SPEC-165 R9: chain hops name executors; discovery runs providers. Known map only —
# an unknown executor in a chain is skipped LOUDLY (graph_providers fellBackTo style).
_EXECUTOR_TO_PROVIDER = {"gemini-compat": "gemini-api", "nvidia-compat": "nvidia-build"}

_EGRESS_DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

def _pre_egress_reason(src: Path, kind: str) -> str | None:
    """SEC.1: deterministic pre-egress gate — reason name only, never a secret value."""
    posix = src.as_posix()
    if src.name.startswith(".env") or src.suffix.lower() in _EGRESS_DENY_SUFFIXES \
            or ".harness/state" in posix or ".git/" in posix:
        return "denylist"
    if kind == "text":
        hits = secret_scan.scan(src.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            return hits[0]["pattern"]
    return None

def _role_provider_order(root: Path, role: str) -> tuple[list[str], list[dict[str, Any]]] | None:
    """SPEC-165 R9: resolve a discovery role to (provider names, loud skip records).

    Returns None for canonical / no primary / ANY exception, so a missing or broken
    routing file falls back to today's hardcoded order (byte-compat). Blanket
    try/except precedent: chat_engines.build_engine. resolve_role is called with
    executor=None on purpose — the executor head-filter would drop an http primary.
    An executor with no discovery-provider mapping, or (discovery-image only) a hop
    whose card is not vision-capable, is skipped LOUDLY rather than silently dropped."""
    try:
        from harness_lib import model_routing  # read-only consume; zero edits there
        res = model_routing.resolve_role(root, role)  # executor=None: no head-filter
        if res.get("profile") == model_routing.CANONICAL or not res.get("primary"):
            return None
        chain = [res["primary"], *(res.get("fallbacks") or [])]
        cards = {c["id"]: c for c in model_routing.cards_list(root)} if role == "discovery-image" else {}
        order: list[str] = []
        skips: list[dict[str, Any]] = []
        for hop in chain:
            executor = hop.get("executor")
            provider = _EXECUTOR_TO_PROVIDER.get(executor)
            if not provider:
                reason = (f"executor {executor} has no discovery provider mapping; "
                          "known: gemini-compat, nvidia-build")
                skips.append({"name": executor, "available": False, "skipped": True,
                              "closed": [reason], "reason": reason})
                continue
            if role == "discovery-image":
                card_id = hop.get("card")
                card = cards.get(card_id)
                if not card or card.get("vision") is not True:
                    reason = (f"card {card_id} is not vision-capable; discovery-image "
                              "needs a card with vision: true")
                    skips.append({"name": executor, "available": False, "skipped": True,
                                  "closed": [reason], "reason": reason})
                    continue
            order.append(provider)
        return order, skips
    except Exception:
        return None  # a broken routing file must never break discover


def _discover_provider_gates(base_root: Path, role: str) -> list[dict[str, Any]]:
    """Provider chain with per-provider availability and the fix for each closed gate.

    SPEC-165 R9: the ORDER comes from the discovery role when a routing profile
    defines it, else today's hardcoded gemini-api -> nvidia-build. Gate AVAILABILITY
    (enabled flags + env keys) still reads the live load_project() — this slice
    changes order selection only. Loud skip records ride along in the gates list."""
    order = _role_provider_order(base_root, role)
    kg = load_project().get("knowledgeGraph", {}) if isinstance(load_project().get("knowledgeGraph"), dict) else {}
    providers_cfg = kg.get("apiAssistedProviders", {}) if isinstance(kg.get("apiAssistedProviders"), dict) else {}
    llm = kg.get("llmAssisted", {}) if isinstance(kg.get("llmAssisted"), dict) else {}
    gates: list[dict[str, Any]] = []
    if order is not None:
        provider_names, skip_records = order
    else:
        provider_names, skip_records = providers_cfg.get("order", ["gemini-api", "nvidia-build"]), []
    for name in provider_names:
        if name == "gemini-api":
            enabled = llm.get("enabled") is True
            key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
            reasons = []
            if not enabled:
                reasons.append("knowledgeGraph.llmAssisted.enabled is false")
            if not key:
                reasons.append("GEMINI_API_KEY/GOOGLE_API_KEY not set")
            gates.append({"name": name, "script": "gemini_extract_fallback.py", "available": enabled and key,
                          "closed": reasons, "fix": "enable knowledgeGraph.llmAssisted.enabled and set GEMINI_API_KEY (env or .env — copy .env.example)"})
        elif name == "nvidia-build":
            nvidia = providers_cfg.get("nvidia", {}) if isinstance(providers_cfg.get("nvidia"), dict) else {}
            enabled = nvidia.get("enabled") is True
            key = bool(os.environ.get("NVIDIA_API_KEY"))
            reasons = []
            if not enabled:
                reasons.append("knowledgeGraph.apiAssistedProviders.nvidia.enabled is false")
            if not key:
                reasons.append("NVIDIA_API_KEY not set")
            gates.append({"name": name, "script": "nvidia_extract_fallback.py", "available": enabled and key,
                          "closed": reasons, "fix": "enable knowledgeGraph.apiAssistedProviders.nvidia.enabled and set NVIDIA_API_KEY in env or .env (free key: build.nvidia.com; copy .env.example)"})
    gates.extend(skip_records)  # loud skips surface in providerGates + the refusal reason
    return gates

def _discover_refusal_reason(gates: list[dict[str, Any]], paths: list[str]) -> str:
    causes = "; ".join(f"{g['name']}: {', '.join(g['closed'])}" for g in gates if not g["available"]) or "no provider configured"
    fixes = " OR ".join(g["fix"] for g in gates if g.get("fix")) or "configure knowledgeGraph.apiAssistedProviders"
    return (
        "no cheap API provider available for text/image discovery\n"
        f"cause: {causes}\n"
        f"fix: {fixes}; manual fallback: focused raw search on {', '.join(paths[:5])} only — do not bulk-read with the coder LLM"
    )

def discover_paths(raw_paths: list[str], *, subject_root: Path | None = None, subject_out: Path | None = None) -> dict[str, Any]:
    # SPEC-110: subject-parameterized — same routing for the harness (default)
    # or a registered target repository (its own root, knowledge dir and cache).
    base_root = subject_root or ROOT
    base_out = subject_out or (ROOT / "graphify-out")
    code: list[str] = []
    batches: dict[str, list[str]] = {"text": [], "image": []}
    results: list[dict[str, Any]] = []
    for raw in raw_paths:
        path = base_root / raw if not Path(raw).is_absolute() else Path(raw)
        rel = raw.replace("\\", "/")
        suffix = path.suffix.lower()
        if not path.is_file():
            results.append({"path": rel, "kind": "unknown", "status": "refused", "reason": f"{rel} not found\ncause: path does not exist or is not a file\nfix: check the path"})
        elif suffix in DISCOVER_CODE_EXTENSIONS:
            code.append(rel)
        elif suffix in DISCOVER_TEXT_EXTENSIONS:
            batches["text"].append(rel)
        elif suffix in DISCOVER_IMAGE_EXTENSIONS:
            batches["image"].append(rel)
        else:
            results.append({"path": rel, "kind": "unknown", "status": "refused", "reason": f"unsupported kind {suffix or '(none)'}\ncause: discover routes code/text/images only\nfix: for other formats use focused raw search on this path"})
    if code:
        # Code never reaches an API (forbiddenInputKinds) — the ACTIVE local
        # graph provider builds (graph-provider-abstraction; fallback is loud).
        from harness_lib import graph_providers
        build = graph_providers.build(base_root, output_dir=base_out)
        for rel in code:
            results.append({"path": rel, "kind": "code", "layer": build.get("provider", "graphify-code-ast"), "status": "ok",
                            "detail": f"graph rebuilt: {build.get('filesParsed')} files, {build.get('edges')} edges; query graphify-out/graph.json"})
    # SPEC-165 R9: text and image resolve their provider order independently through
    # the discovery-text / discovery-image routing roles (canonical => today's order).
    gates_by_kind = {"text": _discover_provider_gates(base_root, "discovery-text"),
                     "image": _discover_provider_gates(base_root, "discovery-image")}
    # MF.4: per-file extraction with a sha256 cache — unchanged files never hit
    # an API twice, and every enrichment entry carries its provenance so
    # `doc-find` can answer "where is X" without agents re-reading the docs.
    out_dir = base_out
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "discover-cache.json"
    cache: dict[str, Any] = read_json(cache_path, {}) or {}
    for kind, batch in batches.items():
        if not batch:
            continue
        enrichment_path = out_dir / ("docs-enrichment.json" if kind == "text" else "image-enrichment.json")
        enrichment = read_json(enrichment_path, {}) or {}
        files_map: dict[str, Any] = enrichment.get("files", {}) if isinstance(enrichment.get("files"), dict) else {}
        for rel in batch:
            src = base_root / rel if not Path(rel).is_absolute() else Path(rel)
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            hit = cache.get(digest)
            if hit and hit.get("path") == rel:
                files_map[rel] = hit
                results.append({"path": rel, "kind": kind, "status": "ok", "layer": "cache",
                                "detail": f"unchanged since {hit.get('extractedAt')} (sha256 match); 0 API tokens"})
                continue
            blocked = _pre_egress_reason(src, kind)
            if blocked:
                # SEC.1: nothing spawns, nothing is cached or enriched — the reason
                # names the denylist rule or secret_scan pattern, never the value.
                results.append({"path": rel, "kind": kind, "status": "blocked", "reason": f"pre-egress: {blocked}"})
                continue
            attempted: list[str] = []
            outcome: dict[str, Any] | None = None
            for gate in gates_by_kind[kind]:
                if not gate["available"]:
                    continue
                chunk_out = out_dir / f".discover_chunk_{kind}.json"
                abs_path = str(base_root / rel) if not Path(rel).is_absolute() else rel
                cmd = [sys.executable, str(ROOT / "tools" / "graphify" / gate["script"]), "--out", str(chunk_out), "--kind", kind, abs_path]
                proc = run_process_tree_bounded(cmd, timeout=bounded_timeout("HARNESS_DISCOVER_TIMEOUT", 300), cwd=ROOT)
                attempted.append(gate["name"])
                if proc.returncode == 0:
                    extracted = read_json(chunk_out, {}) or {}
                    entry = {"path": rel, "sha256": digest, "layer": gate["name"], "extractedAt": now(),
                             "nodes": extracted.get("nodes", []), "edges": extracted.get("edges", []),
                             "input_tokens": extracted.get("input_tokens", 0), "output_tokens": extracted.get("output_tokens", 0)}
                    cache[digest] = entry
                    files_map[rel] = entry
                    chunk_out.unlink(missing_ok=True)
                    outcome = {"status": "ok", "layer": gate["name"],
                               "detail": f"{len(entry['nodes'])} nodes, {len(entry['edges'])} edges"}
                    break
                outcome = {"status": "failed", "layer": gate["name"], "reason": (proc.stderr or "")[-500:]}
            if outcome is None:
                outcome = {"status": "refused", "reason": _discover_refusal_reason(gates_by_kind[kind], [rel])}
            elif outcome["status"] == "failed":
                outcome["reason"] += "\nfix: " + (" OR ".join(g["fix"] for g in gates_by_kind[kind] if g.get("fix") and g["name"] not in attempted) or "check provider keys/credits") + "; manual fallback: focused raw search on the named paths only"
            results.append({"path": rel, "kind": kind, **outcome})
        write_json(enrichment_path, {"schemaVersion": "discover-enrichment/v2", "files": files_map})
    write_json(cache_path, cache)
    # providerGates: union of both kinds, each entry annotated ADDITIVELY with its
    # role (+ skipped/reason for loud skips); the name/available/closed keys hooks
    # consume are never renamed or removed.
    provider_gates: list[dict[str, Any]] = []
    for role, kind_gates in (("discovery-text", gates_by_kind["text"]), ("discovery-image", gates_by_kind["image"])):
        for g in kind_gates:
            entry = {k: g[k] for k in ("name", "available", "closed")}
            entry["role"] = role
            for extra in ("skipped", "reason"):
                if extra in g:
                    entry[extra] = g[extra]
            provider_gates.append(entry)
    report = {"inputs": len(raw_paths), "results": results, "providerGates": provider_gates}
    write_json(base_out / "discover-report.json", report)
    return report

_GRAPH_SLICE_CAP = 25  # bound the provenance slice per token — never dump the whole graph


def _graph_claim(e: dict[str, Any], other_key: str) -> dict[str, Any]:
    """Project a graph edge into a hit's claim entry, carrying the honesty fields
    (provenanceTag + status + confidence). `.get` so a malformed edge never crashes."""
    return {other_key: e.get(other_key), "type": e.get("type"), "status": e.get("status"),
            "provenanceTag": e.get("provenanceTag"), "confidence": e.get("confidence"),
            "inferredBy": e.get("inferredBy")}


def _graph_hits(graph: dict[str, Any], needles: list[str], limit: int) -> list[dict[str, Any]]:
    """Backlog-groom P4 graph lane: retrieval over the P2 derived provenance+
    dependency graph. A token (node id/section, or an edge endpoint) whose text
    matches a needle becomes a hit carrying its BOUNDED provenance slice — what it
    cites / what cites it. Every slice edge keeps the edge's provenanceTag + status
    + confidence: a graph hit is a CLAIM to verify, never a fact (measure-honesty).

    Pure + deterministic (derive_graph hands us pre-sorted edges); an empty/absent
    graph or malformed edge yields no hit and never crashes. Tested in __main__."""
    edges = graph.get("edges") or []
    node_text = {n.get("id"): " ".join(str(n.get(k) or "") for k in ("id", "section")).lower()
                 for n in (graph.get("nodes") or []) if n.get("id")}
    tokens = set(node_text)
    for e in edges:
        tokens.update((e.get("from"), e.get("to")))
    tokens.discard(None)
    hits: list[dict[str, Any]] = []
    for tok in sorted(tokens, key=str):
        text = node_text.get(tok) or str(tok).lower()
        if not any(n in text for n in needles):
            continue
        cites = [_graph_claim(e, "to") for e in edges if e.get("from") == tok][:_GRAPH_SLICE_CAP]
        cited_by = [_graph_claim(e, "from") for e in edges if e.get("to") == tok][:_GRAPH_SLICE_CAP]
        if not cites and not cited_by:
            continue  # a token with no provenance slice is nothing learned — not a hit
        hits.append({"token": tok, "kind": "graph", "score": len(cites) + len(cited_by),
                     "cites": cites, "citedBy": cited_by})
    hits.sort(key=lambda h: (-h["score"], str(h["token"])))
    return hits[:limit]


def doc_find(terms: list[str], *, limit: int = 8, subject_out: Path | None = None) -> dict[str, Any]:
    """MF.4: answer "where is X documented" from the cheap-API enrichment graph.

    Scores each enriched file by term matches over its extracted node labels
    and edge relations, so agents get file pointers plus the matched concepts
    instead of re-reading the docs tree.
    """
    base_out = subject_out or (ROOT / "graphify-out")
    enrichment = read_json(base_out / "docs-enrichment.json", {}) or {}
    files_map = enrichment.get("files", {}) if isinstance(enrichment.get("files"), dict) else {}
    modules_available = (base_out / "modules.json").exists()
    if not files_map and not modules_available:
        # SPEC-110: subjects may have only one knowledge source (a target with no
        # enriched docs still has its module index) — refuse only when BOTH are absent.
        raise HarnessError(
            "no knowledge sources available for this subject\n"
            f"cause: neither docs-enrichment.json nor modules.json under {base_out}\n"
            "fix: python scripts/harness.py graph-build-code-ast [--target <n>] and/or discover <docs...> [--target <n>]"
        )
    needles = [t.lower() for t in terms if t.strip()]
    hits: list[dict[str, Any]] = []
    for rel, entry in files_map.items():
        matched: list[str] = []
        for node in entry.get("nodes", []) or []:
            text = " ".join(str(node.get(k) or "") for k in ("id", "label", "name", "type")).lower()
            if any(n in text for n in needles):
                matched.append(str(node.get("label") or node.get("id") or "?"))
        for edge in entry.get("edges", []) or []:
            text = " ".join(str(edge.get(k) or "") for k in ("source", "target", "relation")).lower()
            if any(n in text for n in needles):
                matched.append(f"{edge.get('source')} -{edge.get('relation', 'rel')}-> {edge.get('target')}")
        if matched:
            hits.append({"path": rel, "kind": "doc", "score": len(matched), "matchedConcepts": list(dict.fromkeys(matched))[:10]})
    # I5 (Voyager's retrieval half): executable artifacts — scenarios, tools,
    # libs — are searchable via their module docstrings, so agents find an
    # existing skill before writing a new one.
    modules = read_json(base_out / "modules.json", {}) or {}
    for rel, doc in modules.items():
        text = f"{rel} {doc}".lower()
        matches = sum(1 for n in needles if n in text)
        if matches:
            hits.append({"path": rel, "kind": "code-module", "score": matches, "matchedConcepts": [doc]})
    hits.sort(key=lambda h: -h["score"])
    hits = hits[:limit]
    # Graph lane (backlog-groom P4): after the enrichment+modules hits, also retrieve
    # over the P2 DERIVED provenance+dependency graph, so the next groom/research round
    # recovers what the house already learned (the graph) instead of re-mining raw
    # history. Read-only, no AI, no new store. Additive: graph hits carry kind:"graph"
    # + a provenance slice (a CLAIM to verify). ponytail: derive_graph is harness-self-
    # scoped (P2), so a --target query (subject_out set) skips this lane; add a
    # target-parameterized derive when a MEASURED target-retrieval win justifies it (D008).
    if subject_out is None and needles:
        try:
            from harness_lib import dep_graph  # read-only consume; never write dep_graph
            hits += _graph_hits(dep_graph.derive_graph(ROOT), needles, limit)
        except Exception:
            pass  # never-crash: an absent/broken graph yields no graph hit, normal hits intact
    return {"query": terms, "filesSearched": len(files_map), "hits": hits,
            "hint": "read only the hit files (or grep the matched concepts inside them); do not bulk-read the docs tree"}


def _self_check() -> None:
    """Hermetic self-check for the P4 graph lane (`_graph_hits`) — no bind() needed,
    since the lane is a pure function over an injected graph."""
    graph = {
        "nodes": [{"id": "P4.1", "section": "doc-find graph lane / edge inference"}],
        "edges": [
            {"from": "P4.1", "to": "SPEC-116", "type": "relates-to", "status": "PROPOSED",
             "provenanceTag": "[judgment]", "confidence": 0.3, "inferredBy": "cooccurrence:item"},
            {"from": "D050", "to": "SPEC-116", "type": "relates-to", "status": "VERIFIED",
             "provenanceTag": "[repo]", "confidence": 1.0, "inferredBy": "dep-tag"},
        ],
    }
    # a needle matching a graph token -> a kind:"graph" hit with the provenance slice.
    hits = _graph_hits(graph, ["spec-116"], 8)
    assert hits and hits[0]["kind"] == "graph" and hits[0]["token"] == "SPEC-116", hits
    cited_by = hits[0]["citedBy"]
    # measure-honesty: every slice edge carries provenanceTag + status (a CLAIM, not a fact).
    assert cited_by and all("provenanceTag" in e and "status" in e for e in cited_by), cited_by
    assert {e["status"] for e in cited_by} == {"PROPOSED", "VERIFIED"}, cited_by
    assert {e["from"] for e in cited_by} == {"P4.1", "D050"}, cited_by
    # a needle matching a node's section text (not a token) also hits.
    assert any(h["token"] == "P4.1" for h in _graph_hits(graph, ["inference"], 8)), "section match"
    # graceful degrade: empty/absent graph -> no hit, no crash.
    assert _graph_hits({}, ["spec-116"], 8) == [], "empty graph must yield no hit"
    assert _graph_hits({"nodes": [], "edges": []}, ["anything"], 8) == [], "empty graph"
    # a malformed edge (missing keys) must not crash the lane.
    assert _graph_hits({"edges": [{"from": "SPEC-116"}]}, ["spec-116"], 8), "malformed edge tolerated"
    # deterministic.
    assert _graph_hits(graph, ["spec-116"], 8) == _graph_hits(graph, ["spec-116"], 8)
    # limit bounds the number of graph hits.
    assert len(_graph_hits(graph, ["spec-116", "p4", "d050"], 1)) == 1, "limit bound"
    print("discovery graph-lane self-check: ok")


if __name__ == "__main__":
    _self_check()
