"""Execution usage aggregation across native sessions and stages.

Pure bounded derivation over tare-native-usage/1 reports without I/O or dependencies.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _aggregate(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate native session execution entries across stages and sources."""
    if not hasattr(entries, "__iter__"):
        raise ValueError("entries must be an iterable")

    seen_calls: dict[tuple[str, str], dict[str, Any]] = {}
    seen_attributions: set[tuple[str, str, str]] = set()
    seen_outcomes: set[tuple[str, str, str | None, str | None, bool, bool]] = set()
    unknown_sessions: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("entry must be a dict")

        session_id = entry.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("entry session_id must be a non-empty string")

        stage = entry.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("entry stage must be a non-empty string")

        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("entry source must be a non-empty string")

        seen_attributions.add((session_id, stage, source))

        outcome = entry.get("outcome")
        if outcome is not None:
            if not isinstance(outcome, dict):
                raise ValueError("entry outcome must be a dict or None")
            raw_status = outcome.get("status")
            if raw_status is None and isinstance(outcome.get("outcome"), str):
                raw_status = outcome["outcome"]
            status = str(raw_status) if raw_status is not None else None

            raw_reason = outcome.get("reason")
            if raw_reason is None and isinstance(outcome.get("outcome"), dict):
                raw_reason = outcome["outcome"].get("reason")
            reason = str(raw_reason) if raw_reason is not None else None

            # Saved model outcomes cannot establish independent acceptance.
            accepted = verified = None

            seen_outcomes.add((session_id, stage, status, reason, accepted, verified))

        usage = entry.get("usage")
        if usage is None:
            unknown_sessions.add(session_id)
            continue

        if not isinstance(usage, dict):
            raise ValueError("entry usage must be a dict or None")

        report_session = usage.get("session_id")
        if not isinstance(report_session, str) or not report_session.strip():
            raise ValueError("usage report session_id must be a non-empty string")
        if report_session != session_id:
            raise ValueError(f"report session mismatch: {report_session!r} != {session_id!r}")

        if usage.get("schema") != "tare-native-usage/1":
            raise ValueError("unsupported usage schema")
        calls = usage.get("calls")
        if not isinstance(calls, list):
            raise ValueError("usage calls must be a list")

        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("call record must be a dict")
            call_id = call.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("call_id must be a non-empty string")

            identity = (session_id, call_id)
            if identity in seen_calls:
                if seen_calls[identity] != call:
                    raise ValueError(f"conflicting duplicate call record for {identity}")
            else:
                seen_calls[identity] = call

    unique_calls = [seen_calls[k] for k in sorted(seen_calls)]

    gen_intents = len(unique_calls)
    summary_intents = sum(1 for c in unique_calls if c.get("purpose") == "summary")
    retry_intents = sum(1 for c in unique_calls if c.get("retry_of") is not None)
    not_started = sum(1 for c in unique_calls if c.get("execution") == "not_started")

    active_calls = [c for c in unique_calls if c.get("execution") != "not_started"]

    tokens: dict[str, dict[str, int]] = {}
    for field in FIELDS:
        known_sum = 0
        unknown_calls = 0
        for c in active_calls:
            norm = c.get("normalized")
            val = None
            if isinstance(norm, dict):
                toks = norm.get("tokens")
                if isinstance(toks, dict):
                    raw_val = toks.get(field)
                    if type(raw_val) is int and raw_val >= 0:
                        val = raw_val
            if val is not None:
                known_sum += val
            else:
                unknown_calls += 1
        tokens[field] = {"known_sum": known_sum, "unknown_calls": unknown_calls}

    currency_sums: dict[str, Decimal] = {}
    unpriced_or_partial_calls = 0
    for c in active_calls:
        cost = c.get("cost")
        if not isinstance(cost, dict):
            unpriced_or_partial_calls += 1
            continue

        curr = cost.get("currency")
        amt_str = cost.get("known_amount")
        complete = cost.get("complete") is True

        if isinstance(curr, str) and curr.strip() and amt_str is not None:
            try:
                amt = Decimal(str(amt_str))
                if amt.is_finite() and amt >= 0:
                    key = curr.strip()
                    currency_sums[key] = currency_sums.get(key, Decimal(0)) + amt
                else:
                    complete = False
            except (InvalidOperation, TypeError, ValueError):
                complete = False
        else:
            complete = False

        if not complete:
            unpriced_or_partial_calls += 1

    known_cost_by_currency = {k: str(currency_sums[k]) for k in sorted(currency_sums)}

    attributions = [
        {"session_id": s, "stage": stg, "source": src}
        for s, stg, src in sorted(seen_attributions)
    ]
    outcomes = [
        {
            "session_id": s,
            "stage": stg,
            "status": st,
            "reason": r,
            "accepted": acc,
            "verified": ver,
        }
        for s, stg, st, r, acc, ver in sorted(
            seen_outcomes, key=lambda x: (x[0], x[1], str(x[2]), str(x[3]))
        )
    ]

    return {
        "schema": "tare-execution-usage/1",
        "generation_intents": gen_intents,
        "summary_intents": summary_intents,
        "retry_intents": retry_intents,
        "not_started": not_started,
        "coverage": {
            "generation_intents": gen_intents,
            "summary_intents": summary_intents,
            "retry_intents": retry_intents,
            "not_started": not_started,
        },
        "tokens": tokens,
        "known_cost_by_currency": known_cost_by_currency,
        "unpriced_or_partial_calls": unpriced_or_partial_calls,
        "unknown_session_ids": sorted(unknown_sessions),
        "attributions": attributions,
        "outcomes": outcomes,
    }


def aggregate_sessions(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Deduplicate cumulative snapshots; stage rows are views, never additive totals."""
    entries = list(entries)
    result = _aggregate(entries)
    groups = sorted({(e['stage'], e['session_id']) for e in entries})
    result['stages'] = [
        {'stage': stage, 'session_id': session_id,
         **_aggregate(e for e in entries if (e['stage'], e['session_id']) == (stage, session_id))}
        for stage, session_id in groups]
    result['scope'] = 'Observed model token charges; excludes infrastructure, tools and counting fees. Stage views may overlap.'
    return result
