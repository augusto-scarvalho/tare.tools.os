#!/usr/bin/env python3
"""wf-gate-observability — the read-only `gate-perf` verb over gate-perf.jsonl.

Aggregates the sidecar `spec_test_gate._run_isolated_scenario` ALREADY writes
(one `{scenario,attempt,snapshotS,subprocessS,restoreS,rc}` row per attempt,
flushed as `{"at",...,"rows":[...]}` per gate run). This module adds NO timing
capture — it only summarizes existing data — so the 7-15min gate stays untouched
while surfacing WHERE the time goes (Theory of Constraints: you can't exploit a
constraint you can't see):

  - per-scenario count, p50 + max of `subprocessS`, total attributed time, and
    share-of-gate % (ranked slowest-first — the drum's hot spots);
  - the snapshot/restore isolation tax vs subprocess body (the per-attempt
    overhead the parallel/pipelining WF items must beat).

Reserved future counters: the cache/speculation WF items (wf-gate-result-cache,
wf-speculative-gate) will emit `cacheHit`/`cacheMiss`/`speculativeMisprediction`
into the SAME rows. Nothing produces them yet — this reader carries any that
appear through the `reserved` block, so those items ship a producer with zero
reader edits. Do NOT fabricate them here.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import sys
import threading
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct self-check execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib.common import ROOT, now

PERF_LOG = ROOT / ".harness" / "runs" / "gate-perf.jsonl"

# W29.N7 (B3-1 consumer + NVIDIA F3 ladder): deterministic tier -> verification
# depth, observe-first. Reuses track_record's burden vocabulary (light/standard/
# heavy) so the overseer reads ONE language across tier and track record and
# calibrates review depth from it (changed hunks are ALWAYS read). Nothing
# branches on it; EXP-33 reads the per-run log written by log_tier_level.
# Lives here (not spec_test_gate) per the gs-7 line ratchet — telemetry home.
EXECUTION_LEVELS = {"low": "light", "medium": "standard", "high": "heavy"}


def execution_level(tier: str) -> str:
    """Unknown tier reads as standard — the fail-closed middle, never silent-light."""
    return EXECUTION_LEVELS.get(tier, "standard")


def log_tier_level(path: Path, gate: str, tier: str | None, status: str, fail_count: int) -> None:
    """W29.N7 observe-first: one (tier, level, result) line per gate run — EXP-33
    reads the series (expand-on-failure rate per tier). Fail-open: telemetry
    loss never breaks the gate.
    # ponytail: append-only ~90B/run; wire log_rotation.spill_if_over if it ever matters."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "gate": gate, "tier": tier,
                                "level": execution_level(str(tier)), "status": status,
                                "failCount": fail_count}) + "\n")
    except Exception:
        pass
RESERVED_COUNTERS = ("cacheHit", "cacheMiss", "speculativeMisprediction")
_TOP = 20  # ponytail: table caps at the 20 hottest; --json returns all


