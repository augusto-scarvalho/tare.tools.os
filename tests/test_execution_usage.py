"""Unit tests for pure bounded session usage aggregation."""
from __future__ import annotations

import pytest

from relay.execution_usage import aggregate_sessions


def _make_call(
    call_id: str = "call-1",
    purpose: str = "model",
    retry_of: str | None = None,
    execution: str = "possibly_executed",
    input_tokens: int | None = 100,
    output_tokens: int | None = 50,
    reasoning_tokens: int | None = 10,
    currency: str | None = "USD",
    amount: str | None = "0.001500",
    complete: bool = True,
) -> dict:
    """Helper creating a realistic tare-native-usage/1 call fragment."""
    tokens = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": input_tokens,
        "cache_read_tokens": 0 if input_tokens is not None else None,
        "cache_write_tokens": 0 if input_tokens is not None else None,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (input_tokens + output_tokens)
        if input_tokens is not None and output_tokens is not None
        else None,
    }
    return {
        "call_id": call_id,
        "purpose": purpose,
        "retry_of": retry_of,
        "effect_ids": [],
        "execution": execution,
        "usage_source": "observation",
        "profile_digest": "sha256-profile-abc",
        "observation": {"observation_id": f"obs-{call_id}", "purpose": purpose},
        "raw_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "normalized": {
            "tokens": tokens,
            "issues": [],
            "contract": {"layout": "chat-inclusive", "source": "test-adapter"},
        },
        "provider_timing": {"observed_operation_ms": 120},
        "cost": {
            "currency": currency,
            "known_amount": amount,
            "complete": complete,
            "unpriced_categories": [] if complete else ["reasoning_tokens"],
            "tariff": None,
        },
    }


def _make_usage(session_id: str, calls: list[dict]) -> dict:
    """Helper creating a realistic tare-native-usage/1 report."""
    return {
        "schema": "tare-native-usage/1",
        "session_id": session_id,
        "binding": "test-binding",
        "calls": calls,
        "counts": [],
        "tokens": {},
        "known_cost_by_currency": {},
        "unpriced_or_partial_calls": 0,
        "coverage": {"generation_intents": len(calls), "not_started": 0},
        "timing": {"observed_operation_ms": 100},
        "scope": "model token charges only",
    }


def test_overlapping_snapshots_deduplication():
    call_1 = _make_call("call-1", input_tokens=100, output_tokens=50, amount="0.0010")
    call_2 = _make_call("call-2", input_tokens=200, output_tokens=80, amount="0.0020")

    # Snapshot 1 has call-1; Snapshot 2 has call-1 (repeated identically) and call-2
    usage_snap_1 = _make_usage("sess-1", [call_1])
    usage_snap_2 = _make_usage("sess-1", [call_1, call_2])

    entries = [
        {"stage": "plan", "source": "codex", "session_id": "sess-1", "usage": usage_snap_1, "outcome": None},
        {"stage": "implement", "source": "codex", "session_id": "sess-1", "usage": usage_snap_2, "outcome": None},
    ]

    result = aggregate_sessions(entries)

    # Overlapping session totals must not double-count call-1
    assert result["generation_intents"] == 2
    assert result["tokens"]["input_tokens"]["known_sum"] == 300
    assert result["tokens"]["output_tokens"]["known_sum"] == 130
    assert result["known_cost_by_currency"] == {"USD": "0.0030"}

    # Unique attributions
    assert len(result["attributions"]) == 2
    assert result["attributions"] == [
        {"session_id": "sess-1", "stage": "implement", "source": "codex"},
        {"session_id": "sess-1", "stage": "plan", "source": "codex"},
    ]


def test_conflicting_duplicate_raises_value_error():
    call_a = _make_call("call-1", input_tokens=100)
    call_b = _make_call("call-1", input_tokens=999)  # Conflicting token count

    entry_1 = {"stage": "stage-1", "source": "agent", "session_id": "sess-conflict", "usage": _make_usage("sess-conflict", [call_a]), "outcome": None}
    entry_2 = {"stage": "stage-2", "source": "agent", "session_id": "sess-conflict", "usage": _make_usage("sess-conflict", [call_b]), "outcome": None}

    with pytest.raises(ValueError, match="conflicting duplicate call record"):
        aggregate_sessions([entry_1, entry_2])


