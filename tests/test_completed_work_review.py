"""Bounded offline completed-Work review; closure is not a quality verdict."""
from copy import deepcopy
import json

import pytest

from scripts.round_table import report_round_table_telemetry as telemetry
from test_inference_telemetry import save
from test_native_work_telemetry import native_call, usage, work
from test_work_call_attribution_report import claim


def settled(run_id, day=1, **changes):
    fields = dict(status="SETTLED", landing={"landed": True},
                  started_at=f"2026-09-{day:02d}T12:00:00+00:00",
                  ended_at=f"2026-09-{day:02d}T12:00:01+00:00")
    fields.update(changes)
    return work(run_id, **fields)


def review(tmp_path):
    return telemetry.completed_work_review(telemetry.collect([tmp_path]))


def test_ten_unique_settled_roots_are_the_latest_bounded_cohort(tmp_path):
    for day in range(1, 12):
        save(tmp_path / str(day) / "run.json", settled(f"root-{day:02d}", day))
    report = telemetry.collect([tmp_path])
    before = deepcopy(report)
    result = telemetry.completed_work_review(report)
    assert report == before  # The reusable TUI projection has no side effects.
    assert result["selected_count"] == result["target_count"] == 10
    assert result["remaining"] == 0
    assert result["cohort_status"] == "complete"
    assert result["eligible_execution_count"] == 11
    assert [r["execution_origin_run_id"] for r in result["rows"]] == [
        f"root-{day:02d}" for day in range(11, 1, -1)]
    assert result["model_preference"] is None
    assert telemetry.completed_work_review(report, limit=100)["selected_count"] == 10
    assert telemetry.completed_work_review(report, limit=2)["selected_count"] == 2


def test_landed_resumes_cannot_fill_missing_slots_or_change_delivery_date(tmp_path):
    for day in range(1, 7):
        save(tmp_path / str(day) / "run.json", settled(f"root-{day}", day))
    for day in range(7, 13):
        save(tmp_path / str(day) / "run.json", settled(f"resume-{day}", day, resumed_from="root-1"))
    result = review(tmp_path)
    assert result["selected_count"] == 6
    assert result["remaining"] == 4
    assert result["cohort_status"] == "partial"
    oldest = result["rows"][-1]
    assert oldest["execution_origin_run_id"] == "root-1"
    assert oldest["settled_at"] == "2026-09-01T12:00:01+00:00"
    assert len(oldest["attempts"]) == len(oldest["settled_run_ids"]) == 7


def test_failed_attempts_and_cumulative_receipts_count_calls_and_cost_once(tmp_path):
    first = native_call(observation={"status": "failed", "error": "recorded_failure"})
    second = native_call("session/model/2")
    save(tmp_path / "origin/run.json", work("origin", inference_calls=[claim()]))
    save(tmp_path / "resume/run.json", settled("resume", 22, resumed_from="origin",
                                               inference_calls=[claim("session/model/2", "audit")]))
    save(tmp_path / "before/usage.json", usage(first))
    save(tmp_path / "after/usage.json", usage(first, second, native_call("unowned/model/3")))
    save(tmp_path / "copy/usage.json", usage(first, second))
    result, = review(tmp_path)["rows"]
    assert {r["status"] for r in result["attempts"]} == {"FAILED", "SETTLED"}
    assert len(result["attributed_calls"]) == result["recorded_attributed_call_count"] == 2
    assert {r["work_stage"] for r in result["attributed_calls"]} == {"implement", "audit"}
    assert {r["status"] for r in result["attributed_calls"]} == {"ERROR", "OK"}
    assert result["recorded_input_tokens"] == 200
    assert result["recorded_output_tokens"] == 40
    assert result["estimated_cost_usd"] == "0.50"
    assert result["reported_cost_usd"] is None
    assert result["coverage"] == "recorded"
    assert result["full_cost_usd"] is None
    assert all("execution_inference_usage" not in r for r in result["attempts"])


def test_legacy_counts_tokens_and_full_costs_stay_unknown(tmp_path):
    save(tmp_path / "run.json", settled("legacy"))
    save(tmp_path / "usage.json", usage(native_call()))
    result, = review(tmp_path)["rows"]
    assert result["recorded_attributed_call_count"] == 0
    assert result["coverage"] == "unknown"
    for key in ("full_call_count", "full_input_tokens", "full_output_tokens",
                "recorded_input_tokens", "recorded_output_tokens",
                "reported_cost_usd", "estimated_cost_usd", "full_cost_usd"):
        assert result[key] is None


