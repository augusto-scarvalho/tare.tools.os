"""Graphify graph normalization and partitioning helpers.

The harness accepts several lightweight graph.json shapes from Graphify and
Graphify-like tools. Keeping this adapter outside ``scripts/harness.py`` makes
the workflow planner smaller and gives adopters a clear extension point for
project-specific graph formats.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

PathFilter = Callable[[str, list[str] | None, list[str] | None], bool]
GroupFn = Callable[[str], str]


def _existing_path(root: Path, value: str) -> str | None:
    candidate = value.strip().lstrip("./")
    if candidate and (root / candidate).exists():
        return candidate
    return None


def collect_file_paths_from_graph(root: Path, obj: Any, found: set[str] | None = None) -> set[str]:
    """Recursively collect repository-relative file paths from arbitrary graph payloads."""
    if found is None:
        found = set()
    if isinstance(obj, str):
        candidate = obj.strip().lstrip("./")
        if candidate and ("/" in candidate or "." in Path(candidate).name) and (root / candidate).exists():
            found.add(candidate)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and ("/" in value or "." in Path(value).name):
                candidate = _existing_path(root, value)
                if candidate:
                    found.add(candidate)
            elif str(key).lower() in {"file", "path", "filepath", "file_path", "source", "target"} and isinstance(value, str):
                candidate = _existing_path(root, value)
                if candidate:
                    found.add(candidate)
            else:
                collect_file_paths_from_graph(root, value, found)
    elif isinstance(obj, list):
        for item in obj:
            collect_file_paths_from_graph(root, item, found)
    return found


def graph_node_path(root: Path, node: Any) -> str | None:
    """Extract a repository path from a graph node/string when possible."""
    if isinstance(node, str):
        return _existing_path(root, node)
    if not isinstance(node, dict):
        return None
    for key in ["path", "file", "filepath", "file_path", "id", "name", "label"]:
        value = node.get(key)
        if isinstance(value, str):
            candidate = _existing_path(root, value)
            if candidate:
                return candidate
    return None


def graph_node_community(node: Any) -> str | None:
    """Extract Graphify community/module labels from common node metadata keys."""
    if not isinstance(node, dict):
        return None
    for key in ["community", "communityId", "community_id", "cluster", "clusterId", "cluster_id", "module", "package", "group"]:
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def normalize_graphify_graph(graph: Any) -> dict[str, Any]:
    """Normalize common Graphify graph shapes into a compact adapter report."""
    if not isinstance(graph, dict):
        return {"adapter": "unknown", "normalizationStatus": "unsupported", "nodesKey": None, "edgesKey": None, "communitiesKey": None, "nodeCount": 0, "edgeCount": 0}
    nodes_key = next((k for k in ["nodes", "vertices"] if isinstance(graph.get(k), (list, dict))), None)
    edges_key = next((k for k in ["edges", "links"] if isinstance(graph.get(k), list)), None)
    communities_key = "communities" if isinstance(graph.get("communities"), (list, dict)) else None
    if communities_key:
        adapter = "graphify-communities"
    elif nodes_key and edges_key:
        adapter = "graphify-nodes-edges"
    elif nodes_key:
        adapter = "graphify-nodes"
    elif edges_key:
        adapter = "graphify-edges"
    else:
        adapter = "graphify-path-scan-fallback"
    nodes = graph.get(nodes_key) if nodes_key else []
    edges = graph.get(edges_key) if edges_key else []
    node_count = len(nodes) if isinstance(nodes, (list, dict)) else 0
    edge_count = len(edges) if isinstance(edges, list) else 0
    return {
        "adapter": adapter,
        "normalizerVersion": "1.0",
        "normalizationStatus": "normalized" if adapter != "graphify-path-scan-fallback" else "fallback",
        "nodesKey": nodes_key,
        "edgesKey": edges_key,
        "communitiesKey": communities_key,
        "nodeCount": node_count,
        "edgeCount": edge_count,
    }


def group_graphify_paths(
    root: Path,
    graph: Any,
    *,
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    path_is_excluded: PathFilter,
    top_level_group: GroupFn,
) -> tuple[dict[str, list[str]], str, list[str], dict[str, Any]]:
    """Group graph paths into worker shards and detect central context nodes."""
    normalized = normalize_graphify_graph(graph)
    grouped: dict[str, list[str]] = {}
    actual = "fallback-roots"

    communities = graph.get("communities") if isinstance(graph, dict) else None
    if isinstance(communities, dict):
        for name, value in communities.items():
            paths = sorted(p for p in collect_file_paths_from_graph(root, value) if not path_is_excluded(p, includes, excludes))
            if paths:
                grouped[f"community:{name}"] = paths
        if grouped:
            actual = "graphify-communities"
    elif isinstance(communities, list):
        for idx, value in enumerate(communities, start=1):
            label = str(value.get("id") or value.get("name") or idx) if isinstance(value, dict) else str(idx)
            paths = sorted(p for p in collect_file_paths_from_graph(root, value) if not path_is_excluded(p, includes, excludes))
            if paths:
                grouped[f"community:{label}"] = paths
        if grouped:
            actual = "graphify-communities"

    if not grouped and isinstance(graph, dict):
        nodes = graph.get("nodes") or graph.get("vertices") or []
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        if isinstance(nodes, list):
            for node in nodes:
                path = graph_node_path(root, node)
                if not path or path_is_excluded(path, includes, excludes):
                    continue
                community = graph_node_community(node)
                grouped.setdefault(f"community:{community}" if community else top_level_group(path), []).append(path)
            if grouped:
                actual = "graphify-communities" if any(str(k).startswith("community:") for k in grouped) else "graphify-modules"

    if not grouped:
        paths = sorted(p for p in collect_file_paths_from_graph(root, graph) if not path_is_excluded(p, includes, excludes))
        for path in paths:
            grouped.setdefault(top_level_group(path), []).append(path)
        if grouped:
            actual = "graphify-roots-fallback"

    degree: dict[str, int] = {}
    if isinstance(graph, dict):
        edges = graph.get("edges") or graph.get("links") or []
        if isinstance(edges, list):
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                for key in ["source", "target", "from", "to"]:
                    raw = edge.get(key)
                    path = graph_node_path(root, raw) if isinstance(raw, dict) else _existing_path(root, str(raw or ""))
                    if path:
                        degree[path] = degree.get(path, 0) + 1
    god_nodes = sorted([p for p, count in degree.items() if count >= 5], key=lambda p: (-degree[p], p))[:25]
    return grouped, actual, god_nodes, normalized
