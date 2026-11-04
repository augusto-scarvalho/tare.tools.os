"""TRAIN-10 falsifiers: asynchronous human intake queue + multi-agent consensus.

Every fixture runs in an isolated scratch root with its own scratch work graph;
the real work-graph.json / relay tree stay untouched.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Import relay modules by their own top-level names (they import each other that
# way); importing as `relay.<mod>` would create a second instance whose globals
# rebind_root would not reach.
sys.path.insert(0, str(REPO / "relay"))
import relay_mesh          # noqa: E402
import relay_io            # noqa: E402
import intake_processor    # noqa: E402
import consensus_engine    # noqa: E402


def _scratch_graph(root: Path) -> None:
    real = json.loads((REPO / "work-graph.json").read_text(encoding="utf-8-sig"))
    (root / "work-graph.json").write_text(json.dumps(
        {"nodes": [], "edges": [], "meta": real.get("meta", {}),
         "sources": real.get("sources", {})}), encoding="utf-8")


class IntakeConsensusTests(unittest.TestCase):
    _REAL = REPO

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="test_intake_"))
        (self.root / "relay").mkdir()
        _scratch_graph(self.root)
        relay_mesh.rebind_root(self.root)   # intake_processor/consensus_engine follow this
        intake_processor.ensure_dirs()
        self._human = intake_processor._dirs()["human"]

    def tearDown(self):
        relay_mesh.rebind_root(self._REAL)  # restore so later test files aren't misrouted
        shutil.rmtree(self.root, ignore_errors=True)

    def _drop(self, name: str, content: str) -> Path:
        p = self._human / name
        p.write_text(content, encoding="utf-8")
        return p

    # -- Falsifier: malformed / unstructured input parsed gracefully ----------

    def test_unstructured_markdown_ingests_without_raising(self):
        # no heading, no fields — just noise; must still become a node
        out = intake_processor.ingest_file(
            self._drop("noise.md", "cant login sometimes??? pls fix ~~~ \n\x00 weird"))
        self.assertTrue(out["ok"], out)
        self.assertIn(out["taskId"], intake_processor._node_ids())

    def test_malformed_json_falls_back_to_text(self):
        out = intake_processor.ingest_file(
            self._drop("broken.json", '{"title": "half'))  # invalid JSON
        self.assertTrue(out["ok"], out)
        self.assertIn(out["taskId"], intake_processor._node_ids())

    def test_structured_fields_are_extracted(self):
        demand = self._drop("feat.md",
                            "# Add dark mode\n\npriority: P1\n- [ ] toggle\n- [ ] persist\n")
        parsed = intake_processor.parse_demand(demand)
        self.assertEqual(parsed["title"], "Add dark mode")
        self.assertEqual(parsed["priority"], "P1")
        self.assertEqual(parsed["definition_of_done"], ["toggle", "persist"])

    # -- Falsifier: duplicate / stale files are idempotent --------------------

    def test_duplicate_intake_yields_one_node(self):
        body = "# Flaky login test\n\nfails on CI\npriority: P1\n"
        first = intake_processor.ingest_file(self._drop("a.md", body))
        second = intake_processor.ingest_file(self._drop("a_again.md", body))
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["already"])
        self.assertTrue(second["already"], "re-dropped identical demand must not duplicate")
        ids = [i for i in intake_processor._node_ids() if i.startswith("INTAKE-")]
        self.assertEqual(len(ids), 1, ids)
        # planner is notified only on the real admission, not the duplicate
        self.assertEqual(len(list((relay_mesh.INBOX_DIR / "planner").glob("*.json"))), 1)

    def test_sweep_ignores_readme_and_ingests_demands(self):
        self._drop("README.md", "# Human Intake Queue\n\ndocs, not a demand\n")
        self._drop("real.md", "# A real demand\n\nplease do this\n")
        ingested = intake_processor.sweep_human()
        self.assertEqual(ingested, 1, "README must be skipped, the demand ingested")
        titles = []
        graph = relay_io.read_json(relay_mesh.WORK_GRAPH_FILE)
        titles = [n.get("title") for n in graph.get("nodes", [])]
        self.assertIn("A real demand", titles)
        self.assertNotIn("Human Intake Queue", titles)

    # -- Falsifier: bounded retry -> quarantine with diagnostic receipt --------

    def test_corrupt_ingest_quarantined_after_three_attempts(self):
        (self.root / "work-graph.json").write_text("{ not json", encoding="utf-8")  # admission will fail
        demand = self._drop("bad.md", "# will not admit\n")
        failures: dict = {}
        for _ in range(intake_processor.MAX_ATTEMPTS - 1):
            out = intake_processor.ingest_file(demand, failures=failures)
            self.assertFalse(out["ok"] or out["quarantined"])
            self.assertTrue(demand.exists(), "not quarantined before the attempt cap")
        final = intake_processor.ingest_file(demand, failures=failures)
        self.assertTrue(final["quarantined"], final)
        q = intake_processor._dirs()["quarantine"]
        self.assertTrue((q / "bad.md").exists())
        self.assertTrue((q / "bad.md.error.txt").exists(), "diagnostic receipt required")

    # -- Falsifier: consensus quorum enforced; minority/disputed cannot act ----

    def test_minority_cannot_synthesize(self):
        consensus_engine.create_proposal("PROP-1", "codex_planner", "Tighten packet schema",
                                         "add a field", broadcast=False)  # 1 implicit AGREE
        t = consensus_engine.tally("PROP-1")
        self.assertFalse(t["consensus"], t)  # quorum is 2
        out = consensus_engine.synthesize("PROP-1")
        self.assertFalse(out["ok"] and out.get("outcome") == "SYNTHESIZED")
        self.assertEqual(out["outcome"], "CONSENSUS_NOT_REACHED")

    def test_objection_blocks_even_with_quorum(self):
        consensus_engine.create_proposal("PROP-2", "codex_planner", "Rename role",
                                         "planner->architect", broadcast=False)
        consensus_engine.cast_vote("PROP-2", "claude_fable_acer", "AGREE")   # 2 AGREE => quorum met
        self.assertTrue(consensus_engine.tally("PROP-2")["consensus"])
        consensus_engine.cast_vote("PROP-2", "antigravity", "OBJECTION", "breaks audits")
        t = consensus_engine.tally("PROP-2")
        self.assertFalse(t["consensus"], "a single objection must block consensus")
        self.assertFalse(consensus_engine.synthesize("PROP-2")["ok"])

    def test_consensus_synthesizes_canonical_task(self):
        consensus_engine.create_proposal("PROP-3", "codex_planner", "Add retry backoff",
                                         "exponential", broadcast=False)
        consensus_engine.cast_vote("PROP-3", "claude_fable_acer", "AGREE")
        out = consensus_engine.synthesize("PROP-3")
        self.assertTrue(out["ok"] and out["outcome"] == "SYNTHESIZED", out)
        self.assertIn(out["taskId"], intake_processor._node_ids())
        # idempotent: re-synthesis creates no second node
        again = consensus_engine.synthesize("PROP-3")
        self.assertEqual(again["outcome"], "ALREADY_SYNTHESIZED")

    def test_invalid_and_nonswarm_votes_refused(self):
        consensus_engine.create_proposal("PROP-4", "codex_planner", "x", "y", broadcast=False)
        self.assertFalse(consensus_engine.cast_vote("PROP-4", "claude_fable_acer", "MAYBE")["ok"])
        self.assertFalse(consensus_engine.cast_vote("PROP-4", "random_stranger", "AGREE")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
