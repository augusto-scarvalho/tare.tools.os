#!/usr/bin/env python3
"""dep-graph (backlog-groom P2) — a DERIVED provenance+dependency graph.

Recomputed fresh on every call (like tasks_board's board): nothing is stored, no
second write path, nothing auto-links or auto-promotes. It unifies two edge
families over signals the repo ALREADY keeps:

  * dependency edges — REUSED from `tasks_board`'s row collector + `_parse_deps`
    (backlog-groom P1); an explicit `depends-on:`/`blocks:`/`relates-to:`/
    `duplicates:` tag is self-evident evidence, so these ship VERIFIED;
  * provenance edges — co-occurrence of reference tokens (EXP-/SPEC-/D0xx/T-…)
    inside a durable repo artifact (a DECISIONS.md entry, a git commit message =
    provenanceTag `[repo]`) or inside one backlog item's own text (a softer pure
    co-occurrence heuristic = provenanceTag `[judgment]`). All heuristic, so they
    ship PROPOSED and await the P5/owner audit that promotes them.

EVERY edge is a CLAIM record, never a fact — the measure-honesty invariant made
structural. There is NO `fact` field. Downstream reads VERIFIED by default
(`verified_edges` / `provenance --verified`).

EXP-BDG-2 (incremental derive, proved by the self-check): each item is
content-hashed; an unchanged whole-pass aggregate hash short-circuits the pass;
when one item changes only its edges are recomputed (dirty-set, one-hop) and the
clean items' edges are reused by identity. The self-check ASSERTS a dirty-set
recompute EQUALS a full recompute (the R-2 "looks-fresh-but-stale" guard),
plus determinism and the edge-as-claim shape.

Query: `harness.py provenance <token>` — a BOUNDED slice (what cites this token /
what this token cites), never the whole graph. Compact TSV under
HARNESS_AGENT_OUTPUT=compact (TE.5), like `tasks list`.

Self-check: `python scripts/harness_lib/dep_graph.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import common, processes, tasks_board  # noqa: E402
from harness_lib.common import HarnessError  # noqa: E402

# Reference-token family (provenance co-occurrence keys). `\b`-guarded so a token
# is never matched as a substring of a larger id.
_TOKEN = re.compile(r"\b(?:EXP-\d+|SPEC-\d+|D\d{2,3}|T-[A-Z0-9-]+)\b")

# DECISIONS.md is durable + gitignored. The brief names `codex/DECISIONS.md`; in
# this repo that path is an adapter shim redirecting to the canonical file, so we
# read BOTH (whichever exist) and tolerate all of them being absent.
DECISIONS_RELS = ("codex/DECISIONS.md", ".harness/context/DECISIONS.md")
GIT_LOG_MAX = 400              # commit refs are bounded (read-only `git log`)
MAX_TOKENS_PER_ENTRY = 12      # ponytail: cap per-entry pair combinatorics (n<=12 -> <=66 pairs)
SLICE_CAP = 200                # provenance slice bound — never dumps the whole graph
STATUS = ("PROPOSED", "VERIFIED", "REJECTED")

# _parse_deps' map keys -> claim edge type (the tag IS the edge kind).
_DEP_EDGE_TYPE = {"dependsOn": "depends-on", "blocks": "blocks",
                  "relatesTo": "relates-to", "duplicates": "duplicates"}


def _claim(etype: str, frm: Any, to: Any, *, confidence: float, status: str,
           source: str, tag: str, inferred_by: str) -> dict[str, Any]:
    """One edge-as-claim record. NO `fact` field by design. `lastVerifiedAt` is
    the slot a later P5 audit stamps — null in this derive-only pass (which keeps
    the derive deterministic; nothing here is wall-clock)."""
    return {"type": etype, "from": frm, "to": to,
            "confidence": confidence, "status": status,
            "source": source, "provenanceTag": tag,
            "inferredBy": inferred_by, "lastVerifiedAt": None}


def _item_edges(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Edges OWNED by a single item (so the dirty-set recompute is one-hop and
    equals a full recompute): its explicit dependency tags (VERIFIED, `[repo]`)
    plus a judgment co-occurrence edge to every reference token in its own text
    (PROPOSED, `[judgment]`). Never-crash-per-row: a bad row yields no edges."""
    rid = row.get("id")
    if not rid:
        return []
    src = row.get("source") or tasks_board.BACKLOG_REL
    edges: list[dict[str, Any]] = []
    deps = row.get("deps") or {}
    for key, etype in _DEP_EDGE_TYPE.items():
        for tgt in dict.fromkeys(deps.get(key) or []):
            if tgt and tgt != rid:
                edges.append(_claim(etype, rid, tgt, confidence=1.0,
                                    status="VERIFIED", source=src,
                                    tag="[repo]", inferred_by="dep-tag"))
    text = f"{rid} {row.get('title') or ''} {row.get('section') or ''}"
    for tok in dict.fromkeys(_TOKEN.findall(text)):
        if tok != rid:
            edges.append(_claim("relates-to", rid, tok, confidence=0.3,
                                status="PROPOSED", source=src,
                                tag="[judgment]", inferred_by="cooccurrence:item"))
    return edges


