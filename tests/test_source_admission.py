"""Real Backlog + owner-tree source bodies + Kernel + OS admission falsifiers."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from graph_backlog.grounding import encode_work_grounding, ground_work_item
from relay import sources
from relay.source_admission import evaluate_source_admission


def git(root, *args):
    return subprocess.run(["git", "-c", f"core.hooksPath={root / 'no-hooks'}", "-c", "user.name=Test",
                           "-c", "user.email=test@example.invalid", "-C", str(root), *args],
                          capture_output=True, check=True, timeout=10).stdout.decode().strip()


@pytest.fixture
def admission(tmp_path):
    root = tmp_path / "owner"
    (root / "docs").mkdir(parents=True)
    body = "---\nid: SPEC-ONE\nstatus: ACCEPTED\nacceptance_criteria:\n  AC-01: preserve exact intent\n---\n# Owner source\n"
    doc = root / "docs/spec.md"
    doc.write_text(body, encoding="utf-8", newline="\n")
    git(root, "init", "-q"); git(root, "add", "."); git(root, "commit", "-qm", "source")
    pin = git(root, "rev-parse", "HEAD")
    sha = hashlib.sha256(doc.read_bytes()).hexdigest()
    ref = "owner:docs/spec.md"
    locator = f"owner@{pin}:docs/spec.md"
    node = {"id": "WORK-1", "title": "Preserve source", "kind": "task", "cluster": "source", "horizon": "H0",
            "work_status": "PROPOSED", "admission_state": "ADMITTED", "epistemic_status": "CERTAIN",
            "bounded_contexts": ["source"], "priority": "P0", "criticality": "HIGH", "authority_required": "none",
            "evidence_required": ["test_logs"], "confidence": {"score": 1.0}, "summary": "Source integration",
            "exit_criteria": ["AC-01: preserve exact intent"], "spec_ref": ref, "source_refs": ["SOURCE"],
            "source_details": [], "provenance": [], "source_claim_ids": [], "unlock_score": 0, "tags": [],
            "metrics": {}, "notes": [], "items": [], "canonical_system": "local", "canonical_id": "WORK-1",
            "canonical_revision": "r1", "observed_at": "2026-09-08", "projection_run_id": "test",
            "staleness_state": "FRESH", "readiness": {"operational_identity_required": True},
            "completion": {"status": "NOT_DONE", "dod_satisfied": False},
            "repository_scopes": [{"repository": "owner", "grounding_refs": [ref], "target_paths": ["src"], "target_symbols": []}]}
    graph = {"meta": {"schema": "work-graph-poc/0.5", "title": "Fixture", "snapshot": "test", "generated_at": "2026-09-08", "projection_run_id": "test"},
             "sources": {"SOURCE": {"kind": "repo_file", "title": "Source", "recoverability": "CONTENT_ADDRESSED",
                         "repository_ref": ref, "sha256": sha, "locator": locator}}, "nodes": [node], "edges": []}
    graph_path = tmp_path / "work-graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    manifest = sources.write(tmp_path / "SOURCES.json", node, graph["sources"], {"owner": (root, "HEAD")})
    descriptor = SimpleNamespace(work_id="WORK-1", execution_repository="owner", verification_checks=[SimpleNamespace(criterion=node["exit_criteria"][0])])
    return dict(work_graph=graph_path, descriptor=descriptor, library_manifest=manifest, owner_roots={"owner": root})


def ground(arguments):
    report = ground_work_item(arguments["work_graph"], "WORK-1", max_bytes=65536)
    assert report.status == "READY", report.reason_codes
    return encode_work_grounding(report)


def test_actual_owners_match_exact_pinned_body_without_mutating_work_or_catalog(admission):
    before = {key: admission[key].read_bytes() for key in ("work_graph", "library_manifest")}
    result = evaluate_source_admission(**admission, work_grounding=ground(admission))
    assert result["status"] == "MATCHED", result
    assert result["authority"] == "NONE" and result["work_modified"] is False
    assert result["criterion_ids"] == ["AC-01"]
    for key, raw in before.items():
        assert admission[key].read_bytes() == raw
    (admission["owner_roots"]["owner"] / "docs/spec.md").write_text("dirty working source")
    assert evaluate_source_admission(**admission, work_grounding=ground(admission))["status"] == "MATCHED"


@pytest.mark.parametrize("mutation,reason", [("text", "SOURCE_AC_TEXT_MISMATCH"), ("source", "WORK_SOURCE_MISMATCH")])
def test_owner_source_and_ac_mismatch_is_refused(admission, mutation, reason):
    path = admission["work_graph"]
    graph = json.loads(path.read_bytes())
    node = graph["nodes"][0]
    if mutation == "text":
        node["exit_criteria"] = ["AC-01: different intent"]
        admission["descriptor"].verification_checks[0].criterion = node["exit_criteria"][0]
    else:
        node["repository_scopes"][0]["grounding_refs"] = ["owner:docs/other.md"]
    path.write_text(json.dumps(graph))
    result = evaluate_source_admission(**admission, work_grounding=ground(admission))
    assert result["status"] == "REFUSED" and reason in result["reason_codes"]


def test_spec_prose_edit_keeps_the_work_and_criterion_edit_refuses_it(admission):
    root = admission["owner_roots"]["owner"]
    graph = json.loads(admission["work_graph"].read_bytes())
    doc = root / "docs/spec.md"
    doc.write_text(doc.read_text() + "More prose.\n", newline="\n")
    git(root, "commit", "-qam", "prose")
    sources.write(admission["library_manifest"], graph["nodes"][0], graph["sources"], {"owner": (root, "HEAD")})
    result = evaluate_source_admission(**admission, work_grounding=ground(admission))
    assert result["status"] == "MATCHED", result  # the Work's recorded SHA-256 no longer matches
    graph["sources"]["SOURCE"].pop("sha256")  # records need no SHA either
    admission["work_graph"].write_text(json.dumps(graph))
    assert evaluate_source_admission(**admission, work_grounding=ground(admission))["status"] == "MATCHED"
    doc.write_text(doc.read_text().replace("preserve exact intent", "preserve some intent"), newline="\n")
    git(root, "commit", "-qam", "criterion")
    sources.write(admission["library_manifest"], graph["nodes"][0], graph["sources"], {"owner": (root, "HEAD")})
    result = evaluate_source_admission(**admission, work_grounding=ground(admission))
    assert result["reason_codes"] == ["SOURCE_AC_TEXT_MISMATCH"]


def test_graph_race_check_mismatch_missing_owner_and_output_bound(admission):
    raw = ground(admission)
    admission["work_graph"].write_bytes(admission["work_graph"].read_bytes() + b" ")
    assert evaluate_source_admission(**admission, work_grounding=raw)["reason_codes"] == ["WORK_GRAPH_CHANGED"]
    raw = ground(admission)
    admission["descriptor"].verification_checks[0].criterion = "AC-99: fake"
    assert evaluate_source_admission(**admission, work_grounding=raw)["reason_codes"] == ["VERIFICATION_CRITERIA_MISMATCH"]
    admission["descriptor"].verification_checks[0].criterion = "AC-01: preserve exact intent"
    result = evaluate_source_admission(**(admission | {"owner_roots": {}}), work_grounding=raw)
    assert result["reason_codes"] == ["PINNED_SOURCE_UNAVAILABLE"]
    result = evaluate_source_admission(**admission, work_grounding=raw, max_bytes=1024)
    assert len((json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()) <= 1024


def test_source_refusal_reaches_existing_os_admission_policy(admission):
    from relay.landing_policy import AgentIdentity, create_evidence, evaluate_admission
    from relay.train_execution import canonical_json_bytes
    identity_payload = {"schema": "tare.tools/agent-identity/1", "agent_id": "test", "role": "implementer",
                        "runtime": "test", "vendor": "test", "bias_domain": "test", "roster_sha256": "1" * 64}
    raw = canonical_json_bytes(identity_payload)
    identity = AgentIdentity(identity_payload, raw, hashlib.sha256(raw).hexdigest())
    admission["descriptor"].verification_checks[0].criterion = "AC-99: fake"
    binding = evaluate_source_admission(**admission, work_grounding=ground(admission))
    checks = {key: True for key in ("candidate_immutable", "scope_complete", "work_current", "specgraph_current",
                                   "verification_complete", "protected_paths_clear", "secrets_clear", "cas_ready")}
    checks["scope_complete"] = binding["status"] == "MATCHED"
    evidence = create_evidence(train_id="T", descriptor_sha256="2"*64, candidate_sha256="3"*64,
                               work_scope_sha256="4"*64, specgraph_scope_sha256="5"*64, checks=checks)
    assert evaluate_admission(evidence, identity).verdict == "REFUSED"


def test_real_materializer_uses_backlog_specgraph_and_source_fence(admission, tmp_path):
    from relay.landing_evidence import materialize_landing_evidence
    from relay.train_execution import canonical_json_bytes
    from compute_plane.managed_work import VerificationCommand, observe_work_progress
    from specgraph.executor_evidence import project_executor_observation
    root = admission["owner_roots"]["owner"]
    git(root, "branch", "-M", "main")
    base = git(root, "rev-parse", "HEAD")
    (root / "src").mkdir()
    (root / "src/value.txt").write_text("implementation\n")
    git(root, "add", "src/value.txt"); git(root, "commit", "-qm", "candidate")
    candidate = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/heads/main", base)
    descriptor = admission["descriptor"]
    descriptor.base_commit = base
    descriptor.target_ref = "refs/heads/main"
    descriptor.tracked_paths = ("src",)
    descriptor.sha256 = "1" * 64
    receipt = SimpleNamespace(sha256="2"*64, payload={"candidate_commit": candidate, "changed_paths": ["src/value.txt"]})
    train = tmp_path / "train"
    train.mkdir()
    command = (sys.executable, "-c", "from pathlib import Path; assert Path('src/value.txt').read_text() == 'implementation\\n'")
    descriptor.verification_checks[0].command = command
    checks = [VerificationCommand(descriptor.verification_checks[0].criterion, command)]
    progress = observe_work_progress(workspace=root, exit_criteria=[checks[0].criterion], checks=checks, tracked_paths=["src"])
    observation = project_executor_observation(progress, commands=checks,
        executor="tare.tools.kernel/compute_plane.managed_work", candidate_sha256=receipt.sha256, descriptor_sha256=descriptor.sha256, max_bytes=65536)
    (train / "EXECUTOR_OBSERVATION.json").write_bytes(canonical_json_bytes(observation))
    kwargs = dict(train_dir=train, train_id="SOURCE-TEST", descriptor=descriptor, candidate_receipt=receipt,
                  repository_root=root, work_graph=admission["work_graph"], library_manifest=admission["library_manifest"],
                  backlog_command=(sys.executable, "-B", "-m", "graph_backlog.cli"),
                  specgraph_command=(sys.executable, "-B", "-m", "specgraph.cli"))
    evidence = materialize_landing_evidence(**kwargs)
    assert all(evidence.payload["checks"].values()), evidence.payload
    assert json.loads((train / "SOURCE_ADMISSION.json").read_bytes())["status"] == "MATCHED"
    # Same real owner chain, now the Work's criterion text disagrees with its source despite metadata READY.
    graph = json.loads(admission["work_graph"].read_bytes())
    graph["nodes"][0]["exit_criteria"] = ["AC-01: different intent"]
    admission["work_graph"].write_text(json.dumps(graph))
    descriptor.verification_checks[0].criterion = "AC-01: different intent"
    refused_train = tmp_path / "refused"
    refused_train.mkdir()
    (refused_train / "EXECUTOR_OBSERVATION.json").write_bytes(canonical_json_bytes(observation))
    refused = materialize_landing_evidence(**(kwargs | {"train_dir": refused_train}))
    assert refused.payload["checks"]["scope_complete"] is False
    assert refused.payload["checks"]["specgraph_current"] is True
    assert json.loads((refused_train / "SOURCE_ADMISSION.json").read_bytes())["reason_codes"] == ["SOURCE_AC_TEXT_MISMATCH"]
