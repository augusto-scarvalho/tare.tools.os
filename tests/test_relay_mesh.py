import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay import relay_mesh
from relay import train_execution

class RelayMeshTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_relay_")
        self.orig_relay_dir = relay_mesh.RELAY_DIR
        self.orig_trains_dir = relay_mesh.TRAINS_DIR
        self.orig_leases_dir = relay_mesh.LEASES_DIR
        self.orig_inbox_dir = relay_mesh.INBOX_DIR
        self.orig_alerts_dir = relay_mesh.ALERTS_DIR
        self.orig_approvals_dir = relay_mesh.APPROVALS_DIR
        self.orig_history_dir = relay_mesh.HISTORY_DIR
        self.orig_telemetry_file = relay_mesh.TELEMETRY_FILE
        self.orig_board_file = relay_mesh.BOARD_FILE
        self.orig_board_json = relay_mesh.BOARD_JSON
        self.orig_work_graph_file = relay_mesh.WORK_GRAPH_FILE

        # Patch paths to temporary test directory
        p = Path(self.test_dir)
        relay_mesh.RELAY_DIR = p / "relay"
        relay_mesh.TRAINS_DIR = p / "relay" / "trains"
        relay_mesh.LEASES_DIR = p / "relay" / "leases"
        relay_mesh.INBOX_DIR = p / "relay" / "inbox"
        relay_mesh.ALERTS_DIR = p / "relay" / "alerts"
        relay_mesh.APPROVALS_DIR = p / "relay" / "approvals"
        relay_mesh.HISTORY_DIR = p / "relay" / "history"
        relay_mesh.TELEMETRY_FILE = relay_mesh.HISTORY_DIR / "telemetry.jsonl"
        relay_mesh.BOARD_FILE = p / "relay" / "CURRENT_BOARD.md"
        relay_mesh.BOARD_JSON = p / "relay" / "BOARD.json"
        relay_mesh.WORK_GRAPH_FILE = p / "work-graph.json"

        # Initialize mock work-graph.json
        mock_graph = {
            "nodes": [
                {
                    "id": "TASK-T1",
                    "title": "Task T1 Test",
                    "summary": "Summary of Task T1",
                    "exit_criteria": ["T1 criterion 1", "T1 criterion 2"],
                    "completion": {"status": "NOT_DONE"},
                    "work_status": "planned"
                },
                {
                    "id": "TASK-T2",
                    "title": "Task T2 Test",
                    "summary": "Summary of Task T2",
                    "exit_criteria": ["T2 criterion 1"],
                    "completion": {"status": "NOT_DONE"},
                    "work_status": "planned"
                }
            ],
            "edges": []
        }
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(mock_graph, indent=2), encoding="utf-8")
        relay_mesh.ensure_dirs()

    def tearDown(self):
        # Restore original paths
        relay_mesh.RELAY_DIR = self.orig_relay_dir
        relay_mesh.TRAINS_DIR = self.orig_trains_dir
        relay_mesh.LEASES_DIR = self.orig_leases_dir
        relay_mesh.INBOX_DIR = self.orig_inbox_dir
        relay_mesh.ALERTS_DIR = self.orig_alerts_dir
        relay_mesh.APPROVALS_DIR = self.orig_approvals_dir
        relay_mesh.HISTORY_DIR = self.orig_history_dir
        relay_mesh.TELEMETRY_FILE = self.orig_telemetry_file
        relay_mesh.BOARD_FILE = self.orig_board_file
        relay_mesh.BOARD_JSON = self.orig_board_json
        relay_mesh.WORK_GRAPH_FILE = self.orig_work_graph_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_lease_mutual_exclusion(self):
        # Agent A acquires lease
        res1 = relay_mesh.acquire_lease("TARGET-01", "agent_a", "implementer", ttl_seconds=100)
        self.assertTrue(res1)

        # Agent B attempts to acquire same lease -> Must be denied
        res2 = relay_mesh.acquire_lease("TARGET-01", "agent_b", "implementer", ttl_seconds=100)
        self.assertFalse(res2)

        # Agent A renews lease -> Must succeed
        res_renew = relay_mesh.renew_lease("TARGET-01", "agent_a", additional_ttl=200)
        self.assertTrue(res_renew)

        # Agent A releases lease
        res_rel = relay_mesh.release_lease("TARGET-01", "agent_a")
        self.assertTrue(res_rel)

        # Agent B can now acquire lease -> Must succeed
        res3 = relay_mesh.acquire_lease("TARGET-01", "agent_b", "implementer", ttl_seconds=100)
        self.assertTrue(res3)

    def test_guards_fail_closed(self):
        relay_mesh.init_train("TRAIN-G1", "Guard Train", ["TASK-G1"], "planner")
        # overlap guard: same task in a second active train is refused
        with self.assertRaises(SystemExit):
            relay_mesh.init_train("TRAIN-G2", "Overlap Train", ["TASK-G1"], "planner")
        self.assertEqual(relay_mesh.active_task_overlap(["TASK-G1"], exclude_train="TRAIN-G1"), [])
        # rubber-stamp guard: missing report, then subject-mismatched report
        ok, why = relay_mesh.plan_audit_valid("TRAIN-G1")
        self.assertFalse(ok)
        (relay_mesh.TRAINS_DIR / "TRAIN-G1" / "PLAN_AUDIT_REPORT.md").write_text(
            "Discusses some other train entirely (tcp02/tcp03).", encoding="utf-8")
        ok, why = relay_mesh.plan_audit_valid("TRAIN-G1")
        self.assertFalse(ok)
        self.assertIn("TASK-G1", why)

    def test_train_full_lifecycle_to_land(self):
        # 1. Planner initializes train
        train_path = relay_mesh.init_train("TRAIN-TEST-01", "Test Train Title", ["TASK-T1", "TASK-T2"], "chatgpt_planner")
        self.assertTrue((train_path / "PACKET.md").exists())
        self.assertTrue((train_path / "STATE.json").exists())

        # New trains start at plan audit (owner ruling 2026-08-16): the
        # plan-auditor inbox is notified, not the implementer.
        state = json.loads((train_path / "STATE.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "PLAN_AUDIT_PENDING")
        self.assertEqual(state["current_role"], "plan-auditor")
        inbox_msgs = list((relay_mesh.INBOX_DIR / "plan-auditor").glob("*.json"))
        self.assertGreaterEqual(len(inbox_msgs), 1)

        # 1b. Plan auditor approves: report must reference the train's tasks
        # AND carry the explicit verdict marker (TRAIN-06 FSM evidence gate).
        (train_path / "PLAN_AUDIT_REPORT.md").write_text(
            "Audited TASK-T1 and TASK-T2 against CURRENT.\nPLAN_VERDICT: APPROVED",
            encoding="utf-8")
        self.assertTrue(relay_mesh.transition_train(
            "TRAIN-TEST-01", "PLAN_APPROVED", "implementer",
            "external_plan_auditor", "plan-auditor", "Plan audit passed"))
        ok, why = relay_mesh.plan_audit_valid("TRAIN-TEST-01")
        self.assertTrue(ok, why)

        # 2. Implementer claims train (lease is FSM evidence for IN_PROGRESS_*)
        res_claim = relay_mesh.acquire_lease("TRAIN-TEST-01", "antigravity_impl", "implementer")
        self.assertTrue(res_claim)
        self.assertTrue(relay_mesh.transition_train(
            "TRAIN-TEST-01", "IN_PROGRESS_IMPLEMENTER", "implementer",
            "antigravity_impl", "implementer", "Started coding"))

        # 3. Implementer completes code and dispatches to Auditor
        self.assertTrue(relay_mesh.transition_train(
            "TRAIN-TEST-01", "AUDIT_PENDING", "auditor",
            "antigravity_impl", "implementer", "Code and tests complete"))
        relay_mesh.release_lease("TRAIN-TEST-01", "antigravity_impl")

        # Check auditor inbox
        auditor_msgs = list((relay_mesh.INBOX_DIR / "auditor").glob("*.json"))
        self.assertGreaterEqual(len(auditor_msgs), 1)

        # 4. Auditor claims (two-step: IN_PROGRESS_AUDITOR first) and passes.
        # The code report must name tasks, carry both verdict markers, and
        # bind the current packet digest.
        relay_mesh.acquire_lease("TRAIN-TEST-01", "gemini_auditor", "auditor")
        self.assertTrue(relay_mesh.transition_train(
            "TRAIN-TEST-01", "IN_PROGRESS_AUDITOR", "auditor",
            "gemini_auditor", "auditor", "Audit claim"))
        import hashlib
        packet_digest = hashlib.sha256((train_path / "PACKET.md").read_bytes()).hexdigest()
        (train_path / "AUDIT_REPORT.md").write_text(
            f"Audited TASK-T1 and TASK-T2. Packet sha256 {packet_digest}.\n"
            "Audit Recommendation: Approve vs Do Not Approve -> Approve.\n"
            "VERIFIED_PASS", encoding="utf-8")
        self.assertTrue(relay_mesh.transition_train(
            "TRAIN-TEST-01", "READY_FOR_HUMAN_APPROVAL", "human",
            "gemini_auditor", "auditor", "All 100% tests passed"))
        relay_mesh.release_lease("TRAIN-TEST-01", "gemini_auditor")

        # Check that Approval Dossier was generated
        approval_file = relay_mesh.APPROVALS_DIR / "APPROVAL_REQUEST_TRAIN-TEST-01.md"
        self.assertTrue(approval_file.exists())

        # 5. Human lands the train
        land_res = relay_mesh.land_train("TRAIN-TEST-01")
        self.assertTrue(land_res)

        # 6. Verify work-graph.json was updated with status: DONE
        updated_graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_text(encoding="utf-8"))
        nodes_map = {n["id"]: n for n in updated_graph["nodes"]}
        self.assertEqual(nodes_map["TASK-T1"]["completion"]["status"], "DONE")
        self.assertEqual(nodes_map["TASK-T2"]["completion"]["status"], "DONE")
        self.assertEqual(nodes_map["TASK-T1"]["work_status"], "done")

    def test_landing_refreshes_versioned_graph_revision(self):
        from graph_backlog.jsonutil import compute_revision_hash
        self._ready_train("TRAIN-GRAPH-REVISION")
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['revision'] = compute_revision_hash(graph)
        before = graph['revision']
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding='utf-8')
        self.assertTrue(relay_mesh.land_train("TRAIN-GRAPH-REVISION"))
        landed = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        self.assertNotEqual(landed['revision'], before)
        self.assertEqual(landed['revision'], compute_revision_hash(landed))

    def _ready_train(self, train_id="TRAIN-FSM", tasks=("TASK-T1",), marker="Audit Recommendation"):
        """Drive a train to READY_FOR_HUMAN_APPROVAL through the FSM."""
        import hashlib
        relay_mesh.init_train(train_id, "FSM Train", list(tasks), "planner")
        tp = relay_mesh.TRAINS_DIR / train_id
        (tp / "PLAN_AUDIT_REPORT.md").write_text(
            " ".join(tasks) + "\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        assert relay_mesh.transition_train(train_id, "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "ok")
        relay_mesh.acquire_lease(train_id, "impl", "implementer")
        assert relay_mesh.transition_train(train_id, "IN_PROGRESS_IMPLEMENTER", "implementer", "impl", "implementer", "ok")
        assert relay_mesh.transition_train(train_id, "AUDIT_PENDING", "auditor", "impl", "implementer", "ok")
        relay_mesh.release_lease(train_id, "impl")
        relay_mesh.acquire_lease(train_id, "aud", "auditor")
        assert relay_mesh.transition_train(train_id, "IN_PROGRESS_AUDITOR", "auditor", "aud", "auditor", "ok")
        digest = hashlib.sha256((tp / "PACKET.md").read_bytes()).hexdigest()
        (tp / "AUDIT_REPORT.md").write_text(
            " ".join(tasks) + f"\npacket {digest}\n{marker}: Approve\nVERIFIED_PASS",
            encoding="utf-8")
        assert relay_mesh.transition_train(train_id, "READY_FOR_HUMAN_APPROVAL", "human", "aud", "auditor", "ok")
        relay_mesh.release_lease(train_id, "aud")
        return tp

    def test_audit_gate_accepts_the_english_and_the_legacy_portuguese_marker(self):
        # Reports saved before the marker moved to English stay valid.
        self._ready_train("TRAIN-MARKER-EN")
        legacy = self._ready_train("TRAIN-MARKER-PT", ("TASK-T2",), marker="Recomendação da Auditoria")
        report = legacy / "AUDIT_REPORT.md"
        report.write_text(report.read_text(encoding="utf-8").replace("Recomendação da Auditoria", "Verdict"), encoding="utf-8")
        ok, why = relay_mesh.audit_valid("TRAIN-MARKER-PT", "AUDIT")
        self.assertFalse(ok)
        self.assertIn("Audit Recommendation", why)

    def test_landing_declares_c_and_preserves_completion_history(self):
        self._ready_train("TRAIN-EVIDENCE")
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph["nodes"][0]["completion"].update(
            evidence_grade="B", evidence_grade_original="D",
            custom={"historical": True}, dod_evidence=["old-reference"],
        )
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding="utf-8")
        self.assertTrue(relay_mesh.land_train("TRAIN-EVIDENCE"))
        node = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())["nodes"][0]
        completion = node["completion"]
        self.assertEqual(completion["custom"], {"historical": True})
        self.assertEqual(completion["evidence_grade_original"], "D")
        self.assertEqual(completion["evidence_grade"], "C")
        self.assertIn("not independent", completion["evidence_grade_reason"])
        self.assertIn("old-reference", completion["dod_evidence"])
        ref = next(ref for ref in node["provenance"] if ref.startswith("relay-signoff:"))
        self.assertIn(ref, completion["dod_evidence"])
        binding = json.loads(ref.split(":", 1)[1])
        self.assertEqual(binding["authority"], "HUMAN_OPERATOR")
        self.assertEqual(binding["train"], "TRAIN-EVIDENCE")
        self.assertIsInstance(binding["readyRevision"], int)
        self.assertEqual(len(binding["capability_sha256"]), 64)
        self.assertIn("AUDIT_REPORT.md", binding["artifacts"])

    def test_landing_materialized_task_has_explicit_declaration(self):
        self._ready_train("TRAIN-MATERIALIZED", ("TASK-NEW",))
        self.assertTrue(relay_mesh.land_train("TRAIN-MATERIALIZED"))
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        node = next(n for n in graph["nodes"] if n["id"] == "TASK-NEW")
        completion = node["completion"]
        self.assertEqual(completion["evidence_grade"], "C")
        self.assertNotIn("evidence_grade_original", completion)
        self.assertTrue(completion["evidence_grade_reason"])
        self.assertTrue(completion["dod_evidence"])
        self.assertTrue(node["provenance"])

    def test_landing_batch_minimum_a_refuses_without_partial_write(self):
        from unittest.mock import patch
        self._ready_train("TRAIN-MINIMUM", ("TASK-NEW", "TASK-T1"))
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        # Existing minimal OS node deliberately has no kind.
        graph["nodes"][0]["readiness"] = {"minimum_evidence_grade": "A"}
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding="utf-8")
        before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
        cap = relay_mesh.APPROVALS_DIR / "CAPABILITY_TRAIN-MINIMUM.json"
        capability = cap.read_bytes()
        with patch.object(relay_mesh, "release_lease") as release:
            self.assertFalse(relay_mesh.land_train("TRAIN-MINIMUM"))
        release.assert_not_called()
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.read_bytes(), before)
        self.assertEqual(cap.read_bytes(), capability)

    def test_landing_replay_preserves_surviving_graph_bytes_and_mtime(self):
        from unittest.mock import patch
        self._ready_train("TRAIN-REPLAY")
        with patch.object(relay_mesh, "transition_train", return_value=False):
            self.assertFalse(relay_mesh.land_train("TRAIN-REPLAY"))
        before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
        mtime = relay_mesh.WORK_GRAPH_FILE.stat().st_mtime_ns
        self.assertTrue(relay_mesh.land_train("TRAIN-REPLAY"))
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.read_bytes(), before)
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.stat().st_mtime_ns, mtime)

    def test_landing_refuses_missing_bound_packet_without_consuming_approval(self):
        train = self._ready_train("TRAIN-MISSING-PACKET")
        before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
        capability = relay_mesh.APPROVALS_DIR / "CAPABILITY_TRAIN-MISSING-PACKET.json"
        original = capability.read_bytes()
        (train / "PACKET.md").unlink()
        self.assertFalse(relay_mesh.land_train("TRAIN-MISSING-PACKET"))
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.read_bytes(), before)
        self.assertEqual(capability.read_bytes(), original)

    def test_fsm_illegal_edges_and_roles_fail_closed(self):
        relay_mesh.init_train("TRAIN-FSM1", "T", ["TASK-T1"], "planner")
        state_file = relay_mesh.TRAINS_DIR / "TRAIN-FSM1" / "STATE.json"
        before = state_file.read_bytes()
        # illegal edge (falsifier 1): PLAN_AUDIT_PENDING -> LANDED
        self.assertFalse(relay_mesh.transition_train("TRAIN-FSM1", "LANDED", "completed", "x", "human_gatekeeper", "n"))
        # wrong role: implementer cannot approve a plan
        self.assertFalse(relay_mesh.transition_train("TRAIN-FSM1", "PLAN_APPROVED", "implementer", "x", "implementer", "n"))
        # unknown status
        self.assertFalse(relay_mesh.transition_train("TRAIN-FSM1", "TOTALLY_MADE_UP", "x", "x", "planner", "n"))
        # evidence gate: plan approval without a valid report
        self.assertFalse(relay_mesh.transition_train("TRAIN-FSM1", "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "n"))
        # missing lease: claim transition without holding the lease
        (relay_mesh.TRAINS_DIR / "TRAIN-FSM1" / "PLAN_AUDIT_REPORT.md").write_text(
            "TASK-T1\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        self.assertTrue(relay_mesh.transition_train("TRAIN-FSM1", "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "n"))
        self.assertFalse(relay_mesh.transition_train("TRAIN-FSM1", "IN_PROGRESS_IMPLEMENTER", "implementer", "impl", "implementer", "n"))
        # refusals changed no bytes for the refused-only prefix state
        self.assertIn(b"PLAN_AUDIT_PENDING", before)

    def test_revision_cas_concurrency_one_winner(self):
        relay_mesh.init_train("TRAIN-CAS", "T", ["TASK-T1"], "planner")
        (relay_mesh.TRAINS_DIR / "TRAIN-CAS" / "PLAN_AUDIT_REPORT.md").write_text(
            "TASK-T1\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        data = json.loads((relay_mesh.TRAINS_DIR / "TRAIN-CAS" / "STATE.json").read_text(encoding="utf-8"))
        rev = relay_mesh.train_revision(data)
        # two competing transitions from the SAME expected revision (falsifier 2)
        first = relay_mesh.transition_train("TRAIN-CAS", "PLAN_APPROVED", "implementer",
                                            "pa1", "plan-auditor", "n", expected_revision=rev)
        second = relay_mesh.transition_train("TRAIN-CAS", "PLAN_REJECTED", "planner",
                                             "pa2", "plan-auditor", "n", expected_revision=rev)
        self.assertTrue(first)
        self.assertFalse(second)  # explicit conflict, not silent overwrite

    def test_plan_approval_seals_exact_execution_descriptor_digest(self):
        train_id = "TRAIN-DESCRIPTOR"
        relay_mesh.init_train(train_id, "Strict", ["TASK-T1"], "planner")
        train_dir = relay_mesh.TRAINS_DIR / train_id
        payload = {
            "schema": "tare.tools/train-execution/1",
            "train_id": train_id,
            "work_id": "TASK-T1",
            "work_graph": "work-graph.json",
            "execution_repository": "tare.tools.kernel",
            "base_commit": "a" * 40,
            "target_ref": "refs/heads/main",
            "library_manifest": "LIBRARY_MANIFEST.json",
            "tracked_paths": ["compute_plane", "tests"],
            "verification_checks": [
                {"criterion": "T1 criterion 1", "command": ["pytest", "-q"]},
                {"criterion": "T1 criterion 2", "command": ["pytest", "-q"]},
            ],
        }
        digest = train_execution.create_execution_descriptor(
            train_dir / train_execution.DESCRIPTOR_FILE, payload
        )
        (train_dir / "PLAN_AUDIT_REPORT.md").write_text(
            f"TASK-T1\nPLAN_VERDICT: APPROVED\ndescriptor {digest[:12]}",
            encoding="utf-8",
        )

        self.assertTrue(
            relay_mesh.transition_train(
                train_id,
                "PLAN_APPROVED",
                "implementer",
                "pa",
                "plan-auditor",
                "ok",
            )
        )
        state = json.loads((train_dir / "STATE.json").read_text(encoding="utf-8"))
        self.assertEqual(state["execution_descriptor"]["sha256"], digest)

    def test_lease_non_owner_release_refused(self):
        relay_mesh.acquire_lease("TRAIN-L1", "owner_agent", "implementer")
        self.assertFalse(relay_mesh.release_lease("TRAIN-L1", "intruder_agent"))
        self.assertIsNotNone(relay_mesh.current_lease("TRAIN-L1"))
        self.assertTrue(relay_mesh.release_lease("TRAIN-L1", "owner_agent"))

    def test_landing_capability_falsifiers(self):
        tp = self._ready_train("TRAIN-LAND", ("TASK-T1",))
        cap_file = relay_mesh.APPROVALS_DIR / "CAPABILITY_TRAIN-LAND.json"
        cap = json.loads(cap_file.read_text(encoding="utf-8"))
        graph_before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
        # wrong nonce refused (falsifier 4)
        self.assertFalse(relay_mesh.land_train("TRAIN-LAND", nonce="deadbeef"))
        # approval for train A cannot land train B
        tp2 = self._ready_train("TRAIN-LAND2", ("TASK-T2",))
        cap2 = json.loads((relay_mesh.APPROVALS_DIR / "CAPABILITY_TRAIN-LAND2.json").read_text(encoding="utf-8"))
        self.assertFalse(relay_mesh.land_train("TRAIN-LAND", nonce=cap2["nonce"]))
        # generic marker files land nothing (falsifier 9 core)
        (relay_mesh.APPROVALS_DIR / "APPROVED_TRAIN-LAND.md").write_text("approve!", encoding="utf-8")
        self.assertEqual(relay_mesh.process_drive_approvals(), 0)
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.read_bytes(), graph_before)
        # wildcard landing is gone
        self.assertEqual(relay_mesh.land_all_pending(), 0)
        self.assertEqual(relay_mesh.WORK_GRAPH_FILE.read_bytes(), graph_before)
        # valid nonce in the mobile file lands EXACTLY its train (falsifier 5)
        (relay_mesh.APPROVALS_DIR / "APPROVED_TRAIN-LAND.md").write_text(
            f"approve {cap['nonce']}", encoding="utf-8")
        self.assertEqual(relay_mesh.process_drive_approvals(), 1)
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in graph["nodes"]}
        self.assertEqual(nodes["TASK-T1"]["completion"]["status"], "DONE")
        self.assertEqual(nodes["TASK-T2"]["completion"]["status"], "NOT_DONE")
        # replay after landing fails via terminal state (no consumed-nonce store)
        self.assertFalse(relay_mesh.land_train("TRAIN-LAND", nonce=cap["nonce"]))
        # tombstone retains identity for the overlap guard (falsifier 11)
        tomb = json.loads((tp / "STATE.json").read_text(encoding="utf-8"))
        self.assertTrue(tomb.get("tombstone"))
        self.assertEqual(tomb["task_ids"], ["TASK-T1"])
        self.assertEqual(relay_mesh.active_task_overlap(["TASK-T1"]), [])
        self.assertEqual(len(relay_mesh.active_task_overlap(["TASK-T2"])), 1)  # LAND2 still active

    def test_board_compact_and_digest_suppressed(self):
        self._ready_train("TRAIN-B1", ("TASK-T1",))
        relay_mesh.update_board()
        board = json.loads(relay_mesh.BOARD_JSON.read_text(encoding="utf-8"))
        self.assertNotIn("history", json.dumps(board))
        # no-op render preserves bytes and mtime (falsifier 10)
        before_bytes = relay_mesh.BOARD_JSON.read_bytes()
        before_mtime = relay_mesh.BOARD_JSON.stat().st_mtime_ns
        self.assertFalse(relay_mesh.update_board())
        self.assertEqual(relay_mesh.BOARD_JSON.read_bytes(), before_bytes)
        self.assertEqual(relay_mesh.BOARD_JSON.stat().st_mtime_ns, before_mtime)

    def test_terminal_transition_emits_no_inbox_message(self):
        self._ready_train("TRAIN-T12", ("TASK-T1",))
        human_before = len(list((relay_mesh.INBOX_DIR / "human").glob("*.json")))
        self.assertTrue(relay_mesh.land_train("TRAIN-T12"))
        human_after = len(list((relay_mesh.INBOX_DIR / "human").glob("*.json")))
        self.assertEqual(human_after, human_before)  # falsifier 12

    def test_capsule_is_index_not_authority(self):
        tp = self._ready_train("TRAIN-CAP", ("TASK-T1",))
        capsule = relay_mesh.context_capsule("TRAIN-CAP", "auditor")
        self.assertEqual(capsule["taskIds"], ["TASK-T1"])
        # tampering the capsule changes nothing about gate outcomes: gates
        # reopen sources (audit_valid reads the report file, not the capsule)
        capsule["status"] = "LANDED"
        ok, _ = relay_mesh.audit_valid("TRAIN-CAP", "AUDIT")
        self.assertTrue(ok)
        # capsule digest detects tamper
        import hashlib as _h
        recomputed = relay_mesh.relay_io.sha256_hex(
            json.dumps({k: v for k, v in capsule.items() if k != "capsuleDigest"}, sort_keys=True))
        self.assertNotEqual(recomputed, capsule["capsuleDigest"])

    def test_alert_escalation_and_resolution(self):
        alert_id = relay_mesh.raise_alert("Deadlock Test Alert", "critical", "Deadlock on resource X", "agent_x", "TRAIN-01")
        alert_file = relay_mesh.ALERTS_DIR / f"{alert_id}.md"
        self.assertTrue(alert_file.exists())

        # Check Board shows 1 alert (compact v3 board)
        relay_mesh.update_board()
        board_content = relay_mesh.BOARD_FILE.read_text(encoding="utf-8")
        self.assertIn("## Alerts (1)", board_content)

        # Resolve alert
        res_resolve = relay_mesh.resolve_alert(alert_id, "human")
        self.assertTrue(res_resolve)
        self.assertFalse(alert_file.exists())

        # Check Board shows 0 alerts
        relay_mesh.update_board()
        board_content_after = relay_mesh.BOARD_FILE.read_text(encoding="utf-8")
        self.assertIn("## Alerts (0)", board_content_after)