def test_missing_report_and_unknown_counters():
    # Call with unknown reasoning tokens and unpriced tariff
    call_with_unknowns = _make_call(
        "call-unk",
        input_tokens=150,
        output_tokens=75,
        reasoning_tokens=None,
        currency=None,
        amount=None,
        complete=False,
    )

    entries = [
        {"stage": "plan", "source": "planner", "session_id": "sess-missing-1", "usage": None, "outcome": None},
        {"stage": "audit", "source": "auditor", "session_id": "sess-missing-2", "usage": None, "outcome": None},
        {"stage": "exec", "source": "worker", "session_id": "sess-active", "usage": _make_usage("sess-active", [call_with_unknowns]), "outcome": None},
    ]

    result = aggregate_sessions(entries)

    # Missing usage reports are recorded as unknown_session_ids
    assert result["unknown_session_ids"] == ["sess-missing-1", "sess-missing-2"]
    # Does not imply zero measured calls overall (active session calls are measured)
    assert result["generation_intents"] == 1

    # Unknown counters are properly tracked
    assert result["tokens"]["reasoning_tokens"]["unknown_calls"] == 1
    assert result["tokens"]["reasoning_tokens"]["known_sum"] == 0
    assert result["tokens"]["input_tokens"]["unknown_calls"] == 0
    assert result["tokens"]["input_tokens"]["known_sum"] == 150

    # Unknown currency/amount is never zero-billed
    assert result["known_cost_by_currency"] == {}
    assert result["unpriced_or_partial_calls"] == 1


def test_currencies_multi_currency_and_decimal_precision():
    call_usd_1 = _make_call("c-usd-1", currency="USD", amount="0.001500")
    call_usd_2 = _make_call("c-usd-2", currency="USD", amount="0.000500")
    call_eur_1 = _make_call("c-eur-1", currency="EUR", amount="0.010000")

    entries = [
        {"stage": "stage-a", "source": "agent-a", "session_id": "sess-a", "usage": _make_usage("sess-a", [call_usd_1, call_eur_1]), "outcome": None},
        {"stage": "stage-b", "source": "agent-b", "session_id": "sess-b", "usage": _make_usage("sess-b", [call_usd_2]), "outcome": None},
    ]

    result = aggregate_sessions(entries)

    # Exact Decimal addition without float drift, sorted deterministically
    assert result["known_cost_by_currency"] == {
        "EUR": "0.010000",
        "USD": "0.002000",
    }
    assert list(result["known_cost_by_currency"].keys()) == ["EUR", "USD"]


def test_summary_retry_not_started_intents_and_token_exclusion():
    c_model = _make_call("c1", purpose="model", retry_of=None, execution="possibly_executed", input_tokens=100, amount="0.001")
    c_summary = _make_call("c2", purpose="summary", retry_of=None, execution="possibly_executed", input_tokens=50, amount="0.0005")
    c_retry = _make_call("c3", purpose="model", retry_of="c1", execution="possibly_executed", input_tokens=80, amount="0.0008")
    c_not_started = _make_call("c4", purpose="model", retry_of=None, execution="not_started", input_tokens=9999, amount="0.9999")

    usage = _make_usage("sess-intents", [c_model, c_summary, c_retry, c_not_started])
    entries = [
        {"stage": "workflow", "source": "agent", "session_id": "sess-intents", "usage": usage, "outcome": None}
    ]

    result = aggregate_sessions(entries)

    assert result["generation_intents"] == 4
    assert result["summary_intents"] == 1
    assert result["retry_intents"] == 1
    assert result["not_started"] == 1

    # not_started tokens and cost are excluded from totals
    assert result["tokens"]["input_tokens"]["known_sum"] == 100 + 50 + 80
    assert result["known_cost_by_currency"] == {"USD": "0.0023"}


