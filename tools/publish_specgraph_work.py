"""Extract the authorized SpecGraph program without importing unrelated Work.

The source is an explicitly selected historical graph. Applying the additive
projection uses the Backlog owner's CAS transaction. This is publication of
recorded evidence, not a new completion judgment or execution permit.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess

PROGRAM_IDS = {"SPECGRAPH-COMPLETION-" + name for name in
               [*("C" + str(number) for number in range(9)), *("X" + str(number) for number in range(1, 6))]}
PROGRAM_IDS |= {"SPECGRAPH-COMPLETION-20260908", "SPECGRAPH-FOLLOWUP-GOVERNANCE-20260908",
                "SPECGRAPH-FOLLOWUP-HOST-ADOPTION-20260908"}


def project(base, source):
    selected = [node for node in source["nodes"] if node["id"] in PROGRAM_IDS]
    identities = {node["id"] for node in selected}
    if identities != PROGRAM_IDS or len(selected) != len(identities):
        raise ValueError("Expected the 15 program records and two explicit followups")
    edges = [edge for edge in source["edges"] if edge["from"] in identities or edge["to"] in identities]
    if any(not {edge["from"], edge["to"]} <= identities for edge in edges):
        raise ValueError("Selected program has an external edge; explicit scope review required")
    refs = {ref for item in selected + edges for ref in item.get("source_refs", [])}
    candidate = copy.deepcopy(base)
    candidate.pop("revision", None)
    nodes = {node["id"]: node for node in candidate["nodes"]}
    for node in selected:
        if node["id"] in nodes:
            if nodes[node["id"]] != node:
                raise ValueError("Existing selected Work differs; publication never overwrites progress")
        else:
            candidate["nodes"].append(copy.deepcopy(node))
    for ref in sorted(refs):
        incoming = source["sources"][ref]
        if ref in candidate["sources"] and candidate["sources"][ref] != incoming:
            raise ValueError("Source identity collision")
        candidate["sources"][ref] = copy.deepcopy(incoming)
    key = lambda edge: (edge["from"], edge["to"], edge["type"], edge.get("semantic"))
    existing = {key(edge): edge for edge in candidate["edges"]}
    for edge in edges:
        if key(edge) in existing:
            if existing[key(edge)] != edge:
                raise ValueError("Edge identity collision")
        else:
            candidate["edges"].append(copy.deepcopy(edge))
    return candidate, {"nodes": sorted(identities), "sources": sorted(refs), "edges": len(edges)}


def verify_sources(graph, mapping, owners):
    """Verify immutable owner blobs, without interpreting historical locators."""
    verified = []
    selected = [n for n in graph["nodes"] if n["id"] in PROGRAM_IDS]
    needed = {ref for node in selected for ref in node.get("source_refs", [])}
    if set(mapping["sources"]) != needed:
        raise ValueError("Portable mapping must cover the exact selected source set")
    for ref, entry in sorted(mapping["sources"].items()):
        relative, commit = entry["path"], entry["commit"]
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
            raise ValueError("Owner source path must be portable and relative")
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
            raise ValueError("Owner revision must be an exact commit SHA")
        root = Path(owners[entry["repository"]]).resolve()
        raw = subprocess.check_output(["git", "-C", str(root), "show", commit + ":" + relative], stderr=subprocess.PIPE)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry["sha256"] or digest != graph["sources"][ref]["sha256"]:
            raise ValueError("Source bytes disagree with historical Work evidence: " + ref)
        verified.append(ref)
    return {"status": "PASS", "verified_sources": len(verified), "method": "git commit:path SHA-256",
            "historical_locators_modified": False, "authority_granted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--source-graph", type=Path)
    parser.add_argument("--source-map", type=Path)
    parser.add_argument("--owner", action="append", default=[], help="Explicit repository=checkout mapping")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    from graph_backlog import validate_work_graph, doctor_check
    from graph_backlog.jsonutil import compute_revision_hash
    from graph_backlog.mutations import execute_graph_transaction
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    before = compute_revision_hash(graph)
    report = {"baseline_revision": before, "baseline_doctor": doctor_check(graph)}
    if args.source_graph:
        source = json.loads(args.source_graph.read_text(encoding="utf-8"))
        result, scope = project(graph, source)
        validation = validate_work_graph(result)
        if validation["status"] != "PASS":
            raise ValueError(str(validation))
        if args.apply:
            result = execute_graph_transaction(args.graph, lambda current: project(current.raw, source)[0],
                                               expected_rev=before, save=True)
        report.update(scope=scope, applied=args.apply, validation=validation, result_revision=compute_revision_hash(result))
        graph = result
    elif args.apply:
        raise ValueError("--apply requires --source-graph")
    if args.source_map:
        owners = dict(item.split("=", 1) for item in args.owner)
        report["portable_sources"] = verify_sources(graph, json.loads(args.source_map.read_text(encoding="utf-8")), owners)
    report["result_doctor"] = doctor_check(graph)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if "doctor" not in key and key != "scope"}))


if __name__ == "__main__":
    main()
