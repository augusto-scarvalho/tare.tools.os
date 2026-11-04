#!/usr/bin/env python3
"""Durable requirement-intake queue (cm-6, owner decision 2026-07-13).

The SPEC-117 triage hook detects feature-shaped prompts and advises the agent
— but advice evaporates when the conversation moves on: the chat-mined
archaeology recovered 8 owner asks that slipped through WITH the hook
running. This queue is the missing capture half: feature-shaped prompts are
appended durably (redacted, deduped, capped) and stay `pending` until an
explicit triage decision (`spec` | `backlog` | `discard`) is recorded;
`doctor` nags when pending entries age past STALE_DAYS.

Registered as the `intake` verb via cli_registry. Writes ONLY
.harness/state/intake-queue.json. Self-check:
python scripts/harness_lib/intake_queue.py.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402
from harness_lib.common import (HarnessError, gate_holding, now,  # noqa: E402
                                parse_iso_datetime, read_json, write_json)

QUEUE_REL = ".harness/state/intake-queue.json"
DECISIONS = ("spec", "backlog", "discard", "experiment", "done")
DEFECT_SOURCE = "defect-ledger"
MAX_ENTRIES = 200      # oldest DECIDED entries are trimmed first; pending never silently drops
ASK_CAP = 400          # chars stored per ask (the queue is a pointer, not a transcript)
STALE_DAYS = 7


def _load(root: Path) -> dict[str, Any]:
    try:
        data = read_json(Path(root) / QUEUE_REL, None)
    except Exception:
        data = None  # corrupt queue reads as empty; the next write repairs it
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"schemaVersion": 1, "entries": []}
    return data


def _entry_id(ask: str) -> str:
    return hashlib.sha1(" ".join(ask.lower().split()).encode("utf-8")).hexdigest()[:12]


def add(root: Path | str, ask: str, source: str = "manual") -> dict[str, Any]:
    """Append one pending ask; identical pending asks dedupe by content hash."""
    ask = " ".join(str(ask or "").split())
    # Day-one field bugs, both caught by the release-hygiene gate (2026-07-13):
    # (a) system payloads (task-notifications, XML-ish blocks) reached the hook
    # as "prompts" and were captured as owner asks — skip anything system-shaped;
    # (b) machine-local absolute paths are noise in a tracked durable ledger —
    # scrub them to a placeholder (the rule release-hygiene enforces repo-wide).
    if ask.startswith("<"):
        return {"id": None, "skipped": "system-shaped payload, not an owner ask"}
    # Day-one bug (c): user-typed prompts arrive double-UTF-8-encoded through
    # the hook stdin (same defect the phase-2 miner found in session JSONLs) —
    # undo ONE layer when the telltale sequences appear and the round-trip holds.
    if "Ã" in ask or "Â" in ask:
        try:
            ask = ask.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    import re
    ask = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "<local-path>", ask)
    ask = secret_scan.redact_text(ask)[:ASK_CAP]
    if not ask:
        raise HarnessError('intake add needs the ask text\nfix: harness.py intake add "<the requirement>"')
    root = Path(root)
    data = _load(root)
    entry_id = _entry_id(ask)
    if any(e.get("id") == entry_id and e.get("status") == "pending" for e in data["entries"]):
        return {"id": entry_id, "deduped": True}
    data["entries"].append({"id": entry_id, "ask": ask, "source": source,
                            "askedAt": now(), "status": "pending"})
    decided = [e for e in data["entries"] if e.get("status") != "pending"]
    overflow = len(data["entries"]) - MAX_ENTRIES
    if overflow > 0 and decided:
        drop = {id(e) for e in decided[:overflow]}
        data["entries"] = [e for e in data["entries"] if id(e) not in drop]
    write_json(root / QUEUE_REL, data)
    return {"id": entry_id, "deduped": False,
            "pending": sum(1 for e in data["entries"] if e.get("status") == "pending")}


def pending(root: Path | str) -> list[dict[str, Any]]:
    return [e for e in _load(Path(root))["entries"] if e.get("status") == "pending"]


def decide(root: Path | str, entry_id: str, decision: str, note: str = "") -> dict[str, Any]:
    if decision not in DECISIONS:
        raise HarnessError(f"unknown decision {decision!r}\nfix: one of {'/'.join(DECISIONS)}")
    root = Path(root)
    if gate_holding(root):  # mirror ingest_review_defects: a hold would discard the write
        return {"id": entry_id, "held": True,
                "note": ("gate-hold: intake decide refused — a gate holds .harness (the status "
                         "write would be discarded on release); re-run the decide after the gate")}
    data = _load(root)
    entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
    if entry is None:
        raise HarnessError(f"unknown intake entry {entry_id!r}\nfix: harness.py intake list")
    entry["status"] = decision
    entry["decidedAt"] = now()
    # New decision evidence is versioned and bound to this exact queue row.  The
    # lazy import avoids decision_inbox's existing read-side import of this module.
    from harness_lib import decision_inbox
    entry["digestVersion"] = "v2"
    entry["decisionDigest"] = decision_inbox.decision_digest_v2(
        str(entry["id"]), "intake", str(entry.get("ask") or ""))
    if note:
        entry["note"] = secret_scan.redact_text(note)[:ASK_CAP]
    write_json(root / QUEUE_REL, data)
    return {"id": entry_id, "status": decision,
            "pending": sum(1 for e in data["entries"] if e.get("status") == "pending")}


# -- defect-ledger-to-fix-queue (L3): structured reviewer capsule -> ledger + intake ---
# The controlled defect vocabulary + the seat gate live in cost_metrics; NEVER duplicate
# them. cost_metrics imports THIS module at load (it reads QUEUE_REL in _approvals), so
# importing it at top would cycle — the lazy accessor breaks the ring.
def _cm():
    from harness_lib import cost_metrics
    return cost_metrics


def _valid_seat(seat: Any) -> tuple[dict | None, bool]:
    """(complete seat | None, ok). A missing/None seat is a legitimate "unknown side"
    (ok=True). A PRESENT-but-incomplete seat, a non-object, or the literal "self" (which
    would resolve to the RECORDER's seat, not the producer/reviewer's) is invalid."""
    if seat is None:
        return None, True
    if seat == "self" or not isinstance(seat, dict):
        return None, False
    ident = _cm().seat_identity(seat)
    return ident, ident is not None


def validate_defect_capsule(finding: Any, idx: int = 0) -> list[str]:
    """Errors for ONE finding's OPTIONAL `defect` capsule; [] when there is no capsule
    (an ordinary finding) or it is well-formed. The recorder never maps title/prose to a
    class — the reviewer declares a controlled class/evidence/seat or omits the capsule."""
    if not isinstance(finding, dict) or finding.get("defect") is None:
        return []
    cm = _cm()
    d = finding["defect"]
    where = f"reviewFindings[{idx}].defect"
    if not isinstance(d, dict):
        return [f"{where} must be an object"]
    errs: list[str] = []
    if d.get("defectClass") not in cm.DEFECT_CLASSES:
        errs.append(f"{where}.defectClass must be one of {'/'.join(cm.DEFECT_CLASSES)}")
    ev = d.get("evidence")
    if not isinstance(ev, dict) or ev.get("kind") not in cm.EVIDENCE_KINDS \
            or not str(ev.get("ref") or "").strip():
        errs.append(f"{where}.evidence must be {{kind: one of "
                    f"{'/'.join(cm.EVIDENCE_KINDS)}, ref: non-empty}}")
    elif not _artifact_ref(str(ev.get("ref"))):
        # a fragment-only ref ("#suffix") strips non-empty but has an EMPTY artifact key,
        # so two UNRELATED findings would silently cluster together (audit, sonnet
        # 2026-07-31). Require the stable <ref> prefix the prompt already documents.
        errs.append(f"{where}.evidence.ref needs a stable prefix before '#' "
                    f"(got {ev.get('ref')!r}) — a fragment-only ref clusters unrelated "
                    f"findings under one empty artifact key")
    produced, ok_p = _valid_seat(d.get("producedBy"))
    missed, ok_m = _valid_seat(d.get("missedBy"))
    if not (ok_p and ok_m):
        errs.append(f"{where}.producedBy/missedBy must each be a complete "
                    f"role:model:effort seat or null (never 'self' in an artifact)")
    elif not (produced or missed):
        errs.append(f"{where} needs at least one complete seat (producedBy or missedBy)")
    return errs


def validate_review_defects(reviewer_result: Any) -> list[str]:
    """Batch: every capsule across reviewFindings. ONE bad capsule fails the whole batch
    so a caller records/clusters NOTHING — no valid sibling lands ahead of a bad row."""
    findings = reviewer_result.get("reviewFindings") if isinstance(reviewer_result, dict) else None
    if not isinstance(findings, list):
        return []
    errs: list[str] = []
    for i, f in enumerate(findings):
        errs.extend(validate_defect_capsule(f, i))
    return errs


def defect_capsule_prompt_block() -> str:
    """Reviewer-prompt doc for the optional capsule, built from the LIVE controlled vocab
    so the prompt can never drift from the validator (one source of truth)."""
    cm = _cm()
    return (
        "Optional defect attribution (a finding that is a genuine DEFECT may carry it; "
        "OMIT it when you cannot name the class, the evidence, or a seat — an honest "
        "omission beats a guessed measurement):\n"
        '  "defect": {"defectClass": <one of ' + "/".join(cm.DEFECT_CLASSES) + ">, "
        '"evidence": {"kind": <one of ' + "/".join(cm.EVIDENCE_KINDS) + '>, "ref": '
        '"<stable-ref>#<per-finding-suffix>"}, "producedBy": <role:model:effort seat|null>, '
        '"missedBy": <seat|null>}\n'
        "- at least one of producedBy/missedBy is a complete seat; NEVER the literal "
        '"self" in an artifact (it would resolve to the recorder, not the producer).')


def _artifact_ref(ref: str) -> str:
    """The clustering portion of an evidence ref: everything before the first '#'. So
    several findings of one class from one audit/commit artifact cluster together, a
    per-finding '#suffix' does NOT split them, and a different artifact does NOT merge."""
    return str(ref or "").split("#", 1)[0].strip()


def _cluster_id(defect_class: str, evidence_kind: str, artifact_ref: str) -> str:
    return "defect-" + hashlib.sha1(
        f"{defect_class}|{evidence_kind}|{artifact_ref}".encode("utf-8")).hexdigest()[:12]


def _cluster_ask(defect_class: str, evidence_kind: str, artifact_ref: str, count: int) -> str:
    return secret_scan.redact_text(
        f"{count} declared {defect_class} defect(s) in {artifact_ref} "
        f"(evidence: {evidence_kind}) — triage: spec, backlog, experiment, or discard")[:ASK_CAP]


def ingest_review_defects(root: Path | str, reviewer_result: dict,
                          source_path: str | None = None) -> dict[str, Any]:
    """Structured reviewer defect capsules -> the defect ledger + CLUSTERED intake.

    The WHOLE batch validates first; one invalid capsule records/clusters NOTHING (a
    valid sibling must not partially land ahead of a bad row). Each valid capsule is
    recorded EXACTLY as declared via cost_metrics.record_defect (no title/recommendation
    prose is parsed into a class). Intake clusters by (defectClass, evidence.kind,
    artifactRef): a PENDING cluster is upserted in place, a DECIDED one is NEVER reopened.
    Promotion to spec/backlog/experiment stays an explicit human `intake decide` — this
    creates NO task/backlog row. Idempotent: a rerun adds no second cluster, and a
    corrected re-declaration rides record_defect's latest-wins without a new logical
    defect. Refused wholesale under a gate hold (the ledger + queue writes would be lost)."""
    errors = validate_review_defects(reviewer_result)
    base = {"recorded": 0, "clustersCreated": 0, "clustersUpdated": 0, "alreadyDecided": 0}
    if errors:
        return {**base, "errors": errors}
    root = Path(root)
    if gate_holding(root):
        return {**base, "held": True,
                "note": "gate-hold: defect ingest refused — a gate holds .harness "
                        "(the ledger + intake writes would be discarded on release); "
                        "re-run review-reduce after the gate"}
    cm = _cm()
    findings = [f for f in (reviewer_result.get("reviewFindings") or [])
                if isinstance(f, dict) and isinstance(f.get("defect"), dict)]
    recorded = 0
    clusters: dict[tuple, set] = {}
    for f in findings:
        d = f["defect"]
        cls, ev = d["defectClass"], d["evidence"]
        kind, ref = ev["kind"], str(ev["ref"]).strip()
        if cm.record_defect(root, defect_class=cls, evidence_kind=kind, evidence_ref=ref,
                            produced_by=d.get("producedBy"), missed_by=d.get("missedBy")):
            recorded += 1
        clusters.setdefault((cls, kind, _artifact_ref(ref)), set()).add(ref)
    created = updated = decided = 0
    data = _load(root)
    by_id = {e.get("id"): e for e in data["entries"]}
    for (cls, kind, art), refset in clusters.items():
        cid = _cluster_id(cls, kind, art)
        existing = by_id.get(cid)
        if existing is None:
            refs = sorted(refset)
            data["entries"].append({
                "id": cid, "ask": _cluster_ask(cls, kind, art, len(refs)),
                "source": DEFECT_SOURCE, "askedAt": now(), "status": "pending",
                "clusterKey": f"{cls}|{kind}|{art}", "defectClass": cls,
                "evidenceKind": kind, "artifactRef": art,
                "evidenceRefs": refs, "findingCount": len(refs)})
            created += 1
        elif existing.get("status") == "pending":
            merged = sorted(set(existing.get("evidenceRefs") or []) | refset)
            existing["evidenceRefs"] = merged
            existing["findingCount"] = len(merged)
            existing["ask"] = _cluster_ask(cls, kind, art, len(merged))
            updated += 1
        else:
            decided += 1  # a decided cluster is never silently reopened
    write_json(root / QUEUE_REL, data)
    return {"recorded": recorded, "clustersCreated": created, "clustersUpdated": updated,
            "alreadyDecided": decided, "source": str(source_path or "")}


def _read_reviewer_result(source: str) -> dict[str, Any]:
    """Load a reviewer HARNESS_RESULT-shaped JSON object for `defect ingest` — a file
    path, or `-` for stdin. UTF-8 bytes EXPLICITLY (the console codepage mangles accents;
    the tasks_board `--body -` precedent). Raises HarnessError for unreadable/invalid JSON
    OR a shape `validate_review_defects` would silently accept as empty: it returns [] for a
    missing/non-list `reviewFindings`, so this CLI envelope guard is what turns a malformed
    input into a refusal instead of a zero-work 'success' (audit PD-M1-VALIDATOR-SHAPE)."""
    if source == "-":
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
    else:
        try:
            raw = Path(source).read_bytes()
        except OSError as exc:
            raise HarnessError(f"defect ingest: cannot read {source!r}: {exc}")
    # BOTH transports decode identically: a non-UTF-8 file (e.g. a PowerShell Set-Content /
    # Out-File default utf-16 — the exact byte-mangling the docstring warns of) then FAILS the
    # JSON parse below and refuses cleanly, never an uncaught UnicodeDecodeError. read_text's
    # strict decode raised a ValueError the file branch's `except OSError` could not catch, so
    # the file path crashed where its stdin twin refused (audit 2026-08-01, opus+sonnet).
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HarnessError(f"defect ingest: {source!r} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("reviewFindings"), list):
        raise HarnessError("defect ingest: input must be a JSON object with a "
                           "`reviewFindings` list (a reviewer HARNESS_RESULT) — nothing recorded")
    return data


def cmd_defect_ingest(args: Any) -> None:
    """`defect ingest [PATH|-]` — feed an INTERACTIVE reviewer's structured capsules into
    the SAME L3 pipeline the workflow reviewer uses (envelope-guard -> whole-batch validate
    -> record_defect -> cluster), so an interactive audit's findings stop dying in prose.
    Adds NO recorder, clusterer, class-mapper, or seat resolver of its own — one pipeline."""
    from harness_lib import common
    for flag in ("producer", "missed_by", "evidence"):
        if getattr(args, flag, None) is not None:
            raise HarnessError(f"defect ingest takes no --{flag.replace('_', '-')}: the "
                               "capsules carry their own seats/evidence — nothing recorded")
    source = getattr(args, "path", None) or "-"
    data = _read_reviewer_result(source)
    errs = validate_review_defects(data)  # whole batch: one bad capsule refuses all
    if errs:
        raise HarnessError("defect ingest refused (whole batch, nothing recorded): "
                           + "; ".join(errs))
    res = ingest_review_defects(common.ROOT, data,
                                source_path=(None if source == "-" else source))
    if res.get("held"):  # a gate holds .harness — a visible refusal, not a success payload
        raise HarnessError(res.get("note") or "defect ingest refused under a gate hold")
    common.emit(res)


def stale_pending(root: Path | str, days: int = STALE_DAYS) -> list[tuple[str, str, int]]:
    """(id, askedAt, ageDays) for pending entries older than `days` — doctor feed."""
    ref = parse_iso_datetime(now())
    out: list[tuple[str, str, int]] = []
    for e in pending(root):
        asked = parse_iso_datetime(str(e.get("askedAt") or ""))
        if asked is None or ref is None:
            continue
        age = (ref - asked).days
        if age > days:
            out.append((str(e.get("id")), str(e.get("askedAt")), age))
    return out


def decided(root: Path | str) -> list[dict[str, Any]]:
    """Decided entries (status in DECISIONS; pending is excluded — it is not a DECISION),
    newest decision first. decidedAt+note are already durable; `intake list --decided` only
    surfaces them so a decision stops vanishing into the queue with no trace."""
    out = list(enumerate(e for e in _load(Path(root))["entries"] if e.get("status") in DECISIONS))
    # sort by decidedAt desc; ties break on insertion index desc so a batch that shares a
    # decidedAt second still comes out later-decided-first, honouring "newest first".
    return [e for _i, e in sorted(out, key=lambda t: (str(t[1].get("decidedAt") or ""), t[0]), reverse=True)]


def decided_noteless(root: Path | str) -> list[str]:
    """Ids of decisions routed to real work (spec|backlog|experiment) that carry NO note —
    'decided but left no pointer to where the intent went', the cheap 'no trace' signal a
    doctor advisory nags on. discard/done are terminal and need no pointer."""
    actionable = {"spec", "backlog", "experiment"}
    return [str(e.get("id")) for e in decided(root)
            if e.get("status") in actionable and not str(e.get("note") or "").strip()]


def cmd_intake(args) -> int:
    from harness_lib import common

    action = getattr(args, "action", "list") or "list"
    params = list(getattr(args, "params", []) or [])
    if action == "add":
        common.emit(add(common.ROOT, " ".join(params)))
        return 0
    if action == "decide":
        if len(params) < 2:
            raise HarnessError("intake decide needs <id> <spec|backlog|discard|experiment>")
        out = decide(common.ROOT, params[0], params[1], note=getattr(args, "note", None) or "")
        if out.get("held"):  # mirror cmd_defect_ingest: a hold refusal surfaces, not a silent rc 0
            raise HarnessError(out.get("note") or "gate-hold: intake decide refused")
        common.emit(out)
        return 0
    if getattr(args, "decided", False):  # read-only: surface DECIDED entries, newest first
        drows = decided(common.ROOT)
        common.emit(drows if getattr(args, "json", False) or common.agent_output_compact()
                    else {"decided": drows, "count": len(drows)}, tsv=True)
        return 0
    rows = pending(common.ROOT)
    common.emit(rows if getattr(args, "json", False) or common.agent_output_compact()
                else {"pending": rows, "count": len(rows),
                      "next": "harness.py intake decide <id> spec|backlog|discard|experiment"},
                tsv=True)  # TE.5/SPEC-139: compact -> TSV rows; bare/--json stay indent=2
    return 0


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake = "sk-FAKEKEYdonotleak0123456789abcdef"
        first = add(root, f"quero que o painel mostre o custo {fake}", source="prompt-hook")
        assert first["deduped"] is False and first["pending"] == 1
        assert add(root, "quero que o painel  mostre o custo " + fake)["deduped"] is True
        stored = pending(root)[0]
        assert fake not in stored["ask"] and "sk-F…(len=" in stored["ask"]
        out = decide(root, first["id"], "backlog", note="vai pro backlog do painel")
        assert out["status"] == "backlog" and out["pending"] == 0
        # SPEC-144 Phase 3: `done` is the routed-and-committed terminal state.
        second = add(root, "route this small docs touch-up", source="prompt-hook")
        assert decide(root, second["id"], "done", note="route: abc1234")["status"] == "done"
        # decided view (S2) + decided-noteless doctor feed (S3)
        dv = decided(root)
        assert {e["id"] for e in dv} == {first["id"], second["id"]} and len(dv) == 2
        assert decided_noteless(root) == []  # first=backlog has a note; second=done is terminal
        third = add(root, "spec-worthy ask left with no pointer", source="prompt-hook")
        decide(root, third["id"], "spec")  # NO note -> the 'no trace' case the doctor nags on
        assert decided_noteless(root) == [third["id"]]
        # gate-hold guard (R1): a decide under a live hold refuses (held) and mutates nothing
        _hold = root / ".harness" / "runs" / "gate-hold" / "h"; _hold.mkdir(parents=True)
        (_hold / "h.json").write_text("{}", encoding="utf-8")
        _held = decide(root, first["id"], "discard")
        assert _held.get("held") is True and "status" not in _held
        assert next(e for e in _load(root)["entries"] if e["id"] == first["id"])["status"] == "backlog"
        # ordering the plan hinges on: guard runs BEFORE entry lookup (an unknown id under a
        # hold is refused as held, not "unknown id"), but AFTER decision validation (a bad
        # decision still raises even under a hold).
        assert decide(root, "ghost-under-hold", "spec").get("held") is True
        try:
            decide(root, first["id"], "maybe"); raise AssertionError("bad decision under hold must raise")
        except HarnessError as _e:
            assert "unknown decision" in str(_e)
        import shutil as _shutil; _shutil.rmtree(root / ".harness" / "runs" / "gate-hold")
        try:
            decide(root, "ghost", "spec")
            raise AssertionError("unknown id accepted")
        except HarnessError as exc:
            assert "unknown intake entry" in str(exc)
        try:
            decide(root, first["id"], "maybe")
            raise AssertionError("bad decision accepted")
        except HarnessError as exc:
            assert "unknown decision" in str(exc)
        data = _load(root)
        data["entries"][0]["askedAt"] = "2026-07-01T00:00:00-03:00"
        data["entries"][0]["status"] = "pending"
        write_json(root / QUEUE_REL, data)
        stale = stale_pending(root)
        assert len(stale) == 1 and stale[0][2] >= 7
        (root / QUEUE_REL).write_text("{corrupt", encoding="utf-8")
        assert pending(root) == []  # corrupt queue reads calm

    # L3 defect-ledger-to-fix-queue: structured capsule -> ledger + clustered intake.
    with tempfile.TemporaryDirectory() as tmp2:
        r2 = Path(tmp2)
        seat = {"role": "implementer", "model": "opus", "effort": "high"}
        rev = lambda ref, cls="silent-drop": {"reviewFindings": [  # noqa: E731
            {"severity": "high", "title": "t", "evidence": ["x"], "recommendation": "y",
             "defect": {"defectClass": cls, "evidence": {"kind": "audit", "ref": ref},
                        "producedBy": seat, "missedBy": None}},
            {"severity": "low", "title": "no capsule", "evidence": ["z"], "recommendation": "w"}]}
        i1 = ingest_review_defects(r2, rev("abc123#one"))
        assert i1["recorded"] == 1 and i1["clustersCreated"] == 1, i1
        clus = [e for e in pending(r2) if e.get("source") == DEFECT_SOURCE]
        assert len(clus) == 1 and clus[0]["artifactRef"] == "abc123", clus
        assert clus[0]["evidenceRefs"] == ["abc123#one"] and clus[0]["defectClass"] == "silent-drop"
        # rerun same key + a NEW ref from the same artifact -> upsert in place, no 2nd cluster
        i2 = ingest_review_defects(r2, rev("abc123#two"))
        assert i2["clustersCreated"] == 0 and i2["clustersUpdated"] == 1, i2
        clus = [e for e in pending(r2) if e.get("source") == DEFECT_SOURCE]
        assert len(clus) == 1 and clus[0]["evidenceRefs"] == ["abc123#one", "abc123#two"], clus
        # a DIFFERENT artifact is its own cluster
        ingest_review_defects(r2, rev("zzz999#a"))
        assert len([e for e in pending(r2) if e.get("source") == DEFECT_SOURCE]) == 2
        # batch refusal: one bad class -> NOTHING lands (no record, no cluster)
        bad = {"reviewFindings": [
            {"severity": "high", "title": "ok", "evidence": ["x"], "recommendation": "y",
             "defect": {"defectClass": "silent-drop", "evidence": {"kind": "audit", "ref": "bad#a"},
                        "producedBy": seat}},
            {"severity": "high", "title": "bad", "evidence": ["x"], "recommendation": "y",
             "defect": {"defectClass": "NOT-A-CLASS", "evidence": {"kind": "audit", "ref": "bad#b"},
                        "producedBy": seat}}]}
        ib = ingest_review_defects(r2, bad)
        assert ib["recorded"] == 0 and ib["clustersCreated"] == 0 and ib.get("errors"), ib
        assert not any(e["artifactRef"] == "bad" for e in pending(r2)
                       if e.get("source") == DEFECT_SOURCE), "bad batch must not land"
        # a finding with NO capsule and no findings at all -> nothing recorded, no error
        assert ingest_review_defects(r2, {"reviewFindings": []}) == {
            "recorded": 0, "clustersCreated": 0, "clustersUpdated": 0,
            "alreadyDecided": 0, "source": ""}
        # a *self* seat in an artifact is refused (would resolve to the recorder)
        assert validate_defect_capsule({"defect": {"defectClass": "silent-drop",
            "evidence": {"kind": "audit", "ref": "r#1"}, "producedBy": "self"}}), "self must be invalid"
        # M1 loader + `defect ingest` handler: a valid object loads; a list, an object with
        # a missing/non-list reviewFindings, and invalid JSON each REFUSE (the envelope guard
        # validate_review_defects lacks); stdin decodes bytes; a held ingest raises, never emits.
        import io as _io
        g = r2 / "g.json"; g.write_text('{"reviewFindings": []}', encoding="utf-8")
        assert _read_reviewer_result(str(g)) == {"reviewFindings": []}
        for bad_in in ("[]", "{}", '{"reviewFindings": 1}', "nope{"):
            b = r2 / "b.json"; b.write_text(bad_in, encoding="utf-8")
            try:
                _read_reviewer_result(str(b)); raise AssertionError(f"envelope must refuse {bad_in!r}")
            except HarnessError:
                pass
        # a non-UTF-8 file (PowerShell Set-Content/Out-File default utf-16) REFUSES cleanly,
        # never an uncaught UnicodeDecodeError — the file branch must decode like its stdin twin.
        u16 = r2 / "u16.json"; u16.write_bytes('{"reviewFindings": []}'.encode("utf-16"))
        try:
            _read_reviewer_result(str(u16)); raise AssertionError("non-utf8 file must refuse cleanly")
        except HarnessError:
            pass
        _sin = sys.stdin
        sys.stdin = type("S", (), {"buffer": _io.BytesIO(b'{"reviewFindings": []}')})()
        try:
            assert _read_reviewer_result("-") == {"reviewFindings": []}
        finally:
            sys.stdin = _sin
        from harness_lib import common as _common
        hold = r2 / ".harness" / "runs" / "gate-hold" / "h"; hold.mkdir(parents=True)
        (hold / "h.json").write_text("{}", encoding="utf-8")
        cap = r2 / "c.json"
        cap.write_text(json.dumps({"reviewFindings": [{"defect": {"defectClass": "silent-drop",
            "evidence": {"kind": "audit", "ref": "h#1"}, "producedBy": seat}}]}), encoding="utf-8")
        _root = _common.ROOT; _common.ROOT = r2
        try:
            cmd_defect_ingest(type("A", (), {"defect_class": "ingest", "path": str(cap),
                              "producer": None, "missed_by": None, "evidence": None})())
            raise AssertionError("held ingest must refuse, never emit")
        except HarnessError:
            pass
        finally:
            _common.ROOT = _root
    print("intake_queue self-check: ok")


if __name__ == "__main__":
    _demo()
