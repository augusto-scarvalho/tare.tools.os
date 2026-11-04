"""SPEC-109 SE: the harness analyzes and criticizes itself.

Collects metrics the repo already produces (events, reduce results, guardrail
actuals, discovery cache, friction observations), evaluates them against the
declarative thresholds in `.harness/self-review.json`, and returns findings.
Action findings enter the existing supervision funnel (escalations + M4 page)
via `self_review_finding` events emitted by the CLI wrapper; the closed set of
safe actions (tighten ratchets, maintain the backlog inbox) is deterministic,
reversible, and event-logged.

Anti-Hive invariant: this module never edits code, policy, prompts, or model
chains — it diagnoses and proposes; execution stays human-gated.
Local and offline by design: gates call it, gates never touch the network.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from harness_lib import agent_parity, cost_metrics, packet_economy, token_calibration
from harness_lib.common import read_json, write_json, write_text
# MF split: the finding RULES live in self_review_rules; re-export the names
# callers and the se_self_review scenario reach through `self_review.`.
from harness_lib.self_review_rules import (  # noqa: F401
    CRITICALITY_ORDER, _now, _parse_ts, annotate_criticality, evaluate)

CONFIG_REL = ".harness/self-review.json"
STATE_REL = ".harness/state/self-review.json"
# M1: a worker stdout above this size is "large" for the measurement rung (~50k tokens
# at chars/4). The observation floor is a constant; whether a recurring pattern of these
# warrants action is the policy knob (stdoutOversizeMinCount) in self_review_rules.
STDOUT_OVERSIZE_BYTES = 200_000
REPORT_REL = ".harness/state/self-review.md"
INBOX_START = "<!-- self-review-inbox:start -->"
INBOX_END = "<!-- self-review-inbox:end -->"

DEFAULT_CONFIG: dict[str, Any] = {
    "schemaVersion": "1.0",
    "autoActions": True,
    "staleAfterDays": 7,
    "staleAfterCommits": 25,
    "thresholds": {
        "eventsLogMaxBytes": 1_000_000,
        "providerSweepMaxAgeDays": 14,
        "escalationMaxPendingHours": 48,
        "attemptSelectionCurrentRateMax": 0.95,
        "attemptSelectionMinSamples": 20,
        "frictionRecurrenceMin": 2,
        "budgetOverridesMax": 3,
        "ratchetSlackFraction": 0.15,
        "workflowCostOutlierFactor": 3.0,
        "workflowCostOutlierMinSamples": 5,
        "delegationCostOutlierFactor": 1.5,
        "delegationCostOutlierMinSamples": 3,
        "delegationCostOutlierAbs": 250_000,
        "delegationCostTrendFactor": 1.3,
        "delegationCostTrendMinSamples": 10,
        "estimatorDriftRatio": 0.25,
        "estimatorMinSamples": 20,
        "charsPerTokenStep": 0.5,
        "charsPerTokenMin": 2.0,
        "charsPerTokenMax": 8.0,
        "usageBoostShare": 0.30,
        "selfReviewMaxDurationS": 30,
        "parityGapsMax": 0,
        "safeActionWindowHours": 24,
        "safeActionMaxPerWindow": 5,
        'perfHotspotMinCount': 5,
        'perfHotspotMinS': 5.0,
        'perfHotspotTrendFactor': 1.3,
        # I9-adj evolving-store audit. Rate: measured worklog baseline peak ~24
        # entries/24h -> default 72 (~3x). Orphan: measured 4 legacy orphans -> 20.
        "storeRateWindowHours": 24,
        "storeEntryRateMax": 72,
        "orphanResolvedMax": 20,
    },
    # I11 zombie-scan (arXiv:2602.15654, MemAudit 2605.23723): case-insensitive
    # regexes flagging instruction-like content persisted in evolving stores.
    # Config so tuning is a config change, not code. Narrowed vs the raw list so
    # legit worklog entries documenting `--approval-token`/`--send` don't fire:
    # the flag pattern requires an attached value (a smuggled token), and fenced
    # shell targets code fences, not prose mentions.
    "zombieScanPatterns": [
        r"ignore previous", r"ignore all prior", r"disregard the above",
        r"disregard (all|the) (previous|prior|above)",
        r"always run ", r"you must run ", r"execute the following",
        r"--approval-token[=\s]+\S", r"```\s*(sh|bash|shell|zsh)\b",
    ],
}

def load_config(root: Path) -> dict[str, Any]:
    cfg = read_json(root / CONFIG_REL, {}) or {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _hash_config(config: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _events(root: Path) -> list[dict[str, Any]]:
    path = root / ".harness" / "runs" / "events.jsonl"
    out: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _normalize_friction(text: str) -> str:
    return re.sub(r"[^a-z ]+", "", text.lower()).strip()[:80]


def collect_metrics(root: Path, *, pending_escalations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read every self-review signal from existing repo state. Offline, read-only."""
    events = _events(root)
    events_path = root / ".harness" / "runs" / "events.jsonl"

    # provider sweep age: newest extraction in the discover cache
    cache = read_json(root / "graphify-out" / "discover-cache.json", {}) or {}
    newest_sweep = None
    for entry in cache.values():
        ts = _parse_ts(entry.get("extractedAt")) if isinstance(entry, dict) else None
        if ts and (newest_sweep is None or ts > newest_sweep):
            newest_sweep = ts

    # attemptSelection (M3.3 / M3.4 signal) over active reduce results
    selections_total = 0
    selections_current = 0
    for path in (root / ".harness" / "workflows" / "active").glob("WF-*/reduce/reducer.result.json"):
        for note in (read_json(path, {}) or {}).get("attemptSelection", []) or []:
            selections_total += 1
            if str(note.get("selected")) == "current":
                selections_current += 1

    # friction observations (SE.4): live events + the durable supervision ledger
    # (gate cleanups compact them there — see escalations_lib)
    friction: dict[str, list[str]] = {}
    for evt in events:
        for obs in evt.get("frictionObservations", []) or []:
            friction.setdefault(_normalize_friction(str(obs)), []).append(str(obs))
    ledger = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
    for entry in ledger.get("frictionLog", []) or []:
        obs = str(entry.get("observation", ""))
        if obs:
            friction.setdefault(_normalize_friction(obs), []).append(obs)

    # I11 zombie-scan reads `text`; I9-adj store-anomaly audit reads `at`
    # (entry-rate window) and worklog `keys` (single-writer shape). Content only —
    # both are report-only and never mutate the stores.
    stores: list[dict[str, Any]] = []
    for idx, entry in enumerate(ledger.get("frictionLog", []) or []):
        stores.append({"store": "frictionLog", "ref": str(entry.get("id") or idx),
                       "at": entry.get("at"),
                       "text": f"{entry.get('observation', '')}\n{entry.get('note', '')}"})
    worklog = read_json(root / ".harness" / "state" / "worklog.json", {}) or {}
    for idx, entry in enumerate(worklog.get("entries", []) or []):
        if isinstance(entry, dict):
            stores.append({"store": "worklog", "ref": str(entry.get("title") or idx)[:60],
                           "at": entry.get("at"), "keys": sorted(entry.keys()),
                           "text": f"{entry.get('title', '')}\n{entry.get('body', '')}"})

    guardrails = read_json(root / "testing" / "engineering-guardrails.json", {}) or {}

    def line_count(rel: str) -> int:
        p = root / rel
        return len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) if p.exists() else 0

    try:
        import jsonschema  # noqa: F401
        schema_validation = "active"
    except ImportError:
        schema_validation = "inactive"

    # SPEC-110: subjects beyond self — every registered target repository gets
    # the same observation the harness applies to its own codebase.
    subjects: dict[str, Any] = {}
    try:
        from collections import Counter

        from harness_lib import targets as targets_lib
        for name, entry in targets_lib.load_targets().items():
            if not entry.get("enabled", True):
                continue
            if not targets_lib.target_root(entry).exists():
                subjects[name] = {"exists": False, "profilePath": entry.get("profilePath")}
                continue
            info = targets_lib.inspect(entry)
            quality = read_json(targets_lib.state_dir(name) / "quality-state.json", {}) or {}
            fail_counts = Counter(
                check for run in (quality.get("recentFailures") or [])[-10:]
                for check in (run.get("failureNames") or []))
            subjects[name] = {
                "exists": True,
                "sourceFiles": info["sourceFiles"],
                "biggestFile": info["biggestFile"],
                "overBudget": info["overBudget"],
                "maxFileLines": info["maxFileLines"],
                "graphStale": info["graphStale"],
                "missingEnv": info["missingEnv"],
                "gitDirty": (info.get("git") or {}).get("dirtyFiles"),
                "lastGateStatus": info.get("lastGateStatus"),
                "recurringRedChecks": sorted(n for n, c in fail_counts.items() if c >= 2),
                "selfReview": entry.get("selfReview", {}) or {},
            }
    except Exception as exc:  # noqa: BLE001 - target trouble must not sink the loop
        subjects = {"_error": {"unavailable": f"{type(exc).__name__}: {exc}"}}

    # I6 loop KPI: triage latency over resolved records (raise -> resolve)
    latencies_h: list[float] = []
    for record in (ledger.get("resolvedRecords", {}) or {}).values():
        raised_at, resolved_at = _parse_ts(record.get("raisedAt")), _parse_ts(record.get("resolvedAt"))
        if raised_at and resolved_at and resolved_at >= raised_at:
            latencies_h.append((resolved_at - raised_at).total_seconds() / 3600)

    try:  # SPEC-111 R26: cost/token ledger feeds the loop; absence is fine
        cost = cost_metrics.summarize(root)
    except Exception:
        cost = {}

    # M1 measurement rung (round: memory+context): how often recent workflow runs
    # produced oversized worker stdout. Signal ONLY — masking stays PARKED; the rule in
    # self_review_rules just surfaces a recurring pattern so a future decision has data.
    try:
        _all_cost_records = cost_metrics._load(root)
    except Exception:
        _all_cost_records = []
    _cost_records = _all_cost_records[-100:]
    _wf_stdout = [int(r.get("maxStdoutBytes") or 0) for r in _cost_records if r.get("kind") == "workflow"]
    stdout_oversize_count = sum(1 for b in _wf_stdout if b > STDOUT_OVERSIZE_BYTES)
    stdout_max_bytes_seen = max(_wf_stdout, default=0)

    # OB.2: per-delegation rows (model/agentType/estTokens/durationS) feed the
    # per-group trend rules in self_review_rules — the same ledger records the
    # summary above already saw, no new collection.
    # ponytail: shares the last-100 window above; widen only if a real group
    # starves under the 10-sample floor while older ledger rows exist.
    if isinstance(cost.get("delegations"), dict):
        cost["delegations"]["rows"] = [
            {k: r.get(k) for k in ("model", "agentType", "estTokens", "durationS")}
            for r in _cost_records if r.get("kind") == "delegation"]
        # SPEC-141 tie: the latest delegation's profile budget IS the outlier
        # ceiling so the pre-spawn warn and the post-hoc flag share one number.
        latest = cost["delegations"].get("latest")
        if isinstance(latest, dict):
            latest["budgetTokens"] = packet_economy.budget_for_agent(root, latest.get("agentType"))

    gates = cost.get('gates') if isinstance(cost.get('gates'), dict) else {}
    slowest_series: dict[str, list[float]] = {}
    for row in _all_cost_records:
        if row.get('kind') != 'gate':
            continue
        for entry in row.get('slowest') or []:
            name, duration = entry.get('name'), entry.get('durationS')
            if name and isinstance(duration, (int, float)):
                slowest_series.setdefault(str(name), []).append(float(duration))
    gate_perf = {
        'runs': int(gates.get('runs') or 0),
        'medianS': (gates.get('durationS') or {}).get('median'),
        'slowestRecurring': gates.get('bySlowest') or {},
        'slowestSeries': slowest_series,
    }

    # QA.3 self<->target parity: the same file-size critique the harness applies
    # to targets, applied to its own harness_lib (mirrors targets.inspect()).
    max_lib_lines = int(guardrails.get("maxLibFileLines") or 900)
    biggest_lib: dict[str, Any] = {}
    lib_over_budget: list[dict[str, Any]] = []
    try:
        for lib_path in sorted((root / "scripts" / "harness_lib").glob("*.py")):
            lines = len(lib_path.read_text(encoding="utf-8", errors="ignore").splitlines())
            if lines > int(biggest_lib.get("lines") or 0):
                biggest_lib = {"path": f"scripts/harness_lib/{lib_path.name}", "lines": lines}
            if lines > max_lib_lines:
                lib_over_budget.append({"path": f"scripts/harness_lib/{lib_path.name}", "lines": lines})
        lib_over_budget.sort(key=lambda item: -item["lines"])
    except OSError:
        pass

    try:  # SPEC-113: agent capability parity (skips itself when no manifest)
        capability_parity = agent_parity.parity_summary(root)
    except Exception:  # noqa: BLE001 - parity trouble must not sink the loop
        capability_parity = {}

    return {
        'gatePerf': gate_perf,
        "targets": subjects,
        "cost": cost,
        "capabilityParity": capability_parity,
        "libFiles": {"biggestLibFile": biggest_lib, "overBudget": lib_over_budget,
                     "maxLibFileLines": max_lib_lines},
        "collectedAt": _now().isoformat(timespec="seconds"),
        "triageLatency": {
            "resolvedCount": len(latencies_h),
            "meanHours": round(sum(latencies_h) / len(latencies_h), 2) if latencies_h else None,
            "maxHours": round(max(latencies_h), 2) if latencies_h else None,
        },
        "eventsLogBytes": events_path.stat().st_size if events_path.exists() else 0,
        "stdoutOversizeCount": stdout_oversize_count,
        "stdoutMaxBytesSeen": stdout_max_bytes_seen,
        "budgetOverrides": sum(1 for e in events if e.get("event") == "workflow_budget_overridden"),
        "providerSweepNewest": newest_sweep.isoformat(timespec="seconds") if newest_sweep else None,
        "attemptSelection": {"total": selections_total, "current": selections_current},
        "friction": friction,
        "evolvingStores": stores,
        # I9-adj orphan-resolution audit: resolvedIds with no raised/resolved record.
        "resolvedLedger": {
            "resolvedIds": ledger.get("resolvedIds", []) or [],
            "raisedIds": list((ledger.get("raised") or {}).keys()),
            "resolvedRecordIds": list((ledger.get("resolvedRecords") or {}).keys()),
        },
        "safeActionEvents": [e.get("timestamp") for e in events
                             if e.get("event") == "self_review_safe_action"],
        "harnessLines": line_count("scripts/harness.py"),
        "gateLines": line_count("scripts/spec_test_gate.py"),
        "guardrails": {k: guardrails.get(k) for k in (
            "maxHarnessLines", "targetHarnessLines", "maxSpecTestGateLines", "targetSpecTestGateLines")},
        "schemaValidation": schema_validation,
        "pendingEscalations": pending_escalations or [],
    }


