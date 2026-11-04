"""The loop ledger replays the shape of the 2026-09-08 inline audit: two rich rounds, then one obligation forever."""
import json

import pytest

from relay import audit_loop


def review(verdict, failed, findings):
    """findings: list of (id, obligation, plausibility or None)."""
    return {"schema": "tare.tools/audit-review/1", "packet_id": "P", "verdict": verdict,
            "coverage": [{"obligation_id": f"OB-{n}", "status": "FAILED" if f"OB-{n}" in failed else "CHECKED",
                          "evidence": "x"} for n in range(1, 9)],
            "findings": [{"id": fid, "obligation_id": oid, "severity": "P1", "category": "c", "claim": "c",
                          "location": {}, "falsifier": {}, "minimal_fix": "f",
                          **({"scenario": {"plausibility": p}} if p else {})} for fid, oid, p in findings],
            "context_requests": [], "residuals": [], "frugality_notes": []}


ROUNDS = [
    review("CHANGES_REQUESTED", {"OB-1", "OB-2", "OB-3", "OB-5", "OB-6"},
           [("F-1", "OB-1", "operational"), ("F-2", "OB-2", "operational"), ("F-3", "OB-3", "operational"),
            ("F-4", "OB-5", "operational"), ("F-5", "OB-6", "operational")]),
    review("CHANGES_REQUESTED", {"OB-2", "OB-5", "OB-7"},
           [("F-1", "OB-2", "operational"), ("F-2", "OB-5", "operational"), ("F-3", "OB-7", "operational")]),
    review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", "adversarial_operator")]),
    review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", "laboratory")]),
    review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", None)]),
]


def test_two_stalled_rounds_on_one_obligation_stop_the_loop():
    result = audit_loop.ledger(ROUNDS[:4])
    assert result["decision"] == "STOP_STALLED" and result["stall"] == {"obligation": "OB-2", "rounds": 2}
    assert result["converged"] == ["OB-1", "OB-3", "OB-5", "OB-6", "OB-7"] and result["open"] == ["OB-2"]
    assert result["first_failed_round"]["OB-7"] == 2 and result["rounds"][2]["new_obligations_failed"] == []
    # round 3 alone is not a stall: the trend needs stall_rounds identical single-obligation rounds
    assert audit_loop.ledger(ROUNDS[:3])["decision"] == "CONTINUE"


