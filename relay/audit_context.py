"""SpecGraph-owned structural context selection; OS only packages its references."""
from collections import defaultdict
import builtins
import difflib
from pathlib import Path
import tempfile
import sys

from relay.audit_cases import checksum, require


def matrix_for(root):
    from specgraph.graph import CausalTraceabilityMatrix
    matrix = CausalTraceabilityMatrix.build(root, auto_cache=False)
    require(not (matrix.parse_errors or matrix.duplicate_warnings or matrix.dangling_spec_refs
                 or matrix.invalid_criteria_refs), "specgraph_invalid_context")
    return matrix


def symbols_for(text, name):
    """Reuse the owner's parser for exact historical bytes, without changing a checkout."""
    from specgraph.code_parser import CodeParser
    if not name.endswith(".py"):
        return []
    with tempfile.TemporaryDirectory(prefix="audit-symbols-") as directory:
        path = Path(directory) / "source.py"
        path.write_bytes(text.encode("utf-8"))
        symbols, errors = CodeParser.parse_file(path, directory)
    require(not errors, "specgraph_source_parse_failed")
    return symbols


def changed_lines(before, after):
    left, right = set(), set()
    for tag, a, b, c, d in difflib.SequenceMatcher(
            None, before.splitlines(), after.splitlines(), autojunk=False).get_opcodes():
        if tag != "equal":
            left.update(range(a + 1, b + 1))
            right.update(range(c + 1, d + 1))
    return left, right


def explicit_segments(text, path, names):
    """Project named Python definitions plus lexical scaffolding, never arbitrary line cuts.

    Names absent in one revision are allowed for additions/deletions; the caller
    checks their existence across both revisions. @module selects scaffolding.
    Dependency sufficiency remains explicit caller input, not an AST guarantee.
    """
    require(path.endswith('.py') and isinstance(names, list) and names
            and all(isinstance(name, str) and name.strip() for name in names)
            and len(names) == len(set(names)), 'invalid_symbol_selection')
    symbols = symbols_for(text, path)
    lines = text.splitlines(keepends=True)
    # Keep class headers/fields/docstrings and module statements. Function bodies
    # can be selected independently; their decorators remain at original lines.
    functions = [item for item in symbols if item.kind != 'class']
    bodies = {line for item in functions for line in range(item.start_line, item.end_line + 1)}
    keep = set(range(1, len(lines) + 1)) - bodies
    selected = [item for item in symbols if item.name in names]
    for item in selected:
        keep.update(range(item.start_line, item.end_line + 1))
        # A closure needs its enclosing function, not just an isolated nested def.
        for parent in functions:
            if parent.start_line <= item.start_line and item.end_line <= parent.end_line:
                keep.update(range(parent.start_line, parent.end_line + 1))
    segments = []
    previous = 0
    for number in sorted(keep):
        if number != previous + 1 or not segments:
            segments.append({'start_line': number, 'text': ''})
        segments[-1]['text'] += lines[number - 1]
        previous = number
    return {'text': '', 'segments': segments, 'omitted_lines': len(lines) - len(keep),
            'selected_symbols': list(names)}, {item.name for item in symbols} | {'@module'}, keep


def projected_text(source):
    return ''.join(item['text'] for item in source.get('segments', [])) if 'segments' in source else source['text']


def validate_explicit_sources(packet):
    """Check original bytes and exact projection even if a caller recomputes the outer seal."""
    archive = packet.get('source_archive', {})
    records = {item['ref']: item for repo in packet['repositories'] for item in repo['sources']}
    for source in packet['input']['sources']:
        if 'selected_symbols' not in source:
            continue
        raw = archive.get(source['id'])
        record = records.get(source['id'])
        require(isinstance(raw, str) and record is not None
                and checksum(raw.encode('utf-8')) == record['sha256'], 'symbol_archive_mismatch')
        expected, _, _ = explicit_segments(raw, source['path'], source['selected_symbols'])
        require(all(source.get(key) == value for key, value in expected.items()), 'symbol_projection_mismatch')


def select_segments(source, keep_names=(), touched=()):
    """Keep entire touched definitions plus module-level context; preserve real line numbers."""
    text = source["text"]
    lines = text.splitlines(keepends=True)
    symbols = symbols_for(text, source["path"])
    if not symbols:
        return
    # Keep module-level statements (imports, constants, decorators), not just hunks.
    body = {line for item in symbols for line in range(item.start_line, item.end_line + 1)}
    keep = set(range(1, len(lines) + 1)) - body
    keep.update(touched)
    for item in symbols:
        span = set(range(item.start_line, item.end_line + 1))
        if item.name in keep_names or span.intersection(touched):
            keep.update(span)
    require(set(touched) <= keep, "changed_lines_omitted")
    if len(keep) >= len(lines):
        return
    segments = []
    previous = 0
    for number in sorted(keep):
        if not segments or number != previous + 1:
            segments.append({"start_line": number, "lines": [lines[number - 1]]})
        else:
            segments[-1]["lines"].append(lines[number - 1])
        previous = number
    segments = [{"start_line": item["start_line"], "text": "".join(item["lines"])} for item in segments]
    source.update(text="", segments=segments, omitted_lines=len(lines) - len(keep))


