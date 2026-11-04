#!/usr/bin/env python3
"""Gate-perf governance (B4, specs/40-features/gate-perf-governance.md).

Deterministic metric->suggestion->decision: after a scenarios gate run, three
zero-LLM rules over ALREADY-RECORDED artifacts open intake items (source
`governance`) for the human to triage. Advisory-only: never applies anything,
never blocks a gate, never calls a model. Dedup/cooldown live in the intake
queue itself — no new state store.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct --self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib.common import read_json

_DEFAULTS = {"flakeWindow": 5, "flakeMin": 2, "hotScenarioS": 45.0,
             "degradeFactor": 1.25, "cooldownDays": 7, "enabled": True}


def _config(root: Path) -> dict[str, Any]:
    cfg = (read_json(root / ".harness" / "project.json", {}) or {}).get("gateGovernance", {})
    out = dict(_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in _DEFAULTS if k in cfg})
    return out


def _perf_runs(root: Path, window: int) -> list[list[dict[str, Any]]]:
    """Last `window` gate-perf run rows (oldest->newest)."""
    out: list[list[dict[str, Any]]] = []
    p = root / ".harness" / "runs" / "gate-perf.jsonl"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line).get("rows", []))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out[-window:]


def rule_flake_reopen(runs: list[list[dict[str, Any]]], min_hits: int) -> list[tuple[str, str]]:
    """Scenario failed its FIRST attempt in >= min_hits of the given runs."""
    hits: dict[str, int] = {}
    for rows in runs:
        seen: set[str] = set()
        for r in rows:
            if r.get("attempt") == "first" and r.get("rc") not in (0,) and r["scenario"] not in seen:
                seen.add(r["scenario"])
                hits[r["scenario"]] = hits.get(r["scenario"], 0) + 1
    return [(s, f"primeira tentativa falhou em {n} das ultimas {len(runs)} rodadas do gate "
                f"(regra >=2/5: bug reaberto, nao ruido)")
            for s, n in sorted(hits.items()) if n >= min_hits]


def rule_scenario_hot(runs: list[list[dict[str, Any]]], hot_s: float) -> list[tuple[str, str]]:
    """Scenario subprocess wall over the threshold in the LATEST run."""
    if not runs:
        return []
    return [(r["scenario"], f"subprocess {float(r.get('subprocessS') or 0):.0f}s > {hot_s:.0f}s na ultima rodada")
            for r in runs[-1]
            if r.get("attempt") == "first" and float(r.get("subprocessS") or 0) > hot_s]


def rule_gate_degraded(walls: list[float], factor: float) -> str | None:
    """Median wall of the last 3 runs vs the median of the 10 before them."""
    if len(walls) < 6:
        return None
    import statistics
    recent, base = walls[-3:], walls[-13:-3]
    m_recent, m_base = statistics.median(recent), statistics.median(base)
    if m_base > 0 and m_recent > factor * m_base:
        return (f"mediana das ultimas 3 rodadas {m_recent:.0f}s > {factor:.2f}x a mediana "
                f"das {len(base)} anteriores ({m_base:.0f}s)")
    return None


def _scenario_walls(root: Path) -> list[float]:
    data = read_json(root / ".harness" / "state" / "cost-metrics.json", {}) or {}
    return [float(r.get("durationS") or 0) for r in data.get("records", [])
            if r.get("kind") == "gate" and r.get("gate") == "scenarios"]


def _blocked_by_queue(entries: list[dict[str, Any]], marker: str, cooldown_days: int,
                      now: _dt.datetime) -> bool:
    """Dedup (same marker pending) or cooldown (same marker decided recently)."""
    for e in entries:
        if not str(e.get("ask", "")).startswith(marker):
            continue
        if e.get("status") == "pending":
            return True
        decided = str(e.get("decidedAt") or "")
        try:
            if decided and (now - _dt.datetime.fromisoformat(decided)).days < cooldown_days:
                return True
        except ValueError:
            continue
    return False


def evaluate(root: Path) -> list[str]:
    """Compute the advisories this run would open (marker-prefixed asks). Pure read."""
    cfg = _config(root)
    if not cfg.get("enabled", True):
        return []
    runs = _perf_runs(root, int(cfg["flakeWindow"]))
    out: list[str] = []
    for stem, why in rule_flake_reopen(runs, int(cfg["flakeMin"])):
        out.append(f"[gov:flake-reopen:{stem}] Cenario {stem}: {why}. Triar como bug (forensics em "
                   f".harness/runs/scenario-forensics/{stem}-*).")
    for stem, why in rule_scenario_hot(runs, float(cfg["hotScenarioS"])):
        out.append(f"[gov:scenario-hot:{stem}] Cenario {stem}: {why}. Candidato a medicao/otimizacao "
                   f"(licao m4/EXP-13: um cenario quente pode esconder bug sistemico).")
    why = rule_gate_degraded(_scenario_walls(root), float(cfg["degradeFactor"]))
    if why:
        out.append(f"[gov:gate-degraded:self] Gate scenarios degradou: {why}. Revisar gate-perf.jsonl "
                   f"e cost-metrics antes que vire normal.")
    return out


def advise(root: Path) -> int:
    """Post-gate hook body: open deduped/cooled-down advisories as intake items.
    Fail-open — governance must never break the gate."""
    try:
        asks = evaluate(root)
        if not asks:
            return 0
        from harness_lib import intake_queue
        entries = (read_json(root / ".harness" / "state" / "intake-queue.json", {}) or {}).get("entries", [])
        now = _dt.datetime.now(_dt.timezone.utc).astimezone()
        opened = 0
        for ask in asks:
            marker = ask.split("]", 1)[0] + "]"
            if _blocked_by_queue(entries, marker, int(_config(root)["cooldownDays"]), now):
                continue
            intake_queue.add(root, ask, source="governance")
            opened += 1
        if opened:
            print(f"gate-governance: {opened} advisory(ies) -> intake queue (triar no painel)", flush=True)
        return opened
    except Exception:
        return 0


def _self_check() -> int:
    runs = [
        [{"scenario": "a", "attempt": "first", "rc": 1, "subprocessS": 5},
         {"scenario": "a", "attempt": "-retry", "rc": 0, "subprocessS": 5}],
        [{"scenario": "a", "attempt": "first", "rc": 0, "subprocessS": 5}],
        [{"scenario": "a", "attempt": "first", "rc": 1, "subprocessS": 5},
         {"scenario": "b", "attempt": "first", "rc": 0, "subprocessS": 50}],
    ]
    fl = rule_flake_reopen(runs, 2)
    assert len(fl) == 1 and fl[0][0] == "a", fl          # a failed-first twice; b never
    assert rule_flake_reopen(runs, 3) == []
    hot = rule_scenario_hot(runs, 45.0)
    assert [h[0] for h in hot] == ["b"], hot             # latest run only, retry rows ignored
    assert rule_gate_degraded([400] * 10 + [400, 600, 600, 600], 1.25) is not None
    assert rule_gate_degraded([400] * 10 + [400, 420, 430, 410], 1.25) is None
    assert rule_gate_degraded([400, 500], 1.25) is None  # not enough history
    now = _dt.datetime.now(_dt.timezone.utc).astimezone()
    q = [{"ask": "[gov:flake-reopen:a] x", "status": "pending"},
         {"ask": "[gov:scenario-hot:b] y", "status": "done", "decidedAt": now.isoformat()}]
    assert _blocked_by_queue(q, "[gov:flake-reopen:a]", 7, now) is True     # dedup
    assert _blocked_by_queue(q, "[gov:scenario-hot:b]", 7, now) is True     # cooldown
    assert _blocked_by_queue(q, "[gov:gate-degraded:self]", 7, now) is False
    print("gate_governance self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
