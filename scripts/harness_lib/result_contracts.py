"""HARNESS_RESULT/WORKER_RESULT contract extraction and validation (extracted from harness.py, MF.1).

Shared harness helpers/constants arrive via bind(env) at import time —
the same idiom as harness_lib.workflow_reduce and the gate_fixtures modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

# CQ.2: the shared oracle exit-class vocabulary — defined ONCE here; the JSON
# schema (schemas/worker-result.schema.json) mirrors it.
# CQ.5 landed 2026-07-13 (mutation_probe.py): killed/survived joined as planned.
EXIT_CLASSES = ("passed", "failed", "error", "timeout", "skipped", "killed", "survived")

# W29.N4: the shared test-quality signal vocabulary — defined ONCE here, the
# EXIT_CLASSES precedent, BEFORE any producer writes them. SEPARATE signals,
# never a composite score (the W29 round REJECTED a single quality score).
# The mutation/canary half already persists keyed by EXIT_CLASSES (W29.A3);
# the AST proxies (W29.N5) write exactly these keys; N6 gate checks and
# EXP-32 read them. flakinessCandidates counts fs/net/clock/random/order hits.
TEST_QUALITY_SIGNALS = ("assertionDensity", "edgeCaseHits", "nullSafetyHits",
                        "flakinessCandidates")


def validate_test_quality_signals(data: Any) -> list[str]:
    """W29.N4 "1 schema, 0 drift": a signals dict carries KNOWN keys -> numbers.
    An unknown key is the drift this schema exists to refuse; a missing key is
    fine (signals stay separate and optional). Booleans are refused — a signal
    is a measurement, not a verdict."""
    if not isinstance(data, dict):
        return ["testQualitySignals must be a dict"]
    errors = [f"unknown test-quality signal: {k}" for k in data
              if k not in TEST_QUALITY_SIGNALS]
    errors += [f"test-quality signal {k} must be a number" for k, v in data.items()
               if k in TEST_QUALITY_SIGNALS
               and (isinstance(v, bool) or not isinstance(v, (int, float)))]
    return errors


def bind(env: dict[str, Any]) -> None:
    """Receive shared runtime helpers/constants from scripts/harness.py."""
    globals().update(env)


def normalize_approved_by(v: Any) -> dict[str, Any] | None:
    """SPEC-161 N-AUTHCHAIN read-side promotion: a legacy `approvedBy` STRING becomes a
    typed actor record `{actorType: "user", identity: <s>}`; a dict passes through
    unchanged; None stays None. READ-ONLY -- never rewrites stored state (the validator
    validates shape only). Anything else reads as None (no fabrication)."""
    if v is None:
        return None
    if isinstance(v, str):
        return {"actorType": "user", "identity": v}
    if isinstance(v, dict):
        return v
    return None


def _validate_friction_observations(data: dict[str, Any], label: str) -> str | None:
    """SE.4: optional list of short friction notes (≤10 items, ≤200 chars each)."""
    obs = data.get("frictionObservations")
    if obs is None:
        return None
    if (not isinstance(obs, list) or len(obs) > 10
            or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in obs)):
        return f"{label} frictionObservations must be a list of at most 10 non-empty strings (≤200 chars each)"
    return None


def extract_json_object(text: str, required_key: str) -> dict[str, Any] | None:
    fenced = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = list(fenced)
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    # Balanced-brace fallback for nested JSON objects emitted without fences.
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        in_string = False
        escape = False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:pos + 1]
                        if f'"{required_key}"' in candidate:
                            candidates.append(candidate)
                        break
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and required_key in data:
                return data
        except Exception:
            continue
    return None

def extract_harness_result(text: str) -> dict[str, Any] | None:
    m = re.search(r"##\s*HARNESS_RESULT\s*```json\s*(\{.*?\})\s*```", text, flags=re.S)
    if not m:
        m = re.search(r"```json\s*(\{[^`]*\"status\"[^`]*\})\s*```", text, flags=re.S)
    if not m:
        return None
    return json.loads(m.group(1))

def validate_result_file(path: Path) -> None:
    data = extract_harness_result(read_text(path))
    if not data:
        raise HarnessError("No HARNESS_RESULT JSON block found")
    required = ["status", "summary", "filesChanged", "testsRun", "failedTests", "coverage", "contextDiscovery", "nextStep"]
    missing = [k for k in required if k not in data]
    if missing:
        raise HarnessError("HARNESS_RESULT missing keys: " + ", ".join(missing))
    if data.get("status") not in {"done", "partial", "blocked", "failed"}:
        raise HarnessError("HARNESS_RESULT status must be one of done|partial|blocked|failed")
    if not isinstance(data.get("testsRun"), list):
        raise HarnessError("HARNESS_RESULT testsRun must be a list")
    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        raise HarnessError("HARNESS_RESULT coverage must be an object")
    allowed_coverage = {"not_applicable", "not_configured", "not_run", "manual", "pass", "fail"}
    if coverage.get("status") not in allowed_coverage:
        raise HarnessError("HARNESS_RESULT coverage.status must be one of " + ", ".join(sorted(allowed_coverage)))

    # SPEC-103 M3.2: optional acceptance-criteria round-trip (proposed -> approved -> graded).
    ac = data.get("acceptanceCriteria")
    if ac is not None:
        if not isinstance(ac, dict):
            raise HarnessError("HARNESS_RESULT acceptanceCriteria must be an object with proposed/approvedBy/graded")
        proposed = ac.get("proposed")
        if not isinstance(proposed, list) or not proposed or not all(isinstance(x, str) and x.strip() for x in proposed):
            raise HarnessError("HARNESS_RESULT acceptanceCriteria.proposed must be a non-empty list of criteria strings")
        # SPEC-161 N-AUTHCHAIN: approvedBy tolerates the legacy STRING or a typed actor
        # record ({actorType in ACTOR_TYPES, non-empty identity}). Validator checks SHAPE
        # only -- legacy state is never rewritten; readers promote via normalize_approved_by.
        approved = ac.get("approvedBy")
        if approved is not None and not isinstance(approved, str):
            from harness_lib import responsibility
            if (not isinstance(approved, dict)
                    or approved.get("actorType") not in responsibility.ACTOR_TYPES
                    or not str(approved.get("identity") or "").strip()):
                raise HarnessError("HARNESS_RESULT acceptanceCriteria.approvedBy must be a string (legacy) "
                                   "or a typed actor record with actorType in {user,worker,overseer,automation} "
                                   "and a non-empty identity, or null")
        graded = ac.get("graded")
        if graded is not None:
            if not isinstance(graded, list) or any(
                not isinstance(g, dict) or not str(g.get("criterion") or "").strip() or g.get("verdict") not in {"pass", "fail"}
                for g in graded
            ):
                raise HarnessError("HARNESS_RESULT acceptanceCriteria.graded items need a criterion and a verdict of pass|fail")
        if data.get("status") == "done":
            if not graded:
                raise HarnessError("HARNESS_RESULT status done under an acceptance contract requires graded criteria (proposed -> approved -> graded)")
            failed = [str(g.get("criterion")) for g in graded if g.get("verdict") == "fail"]
            if failed:
                raise HarnessError("HARNESS_RESULT cannot be done with failed acceptance criteria (" + ", ".join(failed[:5]) + "); use partial|blocked and escalate — waiving is a human decision")
    context_discovery = data.get("contextDiscovery")
    if not isinstance(context_discovery, dict):
        raise HarnessError("HARNESS_RESULT contextDiscovery must be an object")
    graphify = context_discovery.get("graphify")
    if not isinstance(graphify, dict):
        raise HarnessError("HARNESS_RESULT contextDiscovery.graphify must be an object")
    allowed_graph = {"used", "not_available", "stale", "not_applicable", "skipped"}
    if graphify.get("status") not in allowed_graph:
        raise HarnessError("HARNESS_RESULT contextDiscovery.graphify.status must be one of " + ", ".join(sorted(allowed_graph)))
    if not isinstance(graphify.get("queries", []), list) or not isinstance(graphify.get("sourceFilesVerified", []), list):
        raise HarnessError("HARNESS_RESULT contextDiscovery.graphify queries/sourceFilesVerified must be lists")
    friction_error = _validate_friction_observations(data, "HARNESS_RESULT")
    if friction_error:
        raise HarnessError(friction_error)
    append_event("harness_result_validated", {
        "status": data.get("status"),
        "testsRun": len(data.get("testsRun", [])),
        "coverageStatus": coverage.get("status"),
        "graphifyStatus": graphify.get("status"),
        "requiresEscalation": data.get("requiresEscalation", False),
        "suggestedProfile": data.get("suggestedProfile"),
        "taskId": data.get("taskId"),
        "reason": data.get("reason"),
        "failureClass": data.get("failureClass"),
        # esc-subject-scope D1: the escalation must belong to exactly one
        # subject ("self" or a target name); absent stays legacy-self.
        "target": data.get("target"),
        # SE.4: friction rides the validation event into the supervision ledger
        # so the self-review recurrence rule survives gate cleanups.
        "frictionObservations": data.get("frictionObservations") or [],
    })
    print(json.dumps(data, indent=2, ensure_ascii=False))

def extract_worker_result(text: str) -> dict[str, Any] | None:
    return extract_json_object(text, "workerId")

SUMMARY_MAX_CHARS = 1000
# Mirror schemas/worker-result.schema.json#/properties/frictionObservations exactly:
# bounding to a different number than the schema enforces would just move the failure.
FRICTION_MAX_ITEMS = 10
FRICTION_MAX_CHARS = 200


def bound_soft_fields(data: dict[str, Any], *, summary_max: int = SUMMARY_MAX_CHARS) -> list[str]:
    """Truncate-and-warn on soft display fields in place: ``summary`` + ``frictionObservations``.

    M2: a worker result whose findings are valid must not be discarded because its
    summary ran a little long (the incident: all four critics of a research round were
    rejected on ``summary > 1000`` while their verdicts were intact). Truncating the
    summary bounds context without losing the payload.

    Row wr-schema-discards-work (2026-07-27) is that incident happening AGAIN through a
    different soft field: 6 of 8 workers in one round had their results rejected while
    all 8 produced usable content, and one of them died because a single advisory
    metadata string ran 84 chars over -- its content being the worker honestly stating
    what it could not re-verify. The M2 rule was right and was scoped to one field; it
    is now the rule for every soft field.

    SIZE is soft, TYPE is contract. A non-list, or a non-string/empty item, is still a
    hard error (``_validate_friction_observations``) -- the reducer cannot consume it.
    Nothing here ever touches ``findings``: truncating an actual finding would be worse
    than rejecting the result.

    Warnings are also recorded on ``data["softWarnings"]`` (optional schema field) so the
    degradation travels WITH the result to the reducer instead of dying in a run log --
    degrading instead of discarding is only honest if downstream can see it happened.
    Mutates ``data``; callers persist it so every consumer sees the same bounded result.
    """
    warnings: list[str] = []
    summary = data.get("summary")
    if isinstance(summary, str) and len(summary) > summary_max:
        data["summary"] = summary[: summary_max - 1].rstrip() + "…"
        warnings.append(f"summary truncated from {len(summary)} to {summary_max} chars")
    obs = data.get("frictionObservations")
    if isinstance(obs, list):
        bounded = [
            (x[: FRICTION_MAX_CHARS - 1].rstrip() + "…")
            if isinstance(x, str) and len(x) > FRICTION_MAX_CHARS else x
            for x in obs[:FRICTION_MAX_ITEMS]
        ]
        over_len = sum(1 for x in obs if isinstance(x, str) and len(x) > FRICTION_MAX_CHARS)
        if over_len:
            warnings.append(f"{over_len} frictionObservations truncated to {FRICTION_MAX_CHARS} chars")
        if len(obs) > FRICTION_MAX_ITEMS:
            warnings.append(f"frictionObservations trimmed from {len(obs)} to {FRICTION_MAX_ITEMS} items")
        if bounded != obs:
            data["frictionObservations"] = bounded
    if warnings:
        prior = data.get("softWarnings")
        data["softWarnings"] = ([*prior] if isinstance(prior, list) else []) + warnings
    return warnings


def _degrade_over_ceiling(data: dict[str, Any], result_path: Path, max_chars: int) -> list[str]:
    """Persist the full result, then shrink the ingest capsule deterministically."""
    sidecar = result_path.with_name(result_path.name + ".overflow.json")
    sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    overflow_ref = sidecar.name
    data["degraded"] = True
    data["overflowRef"] = overflow_ref

    def payload_size() -> int:
        return len(json.dumps({k: v for k, v in data.items() if k != "softWarnings"},
                              ensure_ascii=False))

    dropped = 0
    findings = data.get("findings")
    if isinstance(findings, list):
        for severity in ("info", "low", "medium", "high", "blocker"):
            for idx in range(len(findings) - 1, -1, -1):
                finding = findings[idx]
                if (isinstance(finding, dict) and finding.get("severity") == severity
                        and payload_size() > max_chars):
                    findings.pop(idx)
                    dropped += 1

    trimmed = 0
    for items in (
        data.get("itemsAnalyzed"),
        data.get("graphify", {}).get("queries") if isinstance(data.get("graphify"), dict) else None,
    ):
        if isinstance(items, list):
            while items and payload_size() > max_chars:
                items.pop()
                trimmed += 1

    warning = f"degraded: dropped {dropped} findings, trimmed {trimmed} list items; full capsule at {overflow_ref}"
    prior = data.get("softWarnings")
    data["softWarnings"] = ([*prior] if isinstance(prior, list) else []) + [warning]
    result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    size = payload_size()
    return ([f"worker result exceeds maxWorkerOutputChars ({size}>{max_chars})"]
            if size > max_chars else [])


EXECUTORS_REL = ".harness/routing/executors.json"


def executor_repo_access(root: Path | str, executor: str | None) -> str:
    """The declared access class of an executor: "repo" or "none".

    Harness-side by construction — the card is ours, the packet is the worker's,
    so a worker can never assert its own exemption (owner decision 2026-07-27).

    DEFAULT DIRECTION IS STRICT: an unknown executor, a missing/corrupt registry,
    or an undeclared card all read as "repo". The relaxation must be EARNED by a
    declaration; it is never granted by ignorance, which is the only default that
    cannot be exploited by deleting a config."""
    if not executor:
        return "repo"
    try:
        cards = json.loads((Path(root) / EXECUTORS_REL).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt registry must not relax anything
        return "repo"
    card = ((cards or {}).get("executors") or {}).get(str(executor)) or {}
    return "none" if card.get("repoAccess") == "none" else "repo"


def validate_worker_result(
    data: dict[str, Any],
    *,
    expected_workflow_id: str | None = None,
    expected_worker_id: str | None = None,
    max_chars: int | None = None,
    expected_scope_paths: list[str] | None = None,
    repo_access: str | None = None,
    result_path: Path | None = None,
) -> list[str]:
    def _pathish(s: str) -> bool:
        # Only strings that plausibly ARE repo paths get existence/scope-checked.
        # URLs ("://") and prose citations (whitespace) are legitimate evidence
        # for research rounds (SPEC-119) and must not be treated as paths.
        s = s.strip()
        if not s or "://" in s or " " in s:
            return False
        return "/" in s or "." in Path(s.split(":", 1)[0]).name

    errors: list[str] = []
    # Row wr-schema-discards-work (2026-07-27): bound the soft fields at the INGEST seam,
    # before any check runs. Previously only ONE of six call sites bounded (run_one_worker);
    # collect, async_state and both workflow_reduce paths validated raw, which is how a
    # frontier critique wave lost 3 of 3 results with their blockers intact. Doing it here
    # makes every seam correct by construction -- including seams that do not exist yet --
    # and the maxWorkerOutputChars measurement at the end of this function then sees the
    # bounded payload for free, with no reordering. Same mutate-at-ingest contract as the
    # TSV normalization below.
    bound_soft_fields(data)
    # TSV/JSON boundary P1 (owner 2026-07-22): an agent may hand `findings` as ONE
    # TSV table string (header severity/category/title/evidence/recommendation;
    # evidence cell = compact-JSON array). Normalize at the ingest seam BEFORE the
    # schema/shape checks, mutating data so every downstream consumer sees the same
    # parsed list (bound_soft_fields persistence precedent). Only a present string
    # is touched: a missing key still reports `missing findings`, and a non-TSV
    # string passes through to the honest `findings must be a list` error.
    if isinstance(data.get("findings"), str):
        from harness_lib.common import tsv_table
        data["findings"] = tsv_table(data["findings"])
    errors.extend(validate_json_schema(data, "workerResult"))
    required = ["workflowId", "workerId", "workerRole", "status", "summary", "itemsAnalyzed", "findings", "sourceFilesVerified", "graphify"]
    for key in required:
        if key not in data:
            errors.append(f"missing {key}")
    if expected_workflow_id and data.get("workflowId") != expected_workflow_id:
        errors.append("workflowId mismatch")
    if expected_worker_id and data.get("workerId") != expected_worker_id:
        errors.append("workerId mismatch")
    if data.get("status") not in {"done", "partial", "blocked", "failed"}:
        errors.append("invalid status")
    if not isinstance(data.get("summary", ""), str) or not data.get("summary"):
        errors.append("summary must be non-empty string")
    # M2: an over-long summary is a SOFT-field overrun, not a contract violation — the
    # whole result (findings, sources) is still valid and reducible, and total size is
    # already capped by maxWorkerOutputChars below. Bound it via bound_soft_fields at the
    # finalize seam (truncate-and-warn) instead of rejecting a valid result here.
    friction_error = _validate_friction_observations(data, "WORKER_RESULT")
    if friction_error:
        errors.append(friction_error)
    # ort item 2: planDeviations is TYPED evidence for the overseer's
    # divergence judgment (review ritual step 7) — a malformed shape is a hard
    # error, unlike prose fields: unreadable evidence is worse than none.
    deviations = data.get("planDeviations")
    if deviations is not None:
        if not isinstance(deviations, list) or len(deviations) > 10:
            errors.append("planDeviations must be a list of at most 10 items")
        else:
            for idx, dev in enumerate(deviations):
                if not isinstance(dev, dict) or set(dev) != {"planSaid", "codeIs", "action"}:
                    errors.append(f"planDeviations {idx} must be an object with exactly planSaid/codeIs/action")
                elif not all(isinstance(dev[k], str) and 0 < len(dev[k]) <= 200
                             for k in ("planSaid", "codeIs", "action")):
                    errors.append(f"planDeviations {idx} fields must be non-empty strings <=200 chars")
    if not isinstance(data.get("itemsAnalyzed", []), list):
        errors.append("itemsAnalyzed must be a list")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []
    verified = data.get("sourceFilesVerified", [])
    if not isinstance(verified, list):
        errors.append("sourceFilesVerified must be a list")
        verified = []
    else:
        for idx, source in enumerate(verified):
            if not isinstance(source, str):
                errors.append(f"sourceFilesVerified {idx} must be string")
            elif _pathish(source) and not existing_rel_path(source):
                errors.append(f"sourceFilesVerified {idx} does not exist: {source}")
    scope_prefixes = [str(x).strip().lstrip("./") for x in (expected_scope_paths or []) if str(x).strip()]
    def in_scope(path_value: str) -> bool:
        p = str(path_value).split(":", 1)[0].strip().lstrip("./")
        if not p or not scope_prefixes:
            return True
        return any(p == scope.rstrip("/") or p.startswith(scope.rstrip("/") + "/") or scope == p for scope in scope_prefixes)
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"finding {idx} must be object")
            continue
        for key in ["severity", "category", "title", "evidence", "recommendation"]:
            if key not in finding:
                errors.append(f"finding {idx} missing {key}")
        if finding.get("severity") not in {"blocker", "high", "medium", "low", "info"}:
            errors.append(f"finding {idx} invalid severity")
        evidence = finding.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"finding {idx} evidence must be list")
            evidence = []
        if finding.get("severity") in {"blocker", "high"} and not evidence:
            errors.append(f"finding {idx} high/blocker requires evidence")
        for ev_idx, item in enumerate(evidence):
            item_s = str(item)
            # Evidence may be a command/test/spec id, a URL, or a prose citation
            # (research rounds, SPEC-119); only path-like evidence is checked.
            if _pathish(item_s) and not existing_rel_path(item_s):
                errors.append(f"finding {idx} evidence {ev_idx} path does not exist: {item_s}")
            if scope_prefixes and _pathish(item_s) and not in_scope(item_s):
                errors.append(f"finding {idx} evidence {ev_idx} is outside worker scope: {item_s}")
    graph = data.get("graphify", {})
    # Row `wr-schema-discards-work`, owner decision 2026-07-27 ("na configuração do
    # executor"): a packet-only worker has NO repo, so it can never legitimately fill
    # `sourceFilesVerified` — and requiring it either made the cheap multi-vendor leg
    # useless for serious findings or PRESSURED the worker into claiming verification
    # it never did (measured: NVIDIA workers tagged packet-derived evidence `[repo]`,
    # a false-provenance factory). The access class is declared HARNESS-SIDE on the
    # executor card, never in the packet, precisely so the worker cannot assert its
    # own exemption. Net effect is a TIGHTENING: the waiver comes with two new
    # positive errors, because a worker with no repo claiming repo work is lying.
    packet_only = repo_access == "none"
    if not isinstance(graph, dict) or graph.get("status") not in {"used", "not_available", "stale", "not_applicable", "skipped"}:
        errors.append("invalid graphify.status")
    elif packet_only and graph.get("status") == "used":
        errors.append("graphify.status=used from a packet-only executor (repoAccess=none): "
                      "this worker never had a repo to graph")
    elif graph.get("status") == "used" and not verified:
        errors.append("sourceFilesVerified required when graphify.status=used")
    if packet_only and verified:
        errors.append("sourceFilesVerified is non-empty from a packet-only executor "
                      "(repoAccess=none): it cannot have read those files")
    elif not packet_only and any(f.get("severity") in {"blocker", "high"} for f in findings if isinstance(f, dict)) and not verified:
        errors.append("sourceFilesVerified required when high/blocker findings are present")
    # CQ.2: optional QA evidence capsule — review cost is O(capsule), not O(re-run).
    # Absent capsule = zero new checks (a capsule-less result validates exactly as before).
    if "oracleEvidence" in data:
        oe = data.get("oracleEvidence")
        if not isinstance(oe, dict):
            errors.append("oracleEvidence must be an object")
        else:
            if not isinstance(oe.get("oracle"), str) or not oe.get("oracle", "").strip():
                errors.append("oracleEvidence.oracle must be a non-empty string")
            if oe.get("exitClass") not in EXIT_CLASSES:
                errors.append("oracleEvidence.exitClass must be one of " + ", ".join(EXIT_CLASSES))
            rerun = oe.get("rerunCmd")
            if rerun is not None and (not isinstance(rerun, str) or len(rerun) > 400):
                errors.append("oracleEvidence.rerunCmd must be a string of at most 400 chars")
            artifact = oe.get("artifactPath")
            if artifact is not None:
                # PureWindowsPath accepts / and \ separators, so one check rejects
                # posix ("/etc/x"), drive ("C:\x"), and any ".." segment on both OSes.
                p = PureWindowsPath(str(artifact))
                if not str(artifact).strip() or p.drive or p.root or ".." in p.parts:
                    errors.append(f"oracleEvidence.artifactPath must be a relative path without '..': {artifact}")
    if max_chars is not None:
        # `softWarnings` is HARNESS-generated diagnostics, not worker payload, and it is
        # written by the bounding above. Counting it would let the degradation mechanism
        # push a result OVER the cap it exists to keep it under: measured on the real
        # 2026-07-27 round, one result went 15371 -> 15432 because trimming 37 chars of
        # summary added ~98 chars of warning. The cap bounds reducer input; measure the
        # payload.
        size = len(json.dumps({k: v for k, v in data.items() if k != "softWarnings"},
                              ensure_ascii=False))
        if size > max_chars:
            if result_path is not None and not errors:
                errors.extend(_degrade_over_ceiling(data, result_path, max_chars))
            else:
                errors.append(f"worker result exceeds maxWorkerOutputChars ({size}>{max_chars})")
    return list(dict.fromkeys(errors))
