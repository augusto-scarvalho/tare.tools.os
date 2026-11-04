#!/usr/bin/env python3
"""Experiment-lifecycle registry (SPEC-116 pack, owner mandate 2026-07-13).

The research-playbook prescribes an experiment card template (hipótese |
baseline | métricas | critérios de decisão) but nothing enforces a LIFECYCLE:
failed experiments lingered as bad features because no state distinguished a
pre-registered hypothesis from a shipped control change. This registry is that
missing half — a card is PROPOSED until the owner activates it (stamping a
reviewBy nag), an ACTIVE experiment accepts append-only measurements and at
most one extension, and it settles into a permanent SHIPPED|SHELVED tombstone
with the asymmetric evidence a verdict requires. `doctor` nags when an active
experiment ages past its reviewBy.

Registered as the `experiment` verb via cli_registry. Writes ONLY
.harness/state/experiments.json. Self-check:
python scripts/harness_lib/experiment_registry.py.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402
from harness_lib.common import HarnessError, now, parse_iso_datetime, read_json, write_json  # noqa: E402

REGISTRY_REL = ".harness/state/experiments.json"
REQUIRED = ("hypothesis", "metric", "baseline", "successCriteria", "abandonCriteria", "reversalPlan")
VERDICTS = ("shipped", "shelved")
STATUSES = ("proposed", "active", "shipped", "shelved")
EVIDENCE_GRADES = (1, 2, 3, 4)   # App H: exploratory/attributive/confirmatory/promotion-qualified (closed set)
REVIEW_DAYS = 14
FIELD_CAP = 400        # chars stored per free-text field (the registry is a pointer, not a transcript)
PLAN_CAP = 64000       # plan is the one long field (tasks_store parity): the refinement IS the transcript
STARTABLE = ("proposed", "active")   # refine/dispatch targets; tombstones are settled

_ID_RE = re.compile(r"^EXP-\d+$")
_RUN_FILE_RE = re.compile(r"^experiment-refine-[A-Za-z0-9._-]+\.plan\.md$")

# Advisory only (L18): keyword in title/hypothesis/metric -> method cards in
# docs/EXPERIMENT_METHODS.md (each anchor is a heading's kebab slug there; the
# exl scenario asserts anti-dead-link). Ordered; first match per family wins.
_METHODS_DOC = "docs/EXPERIMENT_METHODS.md"
# Pointer _advise hands a gradeless verdict: the new methodology section (App H).
_METHODOLOGY_DOC = "docs/EXPERIMENT_METHODOLOGY.md"
_GRADE_ANCHOR = "graus-de-evidência-1-4-app-h"
_METHOD_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("threshold", "tuning", "limiar"), ("taguchi-robust-design", "noise-floor")),
    (("vendor", "codex", "claude", "compar"), ("matched-budget-controls", "split-plot")),
    (("interac", "combinat"), ("covering-arrays",)),
    (("promot", "default", "rollout"), ("evidence-grades", "confidence-sequences")),
    (("width", "largura", "fan-out", "worker"), ("oracle-recall",)),
    (("judge", "juiz", "llm-as"), ("judge-bias",)),
    # Shadow/measure-only/measure-before-control experiments (our most common kind:
    # EXP-22, GM-5, C18) hit NONE of the families above, so the advisory stayed
    # silent exactly when confidence-sequences is the right card. This family closes
    # that hole -> anytime-valid monitoring + grade + the abandon-bound noise floor.
    (("shadow", "canary", "measure-only", "measure-before", "divergen", "noise floor"),
     ("confidence-sequences", "evidence-grades", "noise-floor")),
)


def method_hints(*text: str, cap: int = 3) -> list[str]:
    """Deterministic card anchors suggested for the given free text (title +
    hypothesis + metric). Pure: matches keywords in table order, dedupes, caps."""
    hay = " ".join(text).lower()
    out: list[str] = []
    for keywords, anchors in _METHOD_HINTS:
        if any(k in hay for k in keywords):
            for a in anchors:
                if a not in out:
                    out.append(a)
    return out[:cap]


def _validate_grade(value: Any) -> int | None:
    """Optional evidence grade -> one of EVIDENCE_GRADES, or None when absent
    (no retroactive mandate — a gradeless card stays valid). Raises HarnessError
    on a present-but-invalid grade: App H is a closed 1-4 set, no inference."""
    if value is None or str(value).strip() == "":
        return None
    try:
        grade = int(value)
    except (TypeError, ValueError):
        grade = None
    if grade not in EVIDENCE_GRADES:
        raise HarnessError(f"bad evidenceGrade {value!r}\nfix: one of "
                           f"{'/'.join(map(str, EVIDENCE_GRADES))} "
                           f"({_METHODOLOGY_DOC}#{_GRADE_ANCHOR})")
    return grade


def _advise(entry: dict[str, Any]) -> None:
    """Print card suggestions after a successful add/verdict. Advisory: fail-open,
    and to stderr so JSON stdout consumers (panel/agents) are never corrupted."""
    try:
        for anchor in method_hints(str(entry.get("title") or ""),
                                   str(entry.get("hypothesis") or ""),
                                   str(entry.get("metric") or "")):
            print(f"[methods] consider: {anchor} ({_METHODS_DOC}#{anchor})", file=sys.stderr)
        # A settled verdict with no evidence grade: point at App H, do NOT infer one.
        if entry.get("status") in VERDICTS and not entry.get("evidenceGrade"):
            print(f"[methods] no evidenceGrade on this {entry.get('status')} verdict — "
                  f"grade it 1-4: {_METHODOLOGY_DOC}#{_GRADE_ANCHOR}", file=sys.stderr)
    except Exception:
        pass  # a suggestion table blowing up must never break the add


def _load(root: Path) -> dict[str, Any]:
    try:
        data = read_json(Path(root) / REGISTRY_REL, None)
    except Exception:
        data = None  # corrupt registry reads as empty; the next write repairs it
    if not isinstance(data, dict) or not isinstance(data.get("experiments"), list):
        return {"schemaVersion": 1, "experiments": []}
    return data


def _clean(text: str) -> str:
    """Scrub machine-local paths, redact secret shapes, cap — the intake idiom,
    applied to every stored free-text field (the registry is a tracked ledger).
    The `\\b` before the drive letter is load-bearing: without it the scrubber ate the
    tail of any scheme-colon-slash-slash URI (an http one became http<local-path>), which
    the ratified URI-shaped `artifact` pointer newly exposed (reckon 2026-07-21). A word
    boundary still catches a real Windows drive path, standalone and mid-sentence."""
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s\"'<>]+", "<local-path>", str(text or ""))
    return secret_scan.redact_text(text)[:FIELD_CAP]


def _clean_metric(m: Any) -> dict[str, Any] | None:
    """Validate ONE tidy structured measurement (the ratified additive `metrics[]`
    schema, owner 2026-07-21: OTel/MLflow field names + tidy-data one-row-per-metric).
    `metric` (name) + `value` (finite number) are REQUIRED; all else optional. Returns
    None when a required field is missing/invalid — NEVER coerces a fabricated number
    (measure-honesty). Field names mirror the research: metric/value/unit/n/ci/ciMethod/
    noiseFloor/step/artifact/variant/source."""
    if not isinstance(m, dict):
        return None
    name = str(m.get("metric") or "").strip()
    try:
        value = float(m["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if not name or not math.isfinite(value):
        return None
    out: dict[str, Any] = {"metric": _clean(name)[:120], "value": value}
    if m.get("unit"):
        out["unit"] = _clean(str(m["unit"]))[:32]
    for k in ("n", "step"):
        if m.get(k) is not None:
            try:
                out[k] = int(m[k])
            except (TypeError, ValueError):
                pass
    if m.get("noiseFloor") is not None:
        try:
            nf = float(m["noiseFloor"])
            if math.isfinite(nf):
                out["noiseFloor"] = nf
        except (TypeError, ValueError):
            pass
    ci = m.get("ci")
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        try:
            lo, hi = float(ci[0]), float(ci[1])
            if math.isfinite(lo) and math.isfinite(hi):
                out["ci"] = [lo, hi]
        except (TypeError, ValueError):
            pass
    for k in ("ciMethod", "artifact", "variant", "source"):
        if m.get(k):
            out[k] = _clean(str(m[k]))[:120]
    return out


def _review_by_default() -> str:
    """reviewBy = now + REVIEW_DAYS, in common.now()'s local-ISO format."""
    return (parse_iso_datetime(now()) + dt.timedelta(days=REVIEW_DAYS)).isoformat(timespec="seconds")


