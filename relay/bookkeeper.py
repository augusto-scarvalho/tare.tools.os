#!/usr/bin/env python3
"""
tare.tools Relay Bookkeeper & Horizon AI Curator (Role: bookkeeper)
===================================================================
Orchestrates semantic bookkeeping, AI-driven debt graphification (Gemini 3.7),
topological frontier evaluation, and proactive pipeline horizon staging:
1. Semantic Debt Graphification: AI extracts debts/findings from landed trains into canonical tasks.
2. Graph Heuristics Engine: Evaluates unlock scores, critical paths, and frontier readiness.
3. Horizon Planning Stager: Ensures the cluster pipeline always maintains 3-4 ready release trains.
4. DAG Hygiene & Projections: Validates work-graph.json and exports HTML/Mermaid projections.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
GRAPH_FILE = RELAY_ROOT / "work-graph.json"
GRAPH_OPS = RELAY_ROOT / "satellites" / "tare.tools.backlog-graph" / "graph_ops.py"
BOOKKEEPING_LOG = RELAY_ROOT / "BACKLOG_BOOKKEEPING_LOG.md"
PLANNER_INBOX = RELAY_DIR / "inbox" / "planner"

BOOKKEEPER_PROFILE = {
    "role": "bookkeeper",
    "primary_agent": "gemini_bookkeeper",
    "provider": "google",
    "model": os.getenv("RELAY_BOOKKEEPER_MODEL", "gemini-3.7"),
    "reasoning_effort": os.getenv("RELAY_BOOKKEEPER_THINKING", "medium"),
    "description": "Gemini 3.7 Medium Reasoning profile for semantic graphification, causal synthesis and DAG hygiene."
}

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))

from relay import relay_io, relay_mesh, human_watcher


relay_io.utf8_console()

def log(msg: str):
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [BOOKKEEPER] {msg}", flush=True)
    except Exception:
        clean = msg.encode("ascii", "replace").decode("ascii")
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [BOOKKEEPER] {clean}", flush=True)


def run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(RELAY_ROOT), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **relay_io.safe_subprocess_kwargs()
    )


# -----------------------------------------------------------------------------
# 1. Semantic Debt Graphification via AI (Gemini 3.7 / agy)
# -----------------------------------------------------------------------------

def extract_and_graphify_debts(train_id: str) -> List[Dict[str, Any]]:
    """Uses Gemini 3.7 (via agy) to extract technical debts and graphify them as new tasks."""
    train_dir = relay_mesh.TRAINS_DIR / train_id
    report_file = train_dir / "AUDIT_REPORT.md"
    evidence_file = train_dir / "IMPLEMENTATION_EVIDENCE.md"

    if not report_file.exists() and not evidence_file.exists():
        return []

    report_text = relay_io.read_text(report_file)[:8000] if report_file.exists() else ""
    evidence_text = relay_io.read_text(evidence_file)[:8000] if evidence_file.exists() else ""

    prompt_tmpl_file = RELAY_DIR / "prompts" / "BOOKKEEPER_CURATION_PROMPT.txt"
    if not prompt_tmpl_file.exists():
        return []
    prompt_tmpl = relay_io.read_text(prompt_tmpl_file)
    prompt = prompt_tmpl.format(
        train_id=train_id,
        report_text=report_text,
        evidence_text=evidence_text
    )
    try:
        log(f"🧠 Prompting Gemini 3.7 AI to synthesize debts for '{train_id}'...")
        proc = subprocess.run(
            ["agy", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"],
            cwd=str(RELAY_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, stdin=subprocess.DEVNULL, **relay_io.safe_subprocess_kwargs()
        )
        if proc.returncode != 0:
            log(f"Gemini debt curation returned exit code {proc.returncode}")
            return []

        envelope = json.loads(proc.stdout)
        resp_text = envelope.get("response", "").strip()
        
        # Extract JSON substring if markdown wrapped
        if "```json" in resp_text:
            resp_text = resp_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in resp_text:
            resp_text = resp_text.split("```", 1)[1].split("```", 1)[0].strip()

        data = json.loads(resp_text)
        candidates = data.get("candidate_nodes", [])
        if not candidates:
            log(f"✨ Gemini confirmed 0 technical debts for '{train_id}'.")
            return []

        # Ingest into work-graph.json
        graph_raw = relay_io.read_json(GRAPH_FILE) or {"nodes": [], "edges": []}
        existing_ids = {n.get("id") for n in graph_raw.get("nodes", []) if isinstance(n, dict)}
        
        added = []
        for cand in candidates:
            cid = cand.get("id", "").strip()
            if not cid or cid in existing_ids:
                continue
            
            node = {
                "id": cid,
                "title": cand.get("title", cid),
                "summary": cand.get("summary", ""),
                "priority": cand.get("priority", "P1"),
                "horizon": cand.get("horizon", "H1"),
                "cluster": cand.get("cluster", "08 Relay / Work convergence"),
                "admission_state": "ISSUE_INTAKE",
                "authority_required": "none",
                "bounded_contexts": ["Validation / Assurance", "Relay Governance"],
                "canonical_id": None,
                "canonical_revision": None,
                "canonical_system": None,
                "confidence": {"label": "high", "raw": "high", "score": 0.95},
                "criticality": "medium",
                "epistemic_status": "PROPOSED",
                "evidence_required": [],
                "exit_criteria": cand.get("exit_criteria", ["Verify and complete implementation."]),
                "items": [],
                "kind": "task_candidate",
                "metrics": {},
                "observed_at": None,
                "projection_run_id": f"debt-curation-{train_id}",
                "provenance": [],
                "readiness": {
                    "max_evidence_age_days": None,
                    "minimum_evidence_grade": None,
                    "operational_identity_required": True,
                    "required_canonical_revision": None,
                    "required_materialization_scopes": []
                },
                "source_claim_ids": [],
                "source_details": [],
                "source_refs": [],
                "staleness_state": "FRESH",
                "tags": ["debt", "curated_by_gemini", "followup"],
                "unlock_score": 2.0,
                "work_status": "open",
                "notes": [f"[CURATED_BY_GEMINI] Synthesized from landed train {train_id} on {relay_mesh.now_iso()}."],
                "completion": {
                    "status": "NOT_DONE",
                    "completed_at": None,
                    "dod_satisfied": False,
                    "evidence": None,
                    "coordination_state": None,
                    "dod_evidence": [],
                    "evidence_fresh_until": None,
                    "evidence_grade": None,
                    "evidence_grade_original": None,
                    "evidence_grade_reason": None,
                    "evidence_observed_at": None,
                    "materialization_scope": None,
                    "residuals": [],
                    "superseded_by": []
                }
            }
            graph_raw["nodes"].append(node)
            existing_ids.add(cid)
            added.append(node)
            log(f"📌 Graphified new technical debt task: '{cid}' ({node['title']})")

        if added:
            relay_io.write_json_atomic(GRAPH_FILE, graph_raw)
            log(f"💾 Ingested {len(added)} curated task node(s) into work-graph.json.")
        return added

    except Exception as exc:
        log(f"Debt graphification error: {exc}")
        return []


# -----------------------------------------------------------------------------
# 2. Frontier Evaluation & Graph Heuristics
# -----------------------------------------------------------------------------

def get_actionable_frontier(limit: int = 15) -> List[Dict[str, Any]]:
    """Evaluates the work graph DAG and returns the top unblocked actionable tasks."""
    proc = run_cmd([sys.executable, str(GRAPH_OPS), "--graph", str(GRAPH_FILE), "next", "--limit", str(limit)])
    if proc.returncode == 0:
        try:
            return json.loads(proc.stdout)
        except Exception:
            pass
    return []


def get_active_train_task_ids() -> set[str]:
    """Collects task IDs currently owned by active non-terminal trains."""
    active_ids = set()
    if not relay_mesh.TRAINS_DIR.exists():
        return active_ids

    for td in relay_mesh.TRAINS_DIR.iterdir():
        if not td.is_dir():
            continue
        st = relay_io.read_json(td / "STATE.json") or {}
        if st.get("tombstone") or st.get("status") == relay_mesh.TERMINAL_STATUS:
            continue
        for tid in st.get("task_ids", []):
            active_ids.add(tid)
    return active_ids


# -----------------------------------------------------------------------------
# 3. Pipeline Horizon Stager (Zero Worker Starvation)
# -----------------------------------------------------------------------------

def stage_pipeline_horizon(target_buffer: int = 4) -> int:
    """Proactively feeds top unblocked tare.tools backlog tasks into release trains."""
    active_trains = []
    if relay_mesh.TRAINS_DIR.exists():
        for td in relay_mesh.TRAINS_DIR.iterdir():
            if not td.is_dir():
                continue
            st = relay_io.read_json(td / "STATE.json") or {}
            status = st.get("status", "")
            if not st.get("tombstone") and status not in (
                relay_mesh.TERMINAL_STATUS, "DEFERRED_BLOCKED_DIRTY_TARGET",
                "DEFERRED_BLOCKED_RAW_EXPORT", "DEFERRED_BLOCKED_CI_REGRESSION_02"
            ):
                active_trains.append(st.get("train_id", td.name))

    needed = target_buffer - len(active_trains)
    if needed <= 0:
        log(f"Pipeline horizon healthy: {len(active_trains)} active train(s) in flight.")
        return 0

    log(f"Pipeline horizon requires {needed} new train(s) to saturate cluster capacity ({len(active_trains)}/{target_buffer}).")
    frontier = get_actionable_frontier(limit=20)
    if not frontier:
        log("No unblocked frontier tasks available in work-graph.json.")
        return 0

    active_tasks = get_active_train_task_ids()
    staged_count = 0

    # Determine highest existing train number
    highest_num = 30
    if relay_mesh.TRAINS_DIR.exists():
        for td in relay_mesh.TRAINS_DIR.iterdir():
            m = re.match(r"TRAIN-(\d+)", td.name)
            if m:
                highest_num = max(highest_num, int(m.group(1)))

    for task in frontier:
        if staged_count >= needed:
            break
        tid = task.get("id")
        if not tid or tid in active_tasks:
            continue

        highest_num += 1
        slug = re.sub(r"[^A-Z0-9]+", "-", tid.upper()).strip("-")[:32]
        new_train_id = f"TRAIN-{highest_num:02d}-{slug}-01"
        title = task.get("title", tid)

        log(f"🚂 Staging next tare.tools backlog task '{tid}' as '{new_train_id}'...")
        try:
            train_path = relay_mesh.init_train(
                train_id=new_train_id,
                title=title,
                task_ids=[tid],
                planner_agent="codex_planner"
            )
            if train_path and train_path.exists():
                active_tasks.add(tid)
                staged_count += 1
                log(f"✅ Initialized and queued '{new_train_id}' for autonomous plan audit.")
        except Exception as exc:
            log(f"Failed staging train for '{tid}': {exc}")

    return staged_count


# -----------------------------------------------------------------------------
# 4. Main Comprehensive Bookkeeping & Curation Cycle
# -----------------------------------------------------------------------------

def run_bookkeeping_cycle(landed_train_id: Optional[str] = None) -> Dict[str, Any]:
    """Executes a full bookkeeping, AI curation, and pipeline staging cycle."""
    log("=" * 75)
    log("  📚 STARTING REFORMED AI BOOKKEEPING & HORIZON CURATION CYCLE")
    log("=" * 75)

    results = {
        "timestamp": relay_mesh.now_iso(),
        "landed_train": landed_train_id,
        "curated_debts": [],
        "staged_trains": 0,
        "graph_validation": "UNKNOWN",
        "node_count": 0,
        "edge_count": 0,
        "unblocked_frontier": [],
        "exports": []
    }

    # Step 1: AI-Driven Technical Debt Graphification
    if landed_train_id:
        new_debts = extract_and_graphify_debts(landed_train_id)
        results["curated_debts"] = [d.get("id") for d in new_debts]

    # Step 2: Validate work-graph.json
    val_proc = run_cmd([sys.executable, str(GRAPH_OPS), "--graph", str(GRAPH_FILE), "validate"])
    if val_proc.returncode == 0:
        try:
            val_data = json.loads(val_proc.stdout)
            results["graph_validation"] = val_data.get("status", "PASS")
            results["node_count"] = val_data.get("node_count", 0)
            results["edge_count"] = val_data.get("edge_count", 0)
            log(f"Graph validated: {results['node_count']} nodes, {results['edge_count']} edges (Status: PASS)")
        except Exception:
            results["graph_validation"] = "PASS"
    else:
        log(f"Graph validation failed: {val_proc.stderr}")
        results["graph_validation"] = "FAIL"

    # Step 3: Re-export HTML visualizer & Mermaid diagram
    html_file = RELAY_ROOT / "work-graph.html"
    run_cmd([sys.executable, str(GRAPH_OPS), "--graph", str(GRAPH_FILE),
             "export", "--output", str(html_file), "--export-format", "html"])
    results["exports"].append("work-graph.html")

    mermaid_file = RELAY_ROOT / "work-graph.mermaid"
    run_cmd([sys.executable, str(GRAPH_OPS), "--graph", str(GRAPH_FILE),
             "export", "--output", str(mermaid_file), "--export-format", "mermaid"])
    results["exports"].append("work-graph.mermaid")
    log("Exported work-graph.html and work-graph.mermaid projections.")

    # Step 4: Proactive Horizon Staging (Feed the autonomous worker cluster)
    staged = stage_pipeline_horizon(target_buffer=4)
    results["staged_trains"] = staged

    # Step 5: Query unblocked frontier
    results["unblocked_frontier"] = get_actionable_frontier(limit=8)

    # Step 6: Append journal
    append_journal_entry(results)

    relay_mesh.update_board()
    log("=" * 75)
    log(f"🏁 Bookkeeping & Horizon Curation Complete. Staged {staged} new train(s).")
    log("=" * 75)
    return results


def append_journal_entry(results: Dict[str, Any]):
    try:
        frontier_md = ""
        for i, task in enumerate(results.get("unblocked_frontier", []), start=1):
            frontier_md += (f"{i}. 🎯 **`{task.get('id')}`** (Score: {task.get('score')}) — "
                            f"{task.get('title')} (`{task.get('cluster', 'general')}`)\n")
        if not frontier_md:
            frontier_md = "_Nenhuma tarefa aberta na fronteira no momento._\n"

        debts_md = ""
        if results.get("curated_debts"):
            debts_md = f"\n- **New debts synthesized by AI (Gemini 3.7):** {', '.join(f'`{d}`' for d in results.get('curated_debts'))}"

        entry = f"""
