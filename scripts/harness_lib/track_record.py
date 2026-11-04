"""W29 bet 2 — "rigor by track record": read-only view over the cost
ledger's delegation outcomes, suggesting a burden-of-proof class per lane.

Groups `kind=delegation` records by model|agentType and tallies the overseer's
kept/partial/rejected/reworked verdicts (cost_metrics.record_delegation).
`usefulRate` reuses the ledger's own definition (useful = kept+reworked).
`suggestedBurden` is INFO-ONLY — nothing consumes it yet; wiring it into gate
depth is the EXP-33 ladder's owner-gated control phase:

  light             — enough history, high useful rate: candidate for the
                      minimum-execution path
  standard          — default: history exists but is unremarkable
  heavy             — enough history, low useful rate: candidate for extra
                      review before acceptance
  insufficient-data — fewer than MIN_N judged delegations: never infer from noise

Round: docs/research/w29-evidence-gated-assurance-round.md (cluster 2, ônus da
prova deslocável). Self-check: run this file.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib.common import read_json  # noqa: E402

LEDGER_REL = ".harness/state/cost-metrics.json"
# ponytail: fixed thresholds; move to config only when a consumer ships (EXP-33)
MIN_N = 8
LIGHT_RATE = 0.8
HEAVY_RATE = 0.5
_OUTCOMES = ("kept", "partial", "rejected", "reworked")


def summarize_track(root: Path | str) -> dict[str, Any]:
    records = (read_json(Path(root) / LEDGER_REL, {}) or {}).get("records") or []
    groups: dict[str, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict) or rec.get("kind") != "delegation":
            continue
        key = f"{rec.get('model') or '(none)'}|{rec.get('agentType') or '(none)'}"
        g = groups.setdefault(key, {"n": 0, "unknown": 0,
                                    **{o: 0 for o in _OUTCOMES}})
        g["n"] += 1
        outcome = rec.get("outcome")
        if outcome in _OUTCOMES:
            g[outcome] += 1
        else:
            g["unknown"] += 1
    for g in groups.values():
        judged = sum(g[o] for o in _OUTCOMES)
        useful = g["kept"] + g["reworked"]
        rate = round(useful / judged, 3) if judged else None
        g["judged"] = judged
        g["usefulRate"] = rate
        if judged < MIN_N:
            g["suggestedBurden"] = "insufficient-data"
        elif rate >= LIGHT_RATE:
            g["suggestedBurden"] = "light"
        elif rate < HEAVY_RATE:
            g["suggestedBurden"] = "heavy"
        else:
            g["suggestedBurden"] = "standard"
    return {"groups": groups,
            "note": "info-only (W29 bet 2, observe-first): suggestedBurden has "
                    "no consumer; control phase is EXP-33, owner-gated"}


def _demo() -> None:
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        led = root / LEDGER_REL
        led.parent.mkdir(parents=True)
        recs = ([{"kind": "delegation", "model": "opus", "agentType": "implementer",
                  "outcome": "kept"}] * 8
                + [{"kind": "delegation", "model": "glm", "agentType": "ideator",
                    "outcome": "rejected"}] * 8
                + [{"kind": "delegation", "model": "haiku", "agentType": "scanner",
                    "outcome": "kept"}] * 3
                + [{"kind": "delegation", "model": "opus", "agentType": "implementer"}]
                + [{"kind": "workflow"}])
        led.write_text(json.dumps({"schemaVersion": 1, "records": recs}), encoding="utf-8")
        out = summarize_track(root)["groups"]
        assert out["opus|implementer"]["suggestedBurden"] == "light", out
        assert out["opus|implementer"]["unknown"] == 1 and out["opus|implementer"]["n"] == 9
        assert out["glm|ideator"]["suggestedBurden"] == "heavy", out
        assert out["haiku|scanner"]["suggestedBurden"] == "insufficient-data", out
        assert summarize_track(Path(tmp) / "missing")["groups"] == {}
    print("track_record self-check: ok")


if __name__ == "__main__":
    _demo()