def load_runs(path: Path = PERF_LOG, last: int | None = None) -> list[dict[str, Any]]:
    """Last `last` gate-perf run objects (oldest->newest). Fail-open: a missing,
    empty, or partly-corrupt sidecar yields [] / only the parseable run lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
            runs.append(obj)
    return runs[-last:] if last else runs


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-scenario rollup of `subprocessS` + the snapshot/restore isolation tax.

    share-of-gate % = a scenario's summed subprocessS / the total subprocessS
    across all scenarios. Pure read; empty input -> empty-state dict (runs=0),
    never raises."""
    per: dict[str, dict[str, Any]] = {}
    tax = {"snapshotS": 0.0, "subprocessS": 0.0, "restoreS": 0.0}
    reserved = {k: 0 for k in RESERVED_COUNTERS}
    # SPEC-159 Phase 1 (SHADOW) rollup: cacheHit/cacheMiss ride the reserved block
    # above; wouldSkip/falseSkip are counters and wouldSaveS is a float, so they get
    # their own block. Surfaced ONLY when a producer emits them (back-compat).
    shadow = {"wouldSkip": 0, "falseSkip": 0, "wouldSaveS": 0.0}
    shadow_seen = False
    # wf-subprocess-edges (SPEC-159 v3, SHADOW): the SECOND, subprocess-edge-aware
    # would-skip rollup, mirroring the shadow block above. Surfaced only when a
    # producer stamps the V2 fields (back-compat with pre-v3 sidecars).
    shadow_v2 = {"wouldSkipV2": 0, "falseSkipV2": 0, "wouldSaveV2S": 0.0}
    shadow_v2_seen = False
    attempts = 0
    ats: list[str] = []
    for run in runs:
        if run.get("at"):
            ats.append(str(run["at"]))
        for r in run.get("rows", []):
            name = str(r.get("scenario") or "(unknown)")
            sub = float(r.get("subprocessS") or 0)
            tax["snapshotS"] += float(r.get("snapshotS") or 0)
            tax["subprocessS"] += sub
            tax["restoreS"] += float(r.get("restoreS") or 0)
            attempts += 1
            s = per.setdefault(name, {"scenario": name, "count": 0, "_subs": []})
            s["count"] += 1
            s["_subs"].append(sub)
            for k in RESERVED_COUNTERS:
                if r.get(k) is not None:
                    reserved[k] += int(r.get(k) or 0)
            if "wouldSkip" in r or "falseSkip" in r or "wouldSaveS" in r:
                shadow_seen = True
                if r.get("wouldSkip"):
                    shadow["wouldSkip"] += 1
                if r.get("falseSkip"):
                    shadow["falseSkip"] += 1
                shadow["wouldSaveS"] += float(r.get("wouldSaveS") or 0)
            if "wouldSkipV2" in r or "falseSkipV2" in r or "affectedV2" in r:
                shadow_v2_seen = True
                if r.get("wouldSkipV2"):
                    shadow_v2["wouldSkipV2"] += 1
                if r.get("falseSkipV2"):
                    shadow_v2["falseSkipV2"] += 1
                shadow_v2["wouldSaveV2S"] += float(r.get("wouldSaveV2S") or 0)
    total_sub = tax["subprocessS"]
    scenarios = []
    for s in per.values():
        subs = s.pop("_subs")
        tot = sum(subs)
        scenarios.append({
            "scenario": s["scenario"],
            "count": s["count"],
            "p50S": round(statistics.median(subs), 3) if subs else 0.0,
            "maxS": round(max(subs), 3) if subs else 0.0,
            "totalS": round(tot, 3),
            "sharePct": round(100 * tot / total_sub, 1) if total_sub else 0.0,
        })
    scenarios.sort(key=lambda x: (x["totalS"], x["scenario"]), reverse=True)
    overhead = tax["snapshotS"] + tax["restoreS"]
    wall = overhead + total_sub
    out: dict[str, Any] = {
        "runs": len(runs),
        "attempts": attempts,
        "window": {"first": min(ats) if ats else None, "last": max(ats) if ats else None},
        "scenarios": scenarios,
        "isolationTax": {
            "snapshotS": round(tax["snapshotS"], 3),
            "restoreS": round(tax["restoreS"], 3),
            "subprocessS": round(total_sub, 3),
            "overheadS": round(overhead, 3),
            "overheadPct": round(100 * overhead / wall, 1) if wall else 0.0,
        },
    }
    if any(reserved.values()):  # only once a producer actually emits them
        out["reserved"] = reserved
    if shadow_seen:  # SPEC-159 Phase 1: what a scenario-level cache WOULD skip + its safety
        out["shadow"] = {"wouldSkip": shadow["wouldSkip"], "falseSkip": shadow["falseSkip"],
                         "wouldSaveS": round(shadow["wouldSaveS"], 3)}
    if shadow_v2_seen:  # wf-subprocess-edges (SPEC-159 v3): the edge-aware would-skip + its safety
        out["shadowV2"] = {"wouldSkipV2": shadow_v2["wouldSkipV2"], "falseSkipV2": shadow_v2["falseSkipV2"],
                           "wouldSaveV2S": round(shadow_v2["wouldSaveV2S"], 3)}
    return out


# --- SPEC-159 Phase-2 flip decision (deterministic; EXP-29) ------------------

FALSESKIP_BOUND_TARGET = 1 / 200  # success needs the upper bound under this
STABLE_RUNS_TARGET = 20           # ...over >=20 consecutive stable-gate runs

