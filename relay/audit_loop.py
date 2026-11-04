"""Convergence ledger for implementer/auditor audit loops.

An obligation written as a universal ("never destroy anything local") has no fixed point against
the state space of git or a file system: a competent auditor always finds one more variant, each
one rarer than the last, and each round costs a session. This module makes three things checkable
so the loop cannot drift into that:

- the packet author states a threat model per obligation (actor, window, access);
- the auditor states a scenario per finding (actor, window, access, plausibility);
- the system computes a ledger over the rounds and returns a decision with an exit code that gates
  the next round: CONTINUE, ACCEPTED, STOP_STALLED or STOP_BUDGET.

Stdlib only; reads audit-review/1 and /2 JSON files and never calls a model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

SCHEMA = "tare.tools/audit-loop-ledger/1"
PLAUSIBILITY = ("operational", "adversarial_operator", "laboratory", "outside_threat_model", "undeclared")
THREAT_FIELDS = ("actor", "window", "access")
DEFAULT_STALL_ROUNDS = 2   # same single obligation, no operational finding, this many rounds in a row
DEFAULT_MAX_ROUNDS = 6     # budget of rounds before the operator must decide, whatever the trend


def plausibility(finding, obligation=None):
    """The auditor's declared scenario decides; an undeclared one never counts as operational.

    A scenario whose actor, window or access contradicts the obligation's threat model is
    outside_threat_model regardless of what the auditor declared."""
    scenario = finding.get("scenario") if isinstance(finding, dict) else None
    if not isinstance(scenario, dict):
        return "undeclared"
    model = (obligation or {}).get("threat_model") if isinstance(obligation, dict) else None
    if isinstance(model, dict) and any(
            field in scenario and field in model and str(scenario[field]).strip().lower() != str(model[field]).strip().lower()
            for field in THREAT_FIELDS):
        return "outside_threat_model"
    value = str(scenario.get("plausibility", "undeclared")).strip().lower()
    return value if value in PLAUSIBILITY else "undeclared"


def summarize_round(number, review, obligations=None):
    obligations = {o["id"]: o for o in (obligations or []) if isinstance(o, dict) and "id" in o}
    failed = sorted({c["obligation_id"] for c in review.get("coverage", []) if c.get("status") == "FAILED"})
    findings = [{"id": f.get("id"), "obligation_id": f.get("obligation_id"), "severity": f.get("severity"),
                 "plausibility": plausibility(f, obligations.get(f.get("obligation_id")))}
                for f in review.get("findings", []) if isinstance(f, dict)]
    return {"round": number, "verdict": review.get("verdict"), "failed": failed, "findings": findings,
            "misfiled": [f["id"] for f in findings if f["plausibility"] == "outside_threat_model"],
            "context_requests": len(review.get("context_requests", []) or [])}


def ledger(reviews, *, obligations=None, stall_rounds=DEFAULT_STALL_ROUNDS, max_rounds=DEFAULT_MAX_ROUNDS):
    """Rounds in order; the decision is a function of the trend, never of the last verdict alone."""
    if any(type(value) is not int or value < 1 for value in (stall_rounds, max_rounds)):
        raise ValueError("positive_round_budgets_required")
    for review in reviews:
        if review.get("verdict") == "ACCEPTED_BOUNDED" and (
                review.get("findings") or review.get("context_requests") or not review.get("coverage")
                or any(item.get("status") != "CHECKED" for item in review["coverage"])):
            raise ValueError("accepted_review_has_unresolved_obligations")
    rounds = [summarize_round(i + 1, review, obligations) for i, review in enumerate(reviews)]
    seen, first_failed = set(), {}
    first_stop_round, first_stop_reason = None, None
    streak, streak_obligation = 0, None
    for item in rounds:
        item["new_obligations_failed"] = [o for o in item["failed"] if o not in seen]
        for o in item["new_obligations_failed"]:
            first_failed[o] = item["round"]
        seen.update(item["failed"])
        # Replay the trailing-stall predicate incrementally, without rerunning ledger on prefixes.
        if (len(item["failed"]) == 1 and item["failed"][0] == streak_obligation
                and not any(f["plausibility"] == "operational" for f in item["findings"])):
            streak += 1
        elif (len(item["failed"]) == 1
              and not any(f["plausibility"] == "operational" for f in item["findings"])):
            streak_obligation, streak = item["failed"][0], 1
        else:
            streak_obligation, streak = None, 0
        if first_stop_round is None and item["verdict"] != "ACCEPTED_BOUNDED":
            if streak >= stall_rounds:
                first_stop_round, first_stop_reason = item["round"], "STOP_STALLED"
            elif item["round"] >= max_rounds:
                first_stop_round, first_stop_reason = item["round"], "STOP_BUDGET"
    last = rounds[-1] if rounds else None
    converged = sorted(o for o in seen if last and o not in last["failed"])
    stall = _stall(rounds, stall_rounds)
    if last is None:
        decision, reason = "CONTINUE", "no rounds yet"
    elif last["verdict"] == "ACCEPTED_BOUNDED":
        decision, reason = "ACCEPTED", "last round accepted with no findings"
    elif stall:
        decision = "STOP_STALLED"
        reason = (f"{stall['obligation']} failed alone for {stall['rounds']} consecutive rounds with no operational "
                  "finding: rewrite the obligation with a threat model or accept the residual")
    elif len(rounds) >= max_rounds:
        decision, reason = "STOP_BUDGET", f"{len(rounds)} rounds reached the budget of {max_rounds}"
    else:
        decision, reason = "CONTINUE", "findings still open on more than one obligation or with operational plausibility"
    return {"schema": SCHEMA, "rounds": rounds, "obligations_ever_failed": sorted(seen), "converged": converged,
            "open": sorted(last["failed"]) if last else [], "first_failed_round": first_failed,
            "stall": stall, "decision": decision, "reason": reason,
            "first_stop_round": first_stop_round, "first_stop_reason": first_stop_reason,
            "reviews_after_first_stop": len(rounds) - first_stop_round if first_stop_round is not None else 0,
            "policy": {"stall_rounds": stall_rounds, "max_rounds": max_rounds}, "authority": "NONE"}


def _stall(rounds, stall_rounds):
    """Consecutive trailing rounds that failed exactly one and the same obligation, none operational."""
    streak, obligation = 0, None
    for item in reversed(rounds):
        single = len(item["failed"]) == 1 and (obligation in (None, item["failed"][0]))
        operational = any(f["plausibility"] == "operational" for f in item["findings"])
        if not single or operational:
            break
        obligation, streak = item["failed"][0], streak + 1
    return {"obligation": obligation, "rounds": streak} if streak >= stall_rounds else None


def load_rounds(directory):
    """review_round<N>.json files in numeric order; a JSON object embedded in surrounding text is accepted."""
    files = sorted(Path(directory).glob("review_round*.json"),
                   key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)))
    reviews = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        reviews.append(json.loads(raw[raw.find("{"):raw.rfind("}") + 1]))
    return reviews


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit loop convergence ledger; exit 3 means stop and decide")
    parser.add_argument("directory", help="folder holding review_round<N>.json files")
    parser.add_argument("--obligations", type=Path, help="JSON list of obligations with optional threat_model")
    parser.add_argument("--stall-rounds", type=int, default=DEFAULT_STALL_ROUNDS)
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    args = parser.parse_args(argv)
    obligations = json.loads(args.obligations.read_text(encoding="utf-8")) if args.obligations else None
    result = ledger(load_rounds(args.directory), obligations=obligations,
                    stall_rounds=args.stall_rounds, max_rounds=args.max_rounds)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 3 if result["decision"].startswith("STOP") else 0


if __name__ == "__main__":
    raise SystemExit(main())
