"""Durable cost/token/time ledger feeding the self-evolution loop (SPEC-111 R26).

`.harness/state/cost-metrics.json` (gitignored, survives gate wipes of
.harness/runs/) holds the last 500 records:
- chat turns carry REAL engine-reported numbers (tokens, per-turn cost delta,
  duration) — the only genuinely observed data in the system;
- workflow records carry chars/4 ESTIMATES (`estTokens`/`observedTokens` are
  estimator output, not engine-reported) plus real wall-clock durations from
  roundHistory; `costUsd` is null — the documented seat where real per-worker
  cost attaches once executors surface it.

Writers are never-crash: losing a telemetry record is acceptable, breaking a
chat turn or a workflow finalize is not.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .common import read_json, write_json, gate_holding
    from . import token_calibration, decision_inbox, intake_queue
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import read_json, write_json, gate_holding
    from harness_lib import token_calibration, decision_inbox, intake_queue

LEDGER_REL = ".harness/state/cost-metrics.json"
MAX_RECORDS = 500
# perf-metrics rung: gate records land on every gate run (several/day), so an
# unbounded share would evict chat/workflow history from the shared cap. The
# Phase-1 spec decision is a per-kind trim, not a cap raise.
GATE_RECORDS_MAX = 150
# charsPerToken sanity band for observed samples: below/above this is physically
# impossible for real text (a token averages ~2-4 chars) and is discarded (#2).
CPT_SANE_MIN = 1.5
CPT_SANE_MAX = 12.0


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _load(root: Path) -> list[dict[str, Any]]:
    try:
        data = read_json(root / LEDGER_REL, {}) or {}
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        return []
    return [r for r in data.get("records") or [] if isinstance(r, dict)]


def _append(root: Path, record: dict[str, Any]) -> bool:
    """Append + trim; one retry for OneDrive-style transient locks; never raises.

    Returns whether the row LANDED (audit, kimi 2026-07-31): it used to return
    None and swallow every failure, so `record_delegation` printed "recorded" over
    a silent drop. Automatic writers (turn/gate/workflow) still ignore the boolean.
    Hold-AGNOSTIC on purpose: `record_gate` calls this from INSIDE the staged gate,
    where `.harness/runs/gate-hold/` is non-empty by construction — refusing here
    would drop legitimate in-gate perf rows. The operator entry (record_delegation)
    owns the gate-hold refusal instead."""
    for attempt in (0, 1):
        try:
            records = _load(root)
            records.append(record)
            write_json(root / LEDGER_REL,
                       {"schemaVersion": 1, "records": records[-MAX_RECORDS:]})
            return True
        except OSError:
            if attempt:
                return False
            time.sleep(0.2)
        except Exception:
            return False
    return False


def record_turn(root: Path, *, engine: str, model: str | None, effort: str | None,
                chat_mode: str, target: str | None,
                usage: dict[str, Any] | None, reply_chars: int) -> None:
    # `chat` is the interactive entry (CLI `chat` verb OR the GUI ChatBridge), so like
    # record_delegation it owns a gate-hold refusal: cost-metrics.json is held state, and
    # unlike record_gate this NEVER runs inside the staged gate, so a hold check here is
    # safe (audit, sonnet 2026-07-31 — chat was the cost-metrics writer with no backstop).
    if gate_holding(root):
        print("gate-hold: chat-turn cost NOT recorded — a gate holds .harness (the row "
              "would land on materialized HEAD and be discarded on release)", file=sys.stderr)
        return
    try:
        u = usage or {}
        _append(root, {"at": _now(), "kind": "chat-turn", "engine": engine,
                       "model": model, "effort": effort, "chatMode": chat_mode,
                       "target": target,
                       # additive (panel-chat QoL P2): the GUI sets HARNESS_CHAT_SESSION
                       # in ChatBridge.start so per-session cost can be filtered later.
                       "session": os.environ.get("HARNESS_CHAT_SESSION"),
                       "inTokens": u.get("in_tokens"), "outTokens": u.get("out_tokens"),
                       # TE.6: cached reads bill ~10% of input price — the share
                       # tells whether the stable-prefix discipline actually pays.
                       "cacheReadTokens": u.get("cache_read_tokens"),
                       "costUsd": u.get("cost_usd"), "durationS": u.get("duration_s"),
                       "replyChars": reply_chars})
    except Exception:
        pass


def record_gate(root: Path, *, gate: str, status: str, total_s: float,
                checks: int, slowest: list[dict[str, Any]] | None = None) -> None:
    """Perf-metrics rung Phase 1: one small record per gate run — wall time,
    check count, and the top-5 slowest coarse blocks. Never raises; per-kind
    trim keeps gate records from evicting chat/workflow history."""
    try:
        top = sorted((slowest or []), key=lambda s: -float(s.get("durationS") or 0))[:5]
        record = {"at": _now(), "kind": "gate", "gate": gate, "status": status,
                  "durationS": round(float(total_s), 3), "checks": int(checks),
                  "slowest": top}
        records = _load(root)
        gates = [r for r in records if r.get("kind") == "gate"]
        if len(gates) >= GATE_RECORDS_MAX:
            drop = len(gates) - GATE_RECORDS_MAX + 1
            kept: list[dict[str, Any]] = []
            for r in records:
                if drop and r.get("kind") == "gate":
                    drop -= 1
                    continue
                kept.append(r)
            write_json(root / LEDGER_REL, {"schemaVersion": 1, "records": kept})
        _append(root, record)
    except Exception:
        pass


# -- seat telemetry (2026-07-31 amendment to SPEC delegation-cost-trends) ------
# A SEAT is (role, model, effort) — who sat in a chair for a piece of work, not a
# person and not a vendor. The classes below are CONTROLLED on purpose: a free-text
# class is a column nobody can group by, and grouping is the whole point. They are
# the vocabulary two independent audits converged on (2026-07-31); `unclassified`
# is the pressure release so nobody skips a row for lack of a label, and a class
# that keeps landing there earns a spec amendment, not an ad-hoc string.
DEFECT_CLASSES = (
    "memory-as-evidence",            # asserted from memory what was already written down
    "contract-unread",               # the contract existed (help text, playbook, list)
    "warn-blindness",                # a signal fired and was overridden
    "fire-and-forget",               # launched something and never checked it
    "check-passes-for-the-wrong-reason",
    "silent-drop",                   # swallowed an input/failure with no visible trace
    "overclaim-in-a-record",         # a durable artifact says more than was verified
    "audit-caught",                  # a pre-merge audit seat found it after the implementing lane shipped it
    "unclassified",
)
EVIDENCE_KINDS = ("commit", "gate-run", "audit", "owner-report")


def seat_identity(seat: dict | None) -> dict | None:
    """A seat counts only when COMPLETE: role, model and effort, all non-empty.
    A partial seat is not a smaller truth, it is an unattributable row — it would
    silently merge with other partials under the same key."""
    if not isinstance(seat, dict):
        return None
    out = {k: str(seat.get(k) or "").strip() for k in ("role", "model", "effort")}
    return out if all(out.values()) else None


def seat_from_text(text: str | None) -> dict | None:
    """`role:model:effort` — the operator-declared form. Anything else is None."""
    parts = [p.strip() for p in str(text or "").split(":")]
    return seat_identity(dict(zip(("role", "model", "effort"), parts))) if len(parts) == 3 else None


def _session_seat_file(src: dict, root: Path | None) -> dict | None:
    """The interactive session's LIVE seat from .harness/state/session-seat.json
    (written by the overseer_seat_session hook). Accepted ONLY when its sessionId
    names the running session (== CLAUDE_CODE_SESSION_ID): a seat left by another
    or older session must never attribute THIS session's rows to the wrong model —
    the exact silent mis-attribution seat_from_env's pin-refusal already guards.
    Fail-open: any missing var / read / parse error yields no seat (self refuses)."""
    sid = src.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    if root is None:
        try:
            from harness_lib.common import ROOT as root  # type: ignore
        except ImportError:
            return None
    try:
        doc = json.loads((Path(root) / ".harness" / "state" / "session-seat.json")
                         .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("sessionId") != sid:
        return None
    return seat_identity(doc)


def seat_from_env(env: dict | None = None, root: Path | None = None) -> dict | None:
    """The CURRENT session's seat, spliced by chat_engines.session_role_env. Never
    falls back to the routing pin: the pin is what SHOULD be seated, and this file
    exists precisely because the two differ (a fallback model ran an overseer pin
    for a whole session).

    Interactive-overseer fallback (D1.3): an owner-driven session gets no
    HARNESS_SESSION_* splice, so the env triplet is empty and every `self` refuses.
    When it is, read the LIVE seat the SessionStart hook wrote for THIS session —
    same-session only, never a stale/foreign file (see _session_seat_file)."""
    src = os.environ if env is None else env
    return seat_identity({"role": src.get("HARNESS_SESSION_ROLE"),
                          "model": src.get("HARNESS_SESSION_MODEL"),
                          "effort": src.get("HARNESS_SESSION_EFFORT")}) \
        or _session_seat_file(src, root)


def record_defect(root: Path, *, defect_class: str, evidence_kind: str, evidence_ref: str,
                  produced_by: dict | None = None, missed_by: dict | None = None) -> bool:
    """Append ONE declared defect to the shared SPEC-173 sink. Returns whether it
    landed, so an operator-invoked caller can refuse to print `recorded` over a
    silent drop. Refuses (False, nothing written) when the class is not controlled,
    the evidence is unusable, or NEITHER seat is complete — a defect nobody can be
    attributed to is a story, not a measurement."""
    from harness_lib import mutation_probe, telemetry_sink
    if defect_class not in DEFECT_CLASSES or evidence_kind not in EVIDENCE_KINDS:
        return False
    ref = str(evidence_ref or "").strip()[:200]
    produced, missed = seat_identity(produced_by), seat_identity(missed_by)
    if not ref or not (produced or missed):
        return False
    return telemetry_sink.append_jsonl(mutation_probe.defect_sink(root), {
        "kind": "defect", "defectClass": defect_class,
        "producedBy": produced, "missedBy": missed,
        "evidence": {"kind": evidence_kind, "ref": ref}})


def record_delegation(root: Path, *, model: str, agent_type: str, tokens: int,
                      tool_uses: int | None = None, duration_s: float | None = None,
                      task: str = "", target: str | None = None,
                      outcome: str | None = None, effort: str | None = None,
                      caller_seat: dict | None = None, profile: str | None = None,
                      packet: dict | None = None) -> bool:
    """Subagent delegations (planner->implementer/explore runs) are real spend
    the ledger must see (R26/R27): ~425k Opus tokens were invisible before
    this kind existed. `tokens` lands in estTokens so topExpensive and
    byStack aggregate delegations alongside workflows without new fields.

    Returns whether the row landed. This is the operator-invoked writer, so it owns
    the gate-hold refusal (cost-metrics.json is under the held `.harness/state`):
    a write during a live hold lands on materialized HEAD and is discarded on
    release, so refuse VISIBLY instead of printing 'recorded' over a lost row
    (defect-sink-lost-under-gate-hold, 2026-07-31).

    The HARNESS_SCENARIO_ISOLATED arm is the in-gate contract, the SAME exemption
    scenario_isolation.hold_write_guard already carries: inside a gate scenario the
    per-scenario snapshot/restore of `.harness/state` IS the isolation, so the row the
    scenario asserts on must land. Unlike record_turn (see its comment) this writer DOES
    run inside the gate, and without the arm every delegation-recording scenario was red
    by construction on the serial path (measured 2026-08-03: ort-3)."""
    if gate_holding(root) and os.environ.get("HARNESS_SCENARIO_ISOLATED") != "1":
        print("gate-hold: delegation NOT recorded — a gate holds .harness (the row "
              "would land on materialized HEAD and be discarded on release; retry "
              "after the gate)", file=sys.stderr)
        return False
    try:
        return _append(root, {"at": _now(), "kind": "delegation", "model": model,
                       "agentType": agent_type, "target": target,
                       "estTokens": int(tokens), "toolUses": tool_uses,
                       "durationS": duration_s, "costUsd": None,  # seat for real cost
                       # ort item 4: the overseer's kept/partial/rejected/reworked
                       # verdict — feeds delegations.byOutcome so lane yield is a
                       # number, not a feeling. None = pre-toolkit records.
                       "outcome": outcome,
                       # seat-telemetry (2026-07-31): the row named WHO WAS CALLED but
                       # never who called, so "which seat delegates well" had no key to
                       # group by. Both are additive and NULLABLE — a legacy row keeps
                       # meaning exactly what it meant, and a caller the session cannot
                       # prove stays null instead of being guessed from the routing pin.
                       "effort": effort,
                       "callerSeat": seat_identity(caller_seat) if caller_seat else None,
                       # THE CONDITIONS of the work, not just who did it (owner
                       # 2026-07-31): a seat handed an over-budget capsule under a
                       # high-risk profile errs more BECAUSE OF THE CONDITION, and a
                       # rate that cannot see the condition bills the seat for it.
                       # packet_economy.compose_spawn already computes this per spawn
                       # and threw it away. Nullable: unknown stays unknown.
                       "profile": profile,
                       "packet": dict(packet) if isinstance(packet, dict) else None,
                       "title": task[:200]})
    except Exception:
        # The append is best-effort by design (a ledger hiccup must not kill a lane),
        # but the CALLER now learns it: an operator-invoked `delegation` printing
        # "recorded" over a silent drop is the same defect the defect-verb fixed
        # (audit, kimi 2026-07-31).
        return False


# Fallback chars-per-token when a workflow's token-audit.json carries no
# estimator block — the same seed the sizer uses (project.json tokenBudget).
CAPSULE_COST_CPT_DEFAULT = 3.1


def _read_obj(path: Path) -> dict[str, Any] | None:
    """dict-or-None: an absent, unreadable or non-object JSON file is "no data",
    never an exception (this module's never-crash writer discipline)."""
    try:
        data = read_json(path, None)
    except (OSError, ValueError):  # ValueError covers JSONDecodeError
        return None
    return data if isinstance(data, dict) else None


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def capsule_cost(base: Path) -> dict[str, Any] | None:
    """Cost per VALID capsule for one workflow dir, from artifacts already on disk.

    numerator = worker prompt tokens + the required-reads wave (waveWithDigest when
    a context digest was produced, else waveWithoutDigest) + reducer prompt tokens,
    all from `reduce/token-audit.json`, PLUS the workers' stdout chars divided by
    the audit's `estimator.charsPerToken`. denominator = `workersValid` from
    `reduce/validation.json`.

    Zero valid capsules NEVER divides: the numerator is still recorded and
    `costPerValidCapsuleTokens` is null with `undefinedReason`. Keys are EN-only —
    the round doc's PT names map as `custoPorCapsuleValido` ->
    `costPerValidCapsuleTokens` and `indefinido` -> null + `undefinedReason:
    "zero valid capsules"`.

    Returns None ONLY when `reduce/validation.json` is missing/unreadable (nothing
    to measure). Never raises: a missing token-audit.json degrades its components
    to 0 and says so in `notes`.
    """
    try:
        base = Path(base)
        validation = _read_obj(base / "reduce" / "validation.json")
        if validation is None:
            return None
        notes: list[str] = []
        audit = _read_obj(base / "reduce" / "token-audit.json")
        if audit is None:
            audit = {}
            notes.append("reduce/token-audit.json missing: prompt + required-read "
                         "components counted as 0")
        summary = audit.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        estimator = audit.get("estimator")
        raw_cpt = estimator.get("charsPerToken") if isinstance(estimator, dict) else None
        cpt = (float(raw_cpt) if isinstance(raw_cpt, (int, float)) and raw_cpt > 0
               else CAPSULE_COST_CPT_DEFAULT)
        reads = summary.get("requiredReads")
        reads = reads if isinstance(reads, dict) else {}
        wave = (reads.get("waveWithDigest") if reads.get("digestPresent")
                else reads.get("waveWithoutDigest"))
        chars = 0
        for log in sorted((base / "run-logs").glob("worker-*.stdout.log")):
            try:
                chars += len(log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                notes.append(f"unreadable worker stdout log: {log.name}")
        numerator = {"workerPrompt": _as_int(summary.get("totalWorkerPromptTokens")),
                     "requiredReads": _as_int(wave),
                     "reducerPrompt": _as_int(summary.get("totalReducerPromptTokens")),
                     "workerStdout": round(chars / cpt)}
        numerator["total"] = sum(numerator.values())
        valid = _as_int(validation.get("workersValid"))
        return {
            "schemaVersion": 1,
            "workflowId": (validation.get("workflowId") or summary.get("workflowId")
                           or base.name),
            "charsPerToken": cpt,
            "numeratorTokens": numerator,
            "workersValid": valid,
            "costPerValidCapsuleTokens": (round(numerator["total"] / valid, 3)
                                          if valid > 0 else None),
            "undefinedReason": None if valid > 0 else "zero valid capsules",
            "sources": ["reduce/token-audit.json", "reduce/validation.json",
                        "run-logs/worker-*.stdout.log"],
            "notes": notes,
        }
    except Exception:
        return None


def record_workflow(root: Path, workflow: dict[str, Any], status: str, *,
                    capsule_cost: dict[str, Any] | None = None) -> None:
    try:
        audit = workflow.get("tokenAudit") or {}
        summary = audit.get("summary") or audit
        duration = 0.0
        for rnd in workflow.get("roundHistory") or []:
            started, finished = rnd.get("startedAt"), rnd.get("finishedAt")
            if started and finished:
                try:
                    from .common import parse_iso_datetime
                except ImportError:
                    from harness_lib.common import parse_iso_datetime
                s, f = parse_iso_datetime(str(started)), parse_iso_datetime(str(finished))
                if s and f:
                    duration += max((f - s).total_seconds(), 0)
        cpu = sum(float((w.get("run") or {}).get("cpuSeconds") or 0)
                  for w in workflow.get("workers") or [])
        # M1 measurement rung: the largest worker stdout this run produced. Recorded so a
        # self-review threshold rule can spot repeat-offender oversized-stdout commands —
        # actual masking stays PARKED until this data justifies it (round M1, cost lens).
        max_stdout_bytes = max((int((w.get("run") or {}).get("stdoutBytes") or 0)
                                for w in workflow.get("workers") or []), default=0)
        # SPEC-118 v3: stream-json workers surface REAL usage/cost in run.observedUsage
        # (sum over workers). When present, real numbers displace the chars/4 estimate
        # and costBasis records which side the metrics panel is reading (honesty).
        obs_in = obs_out = 0
        obs_cost = 0.0
        has_obs = False
        for w in workflow.get("workers") or []:
            u = (w.get("run") or {}).get("observedUsage") or {}
            if not u:
                continue
            has_obs = True
            obs_in += int(u.get("inputTokens") or 0)
            obs_out += int(u.get("outputTokens") or 0)
            if isinstance(u.get("costUsd"), (int, float)):
                obs_cost += float(u["costUsd"])
        record = {"at": _now(), "kind": "workflow",
                  "cpuS": round(cpu, 3) if cpu else None,
                  "workflowId": workflow.get("workflowId"),
                  "type": workflow.get("type"),
                  "profile": workflow.get("workflowProfile"),
                  "target": workflow.get("target"),
                  "estTokens": summary.get("totalPlannedTokens"),
                  "observedTokens": (obs_in + obs_out) if has_obs else summary.get("totalObservedTokens"),
                  "workers": len(workflow.get("workers") or []),
                  "maxStdoutBytes": max_stdout_bytes or None,
                  "durationS": duration or None,
                  "costUsd": round(obs_cost, 6) if (has_obs and obs_cost) else None,
                  "costBasis": "observed" if has_obs else "estimated",
                  "status": status}
        # CE.2 communication-economy meter: real in/out split (the summed
        # `observedTokens` loses it) + amplification = input tokens moved into
        # context per output token produced (study §13; higher = more context
        # pushed per unit of useful output). Only when real usage exists, so the
        # estimated path stays byte-identical.
        if has_obs:
            record["economy"] = {"observedInputTokens": obs_in,
                                 "observedOutputTokens": obs_out,
                                 "communicationAmplification":
                                     round(obs_in / obs_out, 3) if obs_out else None}
        # DW.3 graph-shape trace metrics — topology made observable BEFORE any
        # auto-adaptation (measurement before control). All from data already on
        # hand: fanOut = worker count, depth = sequential rounds, makespanS reuses
        # the wall-clock `duration` computed above. Added only when >=1 worker ran,
        # so the empty-workers record stays byte-identical to today.
        workers_l = workflow.get("workers") or []
        if workers_l:
            graph = {"fanOut": len(workers_l),
                     # depth floor 1: a workflow that actually ran a worker executed
                     # at least one round even if roundHistory wasn't recorded.
                     "depth": len(workflow.get("roundHistory") or []) or 1,
                     "makespanS": duration or None}
            # criticalPathS = longest single-node wall-clock. Real run records DO
            # carry per-worker startedAt/finishedAt (harness.py, async_runtime.py),
            # so it's honest to compute — but OMITTED (never fabricated) for any
            # record whose workers lack them, e.g. cpu-only estimate fixtures.
            spans = []
            for w in workers_l:
                run = w.get("run") or {}
                started, finished = run.get("startedAt"), run.get("finishedAt")
                if started and finished:
                    try:
                        from .common import parse_iso_datetime
                    except ImportError:
                        from harness_lib.common import parse_iso_datetime
                    s, f = parse_iso_datetime(str(started)), parse_iso_datetime(str(finished))
                    if s and f:
                        spans.append(max((f - s).total_seconds(), 0))
            if spans:
                graph["criticalPathS"] = round(max(spans), 3)
            record["graph"] = graph
        # ctx-medidor-custo: the codex §5 metric in the DURABLE ledger (the full
        # document lives at reduce/capsule-cost.json). Trimmed to the three numbers
        # a query needs; absent kwarg adds NO key, so every existing record stays
        # byte-identical.
        if capsule_cost:
            record["capsuleCost"] = {
                "numeratorTokens": (capsule_cost.get("numeratorTokens") or {}).get("total"),
                "workersValid": capsule_cost.get("workersValid"),
                "costPerValidCapsuleTokens": capsule_cost.get("costPerValidCapsuleTokens"),
            }
        _append(root, record)
    except Exception:
        pass


def _expense_label(r: dict[str, Any]) -> str | None:
    """One honest display label per record kind, from real ledger fields only
    (never invented) — server-side so the UI needs no per-kind fallback chain."""
    kind = r.get("kind")
    if kind == "chat-turn":
        who = r.get("model") or r.get("engine")
        return "·".join(p for p in (who, r.get("chatMode")) if p) or None
    if kind == "delegation":
        title = (r.get("title") or "").strip()
        return title[:40] or r.get("agentType") or r.get("model")
    if kind == "workflow":
        return r.get("workflowId") or r.get("type")
    return None


def _stats(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    if not values:
        return {}
    return {"count": len(values), "total": round(sum(values), 6),
            "median": round(statistics.median(values), 6),
            "p95": round(values[max(int(len(values) * 0.95) - 1, 0)], 6)}


def _group(records: list[dict[str, Any]], key: str, field: str) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[float]] = {}
    for r in records:
        value = r.get(field)
        if isinstance(value, (int, float)):
            buckets.setdefault(str(r.get(key) or "(none)"), []).append(float(value))
    return {k: _stats(v) for k, v in sorted(buckets.items())}


def _dir_mb(path: Path) -> float:
    total = 0
    try:
        for base, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += (Path(base) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / 1_048_576, 2)


def _shares(records: list[dict[str, Any]], key: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    for r in records:
        name = str(r.get(key) or "(none)")
        counts[name] = counts.get(name, 0) + 1
    total = sum(counts.values()) or 1
    return {k: round(v / total, 3) for k, v in sorted(counts.items())}


def _cts(delegations: list[dict[str, Any]]) -> dict[str, Any]:
    """C6 / manuscript 9.5-b cost-to-success: over the delegation corpus that
    carries an overseer `outcome`, sum of estTokens divided by the count of
    useful outcomes. kept-only (`ctsTokens`) is the hard number; kept+reworked
    (`costToUsefulOutcome`) is the honest variant (rework is useful, with
    rework). A zero denominator yields None — never a divide-by-zero — so a
    corpus with no kept (or no useful) delegation reports null, not a crash."""
    corpus = [r for r in delegations if r.get("outcome") is not None
              and isinstance(r.get("estTokens"), (int, float))]
    total = sum(float(r["estTokens"]) for r in corpus)
    kept = sum(1 for r in corpus if r.get("outcome") == "kept")
    useful = sum(1 for r in corpus if r.get("outcome") in ("kept", "reworked"))
    return {"corpus": len(corpus), "estTokensTotal": round(total, 3),
            "keptCount": kept, "usefulCount": useful,
            "ctsTokens": round(total / kept, 3) if kept else None,
            "costToUsefulOutcome": round(total / useful, 3) if useful else None}


def _stack_view(records: list[dict[str, Any]]) -> dict[str, Any]:
    view: dict[str, Any] = {"count": len(records)}
    for field in ("costUsd", "durationS", "cpuS", "estTokens"):
        view[field] = _stats([float(r[field]) for r in records
                              if isinstance(r.get(field), (int, float))])
    return view


def _slowest_recurring(gates: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Aggregate each gate record's slowest[] blocks by name: how often a
    block recurs in a run's top-5 and its median duration when it does."""
    buckets: dict[str, list[float]] = {}
    for r in gates:
        for entry in r.get("slowest") or []:
            name, dur = entry.get("name"), entry.get("durationS")
            if name and isinstance(dur, (int, float)):
                buckets.setdefault(str(name), []).append(float(dur))
    return {name: {"count": len(vals),
                   "medianS": round(statistics.median(vals), 3)}
            for name, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}


def _approvals(root: Path) -> dict[str, Any]:
    """C5 / manuscript §7.7 approval-service metrics — ALL derived read-only from
    the decide inbox + intake/escalation ledgers, zero new state. Reuses
    `decision_inbox.pending_decisions` (which computes ageHours/sloBreached via
    `_age_fields`/`_limits`) and `_limits` verbatim so this metric and the panel
    can NEVER diverge on age/SLO. `overrideRate` and `invalidatedCount` are
    OMITTED (🔬): recon showed the ledger stores neither the recommended-vs-chosen
    split (intake_queue.decide records only status/decidedAt/note) nor any durable
    digest-mismatch event (C12 `invalidated` is a transient apply_decision refusal,
    never persisted) — so neither is measurable without a new writer."""
    try:
        rows = decision_inbox.pending_decisions(root)
    except Exception:
        rows = []
    slo_hours, expiry_hours = decision_inbox._limits(root)
    ages = _stats([r["ageHours"] for r in rows if isinstance(r.get("ageHours"), (int, float))])
    # resolved intake decisions grouped by the recorded choice (status), from the
    # durable queue. Bounded window: intake_queue trims oldest DECIDED first.
    queue = read_json(root / intake_queue.QUEUE_REL, {}) or {}
    entries = queue.get("entries") or [] if isinstance(queue, dict) else []
    by_choice: dict[str, int] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("status") in intake_queue.DECISIONS:
            by_choice[e["status"]] = by_choice.get(e["status"], 0) + 1
    esc = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
    esc_resolved = len(esc.get("resolvedIds") or []) if isinstance(esc, dict) else 0
    return {
        "pending": len(rows),
        "sloBreached": sum(1 for r in rows if r.get("sloBreached")),
        "medianAgeHours": ages.get("median"),
        "p95AgeHours": ages.get("p95"),
        # L2 safe-pause: pending approvals past the expiry limit — apply_decision
        # refuses these as expired until an explicit --allow-expired override. Each row now
        # carries its own `expired` flag (_age_fields, one source), so reuse it.
        "expired": sum(1 for r in rows if r.get("expired")),
        "resolved": {"intakeByChoice": dict(sorted(by_choice.items())),
                     "escalationsResolved": esc_resolved,
                     "total": sum(by_choice.values()) + esc_resolved},
        "sloHours": slo_hours,
        "expiryHours": expiry_hours,
        "note": "ages/SLO reuse decision_inbox._age_fields/_limits (panel + metric "
                "share one source). resolved intake counts are a bounded window "
                "(queue trims oldest decided first). overrideRate + invalidatedCount "
                "omitted (🔬): the ledger records no recommended-vs-chosen split and "
                "no durable digest-mismatch event.",
    }


def summarize(root: Path, include_disk: bool = True) -> dict[str, Any]:
    # include_disk=False skips the two _dir_mb os.walk()s (harnessDiskMB + stateFilesMB).
    # Measured 2026-07-23: that walk over ~12k files is ~85% of the /api/state COLD path,
    # and the ONLY caller that reads those fields is the CLI `metrics` verb + this module's
    # self-check — the panel (metrics_snapshot -> _costs) and self_review DISCARD them. So
    # the panel path passes include_disk=False and the fields carry None (shape preserved).
    started = time.monotonic()
    records = _load(root)
    turns = [r for r in records if r.get("kind") == "chat-turn"]
    workflows = [r for r in records if r.get("kind") == "workflow"]
    delegations = [r for r in records if r.get("kind") == "delegation"]
    gates = [r for r in records if r.get("kind") == "gate"]
    # Sanity band: a real reply averages ~2-4 chars/token; ratios outside [1.5, 12] are
    # physically impossible for natural language or code and only ever come from skewed
    # or test-seeded records — drop them so the estimator never learns garbage.
    cpt_samples: list[float] = []
    # per-target-token-calibration: target-tagged samples are GROUPED per
    # subject (report-only feed for a future per-target recalibration writer)
    # instead of silently dropped; they still never touch the harness's own set.
    target_cpt: dict[str, list[float]] = {}
    for r in turns:
        rc, ot = r.get("replyChars"), r.get("outTokens")
        if not (isinstance(rc, (int, float)) and isinstance(ot, (int, float)) and ot):
            continue
        ratio = rc / ot
        if not (CPT_SANE_MIN <= ratio <= CPT_SANE_MAX):
            continue
        if r.get("target"):
            target_cpt.setdefault(str(r["target"]), []).append(ratio)
        else:
            cpt_samples.append(ratio)
    project = read_json(root / ".harness" / "project.json", {}) or {}
    seed_cpt = (((project.get("workflows") or {}).get("budgets") or {})
                .get("tokenBudget") or {}).get("charsPerToken", 3.1)
    # The learned local override (if any) is the effective configured value — compare
    # drift against what sizing actually uses, not the frozen committed seed.
    override_cpt = token_calibration.read_override(root)
    configured_cpt = override_cpt if override_cpt is not None else seed_cpt

    def expense(r: dict[str, Any]) -> float:
        if isinstance(r.get("costUsd"), (int, float)):
            return float(r["costUsd"])
        est = r.get("estTokens") or r.get("inTokens")
        return float(est) / 1_000_000 if isinstance(est, (int, float)) else 0.0

    top = sorted(records, key=expense, reverse=True)[:5]
    return {
        "records": len(records),
        "chatTurns": {
            "count": len(turns),
            "costUsd": _stats([r["costUsd"] for r in turns
                               if isinstance(r.get("costUsd"), (int, float))]) if turns else {},
            "byModel": _group(turns, "model", "costUsd"),
            "byTarget": _group(turns, "target", "costUsd"),
            "byDay": _group([{**r, "day": str(r.get("at", ""))[:10]} for r in turns],
                            "day", "costUsd"),
        },
        "workflows": {
            "count": len(workflows),
            "estTokens": _stats([r["estTokens"] for r in workflows
                                 if isinstance(r.get("estTokens"), (int, float))]) if workflows else {},
            "durationS": _stats([r["durationS"] for r in workflows
                                 if isinstance(r.get("durationS"), (int, float))]) if workflows else {},
            "byType": _group(workflows, "type", "estTokens"),
            "byTarget": _group(workflows, "target", "estTokens"),
            # SPEC-118 v3: observed vs estimated split — how much workflow cost is
            # real (stream-json) vs chars/4. costUsd stats cover only real records.
            "costBasisShare": _shares(workflows, "costBasis"),
            "costUsd": _stats([r["costUsd"] for r in workflows
                               if isinstance(r.get("costUsd"), (int, float))]) if workflows else {},
        },
        "estimator": {
            "configuredCharsPerToken": configured_cpt,
            "observedCharsPerToken": round(statistics.median(cpt_samples), 3) if cpt_samples else None,
            "cptSamples": len(cpt_samples),
            # per-target-token-calibration: per-subject observed CPT, report-only
            # (the feed a per-target recalibration writer would consume).
            "byTargetCpt": {t: {"samples": len(vals),
                                "observed": round(statistics.median(vals), 3)}
                            for t, vals in sorted(target_cpt.items())},
            "note": "observed = replyChars/outTokens over real chat turns; workflow "
                    "'observedTokens' are still chars-divisor estimates (R24/R26)",
        },
        "topExpensive": [{**{k: r.get(k) for k in ("at", "kind", "workflowId", "model", "type",
                                                    "target", "costUsd", "estTokens", "durationS")},
                          "label": _expense_label(r)}
                         for r in top],
        "delegations": {
            "count": len(delegations),
            "estTokens": _stats([r["estTokens"] for r in delegations
                                 if isinstance(r.get("estTokens"), (int, float))]),
            "byModel": _group(delegations, "model", "estTokens"),
            # ort item 4: lane yield — tokens grouped by the overseer's
            # kept/partial/rejected/reworked verdict + count shares, so the
            # data (not the overseer's feeling) reassigns item classes to lanes.
            "byOutcome": _group(delegations, "outcome", "estTokens"),
            "outcomeShares": _shares(delegations, "outcome"),
            # C6 / §9.5-b cost-to-success: spend per useful outcome. Additive —
            # no existing key changes. byModelCTS reuses the byX grouping idiom
            # (same {model: {...}} shape as byModel/byOutcome; no new structure),
            # applying the same _cts calc per model since CTS is a total/kept
            # ratio _group's stats can't express. Workflows are excluded (no
            # per-task kept/rejected outcome); global + byModel only.
            "costToSuccess": {
                **_cts(delegations),
                "byModelCTS": {m: _cts([r for r in delegations
                                        if str(r.get("model") or "(none)") == m])
                               for m in sorted({str(r.get("model") or "(none)")
                                                for r in delegations
                                                if r.get("outcome") is not None})},
                "note": "ctsTokens = sum estTokens(outcome present) / #kept; "
                        "costToUsefulOutcome denominator = kept+reworked "
                        "(partial/rejected excluded from useful); null (never a "
                        "divide) when a denominator is zero. Workflows excluded "
                        "(no per-task outcome). No task-class/session "
                        "stratification yet (9.5-b / C6).",
            },
            # TE.2: chronological estTokens + the last delegation, so the
            # self-review outlier/trend rules stay pure over this summary.
            "series": [r["estTokens"] for r in delegations
                       if isinstance(r.get("estTokens"), (int, float))],
            "latest": {k: delegations[-1].get(k) for k in
                       ("at", "model", "agentType", "estTokens", "title", "toolUses")}
                      if delegations else {},
        },
        # SPEC-111 R28: usage shares (all records, field-independent) — feeds
        # the self-review criticality boost for heavily-used subjects
        "usage": {
            "workflowTypeShare": _shares(workflows, "type"),
            "targetShare": _shares(records, "target"),
        },
        # SPEC-111 R28: machine-impact views — harness stack vs project stack,
        # computed on demand only (no polling, R27)
        "machine": {
            "harnessDiskMB": {".harness": _dir_mb(root / ".harness"),
                              "graphify-out": _dir_mb(root / "graphify-out")}
            if include_disk else None,
            "byStack": {
                "harness": _stack_view([r for r in records if not r.get("target")]),
                "project": _stack_view([r for r in records if r.get("target")]),
            },
        },
        # R27: the observation stack measures itself
        # perf-metrics rung Phase 1: the gate wall-time view the future
        # perf-hotspot-recurring rule (Phase 2, OPEN) will consume — same
        # series shape as the delegation trend family.
        "gates": {
            "runs": len(gates),
            "durationS": _stats([float(r["durationS"]) for r in gates
                                 if isinstance(r.get("durationS"), (int, float))]),
            "byGate": _group(gates, "gate", "durationS"),
            "bySlowest": _slowest_recurring(gates),
            "series": [{"at": r.get("at"), "gate": r.get("gate"),
                        "durationS": r.get("durationS")} for r in gates[-20:]],
        },
        # C5 / §7.7 approval-service metrics: pending/SLO/age + resolved-by-choice,
        # all derived from the decide inbox + intake/escalation ledgers (no new state).
        "approvals": _approvals(root),
        # W29 bet 2: per model|agentType outcome tallies + info-only burden
        # suggestion (track_record.py); None on any failure, never a crash.
        "trackRecord": _track_record(root),
        # seat-telemetry: the SIBLING of trackRecord, never a replacement — that one
        # groups the seat that was CALLED, this one the seats that produced and that
        # missed. Info-only, never ranked, never fed to burden (EXP-33 stays gated).
        "defectsBySeat": _defects_by_seat(root, delegations),
        "observability": {
            "ledgerBytes": (root / LEDGER_REL).stat().st_size
            if (root / LEDGER_REL).exists() else 0,
            "stateFilesMB": _dir_mb(root / ".harness" / "state") if include_disk else None,
            "selfReviewLastDurationS": (read_json(root / ".harness" / "state" / "self-review.json",
                                                  {}) or {}).get("durationS"),
            "summarizeMs": round((time.monotonic() - started) * 1000, 1),
        },
    }


def resolve_caller_seat(declared: str | None) -> dict | None:
    """The seat that DELEGATED: the operator's declaration when given, else the
    session triplet. An unparseable declaration RAISES — it used to fall back to
    the env seat, so a typo recorded a caller the operator never named (audit,
    2026-07-31). Silence is the failure mode this whole module exists to remove."""
    from harness_lib.common import HarnessError
    if not declared:
        return seat_from_env()
    seat = seat_from_text(declared)
    if seat is None:
        raise HarnessError(f"--caller-seat {declared!r} is not role:model:effort — "
                           f"nothing recorded (omit it to use the session seat)")
    return seat


def cmd_defect(args: Any) -> None:
    """Declare ONE defect against the seat(s) it belongs to (seat telemetry).
    `self` = the current session seat; refused when the session cannot prove it.
    Lives HERE, not in harness.py: the registry's own docstring says a new verb's
    handler belongs in a harness_lib module, and harness.py carries a pinned line
    budget (wt-3) that the first draft blew straight through."""
    from harness_lib.common import ROOT, HarnessError
    from harness_lib import intake_queue
    if getattr(args, "defect_class", None) == "ingest":  # `defect ingest [PATH|-]` -> L3 pipeline
        intake_queue.cmd_defect_ingest(args)
        return
    if getattr(args, "path", None):  # a declaration takes no positional path
        raise HarnessError("defect <class> takes no positional PATH — did you mean "
                           "`defect ingest <path>`? Nothing recorded")

    def _seat(text: str | None) -> tuple[dict | None, bool]:
        if not text:
            return None, True
        if text == "self":
            seat = seat_from_env()
            return seat, seat is not None
        seat = seat_from_text(text)
        return seat, seat is not None
    produced, ok_p = _seat(args.producer)
    missed, ok_m = _seat(args.missed_by)
    if not (ok_p and ok_m):
        raise HarnessError("`self` needs a complete session seat (HARNESS_SESSION_ROLE/"
                           "MODEL/EFFORT); otherwise pass role:model:effort explicitly")
    kind, _, ref = str(args.evidence or "").partition(":")
    if not record_defect(ROOT, defect_class=args.defect_class, evidence_kind=kind,
                         evidence_ref=ref, produced_by=produced, missed_by=missed):
        raise HarnessError(
            f"defect NOT recorded — need a controlled class, --evidence "
            f"<{'|'.join(EVIDENCE_KINDS)}>:<ref>, and at least one complete seat "
            f"(role:model:effort). Nothing was written.")
    print(json.dumps({"recorded": "defect", "defectClass": args.defect_class,
                      "producedBy": produced, "missedBy": missed,
                      "evidence": {"kind": kind, "ref": ref}}, indent=2, ensure_ascii=False))


def _track_record(root: Path) -> dict[str, Any] | None:
    try:
        from harness_lib import track_record
        return track_record.summarize_track(root)
    except Exception:
        return None


def _seat_key(seat: dict | None) -> str | None:
    s = seat_identity(seat)
    return f"{s['role']}|{s['model']}|{s['effort']}" if s else None


def _defects_by_seat(root: Path, delegations: list[dict]) -> dict[str, Any] | None:
    """Declared defects per seat, DIVIDED by the opportunities that seat actually
    had — a bare count measures volume, which is how the busiest seat looks worst
    and the idle one looks clean (the exact defence an overseer reached for when an
    owner asked who errs more, 2026-07-31).

    Denominators come from the delegation rows the ledger STILL RETAINS (it is a
    500-row ring), so each side carries the window it was computed over and a seat
    with no retained opportunity reports null — never 0%, which reads as perfect.
    Mechanical signals stay in their own block, UNATTRIBUTED: the tree has no
    trustworthy causal join from a gate failure to a seat, and inventing one would
    be the fabrication this whole feature exists to make visible."""
    try:
        from harness_lib import mutation_probe
        sink = mutation_probe.defect_sink(root)
        rows: list[dict] = []
        if sink.exists():
            for line in sink.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 — one bad line never blinds the series
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        signals = {k: sum(1 for r in rows if r.get("kind") == k)
                   for k in ("review-warn", "mutate-survivor", "mutate-waiver")}
        # latest declaration for a given (class, evidence) WINS: attribution can be
        # corrected in an append-only sink without inflating the count.
        declared: dict[tuple, dict] = {}
        malformed = 0
        for row in rows:
            if row.get("kind") != "defect":
                continue
            ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
            cls, ref = row.get("defectClass"), str(ev.get("ref") or "")
            produced, missed = seat_identity(row.get("producedBy")), seat_identity(row.get("missedBy"))
            if cls not in DEFECT_CLASSES or ev.get("kind") not in EVIDENCE_KINDS \
                    or not ref or not (produced or missed):
                malformed += 1
                continue
            declared[(cls, ev.get("kind"), ref)] = {"class": cls, "produced": produced, "missed": missed}
        seats: dict[str, dict[str, Any]] = {}

        def _seat(key: str) -> dict[str, Any]:
            return seats.setdefault(key, {"seat": key, "producedDefects": 0, "missedDefects": 0,
                                          "involvedDefects": 0, "classes": {},
                                          "delegatedOutputs": 0, "acceptedReviews": 0,
                                          # the CONDITIONS the seat worked under, so a
                                          # rate can be read against them instead of in
                                          # a vacuum (owner 2026-07-31)
                                          "conditions": {"profiles": {}, "packetsOverBudget": 0,
                                                         "packetsKnown": 0}})
        for d in declared.values():
            pk, mk = _seat_key(d["produced"]), _seat_key(d["missed"])
            if pk:
                _seat(pk)["producedDefects"] += 1
            if mk:
                _seat(mk)["missedDefects"] += 1
            for key in {k for k in (pk, mk) if k}:  # same seat both sides counts ONCE here
                row = _seat(key)
                row["involvedDefects"] += 1
                row["classes"][d["class"]] = row["classes"].get(d["class"], 0) + 1
        no_effort = no_caller = 0
        for r in delegations:
            seat = seat_identity({"role": r.get("agentType"), "model": r.get("model"),
                                  "effort": r.get("effort")})
            if seat:
                row = _seat(_seat_key(seat))
                row["delegatedOutputs"] += 1
                cond = row["conditions"]
                if r.get("profile"):
                    cond["profiles"][r["profile"]] = cond["profiles"].get(r["profile"], 0) + 1
                packet = r.get("packet")
                if isinstance(packet, dict) and packet.get("over") is not None:
                    cond["packetsKnown"] += 1
                    cond["packetsOverBudget"] += 1 if packet.get("over") else 0
            elif not r.get("effort"):
                no_effort += 1
            caller = _seat_key(r.get("callerSeat"))
            if caller:
                if r.get("outcome") in ("kept", "reworked"):
                    _seat(caller)["acceptedReviews"] += 1
            else:
                no_caller += 1

        def _per100(n: int, d: int) -> float | None:
            return round(100.0 * n / d, 1) if d else None
        for row in seats.values():
            row["producedPer100DelegatedOutputs"] = _per100(row["producedDefects"], row["delegatedOutputs"])
            row["missedPer100AcceptedReviews"] = _per100(row["missedDefects"], row["acceptedReviews"])
        # One PHYSICAL seat can arrive under two role words — a delegation says
        # `agentType: implementer`, a defect declares `role: worker`, same model and
        # effort. The rollup then shows it twice: once clean with a denominator, once
        # unrated with the defect (audit probe, kimi 2026-07-31). Neither number is
        # wrong; together they LIE. There is no safe automatic merge (role is a real
        # dimension), so the split is REPORTED and the reader decides.
        by_me: dict[str, list[str]] = {}
        for key in seats:
            role, _, rest = key.partition("|")
            by_me.setdefault(rest, []).append(role)
        ambiguous = {f"{rest}": sorted(roles) for rest, roles in by_me.items() if len(roles) > 1}
        return {
            "rows": [seats[k] for k in sorted(seats)],
            "signals": signals,
            "ambiguousSeats": ambiguous,
            "excluded": {"malformedDefectRows": malformed,
                         "delegationsMissingEffort": no_effort,
                         "delegationsMissingCallerSeat": no_caller},
            "note": ("declared defects per RETAINED opportunity (the ledger is a 500-row "
                     "ring): not model accuracy, not a defect probability, not causal. "
                     "null = no retained denominator, never 0%. A rate CAN exceed 100 — "
                     "it is per-100-opportunities, not a percentage: 5 defects over 1 "
                     "accepted review reads 500.0 and that is the honest number. "
                     "`ambiguousSeats` = one model|effort seen under several role words, "
                     "so its numbers are SPLIT across keys and neither half is the whole. "
                     "`signals` are mechanical and deliberately UNATTRIBUTED. Info-only: "
                     "nothing consumes this."),
        }
    except Exception:
        return None


def _self_check() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    # never-crash on a bare root; corrupt ledger degrades to empty
    record_turn(tmp, engine="claude", model="sonnet", effort="low", chat_mode="auto",
                target=None, usage={"in_tokens": 1000, "out_tokens": 100,
                                    "cost_usd": 0.03, "duration_s": 2.0},
                reply_chars=350)
    (tmp / LEDGER_REL).parent.mkdir(parents=True, exist_ok=True)
    assert len(_load(tmp)) == 1
    for i in range(MAX_RECORDS + 20):
        record_turn(tmp, engine="claude", model="sonnet", effort=None, chat_mode="auto",
                    target="t1", usage={"cost_usd": 0.01 * (i % 3), "out_tokens": 100},
                    reply_chars=400)
    assert len(_load(tmp)) == MAX_RECORDS  # trim
    record_workflow(tmp, {"workflowId": "WF-X", "type": "map-reduce",
                          "tokenAudit": {"summary": {"totalPlannedTokens": 90000,
                                                     "totalObservedTokens": 70000}},
                          "roundHistory": [{"startedAt": "2026-07-10T10:00:00+00:00",
                                            "finishedAt": "2026-07-10T10:03:00+00:00"}],
                          "workers": [{"run": {"cpuSeconds": 1.5}},
                                      {"run": {"cpuSeconds": 0.5}}]}, "completed")
    summary = summarize(tmp)
    assert summary["workflows"]["count"] == 1
    assert summary["workflows"]["estTokens"]["median"] == 90000
    wf_rec = [r for r in _load(tmp) if r["kind"] == "workflow"][0]
    assert wf_rec["durationS"] == 180.0 and wf_rec["costUsd"] is None
    assert wf_rec["cpuS"] == 2.0 and wf_rec["workers"] == 2  # R28: tree CPU summed
    assert wf_rec["costBasis"] == "estimated"  # SPEC-118 v3: no observedUsage → estimate
    # SPEC-118 v3: stream-json workers surface real usage/cost → observed basis
    record_workflow(tmp, {"workflowId": "WF-OBS", "type": "fork-join",
                          "tokenAudit": {"summary": {"totalPlannedTokens": 5000}},
                          "workers": [{"run": {"observedUsage": {"inputTokens": 161, "outputTokens": 22, "costUsd": 0.0123}}},
                                      {"run": {"observedUsage": {"inputTokens": 100, "outputTokens": 10, "costUsd": 0.01}}}]}, "completed")
    obs_rec = [r for r in _load(tmp) if r.get("workflowId") == "WF-OBS"][0]
    assert obs_rec["costBasis"] == "observed" and obs_rec["costUsd"] == 0.0223
    assert obs_rec["observedTokens"] == 293 and obs_rec["estTokens"] == 5000  # est kept as fallback
    # R28 byStack: turns tagged target -> project stack; workflow untargeted -> harness
    stacks = summary["machine"]["byStack"]
    assert stacks["project"]["count"] > 0 and stacks["harness"]["count"] >= 1
    assert stacks["harness"]["cpuS"].get("total") == 2.0
    assert summary["machine"]["harnessDiskMB"][".harness"] >= 0
    # R27: the observer measures itself
    obs = summary["observability"]
    assert obs["ledgerBytes"] > 0 and isinstance(obs["summarizeMs"], float)
    # CPT-drift: the 500 turns above are all target-tagged, so self-only
    # calibration learns nothing from them — yet byTarget reporting still counts
    # every one (only the calibration sample set is filtered, not the groups).
    assert summary["estimator"]["observedCharsPerToken"] is None
    assert summary["estimator"]["cptSamples"] == 0
    assert summary["chatTurns"]["byTarget"]["t1"]["count"] >= MAX_RECORDS - 5
    assert summary["topExpensive"][0]["kind"] == "workflow"  # 90k est tokens tops $0.02 turns
    # delegations: real subagent spend visible in the ledger (R26/R27)
    record_delegation(tmp, model="opus", agent_type="implementer", tokens=215723,
                      tool_uses=82, duration_s=1448.0, task="SPEC-112 impl")
    summary = summarize(tmp)
    assert summary["delegations"]["count"] == 1
    assert summary["delegations"]["byModel"]["opus"]["total"] == 215723
    assert summary["delegations"]["series"] == [215723]  # TE.2 ordered series
    assert summary["delegations"]["latest"]["estTokens"] == 215723
    assert summary["topExpensive"][0]["kind"] == "delegation"  # 215k tops the 90k workflow
    # C6 §9.5-b cost-to-success: the lone delegation above carries no outcome, so
    # the corpus is empty -> both denominators null (never a divide-by-zero).
    cts = summary["delegations"]["costToSuccess"]
    assert cts["corpus"] == 0 and cts["ctsTokens"] is None and cts["costToUsefulOutcome"] is None, cts
    record_delegation(tmp, model="opus", agent_type="implementer", tokens=10000, outcome="kept")
    record_delegation(tmp, model="haiku", agent_type="implementer", tokens=5000, outcome="rejected")
    record_delegation(tmp, model="opus", agent_type="implementer", tokens=8000, outcome="reworked")
    cts = summarize(tmp)["delegations"]["costToSuccess"]
    assert cts["ctsTokens"] == 23000.0, cts             # sum 23000 / 1 kept
    assert cts["costToUsefulOutcome"] == 11500.0, cts   # sum 23000 / (kept+reworked)
    assert cts["byModelCTS"]["opus"]["ctsTokens"] == 18000.0, cts   # opus 18000 / 1 kept
    assert cts["byModelCTS"]["haiku"]["ctsTokens"] is None, cts     # haiku: no kept -> null
    (tmp / LEDGER_REL).write_text("{broken", encoding="utf-8")
    assert _load(tmp) == [] and summarize(tmp)["records"] == 0
    record_turn(tmp, engine="x", model=None, effort=None, chat_mode="auto",
                target=None, usage=None, reply_chars=0)  # over corrupt file: no crash
    # CPT-drift focused case: a self turn (ratio 3.0) calibrates; a target-tagged
    # turn (ratio 11.0 — inside the sane band, so only the target filter drops it)
    # is excluded from the calibration sample set yet STILL shows in byTarget.
    tmp2 = Path(tempfile.mkdtemp())
    record_turn(tmp2, engine="claude", model="sonnet", effort=None, chat_mode="auto",
                target=None, usage={"out_tokens": 100, "cost_usd": 0.01}, reply_chars=300)
    record_turn(tmp2, engine="claude", model="sonnet", effort=None, chat_mode="auto",
                target="other-repo", usage={"out_tokens": 100, "cost_usd": 0.02}, reply_chars=1100)
    s2 = summarize(tmp2)
    assert s2["estimator"]["cptSamples"] == 1  # only the self turn; target excluded
    assert s2["estimator"]["observedCharsPerToken"] == 3.0  # 300/100 (target 1100/100=11.0 grouped)
    # per-target-token-calibration: the target sample lands in its OWN bucket.
    assert s2["estimator"]["byTargetCpt"] == {"other-repo": {"samples": 1, "observed": 11.0}}, \
        s2["estimator"]["byTargetCpt"]
    assert "other-repo" in s2["chatTurns"]["byTarget"]  # reporting still counts the target turn
    # approval-service metrics (C5 / §7.7): derived read-only from the decide inbox +
    # intake/escalation ledgers. 2 pending (1 older than SLO) + 1 discarded + 1
    # resolved escalation; overrideRate/invalidatedCount stay OMITTED (🔬, not stored).
    tmp3 = Path(tempfile.mkdtemp())
    stamp = dt.datetime.now(dt.timezone.utc)
    old_at = (stamp - dt.timedelta(hours=decision_inbox.SLO_HOURS + 10)).isoformat()
    young_at = (stamp - dt.timedelta(hours=1)).isoformat()
    write_json(tmp3 / intake_queue.QUEUE_REL, {"schemaVersion": 1, "entries": [
        {"id": "aaaa11112222", "ask": "old pending", "source": "manual", "askedAt": old_at, "status": "pending"},
        {"id": "bbbb33334444", "ask": "young pending", "source": "manual", "askedAt": young_at, "status": "pending"},
        {"id": "cccc55556666", "ask": "already discarded", "source": "manual",
         "askedAt": young_at, "status": "discard", "decidedAt": young_at}]})
    write_json(tmp3 / ".harness" / "state" / "escalations.json",
               {"schemaVersion": "1.0", "raised": {}, "resolvedIds": ["ESC-9@t"],
                "resolvedRecords": {}, "frictionLog": []})
    ap = summarize(tmp3)["approvals"]
    assert ap["pending"] == 2 and ap["sloBreached"] == 1, ap
    assert ap["resolved"]["intakeByChoice"] == {"discard": 1}, ap
    assert ap["resolved"]["escalationsResolved"] == 1 and ap["resolved"]["total"] == 2, ap
    assert isinstance(ap["medianAgeHours"], float) and ap["expired"] == 0, ap
    assert "overrideRate" not in ap and "invalidatedCount" not in ap, ap  # 🔬 not persisted
    # ctx-medidor-custo: capsule_cost over a fixture workflow dir + the additive
    # record kwarg. 100 + 200 + 50 prompt/read tokens, 620 stdout chars / 3.1 = 200
    # -> numerator 550; 2 valid capsules -> 275.0 each.
    wf = Path(tempfile.mkdtemp())
    write_json(wf / "reduce" / "token-audit.json",
               {"estimator": {"charsPerToken": 3.1},
                "summary": {"totalWorkerPromptTokens": 100, "totalReducerPromptTokens": 50,
                            "requiredReads": {"digestPresent": True, "waveWithDigest": 200,
                                              "waveWithoutDigest": 900}}})
    write_json(wf / "reduce" / "validation.json", {"workflowId": "WF-CC", "workersValid": 2})
    (wf / "run-logs").mkdir(parents=True, exist_ok=True)
    (wf / "run-logs" / "worker-001.stdout.log").write_text("x" * 620, encoding="utf-8")
    cc = capsule_cost(wf)
    assert cc["numeratorTokens"] == {"workerPrompt": 100, "requiredReads": 200,
                                     "reducerPrompt": 50, "workerStdout": 200,
                                     "total": 550}, cc
    assert cc["costPerValidCapsuleTokens"] == 275.0 and cc["undefinedReason"] is None, cc
    # zero valid capsules: numerator kept, never a divide
    write_json(wf / "reduce" / "validation.json", {"workflowId": "WF-CC", "workersValid": 0})
    zero = capsule_cost(wf)
    assert zero["costPerValidCapsuleTokens"] is None and zero["numeratorTokens"]["total"] == 550
    assert zero["undefinedReason"] == "zero valid capsules", zero
    assert capsule_cost(Path(tempfile.mkdtemp())) is None  # no validation.json -> nothing to measure
    tmp4 = Path(tempfile.mkdtemp())
    record_workflow(tmp4, {"workflowId": "WF-CC", "type": "fork-join"}, "completed",
                    capsule_cost=cc)
    record_workflow(tmp4, {"workflowId": "WF-PLAIN", "type": "fork-join"}, "completed")
    by_id = {r.get("workflowId"): r for r in _load(tmp4)}
    assert by_id["WF-CC"]["capsuleCost"] == {"numeratorTokens": 550, "workersValid": 2,
                                             "costPerValidCapsuleTokens": 275.0}, by_id
    assert "capsuleCost" not in by_id["WF-PLAIN"], by_id  # absent kwarg adds no key
    # _append reports landed-or-not (was unconditional None): a write to a directory
    # where the ledger belongs fails and returns False, never raises.
    blocked = Path(tempfile.mkdtemp())
    (blocked / LEDGER_REL).parent.mkdir(parents=True, exist_ok=True)
    (blocked / LEDGER_REL).mkdir()  # a dir where the JSON file must go -> write fails
    assert _append(blocked, {"kind": "delegation"}) is False
    # defect-sink-lost-under-gate-hold: the OPERATOR writer refuses under a live hold
    # (the row would land on materialized HEAD and be discarded on release) and writes
    # nothing; automatic writers still swallow. Post-hold the write lands.
    held = Path(tempfile.mkdtemp())
    hold = held / ".harness" / "runs" / "gate-hold" / "h1"
    hold.mkdir(parents=True)
    (hold / "hold.json").write_text("{}", encoding="utf-8")
    # The in-gate env is POPPED for the refusal arm: this self-check can run inside a
    # gate scenario with HARNESS_SCENARIO_ISOLATED=1, the exact value the exemption
    # keys on (same idiom as scenario_isolation._selfcheck's guard block).
    saved_iso = os.environ.pop("HARNESS_SCENARIO_ISOLATED", None)
    try:
        assert record_delegation(held, model="opus", agent_type="implementer", tokens=100) is False
        assert not (held / LEDGER_REL).exists(), "held delegation must not write the ledger"
        # ...and the in-gate arm: the same hold ACCEPTS the row, because the scenario's
        # own snapshot/restore is the isolation contract there.
        os.environ["HARNESS_SCENARIO_ISOLATED"] = "1"
        assert record_delegation(held, model="opus", agent_type="implementer", tokens=100) is True, \
            "in-gate delegation must land under a hold"
        assert len([r for r in _load(held) if r["kind"] == "delegation"]) == 1
    finally:
        if saved_iso is None:
            os.environ.pop("HARNESS_SCENARIO_ISOLATED", None)
        else:
            os.environ["HARNESS_SCENARIO_ISOLATED"] = saved_iso
    import shutil
    shutil.rmtree(held / ".harness" / "runs" / "gate-hold")
    assert record_delegation(held, model="opus", agent_type="implementer", tokens=100) is True
    assert len([r for r in _load(held) if r["kind"] == "delegation"]) == 2  # + the in-gate row
    # D1.3 interactive-overseer seat: with no HARNESS_SESSION_* splice, `self` reads
    # the live seat the SessionStart hook wrote — but ONLY when its sessionId names
    # THIS session, so a foreign/stale file can never mis-attribute the caller.
    sroot = Path(tempfile.mkdtemp())
    (sroot / ".harness" / "state").mkdir(parents=True)
    write_json(sroot / ".harness" / "state" / "session-seat.json",
               {"role": "overseer", "model": "claude-opus-4-8", "effort": "xhigh",
                "sessionId": "sess-A"})
    mine = {"CLAUDE_CODE_SESSION_ID": "sess-A"}
    assert seat_from_env(mine, sroot) == {"role": "overseer", "model": "claude-opus-4-8",
                                          "effort": "xhigh"}, "same-session file seat"
    assert seat_from_env({"CLAUDE_CODE_SESSION_ID": "sess-B"}, sroot) is None, "foreign file refused"
    assert seat_from_env({}, sroot) is None, "no session id -> no file seat"
    # a complete env triplet wins; the file is never consulted
    env_seat = {"HARNESS_SESSION_ROLE": "planner", "HARNESS_SESSION_MODEL": "fable",
                "HARNESS_SESSION_EFFORT": "high", "CLAUDE_CODE_SESSION_ID": "sess-A"}
    assert seat_from_env(env_seat, sroot)["role"] == "planner", "env triplet wins over file"
    print("cost_metrics self-check ok")


if __name__ == "__main__":
    _self_check()
