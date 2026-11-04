"""Actual owner APIs, disposable projects, no model or host configuration writes."""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

import tare_ops


@pytest.fixture(autouse=True)
def owners_available():
    pytest.importorskip("graph_backlog")
    pytest.importorskip("specgraph")
    pytest.importorskip("tare_tools_agent_runtime")


@pytest.fixture
def project(tmp_path):
    node = {"id": "WORK-1", "title": "Selected work", "kind": "task", "cluster": "test",
            "horizon": "H0", "priority": "P1", "criticality": "HIGH", "authority_required": "none",
            "admission_state": "ADMITTED", "work_status": "PROPOSED", "epistemic_status": "CERTAIN",
            "source_refs": [], "exit_criteria": ["Specific check passes"], "summary": "Scoped fix",
            "canonical_system": "local", "canonical_id": "WORK-1", "canonical_revision": "r1",
            "observed_at": "2026-09-06T00:00:00Z", "projection_run_id": "test", "staleness_state": "FRESH",
            "completion": {"status": "NOT_DONE", "dod_satisfied": False, "evidence_grade": "B",
                           "materialization_scope": "local"},
            "target_repositories": ["example"], "grounding_refs": ["SPEC-1"],
            "target_paths": ["src/main.py"], "target_symbols": []}
    node.update(bounded_contexts=["test"], confidence={"score": 1.0}, evidence_required=[],
                items=[], metrics={}, notes=[], provenance=[], readiness={"operational_identity_required": True},
                source_claim_ids=[], source_details=[], tags=[], unlock_score=0)
    graph = tmp_path / "work-graph.json"
    graph.write_text(json.dumps({"meta": {"schema": "work-graph-poc/0.5"}, "sources": {},
                                 "nodes": [node], "edges": []}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": "2.0.0", "specs": [
        {"id": "SPEC-1", "doc_type": "spec", "title": "Scoped specification",
         "relative_path": "specs/SPEC-1.md", "sha256": "a" * 64,
         "target_repositories": ["example"], "acceptance_criteria": ["Specific check passes"]}],
        "adrs": [], "experiments": [], "post_mortems": []}), encoding="utf-8")
    (tmp_path / "specgraph.yaml").write_text("project_name: example\ngoverned_paths: [src]\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def useful():\n    return 1\n", encoding="utf-8")
    return tmp_path, graph, manifest


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_doctor_is_read_only_and_does_not_confuse_health_with_qualification(project):
    root, graph, manifest = project
    before = snapshot(root)
    report = tare_ops.doctor(root=root, graph=graph)
    assert snapshot(root) == before
    assert report["qualification_status"] == "NOT_EVALUATED" and report["authority"] == "NONE"
    assert report["operational_health"] == "ACTION_REQUIRED"  # stale cache, not repaired implicitly
    layers = {layer["name"]: layer for layer in report["layers"]}
    assert layers["backlog"]["status"] == "PASS"
    assert set(layers) == {"backlog", "specgraph"}


def test_context_uses_explicit_work_and_exact_declared_refs(project):
    root, graph, manifest = project
    before = snapshot(root)
    output = tare_ops.context(graph=graph, work_id="WORK-1", manifest=manifest)
    assert output["status"] == "READY_CONTEXT"
    assert output["work"]["work_id"] == "WORK-1" and output["authority"] == "NONE"
    assert output["groundings"][0]["requested_ids"] == ["SPEC-1"]
    assert output["groundings"][0]["entries"][0]["id"] == "SPEC-1"
    assert snapshot(root) == before


def test_unknown_work_does_not_fall_back_to_frontier(project):
    _, graph, manifest = project
    output = tare_ops.context(graph=graph, work_id="NOT-SELECTED", manifest=manifest)
    assert output["status"] == "BLOCKED" and output["work"]["status"] == "NOT_FOUND"
    assert output["groundings"] == []


