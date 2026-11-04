"""Exact host-recorded call claims, never inferred Work/session ownership."""
from copy import deepcopy
import json

import pytest

from scripts.round_table.report_round_table_telemetry import collect, link_work_calls, row
from test_inference_telemetry import save
from test_native_work_telemetry import native_call, usage, work


def claim(call_id="session/model/1", stage="implement", session_id="session"):
    return {"session_id": session_id, "call_id": call_id, "stage": stage}


def runs_by_id(report):
    return {r["run_id"]: r for r in report["work_runs"]}


def test_failed_and_resumed_work_own_distinct_calls_from_cumulative_session_usage(tmp_path):
    first, second = native_call(), native_call("session/model/2")
    original = work("origin", inference_calls=[claim()])
    resumed = work("resume", resumed_from="origin", status="SETTLED", landing={"landed": True},
                   inference_calls=[claim("session/model/2")])
    save(tmp_path / "original/run.json", original)
    save(tmp_path / "resumed/run.json", resumed)
    save(tmp_path / "before/usage.json", usage(first))
    save(tmp_path / "after/usage.json", usage(first, second))
    save(tmp_path / "copy/usage.json", usage(first, second))
    save(tmp_path / "copy/run.json", resumed)
    report = collect([tmp_path, tmp_path / "after/usage.json"])
    runs = runs_by_id(report)
    assert report["summary"]["calls"] == 2
    assert report["summary"]["input_tokens"] == 200
    assert report["summary"]["estimated_cost_usd"] == "0.50"
    assert runs["origin"]["status"] == "FAILED"
    assert runs["resume"]["status"] == "SETTLED"
    for run in runs.values():
        assert run["inference_link"] == "recorded"
        assert run["inference_usage"]["calls"] == 1
        assert run["inference_usage"]["input_tokens"] == 100
        assert run["inference_usage"]["estimated_cost_usd"] == "0.25"
        assert run["execution_inference_usage"]["calls"] == 2
        assert run["execution_inference_usage"]["estimated_cost_usd"] == "0.50"
    linked = {r["call_id"]: r for r in report["rows"]}
    assert linked["session/model/1"]["work_run_id"] == "origin"
    assert linked["session/model/2"]["work_run_id"] == "resume"
    assert linked["session/model/1"]["work_stage"] == "implement"
    assert not report["warnings"]


def test_unpriced_native_call_remains_unknown_beside_priced_call(tmp_path):
    calls = [native_call(), native_call("session/model/2", cost={"complete": False})]
    save(tmp_path / "usage.json", usage(*calls))
    save(tmp_path / "run.json", work("run", inference_calls=[claim(), claim("session/model/2")]))
    result = collect([tmp_path])["work_runs"][0]["inference_usage"]
    assert result["calls"] == 2
    assert result["cost_records"] == 1
    assert result["unknown_cost_calls"] == 1
    assert result["estimated_cost_usd"] == "0.25"
    assert result["reported_cost_usd"] is None


def test_attribution_preserves_reported_vs_estimated_cost_provenance(tmp_path):
    save(tmp_path / "run.json", work("run", inference_calls=[claim(), claim("session/model/2")]))
    report = collect([tmp_path])
    records = [row("native-usage:session:session/model/1", "session", "native_usage", "model",
                   reported_cost_usd="0.1", estimated_cost_usd="0.9"),
               row("native-usage:session:session/model/2", "session", "native_usage", "model",
                   estimated_cost_usd="0.2")]
    warnings = []
    link_work_calls(records, report["work_runs"], warnings)
    result = report["work_runs"][0]["inference_usage"]
    assert result["reported_cost_usd"] == "0.1"
    assert result["estimated_cost_usd"] == "0.2"
    assert result["cost_records"] == 2
    assert not warnings


def test_missing_usage_has_one_unknown_call_without_fabricated_zero_usage(tmp_path):
    saved = work("run", inference_calls=[claim(), claim()])
    save(tmp_path / "original/run.json", saved)
    save(tmp_path / "copy/run.json", saved)
    report = collect([tmp_path])
    item, = report["rows"]
    run, = report["work_runs"]
    assert item["usage_receipt_missing"] is True
    assert item["work_run_id"] == "run"
    assert item["status"] == "UNKNOWN"
    assert item["input_tokens"] is item["output_tokens"] is None
    assert run["inference_usage"]["calls"] == 1
    assert run["inference_usage"]["usage_receipts_missing"] == 1
    assert run["inference_usage"]["input_tokens"] is None
    assert run["inference_usage"]["estimated_cost_usd"] is None
    assert run["inference_usage"]["unknown_cost_calls"] == 1


