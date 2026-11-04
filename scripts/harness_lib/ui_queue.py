#!/usr/bin/env python3
"""queue-view-live (B0) — the live work-queue snapshot for the panel.

`queue_snapshot(root)` derives, per active workflow, the queue-level view the
Painel's small Agents strip cannot give: workflow id + phase + task + per-
status counts + one row per worker with a STAGE chip derived from its
taskProfile. Honest labeling (roadmap risk #4): the stage is the declared
PROFILE, not observed activity — the field is named `stage` but the UI labels
it "profile". Pure reads over `.harness/workflows/active/` on every call
(derived-per-poll, nothing stored); never-crash per workflow.

Consumed by `GET /api/queue` (harness_ui) and the `▶ Queue` panel view.
CLI parity is the existing `workflow list` / `workflow async-status` verbs —
no new verb (M2 rule already satisfied).

Self-check: `python scripts/harness_lib/ui_queue.py`.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import async_state  # noqa: E402
from harness_lib.common import read_json  # noqa: E402

# taskProfile → stage label (EN-default per SPEC-122). Deliberately coarse.
STAGES = {"scan": "research", "cheap": "research", "plan": "refining",
          "implementation": "coding", "debug": "debugging",
          "review": "reviewing", "security": "reviewing", "docs": "documenting"}
PHASE_LABELS = {"planned": "planned", "running_workers": "running",
                "awaiting_reduce": "consolidating", "reducing": "consolidating",
                "finalized": "finalized"}


def stage_label(profile: str) -> str:
    return STAGES.get(str(profile or ""), str(profile or "-"))


def _honor_queue_order(root: Path, files: list[Path]) -> list[Path]:
    """queue-oprow-phase: reorder the PENDING (planned) workflow slots by the persisted
    queueOrder (via async_state.ordered_pending_workflows) so a `workflow reorder` is
    VISIBLE on the next snapshot re-fetch. Running/settled workflows keep their sorted slot.
    ponytail: keys by dir name (== workflowId for every real WF dir); a diverging id just
    stays in its sorted slot (safe no-op)."""
    try:
        order = async_state.ordered_pending_workflows(root)  # pending wfids in queue order
    except Exception:
        return files  # never-crash: a corrupt/racing workflow.json -> skip the reorder this poll
    if not order:
        return files
    by_id = {f.parent.name: f for f in files}
    queue = [by_id[w] for w in order if w in by_id]
    pending = set(order)
    out: list[Path] = []
    qi = 0
    for f in files:
        if f.parent.name in pending:  # fill each pending slot from the queue order
            out.append(queue[qi])
            qi += 1
        else:
            out.append(f)
    return out


def queue_snapshot(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    workflows: list[dict[str, Any]] = []
    active = root / ".harness" / "workflows" / "active"
    if active.is_dir():
        for wf_file in _honor_queue_order(root, sorted(active.glob("WF-*/workflow.json"))):
            try:
                wf = read_json(wf_file, {}) or {}
                counts: dict[str, int] = {}
                rows: list[dict[str, Any]] = []
                for worker in wf.get("workers") or []:
                    status = str(worker.get("status") or "unknown")
                    counts[status] = counts.get(status, 0) + 1
                    profile = str(worker.get("taskProfile") or "")
                    rows.append({"workerId": str(worker.get("workerId") or "?"),
                                 "status": status, "profile": profile,
                                 "stage": stage_label(profile),
                                 "unit": str(worker.get("unitId") or "")})
                phase = str(wf.get("phase") or "planned")
                workflows.append({
                    "workflowId": str(wf.get("workflowId") or wf_file.parent.name),
                    "phase": phase,
                    "phaseLabel": PHASE_LABELS.get(phase, phase),
                    "task": str(wf.get("task") or "")[:160],
                    "target": wf.get("target"),
                    "profile": wf.get("workflowProfile"),
                    "counts": counts,
                    "workers": rows,
                })
            except Exception:
                continue  # never-crash collector idiom
    return {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "count": len(workflows), "workflows": workflows}


def _demo() -> None:
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wf = root / ".harness" / "workflows" / "active" / "WF-Q-1"
        wf.mkdir(parents=True)
        (wf / "workflow.json").write_text(json.dumps({
            "workflowId": "WF-Q-1", "phase": "running_workers",
            "task": "queue view fixture", "target": "tA",
            "workers": [
                {"workerId": "w1", "status": "running", "taskProfile": "review"},
                {"workerId": "w2", "status": "queued", "taskProfile": "implementation"},
                {"workerId": "w3", "status": "done", "taskProfile": "scan"}]}),
            encoding="utf-8")
        broken = root / ".harness" / "workflows" / "active" / "WF-Q-2"
        broken.mkdir(parents=True)
        (broken / "workflow.json").write_text("{corrupt", encoding="utf-8")
        snap = queue_snapshot(root)
        assert snap["count"] == 1, snap  # corrupt json is skipped, never a crash
        row = snap["workflows"][0]
        assert row["phaseLabel"] == "running" and row["target"] == "tA", row
        assert row["counts"] == {"running": 1, "queued": 1, "done": 1}, row
        stages = {w["workerId"]: w["stage"] for w in row["workers"]}
        assert stages == {"w1": "reviewing", "w2": "coding", "w3": "research"}, stages
        assert queue_snapshot(root / "nope")["count"] == 0

        # queue-oprow-phase: two PLANNED WFs + a persisted queueOrder -> snapshot honors it.
        # (drop the corrupt WF first: ordered_pending_workflows reads every active json.)
        (broken / "workflow.json").unlink()
        active = root / ".harness" / "workflows" / "active"
        for wfid in ("WF-Q-A", "WF-Q-B"):
            (active / wfid).mkdir(parents=True)
            (active / wfid / "workflow.json").write_text(
                json.dumps({"workflowId": wfid, "phase": "planned", "workers": []}), encoding="utf-8")
        async_state.write_queue_order(root, ["WF-Q-B", "WF-Q-A"])
        ids = [w["workflowId"] for w in queue_snapshot(root)["workflows"]]
        # B before A (queueOrder), running WF-Q-1 keeps its sorted slot (before the planned pair).
        assert ids == ["WF-Q-1", "WF-Q-B", "WF-Q-A"], ids
    print("ui_queue self-check: ok")


if __name__ == "__main__":
    _demo()
