"""Read final numeric receipts and render an offline inference report. No LLM calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "tare.tools/inference-telemetry/1"


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def money(value):
    try:
        result = Decimal(str(value))
        return str(result) if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def known_sum(values):
    values = list(values)
    return sum(values) if values and all(value is not None for value in values) else None


def row(identity, group, origin, model, **fields):
    result = dict(id=identity, group=group, origin=origin, model=model or "Not reported", unit="call",
                  role=None, provider=None, status="UNKNOWN", started_at=None, duration_ms=None,
                  input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
                  reported_cost_usd=None, estimated_cost_usd=None, prompt_change=None, usage_source=None,
                  requested_model=None, observed_model=None)
    result.update(fields)
    for key in ("duration_ms", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        result[key] = number(result[key])
    for key in ("reported_cost_usd", "estimated_cost_usd"):
        result[key] = money(result[key])
    read, total = result["cache_read_tokens"], result["input_tokens"]
    if read is not None and total is not None and read > total:
        result["cache_read_tokens"] = read = None
    result["cache_status"] = "UNKNOWN" if read is None else "MISS" if read == 0 else "HIT"
    return result


def cache_usage(usage):
    """Read archived OpenAI-shaped receipts; no legacy host dependency."""
    usage = usage or {}
    details = usage.get('prompt_tokens_details') or {}
    return {'input_tokens': number(usage.get('prompt_tokens')),
            'output_tokens': number(usage.get('completion_tokens')),
            'cache_read_tokens': number(details.get('cached_tokens', usage.get('prompt_cache_hit_tokens'))),
            'cache_write_tokens': number(details.get('cache_write_tokens', details.get('cache_creation_tokens', usage.get('cache_creation_input_tokens')))),
            'reported_cost_usd': usage.get('cost')}


def native_cache_usage(call):
    from compute_plane.native_usage import UsageContract, normalize_usage
    contract = call.get('usage_contract')
    return normalize_usage(call.get('usage'), UsageContract(**contract) if contract else None)['tokens']


def call_rows(calls, identity, group, origin, *, role=None, provider=None):
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        cache = (native_cache_usage(call) if "effective_profile" in call else
                 call.get("cache") or cache_usage(call.get("usage")))
        yield row(f"{identity}:call:{i}", group, origin, call.get("observed_model") or call.get("runtime_model") or call.get("model"),
            role=role, provider=cache.get("served_provider") or provider,
            status="ERROR" if call.get("error") else "OK" if call.get("usage") or call.get("cache") else "UNKNOWN",
            started_at=call.get("started_at"), duration_ms=call.get("duration_ms"),
            input_tokens=cache.get("input_tokens"), output_tokens=cache.get("output_tokens"),
            cache_read_tokens=cache.get("cache_read_tokens"), cache_write_tokens=cache.get("cache_write_tokens"),
            reported_cost_usd=cache.get("reported_cost_usd"), estimated_cost_usd=cache.get("estimated_cost_usd"),
            prompt_change=(call.get("prompt") or {}).get("change"), usage_source="provider" if call.get("usage") else None)


def explorer_rows(data, group):
    identity = "explorer:" + data["request_id"]
    if data.get("model_observations"):
        yield from call_rows(data["model_observations"], identity, group, "explorer", role="explorer", provider="Local")
    else:
        tokens = (data.get("tokens") or {}) if data.get("token_source") else {}
        yield row(identity + ":aggregate", group, "explorer", data.get("model_requested", "openai/qwen38"),
            unit="attempt", role="explorer", provider="Local", status=data.get("status", "UNKNOWN"),
            duration_ms=data.get("duration_ms"), input_tokens=tokens.get("input"), output_tokens=tokens.get("output"),
            usage_source=data.get("token_source"))


def documents(source):
    if source.is_file():
        yield source
        return
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs if name not in {".git", "node_modules", "__pycache__", ".venv", "vendor"}
                         and not (base / name).is_symlink() and not (base / name / ".git").exists())
        for name in sorted(files):
            if name in {"session.json", "result.json", "mediation.json", "scribe.json", "usage.json", "run.json"} or (name.startswith("seat_") and name.endswith(".json")):
                path = base / name
                if not path.is_symlink():
                    yield path


def extract(data, path):
    schema = data.get("schema", "")
    if schema == "tare.tools/aider-session/1":
        group = data["id"]
        config = data.get("config", {})
        provider = "Local" if config.get("local_gateway") else config.get("model", "").split("/")[0]
        yield from call_rows(data.get("model_observations", []), "session:" + group, group, "managed",
                             role=config.get("role"), provider=provider)
        for delegated in data.get("delegation_observations", []):
            yield from explorer_rows(delegated, group)
    elif schema.startswith("tare.tools/audit-exploration-result/"):
        yield from explorer_rows(data, path.parent.parent.name)
    elif schema == "tare.tools/research-result/1":
        yield from call_rows(data.get("model_observations", []), "research:" + data["job_id"], data["job_id"],
                             "research", role="researcher", provider="Local")
    elif schema == "tare.tools/audit-result/1":
        metrics = data.get("metrics", {})
        usages = list((metrics.get("provider_token_usage") or {}).values())
        inputs = [known_sum(number(u.get(k)) for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")) for u in usages]
        field = lambda name: known_sum(number(u.get(name)) for u in usages)
        model = data.get("primary_model_reported") or data.get("primary_model_requested")
        if len(usages) > 1:
            model = "Several models (aggregated execution)"
        cost = metrics.get("cost_estimate_usd")
        duration = metrics.get("duration_ms")
        yield row("audit:" + data["case_id"] + ":" + data["run_id"], data["case_id"], "audit", model, unit="attempt",
            status=data.get("execution_status", "UNKNOWN"), role="auditor", provider="Vendor CLI / gateway",
            duration_ms=duration.get("value") if isinstance(duration, dict) else duration,
            input_tokens=known_sum(inputs), output_tokens=field("outputTokens"),
            cache_read_tokens=field("cacheReadInputTokens"), cache_write_tokens=field("cacheCreationInputTokens"),
            reported_cost_usd=metrics.get("billable_cost_usd"),
            estimated_cost_usd=cost.get("value") if isinstance(cost, dict) else cost,
            usage_source=metrics.get("provider_token_usage_source"))
    elif path.name.startswith("seat_") and "seat" in data:
        deployment = data.get("deployment") or {}
        attempts = deployment.get("semantic_attempts") or [deployment]
        group = next((p.parent.name for p in path.parents if p.name == "rounds"), path.parent.parent.name)
        for index, attempt in enumerate(attempts):
            identity = attempt.get("attempt_id") or f"{data.get('execution_nonce') or data.get('timestamp')}:{data.get('seat')}:{data.get('round')}:{deployment.get('deployment_id')}:{index}"
            yield row("round-table:" + group + ":" + identity, group, "round_table",
                attempt.get("served_model") if attempt.get("served_model") not in {None, "UNATTESTED"} else
                attempt.get("runtime_model") or data.get("model"), unit="attempt", role=data["seat"],
                status="ERROR" if attempt.get("failure_class") else "OK" if data.get("ok") else "UNKNOWN",
                started_at=data.get("timestamp"), provider=deployment.get("provider"),
                duration_ms=data.get("latency_ms") if len(attempts) == 1 else None)
    elif path.name in {"mediation.json", "scribe.json"}:
        for attempt in data.get("attempts", []):
            if not attempt.get("receipt"):
                continue  # Circuit-open skips and deterministic compilers made no model request.
            role = "mediator" if path.name == "mediation.json" else "scribe"
            yield from extract({"seat": role, "deployment": attempt["receipt"], "ok": attempt.get("ok"),
                                "timestamp": data.get("timestamp")}, path.with_name("seat_" + role + ".json"))
    elif schema == "tare.tools/managed-cache-qualification/1" and "final_routes" in data:
        for provider, route in data["final_routes"].items():
            for case in route["cases"]:
                identity = f"qualification:{data['date']}:{provider}:{case['case']}"
                yield from call_rows(case.get("calls", []), identity, "Qualification " + data["date"], "qualification", provider=provider)
    elif schema == "tare-native-usage/1":
        session_id = scalar(data, "session_id", required=True)
        binding = mapping(data.get("binding"))
        work_id = scalar(binding, "work_id")
        work_id = None if work_id and work_id.startswith("session:") else work_id
        calls = data.get("calls", [])
        if not isinstance(calls, list):
            raise ValueError("invalid_native_calls")
        for call in calls:
            call = mapping(call)
            call_id = scalar(call, "call_id", required=True)
            tokens = mapping(mapping(call.get("normalized")).get("tokens"))
            observation = mapping(call.get("observation"))
            cost = mapping(call.get("cost"))
            amount = money(cost.get("known_amount"))
            complete_usd = cost.get("currency") == "USD" and cost.get("complete") is True
            observed = scalar(observation, "observed_model")
            requested = scalar(observation, "requested_model")
            status = scalar(observation, "status")
            projected_status = ("ERROR" if observation.get("error") else
                                "OK" if status == "complete" else
                                "NOT_DISPATCHED" if status == "not_started" else "UNKNOWN")
            yield row("native-usage:" + session_id + ":" + call_id, work_id or session_id,
                "native_usage", observed or requested, status=projected_status,
                duration_ms=observation.get("duration_ms"), input_tokens=tokens.get("input_tokens"),
                output_tokens=tokens.get("output_tokens"), cache_read_tokens=tokens.get("cache_read_tokens"),
                cache_write_tokens=tokens.get("cache_write_tokens"),
                estimated_cost_usd=amount if complete_usd else None, usage_source=scalar(call, "usage_source"),
                requested_model=requested, observed_model=observed, observation_status=status,
                session_id=session_id, call_id=call_id, work_id=work_id,
                purpose=scalar(call, "purpose"), protocol=scalar(observation, "protocol"))


def mapping(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid_receipt_mapping")
    return value


def scalar(value, key, *, required=False):
    item = value.get(key)
    if item is None and not required:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError("invalid_receipt_identity")
    return item


WORK_METADATA = ("repository_root", "task_class", "runtime_identity", "recorded_audit_outcome")


def declared_metadata(value):
    """Only bounded, printable declarations; malformed optional data stays null."""
    return value if (type(value) is str and value.strip() and len(value) <= 1024
                     and all(c.isprintable() for c in value)) else None


def project_work_run(data, *, include_work_metadata=False):
    """Project typed scalar fields, never arbitrary nested owner payloads."""
    result = {key: scalar(data, key, required=key == "run_id") for key in
              ("run_id", "work_id", "train_id", "status", "stage", "started_at", "ended_at",
               "candidate_commit", "resumed_from")}
    if include_work_metadata:
        result.update({key: declared_metadata(data.get(key)) for key in WORK_METADATA})
    landing, admission = mapping(data.get("landing")), mapping(data.get("admission"))
    verdict = scalar(admission, "verdict")
    result.update(duration_ms=number(data.get("duration_ms")),
                  landed=landing.get("landed") if type(landing.get("landed")) is bool else None,
                  admitted=None if verdict is None else verdict == "ADMITTED")
    result["settlement_confirmed"] = result["status"] == "SETTLED" and result["landed"] is True
    claims = data.get("inference_calls", [])
    if not isinstance(claims, list):
        raise ValueError("invalid_inference_calls")
    result["inference_calls"] = sorted(
        [dict(zip(("session_id", "call_id", "stage"), values)) for values in {
            tuple(scalar(mapping(claim), key, required=True) for key in ("session_id", "call_id", "stage"))
            for claim in claims}], key=lambda value: (value["session_id"], value["call_id"], value["stage"]))
    result["inference_observation_failures"] = number(data.get("inference_observation_failures"))
    return result


def relate_work_runs(work_runs):
    """Resolve only explicit, matching, acyclic origin chains; never infer a cost join."""
    for run in work_runs.values():
        cursor, visited = run, set()
        valid = True
        while True:
            if cursor["run_id"] in visited or cursor["status"] == "CONTRADICTORY":
                valid = False
                break
            visited.add(cursor["run_id"])
            origin = cursor["resumed_from"]
            if origin is None:
                break
            prior = work_runs.get(origin)
            if not prior or any(not cursor[key] or cursor[key] != prior[key] for key in ("work_id", "train_id")):
                valid = False
                break
            cursor = prior
        run["lineage"] = ("original" if run["resumed_from"] is None else "resolved") if valid else "unresolved"
        run["resume_origin_run_id"] = run["resumed_from"] if valid else None
        run["execution_origin_run_id"] = cursor["run_id"] if valid else None
        run["inference_link"] = "unknown"  # A shared Work ID is not an attempt/call association.
    settled = {}
    for run in work_runs.values():
        root = run["execution_origin_run_id"]
        if root and run["settlement_confirmed"]:
            settled.setdefault(root, []).append(run["run_id"])
    for run in work_runs.values():
        root = run["execution_origin_run_id"]
        run["execution_settled"] = bool(settled.get(root)) if root else None
        run["settled_run_ids"] = sorted(settled.get(root, []))
    return sorted(work_runs.values(), key=lambda r: (r["started_at"] or "", r["run_id"]))



def link_work_calls(records, runs, warnings):
    """Join only explicit host-recorded call identities. Missing usage stays unknown."""
    by_id = {r["id"]: r for r in records}
    owners = {}
    for run in runs:
        run["inference_conflicts"] = 0
        for claim in run["inference_calls"]:
            identity = "native-usage:" + claim["session_id"] + ":" + claim["call_id"]
            owners.setdefault(identity, []).append((run, claim))
    for identity, claims in owners.items():
        run, claim = claims[0]
        item = by_id.get(identity)
        if item is None:
            item = row(identity, run["work_id"] or "UNKNOWN", "native_usage", None,
                       session_id=claim["session_id"], call_id=claim["call_id"],
                       usage_receipt_missing=True)
            records.append(item)
            by_id[identity] = item
        ambiguous = (len(claims) != 1 or not run["work_id"] or not run["train_id"]
                     or run["status"] == "CONTRADICTORY" or item["status"] == "CONTRADICTORY"
                     or item.get("work_id") not in {None, run["work_id"]})
        if ambiguous:
            item["work_link"] = "contradictory"
            for owner, _ in claims:
                owner["inference_conflicts"] += 1
            warnings.append({"source": "Work attribution", "reason": "contradictory_call_owner"})
            continue
        item.update(work_link="recorded", work_run_id=run["run_id"],
                    work_id=run["work_id"], work_stage=claim["stage"],
                    execution_origin_run_id=run["execution_origin_run_id"])

    def usage(rows):
        summary = summarize(rows)
        return {"calls": len(rows), "usage_receipts_missing": sum(r.get("usage_receipt_missing", False) for r in rows),
                "input_tokens": summary["input_tokens"] if summary["input_records"] else None,
                "output_tokens": summary["output_tokens"] if summary["output_records"] else None,
                "input_records": summary["input_records"], "output_records": summary["output_records"],
                "reported_cost_usd": summary["reported_cost_usd"], "estimated_cost_usd": summary["estimated_cost_usd"],
                "cost_records": summary["cost_records"], "unknown_cost_calls": len(rows)-summary["cost_records"],
                "scope": "Explicitly attributed calls only; not proof of complete Work cost coverage"}
    for run in runs:
        selected = [r for r in records if r.get("work_run_id") == run["run_id"]]
        run["inference_link"] = ("partial" if run["inference_conflicts"] or run["inference_observation_failures"]
                                  else "recorded") if selected else "unknown"
        run["inference_usage"] = usage(selected)
        root = run["execution_origin_run_id"]
        run["execution_inference_usage"] = usage([r for r in records
            if root and r.get("execution_origin_run_id") == root])


def summarize(rows):
    def values(key):
        return [r[key] for r in rows if r[key] is not None]
    paired = [r for r in rows if r["input_tokens"] is not None and r["cache_read_tokens"] is not None]
    paired_input = sum(r["input_tokens"] for r in paired)
    paired_read = sum(r["cache_read_tokens"] for r in paired)
    latencies = sorted(r["duration_ms"] for r in rows if r["unit"] == "call" and r["duration_ms"] is not None)
    reported = values("reported_cost_usd")
    estimated = [r["estimated_cost_usd"] for r in rows if r["reported_cost_usd"] is None and r["estimated_cost_usd"] is not None]
    return {"records": len(rows), "calls": sum(r["unit"] == "call" for r in rows),
        "aggregated_executions": sum(r["unit"] != "call" for r in rows),
        "errors": sum(r["status"] in {"ERROR", "FAILED", "TIMED_OUT", "CANCELLED", "BUDGET_STOP"} for r in rows),
        "input_tokens": sum(values("input_tokens")), "output_tokens": sum(values("output_tokens")),
        "input_records": len(values("input_tokens")), "output_records": len(values("output_tokens")),
        "cache_read_tokens": sum(values("cache_read_tokens")), "cache_write_tokens": sum(values("cache_write_tokens")),
        "cache_records": len(values("cache_read_tokens")), "cache_paired_records": len(paired),
        "cache_paired_input_tokens": paired_input, "cache_reuse_ratio": paired_read / paired_input if paired_input else None,
        "cache_hits": sum(r["cache_status"] == "HIT" for r in rows),
        "cache_misses": sum(r["cache_status"] == "MISS" for r in rows),
        "reported_cost_usd": str(sum(map(Decimal, reported), Decimal(0))) if reported else None,
        "estimated_cost_usd": str(sum(map(Decimal, estimated), Decimal(0))) if estimated else None,
        "cost_records": len(reported) + len(estimated),
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
        "latency_calls": len(latencies)}


def collect(sources, *, include_work_metadata=False):
    rows, warnings, seen, work_runs = {}, [], set(), {}
    native_conflicts, work_conflicts = set(), set()
    # A parent session supplies the delegation relationship; prefer it over
    # the same explorer's standalone result without counting that call twice.
    parents = []
    for source in sources:
        if not source.exists():
            warnings.append({"source": source.name, "reason": "source_not_found"})
            continue
        for path in documents(source):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("receipt_exceeds_32mb")
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                relative = str(path.relative_to(source)) if source.is_dir() else path.name
                if data.get("schema") == "tare.tools/work-run/1":
                    run = project_work_run(data, include_work_metadata=include_work_metadata)
                    prior = work_runs.get(run["run_id"])
                    if run["run_id"] in work_conflicts:
                        prior["inference_calls"] = project_work_run({"run_id": run["run_id"],
                            "inference_calls": prior["inference_calls"] + run["inference_calls"]},
                            include_work_metadata=include_work_metadata)["inference_calls"]
                        continue
                    if prior is not None and prior != run:
                        warnings.append({"source": path.name, "reason": "contradictory_work_run"})
                        work_conflicts.add(run["run_id"])
                        run = project_work_run({"run_id": run["run_id"], "status": "CONTRADICTORY",
                            "inference_calls": prior["inference_calls"] + run["inference_calls"]},
                            include_work_metadata=include_work_metadata)
                    work_runs[run["run_id"]] = run
                    continue
                items = list(extract(data, path))
                for item in items:
                    item["receipt"] = relative
                    previous = rows.get(item["id"])
                    if item["id"] in native_conflicts:
                        continue
                    semantic = lambda value: {k: v for k, v in value.items() if k != "receipt"}
                    if (item["origin"] == "native_usage" and previous is not None
                            and semantic(previous[1]) != semantic(item)):
                        warnings.append({"source": path.name, "reason": "contradictory_native_usage"})
                        native_conflicts.add(item["id"])
                        item = row(item["id"], "UNKNOWN", "native_usage", None, status="CONTRADICTORY",
                                   requested_model=None, observed_model=None, receipt=relative)
                        rows[item["id"]] = (path.stat().st_mtime, item)
                    elif previous is None or path.stat().st_mtime > previous[0]:
                        rows[item["id"]] = (path.stat().st_mtime, item)
                if data.get("schema") == "tare.tools/aider-session/1":
                    parents.extend(("explorer:" + d["request_id"], data["id"])
                                   for d in data.get("delegation_observations", []))
                elif data.get("schema") == "tare.tools/audit-result/1":
                    parents.extend(("explorer:" + d["request_id"], data["case_id"]) for d in
                                   data.get("metrics", {}).get("exploration", {}).get("reports", []))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                warnings.append({"source": path.name, "reason": type(error).__name__})
    records = [value[1] for value in rows.values()]
    for prefix, group in parents:
        for record in records:
            if record["id"].startswith(prefix + ":"):
                record["group"] = group
    # A newer exact per-call record supersedes a copied legacy aggregate.
    exact_explorers = {r["id"].split(":call:")[0] for r in records if r["origin"] == "explorer" and r["unit"] == "call"}
    records = [r for r in records if not (r["unit"] == "attempt" and r["id"].removesuffix(":aggregate") in exact_explorers)]
    records.sort(key=lambda r: (r["started_at"] or "", r["id"]), reverse=True)
    runs = relate_work_runs(work_runs)
    link_work_calls(records, runs, warnings)
    run_summary = {"attempts": len(runs), "settled_and_landed": sum(r["settlement_confirmed"] for r in runs),
                   "unresolved_lineage": sum(r.get("lineage") == "unresolved" for r in runs),
                   "inference_linked_attempts": sum(r["inference_usage"]["calls"] > 0 for r in runs)}
    return {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summarize(records), "rows": records, "work_runs": runs, "work_run_summary": run_summary, "warnings": warnings,
            "notes": ["Only numeric fields and receipt identities; prompts and conversations are not exported.",
                      "A missing counter is unknown, never zero. CLI aggregates are not counted as individual calls.",
                      "Reported and estimated costs are kept apart. Subscription and local compute costs are not assumed.",
                      "The cache ratio uses only records whose total input and cache reads are known.",
                      "Work calls link only through explicit host records. Execution totals across resumed attempts are overlapping views, not additive totals."]}


def completed_work_review(report, limit=10):
    """Pure, bounded descriptive view of explicit execution roots and call owners."""
    if type(limit) is not int or limit < 1:
        raise ValueError("review limit must be a positive integer")
    limit = min(limit, 10)
    groups, exclusions, reason_counts = {}, [], {}

    def exclude(attempts, reasons):
        for attempt in attempts:
            exclusions.append({"run_id": attempt.get("run_id"),
                               "execution_origin_run_id": attempt.get("execution_origin_run_id"),
                               "reasons": reasons[:]})
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    def instant(value):
        if not isinstance(value, str):
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
        except (ValueError, OverflowError):
            return None

    for run in report.get("work_runs", []):
        reasons = []
        if run.get("status") == "CONTRADICTORY":
            reasons.append("contradictory_identity")
        if (run.get("lineage") not in {"original", "resolved"}
                or not run.get("execution_origin_run_id")
                or not run.get("work_id") or not run.get("train_id")):
            reasons.append("unresolved_identity")
        if reasons:
            exclude([run], reasons)
        else:
            groups.setdefault(run["execution_origin_run_id"], []).append(run)

    eligible, not_completed = [], 0
    for root, attempts in groups.items():
        settled = [r for r in attempts if r.get("settlement_confirmed") is True]
        reasons = []
        if any(r.get("inference_conflicts", 0) for r in attempts):
            reasons.append("contradictory_call_identity")
        dates = []
        for run in settled:
            start, end = instant(run.get("started_at")), instant(run.get("ended_at"))
            if start is None or end is None or end < start:
                reasons.append("uncertain_settlement_date")
                break
            dates.append(end)
        if reasons:
            exclude(attempts, reasons)
        elif settled:
            # A later Resume of an already landed execution is not a new delivery
            # and must not move its first recorded closure up the recency order.
            eligible.append((min(dates), root, attempts))
        else:
            not_completed += 1
    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)

    attempt_fields = ("run_id", "work_id", "train_id", "status", "stage",
                      "started_at", "ended_at", "duration_ms", "candidate_commit",
                      "resumed_from", "lineage", "landed", "admitted",
                      "settlement_confirmed", "inference_link", "inference_observation_failures") + WORK_METADATA
    call_fields = ("id", "session_id", "call_id", "work_run_id", "work_stage",
                   "status", "observation_status", "requested_model", "observed_model",
                   "purpose", "protocol", "usage_source", "duration_ms",
                   "input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_write_tokens", "reported_cost_usd", "estimated_cost_usd",
                   "usage_receipt_missing")

    def scalars(item, fields):
        return {key: item.get(key) if type(item.get(key)) in (str, int, float, bool)
                else None for key in fields}

    rows = []
    for ended, root, attempts in eligible[:limit]:
        attempt_ids = {r["run_id"] for r in attempts}
        # Never add overlapping execution_inference_usage snapshots. collect()
        # has already deduplicated independent native call identities.
        calls = [r for r in report.get("rows", [])
                 if r.get("work_link") == "recorded"
                 and r.get("execution_origin_run_id") == root
                 and r.get("work_run_id") in attempt_ids]
        summary = summarize(calls)
        missing = sum(r.get("usage_receipt_missing") is True for r in calls)
        coverage = ("recorded" if all(r.get("inference_link") == "recorded"
                    for r in attempts) and not missing else "partial") if calls else "unknown"
        metadata = {}
        for key in WORK_METADATA:
            declarations = {declared_metadata(r.get(key)) for r in attempts} - {None}
            metadata[key] = next(iter(declarations)) if len(declarations) == 1 else None
        projected_attempts = []
        for attempt in sorted(attempts, key=lambda r: r["run_id"]):
            projected = scalars(attempt, attempt_fields)
            projected.update({key: declared_metadata(attempt.get(key)) for key in WORK_METADATA})
            projected_attempts.append(projected)
        rows.append({
            "execution_origin_run_id": root, "work_id": attempts[0]["work_id"],
            "train_id": attempts[0]["train_id"], "settled_at": ended.isoformat(),
            "settled_run_ids": sorted(r["run_id"] for r in attempts if r["settlement_confirmed"]),
            "attempts": projected_attempts,
            "attributed_calls": [scalars(r, call_fields) for r in sorted(calls, key=lambda r: r["id"])],
            "recorded_attributed_call_count": len(calls), "coverage": coverage,
            "usage_receipts_missing": missing,
            "recorded_input_tokens": summary["input_tokens"] if summary["input_records"] else None,
            "recorded_output_tokens": summary["output_tokens"] if summary["output_records"] else None,
            "input_records": summary["input_records"], "output_records": summary["output_records"],
            "reported_cost_usd": summary["reported_cost_usd"],
            "estimated_cost_usd": summary["estimated_cost_usd"],
            "unknown_cost_calls": len(calls) - summary["cost_records"],
            # Recorded attribution is not a completeness certificate.
            "full_call_count": None, "full_input_tokens": None, "full_output_tokens": None,
            "full_cost_usd": None, "subscription_quota": None,
            "control_tool_infrastructure_cost_usd": None, "human_cost_usd": None,
            **metadata, "code_quality": None, "independent_audit": None,
            "settlement_confirmed": True})
    return {"target_count": limit, "selected_count": len(rows),
            "remaining": limit - len(rows),
            "cohort_status": "complete" if len(rows) == limit else "partial",
            "eligible_execution_count": len(eligible),
            "not_completed_execution_count": not_completed,
            "excluded_record_count": len(exclusions), "excluded_reason_counts": reason_counts,
            "exclusions": exclusions, "rows": rows, "model_preference": None,
            "notes": ["Descriptive only; recorded settlement confirms closure, not code quality or independent audit.",
                      "Explicitly attributed calls only. Cumulative execution views are not additive; full coverage is unknown.",
                      "Reported USD and additional estimates are separate, and absent evidence remains null.",
                      "Subscription quota, control/tool/infrastructure and human costs are outside recorded model charges and unknown.",
                      "No model calls or vendor lookup; no observation minimum authorizes Work. No model preference is recommended or activated."]}


def provider_pilot_review(report):
    """Describe at most five distinct Works in the latest ten-execution window."""
    base = completed_work_review(report, limit=10)
    groups = {}
    for execution in base['rows']:
        identity = (execution['work_id'], execution['train_id'])
        groups.setdefault(identity, []).append(execution)
    rows = []
    counts = {key: {'known_attempts': 0, 'missing_attempts': 0,
                    'conflicting_works': 0} for key in WORK_METADATA}
    for (work_id, train_id), executions in list(groups.items())[:5]:
        item = {'work_id': work_id, 'train_id': train_id,
                # Keep base evidence intact; execution costs are not additive.
                'executions': executions}
        attempts = [attempt for execution in executions for attempt in execution['attempts']]
        for key in WORK_METADATA:
            values = [attempt.get(key) for attempt in attempts]
            declarations = set(values) - {None}
            item[key] = next(iter(declarations)) if len(declarations) == 1 else None
            counts[key]['known_attempts'] += sum(value is not None for value in values)
            counts[key]['missing_attempts'] += sum(value is None for value in values)
            counts[key]['conflicting_works'] += int(len(declarations) > 1)
        rows.append(item)
    return {'target_count': 5, 'selected_count': len(rows), 'remaining': 5 - len(rows),
            'window_limit': 10, 'window_execution_count': len(base['rows']),
            'distinct_work_count_in_window': len(groups), 'rows': rows,
            'metadata_counts': counts,
            'source_review': {key: value for key, value in base.items() if key != 'rows'},
            'qualification': None, 'representativeness': None, 'provider_equivalence': None,
            'provider': None, 'code_quality': None, 'independent_audit': None,
            'model_preference': None, 'authorization': None,
            'notes': [
                'Descriptive recent candidates only, not a proven representative selection.',
                'At most five work_id + train_id identities within ten completed executions; resumes and repeat executions are not extra deliveries.',
                'Attempt metadata and linked failures are retained. Missing evidence is unknown, not zero; execution costs must not be added.',
                'No vendor inferred from model/alias. SETTLED, ACCEPT, ADMITTED and five rows prove neither quality, independence, qualification, equivalence, preference nor authorization.',
                'Next: observe/review ordinary Works and justify representativeness before concluding FLOW06; existing explicit --source receipts can delimit the selection without duplicate tasks.']}


def render_html(report):
    template = Path(__file__).with_name("telemetry_report.html").read_text(encoding="utf-8")
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return template.replace("__TELEMETRY_DATA__", payload)


def terminal(report):
    s = report["summary"]
    work = report.get("work_run_summary", {})
    ratio = f"{s['cache_reuse_ratio']:.1%}" if s["cache_reuse_ratio"] is not None else "unknown"
    usd = lambda v: "unknown" if v is None else "$" + format(Decimal(v), ".6f")
    lines = ["tare.tools · Inference telemetry", "",
        f"{s['calls']} calls observed | {s['aggregated_executions']} aggregated executions | {s['errors']} failures",
        f"Recorded input: {s['input_tokens']:,} | Recorded output: {s['output_tokens']:,}",
        f"Cache: {ratio} of the input eligible for measurement | {s['cache_hits']} hits | {s['cache_misses']} misses",
        f"Cache coverage: {s['cache_records']}/{s['records']} records; cost: {s['cost_records']}/{s['records']}",
        f"Reported cost: {usd(s['reported_cost_usd'])} | Additional estimate: {usd(s['estimated_cost_usd'])}",
        f"Work attempts: {work.get('attempts', 0)} | settled and landed: {work.get('settled_and_landed', 0)} | unresolved lineage: {work.get('unresolved_lineage', 0)}", ""]
    for run in report.get("work_runs", []):
        lines.append(f"  Work {run['work_id'] or 'unknown'} / {run['run_id']}: {run['status'] or 'UNKNOWN'}"
                     f" | lineage={run['lineage']} | execution settled={run['execution_settled']}"
                     f" | attributed calls={run['inference_usage']['calls']} | unknown cost calls={run['inference_usage']['unknown_cost_calls']}"
                     f" | reported USD={usd(run['inference_usage']['reported_cost_usd'])}"
                     f" | estimated USD={usd(run['inference_usage']['estimated_cost_usd'])}")
    for model in sorted({r["model"] for r in report["rows"]}):
        subset = summarize([r for r in report["rows"] if r["model"] == model])
        cached = f"{subset['cache_read_tokens']:,}" if subset['cache_records'] else "unknown"
        lines.append(f"  {model}: {subset['calls']} calls / {subset['aggregated_executions']} aggregates; cache read {cached}")
    review = report.get("completed_work_review")
    if review is not None:
        lines.extend(["", f"Completed Work review: {review['selected_count']}/{review['target_count']} unique executions"
                      f" | {review['cohort_status']} | remaining={review['remaining']}",
                      f"  Excluded records: {review['excluded_record_count']} | reasons={review['excluded_reason_counts']}"])
        for execution in review["rows"]:
            lines.append(f"  {execution['execution_origin_run_id']}: attempts={len(execution['attempts'])}"
                         f" | attributed calls={execution['recorded_attributed_call_count']} | coverage={execution['coverage']}"
                         f" | reported USD={usd(execution['reported_cost_usd'])}"
                         f" | additional estimated USD={usd(execution['estimated_cost_usd'])} | full cost=unknown")
        lines.append("  Descriptive closure only; quality/audit unknown. No Work observation minimum or model preference.")
    pilot = report.get('provider_pilot_review')
    if pilot is not None:
        lines.extend(['', f"Provider pilot: {pilot['selected_count']}/5 distinct settled Work candidates",
                      f"  Descriptive window: {pilot['window_execution_count']}/{pilot['window_limit']} executions; remaining={pilot['remaining']}",
                      f"  Metadata facts/gaps: {pilot['metadata_counts']}",
                      '  Qualification / representativeness / provider equivalence: Unknown.'])
        for item in pilot['rows']:
            lines.append(f"  Work {item['work_id']} / train {item['train_id']}: "
                         + ' | '.join(key+'='+str(item[key] if item[key] is not None else 'Unknown')
                                      for key in WORK_METADATA))
        lines.append('  '+pilot['notes'][-1])
    if report["warnings"]:
        lines.append(f"\n{len(report['warnings'])} source(s) unavailable or invalid; details in the JSON/HTML.")
    return "\n".join(lines)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", type=Path, help="Receipt or directory; repeat to combine sources")
    parser.add_argument("--html", type=Path, help="Write a self-contained interactive report")
    parser.add_argument("--json", type=Path, help="Write numeric telemetry without transcripts")
    parser.add_argument("--review-completed-works", action="store_true",
                        help="Describe up to ten unique completed executions (requires explicit --source)")
    parser.add_argument('--review-provider-pilot', action='store_true',
                        help='Describe up to five distinct Works within ten completed executions (requires explicit --source)')
    args = parser.parse_args(argv)
    if not args.source:
        if args.review_completed_works:
            parser.error("--review-completed-works requires explicit --source")
        if args.review_provider_pilot:
            parser.error('--review-provider-pilot requires explicit --source')
    report = collect(args.source or [ROOT / "relay/round_tables", ROOT.parent / ".reconciliation"],
                     include_work_metadata=args.review_completed_works or args.review_provider_pilot)
    if args.review_completed_works:
        report["completed_work_review"] = completed_work_review(report)
    if args.review_provider_pilot:
        report['provider_pilot_review'] = provider_pilot_review(report)
    for path, text in ((args.html, render_html(report) if args.html else None),
                       (args.json, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) if args.json else None)):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
    print(terminal(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
