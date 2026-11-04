"""Offline native usage and Work outcome receipts remain distinct evidence."""
from copy import deepcopy
import json
import re

import pytest

from scripts.round_table.report_round_table_telemetry import collect, render_html, terminal
from test_inference_telemetry import save


def native_call(identity="session/model/1", **changes):
    value = {
        "call_id": identity,
        "execution": "possibly_executed",
        "normalized": {"tokens": {
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_tokens": 80, "cache_write_tokens": 0}},
        "observation": {"status": "complete", "requested_model": "requested",
                        "observed_model": "observed", "duration_ms": 12},
        "cost": {"known_amount": "0.25", "currency": "USD", "complete": True},
        "usage_source": "observation",
    }
    value.update(changes)
    return value


def usage(*calls):
    return {"schema": "tare-native-usage/1", "session_id": "session",
            "binding": {"work_id": "session:binding"}, "calls": list(calls),
            # These aggregates and counting operations must not become calls.
            "tokens": {"input_tokens": {"known_sum": 9999}},
            "counts": [{"input_tokens": 9999}],
            "known_cost_by_currency": {"USD": "9999"}}


def work(run_id, **changes):
    value = {"schema": "tare.tools/work-run/1", "run_id": run_id,
             "work_id": "work-a", "train_id": "train-a", "status": "FAILED",
             "stage": "implement", "started_at": "2026-09-21T12:00:00+00:00",
             "ended_at": "2026-09-21T12:00:01+00:00", "duration_ms": 1000,
             "candidate_commit": None, "admission": None, "landing": None}
    value.update(changes)
    return value


def test_native_usage_projects_calls_not_counting_operations_or_aggregates(tmp_path):
    first = native_call()
    first["observation"].update(prompt="PRIVATE PROMPT", usage={"secret": "PRIVATE USAGE"},
                                transport_calls=[{"endpoint": "PRIVATE ENDPOINT"}])
    save(tmp_path / "usage.json", usage(first, native_call("session/model/2", normalized={
        "tokens": {}}, cost={"complete": False}, observation={
            "requested_model": "requested", "status": "unknown", "error": "HTTP_INCOMPLETE_STREAM"})))
    report = collect([tmp_path])
    assert report["summary"]["calls"] == 2
    assert report["summary"]["aggregated_executions"] == 0
    assert report["summary"]["input_tokens"] == 100
    assert report["summary"]["input_records"] == 1
    assert report["summary"]["estimated_cost_usd"] == "0.25"
    assert report["summary"]["reported_cost_usd"] is None
    assert report["summary"]["errors"] == 1
    complete = next(r for r in report["rows"] if r["input_tokens"] is not None)
    assert complete["model"] == complete["observed_model"] == "observed"
    assert complete["requested_model"] == "requested"
    assert complete["group"] == "session"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("cost", [
    {"known_amount": "10", "currency": "EUR", "complete": True},
    {"known_amount": "0.25", "currency": "USD", "complete": False},
    {"known_amount": "NaN", "currency": "USD", "complete": True},
    {},
])
def test_native_unknown_partial_and_non_usd_costs_are_not_reported_as_usd(tmp_path, cost):
    save(tmp_path / "usage.json", usage(native_call(cost=cost)))
    report = collect([tmp_path])
    assert report["summary"]["cost_records"] == 0
    assert report["summary"]["estimated_cost_usd"] is None
    assert report["summary"]["reported_cost_usd"] is None


def test_copied_native_snapshots_deduplicate_independent_of_receipt_path(tmp_path):
    first, second = native_call(), native_call("session/model/2")
    save(tmp_path / "before/usage.json", usage(first))
    save(tmp_path / "after/usage.json", usage(first, second))
    save(tmp_path / "copy/usage.json", usage(first, second))
    report = collect([tmp_path, tmp_path / "copy/usage.json"])
    assert report["summary"]["calls"] == 2
    assert report["summary"]["input_tokens"] == 200
    assert report["summary"]["estimated_cost_usd"] == "0.50"
    assert not report["warnings"]


def test_conflicting_native_call_does_not_keep_cache_hits_or_cost(tmp_path):
    first = native_call()
    different = deepcopy(first)
    different["normalized"]["tokens"]["input_tokens"] = 300
    save(tmp_path / "a/usage.json", usage(first))
    save(tmp_path / "b/usage.json", usage(different))
    save(tmp_path / "c/usage.json", usage(first))
    report = collect([tmp_path])
    result, = report["rows"]
    assert result["status"] == "CONTRADICTORY"
    assert result["input_tokens"] is result["output_tokens"] is None
    assert result["estimated_cost_usd"] is None
    assert result["cache_status"] == "UNKNOWN"
    assert report["summary"]["cache_hits"] == report["summary"]["cache_misses"] == 0
    assert report["warnings"]


@pytest.mark.parametrize("where", ["binding", "calls", "normalized", "tokens", "observation", "cost"])
def test_malformed_nested_native_data_warns_and_preserves_other_sources(tmp_path, where):
    invalid = usage(native_call())
    if where in {"binding", "calls"}:
        invalid[where] = {"malformed": "PRIVATE"} if where == "calls" else ["PRIVATE"]
    elif where == "tokens":
        invalid["calls"][0]["normalized"]["tokens"] = ["PRIVATE"]
    else:
        invalid["calls"][0][where] = ["PRIVATE"]
    save(tmp_path / "invalid/usage.json", invalid)
    save(tmp_path / "valid/usage.json", usage(native_call("other/model/1")))
    report = collect([tmp_path])
    assert report["warnings"]
    assert any(r["id"].endswith("other/model/1") for r in report["rows"])
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("initial_status", ["FAILED", "CANCELLED"])
def test_work_resume_preserves_original_outcome_and_confirms_only_landed_attempt(tmp_path, initial_status):
    origin = work("origin", status=initial_status)
    resumed = work("resume", resumed_from="origin", status="SETTLED", stage="landing",
                   admission={"verdict": "ADMITTED"}, landing={"landed": True})
    save(tmp_path / "a/run.json", origin)
    save(tmp_path / "b/run.json", resumed)
    save(tmp_path / "copy/run.json", resumed)
    report = collect([tmp_path])
    runs = {r["run_id"]: r for r in report["work_runs"]}
    assert runs["origin"]["status"] == initial_status
    assert not runs["origin"]["settlement_confirmed"]
    assert runs["resume"]["settlement_confirmed"]
    assert runs["resume"]["admitted"] is True
    assert runs["resume"]["lineage"] == "resolved"
    assert runs["resume"]["resume_origin_run_id"] == "origin"
    assert runs["origin"]["execution_settled"] is True
    assert runs["origin"]["settled_run_ids"] == ["resume"]
    assert report["work_run_summary"]["attempts"] == 2
    assert report["work_run_summary"]["settled_and_landed"] == 1
    assert report["summary"]["calls"] == report["summary"]["aggregated_executions"] == 0
    assert not report["warnings"]


def test_transitive_work_resume_chain_uses_explicit_root_not_matching_work_id(tmp_path):
    receipts = [work("origin"), work("middle", resumed_from="origin"),
                work("final", resumed_from="middle", status="SETTLED", landing={"landed": True}),
                work("unrelated")]
    for receipt in receipts:
        save(tmp_path / receipt["run_id"] / "run.json", receipt)
    runs = {r["run_id"]: r for r in collect([tmp_path])["work_runs"]}
    for identity in ("origin", "middle", "final"):
        assert runs[identity]["execution_origin_run_id"] == "origin"
        assert runs[identity]["execution_settled"] is True
        assert runs[identity]["settled_run_ids"] == ["final"]
    assert runs["final"]["resume_origin_run_id"] == "middle"
    assert runs["unrelated"]["execution_origin_run_id"] == "unrelated"
    assert runs["unrelated"]["execution_settled"] is False


@pytest.mark.parametrize("changes", [
    {"status": "ADMITTED", "admission": {"verdict": "ADMITTED"}},
    {"status": "CANDIDATE_ACCEPTED", "candidate_commit": "a" * 40},
    {"status": "SETTLED", "landing": {"landed": False}},
    {"status": "SETTLED", "landing": {"landed": "true"}},
    {"status": "SETTLED"},
])
def test_work_settlement_requires_both_terminal_status_and_confirmed_landing(tmp_path, changes):
    save(tmp_path / "run.json", work("one", **changes))
    report = collect([tmp_path])
    assert report["work_run_summary"]["settled_and_landed"] == 0
    assert not report["work_runs"][0]["settlement_confirmed"]


@pytest.mark.parametrize("case", ["missing", "work_mismatch", "train_mismatch", "self", "cycle"])
def test_unproven_resume_lineage_is_not_resolved(tmp_path, case):
    origin = work("origin")
    resumed = work("resume", resumed_from="origin")
    if case == "work_mismatch":
        resumed["work_id"] = "other-work"
    elif case == "train_mismatch":
        resumed["train_id"] = "other-train"
    elif case == "self":
        resumed["resumed_from"] = "resume"
    elif case == "cycle":
        origin["resumed_from"] = "resume"
    if case != "missing":
        save(tmp_path / "a/run.json", origin)
    save(tmp_path / "b/run.json", resumed)
    report = collect([tmp_path])
    result = next(r for r in report["work_runs"] if r["run_id"] == "resume")
    assert result["lineage"] == "unresolved"
    assert result["resume_origin_run_id"] is None


def test_contradictory_work_receipts_do_not_support_settlement_or_lineage(tmp_path):
    save(tmp_path / "a/run.json", work("origin"))
    save(tmp_path / "b/run.json", work("origin", status="SETTLED", landing={"landed": True}))
    save(tmp_path / "c/run.json", work("origin"))
    save(tmp_path / "d/run.json", work("resume", resumed_from="origin"))
    report = collect([tmp_path])
    runs = {r["run_id"]: r for r in report["work_runs"]}
    assert runs["origin"]["status"] == "CONTRADICTORY"
    assert not runs["origin"]["settlement_confirmed"]
    assert runs["resume"]["lineage"] == "unresolved"
    assert report["warnings"]


def test_work_projection_does_not_export_diagnostics_or_nested_non_scalar_fields(tmp_path):
    save(tmp_path / "a/run.json", work("valid", duration_ms=None,
        error="PRIVATE ERROR", repository_root="PRIVATE PATH", stages=[{"findings": "PRIVATE FINDINGS"}],
        admission={"verdict": "ADMITTED", "audit": "PRIVATE AUDIT"},
        landing={"landed": False, "checkout": "PRIVATE CHECKOUT"}))
    save(tmp_path / "b/run.json", work("invalid", resumed_from={"run_id": "valid", "secret": "PRIVATE"},
                                       candidate_commit={"secret": "PRIVATE"}))
    report = collect([tmp_path])
    assert "PRIVATE" not in json.dumps(report)
    assert next(r for r in report["work_runs"] if r["run_id"] == "valid")["duration_ms"] is None


def test_work_coverage_is_exposed_in_terminal_and_dynamic_html(tmp_path):
    label = '</script><script>alert("unsafe")</script>'
    save(tmp_path / "run.json", work("run-ui", work_id=label))
    report = collect([tmp_path])
    assert "Work attempts: 1" in terminal(report)
    html = render_html(report)
    assert label not in html
    assert "\\u003c/script\\u003e" in html
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    executable = next(script for script in scripts if "'use strict'" in script)
    assert "data.work_runs" in executable
    assert "work_run_summary" in executable
