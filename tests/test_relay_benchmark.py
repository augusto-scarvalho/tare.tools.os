"""TRAIN-06 benchmark harness checks (falsifiers 9/10 shape + methodology)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay import relay_benchmark


class RelayBenchmarkTests(unittest.TestCase):
    def test_benchmark_fixture_metrics(self):
        # One hundred landed trains are enough to falsify history leakage and
        # board growth in the unit suite. The 10k run remains the dispatch-time
        # scale artifact over the exact same code path.
        report = relay_benchmark.run(landed=100)
        board = report["board"]
        self.assertEqual(board["activeListed"], 3, "board lists exactly the active trains")
        self.assertTrue(board["under4KiB"], f"board is {board['bytes']}B, must be <=4KiB")
        self.assertFalse(board["fullHistoryEmbedded"])
        self.assertEqual(report["idle"]["boardWrites"], 0, "30 idle renders write nothing")
        self.assertTrue(report["idle"]["noopPreservesMtime"])
        self.assertEqual(report["transition"]["roleMessages"], 1,
                         "one transition -> exactly one role message")
        self.assertEqual(report["replay"]["duplicateEffects"], 0)
        self.assertIsNone(report["tokenAccounting"]["actualTokens"],
                          "estimates must never masquerade as actual tokens")


if __name__ == "__main__":
    unittest.main()
