#!/usr/bin/env python3
"""graph-provider-abstraction (B1) — graphify becomes ONE provider.

The artifact contract IS the interface: everything downstream (doc-find,
symbols, workflow partitioning, the freshness gates, the panel) consumes
files under the subject's `graphify-out/` in shapes `graphify_adapter`
already normalizes. This registry owns WHO produces those artifacts:

  * `graphify-code-ast` — the built-in stdlib AST builder (default);
  * config-declared EXTERNAL providers (`knowledgeGraph.providers.<name>`,
    `{"command": [...]}`) — run bounded via the processes mediation layer
    with the output dir appended as the final argv element; the emitted
    `graph.json` must normalize (`normalizationStatus != "unsupported"`).

Selection: `knowledgeGraph.provider` in `.harness/project.json` (per-target
override: the same dotted key inside `target.json`). FALLBACK is
deterministic and never silent: a failing/unusable external provider falls
back to the built-in and the build stats carry `{fellBackTo, reason}`.
Providers are declared only in tracked config (code review) — no plugin
loading, no entry points, and the GUI (B3, OPEN) may only select among
registered names.

Every successful build stamps `stats.generatedAt` into `graph.json`
(graphs-metrics groundwork). Self-check:
`python scripts/harness_lib/graph_providers.py`.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import graphify_adapter, graphify_code_ast, processes  # noqa: E402
from harness_lib.common import HarnessError, ROOT, read_json, write_json  # noqa: E402

BUILTIN = "graphify-code-ast"
EXTERNAL_TIMEOUT_S = 600


def _kg(root: Path) -> dict[str, Any]:
    project = read_json(Path(root) / ".harness" / "project.json", {}) or {}
    kg = project.get("knowledgeGraph")
    return kg if isinstance(kg, dict) else {}


def registry(root: Path | str) -> dict[str, dict[str, Any]]:
    """Provider name → descriptor. The built-in is always present; external
    entries come only from tracked config (reviewable, never GUI-created)."""
    out: dict[str, dict[str, Any]] = {
        BUILTIN: {"kind": "builtin", "available": True,
                  "detail": "stdlib ast builder (graphify_code_ast)"}}
    for name, entry in (_kg(Path(root)).get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        out[str(name)] = {"kind": "external",
                          "available": bool(command),
                          "command": command,
                          "detail": "configured command" if command
                                    else "missing command (unavailable)"}
    return out


def active_name(root: Path | str, target_entry: dict[str, Any] | None = None) -> str:
    """Selected provider: target override > project.json > built-in."""
    if isinstance(target_entry, dict):
        override = (target_entry.get("knowledgeGraph") or {}).get("provider")
        if override:
            return str(override)
    return str(_kg(Path(root)).get("provider") or BUILTIN)


def _stamp(out_dir: Path) -> str:
    """Add stats.generatedAt to graph.json (metrics groundwork); tolerant."""
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    graph_path = out_dir / "graph.json"
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph.setdefault("stats", {})["generatedAt"] = stamp
        graph_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False),
                              encoding="utf-8", newline="\n")
    except Exception:
        pass
    return stamp


def _build_builtin(root: Path, out_dir: Path, max_files: int) -> dict[str, Any]:
    stats = graphify_code_ast.build_graphify_code_ast(root, output_dir=out_dir,
                                                      max_files=max_files)
    return {"provider": BUILTIN, **stats}


def _build_external(root: Path, out_dir: Path, name: str,
                    command: list[Any]) -> dict[str, Any] | str:
    """Run the configured provider; return stats, or a REASON string on any
    failure (the caller falls back deterministically)."""
    try:
        proc = processes.run_quiet([*(str(c) for c in command), str(out_dir)],
                                   timeout=EXTERNAL_TIMEOUT_S, cwd=str(root),
                                   capture_output=True, text=True)
    except Exception as exc:
        return f"spawn failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {(proc.stderr or proc.stdout or '')[:200]}"
    graph_path = out_dir / "graph.json"
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"graph.json unreadable: {type(exc).__name__}"
    report = graphify_adapter.normalize_graphify_graph(graph)
    if report.get("normalizationStatus") == "unsupported":
        return "graph.json shape unsupported by the adapter"
    return {"provider": name, "graphPath": str(graph_path),
            "nodeCount": report.get("nodeCount"), "edgeCount": report.get("edgeCount"),
            "adapter": report.get("adapter")}


def build(root: Path | str, *, output_dir: Path | None = None,
          max_files: int = 1000,
          target_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the subject's graph artifacts with the ACTIVE provider; a broken
    external provider falls back to the built-in, reported never silent."""
    root = Path(root)
    out_dir = Path(output_dir) if output_dir else root / "graphify-out"
    name = active_name(root, target_entry)
    reg = registry(root)
    entry = reg.get(name)
    if name != BUILTIN and entry and entry.get("kind") == "external" and entry.get("command"):
        result = _build_external(root, out_dir, name, entry["command"])
        if isinstance(result, dict):
            result["generatedAt"] = _stamp(out_dir)
            return result
        stats = _build_builtin(root, out_dir, max_files)
        stats.update({"fellBackTo": BUILTIN, "provider": name, "reason": result,
                      "generatedAt": _stamp(out_dir)})
        return stats
    if name != BUILTIN:  # selected but unknown/unavailable → fallback, said out loud
        stats = _build_builtin(root, out_dir, max_files)
        stats.update({"fellBackTo": BUILTIN, "provider": name,
                      "reason": "provider not registered or unavailable",
                      "generatedAt": _stamp(out_dir)})
        return stats
    stats = _build_builtin(root, out_dir, max_files)
    stats["generatedAt"] = _stamp(out_dir)
    return stats