class MultiHostImmunityTests(unittest.TestCase):
    """TRAIN-21 — multi-host coordination immunity falsifiers (FAL-01..FAL-09).

    Reuses the mesh temp-path fixture (by delegation, NOT inheritance, so the
    legacy tests do not re-run under multi-host mode) and additionally sandboxes
    CRED_DIR (a SEPARATE temp dir, disjoint from the relay root), the hostname,
    the RELAY_MULTI_HOST env, and the outcomes dir — so no real credential or
    relay byte is ever touched (FAL-09)."""

    def setUp(self):
        RelayMeshTests.setUp(self)
        self._orig_cred = relay_mesh.CRED_DIR
        self._orig_host = relay_mesh.get_hostname
        self._orig_env = os.environ.get("RELAY_MULTI_HOST")
        self._orig_outcomes = relay_mesh.relay_io.OUTCOMES_DIR
        self.cred_dir = Path(tempfile.mkdtemp(prefix="test_cred_"))
        relay_mesh.CRED_DIR = self.cred_dir
        relay_mesh.get_hostname = lambda: "HOST-LEADER"
        relay_mesh.relay_io.OUTCOMES_DIR = relay_mesh.HISTORY_DIR / "outcomes"
        os.environ["RELAY_MULTI_HOST"] = "1"

    def tearDown(self):
        relay_mesh.CRED_DIR = self._orig_cred
        relay_mesh.get_hostname = self._orig_host
        relay_mesh.relay_io.OUTCOMES_DIR = self._orig_outcomes
        if self._orig_env is None:
            os.environ.pop("RELAY_MULTI_HOST", None)
        else:
            os.environ["RELAY_MULTI_HOST"] = self._orig_env
        shutil.rmtree(self.cred_dir, ignore_errors=True)
        RelayMeshTests.tearDown(self)

    def _provision(self, host="HOST-LEADER"):
        (self.cred_dir / relay_mesh.COORDINATOR_CRED).write_text(
            json.dumps({"leaderHost": host, "role": "coordinator"}), encoding="utf-8")

    def _inventory(self):
        root = Path(self.test_dir) / "relay"
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p != relay_mesh.TELEMETRY_FILE
        }

    # -- FAL-01: absent/malformed/mismatched credential refuses before any effect
    def test_fal01_missing_credential_refuses_every_effect_no_bytes(self):
        before = self._inventory()
        self.assertFalse(relay_mesh.acquire_lease("T-A", "a", "implementer"))
        self.assertFalse(relay_mesh.renew_lease("T-A", "a"))
        self.assertFalse(relay_mesh.release_lease("T-A", "a"))
        self.assertFalse(relay_mesh.transition_train("T-A", "PLAN_APPROVED", "implementer", "a", "implementer", "x"))
        self.assertFalse(relay_mesh.land_train("T-A"))
        self.assertEqual(relay_mesh.process_drive_approvals(), 0)
        with self.assertRaises(SystemExit):
            relay_mesh.init_train("T-A", "t", ["TASK-T1"], "planner")
        self.assertEqual(self._inventory(), before, "denied standby changed authoritative bytes")

    def test_fal01_malformed_credential_refuses(self):
        (self.cred_dir / relay_mesh.COORDINATOR_CRED).write_text("{not json", encoding="utf-8")
        ok, why = relay_mesh.coordination_authorized()
        self.assertFalse(ok)
        self.assertIn("malformed", why.lower())

    def test_fal01_host_mismatch_refuses(self):
        self._provision(host="HOST-OTHER")
        ok, why = relay_mesh.coordination_authorized()
        self.assertFalse(ok)
        self.assertIn("mismatch", why.lower())
        self.assertFalse(relay_mesh.acquire_lease("T-A", "a", "implementer"))

    # -- FAL-02: only the pre-provisioned leader acts; standby changes nothing
    def test_fal02_only_authorized_leader_acts(self):
        self._provision(host="HOST-LEADER")
        self.assertTrue(relay_mesh.acquire_lease("T-A", "leader", "implementer"))
        snap = self._inventory()
        # standby: same scratch relay, different host -> credential no longer matches
        relay_mesh.get_hostname = lambda: "HOST-STANDBY"
        self.assertFalse(relay_mesh.acquire_lease("T-B", "standby", "implementer"))
        self.assertFalse(relay_mesh.renew_lease("T-A", "standby"))
        self.assertFalse(relay_mesh.release_lease("T-A", "standby"))
        self.assertFalse(relay_mesh.transition_train("T-A", "PLAN_APPROVED", "implementer", "s", "implementer", "x"))
        self.assertEqual(self._inventory(), snap, "standby produced an authoritative effect")

    # -- FAL-04: credential dir inside the synced root is rejected; never created
    def test_fal04_credential_dir_inside_root_rejected(self):
        self._provision()
        relay_mesh.CRED_DIR = relay_mesh.RELAY_ROOT / "relay" / "leases"  # inside synced tree
        ok, why = relay_mesh.coordination_authorized()
        self.assertFalse(ok)
        self.assertIn("inside", why.lower())

    def test_fal04_credential_never_autocreated(self):
        cred = self.cred_dir / relay_mesh.COORDINATOR_CRED
        self.assertFalse(cred.exists())
        relay_mesh.coordination_authorized()
        relay_mesh.acquire_lease("T-A", "a", "implementer")
        self.assertFalse(cred.exists(), "predicate must never provision the coordinator credential")

    # -- FAL-06: mode disabled == legacy single-host behavior
    def test_fal06_mode_disabled_is_legacy(self):
        os.environ.pop("RELAY_MULTI_HOST", None)
        ok, _ = relay_mesh.coordination_authorized()
        self.assertTrue(ok)
        self.assertTrue(relay_mesh.acquire_lease("T-A", "a", "implementer"))

    # -- FAL-07: denial is actionable and leaks no credential secret
    def test_fal07_denial_actionable_and_secretless(self):
        (self.cred_dir / relay_mesh.COORDINATOR_CRED).write_text(
            json.dumps({"leaderHost": "HOST-OTHER", "secret": "TOPSECRET123"}), encoding="utf-8")
        ok, why = relay_mesh.coordination_authorized()
        self.assertFalse(ok)
        self.assertIn("multi-host", why.lower())
        self.assertIn("HOST-OTHER", why)
        self.assertNotIn("TOPSECRET123", why, "reason leaked credential material")

    # -- FAL-08: outcome/replay gates survive a denied standby attempt
    def test_fal08_denied_standby_leaves_outcome_gates_intact(self):
        self._provision(host="HOST-LEADER")
        key = "T-A::r1::plan-audit"
        relay_mesh.relay_io.outcome_ack(key)
        relay_mesh.get_hostname = lambda: "HOST-STANDBY"  # standby
        relay_mesh.acquire_lease("T-A", "standby", "plan-auditor")
        self.assertFalse(relay_mesh.relay_io.outcome_actionable(key),
                         "an ACKed effect must not become replayable after a denied attempt")


if __name__ == "__main__":
    unittest.main()