# ponytail: causeClass is DETERMINISTIC, never LLM. A falseSkip is a `flake`
# ONLY on an in-run retry-recovery (the scenario failed then passed on -retry in
# the SAME run: row-pair signal) or when its id is on the declared in-repo flake
# list; every other cache/verdict divergence is a `cache-fault` (a cache that
# WOULD have shipped a false green). Upgrade path: widen DECLARED_FLAKES or add a
# quarantine registry if a genuinely-flaky scenario keeps landing as cache-fault.
DECLARED_FLAKES: frozenset[str] = frozenset()


def _cause_class(sid: Any, recovered_in_run: set, declared=DECLARED_FLAKES) -> str:
    return "flake" if (sid in recovered_in_run or sid in declared) else "cache-fault"


def _falseskip_bound(n: int, k: int, alpha: float = 0.05) -> float:
    """One-sided upper confidence bound on a Bernoulli rate from `k` events in `n`
    trials, via a Beta(1,1)-mixture test supermartingale (anytime-valid / time-
    uniform: valid at every n by Ville's inequality, so peeking at each gate run is
    honest). Returns the largest rate `p` NOT rejected at level `alpha` — the upper
    edge of the confidence sequence. `n<=0` -> 1.0 (no evidence -> worst case).

    Martingale M_n(p) = B(k+1,n-k+1) / (p^k (1-p)^(n-k)); reject `p` when
    M_n(p) >= 1/alpha. The set {p: M_n(p) < 1/alpha} is an interval around the MLE
    k/n; we bisect its UPPER root. stdlib math only."""
    if n <= 0:
        return 1.0
    k = min(max(k, 0), n)
    ln_b = math.lgamma(k + 1) + math.lgamma(n - k + 1) - math.lgamma(n + 2)
    thresh = math.log(alpha) + ln_b  # solve k*ln p + (n-k)*ln(1-p) == thresh

    def f(p: float) -> float:  # > 0 inside the confidence set, decreasing past the MLE
        val = -thresh
        if k:
            val += k * math.log(p)
        if n - k:
            val += (n - k) * math.log1p(-p)
        return val

    lo, hi = k / n, 1.0 - 1e-15  # MLE (denominator's max) .. p->1 (M_n -> inf)
    if f(lo) <= 0:  # degenerate: MLE already on/over the edge
        return lo
    for _ in range(200):  # bisection; ~60 steps saturate float64, 200 is belt+braces
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def shadow_verdict(runs: list[dict[str, Any]], alpha: float = 0.05) -> dict[str, Any]:
    """The SPEC-159 Phase-2 flip decision over the shadow ledger (EXP-29). Reads the
    wouldSkip/falseSkip/wouldSaveS the gate already stamps (`gate_affected.shadow_row`)
    and answers ONE question: would turning scenario-level skipping ON ship a false
    green? A falseSkip is the safety violation; its causeClass is deterministic
    (`_cause_class`). boundMet (the flip is allowed) requires ALL of: the anytime-valid
    upper bound on the false-skip rate under 1/200, a `STABLE_RUNS_TARGET` trailing
    streak of skip-active runs with no cache-fault falseSkip, and zero cache-fault
    falseSkips overall. Pure; degrades on malformed rows, never raises."""
    would_skip_runs = 0
    would_save_s = 0.0
    false_skips: list[dict[str, Any]] = []
    per_run: list[tuple[bool, bool]] = []  # (skipActive, hasCacheFault)
    for run in runs:
        rows = run.get("rows") or []
        at = run.get("at")
        recovered = {r.get("scenario") for r in rows
                     if "retry" in str(r.get("attempt") or "") and r.get("rc") == 0}
        skip_active = has_cache_fault = False
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("wouldSkip"):
                would_skip_runs += 1
                would_save_s += float(r.get("wouldSaveS") or 0.0)
                skip_active = True
            if r.get("falseSkip"):
                cause = _cause_class(r.get("scenario"), recovered)
                false_skips.append({"at": at, "scenario": r.get("scenario"), "causeClass": cause})
                if cause == "cache-fault":
                    has_cache_fault = True
        per_run.append((skip_active, has_cache_fault))
    # trailing stable streak: newest-first, a cold run (no wouldSkip) or a cache-fault
    # falseSkip ends it — neither proves a skip would have been safe.
    stable_runs = 0
    for skip_active, has_cache_fault in reversed(per_run):
        if skip_active and not has_cache_fault:
            stable_runs += 1
        else:
            break
    cache_fault_false_skips = sum(1 for fs in false_skips if fs["causeClass"] == "cache-fault")
    bound_upper = _falseskip_bound(would_skip_runs, len(false_skips), alpha)
    bound_met = (bound_upper < FALSESKIP_BOUND_TARGET and stable_runs >= STABLE_RUNS_TARGET
                 and cache_fault_false_skips == 0)
    return {
        "gateRuns": len(runs),
        "stableRuns": stable_runs,
        "wouldSkipRuns": would_skip_runs,
        "wouldSaveS": round(would_save_s, 3),
        "falseSkips": false_skips,
        "cacheFaultFalseSkips": cache_fault_false_skips,
        "boundUpper": round(bound_upper, 6),
        "boundTarget": FALSESKIP_BOUND_TARGET,
        "alpha": alpha,
        "boundMet": bound_met,
        "verdict": "flip" if bound_met else "deferred",
    }


