from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "compute_plane.workspace",
    reason="pinned private Kernel package is unavailable on this runner",
)

from relay import relay_mesh
from relay.candidate_change import materialize_verify_and_record_candidate
from relay.landing_policy import (
    attest_identity,
    create_evidence,
    write_evidence,
    write_identity,
)
from relay.strict_workspace import provision_train_workspace
from relay.train_execution import (
    canonical_json_bytes,
    create_execution_descriptor,
    read_execution_descriptor,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("landing_mode,scenario", [
    ("human-audit", "normal"),
    ("smart-optional", "normal"),
    ("smart-optional", "roster-changed"),
    ("human-audit", "minimum-A"),
    ("human-audit", "conflict"),
    ("human-audit", "stale-base"),
    ("human-audit", "report-changed"),
    ("human-audit", "replay"),
    ("human-audit", "legacy-replay"),
    ("human-audit", "replay-diverged"),
    ("human-audit", "replay-descendant"),
])
def test_disposable_strict_train_uses_one_candidate_through_admission_and_cas(
    tmp_path: Path,
    landing_mode: str,
    scenario: str,
    monkeypatch,
    capsys,
):
    original_root = relay_mesh.RELAY_ROOT
    roster_raw = (original_root / "relay" / "round_table_roster.json").read_bytes()
    try:
        os_root = tmp_path / "os"
        os_root.mkdir()
        relay_mesh.rebind_root(os_root)
        relay_mesh.ensure_dirs()
        (relay_mesh.RELAY_DIR / "round_table_roster.json").write_bytes(roster_raw)
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps({
            "nodes": [{
                "id": "work-one",
                "title": "Strict Work",
                "exit_criteria": ["content-is-valid"],
                "completion": {"status": "NOT_DONE"},
                "work_status": "planned",
            }],
            "edges": [],
        }), encoding="utf-8")

        repository = tmp_path / "repository"
        repository.mkdir()
        _git("init", cwd=repository)
        _git("config", "user.email", "test@example.invalid", cwd=repository)
        _git("config", "user.name", "Test", cwd=repository)
        (repository / "src").mkdir()
        (repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
        _git("add", ".", cwd=repository)
        _git("commit", "-m", "base", cwd=repository)
        _git("branch", "-M", "main", cwd=repository)
        base = _git("rev-parse", "HEAD", cwd=repository)

        train_id = "TRAIN-STRICT-E2E"
        relay_mesh.init_train(train_id, "Strict E2E", ["work-one"], "planner")
        train = relay_mesh.TRAINS_DIR / train_id
        digest = create_execution_descriptor(train / "EXECUTION_DESCRIPTOR.json", {
            "schema": "tare.tools/train-execution/1",
            "train_id": train_id,
            "work_id": "work-one",
            "work_graph": "work-graph.json",
            "execution_repository": "repo-one",
            "base_commit": base,
            "target_ref": "refs/heads/main",
            "library_manifest": "LIBRARY_MANIFEST.json",
            "tracked_paths": ["src"],
            "verification_checks": [{
                "criterion": "content-is-valid",
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/value.txt').read_text() == 'strict\\n'",
                ],
            }],
        })
        (train / "PLAN_AUDIT_REPORT.md").write_text(
            f"work-one\nPLAN_VERDICT: APPROVED\ndescriptor {digest[:12]}",
            encoding="utf-8",
        )
        assert relay_mesh.transition_train(
            train_id, "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "ok"
        )
        descriptor = read_execution_descriptor(train / "EXECUTION_DESCRIPTOR.json")
        workspaces = tmp_path / "workspaces"
        workspaces.mkdir()
        artifacts = provision_train_workspace(
            train,
            descriptor,
            repository_root=repository,
            workspace_parent=workspaces,
            id_factory=lambda: "wt-e2e-12345678",
        )
        assert relay_mesh.acquire_lease(train_id, "impl", "implementer")
        assert relay_mesh.transition_train(
            train_id, "IN_PROGRESS_IMPLEMENTER", "implementer",
            "impl", "implementer", "strict started",
        )
        (artifacts.lease.root / "src" / "value.txt").write_text(
            "strict\n", encoding="utf-8"
        )
        receipt = materialize_verify_and_record_candidate(
            train, descriptor, artifacts.lease
        )
        ready_bytes = artifacts.ready_file.read_bytes()
        tampered_ready = json.loads(ready_bytes)
        tampered_ready["repository_root"] = str(tmp_path / "other-repository")
        artifacts.ready_file.write_bytes(canonical_json_bytes(tampered_ready))
        with pytest.raises(ValueError, match="WORKSPACE_READY_MISMATCH"):
            relay_mesh._strict_candidate_context(
                train, json.loads((train / "STATE.json").read_text(encoding="utf-8"))
            )
        artifacts.ready_file.write_bytes(ready_bytes)
        assert relay_mesh.transition_train(
            train_id, "AUDIT_PENDING", "auditor",
            "impl", "implementer", "candidate ready",
        )
        relay_mesh.release_lease(train_id, "impl")
        if landing_mode == "smart-optional":
            identity = attest_identity(
                roster_path=relay_mesh.RELAY_DIR / "round_table_roster.json",
                agent_id="impl",
                role="implementer",
                model_id="gpt-5.6-sol",
                runtime_key="codex",
                runtime_model="gpt-5.6-sol",
            )
            evidence = create_evidence(
                train_id=train_id,
                descriptor_sha256=descriptor.sha256,
                candidate_sha256=receipt.sha256,
                work_scope_sha256="3" * 64,
                specgraph_scope_sha256="4" * 64,
                checks={
                    "candidate_immutable": True,
                    "scope_complete": True,
                    "work_current": True,
                    "specgraph_current": True,
                    "verification_complete": True,
                    "protected_paths_clear": True,
                    "secrets_clear": True,
                    "cas_ready": True,
                },
            )
            write_identity(train / "IMPLEMENTER_IDENTITY.json", identity)
            write_evidence(train / "LANDING_EVIDENCE.json", evidence)
            verdict = relay_mesh.prepare_admission_verdict(train_id)
            assert verdict.verdict == "ADMITTED"
            assert verdict.payload["audit"]["recommended"] is True
            if scenario == "roster-changed":
                # Admitted, not yet landed: the landing owner recomputes the bundle, so a roster that no longer
                # matches the recorded identities stops the landing.
                assert relay_mesh.admit_train(train_id, auto_land=False)
                roster = relay_mesh.RELAY_DIR / "round_table_roster.json"
                roster.write_bytes(roster.read_bytes() + b"\n")
                assert not relay_mesh.land_train(train_id, signoff="SMART_POLICY")
                assert _git("rev-parse", "refs/heads/main", cwd=repository) == base
                assert artifacts.lease.root.exists()
                return
            assert relay_mesh.admit_train(train_id)
            assert _git("rev-parse", "refs/heads/main", cwd=repository) == receipt.payload[
                "candidate_commit"
            ]
            assert not artifacts.lease.root.exists()
            graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_text(encoding="utf-8"))
            assert graph["nodes"][0]["work_status"] == "done"
            assert graph["nodes"][0]["completion"]["evidence_grade"] == "C"
            assert graph["nodes"][0]["provenance"]
            return
        assert relay_mesh.acquire_lease(train_id, "aud", "auditor")
        assert relay_mesh.transition_train(
            train_id, "IN_PROGRESS_AUDITOR", "auditor",
            "aud", "auditor", "audit started",
        )
        packet_digest = hashlib.sha256((train / "PACKET.md").read_bytes()).hexdigest()
        (train / "AUDIT_REPORT.md").write_text(
            "work-one\n"
            f"packet {packet_digest}\n"
            f"candidate {receipt.payload['candidate_commit']}\n"
            f"{relay_mesh._STAGE_VERDICTS['AUDIT'][1]}: Approve\n"
            "RecomendaÃ§Ã£o da Auditoria: Aprovar\nVERIFIED_PASS",
            encoding="utf-8",
        )
        assert relay_mesh.transition_train(
            train_id, "READY_FOR_HUMAN_APPROVAL", "human",
            "aud", "auditor", "candidate audited",
        )
        relay_mesh.release_lease(train_id, "aud")
        capability = json.loads(
            (relay_mesh.APPROVALS_DIR / f"CAPABILITY_{train_id}.json").read_text(
                encoding="utf-8"
            )
        )
        candidate_file = train / "CANDIDATE_CHANGE.json"
        candidate_bytes = candidate_file.read_bytes()
        swapped = json.loads(candidate_bytes)
        swapped["diff_sha256"] = "0" * 64
        candidate_file.write_bytes(canonical_json_bytes(swapped))
        assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
        assert _git("rev-parse", "refs/heads/main", cwd=repository) == base
        # Admission re-derives the candidate from Git too: a swapped receipt is neither admitted nor evidenced.
        ok, why = relay_mesh.admission_valid(train_id)
        assert not ok and "CANDIDATE_IDENTITY_MISMATCH" in why
        with pytest.raises(RuntimeError, match="CANDIDATE_IDENTITY_MISMATCH"):
            relay_mesh.materialize_admission_evidence(train_id)
        candidate_file.write_bytes(candidate_bytes)
        from relay import candidate_change
        from unittest.mock import Mock
        land_spy = Mock(wraps=candidate_change.land_recorded_candidate)
        monkeypatch.setattr(candidate_change, "land_recorded_candidate", land_spy)
        cap_file = relay_mesh.APPROVALS_DIR / f"CAPABILITY_{train_id}.json"
        cap_bytes = cap_file.read_bytes()
        if scenario in {"minimum-A", "conflict"}:
            original_graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
            graph = json.loads(original_graph)
            if scenario == "minimum-A":
                graph["nodes"][0]["readiness"] = {"minimum_evidence_grade": "A"}
            else:
                graph["nodes"][0]["completion"] = {
                    "status": "DONE", "evidence": "another train",
                }
                graph["nodes"][0]["work_status"] = "done"
            relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding="utf-8")
            before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
            capsys.readouterr()
            assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
            diagnostic = capsys.readouterr().out
            assert "[REFUSED]" in diagnostic
            assert ("evidence" if scenario == "minimum-A" else "conflicting completion") in diagnostic
            land_spy.assert_not_called()
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == before
            assert cap_file.read_bytes() == cap_bytes
            assert _git("rev-parse", "refs/heads/main", cwd=repository) == base
            assert artifacts.lease.root.exists()
            relay_mesh.WORK_GRAPH_FILE.write_bytes(original_graph)
        if scenario == "report-changed":
            # Approval is bound to the audit bytes it saw: an edited report cannot ride on the old capability.
            report_file = train / "AUDIT_REPORT.md"
            report_bytes = report_file.read_bytes()
            report_file.write_bytes(report_bytes + b" ")
            assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
            assert _git("rev-parse", "refs/heads/main", cwd=repository) == base
            land_spy.assert_not_called()
            assert cap_file.read_bytes() == cap_bytes
            report_file.write_bytes(report_bytes)
            assert relay_mesh.land_train(train_id, nonce=capability["nonce"])
            return
        if scenario == "stale-base":
            # The target moved after the candidate was recorded and approved: CAS refuses, nothing is rebased.
            (repository / "other.txt").write_text("other\n", encoding="utf-8")
            _git("add", "other.txt", cwd=repository)
            _git("commit", "-qm", "target moved", cwd=repository)
            moved = _git("rev-parse", "refs/heads/main", cwd=repository)
            before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
            capsys.readouterr()
            assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
            assert "BASE_REF_DIVERGED" in capsys.readouterr().out
            land_spy.assert_called_once()
            assert _git("rev-parse", "refs/heads/main", cwd=repository) == moved
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == before
            assert cap_file.read_bytes() == cap_bytes
            assert artifacts.lease.root.exists()
            return
        expected_target = receipt.payload["candidate_commit"]
        if scenario in {"replay", "legacy-replay", "replay-diverged", "replay-descendant"}:
            with monkeypatch.context() as interrupted:
                interrupted.setattr(relay_mesh, "transition_train", lambda *a, **kw: False)
                assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
            land_spy.assert_called_once()
            assert artifacts.lease.root.exists()
            assert cap_file.read_bytes() == cap_bytes
            if scenario == "legacy-replay":
                graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
                completion = graph["nodes"][0]["completion"]
                for field in ("evidence_grade", "evidence_grade_reason", "dod_evidence"):
                    completion.pop(field, None)
                graph["nodes"][0].pop("provenance", None)
                relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding="utf-8")
            surviving = relay_mesh.WORK_GRAPH_FILE.read_bytes()
            mtime = relay_mesh.WORK_GRAPH_FILE.stat().st_mtime_ns
            land_spy.reset_mock()
        if scenario == "replay-descendant":
            expected_target = _git("commit-tree", receipt.payload["candidate_tree"],
                                   "-p", receipt.payload["candidate_commit"], "-m", "later commit", cwd=repository)
            _git("update-ref", descriptor.target_ref, expected_target, receipt.payload["candidate_commit"], cwd=repository)
        if scenario == "replay-diverged":
            _git("update-ref", descriptor.target_ref, base, receipt.payload["candidate_commit"], cwd=repository)
            assert not relay_mesh.land_train(train_id, nonce=capability["nonce"])
            land_spy.assert_not_called()
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == surviving
            assert cap_file.read_bytes() == cap_bytes
            assert _git("rev-parse", descriptor.target_ref, cwd=repository) == base
            assert artifacts.lease.root.exists()
            return
        assert relay_mesh.land_train(train_id, nonce=capability["nonce"])
        if scenario in {"replay", "legacy-replay", "replay-descendant"}:
            land_spy.assert_not_called()
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == surviving
            assert relay_mesh.WORK_GRAPH_FILE.stat().st_mtime_ns == mtime
        else:
            land_spy.assert_called_once()
        assert _git("rev-parse", "refs/heads/main", cwd=repository) == expected_target
        assert not artifacts.lease.root.exists()
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_text(encoding="utf-8"))
        assert graph["nodes"][0]["work_status"] == "done"
        completion = graph["nodes"][0]["completion"]
        if scenario == "legacy-replay":
            assert "evidence_grade" not in completion
        else:
            assert completion["evidence_grade"] == "C"
            assert "not independent" in completion["evidence_grade_reason"]
            binding = json.loads(graph["nodes"][0]["provenance"][-1].split(":", 1)[1])
            assert binding["candidate_commit"] == receipt.payload["candidate_commit"]
            assert binding["candidate_sha256"] == receipt.sha256
            assert binding["descriptor_sha256"] == descriptor.sha256
            assert binding["artifacts"]["CANDIDATE_CHANGE.json"] == hashlib.sha256(candidate_bytes).hexdigest()
    finally:
        relay_mesh.rebind_root(original_root)
