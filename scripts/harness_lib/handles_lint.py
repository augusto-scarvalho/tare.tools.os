"""CE.3 (G5): OBSERVE-ONLY lint for oversized inline payloads in WORKER_RESULTs.

The harness passes large artifacts by reference via 200-char HANDLEs (SPEC-112
records ledger). Nothing flagged when a worker inlined a big payload that should
have been a handle instead. This module is that lint: a pure, deterministic scan
(no LLM, no network) over WORKER_RESULT dicts.

Heuristic: any string field longer than INLINE_PAYLOAD_MAX is an inline payload
that dwarfs a 200-char handle and should have been passed by reference. At 2000
chars it is already far past every schema-legal text field (summary maxLength
1000, evidence/items 500) and any real handle, so length alone is the signal.

# ponytail: length-only heuristic. If false positives appear, refine with a
# multi-line / base64-ratio test on the offending value — the finding already
# carries `chars`, so the upgrade is local to _walk.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INLINE_PAYLOAD_MAX = 2000


def _walk(node: Any, path: str, worker_id: str, out: list[dict[str, Any]]) -> None:
    if isinstance(node, str):
        if len(node) > INLINE_PAYLOAD_MAX:
            out.append({"workerId": worker_id, "field": path or "(root)", "chars": len(node)})
    elif isinstance(node, dict):
        for key, value in node.items():
            _walk(value, f"{path}.{key}" if path else str(key), worker_id, out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk(value, f"{path}[{i}]", worker_id, out)


def scan_worker_result(worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return findings [{workerId, field, chars}] for one WORKER_RESULT dict."""
    worker_id = str(worker.get("workerId") or worker.get("workerRole") or "unknown")
    findings: list[dict[str, Any]] = []
    _walk(worker, "", worker_id, findings)
    return findings


def scan_worker_results(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return findings across a list of WORKER_RESULT dicts."""
    findings: list[dict[str, Any]] = []
    for worker in workers:
        if isinstance(worker, dict):
            findings.extend(scan_worker_result(worker))
    return findings


def scan_workflow_dir(workflow_dir: str | Path) -> list[dict[str, Any]]:
    """Load *.result.json under a workflow dir and scan them. Missing dir -> []."""
    base = Path(workflow_dir)
    findings: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*.result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # malformed/partial result files are not this lint's concern
        if isinstance(data, dict):
            findings.extend(scan_worker_result(data))
    return findings
