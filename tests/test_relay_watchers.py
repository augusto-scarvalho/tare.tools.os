"""TRAIN-06 watcher/message-plane falsifiers (Seam B)."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))

from relay import relay_mesh, resource_telemetry, watcher_daemon
from relay import watcher as role_watcher
from relay import watcher_notifier
import relay_io


class RelayWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_watchers_"))
        self.orig_nodes_dir = resource_telemetry.NODES_DIR
        resource_telemetry.NODES_DIR = self.tmp / "relay" / "nodes"
        telemetry_patch = patch.object(
            resource_telemetry,
            "broadcast_heartbeat",
            return_value=None,
        )
        telemetry_patch.start()
        self.addCleanup(telemetry_patch.stop)
        frontier_patch = patch.object(
            watcher_daemon,
            "_top_frontier",
            return_value=None,
        )
        frontier_patch.start()
        self.addCleanup(frontier_patch.stop)
        relay_mesh.rebind_root(self.tmp)
        relay_mesh.ensure_dirs()
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(
            {"nodes": [{"id": "TASK-W1", "completion": {"status": "NOT_DONE"}}], "edges": []}),
            encoding="utf-8")
        # point the daemon module at the same sandbox
        watcher_daemon.RELAY_DIR = self.tmp / "relay"
        watcher_daemon.HEARTBEAT_FILE = self.tmp / "relay" / "history" / "daemon-heartbeat.json"
        watcher_daemon.LOG_FILE = self.tmp / "relay" / "watcher.log"

    def tearDown(self):
        resource_telemetry.NODES_DIR = self.orig_nodes_dir
        relay_mesh.rebind_root(Path(relay_mesh.__file__).resolve().parent.parent)
        importlib.reload(watcher_daemon)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_poll_floors_are_15s_or_more(self):
        self.assertGreaterEqual(watcher_daemon.TICK_SECONDS, 15)
        self.assertGreaterEqual(role_watcher.MIN_POLL_SECONDS, 15)
        self.assertGreaterEqual(watcher_notifier.MIN_POLL_SECONDS, 15)

    def test_bom_json_accepted_by_canonical_reader(self):
        p = self.tmp / "bom.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode())
        self.assertEqual(relay_io.read_json(p), {"ok": True})

    def test_malformed_message_quarantined_once_after_three_attempts(self):
        box = relay_mesh.INBOX_DIR / "implementer"
        bad = box / "MSG_bad_implementer.json"
        bad.write_text("{not json", encoding="utf-8")
        failures = {}
        for _ in range(3):
            watcher_daemon.sweep_inboxes(failures)
        quarantine = box / "quarantine"
        self.assertFalse(bad.exists())
        self.assertTrue((quarantine / bad.name).exists())
        self.assertTrue((quarantine / (bad.name + ".error.txt")).exists())
        # falsifier 7: 30 further ticks add nothing
        before = sorted(p.name for p in quarantine.iterdir())
        for _ in range(30):
            watcher_daemon.sweep_inboxes(failures)
        self.assertEqual(sorted(p.name for p in quarantine.iterdir()), before)

    def test_good_message_moves_to_processed_and_protected_inboxes_untouched(self):
        box = relay_mesh.INBOX_DIR / "implementer"
        good = box / "MSG_good_implementer.json"
        good.write_text(json.dumps({"id": "x", "subject": "s", "payload": {}}), encoding="utf-8")
        human_box = relay_mesh.INBOX_DIR / "human"
        human_msg = human_box / "MSG_h_human.json"
        human_msg.write_text(json.dumps({"id": "h"}), encoding="utf-8")
        watcher_daemon.sweep_inboxes({})
        self.assertTrue((box / "processed" / good.name).exists())
        self.assertFalse(good.exists())
        self.assertTrue(human_msg.exists(), "human inbox must never be auto-consumed")

    def test_outcome_dedup_prevents_duplicate_effects(self):
        key = "TRAIN-X::r3::plan-audit"
        self.assertTrue(relay_io.outcome_actionable(key))
        relay_io.outcome_ack(key)
        self.assertFalse(relay_io.outcome_actionable(key), "replay must not re-run an ACKed effect")
        # retry path: bounded backoff then quarantine
        key2 = "TRAIN-X::r3::code-audit"
        for _ in range(relay_io.MAX_ATTEMPTS):
            record = relay_io.outcome_retry(key2, "boom")
        self.assertEqual(record["state"], "QUARANTINED")
        self.assertFalse(relay_io.outcome_actionable(key2))

    def test_reconcile_holds_without_llm_critic_and_notifies_once(self):
        # a PLAN_AUDIT_PENDING train exists BEFORE the daemon looks (level trigger)
        relay_mesh.init_train("TRAIN-W1", "W", ["TASK-W1"], "planner")
        alerts_before = len(list(relay_mesh.ALERTS_DIR.glob("*.md")))
        import os
        os.environ.pop("RELAY_PLAN_AUDIT_CMD", None)
        for _ in range(3):
            watcher_daemon.reconcile()
        state = relay_io.read_json(relay_mesh.TRAINS_DIR / "TRAIN-W1" / "STATE.json")
        self.assertEqual(state["status"], "PLAN_AUDIT_PENDING", "no critic -> gate stays pending")
        alerts_after = len(list(relay_mesh.ALERTS_DIR.glob("*.md")))
        self.assertEqual(alerts_after, alerts_before + 1, "exactly ONE deduplicated hold alert")

    def test_supervisor_duplicate_guard(self):
        relay_io.write_json_atomic(watcher_daemon.HEARTBEAT_FILE, {
            "version": "3.0", "pid": 999999, "host": "other-node",
            "lastScan": time.time()})
        self.assertTrue(watcher_daemon.another_supervisor_live())
        # stale heartbeat is not a live supervisor
        relay_io.write_json_atomic(watcher_daemon.HEARTBEAT_FILE, {
            "version": "3.0", "pid": 999999, "host": relay_mesh.get_hostname(),
            "lastScan": time.time() - 600})
        self.assertFalse(watcher_daemon.another_supervisor_live())

    def test_idle_ticks_make_no_board_writes(self):
        relay_mesh.update_board()
        before = relay_mesh.BOARD_JSON.stat().st_mtime_ns
        for _ in range(30):
            watcher_daemon.sweep_inboxes({})
            # reconcile with nothing actionable
            watcher_daemon.reconcile()
        self.assertEqual(relay_mesh.BOARD_JSON.stat().st_mtime_ns, before,
                         "idle ticks must not rewrite derived state")

    def test_in_progress_auditor_level_recovery_no_self_transition_refusal(self):
        from unittest.mock import patch, MagicMock
        train_id = "TRAIN-REC1"
        relay_mesh.init_train(train_id, "Recovery Test", ["TASK-REC1"], "planner")
        tp = relay_mesh.TRAINS_DIR / train_id
        (tp / "PLAN_AUDIT_REPORT.md").write_text("TASK-REC1\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        relay_mesh.transition_train(train_id, "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "ok")
        relay_mesh.acquire_lease(train_id, "impl", "implementer")
        relay_mesh.transition_train(train_id, "IN_PROGRESS_IMPLEMENTER", "implementer", "impl", "implementer", "ok")
        relay_mesh.transition_train(train_id, "AUDIT_PENDING", "auditor", "impl", "implementer", "ok")
        relay_mesh.release_lease(train_id, "impl")

        # First claim to IN_PROGRESS_AUDITOR
        relay_mesh.acquire_lease(train_id, "aud", "auditor")
        relay_mesh.transition_train(train_id, "IN_PROGRESS_AUDITOR", "auditor", "aud", "auditor", "claim")
        relay_mesh.release_lease(train_id, "aud")

        # Now train is in IN_PROGRESS_AUDITOR with no lease (simulating crash/retry).
        # Configure RELAY_CODE_AUDIT_CMD to write a passing report and exit 0.
        import os, hashlib
        digest = hashlib.sha256((tp / "PACKET.md").read_bytes()).hexdigest()
        audit_report = tp / "AUDIT_REPORT.md"
        audit_report.write_text(f"Audited TASK-REC1. sha256 {digest}.\nAudit Recommendation: Approve\nVERIFIED_PASS", encoding="utf-8")
        os.environ["RELAY_CODE_AUDIT_CMD"] = f"{sys.executable} -c \"exit(0)\""

        # Mock the mechanical pytest check to avoid recursive test execution
        orig_run = subprocess.run
        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "pytest" in cmd:
                m = MagicMock()
                m.returncode = 0
                m.stdout = "mocked pytest pass"
                return m
            return orig_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            rev = relay_mesh.train_revision(relay_io.read_json(tp / "STATE.json"))
            ok = watcher_daemon.run_code_audit(train_id, rev)
            self.assertTrue(ok)
            state = relay_io.read_json(tp / "STATE.json")
            self.assertEqual(state["status"], "READY_FOR_HUMAN_APPROVAL")

    def test_code_audit_quarantine_transitions_to_audit_hold(self):
        from unittest.mock import patch, MagicMock
        train_id = "TRAIN-QUAR1"
        relay_mesh.init_train(train_id, "Quarantine Test", ["TASK-QUAR1"], "planner")
        tp = relay_mesh.TRAINS_DIR / train_id
        (tp / "PLAN_AUDIT_REPORT.md").write_text("TASK-QUAR1\nPLAN_VERDICT: APPROVED", encoding="utf-8")
        relay_mesh.transition_train(train_id, "PLAN_APPROVED", "implementer", "pa", "plan-auditor", "ok")
        relay_mesh.acquire_lease(train_id, "impl", "implementer")
        relay_mesh.transition_train(train_id, "IN_PROGRESS_IMPLEMENTER", "implementer", "impl", "implementer", "ok")
        relay_mesh.transition_train(train_id, "AUDIT_PENDING", "auditor", "impl", "implementer", "ok")
        relay_mesh.release_lease(train_id, "impl")

        # Configure critic to fail (rc=1)
        import os
        os.environ["RELAY_CODE_AUDIT_CMD"] = f"{sys.executable} -c \"exit(1)\""
        rev = relay_mesh.train_revision(relay_io.read_json(tp / "STATE.json"))
        key = f"{train_id}::r{rev}::code-audit"

        orig_run = subprocess.run
        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "pytest" in cmd:
                m = MagicMock()
                m.returncode = 0
                m.stdout = "mocked pytest pass"
                return m
            return orig_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            for _ in range(relay_io.MAX_ATTEMPTS):
                # Advance retry timer if backoff was set
                rec = relay_io.outcome_get(key)
                if rec:
                    rec["nextRetry"] = 0
                    relay_io.write_json_atomic(relay_io._outcome_path(key), rec)
                watcher_daemon.run_code_audit(train_id, rev)

        state = relay_io.read_json(tp / "STATE.json")
        self.assertEqual(state["status"], "AUDIT_HOLD")
        self.assertEqual(relay_io.outcome_get(key)["state"], "QUARANTINED")


class MultiHostDaemonTests(unittest.TestCase):
    """TRAIN-21 — supervisor daemon multi-host immunity (FAL-02/03/05)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_mh_"))
        relay_mesh.rebind_root(self.tmp)
        relay_mesh.ensure_dirs()
        self.cred = Path(tempfile.mkdtemp(prefix="test_mh_cred_"))
        self._oc, relay_mesh.CRED_DIR = relay_mesh.CRED_DIR, self.cred
        self._oh, relay_mesh.get_hostname = relay_mesh.get_hostname, (lambda: "HOST-LEADER")
        self._oe = os.environ.get("RELAY_MULTI_HOST")
        os.environ["RELAY_MULTI_HOST"] = "1"
        watcher_daemon.RELAY_DIR = self.tmp / "relay"
        watcher_daemon.HEARTBEAT_FILE = self.tmp / "relay" / "history" / "daemon-heartbeat.json"
        watcher_daemon.LOG_FILE = self.tmp / "relay" / "watcher.log"

    def tearDown(self):
        relay_mesh.CRED_DIR = self._oc
        relay_mesh.get_hostname = self._oh
        if self._oe is None:
            os.environ.pop("RELAY_MULTI_HOST", None)
        else:
            os.environ["RELAY_MULTI_HOST"] = self._oe
        relay_mesh.rebind_root(Path(relay_mesh.__file__).resolve().parent.parent)
        importlib.reload(watcher_daemon)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.cred, ignore_errors=True)

    def _provision(self, host):
        (self.cred / relay_mesh.COORDINATOR_CRED).write_text(
            json.dumps({"leaderHost": host}), encoding="utf-8")

    def test_fal02_standby_writes_no_heartbeat(self):
        self._provision("HOST-OTHER")  # authorizes another host -> this host is standby
        watcher_daemon.heartbeat(0.0, 1)
        self.assertFalse(watcher_daemon.HEARTBEAT_FILE.exists(),
                         "standby host must not write daemon-heartbeat.json")

    def test_authorized_leader_writes_heartbeat(self):
        self._provision("HOST-LEADER")
        watcher_daemon.heartbeat(0.0, 1)
        self.assertTrue(watcher_daemon.HEARTBEAT_FILE.exists())

    def test_fal03_stale_heartbeat_grants_no_authority(self):
        self._provision("HOST-OTHER")  # no local credential authorizing us
        relay_io.write_json_atomic(watcher_daemon.HEARTBEAT_FILE,
                                   {"host": "HOST-LEADER", "lastScan": 0, "pid": 1})
        before = watcher_daemon.HEARTBEAT_FILE.read_bytes()
        watcher_daemon.heartbeat(time.time(), 5)  # denied -> must not overwrite
        self.assertEqual(watcher_daemon.HEARTBEAT_FILE.read_bytes(), before,
                         "a stale/foreign heartbeat must never confer effect authority")

    def test_fal02_standby_sweep_consumes_no_inbox(self):
        self._provision("HOST-OTHER")
        box = relay_mesh.INBOX_DIR / "implementer"
        msg = box / "MSG_x_implementer.json"
        msg.write_text(json.dumps({"id": "x", "subject": "s", "payload": {}}), encoding="utf-8")
        watcher_daemon.sweep_inboxes({})
        self.assertTrue(msg.exists(), "standby must not consume inbox items")
        self.assertFalse((box / "processed").exists())

    def test_authorized_leader_sweep_consumes_inbox(self):
        self._provision("HOST-LEADER")
        box = relay_mesh.INBOX_DIR / "implementer"
        msg = box / "MSG_y_implementer.json"
        msg.write_text(json.dumps({"id": "y", "subject": "s", "payload": {}}), encoding="utf-8")
        watcher_daemon.sweep_inboxes({})
        self.assertTrue((box / "processed" / msg.name).exists())


if __name__ == "__main__":
    unittest.main()