@pytest.mark.parametrize("case", ["missing_receipt", "missing_cost", "observation_failure", "legacy_attempt"])
def test_incomplete_evidence_never_becomes_full_cost(tmp_path, case):
    changes = dict(inference_calls=[claim()])
    if case == "observation_failure":
        changes["inference_observation_failures"] = 1
    if case == "legacy_attempt":
        save(tmp_path / "origin/run.json", work("origin"))
        changes["resumed_from"] = "origin"
    save(tmp_path / "run.json", settled("done", 22, **changes))
    if case != "missing_receipt":
        call = native_call(cost={"complete": False}) if case == "missing_cost" else native_call()
        save(tmp_path / "usage.json", usage(call))
    result, = review(tmp_path)["rows"]
    assert result["full_cost_usd"] is None
    assert result["recorded_attributed_call_count"] == 1
    assert result["coverage"] == ("recorded" if case == "missing_cost" else "partial")
    if case in {"missing_receipt", "missing_cost"}:
        assert result["unknown_cost_calls"] == 1
        assert result["estimated_cost_usd"] is None


def test_reported_charges_and_additional_estimates_remain_separate(tmp_path):
    save(tmp_path / "run.json", settled("done", inference_calls=[claim(), claim("session/model/2")]))
    report = telemetry.collect([tmp_path])
    records = [telemetry.row("native-usage:session:session/model/1", "session", "native_usage", "model",
                             reported_cost_usd="0.1", estimated_cost_usd="0.9"),
               telemetry.row("native-usage:session:session/model/2", "session", "native_usage", "model",
                             estimated_cost_usd="0.2")]
    telemetry.link_work_calls(records, report["work_runs"], [])
    report["rows"] = records
    result, = telemetry.completed_work_review(report)["rows"]
    assert result["reported_cost_usd"] == "0.1"
    assert result["estimated_cost_usd"] == "0.2"


@pytest.mark.parametrize("changes,reason", [
    ({"resumed_from": "absent"}, "unresolved_identity"),
    ({"work_id": None}, "unresolved_identity"),
    ({"ended_at": None}, "uncertain_settlement_date"),
    ({"ended_at": "2026-09-01T12:00:01"}, "uncertain_settlement_date"),
    ({"ended_at": "not-a-date"}, "uncertain_settlement_date"),
    ({"ended_at": "2026-08-01T12:00:01Z"}, "uncertain_settlement_date"),
])
def test_unresolved_identities_and_uncertain_dates_are_excluded(tmp_path, changes, reason):
    save(tmp_path / "bad/run.json", settled("bad", **changes))
    save(tmp_path / "good/run.json", settled("good"))
    result = review(tmp_path)
    assert [r["execution_origin_run_id"] for r in result["rows"]] == ["good"]
    assert result["excluded_record_count"] == 1
    assert result["excluded_reason_counts"][reason] == 1
    assert result["exclusions"][0]["run_id"] == "bad"


def test_contradictory_run_and_call_records_are_excluded(tmp_path):
    save(tmp_path / "a/run.json", settled("conflicting-run"))
    save(tmp_path / "b/run.json", work("conflicting-run"))
    save(tmp_path / "c/run.json", settled("conflicting-call", inference_calls=[claim()]))
    save(tmp_path / "d/run.json", settled("other-owner", inference_calls=[claim()]))
    save(tmp_path / "usage.json", usage(native_call()))
    result = review(tmp_path)
    assert not result["rows"]
    assert result["excluded_reason_counts"]["contradictory_identity"] == 1
    assert result["excluded_reason_counts"]["contradictory_call_identity"] == 2


def test_conflicting_native_receipts_exclude_execution(tmp_path):
    save(tmp_path / "run.json", settled("done", inference_calls=[claim()]))
    save(tmp_path / "a/usage.json", usage(native_call()))
    save(tmp_path / "b/usage.json", usage(native_call(cost={"known_amount": "10", "currency": "USD", "complete": True})))
    result = review(tmp_path)
    assert result["selected_count"] == 0
    assert result["excluded_reason_counts"]["contradictory_call_identity"] == 1