def divergence_report(root: Path | str) -> dict[str, Any]:
    """Serial↔parallel divergence probe (owner direction 2026-07-21: serial
    divergences at the end of loops matter). Reads the gate-perf sidecar, takes
    the LATEST serial-attempt scenarios run and the LATEST parallel one, and
    diffs their per-scenario rc maps — any mismatch (or scenario present on one
    side only) is a flake-candidate to root-cause, not noise. Pure reader; run
    `harness-test.py scenarios --serial` first if the serial side is stale."""
    def rc_map(flush: dict[str, Any]) -> dict[str, Any]:
        return {r["scenario"]: r.get("rc") for r in flush.get("rows") or []
                if r.get("scenario") and r["scenario"] != "_parallel_wall"}

    latest: dict[str, dict[str, Any] | None] = {"serial": None, "parallel": None}
    try:
        text = (Path(root) / ".harness" / "runs" / "gate-perf.jsonl").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        try:
            flush = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows = flush.get("rows") or []
        if not rows:
            continue
        kind = "parallel" if any(r.get("attempt") == "parallel" for r in rows) else "serial"
        latest[kind] = flush
    if not latest["serial"] or not latest["parallel"]:
        return {"status": "insufficient", "haveSerial": bool(latest["serial"]),
                "haveParallel": bool(latest["parallel"]),
                "hint": "need one serial AND one parallel scenarios run in the sidecar window"}
    s, p = rc_map(latest["serial"]), rc_map(latest["parallel"])
    keys = sorted((set(s) ^ set(p)) | {k for k in set(s) & set(p) if s[k] != p[k]})
    return {"status": "ok",
            "serialAt": latest["serial"].get("at"), "parallelAt": latest["parallel"].get("at"),
            "counts": {"serial": len(s), "parallel": len(p), "divergences": len(keys)},
            "divergences": [{"scenario": k, "serialRc": s.get(k), "parallelRc": p.get(k)}
                            for k in keys]}


def cmd_gate_divergence(_args: Any) -> None:
    """`harness.py gate-divergence` — print the probe (registered in cli_registry)."""
    print(json.dumps(divergence_report(ROOT), indent=2, ensure_ascii=False))


PARTIAL_LOG_NAME = "gate-perf-partial.jsonl"