def test_an_operational_finding_keeps_the_loop_alive_and_undeclared_does_not():
    alive = ROUNDS[:3] + [review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", "operational")])]
    assert audit_loop.ledger(alive)["decision"] == "CONTINUE"
    assert audit_loop.ledger(ROUNDS[:3] + [ROUNDS[4]])["decision"] == "STOP_STALLED"  # undeclared counts as not operational
    assert audit_loop.ledger(ROUNDS[:3] + [ROUNDS[4]])["rounds"][-1]["findings"][0]["plausibility"] == "undeclared"


def test_retained_ten_round_shape_records_first_stop_and_six_later_reviews():
    result = audit_loop.ledger(ROUNDS[:4] + [ROUNDS[4]] * 6)
    assert result["decision"] == "STOP_STALLED"
    assert result["authority"] == "NONE"
    assert (result["first_stop_round"], result["first_stop_reason"],
            result["reviews_after_first_stop"]) == (4, "STOP_STALLED", 6)


def test_no_stop_reports_null_and_zero_and_budget_stop_is_recorded():
    for rounds in ([], ROUNDS[:3], ROUNDS[:2] + [review("ACCEPTED_BOUNDED", set(), [])]):
        result = audit_loop.ledger(rounds)
        assert (result["first_stop_round"], result["first_stop_reason"],
                result["reviews_after_first_stop"]) == (None, None, 0)
    churn = [review("CHANGES_REQUESTED", {"OB-1", "OB-2"},
                    [("F-1", "OB-1", "operational")])] * 7
    result = audit_loop.ledger(churn)
    assert (result["first_stop_round"], result["first_stop_reason"],
            result["reviews_after_first_stop"]) == (6, "STOP_BUDGET", 1)


def test_operational_finding_on_failed_obligation_delays_first_stall():
    operational = review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", "operational")])
    result = audit_loop.ledger(ROUNDS[:3] + [operational, ROUNDS[4], ROUNDS[4]])
    assert (result["first_stop_round"], result["first_stop_reason"],
            result["reviews_after_first_stop"]) == (6, "STOP_STALLED", 0)
    assert audit_loop.ledger(ROUNDS[:3] + [operational, ROUNDS[4]])["first_stop_round"] is None


def test_later_acceptance_does_not_rewrite_prior_stop_or_approve_prior_rounds():
    result = audit_loop.ledger(ROUNDS[:4] + [review("ACCEPTED_BOUNDED", set(), [])])
    assert result["decision"] == "ACCEPTED" and result["authority"] == "NONE"
    assert [item["verdict"] for item in result["rounds"][:4]] == ["CHANGES_REQUESTED"] * 4
    assert (result["first_stop_round"], result["first_stop_reason"],
            result["reviews_after_first_stop"]) == (4, "STOP_STALLED", 1)


def test_threat_model_reclassifies_a_finding_outside_it():
    obligations = [{"id": "OB-2", "threat_model": {"actor": "operator", "window": "during run", "access": "worktree"}}]
    inside = {"scenario": {"actor": "Operator", "window": "during run", "access": "worktree", "plausibility": "operational"}}
    outside = {"scenario": {"actor": "remote attacker", "window": "during run", "access": "worktree", "plausibility": "operational"}}
    assert audit_loop.plausibility(inside, obligations[0]) == "operational"
    assert audit_loop.plausibility(outside, obligations[0]) == "outside_threat_model"
    rounds = [review("CHANGES_REQUESTED", {"OB-2"}, [("F-1", "OB-2", "operational")])]
    rounds[0]["findings"][0]["scenario"] = outside["scenario"]
    result = audit_loop.ledger(rounds, obligations=obligations)
    assert result["rounds"][0]["misfiled"] == ["F-1"]


def test_acceptance_and_round_budget():
    accepted = ROUNDS[:2] + [review("ACCEPTED_BOUNDED", set(), [])]
    assert audit_loop.ledger(accepted)["decision"] == "ACCEPTED"
    churn = [review("CHANGES_REQUESTED", {"OB-1", "OB-2"}, [("F-1", "OB-1", "operational"), ("F-2", "OB-2", "operational")])] * 6
    assert audit_loop.ledger(churn, max_rounds=6)["decision"] == "STOP_BUDGET"
    assert audit_loop.ledger(churn[:5], max_rounds=6)["decision"] == "CONTINUE"


def test_cli_reads_rounds_in_numeric_order_and_exits_3_on_stop(tmp_path, capsys):
    for n, item in enumerate(ROUNDS[:4], 1):
        (tmp_path / f"review_round{n}.json").write_text("noise " + json.dumps(item) + " noise", encoding="utf-8")
    (tmp_path / "review_round10.json").write_text(json.dumps(ROUNDS[4]), encoding="utf-8")  # sorts after 4, not after 1
    assert audit_loop.main([str(tmp_path)]) == 3
    out = json.loads(capsys.readouterr().out)
    assert [r["round"] for r in out["rounds"]] == [1, 2, 3, 4, 5] and out["decision"] == "STOP_STALLED"
    assert audit_loop.main([str(tmp_path), "--stall-rounds", "9"]) == 0


@pytest.mark.parametrize("field,value", [
    ("findings", [{"id": "F-1"}]), ("context_requests", [{"reason": "missing source"}]),
    ("coverage", []), ("coverage", [{"obligation_id": "OB-1", "status": "NOT_CHECKED"}]),
    ("coverage", [{"obligation_id": "OB-1", "status": "FAILED"}]),
])
def test_acceptance_label_cannot_override_missing_or_failed_evidence(field, value):
    item = review("ACCEPTED_BOUNDED", set(), [])
    item[field] = value
    with pytest.raises(ValueError, match="accepted_review_has_unresolved_obligations"):
        audit_loop.ledger([item])


@pytest.mark.parametrize("budget", [0, -1, True])
def test_round_budget_must_be_a_positive_integer(budget):
    with pytest.raises(ValueError, match="positive_round_budgets_required"):
        audit_loop.ledger([], max_rounds=budget)
