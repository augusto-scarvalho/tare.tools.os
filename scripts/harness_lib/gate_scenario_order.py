#!/usr/bin/env python3
"""Fail-fast scenario ordering for the `scenarios` gate (EXP-11 v2).

v1 lesson (2026-07-16, shelved then reopened): promoting EVERY flaky scenario
clustered the heavy ones (HTTP servers, engine spawns) back-to-back and doubled
same-subcheck failures — alphabetical order had been giving them OS-resource
spacing (ports, process table, AV bursts) for free. v1 only knew 9 durations
(cost-metrics top-5), so heavies like m5_ui_panel counted "unknown" and were
promoted with the cheap ones.

v2, powered by EXP-12's full per-attempt sidecar (.harness/runs/gate-perf.jsonl,
every scenario's real subprocess wall):

- promote to the FRONT only scenarios that are flaky (forensics history) AND
  measurably cheap (known duration < CHEAP_CUTOFF_S) — fast failure signal
  in the first ~minute;
- EVERYTHING ELSE keeps its alphabetical position: heavy flakies, slow-greens
  and unknowns stay spread out exactly as before (spacing preserved by
  minimal displacement — we only move what is provably cheap).

Coverage untouched: full battery, never skips (owner constraint). Fail-open to
plain alphabetical. Reversal: swap fail_fast_order() back to sorted(glob).

Call-site constraint: compute BEFORE scenario_isolation.hold_dirty_baseline()
— forensics/sidecar/cost-metrics are class D and absent from the materialized
tree.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CHEAP_CUTOFF_S = 10.0  # same tail cutoff as tools/gate_perf_report.py (EXP-12)

# "<stem>[-retry]-YYYYmmdd-HHMMSS" -> stem (forensics dir naming, spec_test_gate F4b)
_FORENSIC_RE = re.compile(r"^(?P<stem>.+?)(?:-retry)?-\d{8}-\d{6}$")


def forensic_stems(root: Path) -> set[str]:
    out: set[str] = set()
    base = root / ".harness" / "runs" / "scenario-forensics"
    try:
        for d in base.iterdir():
            m = _FORENSIC_RE.match(d.name)
            if m:
                out.add(m.group("stem"))
    except OSError:
        pass
    return out


def known_durations(root: Path) -> dict[str, float]:
    """Latest known subprocess wall seconds per scenario stem.

    Primary source: the EXP-12 sidecar (full per-attempt coverage). Fallback
    merge: cost-metrics gate records' `slowest` top-5 (pre-sidecar history)."""
    out: dict[str, float] = {}
    try:
        data = json.loads((root / ".harness" / "state" / "cost-metrics.json")
                          .read_text(encoding="utf-8"))
        for rec in data.get("records", []):
            if rec.get("kind") != "gate":
                continue
            for s in rec.get("slowest", []) or []:
                name = str(s.get("name") or "")
                if name.startswith("scenario:"):
                    try:
                        out[name.split(":", 1)[1]] = float(s.get("durationS") or 0)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    try:
        sidecar = root / ".harness" / "runs" / "gate-perf.jsonl"
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            try:
                run = json.loads(line)
            except json.JSONDecodeError:
                continue
            for r in run.get("rows", []):
                stem = str(r.get("scenario") or "")
                try:
                    if stem:
                        out[stem] = float(r.get("subprocessS") or 0)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return out


def order_stems(stems: list[str], flaky: set[str], dur_s: dict[str, float]) -> list[str]:
    """Pure v2 ordering: cheap-flaky first (duration asc), all else alphabetical
    in place. Only provably-cheap scenarios move — spacing preserved."""
    front = sorted((s for s in stems if s in flaky and 0 < dur_s.get(s, 0.0) < CHEAP_CUTOFF_S),
                   key=lambda s: (dur_s[s], s))
    rest = sorted(s for s in stems if s not in front)
    return front + rest


def fail_fast_order(root: Path, paths: list[Path] | None = None) -> list[Path]:
    """Ordered scenario paths for the gate loop. Fail-open to alphabetical.

    `paths` lets the caller supply an ALREADY-DISCOVERED scenario set (the
    candidate-tree projection frozen from the validation pin) so discovery is a
    projection of the immutable subject, not a glob over the developer working
    tree. `paths=None` keeps the incumbent working-tree glob byte-for-byte. Only
    DISCOVERY changes here; the deterministic cheap-flaky-first ORDERING policy
    below is reused unchanged, never duplicated."""
    paths = sorted(paths) if paths is not None else sorted((root / "testing" / "scenarios").glob("*.py"))
    try:
        by_stem = {p.stem: p for p in paths}
        ordered = order_stems(list(by_stem), forensic_stems(root), known_durations(root))
        return [by_stem[s] for s in ordered]
    except Exception:
        return paths


def _self_check() -> int:
    stems = ["a_alpha", "m_heavy_flaky", "b_cheap_flaky", "c_cheap_flaky", "z_tail", "u_unknown_flaky"]
    flaky = {"m_heavy_flaky", "b_cheap_flaky", "c_cheap_flaky", "u_unknown_flaky"}
    dur = {"m_heavy_flaky": 60.0, "b_cheap_flaky": 3.0, "c_cheap_flaky": 1.5, "a_alpha": 2.0, "z_tail": 2.0}
    got = order_stems(stems, flaky, dur)
    # cheap flakies promoted (duration asc); heavy flaky and UNKNOWN flaky stay alphabetical
    assert got == ["c_cheap_flaky", "b_cheap_flaky", "a_alpha", "m_heavy_flaky", "u_unknown_flaky", "z_tail"], got
    assert sorted(got) == sorted(stems)          # full battery, no dupes
    assert order_stems(stems, set(), dur) == sorted(stems)   # no flakies -> alphabetical
    assert order_stems(stems, flaky, {}) == sorted(stems)    # no durations -> alphabetical (fail-open)
    m = _FORENSIC_RE.match("worker_live_tail-retry-20260715-213012")
    assert m and m.group("stem") == "worker_live_tail"
    print("gate_scenario_order self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