def recover_orphan(partial_path: Path) -> int:
    """Fold a hard-killed run's streamed rows into the canonical sidecar next to it
    as ONE `{at, rows, partial: true}` line (`at` = the orphan's mtime, i.e. when
    the dead run last wrote), then drop the orphan. Returns rows recovered, 0 when
    there is no orphan. Undecodable lines are skipped — a kill can truncate the
    last write mid-line. Fail-open: a fold that cannot be written keeps the orphan
    (its rows then ride the NEXT fold) instead of losing it.

    Fold and drop are SEPARATE steps because they fail differently: once the rows
    are in the sidecar the orphan must go, and `unlink` can lose to a transient
    Windows lock (WinError 32 is recorded reality here — `spec_test_gate.py:247`),
    so a failed unlink falls back to truncating the file. Only if BOTH fail do the
    rows stay, and then they re-fold next run as a second `partial: true` line.

    # ponytail: that residue plus a kill in the microseconds between flush_run's
    # write and its cleanup are the two double-count paths left; the `partial` flag
    # makes both visible — dedupe by row fingerprint only if it ever matters."""
    try:
        lines = partial_path.read_text(encoding="utf-8", errors="replace").splitlines()
        at = dt.datetime.fromtimestamp(partial_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return 0
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if rows:
        try:
            from harness_lib import log_rotation
            sidecar = partial_path.with_name("gate-perf.jsonl")
            log_rotation.spill_if_over(sidecar)  # same rotation flush_run does before appending
            with sidecar.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": at, "rows": rows, "partial": True}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — fold failed: keep the orphan whole for the next try
            return 0
    try:
        partial_path.unlink()
    except OSError:
        try:  # a held file cannot be unlinked; emptying it prevents a second fold
            partial_path.write_text("", encoding="utf-8")
        except OSError:
            pass  # both failed — the rows WILL re-fold, flagged partial, next run
    return len(rows)


class PartialRows(list):
    """The gate's `_PERF_ROWS` accumulator, except every row ALSO hits disk now.

    `flush_run` writes only at settle, so both 1800s hard-kills on 2026-08-03 left
    ZERO per-scenario telemetry for exactly the two runs worth reading (dossier
    `.harness/handoff/gate-latency-dossier.md`). Rows stream to
    `.harness/runs/<PARTIAL_LOG_NAME>` as they are produced; the NEXT run folds any
    orphan into the canonical sidecar (`recover_orphan`) and `flush_run` removes the
    file at settle, so a healthy run leaves nothing behind and a killed one leaves
    its rows. A plain `list` subclass because every producer (parallel rows, the
    wall, `_parallel_abort`, the serial lane, cache-skips) already appends to the
    ONE list the gate passes around — durability lands at that single seam instead
    of six call sites. Disk work is lazy (first append): importing the gate, which
    every scenario subprocess does, writes nothing. Fail-open throughout.

    The streamed copy is the row AS PRODUCED: later in-place stamps (the SPEC-159
    shadow annotation) reach only the settle flush, so a folded partial run reads
    as shadow-cold — honest, since that run never finished."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._path = Path(root) / ".harness" / "runs" / PARTIAL_LOG_NAME
        self._lock = threading.Lock()  # shard workers may report concurrently
        self._started = False

    def append(self, row: dict[str, Any]) -> None:
        super().append(row)
        self._stream([row])

    def extend(self, rows: Any) -> None:
        rows = list(rows)
        super().extend(rows)
        self._stream(rows)

    def _stream(self, rows: list[dict[str, Any]]) -> None:
        try:
            with self._lock:
                if not self._started:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    recover_orphan(self._path)  # a killed run's rows, before we reuse the file
                    self._started = True
                with self._path.open("a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — measurement must never break the gate
            pass


def flush_run(rows: list[dict[str, Any]], root: Path) -> None:
    """Append ONE gate-perf.jsonl line (`{at, rows}`) for a scenarios run (the
    EXP-12 sidecar, extracted from spec_test_gate). Class-D, log-rotated. No-op on
    empty rows; measurement must NEVER break the gate — a failure leaves a
    `gate-perf.err` breadcrumb and returns. Settle also clears the incremental
    partial stream (`PartialRows`): these rows are now canonical."""
    if not rows:
        return
    try:
        from harness_lib import log_rotation
        from harness_lib.common import now
        perf_log = root / ".harness" / "runs" / "gate-perf.jsonl"
        perf_log.parent.mkdir(parents=True, exist_ok=True)
        log_rotation.spill_if_over(perf_log)
        with perf_log.open("a", encoding="utf-8") as pf:
            pf.write(json.dumps({"at": now(), "rows": rows}, ensure_ascii=False) + "\n")
        perf_log.with_name(PARTIAL_LOG_NAME).unlink(missing_ok=True)
        print(f"gate-perf: {len(rows)} attempt timings -> {perf_log}", flush=True)
    except Exception as exc:  # noqa: BLE001 — measurement must never break the gate
        try:  # breadcrumb: a silent measurement failure is invisible otherwise
            (root / ".harness" / "runs" / "gate-perf.err").write_text(repr(exc), encoding="utf-8")
        except OSError:
            pass


def cmd_gate_perf(args) -> int:
    """`harness.py gate-perf`: read-only aggregate of the gate-perf sidecar; rc 0."""
    runs = load_runs(PERF_LOG, getattr(args, "last", None))
    agg = aggregate(runs)
    if getattr(args, "json", False):
        try:  # SPEC-159 Phase-2 flip decision; the analysis must NEVER raise into the reader
            agg["shadowVerdict"] = shadow_verdict(runs)
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps(agg, indent=2, ensure_ascii=False))
        return 0
    if not agg["runs"]:
        print(f"no gate-perf data in {PERF_LOG} — run the scenarios gate first")
        return 0
    scen = agg["scenarios"]
    top = scen[:_TOP]
    header = ("SCENARIO", "N", "P50s", "MAXs", "TOTALs", "SHARE%")
    table = [(r["scenario"], str(r["count"]), f"{r['p50S']:.2f}", f"{r['maxS']:.2f}",
              f"{r['totalS']:.1f}", f"{r['sharePct']:.1f}") for r in top]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h)
              for i, h in enumerate(header)]
    win = agg["window"]
    print(f"slowest scenarios over last {agg['runs']} gate run(s) "
          f"({agg['attempts']} attempts, {win['first']} .. {win['last']}):\n")
    for row in (header, *table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    tax = agg["isolationTax"]
    print(f"\nisolation tax: snapshot {tax['snapshotS']:.1f}s + restore {tax['restoreS']:.1f}s "
          f"= {tax['overheadS']:.1f}s overhead vs {tax['subprocessS']:.1f}s subprocess "
          f"({tax['overheadPct']:.1f}% of attributed wall)")
    if len(scen) > len(top):
        print(f"... {len(scen) - len(top)} more scenario(s) (--json for all)")
    return 0


def _self_check() -> int:
    runs = [
        {"at": "2026-07-16T18:00:00+00:00", "rows": [
            {"scenario": "slow", "attempt": "first", "snapshotS": 0.2, "subprocessS": 5.0, "restoreS": 0.3, "rc": 0},
            {"scenario": "fast", "attempt": "first", "snapshotS": 0.2, "subprocessS": 1.0, "restoreS": 0.3, "rc": 0}]},
        {"at": "2026-07-16T19:00:00+00:00", "rows": [
            {"scenario": "slow", "attempt": "first", "snapshotS": 0.2, "subprocessS": 7.0, "restoreS": 0.3, "rc": 0},
            {"scenario": "fast", "attempt": "first", "snapshotS": 0.2, "subprocessS": 1.0, "restoreS": 0.3, "rc": 0}]},
    ]
    agg = aggregate(runs)
    assert agg["runs"] == 2 and agg["attempts"] == 4, agg
    assert agg["scenarios"][0]["scenario"] == "slow", agg          # ranked slowest-first
    assert agg["scenarios"][0]["p50S"] == 6.0, agg                 # median(5,7)
    assert agg["scenarios"][0]["maxS"] == 7.0, agg
    assert agg["scenarios"][0]["totalS"] == 12.0, agg
    # share: slow 12 / total subprocess 14 -> 85.7%
    assert agg["scenarios"][0]["sharePct"] == 85.7, agg
    tax = agg["isolationTax"]
    assert tax["subprocessS"] == 14.0 and tax["overheadS"] == 2.0, tax  # 4*(0.2+0.3)
    assert "reserved" not in agg, agg                              # nothing produces them yet
    # reserved surfaces only when a row carries one
    agg2 = aggregate([{"at": "t", "rows": [
        {"scenario": "x", "subprocessS": 1.0, "cacheHit": 3}]}])
    assert agg2["reserved"] == {"cacheHit": 3, "cacheMiss": 0, "speculativeMisprediction": 0}, agg2
    assert "shadow" not in agg2, agg2                              # no shadow fields -> no block
    # SPEC-159 shadow rollup: wouldSkip/falseSkip counted, wouldSaveS summed (float).
    agg3 = aggregate([{"at": "t", "rows": [
        {"scenario": "a", "subprocessS": 4.0, "cacheHit": 1, "cacheMiss": 0,
         "wouldSkip": True, "falseSkip": False, "wouldSaveS": 4.0},
        {"scenario": "b", "subprocessS": 2.0, "cacheHit": 0, "cacheMiss": 1,
         "wouldSkip": False, "falseSkip": False, "wouldSaveS": 0.0}]}])
    assert agg3["shadow"] == {"wouldSkip": 1, "falseSkip": 0, "wouldSaveS": 4.0}, agg3
    assert agg3["reserved"]["cacheHit"] == 1 and agg3["reserved"]["cacheMiss"] == 1, agg3
    # a false-skip is counted (the safety signal Phase 2 gates on).
    agg4 = aggregate([{"at": "t", "rows": [
        {"scenario": "c", "subprocessS": 1.0, "wouldSkip": True, "falseSkip": True, "wouldSaveS": 1.0}]}])
    assert agg4["shadow"]["falseSkip"] == 1, agg4
    # wf-subprocess-edges (SPEC-159 v3): the shadowV2 rollup mirrors the shadow block,
    # counts wouldSkipV2/falseSkipV2 + sums wouldSaveV2S, and is absent without producers.
    assert "shadowV2" not in agg3, agg3                            # v3 fields absent -> no block
    agg5 = aggregate([{"at": "t", "rows": [
        {"scenario": "a", "subprocessS": 4.0, "affectedV2": False,
         "wouldSkipV2": True, "falseSkipV2": False, "wouldSaveV2S": 4.0},
        {"scenario": "b", "subprocessS": 2.0, "affectedV2": False,
         "wouldSkipV2": True, "falseSkipV2": True, "wouldSaveV2S": 2.0}]}])
    assert agg5["shadowV2"] == {"wouldSkipV2": 2, "falseSkipV2": 1, "wouldSaveV2S": 6.0}, agg5
    assert "shadow" not in agg5, agg5                              # V1 shadow fields absent here
    # fail-open: empty / missing input
    empty = aggregate([])
    assert empty["runs"] == 0 and empty["scenarios"] == [] and empty["window"]["first"] is None, empty
    assert load_runs(Path("does-not-exist.jsonl")) == [], "missing file must fail open"

    # divergence_report: latest serial vs latest parallel; rc mismatch + one-sided
    # scenarios surface; wall row excluded; insufficient sides degrade honestly.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / ".harness" / "runs"
        runs.mkdir(parents=True)
        lines = [
            {"at": "t1", "rows": [{"scenario": "x", "attempt": "first", "rc": 0},
                                  {"scenario": "y", "attempt": "first", "rc": 0},
                                  {"scenario": "only_serial", "attempt": "first", "rc": 0}]},
            {"at": "t2", "rows": [{"scenario": "x", "attempt": "parallel", "rc": 0},
                                  {"scenario": "y", "attempt": "parallel", "rc": 1},
                                  {"scenario": "_parallel_wall", "attempt": "parallel", "rc": 1}]},
        ]
        (runs / "gate-perf.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        rep = divergence_report(Path(td))
        assert rep["status"] == "ok" and rep["counts"]["divergences"] == 2, rep
        got = {d["scenario"] for d in rep["divergences"]}
        assert got == {"y", "only_serial"}, rep  # rc-mismatch + one-sided; wall excluded
    assert divergence_report(Path("does-not-exist"))["status"] == "insufficient"

    # --- SPEC-159 Phase-2 flip decision (EXP-29) ---------------------------------
    # bound helper: no evidence -> 1.0; more clean trials tighten it; an event loosens it.
    assert _falseskip_bound(0, 0) == 1.0
    b100 = _falseskip_bound(100, 0)
    assert 0.05 < b100 < 0.1, b100                                # ~0.073 (anytime-valid > fixed-n)
    assert _falseskip_bound(1000, 0) < b100                       # more trials -> tighter
    assert _falseskip_bound(564, 1) > _falseskip_bound(564, 0)    # a falseSkip loosens it
    assert 0.005 < _falseskip_bound(564, 1) < 0.05                # today's shape: ~0.02, over 1/200
    # fixture ledger across today's shapes: a cold run, an in-run retry recovery (-> flake),
    # the m5 cache-fault (retry stayed rc=1 -> NOT recovered), then a clean skip-active run.
    fx = [
        {"at": "cold", "rows": [
            {"scenario": "a", "attempt": "first", "subprocessS": 1.0, "cacheMiss": 1,
             "wouldSkip": False, "falseSkip": False, "wouldSaveS": 0.0}]},
        {"at": "flake", "rows": [
            {"scenario": "b", "attempt": "first", "subprocessS": 2.0, "cacheHit": 1, "rc": 1,
             "wouldSkip": True, "falseSkip": True, "wouldSaveS": 2.0},
            {"scenario": "b", "attempt": "-retry", "subprocessS": 2.0, "rc": 0},        # recovered -> flake
            {"scenario": "c", "attempt": "first", "subprocessS": 3.0, "cacheHit": 1,
             "wouldSkip": True, "falseSkip": False, "wouldSaveS": 3.0}]},
        {"at": "m5run", "rows": [
            {"scenario": "m5", "attempt": "first", "subprocessS": 11.0, "cacheHit": 1, "rc": 1,
             "wouldSkip": True, "falseSkip": True, "wouldSaveS": 11.0},
            {"scenario": "m5", "attempt": "-retry", "subprocessS": 10.0, "rc": 1}]},    # NOT recovered -> cache-fault
        {"at": "clean", "rows": [
            {"scenario": "d", "attempt": "first", "subprocessS": 4.0, "cacheHit": 1,
             "wouldSkip": True, "falseSkip": False, "wouldSaveS": 4.0}]},
    ]
    sv = shadow_verdict(fx)
    assert sv["gateRuns"] == 4 and sv["wouldSkipRuns"] == 4 and sv["wouldSaveS"] == 20.0, sv
    assert {f["scenario"]: f["causeClass"] for f in sv["falseSkips"]} == \
        {"b": "flake", "m5": "cache-fault"}, sv                   # deterministic causeClass
    assert sv["cacheFaultFalseSkips"] == 1, sv
    assert sv["stableRuns"] == 1, sv                              # only the trailing clean run; m5 broke the streak
    assert sv["boundUpper"] > FALSESKIP_BOUND_TARGET and sv["boundMet"] is False \
        and sv["verdict"] == "deferred", sv
    assert shadow_verdict([])["verdict"] == "deferred"            # empty ledger -> no evidence, never raises

    # --- incremental partial stream (survives a hard-kill) -----------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        partial = root / ".harness" / "runs" / PARTIAL_LOG_NAME
        sidecar = root / ".harness" / "runs" / "gate-perf.jsonl"
        pr = PartialRows(root)
        assert not partial.exists(), "construction must not touch disk (scenario subprocesses import the gate)"
        pr.append({"scenario": "a", "subprocessS": 1.0})
        pr.extend([{"scenario": "b", "subprocessS": 2.0}])
        assert list(pr) == [{"scenario": "a", "subprocessS": 1.0}, {"scenario": "b", "subprocessS": 2.0}], list(pr)
        assert len(partial.read_text(encoding="utf-8").splitlines()) == 2, "one durable line per row"
        # HARD KILL here: no flush_run. The next run folds the orphan (+ tolerates a
        # write truncated mid-line) into the sidecar as one partial run, then reuses the file.
        with partial.open("a", encoding="utf-8") as f:
            f.write('{"scenario": "trunc"')
        pr2 = PartialRows(root)
        pr2.append({"scenario": "c", "subprocessS": 3.0})
        folded = load_runs(sidecar)
        assert len(folded) == 1 and folded[0]["partial"] is True, folded
        assert [r["scenario"] for r in folded[0]["rows"]] == ["a", "b"], folded  # truncated line dropped
        assert folded[0]["at"], "folded run keeps the dead run's mtime"
        assert partial.read_text(encoding="utf-8").strip() == json.dumps({"scenario": "c", "subprocessS": 3.0}), \
            "orphan folded, file reused by the live run"
        # settle: rows become canonical, the partial stream is cleared.
        flush_run(list(pr2), root)
        assert not partial.exists(), "settle must clean up the partial stream"
        assert len(load_runs(sidecar)) == 2, load_runs(sidecar)
        assert recover_orphan(partial) == 0, "no orphan -> nothing recovered, no raise"
        # F4: a fold that could not unlink falls back to truncation, so the SAME rows
        # never fold twice — an emptied orphan recovers nothing and leaves no line.
        partial.write_text("", encoding="utf-8")
        assert recover_orphan(partial) == 0 and len(load_runs(sidecar)) == 2, "emptied orphan must not re-fold"
        assert not partial.exists(), "an empty orphan is still dropped"
    # fail-open: an unwritable target never raises into the gate
    bad = PartialRows(Path(__file__))  # a FILE as root -> mkdir/open both fail
    bad.append({"scenario": "x"})
    assert list(bad) == [{"scenario": "x"}], "the in-memory ledger survives a dead disk"

    print("gate_perf self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