def staleness(source_root: Path | str, graph_path: Path | str) -> dict[str, Any]:
    """Freshness for the gates, the `graph metrics` CLI and the Graphs screen.

    This DELEGATES to `graphify_code_ast.freshness` — the one computation
    `doctor` and `graph-status` already answer with. It used to be a second,
    independent rule (stale iff graph.json's mtime predates the newest .py),
    and the two disagreed exactly where graph-refresh-decoupled.md rule 6 says
    it matters: touching a file without changing it, and reverting an edit
    back to identical bytes, both read STALE by clock and FRESH by content.
    Measured 2026-07-24 on the same graph: `doctor` said OK while the Graphs
    screen showed a red STALE tile after any checkout / branch switch /
    disposable worktree — rule 6's "an alarm nobody trusts is a disabled
    alarm" arriving through the GUI. The clock rule also scanned a DIFFERENT
    file set than the builder (`_SKIP_PARTS` was a strict subset of
    `DEFAULT_EXCLUDE_DIRS`), so it could cry stale over a .py that never
    enters the graph at all.

    Any doubt still rebuilds: no graph, an unkeyed graph (built before content
    keying) and an unreadable input all report stale, exactly as before. The
    verdict only gets QUIETER, never blinder — a real byte change is stale
    under both rules.
    """
    fresh = graphify_code_ast.freshness(Path(source_root), output_dir=Path(graph_path).parent)
    state = fresh["state"]
    if state == "fresh":
        return {"stale": False, "reason": None, "state": state}
    # "graph.json missing" is load-bearing: the Graphs screen compares this
    # exact string to tell "no graph built" from "a real graph, empty table".
    reason = "graph.json missing" if state == "no_graph" else fresh["detail"]
    return {"stale": True, "reason": reason, "state": state}


def metrics(root: Path | str, out_dir: Path | None = None) -> dict[str, Any]:
    """graphs-metrics (B2): one deterministic snapshot per subject — structure
    stats, freshness (the shared gate rule), artifact sizes/mtimes, consumption
    aggregates from the enrichment/report files, active provider. Pure reads."""
    root = Path(root)
    out = Path(out_dir) if out_dir else root / "graphify-out"
    graph_path = out / "graph.json"
    graph = read_json(graph_path, None) or {}
    artifacts: dict[str, Any] = {}
    for name in ("graph.json", "symbols.json", "modules.json", "GRAPH_REPORT.md",
                 "docs-enrichment.json", "discover-report.json"):
        p = out / name
        try:
            st = p.stat()
            artifacts[name] = {"bytes": st.st_size,
                               "mtime": dt.datetime.fromtimestamp(
                                   st.st_mtime).astimezone().isoformat(timespec="seconds")}
        except OSError:
            artifacts[name] = None
    enrichment = (read_json(out / "docs-enrichment.json", None) or {}).get("files") or {}
    tokens_in = sum(int(e.get("input_tokens") or 0) for e in enrichment.values()
                    if isinstance(e, dict))
    tokens_out = sum(int(e.get("output_tokens") or 0) for e in enrichment.values()
                     if isinstance(e, dict))
    report = read_json(out / "discover-report.json", None) or {}
    by_status: dict[str, int] = {}
    for r in report.get("results") or []:
        if isinstance(r, dict):
            by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
    return {
        "provider": active_name(root),
        "stats": graph.get("stats") or {},
        "freshness": staleness(root, graph_path),
        "artifacts": artifacts,
        "consumption": {"enrichedFiles": len(enrichment),
                        "inputTokens": tokens_in, "outputTokens": tokens_out,
                        "lastDiscoverByStatus": by_status,
                        "providerGates": report.get("providerGates")},
    }


