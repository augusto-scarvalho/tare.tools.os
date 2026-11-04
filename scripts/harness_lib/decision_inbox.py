#!/usr/bin/env python3
"""Plain-language human decision inbox (SPEC-145).

Every pending human decision in the harness hides behind subcommand grammar the
owner does not know (`intake decide <id> spec|backlog|discard|experiment|done`,
`escalations --resolve <id> --target <subject>`). This module is the read-only
collector plus the single apply path that turns each pending decision into a
plain question with selectable answers -- surfaced identically in the CLI
(`harness.py decide`) and the browser panel (Decisions card).

REUSE, never re-implement:
  * `intake_queue.pending` / `decide` / `DECISIONS` -- the intake read + write;
  * `escalations_lib` (read-only ledger read + `compact_supervision_events`
    fold) -- the escalation read + resolve;
  * `prompt_kit` (`select` / `confirm` / `Option` / `detect_mode`) -- the CLI
    "buttons" with the tui/numbered/no-input ladder already solved.

Owner-typed approval tokens (`--approve-writes`) and every `--force`/`scrub`
variant are NEVER decisions here: intake choices are constrained to
`intake_queue.DECISIONS`, escalation choices to resolve/keep. Side-effect free on
import; stdlib only; prompts render on stderr, stdout keeps the JSON contract.
Self-check: python scripts/harness_lib/decision_inbox.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import (  # noqa: E402
    acceptances, escalations_lib, intake_queue, processes, prompt_kit, responsibility,
)
from harness_lib.common import HarnessError, now, parse_iso_datetime, read_json, replay_class  # noqa: E402

ASK_EXCERPT = 600  # chars of the ask shown in a question (a pointer, not a transcript);
# ~600 so the card tooltip + confirm modal carry the full ask (SPEC-145 v2); the card
# itself clamps to 2 lines client-side.
SLO_HOURS = 48
EXPIRY_HOURS = 168

# One-line plain-English hint per intake decision -- the "what does this button do".
INTAKE_HINTS = {
    "spec": "promote to a spec (feature-shaped work)",
    "backlog": "park it on the backlog",
    "discard": "drop it, records the note",
    "experiment": "promote to a pre-registered experiment",
    "done": "handled already, close it",
}
# Notes always record the deciding surface (SPEC-145 invariant 4).
NOTE_INTERACTIVE = "decided via harness.py decide (interactive)"
NOTE_ONESHOT = "decided via harness.py decide"


def _acceptance_rows(root: Path, measured_at: dt.datetime, slo_hours: float,
                     expiry_hours: float) -> list[dict[str, Any]]:
    """One row per vendor with hooks it refuses to run (SPEC-172 inv. 18), from the
    CACHED probe -- never a live one. This collector runs on every panel poll and
    every `decide`; spawning a `codex app-server` per poll is not a read.

    An absent cache yields NO row. "Nobody has asked the vendor yet" and "the vendor
    has nothing pending" are different facts, and only the second may render a clear
    inbox -- so silence here means unknown, and the surface that knows how to ask
    (`harness.py acceptances`, the SessionStart hook) is what fills it in.

    `askedAt` is the PROBE time, not the day the hooks went inert -- the vendor does
    not report when trust lapsed. So the age here reads "as of this probe", and a
    probe that stops being refreshed ages into the expiry refusal, which is the safe
    direction: a stale approval is re-read, never auto-applied.
    """
    rows: list[dict[str, Any]] = []
    cache = acceptances.cached(root)
    probed_at = cache.get("probedAt")
    for report in cache.get("reports") or []:
        if not isinstance(report, dict) or report.get("status") != "pending":
            continue
        # The cache is a FILE, so its vendor is untrusted input, not a name we chose.
        # Accepting an unknown one would hand `acceptance_plan` a vendor with no probe
        # (KeyError through a contract that promises never to raise) and, with codex off
        # PATH, put a cache-supplied string in argv[0] of a real spawn. The browser was
        # never allowed to name a vendor; neither is a file.
        vendor = str(report.get("vendor") or "")
        if vendor not in acceptances.PROBES:
            continue
        pending = [p for p in (report.get("pending") or []) if isinstance(p, dict)]
        if not pending:
            continue
        named = acceptances.labels(pending)
        who = ", ".join(named[:3]) + (f" +{len(named) - 3} more" if len(named) > 3 else "")
        # A non-numeric `running` must not raise out of the collector: every panel poll
        # and every `decide` walks this loop.
        running = report.get("running")
        total = len(pending) + (running if isinstance(running, int)
                                and not isinstance(running, bool) else 0)
        rows.append({
            "id": f"acceptance-{vendor}", "kind": "acceptance", "vendor": vendor,
            # Same shape as the session-start line, so the two surfaces read as one
            # fact: what is inert, how much of the repo it is, then the question.
            "question": (f"{vendor} refuses to run {len(pending)} of this repo's {total} "
                         f"hook(s) ({who}). Accept them?"),
            "detail": f"{vendor}: {who}",
            # Three doors, and the middle one is the SAFEST of them — not the most
            # advanced. `accept` lets a MODEL pick the keystroke (behind the grounding
            # rail); `visible` hands the keyboard to the owner, so the model call, the
            # screen reader and the drive budget all disappear. It is offered plainly,
            # with no "advanced" label and nothing hiding it.
            "options": [
                {"value": "accept", "label": "Accept",
                 "hint": f"types your yes into {vendor}'s own prompt; falls back to "
                         "handing you the command"},
                {"value": "visible", "label": "Eu mesmo faço",
                 "hint": f"opens {vendor}'s own prompt in a visible terminal and you accept "
                         "the hooks with your own hands; no model reads or types anything"},
                # The consequence, named and TRUE: the vendor keeps working, it is the
                # repo's guards that stay silent inside it. "You will not be able to use
                # the vendor" would be the convenient lie; the count is the honest one.
                {"value": "keep", "label": "Agora não",
                 "hint": f"leaves them inert: {vendor} lanes keep running with "
                         f"{len(pending)} of this repo's guards silent"},
            ],
            "askedAt": probed_at,
            # Binds the approval to THIS set of hooks: one more inert hook since the
            # card was drawn and the click is refused as invalidated, not silently
            # spent on something the owner never saw (C12, same rail as intake).
            "digest": _decision_digest("acceptance", _acceptance_content(pending), vendor),
            **_age_fields(probed_at, measured_at, slo_hours, expiry_hours),
        })
    return rows


def _acceptance_content(pending: list[dict[str, Any]]) -> str:
    """The load-bearing content of an acceptance: WHICH hooks, in what trust state.
    Sorted so vendor ordering cannot change the digest on its own."""
    return "\n".join(sorted(
        f"{p.get('trustStatus')}|{p.get('event')}|{p.get('matcher')}|{p.get('command')}"
        for p in pending))


def _pending_escalations(root: Path) -> list[dict[str, Any]]:
    """Pending escalations from the durable ledger, READ-ONLY: `raised` minus
    `resolvedIds`, exactly like `ui_panel._escalations`. It never folds
    events.jsonl (that would write) so the collector stays side-effect free.
    Degrades to [] on a missing/corrupt ledger."""
    state = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
    resolved = set(state.get("resolvedIds") or [])
    rows = [rec for eid, rec in (state.get("raised") or {}).items()
            if eid not in resolved and isinstance(rec, dict)]
    rows.sort(key=lambda r: str(r.get("raisedAt") or ""))
    return rows


def _demojibake(s: str) -> str:
    """Display-only repair of Windows-cp1252-captured UTF-8 mojibake (SPEC-145 v2).

    The UserPromptSubmit hook historically read stdin without forcing UTF-8, so on
    Windows a prompt's UTF-8 bytes were decoded as cp1252 -- 'ç' became 'Ã§' (one
    level) or 'ÃƒÂ§' (two). The exact inverse is a cp1252 re-encode + UTF-8 decode
    (cp1252, NOT latin-1: the two-level form carries chars in 0x80-0x9F such as
    U+0192 that latin-1 cannot encode). Strict-roundtrip gated: apply a round ONLY
    when the string re-encodes as cp1252 AND re-decodes as UTF-8 AND the result is
    SHORTER -- mojibake collapses multi-byte runs, so clean text (a genuine 'ç',
    'café', a non-latin string) round-trips to an encode/decode error or does not
    shrink and is returned untouched. At most two rounds (two observed levels).
    NEVER written back to the queue or any state file -- display only.
    ponytail: heuristic, the strict-decode + shrink guard IS the safety."""
    out = s
    for _ in range(2):
        try:
            cand = out.encode("cp1252").decode("utf-8")
        except UnicodeError:
            break
        if len(cand) < len(out):
            out = cand
        else:
            break
    return out


def _excerpt(text: str) -> str:
    text = _demojibake(" ".join(str(text or "").split()))
    return text if len(text) <= ASK_EXCERPT else text[:ASK_EXCERPT - 1] + "…"


def _decision_digest(kind: str, content: str, subject: str = "") -> str:
    """C12 (ref-arch round, article §7.7): sha256 over the RAW, load-bearing
    content a decision approves -- the intake `ask` or the escalation
    `reason`+`subject`, not the truncated/demojibaked excerpt. Binds the approval
    to the exact bytes shown so a mutation between show and apply is detectable
    (the plan-gate `planSha256` pattern, ported to the decision inbox). NUL
    field-separators keep the kind/content/subject boundaries unambiguous."""
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\x00")
    h.update((content or "").encode("utf-8"))
    if subject:
        h.update(b"\x00")
        h.update(subject.encode("utf-8"))
    return h.hexdigest()


def decision_digest_v2(decision_id: str, kind: str, content: str, subject: str = "") -> str:
    """Versioned, canonical decision digest for future promotion authority.

    V1 remains readable historical evidence.  V2 adds the decision identifier,
    preventing a valid approval digest from being replayed for another row with
    otherwise identical semantic payload.
    """
    payload = {"digestVersion": "v2", "decisionId": decision_id, "kind": kind,
               "content": content or "", "subject": subject or ""}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def decision_identity_record(decision_id: str, kind: str, content: str, subject: str = "") -> dict[str, str]:
    """Canonical persisted V2 identity for decisions created after D2."""
    return {"digestVersion": "v2", "decisionDigest": decision_digest_v2(decision_id, kind, content, subject)}


def validate_decision_identity(record: Any) -> dict[str, Any]:
    """Recompute stored decision identity; never trust receipt-provided bytes."""
    if not isinstance(record, dict):
        return {"status": "MALFORMED", "promotionEligible": False}
    version = record.get("digestVersion")
    if version in (None, "v1"):
        return {"status": "LEGACY_V1", "promotionEligible": False}
    if version != "v2":
        return {"status": "UNSUPPORTED_VERSION", "promotionEligible": False}
    fields = ("id", "kind", "content")
    if any(not isinstance(record.get(field), str) or not record.get(field) for field in fields):
        return {"status": "MALFORMED", "promotionEligible": False}
    expected = decision_digest_v2(record["id"], record["kind"], record["content"], str(record.get("subject") or ""))
    if record.get("decisionDigest") != expected:
        return {"status": "INVALID_DIGEST", "promotionEligible": False, "expectedDigest": expected}
    return {"status": "VALID_V2", "promotionEligible": True, "decisionDigest": expected}


def _promotion_record(root: Path, decision_id: str, *, test_only: bool) -> dict[str, Any] | None:
    """Resolve promotion evidence from canonical state, never from a receipt."""
    if test_only:
        payload = read_json(root / ".harness" / "state-store" / "test-promotion-decisions.json", {}) or {}
        rows = payload.get("records") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return next((row for row in rows if isinstance(row, dict) and row.get("id") == decision_id), None)
        return None
    # Current production intake choices deliberately contain no verifier-promotion
    # authority.  This resolver therefore fails closed until the Genesis ceremony
    # supplies its separately governed decision source.
    return None


def validate_promotion_decision_authority(root: Path | str, receipt: Any, *, test_only: bool = False) -> dict[str, Any]:
    """Future promotion seam.  The receipt names canonical evidence; it grants nothing."""
    if not isinstance(receipt, dict):
        return {"ok": False, "reason": "PROMOTION_RECEIPT_CANNOT_SELF_ASSERT_DECISION_AUTHORITY"}
    record = _promotion_record(Path(root), str(receipt.get("decisionId") or ""), test_only=test_only)
    if record is None:
        return {"ok": False, "reason": "PROMOTION_RECEIPT_CANNOT_SELF_ASSERT_DECISION_AUTHORITY"}
    identity = validate_decision_identity(record)
    if identity["status"] != "VALID_V2" or not isinstance(record, dict) or not isinstance(receipt, dict):
        return {"ok": False, "reason": "PROMOTION_RECEIPT_CANNOT_SELF_ASSERT_DECISION_AUTHORITY", "identity": identity}
    actor = record.get("actor")
    allowed_actor = "TEST_ONLY_OWNER" if test_only else "accepter"
    valid = (record.get("status") == "resolved" and record.get("choice") == "promote-verifier"
             and record.get("purpose") == "trusted-verifier-promotion"
             and isinstance(actor, dict) and actor.get("role") == allowed_actor
             and receipt.get("decisionId") == record["id"]
             and receipt.get("decisionDigest") == identity["decisionDigest"])
    if not valid:
        return {"ok": False, "reason": "DECISION_IDENTITY_DOES_NOT_EQUAL_PROMOTION_AUTHORITY", "identity": identity}
    # TCP-03: expose the canonical decision's binding fields (subject scope,
    # expiry, consumption) so the promotion seam can enforce them without a
    # second decision store. Read-only projection of the canonical row.
    return {"ok": True, "authority": "TEST_ONLY" if test_only else "PRODUCTION", "identity": identity,
            "decisionBinding": {"subject": record.get("subject"), "expiresAt": record.get("expiresAt"),
                                "consumedBy": record.get("consumedBy")}}


def _limits(root: Path) -> tuple[float, float]:
    config = (read_json(root / ".harness" / "project.json", {}) or {}).get("decisionInbox", {})
    if not isinstance(config, dict):
        config = {}
    slo = config.get("sloHours", SLO_HOURS)
    expiry = config.get("expiryHours", EXPIRY_HOURS)
    return (
        float(slo) if isinstance(slo, (int, float)) and not isinstance(slo, bool) else float(SLO_HOURS),
        float(expiry) if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) else float(EXPIRY_HOURS),
    )


def _age_fields(timestamp: Any, measured_at: dt.datetime, slo_hours: float,
                expiry_hours: float) -> dict[str, Any]:
    parsed = parse_iso_datetime(timestamp)
    if parsed is None:
        return {"ageHours": None, "sloBreached": False, "expired": False, "limitHours": expiry_hours}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    age = round((measured_at - parsed.astimezone(dt.timezone.utc)).total_seconds() / 3600, 1)
    return {"ageHours": age, "sloBreached": age > slo_hours,
            "expired": age > expiry_hours, "limitHours": expiry_hours}


def pending_decisions(root: Path | str) -> list[dict[str, Any]]:
    """READ-ONLY collector: every pending human decision as a plain question with
    selectable options. Vendor acceptances first (a vendor refusing this repo's hooks
    is why the other rows' guards may be silent), then escalations (they block), then
    intake by `askedAt` ascending. Row shape:
    `{"id","kind","question","detail","options":[{"value","label","hint"}],"askedAt",
    "ageHours","sloBreached","subject"?}`.
    Never dumps file contents -- detail excerpts are capped."""
    root = Path(root)
    slo_hours, expiry_hours = _limits(root)
    measured_at = dt.datetime.now(dt.timezone.utc)
    rows: list[dict[str, Any]] = []
    # Acceptances lead: a vendor refusing to run this repo's guards is the reason the
    # other rows' guards may not have fired at all.
    rows.extend(_acceptance_rows(root, measured_at, slo_hours, expiry_hours))
    for esc in _pending_escalations(root):
        eid = str(esc.get("escalationId") or esc.get("id") or "")
        title = _excerpt(str(esc.get("reason") or esc.get("suggestedProfile")
                             or esc.get("taskId") or "").split("\n", 1)[0])
        rows.append({
            "id": eid, "kind": "escalation",
            "question": f"Resolve escalation {eid}" + (f" ({title})" if title else "") + "?",
            "detail": title,
            "options": [
                {"value": "resolve", "label": "Resolve", "hint": "mark it handled"},
                {"value": "keep", "label": "Keep", "hint": "leave it pending (no change)"},
            ],
            "askedAt": esc.get("raisedAt"),
            "subject": esc.get("subject") or "self",
            "digest": _decision_digest("escalation", str(esc.get("reason") or ""),
                                       str(esc.get("subject") or "self")),
            **decision_identity_record(eid, "escalation", str(esc.get("reason") or ""),
                                        str(esc.get("subject") or "self")),
            **_age_fields(esc.get("raisedAt"), measured_at, slo_hours, expiry_hours),
        })
    for entry in sorted(intake_queue.pending(root), key=lambda e: str(e.get("askedAt") or "")):
        eid = str(entry.get("id") or "")
        excerpt = _excerpt(entry.get("ask"))
        rows.append({
            # The card/menu shows the id in its eid chip / option description, so the
            # question is just the quoted ask excerpt (SPEC-145 v3, owner 2026-07-15).
            "id": eid, "kind": "intake",
            "question": f'"{excerpt}"?',
            "detail": excerpt,
            "options": [{"value": d, "label": d, "hint": INTAKE_HINTS.get(d, "")}
                        for d in intake_queue.DECISIONS],
            "askedAt": entry.get("askedAt"),
            "digest": _decision_digest("intake", str(entry.get("ask") or "")),
            **decision_identity_record(eid, "intake", str(entry.get("ask") or "")),
            **_age_fields(entry.get("askedAt"), measured_at, slo_hours, expiry_hours),
        })
    return rows


def _refuse(decision_id: str, choice: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "id": decision_id, "choice": choice, "refused": reason, **extra}


def _resolve_escalation(root: Path, esc_id: str, note: str) -> dict[str, Any]:
    """Durable escalation resolve, root-parameterized: record the
    `escalation_resolved` event and fold it into the ledger via
    `escalations_lib.compact_supervision_events` -- the SAME durable fold the
    `escalations --resolve` subcommand triggers. Reuses the lib fold rather than
    re-implementing resolvedIds bookkeeping, and never shells out."""
    events_path = root / ".harness" / "runs" / "events.jsonl"
    state_path = root / ".harness" / "state" / "escalations.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"escalationId": esc_id,
                               "actor": responsibility.make_actor("accepter")}
    if note:
        payload["note"] = note
    from harness_lib import event_ledger
    event_ledger.append_critical("escalation_resolved", payload, root=root)
    escalations_lib.compact_supervision_events(events_path, state_path)

    return {"ok": True, "id": esc_id, "kind": "escalation", "choice": "resolve",
            "status": "resolved", "note": note, "actor": payload["actor"]}


def _accept_vendor(root: Path, row: dict[str, Any], note: str) -> dict[str, Any]:
    """Spend the owner's yes on the vendor that asked for it (SPEC-172 inv. 18).

    The vendor comes from the ROW, looked up against the currently-pending inbox --
    never from the caller -- and the mode is fixed HERE, not chosen by the clicker:
    from a browser, `visible` (which only hands back a command to type) would be a
    dead end, and letting the surface pick a mode would put a vendor argv under the
    clicker's control. So the panel gets exactly one behavior: drive the hidden pty,
    and on ANY refusal fall back to the visible instruction, which is then reported
    back as a failure carrying that instruction rather than a silent no-op.
    """
    driven = acceptances.acceptance_plan(root, row["vendor"], "headless")
    # Re-park the probe whatever happened: this card was drawn from the cache, and a
    # cache that survives the drive would keep offering a button for finished work.
    acceptances.refresh(root)
    out = {"id": row["id"], "kind": "acceptance", "choice": "accept",
           "vendor": row["vendor"], "status": driven.get("status"),
           "reason": driven.get("reason"), "note": note}
    if driven.get("transcript"):
        out["transcript"] = driven["transcript"]
    if driven.get("transcriptWarning"):
        out["transcriptWarning"] = driven["transcriptWarning"]
    if driven.get("status") == "accepted":
        return {"ok": True, **out, "sends": driven.get("sends")}
    fallback = (driven.get("fallback") or {}).get("instruction")
    return _refuse(row["id"], "accept",
                   f"{driven.get('reason') or 'the drive did not complete'}"
                   + (f" -- {fallback}" if fallback else ""),
                   # id/choice/reason are _refuse's own positionals; passing any of them
                   # again is a TypeError, not a duplicate key. `reason` is already folded
                   # into the refusal string above, so dropping the bare key loses nothing.
                   **{k: v for k, v in out.items() if k not in ("id", "choice", "reason")})


def _own_hands(root: Path, row: dict[str, Any], note: str) -> dict[str, Any]:
    """The owner accepts with their OWN hands: open the vendor's prompt where they can
    see it, and hand back the command that does the same thing.

    The SAFEST of the three doors. Nothing here may reach `drive_headless`: no pty, no
    screen reader, no model call, no drive budget — the only thing that chooses a
    keystroke is the human at the keyboard. The mode is fixed HERE from the CHOICE,
    exactly like `_accept_vendor` fixes `headless`; the browser never names one.

    The argv is the PLAN's own (`_visible_plan` resolves the vendor binary absolutely);
    no caller string — not the note, not the id — is ever part of it.

    Opening the console is best effort by contract: a session with no console, a vendor
    that is not on PATH, or a POSIX host all degrade to the instruction instead of
    failing. The command travels EITHER WAY — the button presumes browser and server on
    one machine, and the printed command is what survives when they are not.
    """
    plan = acceptances.acceptance_plan(root, row["vendor"], "visible")
    launch = plan.get("launch") or {}
    argv = [str(a) for a in (launch.get("argv") or [])]
    if not argv:  # nothing-pending / probe-failed: there is no plan to hand over
        return _refuse(row["id"], "visible",
                       str(plan.get("reason") or "no visible plan for this vendor"),
                       kind="acceptance", vendor=row["vendor"], status=plan.get("status"))
    cwd = str(launch.get("cwd") or root)
    opened = processes.open_console(argv, cwd=cwd)
    return {
        "ok": True, "id": row["id"], "kind": "acceptance", "choice": "visible",
        "vendor": row["vendor"], "mode": "visible", "status": "handed-over",
        "opened": opened,
        # ONE string, always present: the prose plus the literal command line. This is
        # what the panel renders and what the owner can select and paste.
        "instruction": f"{plan.get('instruction') or ''} "
                       f"Command: {' '.join(argv)}  (cwd: {cwd})".strip(),
        "note": note,
    }


def apply_decision(root: Path | str, decision_id: str, choice: str, note: str = "",
                   expected_digest: str = "", allow_expired: bool = False) -> dict[str, Any]:
    """Apply ONE human decision. ALWAYS returns a dict, never raising through the
    CLI JSON contract. Dispatch by the decision's REAL kind (looked up against the
    currently-pending inbox -- approval-as-record, never an arbitrary string):
      * intake                 -> `intake_queue.decide` (choice in DECISIONS);
      * escalation + resolve   -> the durable escalation resolve;
      * escalation + keep      -> structured refusal (keep is a deliberate no-op);
      * unknown id / bad choice -> structured refusal.
    `{"ok": True|False, ...}`; `ok` False carries a `refused` reason.

    C12 (article §7.7): `expected_digest`, when given, binds the approval to the
    exact content the human saw. If the live decision's `digest` no longer matches
    (the ask/escalation mutated since it was shown), the decision is REFUSED as
    invalidated -- closing the time-of-check/time-of-use gap. Absent it, behavior
    is byte-identical (backward compatible)."""
    root = Path(root)
    decision_id = str(decision_id)
    choice = str(choice)
    row = next((r for r in pending_decisions(root) if r["id"] == decision_id), None)
    if row is None:
        return _refuse(decision_id, choice, f"unknown or already-decided decision id {decision_id!r}")
    if row.get("expired") and not allow_expired:  # row carries expired + limitHours (one source)
        return _refuse(decision_id, choice,
                       "approval expired: re-read the current decision, then retry with --allow-expired",
                       expired=True, ageHours=row["ageHours"], limitHours=row["limitHours"])
    if expected_digest and expected_digest != row.get("digest"):
        return _refuse(decision_id, choice,
                       "approval invalidated: the decision content changed since it was shown "
                       f"(expected {expected_digest[:12]}, live {str(row.get('digest'))[:12]}) "
                       "-- re-read the current decision and decide again",
                       invalidated=True, liveDigest=row.get("digest"))
    valid = {o["value"] for o in row["options"]}
    if choice not in valid:
        return _refuse(decision_id, choice,
                       f"choice {choice!r} is not an option for {decision_id} "
                       f"(pick one of: {', '.join(sorted(valid))})")
    if row["kind"] == "acceptance":
        if choice == "keep":  # no-op: the hooks stay inert
            return _refuse(decision_id, "keep",
                           "keep is a no-op (leaves the hooks inert); omit it to do nothing",
                           kind="acceptance", noop=True)
        if choice == "visible":  # the owner types it themselves; no model, no pty
            return _own_hands(root, row, note)
        return _accept_vendor(root, row, note)
    if row["kind"] == "intake":
        try:
            out = intake_queue.decide(root, decision_id, choice, note=note)
        except HarnessError as exc:  # keep the JSON contract: no raise escapes here
            return _refuse(decision_id, choice, str(exc).split("\n", 1)[0])
        if out.get("held"):  # gate-hold refusal is not a success (mirror cmd_defect_ingest);
            # the front-door hold_write_guard is checked once, but the interactive picker can
            # wait on a human long enough for a hold to start before decide() runs.
            return _refuse(decision_id, choice, out.get("note") or "gate-hold: intake decide refused")
        return {"ok": True, "id": decision_id, "kind": "intake", "choice": choice,
                "status": out.get("status"), "pending": out.get("pending"), "note": note}
    if choice == "keep":  # no-op: the escalation stays pending
        return _refuse(decision_id, "keep",
                       "keep is a no-op (leaves the escalation pending); omit it to do nothing",
                       kind="escalation", noop=True)
    return _resolve_escalation(root, decision_id, note)


# -- CLI verb (harness.py decide) --------------------------------------------

def _effect(row: dict[str, Any], choice: str) -> str:
    """The final effect restated for the confirm prompt (SPEC-145: a plain
    question, not a subcommand)."""
    if row["kind"] == "intake":
        verb = {"discard": "Discard", "spec": "Promote to spec", "backlog": "Backlog",
                "experiment": "Promote to experiment", "done": "Close"}.get(choice, choice.title())
        return f"{verb} demand {row['id']}?"
    if row["kind"] == "acceptance":
        # Names the actual effect, not the button: one of these types into a vendor's
        # TUI and the other one only opens it. Restating the wrong one is a lie at the
        # exact moment the owner is deciding.
        if choice == "visible":
            return (f"Open {row['vendor']}'s own prompt in a visible terminal so you can "
                    "accept the hooks yourself? Nothing is typed for you.")
        return (f"Accept {row['vendor']}'s pending hooks now? It drives {row['vendor']}'s "
                "own prompt in a hidden terminal, one authorized key at a time.")
    return f"Resolve escalation {row['id']}?"


def _display_question(row: dict[str, Any]) -> str:
    return ("[SLO] " if row.get("sloBreached") else "") + row["question"]


def _report(row: dict[str, Any]) -> None:
    result = row
    keep = {k: result.get(k) for k in ("id", "choice", "status", "refused") if result.get(k) is not None}
    sys.stderr.write(("done: " if result.get("ok") else "no change: ")
                     + json.dumps(keep, ensure_ascii=False) + "\n")


def _interactive_loop(root: Path, note: str) -> None:
    """Pick a pending decision, pick its answer, confirm the effect, apply; repeat
    until the queue is empty or the user quits. Every prompt renders via prompt_kit
    (stderr); keep/skip/quit mutate nothing."""
    while True:
        rows = pending_decisions(root)
        if not rows:
            sys.stderr.write("no pending decisions -- the inbox is clear\n")
            return
        # label is the plain question; the id now rides in the option description
        # (the intake question no longer carries a "Decide demand <id>:" prefix).
        menu = [prompt_kit.Option(r["id"], label=_display_question(r), description=r["id"]) for r in rows]
        menu.append(prompt_kit.Option("\x00quit", label="quit (leave the rest pending)"))
        picked = prompt_kit.select("Pending human decisions", menu)
        if picked == "\x00quit":
            return
        row = next(r for r in rows if r["id"] == picked)
        answers = [prompt_kit.Option(o["value"], label=o["label"], description=o.get("hint", ""))
                   for o in row["options"]]
        answers.append(prompt_kit.Option("\x00skip", label="skip (decide later)"))
        choice = prompt_kit.select(_display_question(row), answers)
        if choice in ("\x00skip", "keep"):
            continue
        if not prompt_kit.confirm(_effect(row, choice), default=False):
            continue
        _report(apply_decision(root, row["id"], choice, note or NOTE_INTERACTIVE))


def cmd_decide(args) -> int:
    from harness_lib import common

    root = common.ROOT
    params = list(getattr(args, "params", []) or [])
    note = getattr(args, "note", None) or ""
    want_json = bool(getattr(args, "json", False))
    want_list = bool(getattr(args, "list", False))

    if len(params) >= 2:  # one-shot: decide <id> <choice>
        result = apply_decision(root, params[0], params[1], note or NOTE_ONESHOT,
                                expected_digest=getattr(args, "expected_digest", None) or "",
                                allow_expired=bool(getattr(args, "allow_expired", False)))
        common.emit(result)
        if not result.get("ok"):
            raise HarnessError(result.get("refused") or "decision refused")
        return 0
    if len(params) == 1:
        raise HarnessError(
            f"decide {params[0]!r} needs a <choice>\n"
            "fix: decide <id> <choice>, or bare `decide` to choose interactively")

    rows = pending_decisions(root)
    # --list, and bare `decide` in no-input mode, both list and mutate nothing.
    if want_list or prompt_kit.detect_mode() == "no-input":
        rows = [{**row, "question": _display_question(row)} for row in rows]
        if want_json or prompt_kit.detect_mode() == "no-input" or common.agent_output_compact():
            common.emit(rows, tsv=True)
        else:
            common.emit({"pending": rows, "count": len(rows),
                         "next": "harness.py decide <id> <choice>, or run `decide` interactively"})
        return 0
    _interactive_loop(root, note)
    return 0


def _demo() -> None:
    import tempfile

    from harness_lib.common import write_json

    def _ago(hours: float) -> str:
        # Relative fixtures stay fresh forever (fixed dates aged past the 168h expiry as
        # this long session ran -- the same expiry-flake the scenario's _recent() fixed).
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Seed 2 pending intake entries (distinct askedAt) + 1 pending escalation.
        write_json(root / intake_queue.QUEUE_REL, {"schemaVersion": 1, "entries": [
            {"id": "aaaa11112222", "ask": "make the panel show costs", "source": "manual",
             "askedAt": _ago(10), "status": "pending"},
            {"id": "bbbb33334444", "ask": "add a dark mode toggle", "source": "manual",
             "askedAt": _ago(9), "status": "pending"},
            {"id": "cccc55556666", "ask": "already handled", "source": "manual",
             "askedAt": _ago(28), "status": "discard"},
        ]})
        write_json(root / ".harness" / "state" / "escalations.json", {
            "schemaVersion": "1.0", "raised": {
                "ESC-1@t": {"escalationId": "ESC-1@t", "reason": "worker blocked on missing spec",
                            "raisedAt": _ago(11), "subject": "self"}},
            "resolvedIds": [], "resolvedRecords": {}, "frictionLog": []})

        rows = pending_decisions(root)
        assert len(rows) == 3, rows
        assert rows[0]["kind"] == "escalation" and rows[0]["id"] == "ESC-1@t", rows[0]
        assert [o["value"] for o in rows[0]["options"]] == ["resolve", "keep"]
        # intake rows follow, sorted by askedAt ascending
        assert rows[1]["id"] == "aaaa11112222" and rows[2]["id"] == "bbbb33334444", rows
        assert [o["value"] for o in rows[1]["options"]] == list(intake_queue.DECISIONS)
        assert rows[1]["question"] == '"make the panel show costs"?', rows[1]["question"]
        # every row now carries expiry state: a young (10h) row is not expired, limit 168.0
        assert rows[1]["expired"] is False and rows[1]["limitHours"] == float(EXPIRY_HOURS), rows[1]

        # C12: every row carries a content digest; it binds the RAW ask, not the excerpt.
        assert all(len(r["digest"]) == 64 for r in rows), "every row has a sha256 digest"
        assert rows[1]["digest"] == _decision_digest("intake", "make the panel show costs")
        # a matching expected_digest is honored; a stale one is REFUSED as invalidated.
        good = rows[1]["digest"]
        assert apply_decision(root, "aaaa11112222", "backlog", expected_digest=good)["ok"] is True
        # (re-seed since the line above consumed the entry)
        write_json(root / intake_queue.QUEUE_REL, {"schemaVersion": 1, "entries": [
            {"id": "aaaa11112222", "ask": "make the panel show costs", "source": "manual",
             "askedAt": _ago(10), "status": "pending"}]})
        stale = apply_decision(root, "aaaa11112222", "discard", expected_digest="deadbeef" * 8)
        assert stale["ok"] is False and stale.get("invalidated") is True, stale
        assert stale["liveDigest"] == _decision_digest("intake", "make the panel show costs")

        # intake apply flips the entry and records the surface note
        out = apply_decision(root, "aaaa11112222", "discard", note="decided via test")
        assert out["ok"] and out["status"] == "discard", out
        stored = next(e for e in read_json(root / intake_queue.QUEUE_REL)["entries"]
                      if e["id"] == "aaaa11112222")
        assert stored["status"] == "discard" and stored["note"] == "decided via test", stored

        # refusals never raise through the JSON contract
        assert apply_decision(root, "ghostid", "discard")["ok"] is False
        assert apply_decision(root, "bbbb33334444", "nuke")["ok"] is False
        keep = apply_decision(root, "ESC-1@t", "keep")
        assert keep["ok"] is False and keep.get("noop") is True, keep

        # escalation resolve folds durably -> leaves the pending inbox
        res = apply_decision(root, "ESC-1@t", "resolve", note="decided via test")
        assert res["ok"] and res["status"] == "resolved", res
        assert not any(r["id"] == "ESC-1@t" for r in pending_decisions(root))
    print("decision_inbox self-check: ok")


if __name__ == "__main__":
    _demo()
