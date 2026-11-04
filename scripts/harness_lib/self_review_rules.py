"""SPEC-109 SE: the finding RULES for self-review (extracted from self_review.py).

Pure functions over the metrics/state dict `collect_metrics` builds — no file
I/O, no network — so they are trivially testable and re-exported by
`self_review.py` (callers and the se_self_review scenario keep importing them
from there). MF discipline: this split is the chat_engines/async_state precedent
applied to the rule table before it breached the 900-line budget.

Owns: `evaluate` (the rule table), `annotate_criticality` (R28 tiering), the
I2-adj rule-proposal provenance guard, and the I9-adj evolving-store audit.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from typing import Any


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# SPEC-111 R28: deterministic base criticality per rule family. Usage share of
# the finding's subject can raise it one tier (heavily used = review first).
CRITICALITY_ORDER = ("info", "watch", "high", "critical")
_CRITICAL_MARKERS = ("ledger-anomaly", "hitl", "security", "protected", "canonical")
# friction (2x = missing tool) and custom rules (a human wrote the threshold)
# stay high: both are deliberate, actionable signals — not background aging.
_HIGH_MARKERS = ("workflow-cost-outlier", "delegation-cost", "estimator-drift", "lines-burndown",
                 "escalation-stale", "observability-overhead", "budget-override",
                 "/missing", "friction-", "custom-", "file-budget", "capability-parity",
                 "safe-action-rate-breaker", "evolving-store-zombie", "evolving-store-anomaly")


def _usage_share(metrics: dict[str, Any], kind: str, name: str | None) -> float:
    if not name:
        return 0.0
    usage = (metrics.get("cost") or {}).get("usage") or {}
    table = usage.get("workflowTypeShare" if kind == "type" else "targetShare") or {}
    return float(table.get(str(name)) or 0.0)


def annotate_criticality(findings: list[dict[str, Any]], metrics: dict[str, Any],
                         config: dict[str, Any]) -> None:
    """Assign each finding a criticality tier and sort most-critical first."""
    boost_share = float(config.get("thresholds", {}).get("usageBoostShare", 0.30))
    for f in findings:
        fid = str(f.get("id") or "")
        if f.get("severity") == "info":
            tier = "info"
        elif any(m in fid for m in _CRITICAL_MARKERS):
            tier = "critical"
        elif any(m in fid for m in _HIGH_MARKERS):
            tier = "high"
        else:
            tier = "watch"
        subject = f.get("subject")
        if not subject and fid.startswith("target/"):
            subject = ("target", fid.split("/")[1])
        if (subject and tier in ("watch", "high")
                and _usage_share(metrics, *subject) >= boost_share):
            tier = CRITICALITY_ORDER[CRITICALITY_ORDER.index(tier) + 1]
        f["criticality"] = tier
    findings.sort(key=lambda f: -CRITICALITY_ORDER.index(f.get("criticality", "info")))


def evaluate(metrics: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the rule table to collected metrics. Pure — trivially testable."""
    th = config.get("thresholds", {})
    now = _now()
    findings: list[dict[str, Any]] = []

    def finding(fid: str, severity: str, title: str, observed: str, threshold: str,
                proposed: str, backlog_ref: str | None = None) -> None:
        findings.append({"id": fid, "severity": severity, "title": title, "observed": observed,
                         "threshold": threshold, "proposedAction": proposed, "backlogRef": backlog_ref})

    size = int(metrics.get("eventsLogBytes", 0))
    if size > int(th.get("eventsLogMaxBytes", 10**6)):
        finding("events-log-rotation", "action", "events.jsonl exceeds its growth trigger",
                f"{size} bytes", f"{th['eventsLogMaxBytes']} bytes",
                "implement events.jsonl rotation/compaction (Deferred item's trigger has fired)",
                "Deferred: events.jsonl rotation")

    # M1 measurement rung: surface a RECURRING oversized-worker-stdout pattern so the
    # parked stdout-masking loop gets a data-backed trigger. Watch-level (info): this
    # reports a signal, it does not demand masking (masking-before-measurement inverts
    # the cost equation — round M1 cost lens).
    oversize = int(metrics.get("stdoutOversizeCount", 0))
    if oversize >= int(th.get("stdoutOversizeMinCount", 5)):
        finding("stdout-oversize-recurring", "info", "oversized worker stdout is a recurring pattern",
                f"{oversize} recent runs > {int(metrics.get('stdoutMaxBytesSeen', 0))}B peak",
                f"{th.get('stdoutOversizeMinCount', 5)} runs",
                "the parked stdout-masking loop (round M1) now has a signal — weigh masking the repeat-offender command against retry-cost inversion",
                "Parked: M1 stdout masking")

    sweep = _parse_ts(metrics.get("providerSweepNewest"))
    max_age = int(th.get("providerSweepMaxAgeDays", 14))
    gate_perf = metrics.get('gatePerf') or {}
    min_count = int(th.get('perfHotspotMinCount', 5))
    min_seconds = float(th.get('perfHotspotMinS', 5.0))
    trend_factor = float(th.get('perfHotspotTrendFactor', 1.3))
    slowest_series = gate_perf.get('slowestSeries') or {}
    for seam, recurring in (gate_perf.get('slowestRecurring') or {}).items():
        count = int(recurring.get('count') or 0)
        median_s = float(recurring.get('medianS') or 0)
        if count < min_count or median_s < min_seconds:
            continue
        severity = 'info'
        observed = f'{seam}: {count} recurring samples, median {median_s:.3f}s'
        series = [float(value) for value in (slowest_series.get(seam) or [])
                  if isinstance(value, (int, float))]
        if len(series) >= 10:
            previous, recent = statistics.median(series[-10:-5]), statistics.median(series[-5:])
            if previous and recent >= previous * trend_factor:
                severity = 'high'
                observed += f'; recent median {recent:.3f}s vs previous {previous:.3f}s'
        finding('perf-hotspot-recurring', severity, 'a gate seam is recurrently slow',
                observed, f'>= {min_count} samples and >= {min_seconds:.3f}s median',
                f'inspect {seam}; this watch rule reports only and never changes gate behavior')

    if sweep and (now - sweep).days > max_age:
        finding("provider-sweep-age", "action", "provider model chains are aging without re-measurement",
                f"newest extraction {(now - sweep).days}d ago", f"{max_age}d",
                "run a live model sweep / build P1 `providers probe` (chains are perishable, SPEC-107 findings)",
                "P1")

    lines, target = int(metrics.get("harnessLines", 0)), int((metrics.get("guardrails") or {}).get("targetHarnessLines") or 0)
    if target and lines > target:
        finding("harness-lines-burndown", "action", "harness.py is above its burn-down target",
                f"{lines} lines", f"target {target}",
                "run the next extraction round (MF.1 round 2: workflow lifecycle + CLI parser table)",
                "MF.1")
    lines, target = int(metrics.get("gateLines", 0)), int((metrics.get("guardrails") or {}).get("targetSpecTestGateLines") or 0)
    if target and lines > target:
        finding("gate-lines-burndown", "action", "spec_test_gate.py is above its burn-down target",
                f"{lines} lines", f"target {target}",
                "extract another fixture domain behind harness_lib", "MF.2 follow-up")

    max_hours = int(th.get("escalationMaxPendingHours", 48))
    for esc in metrics.get("pendingEscalations", []):
        raised = _parse_ts(esc.get("raisedAt"))
        if raised and (now - raised).total_seconds() > max_hours * 3600:
            finding("escalation-stale", "action", "an escalation has been waiting on a human too long",
                    f"{esc.get('escalationId')} pending {(now - raised).days}d", f"{max_hours}h",
                    f"triage it: python scripts/harness.py escalations --resolve {esc.get('escalationId')} (or act on it)")
            break

    sel = metrics.get("attemptSelection", {})
    total = int(sel.get("total", 0))
    if total >= int(th.get("attemptSelectionMinSamples", 20)):
        rate = int(sel.get("current", 0)) / total
        if rate > float(th.get("attemptSelectionCurrentRateMax", 0.95)):
            finding("attempt-selection-linear", "action",
                    "non-linear reduce almost always picks the latest attempt",
                    f"current picked {rate:.0%} of {total}", f"{th['attemptSelectionCurrentRateMax']:.0%}",
                    "M3.4 assumptions-registry signal fired: re-test whether attempt archiving still pays for itself",
                    "assumptions registry: non-linear reduce")

    min_rec = int(th.get("frictionRecurrenceMin", 2))
    for key, occurrences in (metrics.get("friction") or {}).items():
        if key and len(occurrences) >= min_rec:
            finding(f"friction-{key[:40].replace(' ', '-')}", "action",
                    "recurring friction reported by agents (friction x2 = missing tool)",
                    f"{len(occurrences)}x: {occurrences[0][:120]}", f"{min_rec}x",
                    "wrap this dance in a tool/index/command (ideas doc SJ rule)")

    overrides = int(metrics.get("budgetOverrides", 0))
    if overrides > int(th.get("budgetOverridesMax", 3)):
        finding("budget-override-rate", "action", "token budgets are overridden often enough to look mistuned",
                f"{overrides} overrides on record", f"{th['budgetOverridesMax']}",
                "revisit profile tokenBudget values instead of overriding per run")

    # SPEC-111 R26: cost-ledger rules. Routing/model changes are NEVER automatic —
    # expensive workflows become findings for a human; only the estimator
    # calibration has a safe action (recalibrate_chars_per_token).
    cost = metrics.get("cost") or {}
    wf_stats = ((cost.get("workflows") or {}).get("estTokens") or {})
    wf_count = int(wf_stats.get("count") or 0)
    if wf_count >= int(th.get("workflowCostOutlierMinSamples", 5)):
        factor = float(th.get("workflowCostOutlierFactor", 3.0))
        median = float(wf_stats.get("median") or 0)
        worst = next((r for r in cost.get("topExpensive") or [] if r.get("kind") == "workflow"), None)
        worst_est = float((worst or {}).get("estTokens") or 0)
        if median and worst_est > median * factor:
            finding("workflow-cost-outlier", "action",
                    "a workflow costs far more than its peers",
                    f"{worst.get('workflowId')} ~{int(worst_est)} est tokens vs median {int(median)}",
                    f"{factor}x median over {wf_count} workflows",
                    "inspect that workflow's plan/profile: smaller shards, cheaper profile, or better split "
                    "(routing changes stay human-gated)")
            findings[-1]["subject"] = ("type", (worst or {}).get("type"))  # R28 usage boost

    # TE.2: delegations are real subagent spend (R26) — the one cost stream
    # without a rule. The ledger saw the 309k SPEC-115 round (median ~160k)
    # and nothing flagged it. No subject => stays base tier `high` (no boost).
    deleg = cost.get("delegations") or {}
    series = [float(x) for x in (deleg.get("series") or []) if isinstance(x, (int, float))]
    if series:
        latest = series[-1]
        who = str((deleg.get("latest") or {}).get("title")
                  or (deleg.get("latest") or {}).get("model") or "latest delegation")[:80]
        prev = series[-11:-1]  # up to 10 delegations before the latest
        factor = float(th.get("delegationCostOutlierFactor", 1.5))
        ceiling = int((deleg.get("latest") or {}).get("budgetTokens")  # SPEC-141: profile budget when set
                      or th.get("delegationCostOutlierAbs", 250_000))   # median/absolute fallback otherwise
        reason = None
        if len(prev) >= int(th.get("delegationCostOutlierMinSamples", 3)):
            med = statistics.median(prev)
            if med and latest > med * factor:
                reason = f"{who}: ~{int(latest)} est tokens vs median {int(med)} of previous {len(prev)}"
        if reason is None and latest > ceiling:
            reason = f"{who}: ~{int(latest)} est tokens over the {ceiling} absolute ceiling"
        if reason:
            finding("delegation-cost-outlier", "action",
                    "the latest subagent delegation cost far more than its peers",
                    reason, f"{factor}x median (min 3) or {ceiling} absolute",
                    "inspect that delegation: over-broad brief, missing context reuse, or a task that "
                    "should have been split — delegations are the one spend stream self-review now watches (TE.2)")
        if len(series) >= int(th.get("delegationCostTrendMinSamples", 10)):
            last5, prev5 = series[-5:], series[-10:-5]
            tf = float(th.get("delegationCostTrendFactor", 1.3))
            m_last, m_prev = statistics.median(last5), statistics.median(prev5)
            if m_prev and m_last > m_prev * tf:
                finding("delegation-cost-trend", "action",
                        "subagent delegation cost is trending up across recent runs",
                        f"median of last 5 {int(m_last)} vs previous 5 {int(m_prev)}",
                        f"{tf}x rising median over {len(series)} delegations",
                        "the whole delegation stream is getting more expensive, not one outlier — review "
                        "round scoping / context packaging before the trend compounds (TE.2)")

    # OB.2: per-group trend — one model or agent type can drift while the mix
    # keeps the aggregate series flat. Same last5/prev5 rising-median pattern
    # as the aggregate rule above, over the per-delegation rows collect_metrics
    # attaches; groups under the sample floor stay silent.
    # ponytail: knobs reuse the aggregate trend thresholds (factor 1.3, min 10
    # samples per group); split into per-group keys only if the two ever need
    # separate tuning.
    rows = [r for r in (deleg.get("rows") or []) if isinstance(r, dict)]
    if rows:
        g_tf = float(th.get("delegationCostTrendFactor", 1.3))
        g_min = int(th.get("delegationCostTrendMinSamples", 10))
        for key, tag in (("model", "model"), ("agentType", "agent")):
            groups: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                if r.get(key):
                    groups.setdefault(str(r[key]), []).append(r)
            for name, grp in sorted(groups.items()):
                for field, family, noun in (("estTokens", "delegation-cost-trend", "cost"),
                                            ("durationS", "delegation-latency-trend", "duration")):
                    vals = [float(g[field]) for g in grp if isinstance(g.get(field), (int, float))]
                    if len(vals) < g_min:
                        continue  # sparse group: silent (same floor as the aggregate)
                    m_last, m_prev = statistics.median(vals[-5:]), statistics.median(vals[-10:-5])
                    if m_prev and m_last > m_prev * g_tf:
                        finding(f"{family}:{tag}:{name}", "action",
                                f"subagent delegation {noun} is trending up for {tag} {name}",
                                f"median of last 5 {int(m_last)} vs previous 5 {int(m_prev)}",
                                f"{g_tf}x rising median over {len(vals)} delegations for {tag} {name}",
                                f"this {tag}'s delegation stream is drifting while the aggregate can look "
                                "flat — review its briefs / context packaging before the trend compounds (OB.2)")

    # I3-adj: safe-action rate circuit breaker (2026 auto-remediation guides —
    # aurorasre.ai, safeguard.sh; in-repo precedent: workflow_executor_circuit_*).
    # Too many EXECUTED safe actions in the window = automation running hot. The
    # finding raises the escalation; run_self_review latches EXECUTION off it.
    window_h = float(th.get("safeActionWindowHours", 24))
    cutoff = now - dt.timedelta(hours=window_h)
    recent_actions = sum(1 for t in metrics.get("safeActionEvents", []) or []
                         if (ts := _parse_ts(t)) and ts >= cutoff)
    max_actions = int(th.get("safeActionMaxPerWindow", 5))
    if recent_actions > max_actions:
        finding("safe-action-rate-breaker", "action",
                "too many safe actions executed in the window — automation is running hot",
                f"{recent_actions} executed in {int(window_h)}h", f"{max_actions} per window",
                "circuit breaker tripped: auto-action EXECUTION halts (proposals still flow) until you "
                "resolve this escalation — find why the loop fires so often before re-opening it")

    # I11: deterministic zombie-scan of evolving stores (arXiv:2602.15654,
    # MemAudit 2605.23723). Report-only — flags instruction-like persisted
    # content for a human; NEVER modifies the stores (the scan is not an action).
    compiled: list[re.Pattern[str]] = []
    for pat in config.get("zombieScanPatterns") or []:
        try:
            compiled.append(re.compile(str(pat), re.IGNORECASE))
        except re.error:
            continue
    for entry in metrics.get("evolvingStores", []) or []:
        text = str(entry.get("text", ""))
        hit = next((rx.pattern for rx in compiled if rx.search(text)), None)
        if hit:
            store, ref = entry.get("store", "?"), entry.get("ref", "?")
            finding(f"evolving-store-zombie-scan/{store}/{ref}", "action",
                    "instruction-like content persisted in an evolving memory store",
                    f"[{store}:{ref}] matched /{hit}/", "no imperative/injection patterns",
                    "INSPECT, do not auto-delete: a persisted note/record carries instruction-like text that "
                    "could steer a future evolution cycle (Zombie Agents) — confirm provenance or remove it by hand")

    # QA.3: harness_lib file budget — the critique we apply to targets, on ourselves
    lib = metrics.get("libFiles") or {}
    lib_over = lib.get("overBudget") or []
    if lib_over:
        worst_lib = lib_over[0]
        finding("self-lib-file-budget", "action",
                "a harness_lib module outgrew the file budget the harness enforces on targets",
                f"{worst_lib['path']} {worst_lib['lines']} lines"
                + (f" (+{len(lib_over) - 1} more over budget)" if len(lib_over) > 1 else ""),
                f"{lib.get('maxLibFileLines')} lines",
                "fragment the module (MF discipline: extract a cohesive domain, bind() or pure module)")

    # R27: the observation loop denounces itself when it gets expensive
    obs = cost.get("observability") or {}
    last_duration = obs.get("selfReviewLastDurationS")
    max_duration = float(th.get("selfReviewMaxDurationS", 30))
    if isinstance(last_duration, (int, float)) and last_duration > max_duration:
        finding("observability-overhead", "action",
                "the self-review observation loop itself is getting expensive",
                f"last run took {last_duration}s", f"{max_duration}s",
                "trim collect_metrics inputs or raise thresholds — observation must pay for itself (R27)")

    est = cost.get("estimator") or {}
    observed_cpt = est.get("observedCharsPerToken")
    configured_cpt = float(est.get("configuredCharsPerToken") or 0)
    if (isinstance(observed_cpt, (int, float)) and configured_cpt
            and int(est.get("cptSamples") or 0) >= int(th.get("estimatorMinSamples", 20))):
        drift = abs(observed_cpt - configured_cpt) / configured_cpt
        if drift > float(th.get("estimatorDriftRatio", 0.25)):
            finding("estimator-drift", "action",
                    "the chars/token estimator disagrees with real engine-reported tokens",
                    f"observed {observed_cpt} vs configured {configured_cpt} "
                    f"({drift:.0%} drift over {est.get('cptSamples')} samples)",
                    f"{float(th.get('estimatorDriftRatio', 0.25)):.0%}",
                    "safe action recalibrate_chars_per_token steps tokenBudget.charsPerToken toward the observed value")

    # SPEC-113: agent CLIs must stay at capability parity; drift is a human-gated
    # finding routed through the same funnel (pair renders adapters, never auto-applied).
    parity = metrics.get("capabilityParity") or {}
    high_gaps = int(parity.get("highSeverityGaps") or 0)
    if high_gaps > int(th.get("parityGapsMax", 0)):
        finding("capability-parity-gap", "action",
                "agent CLIs have drifted out of capability parity",
                f"{high_gaps} high-severity gap(s); worst: {parity.get('worstGap')}",
                f"<= {th.get('parityGapsMax', 0)} high-severity gaps",
                "run python scripts/harness.py agents pair (review the dry-run plan, then --apply)")

    if metrics.get("schemaValidation") == "inactive":
        finding("schema-validation-inactive", "info", "runtime JSON-schema validation is not running",
                "jsonschema not installed", "active",
                "conscious stdlib-only choice (SPEC-108 H1); install jsonschema if contract regressions appear")

    # I6 loop KPI: is the human side of the loop healthy? (AIOps MTTR analog)
    latency = metrics.get("triageLatency", {}) or {}
    if int(latency.get("resolvedCount") or 0) >= 5:
        mean_h = float(latency.get("meanHours") or 0)
        max_ok = float(th.get("triageLatencyMeanMaxHours", 72))
        finding("triage-latency", "action" if mean_h > max_ok else "info",
                "triage latency of resolved findings",
                f"mean {mean_h}h over {latency['resolvedCount']} resolved (max {latency.get('maxHours')}h)",
                f"mean ≤ {max_ok}h",
                "the human side of the loop is the bottleneck — schedule a triage pass or tune noisy rules"
                if mean_h > max_ok else "healthy; informational KPI")

    # SPEC-110: the same critique the harness applies to itself, per target subject.
    for tname, subject in (metrics.get("targets") or {}).items():
        if not isinstance(subject, dict) or tname == "_error":
            continue
        if subject.get("exists") is False:
            finding(f"target/{tname}/missing", "action", f"target repository {tname} is gone",
                    "path does not exist", "registered path present",
                    f"restore the repository or update \"path\" in {subject.get('profilePath')}")
            continue
        for offender in (subject.get("overBudget") or [])[:3]:
            finding(f"target/{tname}/file-budget", "action",
                    f"[{tname}] source file exceeds its line budget",
                    f"{offender['path']}={offender['lines']} lines", f"maxFileLines {subject.get('maxFileLines')}",
                    f"extract cohesive modules from {offender['path']} — the same monolith critique the harness applies to its own code")
            break  # one finding per target; offenders are listed in `targets`/gate output
        for check in (subject.get("recurringRedChecks") or [])[:1]:
            finding(f"target/{tname}/gate-red-recurring", "action",
                    f"[{tname}] the same gate check keeps failing",
                    f"{check} failed >=2 of the last 10 target-gate runs", "no recurring reds",
                    f"fix the root cause in the target (see .harness/state/targets/{tname}/quality-state.json) instead of re-running")
        if subject.get("missingEnv"):
            finding(f"target/{tname}/env-missing", "action",
                    f"[{tname}] declared environment keys are absent",
                    ", ".join(subject["missingEnv"][:5]), "all requiresEnv present",
                    "add the values to the harness root .env (agents for this target get them deny-by-default)")
        if subject.get("graphStale"):
            finding(f"target/{tname}/graph-stale", "info",
                    f"[{tname}] knowledge graph is stale or missing",
                    "graph older than newest source", "fresh",
                    f"python scripts/harness.py graph-build-code-ast --target {tname}")

    # I2 (adaptive-automata move, restricted): human-approved custom rules from
    # config extend the table in a closed language — dotted metric path, numeric
    # comparison — without touching code. Collections compare by length.
    # SPEC-110: rules may scope to a subject via "target": "<name>"|"self"|"*".
    ops = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b,
           "lte": lambda a, b: a <= b, "eq": lambda a, b: a == b, "ne": lambda a, b: a != b}
    for rule in config.get("customRules", []) or []:
        # I2-adj: an auto-proposed rule (proposedBy=self-review) MUST carry
        # provenance or the acceptance path REFUSES it — the human is not
        # approving blind against OEP poisoning (arXiv:2605.18930).
        if str(rule.get("proposedBy") or "") == PROPOSAL_ORIGIN and not _valid_provenance(rule.get("provenance")):
            finding(f"custom-{rule.get('id', 'rule')}-unprovenanced", "info",
                    "auto-proposed rule refused at apply",
                    json.dumps(rule, ensure_ascii=False)[:120], "provenance {sourceFinding, evidence}",
                    "[no provenance — do not approve] this rule was auto-proposed but carries no provenance "
                    "chain; do not approve — re-derive it from a real finding with its evidence")
            continue
        try:
            scope = str(rule.get("target", "*"))
            if scope in ("*", "self"):
                value: Any = metrics
            else:
                value = (metrics.get("targets") or {}).get(scope)
                if value is None:
                    continue  # scoped to a target that is not registered here
            for part in str(rule.get("metric", "")).split("."):
                value = value[part]
            if isinstance(value, (list, dict, str)):
                value = len(value)
            op = ops[str(rule.get("op", "gt"))]
            if value is not None and op(float(value), float(rule["threshold"])):
                finding(f"custom-{rule.get('id', 'rule')}", str(rule.get("severity", "action")),
                        str(rule.get("title", f"custom rule {rule.get('id')}")),
                        f"{rule.get('metric')}={value}", f"{rule.get('op', 'gt')} {rule['threshold']}",
                        str(rule.get("proposedAction", "triage this custom signal")),
                        rule.get("backlogRef"))
        except (KeyError, TypeError, ValueError, IndexError):
            finding(f"custom-{rule.get('id', 'rule')}-invalid", "info",
                    "custom rule could not be evaluated",
                    json.dumps(rule)[:120], "valid {id, metric, op, threshold}",
                    "fix or remove this entry in .harness/self-review.json customRules")

    findings.extend(rule_proposals(metrics, config))
    findings.extend(evolving_store_anomalies(metrics, config))
    return findings