def set_provider(root: Path | str, name: str) -> None:
    """`graph set <name>`: validated write of knowledgeGraph.provider."""
    root = Path(root)
    if name not in registry(root):
        raise HarnessError(
            f"unknown graph provider: {name}\n"
            f"fix: one of {', '.join(sorted(registry(root)))} (external providers "
            "are declared in .harness/project.json knowledgeGraph.providers)")
    project_path = root / ".harness" / "project.json"
    project = read_json(project_path, {}) or {}
    project.setdefault("knowledgeGraph", {})["provider"] = name
    write_json(project_path, project)


def cmd_graph(args) -> int:
    """`harness.py graph providers|set <name>|metrics [--target N] [--json]`."""
    action = getattr(args, "action", "providers") or "providers"
    if action == "set":
        if not getattr(args, "name", None):
            raise HarnessError("usage: graph set <provider-name>")
        set_provider(ROOT, args.name)
        print(f"active graph provider: {args.name}")
        return 0
    if action == "metrics":
        root, out_dir = ROOT, None
        target = getattr(args, "target", None)
        if target:
            from harness_lib import targets as targets_lib
            entry = targets_lib.resolve(target)
            root = targets_lib.target_root(entry)
            out_dir = targets_lib.out_dir(target)
        data = metrics(root, out_dir)
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0
        s, f, c = data["stats"], data["freshness"], data["consumption"]
        print(f"provider: {data['provider']}")
        print(f"structure: files={s.get('filesParsed')} edges={s.get('edges')} "
              f"errors={s.get('parseErrors')} builtIn={s.get('durationS')}s "
              f"at={s.get('generatedAt') or '(pre-stamp build)'}")
        print(f"freshness: {'STALE — ' + str(f['reason']) if f['stale'] else 'fresh'}")
        print(f"consumption: {c['enrichedFiles']} enriched files, "
              f"{c['inputTokens']}in/{c['outputTokens']}out tokens"
              + (f", last discover {c['lastDiscoverByStatus']}" if c["lastDiscoverByStatus"] else ""))
        for name, meta in data["artifacts"].items():
            print(f"  {name:<24} {'-' if meta is None else str(meta['bytes']) + ' B  ' + meta['mtime']}")
        return 0
    active = active_name(ROOT)
    for name, entry in sorted(registry(ROOT).items()):
        mark = "*" if name == active else " "
        print(f"{mark} {name:<24} {entry['kind']:<9} "
              f"{'available' if entry['available'] else 'UNAVAILABLE'}  {entry['detail']}")
    print("\n* = active (knowledgeGraph.provider; per-target override in target.json)")
    return 0


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".harness").mkdir()
        (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (root / ".harness" / "project.json").write_text(json.dumps({
            "knowledgeGraph": {"provider": "graphify-code-ast"}}), encoding="utf-8")
        stats = build(root)
        assert stats["provider"] == BUILTIN and stats.get("generatedAt"), stats
        graph = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
        assert graph["stats"].get("generatedAt"), graph["stats"]
        # unknown selected provider → deterministic, loud fallback.
        (root / ".harness" / "project.json").write_text(json.dumps({
            "knowledgeGraph": {"provider": "ghost"}}), encoding="utf-8")
        stats = build(root)
        assert stats["fellBackTo"] == BUILTIN and stats["provider"] == "ghost", stats
        try:
            set_provider(root, "nope")
            raise AssertionError("unknown provider accepted")
        except HarnessError as exc:
            assert "unknown graph provider" in str(exc)
        set_provider(root, BUILTIN)
        assert active_name(root) == BUILTIN
        # per-target override wins.
        assert active_name(root, {"knowledgeGraph": {"provider": "x"}}) == "x"
    print("graph_providers self-check: ok")


if __name__ == "__main__":
    _demo()