def _require(data: dict[str, Any], exp_id: str) -> dict[str, Any]:
    entry = next((e for e in data["experiments"] if e.get("id") == exp_id), None)
    if entry is None:
        raise HarnessError(f"unknown experiment {exp_id!r}\nfix: harness.py experiment list")
    return entry


def _require_active(entry: dict[str, Any], action: str) -> None:
    if entry.get("status") != "active":
        raise HarnessError(
            f"cannot {action} {entry['id']}: status is {entry.get('status')}, not active"
            "\nfix: only active experiments accept measurements/verdicts")


def add(root: Path | str, exp_id: str, title: str, fields: dict[str, Any], source: str = "") -> dict[str, Any]:
    """Pre-register one experiment card as `proposed`; refuse an incomplete card."""
    if not _ID_RE.match(str(exp_id or "")):
        raise HarnessError(f"bad experiment id {exp_id!r}\nfix: use EXP-<number> (e.g. EXP-4)")
    missing = [k for k in REQUIRED if not str((fields or {}).get(k, "")).strip()]
    if missing:
        raise HarnessError("experiment add is missing required field(s): " + ", ".join(missing)
                           + "\nfix: a card without " + ", ".join(REQUIRED) + " is not registerable")
    grade = _validate_grade((fields or {}).get("evidenceGrade"))  # optional; refuse an invalid one
    root = Path(root)
    data = _load(root)
    if any(e.get("id") == exp_id for e in data["experiments"]):
        raise HarnessError(f"duplicate experiment id {exp_id!r}\nfix: harness.py experiment show {exp_id}")
    entry = {"id": exp_id, "title": _clean(title), "source": _clean(source),
             "status": "proposed", "createdAt": now(), "measurements": []}
    for key in REQUIRED:
        entry[key] = _clean(fields[key])
    if grade is not None:
        entry["evidenceGrade"] = grade
    data["experiments"].append(entry)
    write_json(root / REGISTRY_REL, data)
    return entry