def enrich(packet):
    """Select governing specs, linked implementations/tests and direct static dependencies."""
    from compute_plane.audit_driver import audit_source
    view = packet["input"]
    by_id = {source["id"]: source for source in view["sources"]}
    archive = packet.setdefault("source_archive", {})
    for repo in packet["repositories"]:
        if repo.get("context_strategy") != "specgraph":
            continue
        require(repo["target"] == "WORKTREE", "specgraph_requires_current_worktree")
        matrix = matrix_for(repo["root"])
        repo["specgraph_digest"] = matrix.canonical_digest()
        changed = [item for item in view["changes"] if item["repository"] == repo["id"]]
        links = defaultdict(set)
        for edge in matrix.edges:
            if edge["type"] == "CODE_IMPLEMENTS_SPEC":
                links[edge["from"]].add(edge["to"])
        selected, specs, obligation_ids = set(), set(), set()
        touched_by_ref = {}
        for change in changed:
            local_specs = set()
            relevant = {item["id"] for item in view["obligations"] if change["id"] in item["change_ids"]}
            obligation_ids.update(relevant)
            before = by_id.get(change["id"] + ".before", {}).get("text", "")
            after = by_id.get(change["id"] + ".after", {}).get("text", "")
            left, right = changed_lines(before, after)
            touched_by_ref[change["id"] + ".before"] = left
            touched_by_ref[change["id"] + ".after"] = right
            file_symbols = [item for item in matrix.symbols.values() if item.file_path == change["path"]]
            # Deleted definitions need their historical declaration links too.
            historical = symbols_for(before, change["path"])
            for symbol in file_symbols:
                if set(range(symbol.start_line, symbol.end_line + 1)) & right:
                    selected.add(symbol.symbol_id)
                    local_specs.update(links[symbol.symbol_id])
            for symbol in historical:
                if set(range(symbol.start_line, symbol.end_line + 1)) & left:
                    local_specs.update(ref.split(":")[0] for ref in symbol.linked_specs)
                    local_specs.update(links.get(change["path"] + ":" + symbol.name, ()))
            covered = {line for symbol in file_symbols for line in range(symbol.start_line, symbol.end_line + 1)}
            if not right or right - covered:
                for symbol in file_symbols:
                    local_specs.update(links[symbol.symbol_id])
            require(local_specs and local_specs <= set(matrix.specs), "specgraph_context_unmapped")
            specs.update(local_specs)
        # Same-spec implementation links and direct dependency references are
        # selected from the owner's graph, not reconstructed by another engine.
        selected.update(edge["from"] for edge in matrix.edges
                        if edge["type"] == "CODE_IMPLEMENTS_SPEC" and edge["to"] in specs)
        dependencies = {edge["to"] for edge in matrix.edges
                        if edge["type"] == "DEPENDS_ON_STATIC" and edge["from"] in selected}
        unresolved = {dep for dep in dependencies - set(matrix.symbols)
                      if dep.split(".")[0] not in sys.stdlib_module_names}
        # Precise owner graphs retain unknowns as references, never fake edges.
        # Local value reads need no definition; unknown call targets do. Only
        # owner-qualified stdlib and unshadowed builtins retain the old exception.
        for key in selected:
            for ref in getattr(matrix.symbols[key], "references", ()):
                if ref.status == "resolved" or (
                        ref.status == "external" and ref.reason == "stdlib_outside_index"):
                    continue
                if (ref.reason == "unbound_name" and ref.name in vars(builtins)
                        and not ref.imported_target):
                    continue
                if ref.imported_target or ref.status == "ambiguous" or ref.kind == "call":
                    unresolved.add(f"{ref.imported_target or ref.name}"
                                   f"[{ref.origin}:{ref.status}:{ref.reason}]")
        require(not unresolved, "specgraph_dependency_context_missing:" + ",".join(sorted(unresolved))[:160])
        selected.update(dependencies & set(matrix.symbols))
        require(len(selected) <= 128, "specgraph_selection_over_budget")
        names_by_path = defaultdict(set)
        for key in selected:
            symbol = matrix.symbols[key]
            names_by_path[symbol.file_path].add(symbol.name)
        extra_paths = {matrix.specs[key].file_path for key in specs}
        extra_paths.update(matrix.tests[edge["from"]].file_path for edge in matrix.edges
                           if edge["type"] == "TEST_VERIFIES_SPEC" and edge["to"] in specs)
        extra_paths.update(names_by_path)
        existing_paths = {s["path"] for s in view["sources"]
                          if s.get("repository") == repo["id"] and s.get("revision") == repo["target"]}
        for index, name in enumerate(sorted(extra_paths - existing_paths)):
            raw = audit_source(repo["root"], repo["target"], name, limit=262144)
            require(raw is not None, "specgraph_context_missing")
            require(b'\0' not in raw, 'binary_source_requires_explicit_strategy')
            ref = repo["id"] + ".SG" + str(index)
            source = {"id": ref, "kind": "source", "repository": repo["id"], "path": name,
                "revision": repo["target"], "start_line": 1, "text": raw.decode("utf-8"),
                "why": "SpecGraph governing contract, implementation, direct dependency or verifying test",
                "obligation_ids": sorted(obligation_ids)}
            view["sources"].append(source)
            repo["sources"].append({"ref": ref, "path": name, "revision": repo["target"],
                                    "sha256": checksum(raw), "size_bytes": len(raw)})
        for source in view["sources"]:
            if source.get("repository") != repo["id"] or source["kind"] != "source":
                continue
            archive[source["id"]] = source["text"]
            # Linked tests/spec documents remain complete, including fixtures and criteria.
            if source["path"] in names_by_path or source["id"] in touched_by_ref:
                select_segments(source, names_by_path[source["path"]], touched_by_ref.get(source["id"], ()))
        view["context"].setdefault("structural_selection", []).append({
            "repository": repo["id"], "specs": sorted(specs), "symbols": sorted(selected),
            "assurance": "declared_static_links_not_dynamic_completeness"})
    return packet
