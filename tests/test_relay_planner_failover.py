"""Falsifier battery for RELAY-PLANNER-VENDOR-FAILOVER-01 (TRAIN-08).

Vendor-neutral Planner succession, monotonic epoch fencing, digest-bound
handoff capsule, planned + emergency takeover. Every test is a falsifier: it
fails iff the guarantee it names is broken.
"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Import the relay modules by the SAME top-level names they import each other
# with (they insert relay/ onto sys.path when run as scripts). Importing them as
# `relay.<mod>` here would create a second module object with its own globals, so
# rebind_root would rebind one instance while the test read another.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))
import relay_mesh          # noqa: E402
import planner_role        # noqa: E402
import autonomous_planner  # noqa: E402


def _good_contract():
    return {c: True for c in planner_role.REQUIRED_CAPABILITIES}


def _owner_cap(nonce, target):
    return {"authorizedBy": "owner", "nonce": nonce, "targetEpoch": target}


class PlannerFailoverTests(unittest.TestCase):
    _REAL_ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self._orig_cred = planner_role.CRED_DIR
        self.root = Path(tempfile.mkdtemp(prefix="test_planner_"))
        (self.root / "relay").mkdir(parents=True)
        (self.root / "work-graph.json").write_text(
            json.dumps({"nodes": [{"id": "T-1", "title": "t", "exit_criteria": ["c"],
                                   "completion": {"status": "NOT_DONE"}, "work_status": "planned"}],
                        "edges": []}), encoding="utf-8")
        relay_mesh.rebind_root(self.root)          # also rebinds planner_role (lockstep)
        self.cred = Path(tempfile.mkdtemp(prefix="test_cred_"))
        planner_role.CRED_DIR = self.cred          # machine-local secret isolation

    def tearDown(self):
        # Restore globals so a leftover epoch under our temp root cannot fence
        # a later test file that patches paths without calling rebind_root.
        relay_mesh.rebind_root(self._REAL_ROOT)
        planner_role.CRED_DIR = self._orig_cred
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.cred, ignore_errors=True)

    # -- DoD 1: PlannerRoleContract ----------------------------------------

    def test_contract_probes_are_real_not_self_declared(self):
        results = planner_role.probe_capabilities(planner_role.default_probes(self.root))
        ok, why = planner_role.verify_contract(results)
        self.assertTrue(ok, why)
        # A missing graph makes graph_read actually fail -> contract fails closed.
        (self.root / "work-graph.json").unlink()
        bad = planner_role.probe_capabilities(planner_role.default_probes(self.root))
        self.assertFalse(planner_role.verify_contract(bad)[0])

    def test_contract_partial_declaration_rejected(self):
        partial = {"graph_read": True}  # a vendor asserting one capability
        self.assertFalse(planner_role.verify_contract(partial)[0])

    # -- Backward compatibility: fence dormant until an epoch is minted -----

    def test_fence_noop_before_any_epoch(self):
        ok, _ = planner_role.enforce_fence("anyone")
        self.assertTrue(ok)  # legacy/unfenced operation stays green

    # -- Falsifier 1: stale epoch N rejected when active is N+1 -------------

    def test_stale_epoch_token_rejected_after_advance(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        old_token = planner_role.fencing_token(1, "codex_planner")
        self.assertTrue(planner_role.enforce_fence("codex_planner", old_token)[0])
        ok, new = planner_role.cas_advance_epoch(
            expected_epoch=1, successor_agent="claude_planner", successor_vendor="claude",
            contract_result=_good_contract(), takeover=_owner_cap("h1", 2))
        self.assertTrue(ok, new)
        self.assertEqual(new["plannerEpoch"], 2)
        # Old planner (epoch 1) is fenced: wrong active agent AND stale token.
        self.assertFalse(planner_role.enforce_fence("codex_planner", old_token)[0])
        # A recomputed epoch-1 token, even for the now-active agent, is stale.
        stale = planner_role.fencing_token(1, "claude_planner")
        self.assertFalse(planner_role.enforce_fence("claude_planner", stale)[0])
        # The genuine epoch-2 token is accepted.
        good = planner_role.fencing_token(2, "claude_planner")
        self.assertTrue(planner_role.enforce_fence("claude_planner", good)[0])

    # -- Falsifier 2: uncredentialed successor rejected --------------------

    def test_uncredentialed_successor_rejected(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        bad = {"graph_read": True, "command_exec": False,
               "hashing": True, "envelope_routing": True}
        ok, why = planner_role.cas_advance_epoch(
            expected_epoch=1, successor_agent="sketchy", successor_vendor="x",
            contract_result=bad, takeover=_owner_cap("h1", 2))
        self.assertFalse(ok, why)
        self.assertEqual(planner_role.read_epoch()["plannerEpoch"], 1)  # unchanged

    def test_takeover_without_capability_rejected(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        ok, _ = planner_role.cas_advance_epoch(
            expected_epoch=1, successor_agent="s", successor_vendor="v",
            contract_result=_good_contract(), takeover={})
        self.assertFalse(ok)
        # A capability bound to the wrong epoch cannot be replayed forward.
        ok2, _ = planner_role.cas_advance_epoch(
            expected_epoch=1, successor_agent="s", successor_vendor="v",
            contract_result=_good_contract(), takeover=_owner_cap("h1", 99))
        self.assertFalse(ok2)

    # -- Falsifier 3: replayed / tampered / stale handoff capsule rejected --

    def test_handoff_capsule_replay_and_tamper_rejected(self):
        cap = planner_role.build_capsule(
            root=self.root, reason="budget", source_epoch=1, successor="claude_planner",
            source_paths={"graph": "work-graph.json"})
        ok, why = planner_role.verify_capsule(cap, root=self.root)
        self.assertTrue(ok, why)
        # Replay: nonce already consumed.
        self.assertFalse(planner_role.verify_capsule(
            cap, root=self.root, consumed_nonces=frozenset({cap["handoffId"]}))[0])
        # Tamper: mutate a field without recomputing the digest.
        tampered = dict(cap, reason="malicious")
        self.assertFalse(planner_role.verify_capsule(tampered, root=self.root)[0])
        # Stale: a source changed since the capsule was built.
        (self.root / "work-graph.json").write_text('{"nodes": [], "edges": []}', encoding="utf-8")
        self.assertFalse(planner_role.verify_capsule(cap, root=self.root)[0])

    # -- Falsifier 4: dual-writer split-brain -> exactly one winner ---------

    def test_dual_writer_split_brain_single_winner(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        wins, barrier = [], threading.Barrier(2)

        def racer(agent):
            barrier.wait()
            ok, _ = planner_role.cas_advance_epoch(
                expected_epoch=1, successor_agent=agent, successor_vendor="v",
                contract_result=_good_contract(), takeover=_owner_cap(agent, 2))
            if ok:
                wins.append(agent)

        threads = [threading.Thread(target=racer, args=(a,)) for a in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1, f"expected exactly one CAS winner, got {wins}")
        rec = planner_role.read_epoch()
        self.assertEqual(rec["plannerEpoch"], 2)           # advanced exactly once
        self.assertEqual(rec["agentId"], wins[0])          # winner installed
        # File is intact JSON (not corrupted by the race).
        json.loads((self.root / "relay" / "state" / "planner-epoch.json").read_text(encoding="utf-8"))

    # -- Falsifier 5: emergency takeover timing + capability ---------------

    def test_emergency_takeover_respects_lease_and_capability(self):
        rec = planner_role.mint_root_epoch("codex_planner", "codex", now=1000.0)
        # Before lease expiry: refused even with a valid owner capability.
        ok, _ = planner_role.emergency_takeover(
            successor_agent="claude_planner", successor_vendor="claude",
            contract_result=_good_contract(),
            recovery_capability={"authorizedBy": "owner", "nonce": "r1"},
            root=self.root, now=1000.0 + planner_role.LEASE_TTL_SECONDS - 5)
        self.assertFalse(ok)
        after = 1000.0 + planner_role.LEASE_TTL_SECONDS + 5
        # After expiry, WITHOUT an owner capability: stays read-only.
        ok, _ = planner_role.emergency_takeover(
            successor_agent="claude_planner", successor_vendor="claude",
            contract_result=_good_contract(),
            recovery_capability={"authorizedBy": "self", "nonce": "r1"},
            root=self.root, now=after)
        self.assertFalse(ok)
        self.assertEqual(planner_role.read_epoch()["plannerEpoch"], 1)
        # After expiry, WITH an owner capability: epoch advances.
        ok, new = planner_role.emergency_takeover(
            successor_agent="claude_planner", successor_vendor="claude",
            contract_result=_good_contract(),
            recovery_capability={"authorizedBy": "owner", "nonce": "r1"},
            root=self.root, now=after)
        self.assertTrue(ok, new)
        self.assertEqual(new["plannerEpoch"], 2)
        self.assertEqual(new["takeoverKind"], "emergency")

    # -- DoD 4: planned graceful handoff -----------------------------------

    def test_planned_handoff_transfers_vendor_and_epoch(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        token = planner_role.fencing_token(1, "codex_planner")
        cap = planner_role.build_capsule(
            root=self.root, reason="budget", source_epoch=1, successor="claude_planner",
            source_paths={"graph": "work-graph.json"})
        ok, new = planner_role.planned_handoff(
            incumbent_agent="codex_planner", incumbent_token=token,
            successor_agent="claude_planner", successor_vendor="claude",
            contract_result=_good_contract(), capsule=cap, root=self.root)
        self.assertTrue(ok, new)
        self.assertEqual(new["plannerEpoch"], 2)
        self.assertEqual(new["vendorId"], "claude")
        self.assertEqual(autonomous_planner.active_vendor(), "claude")  # executor follows epoch

    # -- Role authority is inherited exactly, never broadened (intake #11) --

    def test_module_grants_no_implement_audit_or_land_authority(self):
        for forbidden in ("implement", "audit", "land", "approve", "mark_done"):
            self.assertFalse(hasattr(planner_role, forbidden),
                             f"planner_role must not expose {forbidden!r}")

    # -- relay_mesh integration: init-train fence guard --------------------

    def test_init_train_fenced_for_non_active_planner(self):
        planner_role.mint_root_epoch("codex_planner", "codex")
        # A different agent (e.g. a resurrected old chat) cannot init a train.
        with self.assertRaises(SystemExit):
            relay_mesh.init_train("TRAIN-Z", "z", ["T-1"], "impostor_planner")
        self.assertFalse((self.root / "relay" / "trains" / "TRAIN-Z").exists())

    def test_init_train_allowed_for_active_planner_with_token(self):
        import os
        planner_role.mint_root_epoch("codex_planner", "codex")
        os.environ["RELAY_PLANNER_FENCE_TOKEN"] = planner_role.fencing_token(1, "codex_planner")
        try:
            relay_mesh.init_train("TRAIN-Y", "y", ["T-1"], "codex_planner")
        finally:
            os.environ.pop("RELAY_PLANNER_FENCE_TOKEN", None)
        self.assertTrue((self.root / "relay" / "trains" / "TRAIN-Y" / "STATE.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