# -- I2-adj: rule-proposal provenance ----------------------------------------

PROPOSAL_ORIGIN = "self-review"  # stamped on rules the loop auto-proposes


def _valid_provenance(prov: Any) -> bool:
    """Trustworthy only with a source finding + at least one concrete evidence ref."""
    return (isinstance(prov, dict) and bool(prov.get("sourceFinding"))
            and isinstance(prov.get("evidence"), (list, tuple)) and len(prov["evidence"]) > 0)


def rule_proposals(metrics: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """I2-adj (adaptive-automata 'rules-that-propose-rules', restricted): propose a
    standing customRule for a signal that keeps recurring, each proposal carrying
    provenance (the source finding + the concrete data that motivated it) so the
    human reviewer is not approving blind against non-transferable/poisoned
    experiences (OEP, arXiv:2605.18930). Real path = friction recurrence, the
    canonical "this should be a rule" signal. Info-only + human-gated: a proposal
    only APPLIES once a human copies its proposedRule into customRules.
    """
    th = config.get("thresholds", {})
    min_rec = int(th.get("frictionRecurrenceMin", 2))
    watched = {str(r.get("metric") or "") for r in (config.get("customRules") or [])}
    out: list[dict[str, Any]] = []
    for key, occurrences in (metrics.get("friction") or {}).items():
        metric = f"friction.{key}"
        if not key or len(occurrences) < min_rec or metric in watched:
            continue  # already a standing rule -> nothing left to propose
        slug = key[:40].replace(" ", "-")
        prov = {"sourceFinding": f"friction-{slug}",
                "evidence": [str(o)[:120] for o in occurrences[:5]],
                "generatedAt": _now().isoformat(timespec="seconds")}
        proposed = {"id": f"friction-watch-{slug[:30]}", "metric": metric, "op": "gte",
                    "threshold": len(occurrences), "severity": "action",
                    "title": f"standing watch: recurring friction {key[:50]}",
                    "proposedAction": "wrap this dance in a tool; this rule keeps its recurrence visible",
                    "proposedBy": PROPOSAL_ORIGIN, "provenance": prov}
        out.append({"id": f"rule-proposal/friction-{slug}", "severity": "info",
                    "title": "self-review proposes a standing rule for a recurring signal",
                    "observed": f"{len(occurrences)}x {key[:60]}", "threshold": f">= {min_rec}x recurrence",
                    "proposedAction": "review the proposedRule (provenance attached) and add it to "
                                      ".harness/self-review.json customRules to accept — human-gated",
                    "proposedRule": proposed, "provenance": prov, "backlogRef": None})
    return out


# -- I9-adj: evolving-store anomaly audit ------------------------------------

# Canonical worklog writer contract (records.add_entry): a worklog entry whose
# top-level keys stray outside this set was written by something other than the
# single sanctioned writer — a single-writer violation (MemAudit 2605.23723).
WORKLOG_WRITER_KEYS = ("at", "kind", "title", "body", "refs", "tags")


def evolving_store_anomalies(metrics: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """I9-adj: deterministic anomaly audit over the EVOLVING memory stores
    (worklog, frictionLog, resolvedIds) — the target the 2026 memory-governance
    literature names (MemAudit 2605.23723, SSGM 2603.11768), beyond I11's
    zombie-scan. Report-only: never mutates a store; at most one finding per
    anomaly class per store.
    """
    th = config.get("thresholds", {})
    now = _now()
    out: list[dict[str, Any]] = []

    # (a) entry-rate spike + (b) single-writer violation, over the store entries
    window_h = float(th.get("storeRateWindowHours", 24))
    rate_max = int(th.get("storeEntryRateMax", 72))
    cutoff = now - dt.timedelta(hours=window_h)
    canon = set(WORKLOG_WRITER_KEYS)
    recent: dict[str, int] = {}
    foreign: dict[str, set[str]] = {}
    for e in metrics.get("evolvingStores") or []:
        store = str(e.get("store") or "?")
        ts = _parse_ts(e.get("at"))
        if ts and ts >= cutoff:
            recent[store] = recent.get(store, 0) + 1
        if store == "worklog" and e.get("keys") is not None:  # only worklog has the records.py writer
            foreign.setdefault(store, set()).update(k for k in (e.get("keys") or []) if k not in canon)
    for store, n in sorted(recent.items()):
        if n > rate_max:
            out.append({"id": f"evolving-store-anomaly/rate/{store}", "severity": "action",
                        "title": "an evolving memory store is growing anomalously fast",
                        "observed": f"[{store}] {n} entries in {int(window_h)}h",
                        "threshold": f"{rate_max} per {int(window_h)}h",
                        "proposedAction": "INSPECT, do not auto-trim: a burst of writes to an evolving store "
                                          "can be poisoning in progress — confirm each new entry's provenance",
                        "backlogRef": None})
    for store, keys in sorted(foreign.items()):
        if keys:
            out.append({"id": f"evolving-store-anomaly/writer/{store}", "severity": "action",
                        "title": "an evolving store carries entries the single writer never produces",
                        "observed": f"[{store}] foreign top-level keys: {', '.join(sorted(keys))}",
                        "threshold": f"only {', '.join(WORKLOG_WRITER_KEYS)}",
                        "proposedAction": "INSPECT: worklog entries must come from records.add_entry; unknown "
                                          "top-level keys mean another writer touched the store",
                        "backlogRef": None})

    # (c) orphan resolvedIds: resolutions with no raised/resolved record behind them
    led = metrics.get("resolvedLedger") or {}
    known = ({str(i) for i in (led.get("raisedIds") or [])}
             | {str(i) for i in (led.get("resolvedRecordIds") or [])})
    orphans = [str(i) for i in (led.get("resolvedIds") or []) if str(i) not in known]
    orphan_max = int(th.get("orphanResolvedMax", 20))
    if len(orphans) > orphan_max:
        out.append({"id": "evolving-store-anomaly/orphan-resolved/escalations", "severity": "action",
                    "title": "resolvedIds is growing without matching raised/resolved records",
                    "observed": f"{len(orphans)} orphan resolutions (e.g. {orphans[0]})",
                    "threshold": f"<= {orphan_max} orphan resolvedIds",
                    "proposedAction": "INSPECT: resolved ids with no record behind them can silence escalations "
                                      "(resolved-spam) — reconcile resolvedIds against git history",
                    "backlogRef": None})
    return out
