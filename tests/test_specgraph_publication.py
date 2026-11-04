import importlib.util
import copy
import hashlib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("publish_specgraph_work", Path(__file__).parents[1] / "tools/publish_specgraph_work.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def graphs():
    base = {"nodes": [{"id": "unrelated", "status": "unchanged"}], "edges": [], "sources": {}, "meta": {"old": True}}
    source = copy.deepcopy(base)
    for identity in sorted(publication.PROGRAM_IDS):
        source["nodes"].append({"id": identity, "source_refs": ["proof"], "status": "recorded"})
    source["sources"]["proof"] = {"sha256": "a" * 64, "locator": "/historical/source"}
    source["nodes"][0]["status"] = "unrelated local change"
    source["meta"] = {"unrelated": "local state"}
    return base, source


def test_projection_preserves_unrelated_records_and_is_idempotent():
    base, source = graphs()
    result, scope = publication.project(base, source)
    assert result["nodes"][0] == base["nodes"][0]
    assert result["meta"] == base["meta"]
    assert len(result["nodes"]) == 18
    assert publication.project(result, source)[0] == result
    assert len(scope["nodes"]) == 17


def test_collision_and_external_dependency_refuse_import():
    base, source = graphs()
    copied, _ = publication.project(base, source)
    copied["nodes"][1]["status"] = "later progress"
    with pytest.raises(ValueError, match="overwrites progress"):
        publication.project(copied, source)
    source["edges"] = [{"from": "unrelated", "to": "SPECGRAPH-COMPLETION-C0", "type": "UNLOCKS"}]
    with pytest.raises(ValueError, match="external edge"):
        publication.project(base, source)


def test_portable_evidence_rejects_bad_bytes_and_escaping_paths(monkeypatch, tmp_path):
    raw = b"actual pinned source\n"
    digest = hashlib.sha256(raw).hexdigest()
    graph = {"nodes": [{"id": "SPECGRAPH-COMPLETION-C1", "source_refs": ["proof"]}], "sources": {"proof": {"sha256": digest}}}
    entry = {"repository": "owner", "path": "docs/spec.md", "commit": "a" * 40, "sha256": digest}
    mapping = {"sources": {"proof": entry}}
    monkeypatch.setattr(publication.subprocess, "check_output", lambda *a, **k: raw)
    assert publication.verify_sources(graph, mapping, {"owner": tmp_path})["verified_sources"] == 1
    entry["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="disagree"):
        publication.verify_sources(graph, mapping, {"owner": tmp_path})
    entry["path"] = "../outside"
    with pytest.raises(ValueError, match="relative"):
        publication.verify_sources(graph, mapping, {"owner": tmp_path})


def test_ci_dependencies_reject_mutable_refs_and_unknown_owners():
    spec = importlib.util.spec_from_file_location("ci_dependency_pins", Path(__file__).parents[1] / "tools/install_specgraph_ci_dependencies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pins = {owner: "a" * 40 for owner in module.OWNERS}
    assert module.validate_pins({"repositories": pins}) == pins
    pins["tare.tools.kernel"] = "main"
    with pytest.raises(ValueError, match="exact"):
        module.validate_pins({"repositories": pins})
    pins["tare.tools.kernel"] = "a" * 40
    pins["unexpected-owner"] = "b" * 40
    with pytest.raises(ValueError, match="Four"):
        module.validate_pins({"repositories": pins})