@pytest.mark.parametrize("different_run", [False, True])
def test_duplicate_call_ownership_across_runs_or_stages_attributes_neither(tmp_path, different_run):
    save(tmp_path / "usage.json", usage(native_call()))
    if different_run:
        save(tmp_path / "a/run.json", work("a", inference_calls=[claim()]))
        save(tmp_path / "b/run.json", work("b", inference_calls=[claim()]))
    else:
        save(tmp_path / "a/run.json", work("a", inference_calls=[claim(), claim(stage="audit")]))
    report = collect([tmp_path])
    item, = report["rows"]
    assert item["work_link"] == "contradictory"
    assert not item.get("work_run_id")
    assert report["summary"]["calls"] == 1
    for run in report["work_runs"]:
        assert run["inference_conflicts"]
        assert run["inference_usage"]["calls"] == 0
        assert run["inference_usage"]["estimated_cost_usd"] is None
    assert report["warnings"]


def test_native_work_binding_mismatch_refuses_explicit_owner_claim(tmp_path):
    native = usage(native_call())
    native["binding"]["work_id"] = "different-work"
    save(tmp_path / "usage.json", native)
    save(tmp_path / "run.json", work("run", inference_calls=[claim()]))
    report = collect([tmp_path])
    assert report["rows"][0]["work_link"] == "contradictory"
    assert not report["rows"][0].get("work_run_id")
    assert report["work_runs"][0]["inference_usage"]["calls"] == 0
    assert report["warnings"]


@pytest.mark.parametrize("malformed", [
    "PRIVATE", [{"session_id": "session", "call_id": "session/model/1"}],
    [claim(stage="")], [claim(session_id={"secret": "PRIVATE"})],
    [claim(call_id=["PRIVATE"])], [None],
])
def test_malformed_claim_does_not_crash_or_expose_payload(tmp_path, malformed):
    save(tmp_path / "invalid/run.json", work("invalid", inference_calls=malformed))
    save(tmp_path / "valid/run.json", work("valid"))
    save(tmp_path / "usage.json", usage(native_call()))
    report = collect([tmp_path])
    assert report["warnings"]
    assert "valid" in runs_by_id(report)
    assert not any(r.get("work_run_id") == "invalid" for r in report["rows"])
    assert "PRIVATE" not in json.dumps(report)


def test_legacy_receipt_never_guesses_ownership_from_matching_work_and_session(tmp_path):
    native = usage(native_call())
    native["binding"]["work_id"] = "work-a"
    save(tmp_path / "usage.json", native)
    save(tmp_path / "run.json", work("session", session_id="session"))
    report = collect([tmp_path])
    assert report["work_runs"][0]["inference_link"] == "unknown"
    assert report["work_runs"][0]["inference_usage"]["calls"] == 0
    assert not report["rows"][0].get("work_run_id")


def test_contradictory_run_does_not_erase_competing_call_ownership_evidence(tmp_path):
    save(tmp_path / "a/run.json", work("bad", inference_calls=[claim()]))
    save(tmp_path / "b/run.json", work("bad", status="CANCELLED", inference_calls=[claim()]))
    save(tmp_path / "c/run.json", work("other", inference_calls=[claim()]))
    save(tmp_path / "usage.json", usage(native_call()))
    report = collect([tmp_path])
    assert runs_by_id(report)["bad"]["status"] == "CONTRADICTORY"
    assert not report["rows"][0].get("work_run_id")
    assert runs_by_id(report)["other"]["inference_usage"]["calls"] == 0
    assert report["warnings"]


def test_conflicting_native_usage_cannot_be_attributed_as_known_call_cost(tmp_path):
    first = native_call()
    conflicting = deepcopy(first)
    conflicting["cost"]["known_amount"] = "10"
    save(tmp_path / "a/usage.json", usage(first))
    save(tmp_path / "b/usage.json", usage(conflicting))
    save(tmp_path / "run.json", work("run", inference_calls=[claim()]))
    report = collect([tmp_path])
    assert report["rows"][0]["status"] == "CONTRADICTORY"
    assert not report["rows"][0].get("work_run_id")
    assert report["work_runs"][0]["inference_usage"]["cost_records"] == 0


def test_observation_failure_keeps_partial_call_attribution_explicit(tmp_path):
    save(tmp_path / "run.json", work("run", inference_calls=[claim()], inference_observation_failures=1))
    save(tmp_path / "usage.json", usage(native_call()))
    run, = collect([tmp_path])["work_runs"]
    assert run["inference_link"] == "partial"
    assert run["inference_usage"]["calls"] == 1
    assert run["inference_observation_failures"] == 1