## 📖 Bookkeeping & Horizon Curation Journal — {results.get('timestamp')}
- **Trem Pousado:** `{results.get('landed_train') or 'Grooming Periódico'}`
- **Status do Grafo:** `{results.get('graph_validation')}` ({results.get('node_count')} nós, {results.get('edge_count')} arestas)
- **Trens Injetados no Horizonte:** `{results.get('staged_trains')} novo(s) trem(ns)`{debts_md}
- **Artefatos Reexportados:** {', '.join(f'`{e}`' for e in results.get('exports', []))}

### 🔓 Topologia da Próxima Fronteira Destravada:
{frontier_md}
---
"""
        existing = ""
        if BOOKKEEPING_LOG.exists():
            existing = BOOKKEEPING_LOG.read_text(encoding="utf-8-sig")
        else:
            existing = "# 📚 tare.tools Backlog & Graph Bookkeeping Journal\n\nRegistro contábil executivo e curadoria topológica do grafo de trabalho.\n\n---\n"

        relay_io.write_text_atomic(BOOKKEEPING_LOG, existing + entry)
    except Exception as e:
        log(f"Failed writing bookkeeping log: {e}")


if __name__ == "__main__":
    train_arg = sys.argv[1] if len(sys.argv) > 1 else None
    res = run_bookkeeping_cycle(train_arg)