def test_missing_required_document_does_not_inherit_other_context(project):
    _, graph, manifest = project
    payload = json.loads(manifest.read_bytes())
    payload["specs"][0]["id"] = "UNRELATED"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    output = tare_ops.context(graph=graph, work_id="WORK-1", manifest=manifest)
    assert output["status"] == "BLOCKED"
    assert output["groundings"][0]["status"] == "INCOMPLETE_MAPPING"


def test_context_without_manifest_reads_declared_refs_from_their_owner_trees(project, tmp_path, monkeypatch):
    from relay import relay_mesh
    root, graph, _ = project
    control = tmp_path / "control"
    (control / "docs").mkdir(parents=True)
    (control / "docs/spec.md").write_text("---\nacceptance_criteria:\n  AC-01: holds\n---\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                                 "commit", "-qm", "spec"]):
        subprocess.run(["git", "-C", str(control), *args], check=True, capture_output=True)
    monkeypatch.setattr(relay_mesh, "RELAY_ROOT", control)
    value = json.loads(graph.read_bytes())
    value["nodes"][0]["grounding_refs"] = ["tare.tools.os:docs/spec.md"]
    graph.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(root)
    output = tare_ops.context(graph=graph, work_id="WORK-1")
    assert output["status"] == "READY_CONTEXT", output
    assert output["groundings"][0]["entries"][0]["semantic_document_id"] == "tare.tools.os:docs/spec.md"
    assert snapshot(root) == before
    value["nodes"][0]["grounding_refs"] = ["tare.tools.os:docs/absent.md"]
    graph.write_text(json.dumps(value), encoding="utf-8")
    assert tare_ops.context(graph=graph, work_id="WORK-1")["reason"] == "REQUIRED_GROUNDING_UNAVAILABLE"


def test_maintenance_preview_and_apply_touch_only_derived_cache(project):
    root, _, _ = project
    before = snapshot(root)
    assert tare_ops.maintain(root)["status"] == "REPAIR_AVAILABLE"
    assert snapshot(root) == before
    assert tare_ops.maintain(root, apply=True)["status"] == "REPAIRED"
    after = snapshot(root)
    assert {name: value for name, value in after.items() if name in before} == before
    assert set(after) - set(before) == {str(Path(".specgraph/cache.json"))}
    cache = root / ".specgraph" / "cache.json"
    stamp = cache.stat().st_mtime_ns
    assert tare_ops.maintain(root, apply=True)["status"] == "UNCHANGED"
    assert cache.stat().st_mtime_ns == stamp


def test_malformed_source_is_not_repaired_into_a_pass(project):
    root, _, _ = project
    (root / "specgraph.yaml").write_text("governed_paths: 3\n", encoding="utf-8")
    before = snapshot(root)
    with pytest.raises(ValueError, match="PARSE_ERRORS"):
        tare_ops.maintain(root, apply=True)
    assert snapshot(root) == before


def test_unconfigured_repo_does_not_require_cargo_or_hooks(tmp_path):
    report = tare_ops.doctor(root=tmp_path)
    assert report["operational_health"] == "NOT_CONFIGURED"
    assert all(layer["status"] == "NOT_CONFIGURED" for layer in report["layers"])
    assert list(tmp_path.iterdir()) == []


def test_layer_failure_does_not_hide_other_layers(project):
    root, graph, manifest = project
    graph.write_bytes(b"invalid")
    output = tare_ops.doctor(root=root, graph=graph)
    assert output["layers"][0]["status"] == "UNAVAILABLE"
    assert output["layers"][1]["status"] == "STALE"


def test_cli_budget_never_silently_truncates_required_work(project, capsys):
    _, graph, _ = project
    code = tare_ops.main(["context", "--graph", str(graph), "--work", "WORK-1", "--max-bytes", "1024"])
    raw = capsys.readouterr().out.encode()
    assert code != 0 and len(raw) <= 1024
    assert json.loads(raw)["status"] != "READY_CONTEXT"


@pytest.mark.parametrize("name,module", [("audit", "relay.audit_cases"), ("hook", "compute_plane.hook_cli"),
                                       ("backlog", "graph_backlog.cli"), ("specgraph", "specgraph.cli")])
def test_owner_dispatch_preserves_args_and_exit_status(monkeypatch, name, module):
    from types import SimpleNamespace
    calls = []
    def load(selected):
        assert selected == module
        return SimpleNamespace(main=lambda args: calls.append(args) or 7)
    monkeypatch.setattr(tare_ops.importlib, "import_module", load)
    assert tare_ops.main([name, "original", "--argument"]) == 7
    assert calls == [["original", "--argument"]]


def test_real_kernel_hook_pass_is_neutral_not_permission_bypass(monkeypatch, capsys):
    pytest.importorskip("compute_plane.hook_cli")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "README.md"}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert tare_ops.main(["hook", "claude"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_module_cli_is_usable_without_provider(project):
    root, graph, _ = project
    completed = subprocess.run([sys.executable, "-B", "-m", "tare_ops", "doctor", "--root", str(root),
                                "--graph", str(graph)], cwd=Path(tare_ops.__file__).parent,
                               capture_output=True, timeout=15, check=False)
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["authority"] == "NONE"


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_bytes(b"value = 1\nother = 2\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c", "user.email=f@localhost",
                    "commit", "-qm", "fixture"], check=True)
    return root


def test_explore_cli_sources_only_keeps_verified_receipts_in_custody(repository, tmp_path, capsys):
    custody = tmp_path / 'custody'
    assert tare_ops.main(['explore','--root',str(repository),'--question','Where is value?',
                          '--focus','a.py:1-2','--custody',str(custody),'--sources-only']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status']=='SOURCES_READY' and output['sources'][0]['path']=='a.py'
    assert output['tokens']=={'input':0,'output':0}
    assert Path(output['custody']).is_relative_to(custody/'explore'/'repo')
    assert not (custody/'explorer.lock').exists()


def test_evidence_cli_returns_citable_range_without_a_model(repository, tmp_path, capsys):
    custody = tmp_path / "custody"
    assert tare_ops.main(["evidence", "--root", str(repository), "--path", "a.py", "--start", "2", "--end", "2",
                          "--custody", str(custody)]) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["text"] == "other = 2\n" and source["id"].startswith("X-") and source["repository"] == "repo"
    assert tare_ops.main(["evidence", "--root", str(repository), "--path", "../secret", "--start", "1", "--end", "1",
                          "--custody", str(custody)]) == 2


def test_research_cli_requires_explicit_question(tmp_path, capsys):
    assert tare_ops.main(["research", "--seed", "https://x.example", "--domain", "x.example",
                          "--custody", str(tmp_path)]) == 2
    assert "requires --question" in json.loads(capsys.readouterr().out)["error"]


def test_run_cli_rejects_malformed_verify_with_json_error(tmp_path, capsys):
    assert tare_ops.main(["run", "work-x", "--verify", "unit"]) == 2
    assert "--verify requires" in json.loads(capsys.readouterr().out)["error"]


def test_research_result_takes_a_job_id_not_a_path_and_forwards_the_ref_range(tmp_path, monkeypatch, capsys):
    from relay import research as study
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text(json.dumps({"status": "COMPLETED", "schema": study.SCHEMA}))
    assert tare_ops.main(["research", "--result", str(outside), "--custody", str(tmp_path)]) == 2
    assert "research_job_id_required" in json.loads(capsys.readouterr().out)["error"]
    seen = {}

    def fake_read_ref(jobs, job_id, ref, start_line=1, end_line=200):
        seen.update(jobs=jobs, job_id=job_id, ref=ref, start=start_line, end=end_line)
        return {"id": ref, "text": "x"}

    monkeypatch.setattr(study, "read_ref", fake_read_ref)
    job = "research-" + "a" * 32
    assert tare_ops.main(["research", "--result", job, "--ref", "W-1", "--start", "201", "--end", "260",
                          "--custody", str(tmp_path)]) == 0
    assert seen == {"jobs": tmp_path / "research", "job_id": job, "ref": "W-1", "start": 201, "end": 260}