# -- safe actions (closed set; deterministic; reversible; never code/policy) --

def tighten_ratchets(root: Path, metrics: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Ideas §J mechanized: lower line maxima to actual+10% when slack ≥ threshold. Never raises.

    I3: tier-1 blast radius (numeric config values). I7: the action verifies its
    own outcome by replay — re-read the file and confirm every budget still holds;
    any violation restores the original bytes.
    """
    if not config.get("autoActions", True):
        return []
    slack = float(config.get("thresholds", {}).get("ratchetSlackFraction", 0.15))
    path = root / "testing" / "engineering-guardrails.json"
    backup = path.read_bytes() if path.exists() else None
    guardrails = read_json(path, {}) or {}
    actions: list[str] = []
    tightened: list[tuple[str, str]] = []
    for max_key, actual_key in (("maxHarnessLines", "harnessLines"), ("maxSpecTestGateLines", "gateLines")):
        current_max = int(guardrails.get(max_key) or 0)
        actual = int(metrics.get(actual_key) or 0)
        if current_max and actual and actual <= current_max * (1 - slack):
            new_max = int(actual * 1.10)
            if new_max < current_max:
                guardrails[max_key] = new_max
                tightened.append((max_key, actual_key))
                actions.append(f"[tier-1] ratchet: {max_key} {current_max} -> {new_max} (actual {actual})")
    if actions:
        write_json(path, guardrails)
        # I7 replay verification — scoped to the keys THIS run tightened. A
        # pre-existing violation of an untouched budget (e.g. harness.py over
        # its max while the burndown is mid-flight) must not veto a legitimate
        # tighten of another key: that revert loop re-fires every run and spams
        # one safe-action event per run (found 2026-07-13 via the se-scenario
        # inbox-stability check).
        applied = read_json(path, {}) or {}
        for max_key, actual_key in tightened:
            if int(metrics.get(actual_key) or 0) > int(applied.get(max_key) or 10**9):
                if backup is not None:
                    path.write_bytes(backup)
                return [f"[tier-1] ratchet REVERTED: replay verification failed for {max_key}"]
    return actions


def tighten_target_ratchets(root: Path, metrics: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """SPEC-110: per-target maxFileLines ratchet — same §J rule, per subject, replay-verified."""
    if not config.get("autoActions", True):
        return []
    slack = float(config.get("thresholds", {}).get("ratchetSlackFraction", 0.15))
    actions: list[str] = []
    try:
        from harness_lib import targets as targets_lib
        registered = targets_lib.load_targets()
    except Exception:
        return []
    for tname, subject in (metrics.get("targets") or {}).items():
        entry = registered.get(tname)
        if not entry or not isinstance(subject, dict) or not subject.get("exists"):
            continue
        layered = targets_lib.self_review_config(entry, config)
        if not layered.get("autoActions", True):
            continue
        biggest = int((subject.get("biggestFile") or {}).get("lines") or 0)
        current_max = int(subject.get("maxFileLines") or 0)
        if biggest and current_max and biggest <= current_max * (1 - slack):
            new_max = int(biggest * 1.10)
            if new_max < current_max:
                profile_path = root / str(entry["profilePath"])
                backup = profile_path.read_bytes()
                raw = read_json(profile_path, {}) or {}
                raw.setdefault("lineBudgets", {})["maxFileLines"] = new_max
                write_json(profile_path, raw)
                applied = (read_json(profile_path, {}) or {}).get("lineBudgets", {}).get("maxFileLines")
                if applied != new_max or biggest > int(applied or 0):
                    profile_path.write_bytes(backup)
                    actions.append(f"[tier-1] target {tname}: ratchet REVERTED (replay verification failed)")
                else:
                    actions.append(f"[tier-1] target {tname}: maxFileLines {current_max} -> {new_max} (biggest {biggest})")
    return actions


def recalibrate_chars_per_token(root: Path, metrics: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """SPEC-111 R26 safe action: one bounded step of charsPerToken toward the observed
    (engine-reported) ratio. The learned value is written to a LOCAL, gitignored override
    (token_calibration), NOT the committed project.json — the committed value is the
    seed, learning is per-machine derived state, so a gate/test run can never mutate
    tracked config. Replay-verified; the override is reverted on failure."""
    if not config.get("autoActions", True):
        return []
    th = config.get("thresholds", {})
    est = (metrics.get("cost") or {}).get("estimator") or {}
    observed = est.get("observedCharsPerToken")
    if not isinstance(observed, (int, float)):
        return []
    if int(est.get("cptSamples") or 0) < int(th.get("estimatorMinSamples", 20)):
        return []
    project = read_json(root / ".harness" / "project.json", {}) or {}
    budget = ((project.get("workflows") or {}).get("budgets") or {}).get("tokenBudget")
    if not isinstance(budget, dict):
        return []
    # Effective current value: the local override if one has been learned, else the seed.
    override_before = token_calibration.read_override(root)
    old = float(override_before) if override_before is not None else float(budget.get("charsPerToken") or 4)
    if not old or abs(observed - old) / old <= float(th.get("estimatorDriftRatio", 0.25)):
        return []
    step = min(float(th.get("charsPerTokenStep", 0.5)), abs(observed - old))
    new = old + step if observed > old else old - step
    new = round(min(max(new, float(th.get("charsPerTokenMin", 2.0))),
                    float(th.get("charsPerTokenMax", 8.0))), 2)
    if new == old:
        return []
    token_calibration.write_override(root, new, observed=float(observed),
                                     samples=int(est.get("cptSamples") or 0),
                                     at=_now().isoformat(timespec="seconds"))
    # replay verification: the persisted override must be exactly `new` and strictly
    # closer to observed than the value we stepped from; otherwise revert.
    applied_value = token_calibration.read_override(root)
    if applied_value != new or abs(observed - new) >= abs(observed - old):
        if override_before is not None:
            token_calibration.write_override(root, override_before)  # restore prior learned value
        else:
            (root / token_calibration.CALIBRATION_REL).unlink(missing_ok=True)  # back to the seed
        return ["[tier-1] charsPerToken REVERTED: replay verification failed"]
    return [f"[tier-1] estimator: charsPerToken {old} -> {new} (observed {observed}, "
            f"{est.get('cptSamples')} samples; local override, project.json unchanged)"]


def update_inbox(root: Path, findings: list[dict[str, Any]], config: dict[str, Any]) -> bool:
    """Maintain the auto-generated inbox section in the backlog (marker-delimited)."""
    if not config.get("autoActions", True):
        return False
    path = root / "docs" / "IMPLEMENTATION_BACKLOG.md"
    if not path.exists():
        return False
    actionable = [f for f in findings if f.get("severity") == "action"]
    lines = ["", INBOX_START, "## Self-review inbox (auto-generated — triage, then resolve the escalation)", ""]
    if actionable:
        lines.append("| finding | observed | proposed action | ref |")
        lines.append("|---|---|---|---|")
        for f in sorted(actionable, key=lambda x: x["id"]):
            lines.append(f"| `{f['id']}` | {f['observed']} | {f['proposedAction']} | {f.get('backlogRef') or '—'} |")
    else:
        lines.append("Nothing pending — last self-review found no actionable signals.")
    lines += ["", INBOX_END, ""]
    block = "\n".join(lines)
    text = path.read_text(encoding="utf-8")
    if INBOX_START in text and INBOX_END in text:
        head, rest = text.split(INBOX_START, 1)
        _, tail = rest.split(INBOX_END, 1)
        new_text = head.rstrip("\n") + "\n" + block + tail.lstrip("\n")
    else:
        new_text = text.rstrip("\n") + "\n" + block
    if new_text != text:
        write_text(path, new_text)
        return True
    return False


def commit_count(root: Path) -> int | None:
    try:
        proc = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except Exception:
        return None


def staleness(root: Path, config: dict[str, Any]) -> str | None:
    """Why a new self-review is due, or None when fresh (gate-check cadence)."""
    state = read_json(root / STATE_REL, {}) or {}
    ran_at = _parse_ts(state.get("runAt"))
    if ran_at is None:
        return "no previous self-review on record"
    age_days = (_now() - ran_at).days
    if age_days > int(config.get("staleAfterDays", 7)):
        return f"last run {age_days}d ago (> {config.get('staleAfterDays')}d)"
    commits = commit_count(root)
    ran_commits = state.get("commitCount")
    if commits is not None and isinstance(ran_commits, int) and commits - ran_commits > int(config.get("staleAfterCommits", 25)):
        return f"{commits - ran_commits} commits since last run (> {config.get('staleAfterCommits')})"
    return None


def run_self_review(root: Path, *, list_escalations: Callable[[], dict[str, Any]],
                    emit_event: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
    """Collect → evaluate → safe actions → report → funnel new findings as escalations."""
    run_started = time.monotonic()
    config = load_config(root)
    previous_state = read_json(root / STATE_REL, {}) or {}
    escalations = list_escalations()
    metrics = collect_metrics(root, pending_escalations=escalations.get("pending", []))
    findings = evaluate(metrics, config)
    # I9 evolution-process audit: the loop watches itself (arXiv:2606.23075 —
    # verify the evolution process, not just its outputs).
    config_hash = _hash_config(config)
    if previous_state.get("configHash") and previous_state["configHash"] != config_hash:
        findings.append({"id": "self-review-config-changed", "severity": "info",
                         "title": "self-review configuration changed since the last run",
                         "observed": f"autoActions={config.get('autoActions')}, {len(config.get('customRules', []) or [])} custom rule(s)",
                         "threshold": "previous config hash", "backlogRef": None,
                         "proposedAction": "expected if a human edited .harness/self-review.json; investigate if nobody did"})
    prev_resolved = previous_state.get("resolvedCount")
    now_resolved = len(escalations.get("resolvedIds", []))
    if isinstance(prev_resolved, int) and now_resolved < prev_resolved:
        findings.append({"id": "ledger-anomaly", "severity": "action",
                         "title": "supervision ledger lost resolved history",
                         "observed": f"resolvedIds {prev_resolved} -> {now_resolved}",
                         "threshold": "monotonic", "backlogRef": None,
                         "proposedAction": "resolved ids must never disappear; inspect .harness/state/escalations.json history (git) before trusting the loop"})
    annotate_criticality(findings, metrics, config)  # R28: tier + sort, incl. late findings
    pending_ids = {e.get("escalationId") for e in escalations.get("pending", [])}
    resolved_ids = set(escalations.get("resolvedIds", []))
    # I3-adj structural circuit breaker: ONE honest check gates the execution seam.
    # Open (execution halted) while the breaker escalation is pending, or on the
    # run it first fires — but a human resolve re-opens it (resolved wins).
    breaker = "self-review/safe-action-rate-breaker"
    breaker_open = breaker in pending_ids or (
        breaker not in resolved_ids
        and any(f.get("id") == "safe-action-rate-breaker" for f in findings))
    actions: list[str] = []
    if not breaker_open:
        actions = tighten_ratchets(root, metrics, config)
        actions += tighten_target_ratchets(root, metrics, config)
        actions += recalibrate_chars_per_token(root, metrics, config)
        for detail in actions:  # event-log each executed action (breaker evidence)
            emit_event("self_review_safe_action", {"detail": detail})
    if actions:
        # re-read actuals so the report reflects the tightened maxima
        metrics["guardrails"] = {k: (read_json(root / "testing" / "engineering-guardrails.json", {}) or {}).get(k)
                                 for k in metrics.get("guardrails", {})}
    inbox_changed = update_inbox(root, [f for f in findings if f.get("criticality") != "info"], config)

    raised: list[str] = []
    for f in findings:
        # R28 tier routing: info = report only; watch = report + inbox;
        # high/critical = escalation into the supervision funnel
        if f.get("criticality") not in ("high", "critical"):
            continue
        esc_id = f"self-review/{f['id']}"
        if esc_id in pending_ids or esc_id in resolved_ids:
            continue
        # esc-subject-scope D1 (G2): the finding's subject becomes structural,
        # not an id-prefix convention — `target/<name>/...` ids and the tuple
        # subject annotate_criticality derives both resolve to the target name.
        subject = f.get("subject")
        if isinstance(subject, (tuple, list)) and len(subject) == 2:
            subject = subject[1]
        if not subject and str(f.get("id") or "").startswith("target/"):
            subject = str(f["id"]).split("/")[1]
        emit_event("self_review_finding", {
            "escalationId": esc_id, "title": f["title"], "criticality": f.get("criticality"),
            "reason": f"{f['title']}\ncause: observed {f['observed']} vs threshold {f['threshold']}\nfix: {f['proposedAction']}",
            "suggestedProfile": "plan", "backlogRef": f.get("backlogRef"),
            "subject": subject or "self",
        })
        raised.append(esc_id)

    state = {
        "runAt": metrics["collectedAt"],
        "commitCount": commit_count(root),
        "configHash": config_hash,
        "resolvedCount": now_resolved,
        "triageLatency": metrics.get("triageLatency"),
        "findings": findings,
        "safeActions": actions,
        "inboxUpdated": inbox_changed,
        "raisedEscalations": raised,
        "durationS": round(time.monotonic() - run_started, 2),
    }
    write_json(root / STATE_REL, state)
    counts = {tier: sum(1 for f in findings if f.get("criticality") == tier)
              for tier in reversed(CRITICALITY_ORDER)}
    report = ["# Self-review report", "", f"Run: {metrics['collectedAt']}",
              "Findings: " + (", ".join(f"{n} {tier}" for tier, n in counts.items() if n) or "none"),
              f"Safe actions: {'; '.join(actions) or 'none'}", ""]
    for f in findings:  # already sorted most-critical first (R28)
        report += [f"## [{f.get('criticality', f['severity'])}] {f['title']} (`{f['id']}`)",
                   f"- observed: {f['observed']} (threshold {f['threshold']})",
                   f"- proposed: {f['proposedAction']}", ""]
    write_text(root / REPORT_REL, "\n".join(report) + "\n")
    return state