def _decisions_entries(root: Path) -> list[tuple[str, str]]:
    """(block, source-rel) for every DECISIONS.md that exists (blank-line split),
    tolerant of all being absent. PER-BLOCK source so a co-occurrence edge is
    attributed to the file it ACTUALLY came from -- never the first/shim file (the
    repo's codex/DECISIONS.md is a token-less redirect shim; the real content is
    .harness/context/DECISIONS.md, and its edges must cite it, not the shim)."""
    out: list[tuple[str, str]] = []
    for rel in DECISIONS_RELS:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for b in re.split(r"\n\s*\n", text):
            if b.strip():
                out.append((b.strip(), rel))
    return out


def _git_entries(root: Path, commit_msgs: list[str] | None = None) -> list[str]:
    """Recent commit messages (subject+body), bounded. Read-only; a missing git /
    non-repo degrades to []."""
    if commit_msgs is not None:
        return list(commit_msgs)
    # routed through the processes.py mediation layer (hidden console + bounded),
    # not a raw subprocess -> the no-raw-subprocess ratchet stays green.
    try:
        out = processes.run_quiet(
            ["git", "log", f"-n{GIT_LOG_MAX}", "--format=%s%n%b%x1e"],
            timeout=15, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [m.strip() for m in (out.stdout or "").split("\x1e") if m.strip()]


def _corpus_edges(root: Path, *, decisions_entries: list[str] | None = None,
                  commit_msgs: list[str] | None = None) -> list[dict[str, Any]]:
    """Provenance edges from token co-occurrence inside durable artifacts
    (DECISIONS entries + git commits), all `[repo]` + PROPOSED. Confidence rises
    with co-occurrence count but never reaches VERIFIED's 1.0 (heuristic honesty).
    Corpus-owned, not item-owned: recomputed only when the corpus changes."""
    from collections import Counter

    # (source-label, inferred-by, block-text). decisions blocks carry their OWN
    # file as source (honesty: cite where the tokens co-occurred); an injected
    # decisions_entries list (hermetic self-check) is labeled generically.
    blocks: list[tuple[str, str, str]] = []
    if decisions_entries is not None:
        blocks += [("DECISIONS.md", "cooccurrence:decisions", b) for b in decisions_entries]
    else:
        blocks += [(rel, "cooccurrence:decisions", b) for b, rel in _decisions_entries(root)]
    blocks += [("git-log", "cooccurrence:git", b) for b in _git_entries(root, commit_msgs)]

    counts: Counter[tuple[str, str, str, str]] = Counter()
    for source, inferred, block in blocks:
        toks = sorted(set(_TOKEN.findall(block)))[:MAX_TOKENS_PER_ENTRY]
        for a, b in combinations(toks, 2):
            counts[(source, inferred, a, b)] += 1
    edges: list[dict[str, Any]] = []
    for (source, inferred, a, b), c in counts.items():
        edges.append(_claim("relates-to", a, b,
                            confidence=round(1 - 1.0 / (1 + c), 4),
                            status="PROPOSED", source=source,
                            tag="[repo]", inferred_by=inferred))
    return edges


def _dedup_sort(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical claims (keep the higher confidence) and sort
    deterministically so two derives are byte-identical."""
    seen: dict[tuple, dict[str, Any]] = {}
    for e in edges:
        k = (e["type"], e["from"], e["to"], e["status"], e["source"],
             e["provenanceTag"], e["inferredBy"])
        if k not in seen or e["confidence"] > seen[k]["confidence"]:
            seen[k] = e
    return sorted(seen.values(),
                  key=lambda e: (e["type"], str(e["from"]), str(e["to"]),
                                 e["source"], e["inferredBy"], e["status"]))


def _nodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes, seen = [], set()
    for r in rows:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        nodes.append({"id": rid, "section": r.get("section"), "source": r.get("source")})
    return sorted(nodes, key=lambda n: str(n["id"]))


def _assemble(rows: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    edges = _dedup_sort(edges)
    nodes = _nodes(rows)
    verified = sum(1 for e in edges if e["status"] == "VERIFIED")
    return {"derived": True, "nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges),
                       "verified": verified, "proposed": len(edges) - verified}}


def derive_graph(root: Path | str, *, snapshot: dict[str, Any] | None = None,
                 decisions_entries: list[str] | None = None,
                 commit_msgs: list[str] | None = None) -> dict[str, Any]:
    """The full derive: {nodes, edges, counts}, read-only, recomputed each call.
    Deterministic (no timestamps in the graph). `snapshot`/`decisions_entries`/
    `commit_msgs` are injectable for a hermetic self-check; absent, they read the
    live tasks board + DECISIONS.md + git."""
    root = Path(root)
    snap = snapshot if snapshot is not None else tasks_board.tasks_snapshot(root)
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    edges: list[dict[str, Any]] = []
    for r in rows:
        edges.extend(_item_edges(r))
    edges.extend(_corpus_edges(root, decisions_entries=decisions_entries,
                               commit_msgs=commit_msgs))
    return _assemble(rows, edges)


def verified_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Downstream default read: VERIFIED claims only (the explicit-tag edges)."""
    return [e for e in graph.get("edges", []) if e["status"] == "VERIFIED"]


# tag-laundering guard (backlog-groom P5): the edge-as-claim honesty invariants made
# checkable. Runs in the self-check as a PERMANENT guard (proof that derive_graph
# never emits a laundered tag) and is exposed for defensive reuse anywhere an
# untrusted graph enters. ponytail: a [judgment] can only ever be PROPOSED today --
# no PROPOSED->VERIFIED promotion path exists (that audit gate is the DEFERRED P5
# piece, ahead of its consumer). When one lands, relax to allow VERIFIED [judgment]
# iff lastVerifiedAt is stamped by the audit.
_KNOWN_TAGS = {"[repo]", "[judgment]"}


def validate_claim_tags(graph: dict[str, Any]) -> list[str]:
    """Return tag/status honesty violations in a graph (empty == honest). Guards
    against tag-laundering — a claim wearing more trust than its provenance earns:

      * unknown provenanceTag or status  -> a tag/status that cannot ground;
      * [repo] + VERIFIED, empty source  -> a "verified fact" with nothing to cite
                                            (a VERIFIED edge's tag IS its evidence);
      * [judgment] marked VERIFIED       -> a heuristic guess laundered into a
                                            VERIFIED fact (no promotion path today).
    """
    violations: list[str] = []
    for i, e in enumerate(graph.get("edges", [])):
        where = f"edge[{i}] {e.get('type')} {e.get('from')}->{e.get('to')}"
        tag, status = e.get("provenanceTag"), e.get("status")
        if tag not in _KNOWN_TAGS:
            violations.append(f"{where}: unknown provenanceTag {tag!r}")
        if status not in STATUS:
            violations.append(f"{where}: unknown status {status!r}")
        if tag == "[repo]" and status == "VERIFIED" and not str(e.get("source") or "").strip():
            violations.append(f"{where}: [repo]+VERIFIED with empty source (unciteable fact)")
        if tag == "[judgment]" and status == "VERIFIED":
            violations.append(f"{where}: [judgment] marked VERIFIED (laundered guess)")
    return violations


def _item_hash(row: dict[str, Any]) -> str:
    key = json.dumps({"id": row.get("id"), "title": row.get("title"),
                      "section": row.get("section"), "source": row.get("source"),
                      "deps": row.get("deps")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _agg_hash(item_hashes: dict[Any, str], corpus_hash: str) -> str:
    h = hashlib.sha1()
    for rid in sorted(item_hashes, key=repr):
        h.update(repr(rid).encode("utf-8"))
        h.update(item_hashes[rid].encode("utf-8"))
    h.update(corpus_hash.encode("utf-8"))
    return h.hexdigest()


def derive_incremental(root: Path | str, prev: dict[str, Any] | None = None, *,
                       snapshot: dict[str, Any] | None = None,
                       decisions_entries: list[str] | None = None,
                       commit_msgs: list[str] | None = None
                       ) -> tuple[dict[str, Any], dict[str, Any]]:
    """EXP-BDG-2 incremental derive → (graph, state). `state` carries
    {aggregateHash, itemHashes, itemEdges, graph} for the NEXT call — it is an
    in-memory proof object, never persisted (derive-only, no store).

      * unchanged aggregate hash  -> whole-pass short-circuit (returns prev graph);
      * else per-item content hash -> recompute only DIRTY items' edges (one-hop),
        reuse clean items' edges by identity, always recompute corpus edges.

    Because each item's edges depend only on that item, the reuse+recompute union
    equals a full derive — the self-check proves it (dirty == full)."""
    root = Path(root)
    snap = snapshot if snapshot is not None else tasks_board.tasks_snapshot(root)
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    item_hashes: dict[Any, str] = {}
    ordered_rows: list[dict[str, Any]] = []
    for r in rows:
        rid = r.get("id")
        if not rid or rid in item_hashes:
            continue
        item_hashes[rid] = _item_hash(r)
        ordered_rows.append(r)
    corpus_edges = _corpus_edges(root, decisions_entries=decisions_entries,
                                 commit_msgs=commit_msgs)
    corpus_hash = hashlib.sha1(json.dumps(
        sorted((e["type"], e["from"], e["to"], e["source"], e["inferredBy"],
                e["confidence"]) for e in corpus_edges),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    agg = _agg_hash(item_hashes, corpus_hash)
    if prev and prev.get("aggregateHash") == agg:
        return prev["graph"], prev  # whole-pass short-circuit

    prev_hashes = (prev or {}).get("itemHashes") or {}
    prev_edges = (prev or {}).get("itemEdges") or {}
    item_edges: dict[Any, list[dict[str, Any]]] = {}
    for r in ordered_rows:
        rid = r.get("id")
        if prev_hashes.get(rid) == item_hashes.get(rid) and rid in prev_edges:
            item_edges[rid] = prev_edges[rid]      # clean: reuse (dirty-set skip)
        else:
            item_edges[rid] = _item_edges(r)       # dirty: one-hop recompute
    all_edges = [e for es in item_edges.values() for e in es] + corpus_edges
    graph = _assemble(ordered_rows, all_edges)
    state = {"aggregateHash": agg, "itemHashes": item_hashes,
             "itemEdges": item_edges, "graph": graph}
    return graph, state


def ripple(graph: dict[str, Any], changed_id: Any, *, max_depth: int = 3) -> dict[str, Any]:
    """backlog-groom P3a — bounded-hop BFS impact set: which items are AFFECTED
    (need revision) when `changed_id` changes. Measure-only — computes the set,
    changes NO status, auto-acts on nothing (the compiler-error-cascade analogy:
    propagate to the break-sites, don't render the graph).

    Impact direction, over VERIFIED order-edges ONLY (the trustworthy default):
      * items that DEPEND ON x  -> `depends-on` edges with to==x   (from is affected);
      * items x BLOCKS          -> `blocks` edges with from==x     (to is affected).
    BFS this transitively to `max_depth`. Each affected item carries `hops` (BFS
    distance) and `minConfidence` (the MIN confidence along its chain — propagated,
    never laundered upward: a hop-1 item inherits the edge conf, a hop-k item the
    path min). If the frontier still has unexpanded nodes past `max_depth`, report
    `truncated: N` (honest "N more hops"). Never-crash: ghost refs (an affected id
    that is not a node) are kept as-is, cycles terminate (visited set), an empty
    graph yields an empty affected set. Deterministic (edges are already sorted)."""
    adj: dict[Any, list[tuple[Any, Any]]] = {}
    for e in graph.get("edges", []):
        if e.get("status") != "VERIFIED":
            continue
        etype = e.get("type")
        if etype == "depends-on":            # from depends on to -> to changing affects from
            adj.setdefault(e.get("to"), []).append((e.get("from"), e.get("confidence")))
        elif etype == "blocks":              # to needs from first -> from changing affects to
            adj.setdefault(e.get("from"), []).append((e.get("to"), e.get("confidence")))

    affected: dict[Any, dict[str, Any]] = {}
    visited = {changed_id}
    frontier: list[tuple[Any, float]] = [(changed_id, float("inf"))]
    hop = 0
    while frontier and hop < max_depth:
        hop += 1
        nxt: list[tuple[Any, float]] = []
        for cur, cur_min in frontier:
            for nb, conf in adj.get(cur, ()):
                if nb in visited:
                    continue                 # cycle / already-closer node: terminate
                visited.add(nb)
                path_min = min(cur_min, conf)  # propagate the MIN, never launder up
                affected[nb] = {"id": nb, "hops": hop, "minConfidence": path_min}
                nxt.append((nb, path_min))
        frontier = nxt
    # honest truncation: frontier nodes at max_depth that still have unexpanded kids.
    truncated = sum(1 for cur, _ in frontier
                    if any(nb not in visited for nb, _ in adj.get(cur, ())))
    return {"changed": changed_id, "maxDepth": max_depth,
            "affected": sorted(affected.values(), key=lambda a: (a["hops"], str(a["id"]))),
            "truncated": truncated}


def cmd_provenance(args) -> int:
    """`provenance <token>` — bounded provenance/dependency slice for one token:
    what it cites (edges from it) and what cites it (edges to it). Never the whole
    graph. `--verified` restricts to the downstream default (VERIFIED-only).
    `--ripple` switches to the P3a impact set (who is affected if the token changes)."""
    token = (getattr(args, "token", None) or "").strip()
    if not token:
        raise HarnessError("usage: provenance <TOKEN>  (e.g. SPEC-116, EXP-25)")
    graph = derive_graph(common.ROOT)
    if getattr(args, "ripple", False):       # additive P3a branch — bare provenance is untouched
        rip = ripple(graph, token)
        if getattr(args, "json", False):
            common.emit(rip)
            return 0
        aff = rip["affected"]
        print(f"ripple {token} — affected {len(aff)} (<= {rip['maxDepth']} hops)"
              + (f", truncated {rip['truncated']} more" if rip["truncated"] else ""))
        for a in aff:
            print(f"  hop {a['hops']}  {str(a['id']):<18} minConf {a['minConfidence']}")
        return 0
    verified_only = bool(getattr(args, "verified", False))
    cites, cited_by = [], []
    for e in graph["edges"]:
        if verified_only and e["status"] != "VERIFIED":
            continue
        if e["from"] == token:
            cites.append(e)
        if e["to"] == token:
            cited_by.append(e)
    cites, cited_by = cites[:SLICE_CAP], cited_by[:SLICE_CAP]

    if common.agent_output_compact():
        rows = ([{"dir": "cites", **e} for e in cites]
                + [{"dir": "citedBy", **e} for e in cited_by])
        common.emit(rows, tsv=True)  # TE.5 compact path (dep_graph.py tsv opt-in)
        return 0
    payload = {"token": token, "verifiedOnly": verified_only,
               "cites": cites, "citedBy": cited_by,
               "counts": {"cites": len(cites), "citedBy": len(cited_by)}}
    if getattr(args, "json", False):
        common.emit(payload)
        return 0
    print(f"provenance {token} — cites {len(cites)}, citedBy {len(cited_by)}"
          + ("  (VERIFIED only)" if verified_only else ""))
    for label, rows in (("cites", cites), ("citedBy", cited_by)):
        for e in rows:
            other = e["to"] if label == "cites" else e["from"]
            print(f"  {label:<8} {e['status']:<8} {e['type']:<11} "
                  f"{str(other):<18} {e['provenanceTag']} {e['inferredBy']} "
                  f"(conf {e['confidence']})")
    return 0


def _demo() -> None:
    import tempfile

    root = Path(".")  # corpus is injected below; root is unused for the fixture
    rows0 = [
        {"id": "P2.1", "title": "provenance graph depends-on:P2.0 relates-to:EXP-25",
         "section": "Backlog groom", "source": "docs/IMPLEMENTATION_BACKLOG.md",
         "deps": tasks_board._parse_deps("depends-on:P2.0 relates-to:EXP-25")},
        {"id": "P2.0", "title": "dependency tags shipped", "section": "Backlog groom",
         "source": "docs/IMPLEMENTATION_BACKLOG.md", "deps": tasks_board._parse_deps("")},
        {"id": "EXP-25", "title": "PING probe touches SPEC-116", "section": "Experiments",
         "source": ".harness/state/tasks.json", "deps": tasks_board._parse_deps("")},
    ]
    snap0 = {"rows": rows0}
    dec = ["D033 promotes EXP-25 under SPEC-116 provenance work",
           "an unrelated note carrying no reference tokens at all"]

    g = derive_graph(root, snapshot=snap0, decisions_entries=dec, commit_msgs=[])
    edges = g["edges"]
    # edge-as-claim: exactly the 9 fields, NO `fact`, valid status.
    fields = {"type", "from", "to", "confidence", "status", "source",
              "provenanceTag", "inferredBy", "lastVerifiedAt"}
    for e in edges:
        assert set(e) == fields, set(e) ^ fields
        assert "fact" not in e
        assert e["status"] in STATUS, e
    # explicit dependency tag -> VERIFIED + [repo] + confidence 1.0.
    dep = [e for e in edges if e["type"] == "depends-on"
           and e["from"] == "P2.1" and e["to"] == "P2.0"]
    assert (dep and dep[0]["status"] == "VERIFIED"
            and dep[0]["provenanceTag"] == "[repo]" and dep[0]["confidence"] == 1.0), dep
    # explicit relates-to tag is ALSO VERIFIED (the tag is the evidence).
    assert any(e["type"] == "relates-to" and e["from"] == "P2.1" and e["to"] == "EXP-25"
               and e["inferredBy"] == "dep-tag" and e["status"] == "VERIFIED"
               for e in edges), edges
    # DECISIONS co-occurrence -> PROPOSED + [repo]; the EXP-25<->SPEC-116 pair is there.
    dec_e = [e for e in edges if e["inferredBy"] == "cooccurrence:decisions"]
    assert dec_e and all(e["status"] == "PROPOSED" and e["provenanceTag"] == "[repo]"
                         for e in dec_e), dec_e
    assert any({e["from"], e["to"]} == {"EXP-25", "SPEC-116"} for e in dec_e), dec_e
    # item text co-occurrence -> PROPOSED + [judgment].
    jud = [e for e in edges if e["provenanceTag"] == "[judgment]"]
    assert jud and all(e["status"] == "PROPOSED" for e in jud), jud
    assert any(e["from"] == "EXP-25" and e["to"] == "SPEC-116" for e in jud), jud
    # determinism: two derives are identical.
    assert derive_graph(root, snapshot=snap0, decisions_entries=dec, commit_msgs=[]) == g

    # EXP-BDG-2 incremental: full-agrees, short-circuit, then dirty == full (R-2).
    g0, st0 = derive_incremental(root, None, snapshot=snap0, decisions_entries=dec, commit_msgs=[])
    assert g0 == g, "incremental full != derive_graph"
    g_sc, _ = derive_incremental(root, st0, snapshot=snap0, decisions_entries=dec, commit_msgs=[])
    assert g_sc is st0["graph"], "unchanged inputs must short-circuit to the prior graph"

    rows1 = [dict(r) for r in rows0]
    rows1[2] = {**rows1[2], "title": "PING probe now cites SPEC-999 instead"}
    g_inc, st1 = derive_incremental(root, st0, snapshot={"rows": rows1},
                                    decisions_entries=dec, commit_msgs=[])
    assert g_inc is not st0["graph"], "a changed input must not short-circuit"
    assert st1["itemEdges"]["P2.1"] is st0["itemEdges"]["P2.1"], "clean item edges must be reused"
    assert st1["itemEdges"]["EXP-25"] is not st0["itemEdges"]["EXP-25"], "dirty item must recompute"
    g_ref = derive_graph(root, snapshot={"rows": rows1}, decisions_entries=dec, commit_msgs=[])
    assert g_inc == g_ref, "dirty-set recompute != full recompute (looks-fresh-but-stale, R-2)"
    exp_judg = {e["to"] for e in g_inc["edges"]
                if e["from"] == "EXP-25" and e["provenanceTag"] == "[judgment]"}
    assert "SPEC-999" in exp_judg and "SPEC-116" not in exp_judg, exp_judg

    # provenance-source honesty: a decisions edge cites the FILE it came from, not
    # the first/shim file. Two DECISIONS files under tmp -- a token-less shim (first
    # in DECISIONS_RELS) + a canonical file with the real pair -> edge cites canonical.
    with tempfile.TemporaryDirectory() as tmp:
        tmpp = Path(tmp)
        (tmpp / "codex").mkdir()
        (tmpp / "codex" / "DECISIONS.md").write_text(
            "redirect shim: canonical lives elsewhere, no reference tokens here\n",
            encoding="utf-8")
        (tmpp / ".harness" / "context").mkdir(parents=True)
        (tmpp / ".harness" / "context" / "DECISIONS.md").write_text(
            "D050 links EXP-99 to SPEC-42 in the canonical decisions file\n",
            encoding="utf-8")
        gsrc = derive_graph(tmpp, snapshot={"rows": []}, commit_msgs=[])
        dsrc = [e for e in gsrc["edges"] if e["inferredBy"] == "cooccurrence:decisions"]
        assert dsrc and all(e["source"] == ".harness/context/DECISIONS.md" for e in dsrc), dsrc
        assert any({e["from"], e["to"]} == {"EXP-99", "SPEC-42"} for e in dsrc), dsrc

    # graceful degrade: no items, no DECISIONS, no git -> empty, no crash.
    with tempfile.TemporaryDirectory() as tmp:
        empty = derive_graph(Path(tmp), snapshot={"rows": []})
        assert empty["edges"] == [] and empty["nodes"] == [], empty

    # --- ripple (P3a): bounded-hop BFS impact set over VERIFIED order-edges ---
    def _v(etype, frm, to, conf):  # a VERIFIED order-edge with a chosen confidence
        return _claim(etype, frm, to, confidence=conf, status="VERIFIED",
                      source="fixture", tag="[repo]", inferred_by="dep-tag")

    # chain A depends-on B, B depends-on C (all VERIFIED, distinct confidences so
    # the min propagation is observable) + a PROPOSED edge ripple MUST ignore.
    chain = {"nodes": [{"id": x} for x in ("A", "B", "C")],
             "edges": [_v("depends-on", "A", "B", 0.6), _v("depends-on", "B", "C", 0.8),
                       _claim("depends-on", "Z", "C", confidence=0.9, status="PROPOSED",
                              source="fixture", tag="[judgment]", inferred_by="cooccurrence:item")]}
    r = ripple(chain, "C")
    aff = {a["id"]: a for a in r["affected"]}
    assert set(aff) == {"A", "B"}, aff                       # Z is PROPOSED -> ignored
    assert aff["B"]["hops"] == 1 and aff["B"]["minConfidence"] == 0.8, aff["B"]  # inherits edge
    assert aff["A"]["hops"] == 2 and aff["A"]["minConfidence"] == 0.6, aff["A"]  # min(0.8,0.6)
    assert r["truncated"] == 0, r
    # max_depth=1 truncates: B reached (hop1), A not expanded past the bound.
    r1 = ripple(chain, "C", max_depth=1)
    assert {a["id"] for a in r1["affected"]} == {"B"} and r1["truncated"] >= 1, r1
    # blocks direction: X blocks Y (Y needs X first) -> changing X affects Y.
    blk = {"nodes": [{"id": "X"}, {"id": "Y"}], "edges": [_v("blocks", "X", "Y", 0.7)]}
    rb = ripple(blk, "X")
    assert [a["id"] for a in rb["affected"]] == ["Y"] and rb["affected"][0]["hops"] == 1, rb
    # cycle terminates via the visited set: A<->B, seed A not re-added.
    cyc = {"nodes": [{"id": "A"}, {"id": "B"}],
           "edges": [_v("depends-on", "A", "B", 1.0), _v("depends-on", "B", "A", 1.0)]}
    assert {a["id"] for a in ripple(cyc, "A")["affected"]} == {"B"}, ripple(cyc, "A")
    # ghost ref (affected id not a node) is kept, not a crash.
    ghost = {"nodes": [{"id": "C"}], "edges": [_v("depends-on", "GHOST", "C", 0.5)]}
    assert {a["id"] for a in ripple(ghost, "C")["affected"]} == {"GHOST"}, ripple(ghost, "C")
    # empty graph / unknown seed -> empty affected set, no crash.
    assert ripple({"nodes": [], "edges": []}, "nope")["affected"] == []
    # deterministic: two calls byte-identical.
    assert ripple(chain, "C") == ripple(chain, "C")

    # --- tag-laundering guard (P5): derive is honest by construction; forgery caught ---
    assert validate_claim_tags(g) == [], validate_claim_tags(g)  # the honest fixture
    # forge a laundered graph: a [judgment] worn as VERIFIED + a [repo]+VERIFIED with
    # no source. Both must be CAUGHT (non-empty violations) -> the guard has teeth.
    forged = {"edges": [
        _claim("relates-to", "X", "Y", confidence=0.3, status="VERIFIED",
               source="fixture", tag="[judgment]", inferred_by="cooccurrence:item"),
        _claim("depends-on", "P", "Q", confidence=1.0, status="VERIFIED",
               source="", tag="[repo]", inferred_by="dep-tag"),
    ]}
    fv = validate_claim_tags(forged)
    assert len(fv) >= 2, fv
    assert any("[judgment] marked VERIFIED" in m for m in fv), fv
    assert any("empty source" in m for m in fv), fv
    # honest partials pass: PROPOSED [judgment], PROPOSED [repo], VERIFIED [repo]+source.
    honest = {"edges": [
        _claim("relates-to", "A", "B", confidence=0.3, status="PROPOSED",
               source="backlog", tag="[judgment]", inferred_by="cooccurrence:item"),
        _claim("relates-to", "C", "D", confidence=0.5, status="PROPOSED",
               source="git-log", tag="[repo]", inferred_by="cooccurrence:git"),
        _claim("depends-on", "E", "F", confidence=1.0, status="VERIFIED",
               source="backlog", tag="[repo]", inferred_by="dep-tag"),
    ]}
    assert validate_claim_tags(honest) == [], validate_claim_tags(honest)

    print("dep_graph self-check: ok")


if __name__ == "__main__":
    _demo()