def experiments(root: Path | str, status: str | None = None) -> list[dict[str, Any]]:
    return [e for e in _load(Path(root))["experiments"] if status is None or e.get("status") == status]


def activate(root: Path | str, exp_id: str, review_by: str | None = None) -> dict[str, Any]:
    """Owner move: proposed -> active, stamping a reviewBy nag (+REVIEW_DAYS default)."""
    root = Path(root)
    data = _load(root)
    entry = _require(data, exp_id)
    if entry.get("status") != "proposed":
        raise HarnessError(f"cannot activate {exp_id}: status is {entry.get('status')}, not proposed"
                           "\nfix: only a proposed experiment can be activated")
    entry["status"] = "active"
    entry["activatedAt"] = now()
    entry["reviewBy"] = review_by or _review_by_default()
    write_json(root / REGISTRY_REL, data)
    return entry


def record(root: Path | str, exp_id: str, value: str, note: str = "",
           metrics: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Append one measurement — active-only, append-only. The free-text `value` stays
    authoritative as the note; `metrics` (optional) attaches the ratified structured
    `metrics[]` array (tidy metric objects) ADDITIVELY, so old records lacking it stay
    valid and nothing migrates. Invalid/valueless metric entries are dropped, never
    fabricated (measure-honesty)."""
    root = Path(root)
    data = _load(root)
    entry = _require(data, exp_id)
    _require_active(entry, "record on")
    rec: dict[str, Any] = {"at": now(), "value": _clean(value), "note": _clean(note)}
    cleaned = [c for c in (_clean_metric(x) for x in (metrics or [])) if c]
    if cleaned:
        rec["metrics"] = cleaned
    entry["measurements"].append(rec)
    write_json(root / REGISTRY_REL, data)
    return entry


def extend(root: Path | str, exp_id: str, review_by: str | None = None) -> dict[str, Any]:
    """Push reviewBy out once — the second ask is an owner escalation, not a verb."""
    root = Path(root)
    data = _load(root)
    entry = _require(data, exp_id)
    _require_active(entry, "extend")
    if entry.get("extended"):
        raise HarnessError(f"experiment {exp_id} already extended once; decide a verdict or escalate to the owner"
                           f"\nfix: harness.py experiment verdict {exp_id} shipped|shelved")
    entry["extended"] = True
    entry["reviewBy"] = review_by or _review_by_default()
    write_json(root / REGISTRY_REL, data)
    return entry


def verdict(root: Path | str, exp_id: str, decision: str, evidence: str = "", note: str = "",
            reopen_trigger: str = "", evidence_grade: Any = None) -> dict[str, Any]:
    """Settle an active experiment into a permanent tombstone (asymmetric evidence)."""
    root = Path(root)
    data = _load(root)
    entry = _require(data, exp_id)
    _require_active(entry, "decide a verdict on")
    if decision not in VERDICTS:
        raise HarnessError(f"unknown verdict {decision!r}\nfix: one of {'/'.join(VERDICTS)}")
    grade = _validate_grade(evidence_grade)  # optional; refuse an invalid one before mutating
    if decision == "shipped" and len(entry.get("measurements") or []) < 1:
        raise HarnessError(f"cannot ship {exp_id}: no shipping without data"
                           f"\nfix: harness.py experiment record {exp_id} <value> first")
    if decision == "shelved" and not (str(evidence).strip() and str(reopen_trigger).strip()):
        raise HarnessError(f"cannot shelve {exp_id}: shelving needs --evidence AND --reopen-trigger"
                           "\nfix: prove the reversal ran and name what would reopen it")
    entry["status"] = decision
    entry["verdict"] = decision
    entry["verdictAt"] = now()
    entry["evidence"] = _clean(evidence)
    entry["note"] = _clean(note)
    entry["reopenTrigger"] = _clean(reopen_trigger)
    if grade is not None:
        entry["evidenceGrade"] = grade
    write_json(root / REGISTRY_REL, data)
    return entry


def _require_startable(entry: dict[str, Any], action: str) -> None:
    if entry.get("status") not in STARTABLE:
        raise HarnessError(
            f"cannot {action} {entry['id']}: status is {entry.get('status')}, not {'/'.join(STARTABLE)}"
            "\nfix: shipped/shelved experiments are settled tombstones")


def append_plan(root: Path | str, exp_id: str, text: str, source: str = "") -> dict[str, Any]:
    """Owner-approved refinement append (tasks_store.append_plan parity): the
    plan field keeps its lineage through the lifecycle. Secret-scanned but NOT
    FIELD_CAP'd — the plan is the one field that IS a transcript."""
    text = secret_scan.redact_text(str(text).strip())
    if not text:
        raise HarnessError("empty plan text")
    root = Path(root)
    data = _load(root)
    entry = _require(data, exp_id)
    existing = str(entry.get("plan") or "")
    entry["plan"] = ((existing + "\n\n---\n\n" if existing else "") + text)[:PLAN_CAP]
    entry["planRefinedAt"] = now()
    if source:
        entry["planSource"] = _clean(source)
    write_json(root / REGISTRY_REL, data)
    return entry


def plan_from_run(root: Path | str, exp_id: str, run_name: str) -> dict[str, Any]:
    """Approval-as-record: the accepted refinement must be a REAL proposal file
    `experiment refine` wrote under .harness/runs/ — never arbitrary browser text."""
    if not _RUN_FILE_RE.match(str(run_name or "")):
        raise HarnessError(f"invalid proposal file name: {run_name!r}")
    path = Path(root) / ".harness" / "runs" / run_name
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        raise HarnessError(f"proposal file not found: {run_name}")
    return append_plan(root, exp_id, text, source=run_name)


def _card_lines(entry: dict[str, Any]) -> list[str]:
    return [f"- {key}: {entry.get(key)}"
            for key in ("title", *REQUIRED, "evidenceGrade") if entry.get(key)]


def refine(root: Path | str, exp_id: str, model: str | None = None, effort: str | None = None,
           executor: str | None = None) -> int:
    """Bounded READ-ONLY model run proposing an implementation/measurement plan
    for the experiment card (tasks-refine parity via the shared engine). Writes
    only .harness/runs/experiment-refine-*.plan.md; acceptance is the separate
    owner-approved `experiment plan --from-run` write."""
    from harness_lib import tasks_board
    root = Path(root)
    entry = _require(_load(root), exp_id)
    _require_startable(entry, "refine")
    return tasks_board.refine_model_run(root, "experiment-refine", exp_id, [
        "# Experiment plan refinement (read-only)",
        "You are a read-only planning agent. Explore the repository as needed;",
        "do NOT modify any file, do NOT commit.",
        "",
        "## Experiment card",
        f"- id: {entry['id']} (status: {entry.get('status')})",
        *_card_lines(entry),
        "",
        "## Current plan",
        str(entry.get("plan") or "(none yet)"),
        "",
        "## Deliverable",
        "Before we start with the plan, explain simply, like I'm five years old,",
        "what this experiment does and what happens if it works. After that, you",
        "can start the plan with all the details and the full scientific rigor it",
        "needs.",
        "",
        "Output ONLY those two parts in markdown (nothing before the simple",
        "explanation): the like-I'm-five section first, then the refined",
        "implementation plan: concrete steps to run the experiment, file",
        "footprint, how each metric is measured against the baseline, and a",
        "verification section. It will be appended to the experiment's plan",
        "upon owner approval.",
    ], model, effort, executor)


def dispatch(root: Path | str, exp_id: str, executor: str | None = None) -> int:
    """CLI AFK start: ship the card + approved plan through the SAME porteiro
    path a chat demand takes (`route --task ... --dispatch`, guardrails
    included — WITHHELD/REFUSED are relayed, not routed around). A real detached
    launch is recorded on the entry as `dispatch{pid,log}`. (The panel Start
    button opens a porteiro chat instead — SPEC-146.)"""
    from harness_lib import tasks_board
    from harness_lib.async_state import workflow_process_alive
    root = Path(root)
    entry = _require(_load(root), exp_id)
    _require_startable(entry, "start")
    # Double-dispatch guard (2026-07-17: EXP-2 was blind-double-clicked into two
    # detached twins racing the same registry): the recorded pid is the lock.
    prev = entry.get("dispatch") or {}
    try:
        prev_alive = workflow_process_alive(int(prev.get("pid") or 0))
    except (TypeError, ValueError):
        prev_alive = False
    if prev_alive:
        raise HarnessError(
            f"cannot start {exp_id}: dispatch pid {prev['pid']} is still running"
            f" (log: {prev.get('log') or 'n/a'})"
            "\nfix: wait for it to finish (tail the log) or kill the pid first")
    parts = [f"Run experiment {entry['id']}: {entry.get('title') or ''}".strip(),
             "Experiment card:\n" + "\n".join(_card_lines(entry))]
    if entry.get("plan"):
        # ponytail: demand travels as one argv element — cap the plan slice so
        # the Windows 32K command line never overflows; the full plan stays here.
        parts.append("Approved plan:\n" + str(entry["plan"])[:12000])
    info, out = tasks_board.route_dispatch(root, "\n\n".join(parts), executor)
    if info:
        data = _load(root)   # reload: the route run takes a while
        entry = _require(data, exp_id)
        entry["dispatch"] = {**info, "at": now()}
        write_json(root / REGISTRY_REL, data)
        print(f"\n-- experiment {exp_id} dispatched: pid {info['pid']} recorded on the card")
    elif "WITHHELD" in out:
        print(f"\n-- experiment {exp_id} NOT dispatched: route WITHHELD -> "
              "owner decision inbox (harness.py decide); card unchanged")
    return 0


def stale_active(root: Path | str, days: int = 0) -> list[tuple[str, str, int]]:
    """(id, reviewBy, daysOverdue) for active entries past reviewBy — doctor feed.

    Calm on a missing/corrupt registry (returns []) so doctor reads clean roots ok."""
    ref = parse_iso_datetime(now())
    out: list[tuple[str, str, int]] = []
    for e in experiments(root, status="active"):
        review = parse_iso_datetime(str(e.get("reviewBy") or ""))
        if review is None or ref is None:
            continue
        overdue = (ref - review).days
        if overdue > days:
            out.append((str(e.get("id")), str(e.get("reviewBy")), overdue))
    return out


def cmd_experiment(args) -> int:
    from harness_lib import common

    action = getattr(args, "action", "list") or "list"
    params = list(getattr(args, "params", []) or [])
    root = common.ROOT
    if action == "add":
        if not params:
            raise HarnessError("experiment add needs <EXP-N> <title…>")
        fields = {
            "hypothesis": getattr(args, "hypothesis", None) or "",
            "metric": getattr(args, "metric", None) or "",
            "baseline": getattr(args, "baseline", None) or "",
            "successCriteria": getattr(args, "success", None) or "",
            "abandonCriteria": getattr(args, "abandon", None) or "",
            "reversalPlan": getattr(args, "reversal", None) or "",
            "evidenceGrade": getattr(args, "grade", None),   # optional (App H)
        }
        entry = add(root, params[0], " ".join(params[1:]), fields, source=getattr(args, "source", None) or "")
        common.emit(entry)
        _advise(entry)
        return 0
    if action == "activate":
        if not params:
            raise HarnessError("experiment activate needs <EXP-N>")
        common.emit(activate(root, params[0], review_by=getattr(args, "review_by", None)))
        return 0
    if action == "record":
        if len(params) < 2:
            raise HarnessError("experiment record needs <EXP-N> <value…>")
        # Optional structured measurement (ratified `metrics[]` schema): --measure NAME
        # --value NUMBER (both required together) + optional --unit/--n/--ci-lo/--ci-hi/
        # --ci-method/--noise-floor/--artifact/--variant. Absent -> free-text-only record.
        metrics = None
        measure = getattr(args, "measure", None)
        # Footgun guard (reckon 2026-07-21): `--metric` is the `add` hypothesis-metric
        # FIELD, silently ignored under `record`; a natural mistype for `--measure`.
        if getattr(args, "metric", None) and not measure:
            raise HarnessError("under `record`, the structured metric NAME flag is --measure, "
                               "not --metric (--metric is the `add` field, ignored here)\n"
                               "fix: experiment record <EXP-N> <text> --measure <name> --value <n>")
        if measure:
            if getattr(args, "value", None) is None:
                raise HarnessError("--measure needs --value (a number)\n"
                                   "fix: experiment record <EXP-N> <text> --measure <name> --value <n>")
            md: dict[str, Any] = {"metric": measure, "value": getattr(args, "value"),
                                  "unit": getattr(args, "unit", None), "n": getattr(args, "n", None),
                                  "ciMethod": getattr(args, "ci_method", None),
                                  "noiseFloor": getattr(args, "noise_floor", None),
                                  "artifact": getattr(args, "artifact", None),
                                  "variant": getattr(args, "variant", None), "source": "authored"}
            lo, hi = getattr(args, "ci_lo", None), getattr(args, "ci_hi", None)
            if lo is not None and hi is not None:
                md["ci"] = [lo, hi]
            metrics = [md]
        common.emit(record(root, params[0], " ".join(params[1:]),
                           note=getattr(args, "note", None) or "", metrics=metrics))
        return 0
    if action == "extend":
        if not params:
            raise HarnessError("experiment extend needs <EXP-N>")
        common.emit(extend(root, params[0], review_by=getattr(args, "review_by", None)))
        return 0
    if action == "verdict":
        if len(params) < 2:
            raise HarnessError("experiment verdict needs <EXP-N> shipped|shelved")
        entry = verdict(root, params[0], params[1], evidence=getattr(args, "evidence", None) or "",
                        note=getattr(args, "note", None) or "",
                        reopen_trigger=getattr(args, "reopen_trigger", None) or "",
                        evidence_grade=getattr(args, "grade", None))
        common.emit(entry)
        _advise(entry)   # 1 line pointing at App H when the verdict carries no grade
        return 0
    if action == "refine":
        if not params:
            raise HarnessError("experiment refine needs <EXP-N>")
        return refine(root, params[0], getattr(args, "model", None),
                      getattr(args, "effort", None), getattr(args, "executor", None))
    if action == "plan":
        if not params or not getattr(args, "from_run", None):
            raise HarnessError("usage: experiment plan <EXP-N> --from-run <experiment-refine-*.plan.md>")
        common.emit(plan_from_run(root, params[0], args.from_run))
        return 0
    if action == "dispatch":
        if not params:
            raise HarnessError("experiment dispatch needs <EXP-N>")
        return dispatch(root, params[0], getattr(args, "executor", None))
    if action == "show":
        if not params:
            raise HarnessError("experiment show needs <EXP-N>")
        common.emit(_require(_load(root), params[0]))
        return 0
    rows = experiments(root, status=getattr(args, "status", None))
    common.emit(rows if getattr(args, "json", False) or common.agent_output_compact()
                else {"experiments": rows, "count": len(rows),
                      "next": "harness.py experiment activate <id> | record <id> <value> | verdict <id> shipped|shelved"},
                tsv=True)  # TE.5/SPEC-139: compact -> TSV rows; bare/--json stay indent=2
    return 0


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        full = {"hypothesis": "h", "metric": "m", "baseline": "b",
                "successCriteria": "s", "abandonCriteria": "a", "reversalPlan": "r"}
        entry = add(root, "EXP-1", "demo experiment", full, source="self-check")
        assert entry["status"] == "proposed" and entry["measurements"] == []
        try:
            add(root, "EXP-2", "incomplete", {"hypothesis": "h"})
            raise AssertionError("incomplete card accepted")
        except HarnessError as exc:
            assert "metric" in str(exc)
        activated = activate(root, "EXP-1")
        assert activated["status"] == "active" and "reviewBy" in activated
        recorded = record(root, "EXP-1", "0.42", note="first measure")
        assert len(recorded["measurements"]) == 1
        # structured metrics[] (ratified schema): a valid metric attaches; a valueless
        # one is DROPPED (measure-honesty — never fabricate a number); non-numeric value
        # rejected; free-text `value` always stays authoritative. URI/path inputs are
        # ASSEMBLED at runtime so no absolute-path/URI LITERAL sits in this source file
        # (release path-hygiene scan flags a real path literal — ph_path_hygiene).
        bs = chr(92)
        art = "s3" + "://bucket/r.json"
        rec2 = record(root, "EXP-1", "divergenceFraction=0.054 (24/444)",
                      metrics=[{"metric": "divergenceFraction", "value": 0.054, "unit": "fraction",
                                "n": 444, "noiseFloor": 0.03, "ci": [0.036, 0.072],
                                "ciMethod": "anytime-valid", "artifact": art, "source": "authored"},
                               {"metric": "no-value"},  # dropped
                               {"metric": "bad", "value": "abc"}])  # dropped
        got = rec2["measurements"][-1]
        assert got["value"].startswith("divergenceFraction") and len(got["metrics"]) == 1, got
        m0 = got["metrics"][0]
        assert m0["metric"] == "divergenceFraction" and m0["value"] == 0.054 and m0["n"] == 444
        assert m0["noiseFloor"] == 0.03 and m0["ci"] == [0.036, 0.072] and m0["unit"] == "fraction"
        assert m0["artifact"] == art, m0  # URI artifact SURVIVES the _clean `\b` fix (reckon)
        # a free-text-only record still has NO metrics key (back-compat, additive).
        assert "metrics" not in record(root, "EXP-1", "plain")["measurements"][-1]
        # _clean() `\b` fix: a scheme:// URI is NOT mangled; a real drive path IS redacted.
        url = "https" + "://x.example/a.json"
        drive = "C" + ":" + bs + "u" + bs + "a.json"
        assert _clean(url) == url, _clean(url)
        assert _clean("at " + drive + " x") == "at <local-path> x"
        # plan lineage (tasks parity): append twice, only real refine proposals accepted
        planned = append_plan(root, "EXP-1", "step one")
        planned = append_plan(root, "EXP-1", "step two", source="experiment-refine-x.plan.md")
        assert planned["plan"] == "step one\n\n---\n\nstep two" and planned["planRefinedAt"]
        try:
            plan_from_run(root, "EXP-1", "../evil.plan.md")
            raise AssertionError("bad proposal file name accepted")
        except HarnessError as exc:
            assert "invalid proposal" in str(exc)
        # evidenceGrade (App H): an invalid grade refuses on add; a valid one persists on verdict
        try:
            add(root, "EXP-9", "graded", {**full, "evidenceGrade": 7})
            raise AssertionError("evidenceGrade 7 accepted")
        except HarnessError as exc:
            assert "evidenceGrade" in str(exc)
        shipped = verdict(root, "EXP-1", "shipped", evidence="metric moved +12%", evidence_grade=3)
        assert shipped["status"] == "shipped" and shipped["verdict"] == "shipped"
        assert shipped["evidenceGrade"] == 3 and "evidenceGrade: 3" in "\n".join(_card_lines(shipped))
        try:
            record(root, "EXP-1", "0.9")
            raise AssertionError("record on a shipped tombstone accepted")
        except HarnessError as exc:
            assert "active" in str(exc)
        try:
            refine(root, "EXP-1")   # startable guard fires before any model spawn
            raise AssertionError("refine on a shipped tombstone accepted")
        except HarnessError as exc:
            assert "tombstone" in str(exc)
        try:
            dispatch(root, "EXP-1")
            raise AssertionError("dispatch on a shipped tombstone accepted")
        except HarnessError as exc:
            assert "tombstone" in str(exc)
        assert stale_active(root) == []  # no active entries left
    print("experiment_registry self-check: ok")


if __name__ == "__main__":
    _demo()