def test_unknown_metadata_quality_and_private_payloads_are_not_inferred_or_exported(tmp_path):
    save(tmp_path / "model-repository/run.json", settled("model-task", inference_calls=[claim()],
        owner={"prompt": "PRIVATE PROMPT", "credential": "PRIVATE TOKEN"},
        repository_root={"secret": "PRIVATE ROOT"}, runtime_identity=["PRIVATE VERSION"],
        audit={"outcome": "PRIVATE AUDIT"}))
    call = native_call()
    call["observation"].update(prompt="PRIVATE PROMPT", credentials="PRIVATE TOKEN")
    save(tmp_path / "usage.json", usage(call))
    report = telemetry.collect([tmp_path])
    result = telemetry.completed_work_review(report)
    item, = result["rows"]
    for key in ("repository_root", "task_class", "runtime_identity", "recorded_audit_outcome",
                "code_quality", "independent_audit", "subscription_quota",
                "control_tool_infrastructure_cost_usd", "human_cost_usd"):
        assert item[key] is None
    assert item["settlement_confirmed"] is True
    assert "PRIVATE" not in json.dumps(report) + json.dumps(result)


def test_cli_requires_explicit_sources_before_collecting(monkeypatch, capsys):
    monkeypatch.setattr(telemetry, "collect", lambda sources: pytest.fail("must reject before reading defaults"))
    with pytest.raises(SystemExit) as error:
        telemetry.main(["--review-completed-works"])
    assert error.value.code == 2
    assert "requires explicit --source" in capsys.readouterr().err


def test_cli_opt_in_json_and_concise_terminal_leave_legacy_output_unchanged(tmp_path, capsys):
    source, output = tmp_path / "run.json", tmp_path / "review.json"
    save(source, settled("done"))
    assert telemetry.main(["--source", str(source), "--json", str(output)]) == 0
    assert "completed_work_review" not in json.loads(output.read_text(encoding="utf-8"))
    assert "Completed Work review" not in capsys.readouterr().out
    assert telemetry.main(["--source", str(source), "--review-completed-works", "--json", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))["completed_work_review"]
    assert result["selected_count"] == 1 and result["remaining"] == 9
    text = capsys.readouterr().out
    assert "Completed Work review: 1/10" in text
    assert "partial" in text and "No Work observation minimum or model preference" in text


def test_declared_metadata_survives_collect_review_pipeline(tmp_path):
    declared = dict(repository_root='C:/side-projects/kanban', task_class='bugfix',
                    runtime_identity='recorded-runtime-1', recorded_audit_outcome='REVISE')
    save(tmp_path/'origin/run.json', settled('origin', **declared,
        owner={'prompt': 'PRIVATE', 'credential': 'PRIVATE'}, diagnostics={'secret': 'PRIVATE'}))
    save(tmp_path/'resume/run.json', settled('resume', 2, resumed_from='origin',
        repository_root=declared['repository_root'], task_class={'secret': 'PRIVATE'},
        runtime_identity=['PRIVATE'], recorded_audit_outcome='x'*1025))
    default = telemetry.collect([tmp_path])
    assert all(key not in attempt for attempt in default['work_runs']
               for key in telemetry.WORK_METADATA)
    assert declared['repository_root'] not in json.dumps(default)
    report = telemetry.collect([tmp_path], include_work_metadata=True)
    result = telemetry.completed_work_review(report)
    item, = result['rows']
    for key, value in declared.items():
        assert item[key] == value
        assert item['attempts'][0][key] == value
    resumed = item['attempts'][1]
    assert resumed['repository_root'] == declared['repository_root']
    assert all(resumed[key] is None for key in declared if key != 'repository_root')
    assert item['code_quality'] is item['independent_audit'] is None
    assert 'PRIVATE' not in json.dumps(report) + json.dumps(result)


def test_conflicting_metadata_is_unknown_without_excluding_settlement(tmp_path):
    save(tmp_path/'origin/run.json', settled('origin', repository_root='C:/one',
                                               runtime_identity='recorded-v1'))
    save(tmp_path/'resume/run.json', settled('resume', 2, resumed_from='origin',
        repository_root='C:/two', runtime_identity='recorded-v2'))
    result = telemetry.completed_work_review(
        telemetry.collect([tmp_path], include_work_metadata=True))
    assert result['selected_count'] == 1
    item, = result['rows']
    assert item['repository_root'] is item['runtime_identity'] is None
    assert {a['repository_root'] for a in item['attempts']} == {'C:/one', 'C:/two'}


@pytest.mark.parametrize('value', [None, '', '   ', 42, True, ['PRIVATE'],
                                  {'secret': 'PRIVATE'}, 'x'*1025, 'bad\nvalue'])
def test_optional_metadata_rejects_malformed_declarations(tmp_path, value):
    save(tmp_path/'run.json', settled('done', **dict.fromkeys(telemetry.WORK_METADATA, value)))
    result = telemetry.completed_work_review(
        telemetry.collect([tmp_path], include_work_metadata=True))
    item, = result['rows']
    assert all(item[key] is None and item['attempts'][0][key] is None
               for key in telemetry.WORK_METADATA)
