#!/usr/bin/env python3
"""OB.3 deterministic half: the `anomaly-card` verb (SPEC-131).

When the self-review `workflow-cost-outlier` escalation fires, the operator
gets a number with no shape. This card gives the number a shape from data
already on disk — PRIMARY input is the workflow's `cost_metrics` LEDGER record
(`.harness/state/cost-metrics.json`, which survives WF-dir scrubbing), read via
`cost_metrics._load`, the ledger's existing reader. When the WF dir still
exists it enriches gracefully-null with the token-audit per-worker report and
SPEC-129 `evidence_bundle.bundle` handles (imported, never re-implemented).

`factors` is a FIXED deterministic rule list — no scoring model, no LLM:

  1. fan-out above the same-profile ledger median (DW.3 `graph.fanOut`);
  2. a single worker dominating estimated tokens (WF-dir token-audit report);
  3. duration outlier vs the same-profile ledger median (3.0x, mirroring
     self-review's `workflowCostOutlierFactor`).

The opt-in LLM explanation half of OB.3 is DEFERRED (with UX-GA.4); its seat is
the card's `llmExplanation: null` field. Read-only, verify-on-demand, rc 0.
Registered via `cli_registry.register()` with zero harness.py edits
(spec: specs/40-features/anomaly-card.md).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from harness_lib import cost_metrics, evidence_bundle, workflow_ids
from harness_lib.common import ROOT, read_json

# ponytail: fixed thresholds, not knobs — promote to .harness/self-review.json
# thresholds only if a real operator ever needs to tune them.
MIN_PEERS = 3                    # median rules stay silent below this sample size
DOMINANT_WORKER_SHARE = 0.5      # one worker holding >50% of estimated tokens
DURATION_OUTLIER_FACTOR = 3.0    # mirrors self_review_rules workflowCostOutlierFactor

LEDGER_FIELDS = ("at", "type", "profile", "target", "status", "estTokens",
                 "observedTokens", "costUsd", "costBasis", "workers",
                 "durationS", "cpuS", "graph", "economy")


def _peer_values(records: list[dict[str, Any]], profile: Any, wfid: str,
                 getter) -> list[float]:
    """Numeric `getter(record)` values over OTHER workflows of the same profile."""
    values: list[float] = []
    for r in records:
        if r.get("kind") != "workflow" or r.get("workflowId") == wfid:
            continue
        if r.get("profile") != profile:
            continue
        v = getter(r)
        if isinstance(v, (int, float)):
            values.append(float(v))
    return values


def _worker_token_rows(base: Path) -> list[dict[str, Any]] | None:
    """Per-worker estimated tokens from the WF dir's token-audit report; None
    when the report (or the dir) is gone — enrichment is gracefully null."""
    report = read_json(base / "reduce" / "token-audit.json", None)
    if not isinstance(report, dict):
        return None
    rows = []
    for w in report.get("workers") or []:
        if isinstance(w, dict):
            rows.append({"workerId": w.get("workerId"),
                         "estTokens": int(w.get("estimatedPromptTokens") or 0)
                         + int(w.get("estimatedResultTokens") or 0)})
    return rows or None


def _factors(record: dict[str, Any], records: list[dict[str, Any]],
             worker_rows: list[dict[str, Any]] | None) -> list[str]:
    """The FIXED deterministic rule list — each hit is a named string with its
    numbers; rules whose inputs are absent stay silent (never fabricate)."""
    wfid = str(record.get("workflowId") or "")
    profile = record.get("profile")
    label = str(profile or "(none)")
    graph = record.get("graph") or {}
    factors: list[str] = []

    fan = graph.get("fanOut")
    fan_peers = _peer_values(records, profile, wfid,
                             lambda r: (r.get("graph") or {}).get("fanOut"))
    if isinstance(fan, (int, float)) and len(fan_peers) >= MIN_PEERS:
        med = statistics.median(fan_peers)
        if fan > med:
            factors.append(f"fan-out {int(fan)} above profile '{label}' median "
                           f"{med:g} ({len(fan_peers)} peers)")

    if worker_rows and len(worker_rows) >= 2:
        total = sum(r["estTokens"] for r in worker_rows)
        top = max(worker_rows, key=lambda r: r["estTokens"])
        if total and top["estTokens"] / total > DOMINANT_WORKER_SHARE:
            factors.append(f"worker {top['workerId']} dominates tokens: "
                           f"{top['estTokens']} of {total} "
                           f"({top['estTokens'] / total:.0%})")

    dur = record.get("durationS")
    dur_peers = _peer_values(records, profile, wfid, lambda r: r.get("durationS"))
    if isinstance(dur, (int, float)) and len(dur_peers) >= MIN_PEERS:
        med = statistics.median(dur_peers)
        if med and dur > med * DURATION_OUTLIER_FACTOR:
            factors.append(f"duration {dur:g}s is {dur / med:.1f}x profile "
                           f"'{label}' median {med:g}s ({len(dur_peers)} peers)")
    return factors


def card(root: Path | str, workflow_id: str) -> dict[str, Any]:
    """Deterministic anomaly feature card for `workflow_id`; pure read.

    Renders from the ledger record alone when the WF dir was scrubbed;
    `found: False` (never a raise) when the ledger has no such workflow.
    """
    root = Path(root)
    records = cost_metrics._load(root)
    matches = [r for r in records if r.get("kind") == "workflow"
               and r.get("workflowId") == workflow_id]
    if not matches:
        return {"workflowId": workflow_id, "found": False,
                "error": "no workflow record in the cost ledger"}
    record = matches[-1]  # most recent finalize wins

    base = workflow_ids.safe_workflow_path(
        root / ".harness" / "workflows" / "active", workflow_id)
    wf_dir_present = base.is_dir()
    worker_rows = _worker_token_rows(base) if wf_dir_present else None
    evidence = None
    if wf_dir_present:
        try:
            evidence = evidence_bundle.bundle(root, workflow_id)
        except Exception:
            evidence = None  # half-scrubbed dir: enrichment stays null, card stands

    return {
        "workflowId": workflow_id,
        "found": True,
        "ledger": {k: record.get(k) for k in LEDGER_FIELDS},
        "factors": _factors(record, records, worker_rows),
        "enrichment": {"workflowDirPresent": wf_dir_present,
                       "perWorkerTokens": worker_rows,
                       "evidence": evidence},
        # Seat for OB.3's opt-in LLM explanation — DEFERRED (with UX-GA.4).
        # It sits ON TOP of this card, view-only; the deterministic half never
        # calls a model.
        "llmExplanation": None,
    }


def cmd_anomaly_card(args) -> int:
    """`harness.py anomaly-card <workflow_id> [--json]`: read-only, rc 0."""
    data = card(ROOT, args.workflow_id)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data["found"]:
        print(f"anomaly-card: {data['error']}: {args.workflow_id}")
        return 0
    led, enr = data["ledger"], data["enrichment"]
    graph = led.get("graph") or {}
    print(f"ANOMALY CARD  {data['workflowId']}")
    print(f"  profile={led.get('profile')}  type={led.get('type')}  "
          f"status={led.get('status')}  costBasis={led.get('costBasis')}")
    print(f"  estTokens={led.get('estTokens')}  observedTokens={led.get('observedTokens')}  "
          f"costUsd={led.get('costUsd')}  durationS={led.get('durationS')}")
    print(f"  graph: fanOut={graph.get('fanOut')}  depth={graph.get('depth')}  "
          f"makespanS={graph.get('makespanS')}  criticalPathS={graph.get('criticalPathS')}")
    if data["factors"]:
        print("  factors:")
        for f in data["factors"]:
            print(f"    - {f}")
    else:
        print("  factors: none flagged by the deterministic rules")
    print(f"  enrichment: workflow dir {'present' if enr['workflowDirPresent'] else 'scrubbed'}; "
          f"per-worker token rows: {len(enr['perWorkerTokens'] or [])}; "
          f"evidence handles: {'yes' if enr['evidence'] else 'no'}")
    print("  llm explanation: deferred (OB.3 opt-in half; sits on top of this card)")
    return 0