def test_session_mismatch_and_malformed_identities():
    # Report session mismatch
    mismatch_usage = _make_usage("sess-WRONG", [_make_call("c1")])
    with pytest.raises(ValueError, match="report session mismatch"):
        aggregate_sessions([
            {"stage": "exec", "source": "agent", "session_id": "sess-EXPECTED", "usage": mismatch_usage, "outcome": None}
        ])

    # Malformed session_id
    with pytest.raises(ValueError, match="entry session_id"):
        aggregate_sessions([
            {"stage": "exec", "source": "agent", "session_id": "", "usage": None, "outcome": None}
        ])

    # Malformed stage
    with pytest.raises(ValueError, match="entry stage"):
        aggregate_sessions([
            {"stage": "", "source": "agent", "session_id": "s1", "usage": None, "outcome": None}
        ])

    # Malformed source
    with pytest.raises(ValueError, match="entry source"):
        aggregate_sessions([
            {"stage": "exec", "source": "   ", "session_id": "s1", "usage": None, "outcome": None}
        ])

    # Malformed call_id
    bad_call = _make_call("   ")
    with pytest.raises(ValueError, match="call_id"):
        aggregate_sessions([
            {"stage": "exec", "source": "agent", "session_id": "s1", "usage": _make_usage("s1", [bad_call]), "outcome": None}
        ])


def test_proposed_vs_verified_distinction():
    entries = [
        {
            "stage": "implement",
            "source": "implementer",
            "session_id": "s-impl",
            "usage": None,
            "outcome": {"status": "PROPOSED", "reason": "diff written", "accepted": True},
        },
        {
            "stage": "audit",
            "source": "auditor",
            "session_id": "s-audit",
            "usage": None,
            "outcome": {"status": "VERIFIED", "reason": "adversarial test passed"},
        },
        {
            "stage": "admit",
            "source": "policy",
            "session_id": "s-admit",
            "usage": None,
            "outcome": {"status": "CANDIDATE_ACCEPTED", "reason": "clean land"},
        },
        {
            "stage": "check",
            "source": "ci",
            "session_id": "s-check",
            "usage": None,
            "outcome": {"status": "FAILED", "reason": "linter error"},
        },
    ]

    result = aggregate_sessions(entries)
    outcomes = {o["session_id"]: o for o in result["outcomes"]}

    # PROPOSED never means accepted, even if caller set accepted: True
    impl_out = outcomes["s-impl"]
    assert impl_out["status"] == "PROPOSED"
    assert impl_out["accepted"] is None
    assert impl_out["verified"] is None
    assert impl_out["reason"] == "diff written"

    # Outcome labels alone do not establish independent verification
    audit_out = outcomes["s-audit"]
    assert audit_out["status"] == "VERIFIED"
    assert audit_out["accepted"] is None
    assert audit_out["verified"] is None
    assert audit_out["reason"] == "adversarial test passed"

    # Candidate labels alone do not establish independent verification
    admit_out = outcomes["s-admit"]
    assert admit_out["status"] == "CANDIDATE_ACCEPTED"
    assert admit_out["accepted"] is None
    assert admit_out["verified"] is None

    # FAILED is neither
    check_out = outcomes["s-check"]
    assert check_out["status"] == "FAILED"
    assert check_out["accepted"] is None
    assert check_out["verified"] is None


def test_deterministic_ordering_and_no_payload_copies():
    call = _make_call("c1", input_tokens=100)
    usage = _make_usage("s1", [call])
    entries = [
        {"stage": "stg2", "source": "src2", "session_id": "s2", "usage": None, "outcome": None},
        {"stage": "stg1", "source": "src1", "session_id": "s1", "usage": usage, "outcome": {"status": "PASS"}},
    ]

    result = aggregate_sessions(entries)

    # No raw calls or payload copies in output
    assert "calls" not in result
    assert "payload" not in result

    # Ordering of attributions
    assert result["attributions"][0]["session_id"] == "s1"
    assert result["attributions"][1]["session_id"] == "s2"

    # Tokens dictionary contains all canonical fields in order
    expected_fields = [
        "input_tokens",
        "uncached_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    ]
    assert list(result["tokens"].keys()) == expected_fields
