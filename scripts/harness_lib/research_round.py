#!/usr/bin/env python3
"""Compile and advance canonical research rounds."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib import (async_runtime, model_routing, research_index, workflow_plan,
                         workflow_reduce, workflow_schema, workflow_spawn)
from harness_lib.common import HarnessError, ROOT, read_json

Validator = Callable[..., dict[str, Any]]
_STATE_START = "<!-- round-state:start -->"
_STATE_END = "<!-- round-state:end -->"
_SETTLED = frozenset({"done", "partial", "reduced"})
_ADVANCE_TERMINAL = _SETTLED | {"failed"}
_ADVANCE_TIMEOUT_SECONDS = 1800


def _rule(number: int, detail: str) -> HarnessError:
    return HarnessError(f"research round compile rule {number}: {detail}")


def _branches(phase: str, block: dict[str, Any]) -> list[dict[str, Any]]:
    key = "perspectives" if phase == "develop" else "lenses"
    prefix = "ideator" if phase == "develop" else "critic"
    return [{"title": str(item.get("title") or f"{item['id']} {phase}"),
             "taskProfile": item["taskProfile"],
             "workerRole": str(item.get("workerRole") or f"{prefix}-{item['id']}")}
            for item in block[key]]


def _cohorts(phase: str, block: dict[str, Any]) -> list[dict[str, Any]]:
    branches = _branches(phase, block)
    out, offset = [], 0
    for fleet in block["fleet"]:
        count = fleet["count"]
        cohort_id = f"{phase}-{fleet['id']}"
        spawn = {key: fleet[key] for key in ("executor", "model", "effort")}
        out.append({**fleet, "cohortId": cohort_id,
                    "branches": [{**branch, "cohortId": cohort_id, "spawn": spawn}
                                 for branch in branches[offset:offset + count]]})
        offset += count
    return out


def _unified_branches(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [branch for cohort in cohorts for branch in cohort["branches"]]


def _declared_tuples(root: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str], dict[str, Any]]:
    executors = read_json(root / ".harness" / "routing" / "executors.json", {}) or {}
    profiles = read_json(root / ".harness" / "routing" / "task-profiles.json", {}) or {}
    routing = read_json(root / ".harness" / "routing" / "model-routing.json", {}) or {}
    declared: set[tuple[str, str]] = set()
    for profile in (profiles.get("profiles") or {}).values():
        for executor, spawn in (profile.get("spawn") or {}).items():
            if isinstance(spawn, dict) and spawn.get("model"):
                declared.add((str(executor), str(spawn["model"])))
    for executor, config in (executors.get("executors") or {}).items():
        spawn = config.get("defaultSpawn") if isinstance(config, dict) else None
        if isinstance(spawn, dict) and spawn.get("model"):
            declared.add((str(executor), str(spawn["model"])))
    for profile in (routing.get("profiles") or {}).values():
        for role in (profile.get("roles") or {}).values():
            for entry in [role.get("primary"), *(role.get("fallbacks") or [])]:
                if isinstance(entry, dict) and entry.get("executor") and entry.get("card"):
                    declared.add((str(entry["executor"]), str(entry["card"])))
    cards = {(str(card["engine"]), str(card["id"])) for card in model_routing.cards_list(root)}
    return declared, cards, set((executors.get("executors") or {})), profiles.get("profiles") or {}


def _precheck(root: Path, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    for phase, key in (("develop", "perspectives"), ("refine", "lenses")):
        block = spec[phase]
        fleet_count = sum(item["count"] for item in block["fleet"])
        if fleet_count != block["width"]["count"]:
            raise _rule(1, f"{phase} fleet count {fleet_count} != width count {block['width']['count']}")
        if block.get("assignment", "zip") == "zip" and len(block[key]) != block["width"]["count"]:
            raise _rule(2, f"{phase} zip assignment has {len(block[key])} {key} for width {block['width']['count']}")

    develop, refine = _cohorts("develop", spec["develop"]), _cohorts("refine", spec["refine"])
    declared, cards, executors, task_profiles = _declared_tuples(root)
    for cohort in [*develop, *refine]:
        pair = (cohort["executor"], cohort["model"])
        if pair not in declared or pair not in cards or pair[0] not in executors:
            raise _rule(3, f"missing live executor/model tuple {pair[0]}/{pair[1]}")
        for branch in cohort["branches"]:
            spawn = (task_profiles.get(branch["taskProfile"]) or {}).get("spawn") or {}
            if cohort["executor"] not in spawn:
                raise _rule(3, f"tuple {pair[0]}/{pair[1]} does not resolve task profile {branch['taskProfile']}")

    workflow_profiles = (read_json(root / ".harness" / "workflows" / "workflow-profiles.json", {}) or {}).get("profiles") or {}
    minimums: dict[str, Any] = {}
    for phase, cohorts in (("develop", develop), ("refine", refine)):
        profile = spec[phase]["profile"]
        try:
            configured = int(workflow_profiles[profile]["awaitPolicy"]["minSuccess"])
        except (KeyError, TypeError, ValueError):
            raise _rule(5, f"profile {profile!r} has no integer awaitPolicy.minSuccess")
        minimums[phase] = configured
        for cohort in cohorts:
            cohort["minSuccessConfigured"] = configured
            cohort["minSuccessEffective"] = min(configured, len(cohort["branches"]))
            for branch in cohort["branches"]:
                branch.update({"minSuccessConfigured": configured,
                               "minSuccessEffective": cohort["minSuccessEffective"]})

    seeded = next((c for c in develop if c.get("seed")), None)
    if seeded:
        raise _rule(6, f"develop cohort {seeded['cohortId']} must not carry a seed")

    edges = []
    for index, critic in enumerate(refine):
        source = develop[index % len(develop)]
        edge = {"sourceCohortId": source["cohortId"], "criticCohortId": critic["cohortId"],
                "sourceExecutor": source["executor"], "criticExecutor": critic["executor"],
                "seed": critic.get("seed")}
        if edge["sourceExecutor"] == edge["criticExecutor"]:
            raise _rule(7, f"critique edge {edge['sourceCohortId']} -> {edge['criticCohortId']} uses executor {edge['sourceExecutor']} on both sides")
        edges.append(edge)
    covered = {edge["sourceCohortId"] for edge in edges}
    missing = next((c["cohortId"] for c in develop if c["cohortId"] not in covered), None)
    if missing:
        raise _rule(8, f"source cohort {missing} has no cross-vendor critic match")
    return develop, refine, edges, minimums


def _compile_spec(root: Path, slug: str, spec: dict[str, Any], validator: Validator) -> dict[str, Any]:
    workflow_schema.validate_packet(spec, root / "schemas" / "research-round.schema.json")
    if spec["slug"] != slug:
        raise HarnessError(f"research round spec slug {spec['slug']!r} does not match document slug {slug!r}")
    develop, refine, edges, _minimums = _precheck(root, spec)
    totals = {"develop": 0, "refine": 0}
    children: dict[str, list[dict[str, Any]]] = {"develop": [], "refine": []}
    workflows: dict[str, dict[str, Any]] = {}
    for phase, cohorts in (("develop", develop), ("refine", refine)):
        branches = _unified_branches(cohorts)
        task = (f"Research round {slug} {phase} cohort {cohorts[0]['id']}: {spec['question']}"
                if len(cohorts) == 1 else f"Research round {slug} {phase} cohorts: {spec['question']}")
        seeds = [edge.get("seed") for edge in edges if edge["criticCohortId"] in
                 {cohort["cohortId"] for cohort in cohorts} and edge.get("seed")]
        seed = None if phase == "develop" else (seeds[0] if len(seeds) == 1 else (seeds or None))
        report = validator(
            task, None, branches=branches, profile_name=spec[phase]["profile"], seed=seed,
            width_mode=spec[phase]["width"]["mode"], width_why=spec[phase]["width"]["why"])
        if not report.get("valid"):
            first = (report.get("errors") or ["unknown child validation error"])[0]
            raise HarnessError(f"research round {phase} workflow failed validation: {first}")
        audit = report.get("tokenAudit") or {}
        if not isinstance(audit.get("totalPlanned"), int):
            raise _rule(4, f"{phase} workflow has no integer tokenAudit.totalPlanned")
        totals[phase] = audit["totalPlanned"]
        workflows[phase] = {"profile": spec[phase]["profile"], "branches": branches,
                            "cohortIds": [cohort["cohortId"] for cohort in cohorts], "plan": report}
        for cohort in cohorts:
            child = {key: cohort[key] for key in ("cohortId", "id", "executor", "model", "effort",
                                                   "branches", "minSuccessConfigured", "minSuccessEffective")}
            child.update({"seed": seed, "plan": report})
            edge = next((item for item in edges if item["criticCohortId"] == cohort["cohortId"]), None)
            if edge:
                child["sourceCohortId"] = edge["sourceCohortId"]
            children[phase].append(child)
    logical_budget = {**totals, "totalPlanned": totals["develop"] + totals["refine"],
                      "declaredRoundMaxPlannedTokens": spec["budget"]["roundMaxPlannedTokens"]}
    return {"schemaVersion": spec["schemaVersion"], "slug": slug, "validateOnly": True,
            "zeroMaterialization": True, "develop": children["develop"],
            "refine": children["refine"], "workflows": workflows, "critiqueEdges": edges,
            "logicalBudget": logical_budget, "valid": True, "errors": []}


def compile_round(root: Path | str, slug: str) -> dict[str, Any]:
    root = Path(root)
    parsed = research_index.parse_round(root / "docs" / "research" / f"{slug}.md")
    spec = parsed.get("roundSpec") or {}
    if not spec:
        detail = "; ".join(parsed.get("parseErrors") or []) or "missing fenced JSON round-spec block"
        raise HarnessError(f"research round {slug!r}: {detail}")
    return _compile_spec(root, slug, spec, workflow_plan.validate_workflow_plan)


def _state_block(state: dict[str, Any], newline: str = "\n") -> str:
    body = json.dumps(state, indent=2, ensure_ascii=False).replace("\n", newline)
    return newline.join((_STATE_START, "```json", body, "```", _STATE_END))


def _write_round_state(path: Path, state: dict[str, Any]) -> None:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _state_block(state, newline)
    start, end = text.find(_STATE_START), text.find(_STATE_END)
    if (start < 0) != (end < 0):
        raise HarnessError(f"research round {path.stem!r}: incomplete round-state markers")
    if start < 0:
        separator = newline if text.endswith(("\n", "\r")) else newline * 2
        text = text + separator + block + newline
    else:
        end += len(_STATE_END)
        text = text[:start] + block + text[end:]
    path.write_bytes(text.encode("utf-8"))


def _section_present(text: str, term: str) -> bool:
    text = re.sub(
        rf"{re.escape(_STATE_START)}.*?{re.escape(_STATE_END)}", "", text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"^ {0,3}```[^\r\n]*(?:\r?\n).*?^ {0,3}```[ \t]*(?:\r?\n|$)", "", text,
        flags=re.MULTILINE | re.DOTALL,
    )
    heading = re.compile(rf"^(#{{1,3}}) [^\r\n]*{term}[^\r\n]*(?:\r?\n|$)",
                         re.IGNORECASE | re.MULTILINE)
    for match in heading.finditer(text):
        rest = text[match.end():]
        boundary = re.search(rf"^#{{1,{len(match.group(1))}}} ", rest, re.MULTILINE)
        if rest[:boundary.start() if boundary else None].strip():
            return True
    return False


def _registered_experiment(root: Path, slug: str, text: str) -> bool:
    registry = read_json(root / ".harness" / "state" / "experiments.json", {}) or {}
    source = f"docs/research/{slug}.md"
    for entry in registry.get("experiments", []):
        if not isinstance(entry, dict):
            continue
        exp_id = str(entry.get("id") or "")
        if str(entry.get("source") or "").replace("\\", "/") == source:
            return True
        if exp_id and re.search(rf"(?<![A-Za-z0-9-]){re.escape(exp_id)}(?![A-Za-z0-9-])", text):
            return True
    return False


def approve_round(root: Path | str, slug: str, note: str | None = None) -> dict[str, Any]:
    root = Path(root)
    note = note or ""
    if _STATE_START in note or _STATE_END in note or "```" in note:
        raise HarnessError(
            "research round note must not contain round-state markers or triple-backtick fences")
    note = note[:400]
    plan = compile_round(root, slug)
    doc = root / "docs" / "research" / f"{slug}.md"
    parsed = research_index.parse_round(doc)
    state = parsed.get("roundState") or {}
    existing = {item.get("cohortId"): item
                for item in state.get("cohorts", [])
                if isinstance(item, dict) and item.get("cohortId")}
    incomplete = [cohort["cohortId"] for cohort in [*plan["develop"], *plan["refine"]]
                  if existing.get(cohort["cohortId"], {}).get("status") not in _SETTLED]
    if incomplete:
        raise HarnessError(f"research round {slug!r} incomplete cohorts: {', '.join(incomplete)}")

    text = doc.read_text(encoding="utf-8")
    spec = parsed["roundSpec"]
    deliver = spec["deliver"]
    sections = {
        "operations": _section_present(text, r"opera"),
        "portfolio": _section_present(text, r"portf"),
        "experiment": (_section_present(text, r"experiment")
                       or _registered_experiment(root, slug, text)),
        "traceability": _section_present(text, r"(?:rastreab|traceab)"),
    }
    required = {
        "operations-per-concept": (deliver.get("requireOneOperationPerConcept"),
                                   sections["operations"]),
        "portfolio": (deliver.get("requireExactlyOnePortfolioBucket"), sections["portfolio"]),
        "experiment": ("experimentDesign" in spec, sections["experiment"]),
        "traceability": (True, sections["traceability"]),
    }
    missing = [name for name, (needed, present) in required.items() if needed and not present]
    if missing:
        raise HarnessError(f"research round {slug!r} missing deliver sections: {', '.join(missing)}")
    if deliver.get("autoPromote"):
        raise HarnessError("research round autoPromote=true not yet: promotion remains explicit")

    decision = {
        "approvedBy": "human",
        "at": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "checks": {"stateComplete": True, "sections": sections},
    }
    state["deliver"] = decision
    _write_round_state(doc, state)
    return decision


def _terminal_executor(worker: dict[str, Any]) -> str | None:
    run = worker.get("run") if isinstance(worker.get("run"), dict) else {}
    fallback = run.get("fellBackTo") if isinstance(run.get("fellBackTo"), dict) else {}
    history = run.get("failoverHistory") if isinstance(run.get("failoverHistory"), list) else []
    terminal = fallback.get("executor") or (history[-1].get("executor") if history else None)
    value = terminal or worker.get("resolvedExecutor") or (worker.get("spawn") or {}).get("executor")
    return str(value) if value else None


def _cohort_outcomes(base: Path, workflow: dict[str, Any], failures: set[str], pending: set[str],
                     cohorts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        workers = [worker for worker in workflow.get("workers", [])
                   if worker.get("cohortId") == cohort["cohortId"]]
        valid = [worker for worker in workers
                 if worker.get("workerId") not in failures
                 and (base / str(worker.get("resultPath") or "")).is_file()]
        worker_executors = {str(worker["workerId"]): executor for worker in workers
                            if (executor := _terminal_executor(worker))}
        executors = sorted(set(worker_executors.values()))
        met = len(valid) >= cohort["minSuccessEffective"]
        outcomes[cohort["cohortId"]] = {
            "status": "running" if pending & {str(w.get("workerId")) for w in workers}
            else ("done" if met else "failed"),
            "executor": executors[0] if len(executors) == 1 else ("mixed" if executors else cohort["executor"]),
            "workerExecutors": worker_executors,
            "minSuccessConfigured": cohort["minSuccessConfigured"],
            "minSuccessEffective": cohort["minSuccessEffective"],
            "successes": len(valid), "minSuccessMet": met,
        }
    return outcomes


def _run_child(wfid: str, cohorts: list[dict[str, Any]], executor_override: str | None,
               *, start: bool, relaunch: bool = False) -> dict[str, dict[str, Any]]:
    resolver = workflow_spawn.resolve_worker_executor
    if not callable(resolver):
        raise HarnessError("research round branch executor resolver is not bound")
    base, workflow = async_runtime.load_workflow(wfid)
    reduced = workflow.get("phase") == "reduced" or (base / "reduce" / "reducer.result.json").is_file()
    if relaunch:
        # A retry round STALES any prior (partial) reduce: it was computed without
        # the retried workers, so the settled group must re-reduce over the full
        # set (measured 2026-08-03: the stale partial reduce made canReduce True
        # and the cleared workers were never relaunched).
        reduced = False

    def launch() -> dict[str, Any]:
        return async_runtime.workflow_start(
            wfid, executor_name=executor_override, only_missing=True,
            executor_resolver=resolver)

    if start or relaunch:
        launch()
    else:
        try:
            awaited = async_runtime.workflow_await(
                wfid, timeout=_ADVANCE_TIMEOUT_SECONDS)
        except HarnessError as exc:
            if "has no async group" not in str(exc):
                raise
            launch()
        else:
            collected = async_runtime.workflow_async_collect(wfid)
            if collected.get("canReduce") and not reduced:
                workflow_reduce.workflow_reduce(
                    wfid, allow_partial=bool(collected.get("allowPartialReduce")))
                reduced = True
            if collected.get("pending"):
                return {cohort["cohortId"]: {"status": str(
                    awaited.get("status") or collected.get("status") or "running")}
                        for cohort in cohorts}
            if not collected.get("canReduce"):
                launch()
    async_runtime.workflow_await(
        wfid, timeout=_ADVANCE_TIMEOUT_SECONDS)
    collected = async_runtime.workflow_async_collect(wfid)
    if collected.get("canReduce") and not reduced:
        workflow_reduce.workflow_reduce(
            wfid, allow_partial=bool(collected.get("allowPartialReduce")))
    base, workflow = async_runtime.load_workflow(wfid)
    failures = {str(item.get("workerId")) for item in
                workflow_reduce.workflow_validate_results(wfid).get("failures", [])}
    pending = {str(item.get("workerId")) for item in collected.get("pending", [])}
    return _cohort_outcomes(base, workflow, failures, pending, cohorts)


def _retry_failed_workers(wfid: str, cohort_ids: set[str]) -> tuple[list[str], list[str]]:
    """`(retried, stuck)` — cohort-SCOPED recovery of validator-rejected workers.

    Only workers belonging to a FAILED cohort are touched (an already-settled
    cohort's straggler is never swept into someone else's recovery pass). A
    worker with an invalid result on disk is cleared+retried through
    `workflow_lifecycle.workflow_retry` (maxRounds ratchet applies: past the
    bound it is skipped and stays terminal — the human door is `workflow retry
    --force-round`). A worker ALREADY cleared by an earlier pass (retry
    requested, no result on disk — the crash-between-retry-and-relaunch window)
    is returned as `stuck`: the caller relaunches it WITHOUT consuming another
    attempt, so repeated `advance` calls never archive-and-clear in a loop.
    Only the maxRounds refusal is swallowed; any other refusal (epoch fence,
    invalid workflow state) re-raises loudly — it is actionable, not noise."""
    from harness_lib import workflow_lifecycle
    base, workflow = async_runtime.load_workflow(wfid)
    workers = {str(w.get("workerId") or ""): w for w in workflow.get("workers", [])}
    retried: list[str] = []
    stuck: list[str] = []
    for item in workflow_reduce.workflow_validate_results(wfid).get("failures", []):
        worker_id = str(item.get("workerId") or "")
        worker = workers.get(worker_id)
        if not worker or str(worker.get("cohortId") or "") not in cohort_ids:
            continue
        has_result = (base / str(worker.get("resultPath") or "")).is_file()
        if not has_result and worker.get("retryRequestedAt"):
            stuck.append(worker_id)
            continue
        try:
            workflow_lifecycle.workflow_retry(wfid, worker_id, clear_result=True)
        except HarnessError as exc:
            if "maxRounds" in str(exc):
                continue
            raise
        retried.append(worker_id)
    return retried, stuck


def _advance_phase(doc: Path, state: dict[str, Any], existing: dict[str, dict[str, Any]],
                   slug: str, question: str, phase: str, cohorts: list[dict[str, Any]],
                   executor_override: str | None, seeds: dict[str, str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for cohort in cohorts:
        previous = existing.get(cohort["cohortId"], {})
        row = {"cohortId": cohort["cohortId"], "phase": phase,
               "declaredExecutor": cohort["executor"],
               "executor": previous.get("executor", cohort["executor"]),
               "model": cohort["model"], "wfid": previous.get("wfid"),
               "status": previous.get("status", "planned")}
        if phase == "refine":
            row.update({"seed": (seeds or {}).get(cohort["cohortId"], ""),
                        "sourceCohortId": cohort["sourceCohortId"]})
        rows.append(row)
        state["cohorts"].append(row)
    if all(row.get("wfid") and row["status"] in _SETTLED for row in rows):
        return rows
    wfids = {str(row["wfid"]) for row in rows if row.get("wfid")}
    if len(wfids) > 1:
        # Consistency guard runs BEFORE any recovery side effect: a mixed-wfid
        # phase is corrupt state and must raise with nothing mutated.
        raise HarnessError(f"research round {slug!r} {phase} cohorts have multiple workflow IDs")
    if seeds is not None and any(not row.get("seed") for row in rows):
        return rows
    failed_rows = [row for row in rows if row.get("wfid") and row["status"] == "failed"]
    retried: list[str] = []
    stuck: list[str] = []
    if failed_rows and all(row.get("wfid") and row["status"] in _ADVANCE_TERMINAL for row in rows):
        # `failed` is NOT a dead end (measured 2026-08-03, round audit-quorum-
        # dinamico): retry the invalidated workers of the FAILED cohorts and
        # re-arm the rows; `stuck` workers (cleared by a crashed pass) are
        # relaunch-only. Nothing retryable left -> genuinely terminal,
        # byte-identical to the old behavior.
        retried, stuck = _retry_failed_workers(
            next(iter(wfids)), {str(row["cohortId"]) for row in failed_rows})
        if not retried and not stuck:
            return rows
        for row in failed_rows:
            row["status"] = "running"
    wfid = next(iter(wfids), "")
    is_new = not wfid
    if is_new:
        task = (f"Research round {slug} {phase} cohort {cohorts[0]['id']}: {question}"
                if len(cohorts) == 1 else f"Research round {slug} {phase} cohorts: {question}")
        seed_values = list(dict.fromkeys(row.get("seed") for row in rows if row.get("seed")))
        try:
            workflow = workflow_plan.plan_workflow(
                task, None, branches=_unified_branches(cohorts),
                profile_name=cohorts[0]["plan"]["profile"],
                seed=(seed_values[0] if len(seed_values) == 1 else (seed_values or None)))
        except HarnessError as exc:
            if "reduce" in str(exc):
                return rows
            raise
        wfid = str(workflow["workflowId"])
        for row in rows:
            row["wfid"] = wfid
        _write_round_state(doc, state)
    for row in rows:
        if row["status"] not in _ADVANCE_TERMINAL:
            row["status"] = "running"
    _write_round_state(doc, state)
    outcomes = _run_child(wfid, cohorts, executor_override, start=is_new,
                          relaunch=bool(retried or stuck))
    for row in rows:
        if row["status"] not in _ADVANCE_TERMINAL:
            row.update(outcomes[row["cohortId"]])
    _write_round_state(doc, state)
    return rows


def advance_round(root: Path | str, slug: str,
                  executor_override: str | None = None) -> dict[str, Any]:
    root = Path(root)
    plan = compile_round(root, slug)
    doc = root / "docs" / "research" / f"{slug}.md"
    parsed = research_index.parse_round(doc)
    question = parsed["roundSpec"]["question"]
    previous_state = parsed.get("roundState") or {}
    existing = {item.get("cohortId"): item
                for item in previous_state.get("cohorts", [])
                if isinstance(item, dict) and item.get("cohortId")}
    state: dict[str, Any] = {key: value for key, value in previous_state.items()
                             if key != "cohorts"}
    state["cohorts"] = []
    develop_rows = _advance_phase(doc, state, existing, slug, question, "develop",
                                  plan["develop"], executor_override)
    develop_by_id = {row["cohortId"]: row for row in develop_rows}
    refine_seeds = {cohort["cohortId"]: str(
        develop_by_id[cohort["sourceCohortId"]].get("wfid") or "") for cohort in plan["refine"]}
    _advance_phase(doc, state, existing, slug, question, "refine", plan["refine"],
                   executor_override, refine_seeds)
    return state


def cmd_round(args) -> int:
    if args.subaction == "compile":
        if not args.slug:
            raise HarnessError("research round requires a <slug>")
        print(json.dumps(compile_round(ROOT, args.slug), indent=2, ensure_ascii=False))
        return 0
    if args.subaction == "advance":
        if not args.slug:
            raise HarnessError("research round requires a <slug>")
        print(json.dumps(advance_round(ROOT, args.slug, getattr(args, "executor", None)),
                         indent=2, ensure_ascii=False))
        return 0
    if args.subaction == "approve":
        if not args.slug:
            raise HarnessError("research round requires a <slug>")
        print(json.dumps(approve_round(ROOT, args.slug, getattr(args, "note", None)),
                         indent=2, ensure_ascii=False))
        print("Promotion remains explicit: specs/templates/intake-refinement.md")
        return 0
    raise HarnessError("research round requires subaction compile|advance|approve")


def _self_check() -> int:
    try:
        raise _rule(1, "self-check")
    except HarnessError as exc:
        assert "rule 1" in str(exc)
    workflow_schema.validate({"slug": "self-check"},
                             {"type": "object", "required": ["slug"],
                              "properties": {"slug": {"type": "string"}}})
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=ROOT / ".harness" / "runs", suffix=".md")
    os.close(fd)
    doc = Path(tmp)
    try:
        doc.write_text("# Round\n", encoding="utf-8")
        first = {"cohorts": [
            {"cohortId": "develop-a", "phase": "develop", "executor": "x", "model": "m",
             "wfid": "WF-20260101-000000-000001", "status": "running"},
            {"cohortId": "refine-b", "phase": "refine", "executor": "y", "model": "n",
             "wfid": "WF-20260101-000000-000002", "status": "running",
             "seed": "WF-20260101-000000-000001", "sourceCohortId": "develop-a"}]}
        _write_round_state(doc, first)
        assert research_index.parse_round(doc)["roundState"] == first
        second = {"cohorts": [{**row, "status": "done"} for row in first["cohorts"]]}
        _write_round_state(doc, second)
        text = doc.read_text(encoding="utf-8")
        assert text.count(_STATE_START) == 1
        assert research_index.parse_round(doc)["roundState"] == second
        second["deliver"] = {"approvedBy": "human", "note": "self-check"}
        _write_round_state(doc, second)
        assert research_index.parse_round(doc)["roundState"] == second
    finally:
        doc.unlink(missing_ok=True)
    print("research_round self-check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
