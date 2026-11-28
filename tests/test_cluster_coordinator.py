"""
tests/test_cluster_coordinator.py — Unit & Concurrency Tests for Tailscale RPC Coordinator
========================================================================================
Validates SQLite durable state machine, claim atomicity, fencing tokens, and watchdog auto-reap.
"""
import json
import socketserver
import tempfile
import threading
import time
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from relay import coordinator_db as cluster_coordinator


class ClusterCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_coordinator.db"
        self.db = cluster_coordinator.CoordinatorDB(self.db_path)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_job_claim_atomicity_and_mutual_exclusion(self):
        """U-7D #2: Two simultaneous claim attempts yield exactly one winner."""
        train_id = "TRAIN-TEST-CONCURRENCY-01"
        results = []

        def _claim(host, agent):
            res = self.db.claim_job(train_id, "implementer", host, agent, ttl_s=60)
            results.append((host, res))

        t1 = threading.Thread(target=_claim, args=("host_a", "agent_a"))
        t2 = threading.Thread(target=_claim, args=("host_b", "agent_b"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for h, r in results if r is not None]
        conflicts = [r for h, r in results if r is None]

        self.assertEqual(len(successes), 1, "Exactly one host must win the lease")
        self.assertEqual(len(conflicts), 1, "The second host must receive None (conflict)")

        # Verify winner lease structure
        winner = successes[0]
        self.assertEqual(winner["train_id"], train_id)
        self.assertTrue(len(winner["lease_token"]) >= 16)
        self.assertEqual(winner["fence_seq"], 1)

    def test_job_renewal_and_release(self):
        """U-7D #1: Renew extends expiration; release clears active lease."""
        train_id = "TRAIN-TEST-LIFECYCLE-01"
        claim = self.db.claim_job(train_id, "implementer", "host_a", "agent_a", ttl_s=30)
        self.assertIsNotNone(claim)
        token = claim["lease_token"]

        # Renewal with correct token succeeds
        self.assertTrue(self.db.renew_job(train_id, token, ttl_s=120))

        # Renewal with invalid token fails
        self.assertFalse(self.db.renew_job(train_id, "invalid_token", ttl_s=120))

        # Release with correct token succeeds
        self.assertTrue(self.db.release_job(train_id, token))

        # Train is now unleased: another host can claim it
        claim_b = self.db.claim_job(train_id, "implementer", "host_b", "agent_b", ttl_s=60)
        self.assertIsNotNone(claim_b)
        self.assertEqual(claim_b["fence_seq"], 2, "Monotonic fence sequence must increment")

    def test_watchdog_reaps_expired_leases(self):
        """U-7D #3: Expired leases are automatically reaped and made available again."""
        train_id = "TRAIN-TEST-EXPIRED-01"
        # Claim with 0s TTL (immediately expired)
        claim = self.db.claim_job(train_id, "implementer", "host_a", "agent_a", ttl_s=-5)
        self.assertIsNotNone(claim)

        # Watchdog reap detects and clears expired lease
        reaped = self.db.reap_expired_leases()
        self.assertIn(train_id, reaped)

        # Host B can now claim immediately without conflict
        claim_b = self.db.claim_job(train_id, "implementer", "host_b", "agent_b", ttl_s=60)
        self.assertIsNotNone(claim_b)

    def test_node_telemetry_beacon(self):
        """U-7D #7: Node telemetry is stored and filtered by active cutoff."""
        self.db.update_node_heartbeat({
            "hostname": "test_desktop",
            "ip": "100.64.0.10",
            "pressure": "GPU_BUSY (96%)",
            "resources": {
                "cpu_load_pct": 12.5,
                "ram_available_gb": 16.0,
                "ram_load_pct": 75.0,
                "gpu_name": "NVIDIA GeForce RTX 3090",
                "gpu_load_pct": 96.0,
                "gpu_vram_free_mb": 6000.0,
            }
        })

        nodes = self.db.get_live_nodes()
        self.assertTrue(len(nodes) >= 1)
        test_node = next((n for n in nodes if n["hostname"] == "test_desktop"), None)
        self.assertIsNotNone(test_node, "test_desktop node must be present in live nodes")
        self.assertEqual(test_node["hostname"], "test_desktop")
        self.assertEqual(test_node["ip"], "100.64.0.10")
        self.assertEqual(test_node["resources"]["gpu_name"], "NVIDIA GeForce RTX 3090")


class AgentRuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.db = cluster_coordinator.CoordinatorDB(
            Path(self.tmp_dir.name) / "agent-runtime.db",
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    @staticmethod
    def _identity(binding):
        return {
            "work_id": binding["work_id"],
            "binding_id": binding["binding_id"],
            "owner_id": binding["owner_id"],
            "fence_seq": binding["fence_seq"],
            "token": binding["token"],
        }

    def test_mutual_exclusion_and_authenticated_resume(self):
        work_id = "WORK-BINDING-01"
        results = []

        def acquire(owner):
            results.append(self.db.acquire_agent_runtime_binding(work_id, owner, ttl_s=30))

        first = threading.Thread(target=acquire, args=("owner-a",))
        second = threading.Thread(target=acquire, args=("owner-b",))
        first.start()
        second.start()
        first.join()
        second.join()

        winners = [item for item in results if item is not None]
        self.assertEqual(len(winners), 1)
        binding = winners[0]
        self.assertEqual(binding["fence_seq"], 1)
        self.assertIsNone(
            self.db.acquire_agent_runtime_binding(work_id, binding["owner_id"], ttl_s=30),
            "owner id alone must not resume a bearer binding",
        )
        resumed = self.db.acquire_agent_runtime_binding(
            work_id, binding["owner_id"], ttl_s=60, resume_token=binding["token"]
        )
        self.assertEqual(resumed["binding_id"], binding["binding_id"])
        self.assertEqual(resumed["fence_seq"], binding["fence_seq"])
        self.assertEqual(resumed["token"], binding["token"])

    def test_handoff_fences_old_owner_and_rejects_late_claim(self):
        old = self.db.acquire_agent_runtime_binding("WORK-HANDOFF", "owner-a", ttl_s=30)
        new = self.db.handoff_agent_runtime_binding(
            **self._identity(old), new_owner_id="owner-b", ttl_s=30
        )

        self.assertIsNotNone(new)
        self.assertEqual(new["fence_seq"], old["fence_seq"] + 1)
        self.assertFalse(
            self.db.is_agent_runtime_binding_current(**self._identity(old))
        )
        self.assertTrue(
            self.db.is_agent_runtime_binding_current(**self._identity(new))
        )
        self.assertFalse(
            self.db.submit_agent_runtime_terminal_claim(
                **self._identity(old), status="SUCCEEDED", output="late",
                turns=1, attempts=1,
            )
        )

    def test_expiry_rebinds_with_monotonic_fence(self):
        old = self.db.acquire_agent_runtime_binding("WORK-EXPIRED", "owner-a", ttl_s=10)
        self.now[0] += 11
        self.assertFalse(
            self.db.is_agent_runtime_binding_current(**self._identity(old))
        )
        self.assertEqual(
            self.db.reap_expired_agent_runtime_bindings(), ["WORK-EXPIRED"]
        )
        new = self.db.acquire_agent_runtime_binding("WORK-EXPIRED", "owner-b", ttl_s=10)
        self.assertEqual(new["fence_seq"], old["fence_seq"] + 1)

    def test_revocation_is_observable_and_permanently_rejects_binding(self):
        binding = self.db.acquire_agent_runtime_binding("WORK-REVOKE", "owner-a", ttl_s=30)
        self.assertTrue(
            self.db.revoke_agent_runtime_binding(
                **self._identity(binding), reason="operator-cancel"
            )
        )
        self.assertFalse(
            self.db.is_agent_runtime_binding_current(**self._identity(binding))
        )
        with self.db._get_conn() as conn:
            events = conn.execute(
                "SELECT event_type, details_json FROM agent_runtime_binding_events "
                "WHERE work_id=? ORDER BY event_id",
                ("WORK-REVOKE",),
            ).fetchall()
        self.assertEqual([row["event_type"] for row in events], ["ACQUIRED", "REVOKED"])
        self.assertEqual(json.loads(events[-1]["details_json"])["reason"], "operator-cancel")

    def test_terminal_claim_is_single_use_and_stores_no_secret_or_output(self):
        binding = self.db.acquire_agent_runtime_binding("WORK-CLAIM", "owner-a", ttl_s=30)
        claim = dict(
            **self._identity(binding), status="SUCCEEDED", output="private output",
            turns=2, attempts=3,
        )
        self.assertTrue(self.db.submit_agent_runtime_terminal_claim(**claim))
        self.assertFalse(self.db.submit_agent_runtime_terminal_claim(**claim))
        with self.db._get_conn() as conn:
            persisted = conn.execute(
                "SELECT * FROM agent_runtime_claims WHERE work_id=?", ("WORK-CLAIM",)
            ).fetchone()
            binding_row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE work_id=?", ("WORK-CLAIM",)
            ).fetchone()
        self.assertEqual(persisted["status"], "SUCCEEDED")
        self.assertNotIn("private output", tuple(persisted))
        self.assertNotIn(binding["token"], tuple(binding_row))
        self.assertEqual(binding_row["state"], "TERMINAL")

    def test_invalid_ttl_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "ttl_s"):
            self.db.acquire_agent_runtime_binding("WORK-TTL", "owner-a", ttl_s=0)


if __name__ == "__main__":
    unittest.main()
