"""UX-GA.3 phase 1 — deterministic reviewer evidence bundle (CLI half).

`bundle(root, wfid, worker_id=None)` assembles EXISTING handles for one
workflow so a reviewer stops re-opening N files to judge a worker result:
per worker the lifecycle status, the result's `failureClass`, the result-file
path, and the CQ.2 `oracleEvidence` capsule (null when absent — never a
crash), plus the HARNESS_RESULT path, records-ledger refs, and reduce-level
risk flags. Pure assembly: no re-run, no LLM, and HANDLES-NOT-BODIES — paths
plus the capped capsule only, never artifact bodies (findings/summaries stay
at their paths). Spec: specs/40-features/evidence-bundle.md (the panel render
is deferred there; this module is collector-only).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_lib import records, workflow_ids
from harness_lib.common import HarnessError, read_json

# The CQ.2 capsule contract keys (schemas/worker-result.schema.json). Copying
# ONLY these keys keeps the bundle handles-only even if a result file grows
# body-sized fields later — the capsule is capped by the worker-result contract
# (rerunCmd <= 400 chars, artifactPath is a relative handle).
_CAPSULE_KEYS = ("oracle", "exitClass", "artifactPath", "rerunCmd")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_result(path: Path | None) -> dict[str, Any] | None:
    """Worker result JSON, or None on missing/corrupt file (eb-2: never crash)."""
    if path is None or not path.exists():
        return None
    try:
        data = read_json(path, None)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _capsule(result: dict[str, Any] | None) -> dict[str, Any] | None:
    oe = (result or {}).get("oracleEvidence")
    if not isinstance(oe, dict):
        return None
    return {k: oe.get(k) for k in _CAPSULE_KEYS if k in oe}


def bundle(root: Path | str, wfid: str, worker_id: str | None = None) -> dict[str, Any]:
    """One compact, deterministic dict of handles for reviewing `wfid`."""
    root = Path(root)
    base = workflow_ids.safe_workflow_path(root / ".harness" / "workflows" / "active", wfid)
    workflow = read_json(base / "workflow.json", None)
    if not isinstance(workflow, dict):
        raise HarnessError(f"Workflow not found: {wfid}")
    workers: list[dict[str, Any]] = []
    for worker in workflow.get("workers") or []:
        if not isinstance(worker, dict):
            continue
        wid = worker.get("workerId")
        if worker_id and wid != worker_id:
            continue
        rel = str(worker.get("resultPath") or "")
        result_path = (base / rel) if rel else None
        result = _read_result(result_path)
        workers.append({
            "workerId": wid,
            "status": worker.get("status"),
            "failureClass": (result or {}).get("failureClass"),
            "resultPath": _rel(root, result_path) if result_path and result_path.exists() else None,
            "oracleEvidence": _capsule(result),
        })
    harness_result = base / "reduce" / "harness-result.json"
    reduce_result = _read_result(base / "reduce" / "reducer.result.json")
    try:  # W29 evidence-that-rots: freshness of this workflow's evidence anchor;
        # paths=() so only fresh/aging/unanchored apply here (stale needs paths)
        from harness_lib import evidence_decay
        decay = evidence_decay.assess(root, workflow.get("headSha"))
    except Exception:
        decay = None
    try:  # records refs are best-effort handles; a broken index never blocks review
        refs = [{"title": r.get("title"), "ref": r.get("ref")} for r in records.search(root, wfid)]
    except Exception:
        refs = []
    return {
        "workflowId": wfid,
        "workers": workers,
        "harnessResultPath": _rel(root, harness_result) if harness_result.exists() else None,
        "recordsRefs": refs,
        "riskFlags": ({"requiresSecurityReview": bool(reduce_result.get("requiresSecurityReview"))}
                      if reduce_result is not None else None),
        "evidenceDecay": decay,
    }
