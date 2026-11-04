"""Mechanized pre-merge AUDIT: the FOURTH commit-join leg (gate || reckon || mutate || AUDIT).

The manual choreography this replaces is on record: an adversarial packet
(`.harness/handoff/audit-rule6-doors.md`), N cross-vendor seats reading it, and
one typed VERDICT per seat (`audit-rule6-doors-opus5-VERDICT.md`). It worked and
it was hand-driven, so it happened when someone remembered. This module is the
machine: packet assembly from the STAGED surface, governed seat spawn, verdict
collection + quorum, and a fingerprint-keyed record the commit gate enforces.

Split of responsibility (mirrors reckon/mutate):
  * HERE: the packet, the seats, the parse, the quorum, the CLI (`harness.py audit`);
  * `validation_stamp`: `check_audit` (the leg), `stamp_audit` (the record),
    `record_audit_override` (the --no-verify escape). That module is imported
    here; the reverse import is LAZY (inside `check_audit`), so there is no cycle.

Owner rulings encoded here (2026-08-03, round `audit-quorum-dinamico`):
  (b) seat count is a deterministic BAND LADDER over `.harness/audit-policy.json`;
      unanimity to SHIP; any BLOCK blocks; a blocker-severity finding blocks
      regardless of majority; a split names the dissenting seat.
  (c) mechanical predicate + `audit waive --reason` (block -> warn, actor-recorded).
  (d) seats ride the existing governed `review` task profile.
  (C3) the overseer is a post-verdict ARBITER, never a seat: `collect` computes the
      verdict BEFORE any human reads the seat verdicts; disagreement goes through
      `audit record` (typed, never auto-rescued).
  (C4) vendor diversity is mandatory at N>=2; a forced single-vendor run RECORDS
      `vendorFallback`.
  (C5) fuel-aware degradation reuses SPEC-165 R13 (`gas_balance`/`_gas_pcts`); running
      below the planned N is RECORDED, never silent.
  (C6) `dissent` rides the ledger row even when quorum resolves SHIP.
  invalid seat: re-spawned ONCE; a second contract failure degrades to N-1, RECORDED.

Every subprocess goes through `harness_lib.processes` (a raw `subprocess.run` here
trips the spawn ratchet) and every seat spawn goes through `harness.spawn_command`
-- rule-6 door 1 lives inside it, so an ungoverned seat profile refuses typed
instead of quietly launching.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":  # standalone self-check: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_lib import processes, responsibility, validation_stamp
from harness_lib.common import HarnessError, ROOT, emit, now

POLICY_REL = ".harness/audit-policy.json"
# Decision (d): the governed `review` task profile. Sizing v1 (`audit-seat-sizing`,
# 2026-08-03) landed as the CLASS LADDER in the policy's `seatClasses` block -- the
# profile NAME stays a constant, NOT a policy knob, so the roster cannot drift out of
# the metamodel through a config edit; only the per-seat model/effort does. v2 is the
# RF.1 bakeoff recalibration of those numbers.
SEAT_PROFILE = "review"
# The `generic` spawn entry is the echo stub -- it never audits anything.
SEAT_STUB_EXECUTORS = frozenset({"generic"})
# ponytail: the packet carries a BOUNDED diff excerpt; a seat that needs more runs
# `git diff --cached` itself (the packet says so). Raise if seats start truncating.
MAX_DIFF_CHARS = 20000
# The plan brief rides the packet INLINE (item 9, 2026-08-03): an http seat has no
# repo to open a path with. Same posture as the diff -- bounded, and the cut is named.
MAX_BRIEF_CHARS = 16000
SEVERITIES = ("blocker", "major", "minor", "nit")
VERDICTS = ("ship", "block", "waived")

AUDIT_RUN_COMMAND = "python scripts/harness.py audit run   (then: audit collect)"
AUDIT_RECORD_COMMAND = "python scripts/harness.py audit record --verdict ship"
AUDIT_WAIVE_COMMAND = 'python scripts/harness.py audit waive --reason "…"'


# --- git (mediated) ----------------------------------------------------------

def _git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    # The validation_stamp._git idiom: processes.run_quiet gives the bounded
    # timeout + hidden console + tree-kill; a raw subprocess here trips the ratchet.
    return processes.run_quiet(["git", *args], timeout=timeout, cwd=str(root),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")


# --- policy + band ladder ----------------------------------------------------

def _policy_text(root: Path, ref: str) -> str | None:
    """The policy file's blob AT A GIT REF (`":"` = the index, `"HEAD"` = the last
    commit); None when the path does not exist there (or this is not a repo)."""
    proc = _git(root, "show", (ref if ref.endswith(":") else ref + ":") + POLICY_REL)
    return proc.stdout if proc.returncode == 0 else None


def _enabled_policy(text: str | None) -> dict | None:
    """The one "None unless `enabled`" filter, shared by every read below."""
    try:
        doc = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(doc, dict) or not doc.get("enabled"):
        return None
    return doc


def _policy_blob(root: Path, ref: str) -> dict | None:
    """`.harness/audit-policy.json` as STAGED/COMMITTED at `ref` -- None when absent,
    corrupt or disabled there."""
    return _enabled_policy(_policy_text(root, ref))


def load_audit_policy(root: Path | str) -> dict | None:
    """`.harness/audit-policy.json`; None when absent, corrupt or disabled.

    Reads the INDEX blob whenever the file is TRACKED -- the live disk copy is
    ignored there (seat1-F1: `.harness/` sits outside `surfaceRoots`, so an edit
    that disables the gate moves no fingerprint and leaves no trace; edit, run,
    revert was a zero-trace bypass). Untracked -> disk, which is the adoption
    window before the policy is ever committed.

    None IS the migration window (the `load_policy` contract): the leg degrades to
    warn rather than blocking a repo that never adopted it."""
    root = Path(root)
    text = _policy_text(root, ":")
    if text is None:  # not in the index: adoption window, the disk copy still counts
        try:
            text = (root / POLICY_REL).read_text(encoding="utf-8")
        except OSError:
            return None
    return _enabled_policy(text)


def policy_edited(root: Path) -> bool:
    """Does the INDEX audit policy differ from HEAD's? The ONE predicate `audit_fp`
    and `check_audit` share, so the fingerprint and the gate can never disagree
    about what counts as an edit.

    ADOPTION IS NOT AN EDIT (owner ruling, 2026-08-03): HEAD carrying no policy at
    all means there is nothing to relax -- adding one only ADDS enforcement, and the
    leg's migration posture is warn-never-block. A DELETION (HEAD has one, the index
    does not) is a relaxation like any other and still counts."""
    head_text = _policy_text(root, "HEAD")
    return head_text is not None and _policy_text(root, ":") != head_text


def audit_fp(root: Path, manifest_fp: str) -> str:
    """The fingerprint an AUDIT verdict is keyed on: the staged manifest fp, PLUS the
    index policy blob whenever it differs from HEAD's (seat3-F1, 2026-08-03).

    `.harness/audit-policy.json` sits OUTSIDE `surfaceRoots`, so a policy-only commit
    moves no manifest fingerprint at all: `check_audit` took its "surface unchanged"
    fast-path, the relaxation shipped with zero seats owed and no trace, and commit 2
    was then judged under the relaxed HEAD (seat_plan's dual ladder only defends the
    SAME-commit case). Binding the policy text to the fp makes a policy edit
    un-rescuable by a verdict recorded for the same manifest.

    Policy untouched between INDEX and HEAD -- the overwhelmingly common case, plus
    both-absent and the ADOPTION commit (`policy_edited`) -- returns `manifest_fp`
    UNCHANGED: every fingerprint already on record stays valid."""
    if not policy_edited(root):
        return manifest_fp
    index_text = _policy_text(root, ":") or ""  # deletion: the empty side is the key
    return hashlib.sha256((manifest_fp + "\n" + index_text).encode("utf-8")).hexdigest()


def _num(block: Any, key: str, default: float) -> float:
    value = (block or {}).get(key) if isinstance(block, dict) else None
    return float(value) if isinstance(value, (int, float)) else default


def diff_lines(root: Path, policy: dict) -> int:
    """added+deleted over the STAGED surface roots (`git diff --cached --numstat`).
    Binary rows carry '-' in both cells and contribute nothing. The policy `exclude`
    globs apply here too (seat2-F3): `changed` is exclude-filtered, so counting an
    excluded file's lines made `diffLines` describe a different surface than the
    one being audited."""
    roots = policy.get("surfaceRoots") or validation_stamp.SURFACE_DEFAULT
    proc = _git(root, "diff", "--cached", "--numstat", "--", *roots)
    total = 0
    for raw in (proc.stdout or "").splitlines():
        cells = raw.split("\t")
        if len(cells) < 3:
            continue
        path = cells[2]
        if " => " in path:  # rename row, best-effort: judge the post-rename side
            # Brace form FIRST (`dir/{old => new}/f.py`): the whole-path split below
            # would keep `new}/f.py`, which matches no exclude glob and no root --
            # a moved file's lines were counted for a surface nobody audits.
            path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path).replace("//", "/")
            path = path.split(" => ")[-1].rstrip("}")
        if validation_stamp._excluded(path, policy):
            continue
        for cell in cells[:2]:
            if cell.isdigit():
                total += int(cell)
    return total


def _test_only(changed: list[str], audit_policy: dict) -> bool:
    """C9: a diff confined to the test/doc surface never triggers the REACH ceiling
    (reach 223/223 on a test-only diff is the documented degenerate case)."""
    roots = audit_policy.get("testOnlyRoots") or []
    if not changed or not roots:
        return False
    return all(any(p == r or p.startswith(r + "/") for r in roots) for p in changed)


def _band(audit_policy: dict | None, signals: dict, files: int, lines: int,
          affected: int) -> dict:
    """The pure ladder: trivial floor -> ceiling -> single -> default, under ONE
    policy doc. Split out of `seat_plan` so the same signals can be judged under two
    docs (index vs HEAD) without recomputing the diff."""
    if audit_policy is None:
        return {"seats": 0, "band": "unadopted", "signals": signals,
                "reason": "audit policy absent or disabled"}
    floor = audit_policy.get("trivialFloor") or {}
    if (files < _num(floor, "minChangedFiles", 0)
            and lines < _num(floor, "minDiffLines", 0)
            and affected < _num(floor, "minAffectedScenarios", 0)):
        return {"seats": 0, "band": "trivial", "signals": signals,
                "reason": "below the trivial floor"}
    bands = audit_policy.get("bands") or {}
    ceiling = bands.get("ceiling") or {}
    inf = float("inf")
    if (lines >= _num(ceiling, "minDiffLines", inf)
            or files >= _num(ceiling, "minChangedFiles", inf)
            or (affected >= _num(ceiling, "minAffectedScenarios", inf)
                and not signals["testOnlySurface"])):
        return {"seats": int(_num(ceiling, "seats", 3)), "band": "ceiling", "signals": signals,
                "reason": "size/reach ceiling band"}
    single = bands.get("single") or {}
    if (lines < _num(single, "maxDiffLines", 0) and files < _num(single, "maxChangedFiles", 0)):
        return {"seats": int(_num(single, "seats", 1)), "band": "single", "signals": signals,
                "reason": "small risk-bearing band"}
    return {"seats": int(_num(bands, "defaultSeats", 2)), "band": "default", "signals": signals,
            "reason": "default risk-bearing band"}


def seat_plan(root: Path, policy: dict, changed: list[str], req: dict,
              reach: dict | None = None) -> dict:
    """The deterministic BAND LADDER (owner ruling b) -> how many seats this staged
    surface owes. `{seats, band, reason, signals}`; band 'unadopted' means the audit
    policy is absent/disabled (the caller degrades to warn, never to a block).

    A commit cannot vote on its own pay raise (seat1-F1): when the STAGED policy
    differs from the COMMITTED one, the ladder runs under BOTH docs and the result
    owing MORE seats wins (tie -> the staged doc). Staging a disabled/relaxed policy
    therefore still owes what HEAD's policy asked, and the swap is RECORDED as
    `policyEditedInCommit`. That guarantee is SAME-COMMIT only -- it says nothing
    about a policy relaxed in commit 1 and used in commit 2. The two-commit split is
    closed elsewhere: `audit_fp` binds the policy text to the audit fingerprint and
    `check_audit` makes a policy-only commit owe an explicit verdict."""
    index_doc = load_audit_policy(root)
    head_doc = _policy_blob(root, "HEAD")
    if index_doc is None and head_doc is None:
        return {"seats": 0, "band": "unadopted", "signals": {},
                "reason": "audit policy absent or disabled"}
    if not validation_stamp.reckon_required(policy, req):
        return {"seats": 0, "band": "n/a", "signals": {},
                "reason": "staged surface not risk-bearing"}
    if reach is None:
        reach = validation_stamp.reckon_reach(root, changed)
    affected = int((reach or {}).get("affected") or 0)
    lines = diff_lines(root, policy)
    files = len(changed)
    # Signals are computed ONCE and shared by both evaluations -- only the
    # thresholds differ. `testOnlyRoots` reads from HEAD's doc when the two differ:
    # a staged root that newly declares the surface test-only would otherwise
    # suppress the reach ceiling under both docs.
    signals = {"changedFiles": files, "diffLines": lines, "affectedScenarios": affected,
               "testOnlySurface": _test_only(changed, head_doc or index_doc or {})}
    plan = _band(index_doc, signals, files, lines, affected)
    if head_doc is not None and head_doc != index_doc:
        alt = _band(head_doc, signals, files, lines, affected)
        signals["policyEditedInCommit"] = True  # shared dict: rides whichever wins
        if int(alt["seats"]) > int(plan["seats"]):
            plan = alt
    return plan


# --- iteration ledger (consecutive BLOCK cycles) -----------------------------

DEFAULT_MAX_BLOCK_CYCLES = 3


def max_block_cycles(root: Path | str) -> int:
    """K: consecutive BLOCK cycles before the iteration escalation is MANDATORY.
    Read from the INDEX policy blob like every other policy read -- a disk-only
    relaxation is invisible to the fingerprint, so it must be invisible here too."""
    return int(_num(load_audit_policy(root) or {}, "maxBlockCycles", DEFAULT_MAX_BLOCK_CYCLES))


def block_streak(root: Path | str) -> dict:
    """`{count, firstFp, lastFp}` over the TRAILING run of `block` rows in the
    durable ledger `.harness/runs/audit-results.jsonl` -- a ship/waived row RESETS
    it, so the count answers "how many times has THIS iteration been blocked".

    `firstFp` is the streak HEAD, and the head is the only stable identity an
    iteration has: every fix moves the current fingerprint, the head does not move
    until a ship/waive closes the streak (see `iteration_escalation_id`).

    Missing file, unreadable file and JSON garbage all read as "no streak" -- the
    `check_audit` fail-open posture: a broken ledger must never wedge the leg."""
    try:
        lines = validation_stamp._audit_results(Path(root)).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        lines = []
    streak: list[str] = []
    for raw in lines:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("verdict") == "block":
            streak.append(str(row.get("stagedFingerprint") or ""))
        elif row.get("verdict") in VERDICTS:  # a ship/waived row closes the iteration
            streak = []
    return {"count": len(streak), "firstFp": streak[0] if streak else None,
            "lastFp": streak[-1] if streak else None}


def iteration_escalation_id(streak: dict) -> str:
    """The iteration's escalation id, keyed on the streak HEAD so every cycle of the
    SAME iteration raises ONE id and the durable fold stays idempotent."""
    return "audit-iteration:" + str((streak or {}).get("firstFp") or "")[:12]


def iteration_registered(root: Path | str, esc_id: str) -> bool:
    """Is this iteration's escalation in the DURABLE ledger -- pending OR resolved?
    Registered is enough (owner decision): the mandate is visibility in the owner
    funnel, not a work stoppage. Reads `list_escalations`, never the raw events log
    (`.harness/runs/events.jsonl` is transient; gates wipe it)."""
    from harness_lib import escalations_lib
    known = escalations_lib.list_escalations(root=Path(root))
    return esc_id in {str(e.get("escalationId")) for e in known.get("pending") or []} \
        or esc_id in set(known.get("resolvedIds") or [])


def _refuse_unregistered_iteration(root: Path) -> None:
    """Cycle K+1 refuses to run while the iteration owes an OWNER decision.

    Defense in depth, not the primary path: the collect at cycle K raises the
    escalation. This is what catches a SCRUBBED events log (a gate wipe between
    collect and the next run) -- and it refuses only when the id is registered
    NOWHERE; pending is registered, and a pending escalation still runs (the
    commit stays blocked by the BLOCK verdict itself, which is the real stop)."""
    streak = block_streak(root)
    count, k = int(streak["count"]), max_block_cycles(root)
    if count < k:
        return
    esc_id = iteration_escalation_id(streak)
    if iteration_registered(root, esc_id):
        return
    raise HarnessError(
        f"audit run: {count} consecutive BLOCK cycles (max {k}) and escalation {esc_id} "
        f"is not registered; the iteration owes an owner decision -- fix: "
        f"python scripts/harness.py escalations (the collect at cycle {k} raises it; "
        f"if events were scrubbed, re-run audit collect)")


def _iteration_escalation(root: Path, ctx: dict) -> dict:
    """Raise the iteration escalation once the stamped BLOCK makes the streak reach
    K. Idempotency lives in the compaction fold (a RESOLVED id is never re-raised),
    so appending twice is harmless by design.

    NEVER blocks the leg (the `_gas_pcts` posture): the quorum's verdict is already
    stamped, and escalation plumbing failing is a note, not a lost verdict."""
    try:
        streak = block_streak(root)
        count = int(streak["count"])
        out: dict = {"cycles": count}
        if count >= max_block_cycles(root):
            esc_id = iteration_escalation_id(streak)
            import harness

            harness.append_event("audit_iteration_escalation", {
                "escalationId": esc_id,
                "reason": f"{count} consecutive audit BLOCK cycles (streak head "
                          f"{str(streak['firstFp'])[:12]}, current {ctx['fp'][:12]}) without "
                          f"a SHIP; owner decides: accept risk (audit waive), revert, or redesign",
                "cycles": count, "streakHeadFp": streak["firstFp"],
            })
            out["iterationEscalation"] = esc_id
        return out
    except Exception as exc:  # noqa: BLE001 -- the verdict stands whatever this does
        return {"iterationNote": f"iteration escalation not raised ({type(exc).__name__})"}


def _mint_defects(root: Path, base: Path, manifest: dict, results: list[dict]) -> dict:
    """A BLOCK is a defect the audit CAUGHT: one capsule per blocker/major finding
    on a blocking seat, attributed to the seat that PRODUCED the change.

    Attribution comes ONLY from the run's `--lane-seat` (`role:model:effort`): no
    guessing and no placeholder seat -- `record_defect` refuses an incomplete seat
    on purpose, and a wrong attribution is worse than none. Absent or malformed ->
    mint nothing and SAY so. Same never-block posture as `_iteration_escalation`."""
    try:
        from harness_lib import cost_metrics
        seat = cost_metrics.seat_from_text(manifest.get("laneSeat"))
        if seat is None:
            return {"defectNote": "defects not minted: no --lane-seat named on audit run"}
        minted = 0
        for row in results:
            if row.get("verdict") != "block":
                continue
            for finding in row.get("findings") or []:
                if finding.get("severity") not in ("blocker", "major"):
                    continue
                minted += bool(cost_metrics.record_defect(
                    root, defect_class="audit-caught", evidence_kind="audit",
                    evidence_ref=f"{_rel(root, base)}/seat-{row['seat']} {finding.get('id')} "
                                 f"{finding.get('file')}:{finding.get('line')}",
                    produced_by=seat, missed_by=None))
        return {"defectsMinted": minted}
    except Exception as exc:  # noqa: BLE001 -- the verdict stands whatever this does
        return {"defectNote": f"defects not minted ({type(exc).__name__})"}


# --- packet ------------------------------------------------------------------

def _plan_brief_line(root: Path, plan_brief: str | None) -> str:
    """The intent pointer -- with the brief INLINED under it (item 9, 2026-08-03).

    A path is unreadable to an http seat (`openai_worker` has no filesystem), and
    such a seat twice block-voted on design decisions the brief had already made.
    Every seat gets the text, not just the pointer: a CLI seat saves the read too.
    A 4-backtick fence because briefs carry ``` blocks of their own."""
    if not plan_brief:
        return ("No plan brief was named for this surface. Judge the change against the "
                "repo's own contracts (AGENTS.md, the specs it touches).")
    line = (f"Plan brief (authoritative intent -- any deviation is a finding): "
            f"`{plan_brief}`. Read it before the diff.")
    try:
        text = (Path(root) / plan_brief).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return line + " (file not found -- judge against repo contracts)"
    if len(text) > MAX_BRIEF_CHARS:
        text = (text[:MAX_BRIEF_CHARS]
                + f"\n... {plan_brief} truncated at {MAX_BRIEF_CHARS} chars\n")
    return line + "\n\n````markdown\n" + text + "\n````"


def build_packet(root: Path, ctx: dict) -> str:
    """The adversarial audit packet, cloning `.harness/handoff/audit-rule6-doors.md`:
    framing, what shipped, attack surfaces, rerunnable read-only verification, and
    the output contract. Everything in it is derived from the STAGED index -- the
    seat audits what is about to be committed, not the working tree's mood."""
    changed: list[str] = ctx["changed"]
    reach = ctx.get("reach") or {}
    sample = ", ".join((reach.get("sample") or [])[:8])
    signals = (ctx.get("plan") or {}).get("signals") or {}
    shown = changed[:40]
    out = [
        f"# Audit packet — staged surface {ctx['fp'][:12]}",
        "",
        "You are an ADVERSARIAL pre-merge auditor. Your job is to find reasons to BLOCK.",
        "A clean SHIP must be earned by failing to break the change, not by summarizing it.",
        "Read-only: do NOT write, stage, commit, or touch `.harness/` state — the ONE file",
        "you may write is your own verdict slot, named in the output contract below.",
        "",
        "## What shipped (the STAGED index, not the worktree)",
        "",
        _plan_brief_line(root, ctx.get("planBrief")),
        "",
        f"- staged fingerprint: `{ctx['fp']}`",
        f"- risk profile(s): {', '.join((ctx.get('req') or {}).get('names') or []) or 'none'}",
        f"- required gates: {', '.join((ctx.get('req') or {}).get('gates') or []) or 'none'}",
        f"- size signals: {json.dumps(signals, ensure_ascii=False)}",
        f"- seat band: {(ctx.get('plan') or {}).get('band')} "
        f"({(ctx.get('plan') or {}).get('reason')})",
    ]
    if reach:
        out.append(f"- affected reach (gate_affected map): {reach.get('affected')}/{reach.get('total')} "
                   f"scenarios{' — e.g. ' + sample if sample else ''}")
    out += ["", f"Changed surface paths ({len(changed)}):"]
    out += [f"  - {p}" for p in shown]
    if len(changed) > len(shown):
        out.append(f"  ... and {len(changed) - len(shown)} more (`git diff --cached --name-only`)")
    cut = ctx.get("diffCut") or []
    out += ["", "## Staged diff" + (" (per-file excerpts)" if cut else "")]
    if cut:
        out.append("Truncated file(s): " + ", ".join(cut)
                   + ". Each cut carries an explicit `[EXCERPT TRUNCATED …]` marker "
                   "(normally on a line boundary; a single over-long line is hard-cut) "
                   "— NEVER judge text at or near a marker as source (a clipped token is "
                   "truncation, not a defect). Run `git diff --cached` for the unabridged diff.")
    out += [
        "",
        "```diff",
        ctx["diff"],
        "```",
    ]
    harness_diff = ctx.get("harnessDiff") or ""
    if harness_diff:
        out += [
            "",
            "## Staged `.harness/` config (CONTEXT — read it, do not re-audit it)",
            "",
            f"These {len(ctx.get('harnessPaths') or [])} governance-config file(s) are staged "
            "in THIS commit but sit OUTSIDE the audited surface roots, so they are absent from "
            "the changed list and the diff above. Shown so you do not block on \"X was never "
            "updated\" when X is staged right here (that exact false BLOCK is why this section "
            "exists). Judge the code change against them; do NOT write, stage or re-audit them.",
            "",
            "```diff",
            harness_diff,
            "```",
        ]
    out += [
        "",
        "## Attack surfaces (verify each; do not take the claims on faith)",
        "",
        "1. **Correctness under the real callers.** For every function touched, find its",
        "   callers and judge whether the change holds for ALL of them, not just the path",
        "   the diff was written for.",
        "2. **Guard placement.** Anything observable (writes, events, processes, spawns)",
        "   happening before a validation/refusal? Any early return above a guard?",
        "3. **Failure modes.** What happens on a missing file, a corrupt JSON, a hard-kill",
        "   mid-write, a concurrent run? Fail-open where it must fail-closed, or the reverse?",
        "4. **Evidence vs claim.** Does the change's own test/scenario actually FAIL if the",
        "   logic is broken, or does it pass vacuously? Delete one assertion mentally and see.",
        "5. **Footprint honesty.** Anything outside the plan's declared footprint? Deleted",
        "   assertions, blanked bodies, weakened checks that make a gate green cheaply?",
        "6. **Anything else:** error-message quality, Windows paths, encoding, performance",
        "   on the hot path, thread/process safety.",
        "",
        "## Verification you may rerun (read-only, venv python)",
        "",
        "```",
        "HARNESS_QUIET=1 HARNESS_AGENT_OUTPUT=compact .venv/Scripts/python.exe scripts/harness.py verify-status --json",
        "git diff --cached           # the full staged diff, unabridged",
    ]
    for scenario in (reach.get("sample") or [])[:6]:
        out.append(f"HARNESS_QUIET=1 HARNESS_AGENT_OUTPUT=compact .venv/Scripts/python.exe {scenario}")
    out += [
        "```",
        "",
        "## Output contract",
        "",
        f"Write your verdict to `{ctx['verdictRel']}` — that file and NOTHING else.",
        "First line, verbatim: `VERDICT: SHIP` or `VERDICT: BLOCK`.",
        "API seat that cannot write files: your WORKER_RESULT `summary` field MUST begin "
        "with `VERDICT: SHIP` or `VERDICT: BLOCK`, followed by your findings lines.",
        "Then findings `F1..Fn`, one per line-start, each:",
        "`F<n> — severity — file:line — evidence` with severity in "
        f"{'|'.join(SEVERITIES)}.",
        "A BLOCK with no findings is REFUSED at collect (a block must point somewhere).",
        "No finding at all? Say so explicitly, then end with a short rationale paragraph.",
        "If a tool call is denied, fall back to Read/Grep/Glob — do not stall.",
    ]
    return "\n".join(out) + "\n"


# --- seats -------------------------------------------------------------------

def _gas_pcts(root: Path) -> dict[str, float]:
    """SPEC-165 R13 fuel readings; fail-open to {} (no fuel state is a valid state)."""
    try:
        from harness_lib import model_routing
        return model_routing._gas_pcts(Path(root))
    except Exception:  # noqa: BLE001 — a fuel probe must never block an audit
        return {}


def _fuel_vendors(root: Path) -> dict | None:
    """The annotated per-vendor fuel rows (`vendor_fuel.show`); None when the state
    is unreadable -- a fuel probe must never block an audit."""
    try:
        from harness_lib import vendor_fuel
        vendors = (vendor_fuel.show(Path(root)) or {}).get("vendors") or {}
    except Exception:  # noqa: BLE001 -- a fuel probe must never block an audit
        return None
    return vendors if isinstance(vendors, dict) else {}


def _api_reachable(row: Any) -> bool:
    """An http vendor whose probe came back FRESH: reachable even though it exposes
    no quota number. `baseUrl` is the discriminator every http row carries
    (vendor_fuel.probe_http) -- a CLI vendor never has one."""
    row = row if isinstance(row, dict) else {}
    return bool((row.get("value") or {}).get("baseUrl")) and row.get("effectiveStatus") == "fresh"


def _unmeasured_note(root: Path, executors: list[str]) -> str:
    """WHY a chosen executor has no fuel reading (seat1-F3): a CLI vendor with no
    fresh reading and an api provider that publishes no quota both land absent from
    `_gas_pcts` and sort identically, so the record could not tell them apart."""
    vendors = _fuel_vendors(root)
    parts: list[str] = []
    for name in executors:
        row = vendors.get(name) if isinstance(vendors, dict) else None
        row = row if isinstance(row, dict) else {}
        if vendors is None:
            why = "fuel state unreadable"
        elif _api_reachable(row):
            why = "api reachable, quota not exposed"
        elif (row.get("value") or {}).get("baseUrl"):
            # An http vendor that HAS a baseUrl but did not probe fresh is not a
            # dark CLI vendor -- saying so sent the reader hunting for a CLI login.
            why = "api probe stale or unreachable"
        else:
            why = "no fresh CLI reading"
        parts.append(f"{name} ({why})")
    return "unmeasured executor(s): " + ", ".join(parts)


def _unreachable(name: str, pcts: dict[str, float], vendors: dict | None) -> str | None:
    """WHY this executor cannot be seated, else None (eligible).

    Owner ruling 2026-08-03: a vendor with no gas/credit is UNREACHABLE -- skipped at
    pick time and respawned elsewhere at collect, NEVER launch-and-let-die (the
    codex seat that burned a band-ceiling quorum from 3 to 1 on zero credit). A fresh
    0% is an empty tank; a CLI vendor with no fresh reading is dark; an http vendor
    that probed reachable stays eligible even with no quota number -- its
    reachability IS the signal."""
    if pcts.get(name) == 0.0:
        return "empty tank"
    if name in pcts or _api_reachable((vendors or {}).get(name)):
        return None
    return "no fresh CLI reading"


# The full-fleet fuel probe, as a SEAM (item 6, 2026-08-03): None = never re-probe,
# which is what every direct `seat_executors` caller and unit test sees. `cmd_audit`
# wires the real `vendor_fuel.probe_all` for the CLI paths that actually seat.
_reprobe = None


def seat_executors(root: Path, seats: int, executor: str | None = None,
                   exclude: set[str] | None = None) -> tuple[list[str], str | None]:
    """`(executor per seat, vendorFallback reason | None)`.

    C4: seats must span >=2 executors at N>=2 (the cross-vendor rule-7 idiom of
    `research_round._precheck`). C5: ordering reuses R13 `gas_balance` and an
    UNREACHABLE vendor never gets a seat -- both facts are RECORDED in the returned
    reason when they force a single-vendor or degraded run, never silently applied.
    `exclude` is the respawn-elsewhere ask (a seat that died on X retries on
    something else) and yields only when nothing else is reachable; an operator
    `--executor` pin outranks it -- a pinned run respawns on the pin."""
    from harness_lib import model_routing
    import harness

    diversity_min = int(_num(load_audit_policy(root) or {}, "vendorDiversityMinSeats", 2))
    if executor:
        return ([executor] * seats,
                f"operator pinned --executor {executor}" if seats >= diversity_min else None)
    profile = (harness.load_profiles().get("profiles") or {}).get(SEAT_PROFILE) or {}
    candidates = [e for e in (profile.get("spawn") or {}) if e not in SEAT_STUB_EXECUTORS]
    if not candidates:
        return ([harness.active_executor(None)] * seats,
                f"no non-stub executor declared on the {SEAT_PROFILE} profile")

    def ladder(pcts: dict[str, float], vendors: dict | None) -> tuple[list, dict, list]:
        ordered = [hop["executor"] for hop in
                   model_routing.gas_balance([{"executor": e} for e in candidates], pcts)]
        skipped = {e: why for e in ordered if (why := _unreachable(e, pcts, vendors))}
        return ordered, skipped, [e for e in ordered if e not in skipped]

    pcts = _gas_pcts(root)
    vendors = _fuel_vendors(root)
    ordered, skipped, live = ladder(pcts, vendors)
    reprobed = False
    if not live and vendors is not None and _reprobe is not None:
        # 2026-08-03: every reading aged past the freshness window (~3h) mid-round,
        # so the whole fleet read dark and the unfiltered fallback below seated an
        # owner-BANNED vendor. Stale data is not an empty fleet -- refresh once and
        # ask again. A probe that fails degrades to exactly the old behavior.
        try:
            _reprobe(root)
            reprobed = True
        except Exception:  # noqa: BLE001 -- a fuel probe must never block an audit
            pass
        if reprobed:
            pcts = _gas_pcts(root)
            vendors = _fuel_vendors(root)
            ordered, skipped, live = ladder(pcts, vendors)
    notes: list[str] = []
    if skipped:
        notes.append("skipped unreachable executor(s): "
                     + ", ".join(f"{e} ({skipped[e]})" for e in ordered if e in skipped))
    if not live:
        # Never refuse to audit for lack of fuel DATA: an unreadable/empty fuel state
        # must not read as "every vendor is dead". Unfiltered, and RECORDED.
        live = ordered
        notes.append("no reachable vendor by fuel state"
                     + (" after a fresh probe" if reprobed else "")
                     + "; proceeding unfiltered")
    if exclude:
        live = [e for e in live if e not in exclude] or live
    chosen = [live[i % len(live)] for i in range(seats)]
    if seats >= diversity_min and len(set(chosen)) < 2:
        notes.insert(0, f"only {len(set(live))} executor(s) available for profile {SEAT_PROFILE}")
    unmeasured = [e for e in dict.fromkeys(chosen) if e not in pcts]
    if unmeasured:
        notes.append(_unmeasured_note(root, unmeasured))
    return chosen, ("; ".join(notes) or None)


def seat_class_plan(policy: dict | None, band: str, count: int) -> list[str | None]:
    """Class name per seat (`seatClasses.bands[band]`): the band's vector, padded with
    its LAST entry and truncated to `count` -- `--seats N` overrides the planned count,
    so the vector has to stretch and shrink. No `seatClasses` block, no vector for this
    band -> `[None] * count`, i.e. every seat on the `review` profile's own pins."""
    vector = (((policy or {}).get("seatClasses") or {}).get("bands") or {}).get(band)
    if not isinstance(vector, list) or not vector:
        return [None] * count
    return [str(vector[i if i < len(vector) else -1]) for i in range(count)]


def class_override(policy: dict | None, cls: str | None, executor: str | None) -> tuple[dict, str | None]:
    """`(spawn override, note | None)` for ONE seat: the class's entry for this
    executor, else `({}, note)`.

    An unknown class name and a class whose table names OTHER executors but not
    this one (kimi and gemini-compat have none in v1) land on the same typed path:
    profile defaults plus a note on the seat row (owner rule c: degradation is
    never silent). A known class with an EMPTY table (`xhigh: {}`) is the
    REFERENCE class -- profile defaults are its intent, not a degradation, so it
    carries no note (otherwise every default-band seat ships a pseudo-warning).
    Never a crash: the policy is config, and config must not brick the leg."""
    if not cls:
        return {}, None
    table = (((policy or {}).get("seatClasses") or {}).get("classes") or {}).get(cls)
    if isinstance(table, dict) and not table:
        return {}, None  # reference class: defaults by design
    entry = table.get(executor) if isinstance(table, dict) else None
    if not isinstance(entry, dict) or not entry:
        return {}, f"class {cls}: no override for {executor}; profile defaults"
    return dict(entry), None


def seat_argv(task: str, executor: str | None, prompt_file: Path,
              spawn_override: dict | None = None) -> tuple[list[str], str, dict[str, str]]:
    """The ONE spawn seam (the `route_dispatcher.dispatch_command` idiom): a lazy
    `import harness` so rule-6 door 1 (`model_routing.enforce_spawn`, inside
    `spawn_command`) refuses an ungoverned seat profile HERE, typed. No new spawn
    path, no door bypass -- `spawn_command` also writes the composed prompt to
    `prompt_file` and points the argv at it (Windows cmd.exe cannot carry a
    multi-line prompt argv)."""
    import harness

    argv, _profile, resolved_executor, env = harness.spawn_command(
        task, executor, SEAT_PROFILE, prompt_file, spawn_override=spawn_override)
    return argv, resolved_executor, env


def _launch_seat(root: Path, seat_dir: Path, task: str, executor: str | None,
                 spawn_override: dict | None = None) -> dict:
    seat_dir.mkdir(parents=True, exist_ok=True)
    (seat_dir / "task.md").write_text(task, encoding="utf-8")
    argv, resolved, spawn_env = seat_argv(task, executor, seat_dir / "prompt.md", spawn_override)
    import harness

    executor_doc = (harness.load_executors().get("executors") or {}).get(resolved) or {}
    harness_vars = dict(spawn_env or {})
    if executor_doc.get("type") == "http":
        harness_vars["HARNESS_WORKER_PROMPT_PATH"] = str((seat_dir / "task.md").resolve())
        harness_vars["HARNESS_WORKER_RESULT_PATH"] = str((seat_dir / "result.json").resolve())
    env = processes.filter_spawn_env(os.environ, harness_vars,
                                     keep_list=executor_doc.get("envKeepList", ()))
    log = seat_dir / "seat.log"
    pid = processes.launch_detached(argv, log_path=log, cwd=str(root), env=env)

    # ER-2: Mint initial SeatLaunchReceipt (status: launched)
    task_digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
    now_ts = now()
    seat_execution_id = f"SEXEC-{now_ts.replace(':', '').replace('-', '').replace('.', '')[:15]}-{task_digest[:8]}"
    receipt = {
        "seatExecutionId": seat_execution_id,
        "auditRunId": seat_dir.parent.name,
        "seatId": seat_dir.name,
        "executor": resolved,
        "packetDigest": task_digest,
        "startedAt": now_ts,
        "pid": pid,
        "processIdentity": {"pid": pid, "nonce": hashlib.sha256(os.urandom(16)).hexdigest()[:12]},
        "logPath": _rel(root, log),
        "status": "launched",
        "controllerSupervised": True,
        "controllerIdentity": "harness-audit-controller"

    }
    (seat_dir / "seat-execution.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    return {"executor": resolved, "pid": pid, "log": _rel(root, log), "seatExecutionId": seat_execution_id}


def settle_seat_execution(root: Path, seat_dir: Path, pid: int | None = None) -> None:
    """ER-2 & ER-3: AuditController settles seat process completion and captures output directly at process boundary."""
    rc_file = seat_dir / "seat-execution.json"
    if not rc_file.is_file():
        return
    try:
        doc = json.loads(rc_file.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("status") == "completed":
            return
        # ER-2 & ER-6: A seat in 'launched' state can ONLY be settled if the process was actually tracked by the controller
        proc_pid = pid or doc.get("pid")
        if proc_pid is None:
            return  # missing process identity
        if processes.pid_alive(proc_pid):
            return  # still running
        if not doc.get("controllerSupervised"):
            return  # unverified process origin; manual log edit post-launch


        # Controller captures raw output bytes from log or result.json
        from harness_lib import governance_receipt
        raw_bytes = b""
        result_file = seat_dir / "result.json"
        log_file = seat_dir / "seat.log"

        if result_file.is_file():
            raw_bytes = result_file.read_bytes()
            text = _result_text(result_file)
        elif log_file.is_file():
            raw_bytes = log_file.read_bytes()
            text = _claude_result_text(log_file) or raw_bytes.decode(errors="replace")
        else:
            text = ""

        if not text.strip():
            return

        parsed = parse_verdict(text)
        if parsed.get("verdict") is None:
            return  # malformed verdict, leave for collect respawn

        stdout_digest = governance_receipt.compute_digest(raw_bytes)
        doc.update({
            "status": "completed",
            "endedAt": now(),
            "exitCode": 0,
            "terminationReason": "exited_cleanly",
            "outputDigests": {
                "stdoutDigest": stdout_digest,
                "rawResultDigest": stdout_digest
            },
            "parsedVerdict": parsed,
            "controllerIdentity": "harness-audit-controller"
        })
        rc_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        # Materialize derived VERDICT.md artifact
        verdict_file = seat_dir / "VERDICT.md"
        if not verdict_file.is_file():
            verdict_file.write_text(
                f"<!-- harness-derived from SeatExecutionReceipt {doc['seatExecutionId']} (controller-captured output) -->\n" + text,
                encoding="utf-8"
            )
    except Exception:
        pass


def verify_seat_receipt(root: Path, seat_dir: Path) -> tuple[bool, str]:
    """ER-2, ER-4 & ER-5: Validate process-captured SeatCompletionReceipt.
    A raw VERDICT.md or seat.log alone is NEVER authoritative.
    status: launched is NOT execution proof.
    Modifying seat.log manually invalidates stdoutDigest and is rejected as TAMPERED_DERIVED_ARTIFACT.
    """
    rc_file = seat_dir / "seat-execution.json"
    if not rc_file.is_file():
        return False, "missing process execution receipt (seat-execution.json missing; raw VERDICT.md without execution receipt is unverified)"
    try:
        doc = json.loads(rc_file.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return False, "corrupt seat-execution.json"

        controller_id = doc.get("controllerIdentity") or doc.get("authoredBy")
        if controller_id != "harness-audit-controller":
            return False, f"unauthorized controllerIdentity {controller_id!r} (must be harness-audit-controller)"

        if doc.get("status") != "completed":
            return False, f"seat execution receipt status is {doc.get('status')!r}, not 'completed' (SEAT_LAUNCH_IS_NOT_EXECUTION_PROOF)"

        if doc.get("exitCode") != 0:
            return False, f"seat execution process exitCode is {doc.get('exitCode')}, not 0"

        # ER-5: Digest integrity verification against derived seat.log if present
        log_file = seat_dir / "seat.log"
        if log_file.is_file():
            from harness_lib import governance_receipt
            captured_digest = (doc.get("outputDigests") or {}).get("stdoutDigest") or doc.get("stdoutDigest")
            if captured_digest:
                actual_digest = governance_receipt.compute_digest(log_file.read_bytes())
                if actual_digest != captured_digest:
                    return False, f"tampered derived artifact: seat.log hash ({actual_digest[:16]}) does not match controller-captured stdoutDigest ({captured_digest[:16]})"

        return True, "valid completed execution receipt"
    except Exception as ex:
        return False, f"receipt parse error: {ex}"


def _seat_text(root: Path, row: dict) -> str:
    """The seat's verdict slot, requiring a valid process-level SeatCompletionReceipt (ER-2/ER-4)."""
    verdict_file = Path(root) / row["verdictFile"]
    seat_dir = verdict_file.parent

    # Try settling seat process first
    settle_seat_execution(root, seat_dir, row.get("pid"))

    is_valid_rc, rc_msg = verify_seat_receipt(root, seat_dir)
    if not is_valid_rc:
        return ""  # Ignore raw files if execution receipt is missing, incomplete, or untrusted!

    if verdict_file.is_file():
        return verdict_file.read_text(encoding="utf-8", errors="replace")
    result_text = _result_text(seat_dir / "result.json")
    if result_text:
        return result_text
    log = row.get("log")
    if log and (Path(root) / log).is_file():
        log_path = Path(root) / log
        return _claude_result_text(log_path) or \
            log_path.read_text(encoding="utf-8", errors="replace")
    return ""



def _rel(root: Path, path: Path) -> str:

    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# --- verdict parsing + quorum ------------------------------------------------

_VERDICT_RE = re.compile(r"^[\s*#>]*VERDICT[\s*]*:[\s*]*(SHIP|BLOCK)\b", re.IGNORECASE)
_FINDING_RE = re.compile(
    r"^[\s*#>\-]*F(\d+)\b[\s*`]*[—–\-:]+[\s*`]*(" + "|".join(SEVERITIES) + r")\b(.*)$",
    re.IGNORECASE)
_FILE_LINE_RE = re.compile(r"([\w./\\+-]+\.[A-Za-z0-9_]+):(\d+)")


def parse_verdict(text: str) -> dict:
    """ASC-4 & ASC-5: Parse CanonicalAuditSeatResult JSON envelope or legacy VERDICT line into canonical format."""
    raw_str = (text or "").strip()
    if not raw_str:
        return {"verdict": None, "findings": [], "error": "empty output"}

    # ASC-4: Try parsing structured JSON envelope (CanonicalAuditSeatResult)
    json_candidate = raw_str
    if "```json" in raw_str:
        try:
            start = raw_str.find("```json") + 7
            end = raw_str.find("```", start)
            if end > start:
                json_candidate = raw_str[start:end].strip()
        except Exception:
            pass
    elif "{" in raw_str and "}" in raw_str:
        try:
            start = raw_str.find("{")
            end = raw_str.rfind("}") + 1
            if end > start:
                json_candidate = raw_str[start:end].strip()
        except Exception:
            pass

    try:
        obj = json.loads(json_candidate)
        if isinstance(obj, dict) and ("verdict" in obj or "verdict" in (obj.get("result") if isinstance(obj.get("result"), dict) else {})):
            v_val = str(obj.get("verdict") or (obj.get("result") or {}).get("verdict") or "").strip().lower()
            if v_val in ("ship", "block"):
                raw_findings = obj.get("findings") or (obj.get("result") or {}).get("findings") or []
                findings = []
                for idx, f in enumerate(raw_findings, start=1):
                    if isinstance(f, dict):
                        findings.append({
                            "id": str(f.get("id") or f"F{idx}"),
                            "severity": str(f.get("severity") or "major").lower(),
                            "file": f.get("file"),
                            "line": f.get("line"),
                            "evidence": str(f.get("evidence") or "")[:400]
                        })
                if v_val == "block" and not findings:
                    return {"verdict": None, "findings": [], "error": "findings-less BLOCK refused in structured JSON"}
                return {"verdict": v_val, "findings": findings, "error": None, "schemaVersion": "1.0"}
    except Exception:
        pass

    # Fallback: Parse line-based VERDICT: SHIP|BLOCK
    lines = raw_str.splitlines()
    verdicts = [m.group(1).lower() for ln in lines if (m := _VERDICT_RE.match(ln))]
    if not verdicts:
        return {"verdict": None, "findings": [],
                "error": "no `VERDICT: SHIP|BLOCK` line or CanonicalAuditSeatResult JSON found"}
    if len(set(verdicts)) > 1:
        return {"verdict": None, "findings": [],
                "error": f"conflicting VERDICT lines {verdicts}; a seat must state ONE verdict"}
    verdict = verdicts[0]
    findings: list[dict] = []
    for raw in lines:
        hit = _FINDING_RE.match(raw)
        if not hit:
            continue
        rest = hit.group(3) or ""
        where = _FILE_LINE_RE.search(rest)
        findings.append({"id": f"F{hit.group(1)}", "severity": hit.group(2).lower(),
                         "file": where.group(1) if where else None,
                         "line": int(where.group(2)) if where else None,
                         "evidence": rest.strip(" *`—–-:")[:400]})
    if verdict == "block" and not findings:
        return {"verdict": None, "findings": [],
                "error": "findings-less BLOCK refused (a block must point somewhere)"}
    return {"verdict": verdict, "findings": findings, "error": None, "schemaVersion": "1.0"}



def quorum(seats: list[dict]) -> dict:
    """Owner ruling (b): unanimity to SHIP; ANY block blocks; a blocker-severity
    finding blocks regardless of majority; the split names the dissenting seat.
    `dissent` is always present (C6) -- empty on unanimity."""
    valid = [s for s in seats if s.get("verdict") in ("ship", "block")]
    if not valid:
        return {"verdict": None, "dissent": [], "blockerSeats": [], "findingsCount": 0,
                "reason": "no seat produced a contract-conforming verdict"}
    blocks = [s for s in valid if s["verdict"] == "block"]
    blocker_seats = sorted({f"seat-{s['seat']}" for s in blocks
                            for f in s.get("findings") or [] if f.get("severity") == "blocker"})
    verdict = "block" if blocks else "ship"
    dissent = [f"seat-{s['seat']}:{s['verdict']}" for s in valid if s["verdict"] != verdict]
    count = sum(len(s.get("findings") or []) for s in valid)
    if verdict == "block":
        who = ", ".join(f"seat-{s['seat']}" for s in blocks)
        reason = (f"{len(blocks)}/{len(valid)} seat(s) BLOCK ({who})"
                  + (f"; blocker-severity findings from {', '.join(blocker_seats)}" if blocker_seats else "")
                  + (f"; dissent: {', '.join(dissent)}" if dissent else ""))
    else:
        reason = f"unanimous SHIP across {len(valid)} seat(s), {count} finding(s) recorded"
    return {"verdict": verdict, "dissent": dissent, "blockerSeats": blocker_seats,
            "findingsCount": count, "reason": reason}


# --- run / collect / record / waive ------------------------------------------

def _audit_dir(root: Path, fp: str) -> Path:
    return Path(root) / ".harness" / "runs" / f"audit-{fp[:12]}"


def _persist_run(base: Path, manifest: dict) -> None:
    """The ONE run.json writer -- launch loop and respawn share it. Same-dir tmp +
    `os.replace` (the `_write_stash_manifest` idiom): this file is rewritten 1+N+1
    times per run, and a hard-kill caught mid-`write_text` leaves a TRUNCATED
    manifest that `collect` reads as "no audit run on record" -- every detached seat
    it was holding becomes an orphan. The swap is atomic; readers see old or new.

    Decision 4 (audit-seat-sizing): the distinct seat `classNote`s are rolled up to
    `classNotes` HERE, in the ONE writer, so the launch loop and the respawn cannot
    drift apart about what the forensic record says a seat actually ran on."""
    notes = list(dict.fromkeys(row["classNote"] for row in manifest.get("seatRows") or []
                               if row.get("classNote")))
    if notes:
        manifest["classNotes"] = notes
    else:
        # ...and CLEARS when the last noted seat stops noting (audit F3): a respawn
        # that lands on a named vendor pops the row note, and a stale top-level
        # rollup surviving that made run.json claim a degradation nobody ran on.
        manifest.pop("classNotes", None)
    tmp = base / "run.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, base / "run.json")


def _existing_run(base: Path) -> dict | None:
    """The parsed run.json already on record for this fp, or None when it is absent,
    truncated, or not a JSON object. A hard-kill mid-write leaves a corrupt manifest
    (see `_persist_run`); a valid-but-non-dict body (`[]`, `123` — manual tamper)
    would crash the `prior.get(...)` reads downstream. Both read as absent so a fresh
    run proceeds instead of dead-ending on a reprint or an AttributeError (the
    `_result_text` isinstance idiom)."""
    try:
        doc = json.loads((base / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _rerun_is_noop(prior: dict | None) -> bool:
    """audit-run-not-idempotent: a REAL run already on record for this fp turns a
    bare `audit run` into a no-op — re-launching would spawn a second seat trio into
    the same dirs and orphan the first. A dry-run PREVIEW never blocks the real run
    that follows it. Recovering a live run is `collect`'s job (liveness+respawn),
    never a second `run`; the rare genuine re-run deletes run.json by hand (no
    --force flag: it only reintroduced the orphan race it was meant to guard)."""
    return prior is not None and not prior.get("dryRun")


def _staged_context(root: Path) -> dict:
    policy = validation_stamp.load_policy(root)
    if policy is None:
        raise HarnessError("audit: precommitValidation disabled or absent; nothing to audit "
                           "(migration window)")
    staged = validation_stamp.staged_manifest(root, policy)
    head = validation_stamp.head_manifest(root, policy)
    changed = validation_stamp._changed_paths(staged, head)
    # audit_fp, not the bare manifest fp: an edited audit policy rides the key, so
    # `run`, `collect`, `record` and `waive` all stamp the SAME combined fingerprint
    # `check_audit` verifies against (seat3-F1).
    fp = audit_fp(root, validation_stamp.manifest_fingerprint(staged))
    return {"policy": policy, "staged": staged, "changed": changed, "fp": fp,
            "req": validation_stamp.required_profile(changed, policy)}


def _diff_excerpt(body: str, limit: int, path: str) -> tuple[str, bool]:
    """A bounded per-file diff slice cut on a LINE BOUNDARY so no token splits
    mid-word, followed by an explicit NOT-source marker. packet-diff-truncation-
    midtoken: a raw `body[:limit]` clipped `.hexdigest()` to `.hexd`, and two
    repo-blind http seats read the clip as source and BLOCKed on a fabricated
    'truncated hex digest'. A seat must never judge text at/after the marker as
    content. ponytail: a single line longer than `limit` has no boundary; the hard
    cut then stands (marker still flags it), no split-line reflow — rare and fine."""
    if len(body) <= limit:
        return body, False
    clip = body[:limit]
    nl = clip.rfind("\n")
    if nl > 0:
        clip = clip[:nl]
    return (clip + f"\n… [EXCERPT TRUNCATED near a {limit}-char budget — text at/after this marker is "
            f"NOT source; run `git diff --cached -- {path}` for the full file.]\n"), True


# `.harness/` subtrees that are TRANSIENT bookkeeping -- rewritten every turn, no
# intent to audit. Everything ELSE under `.harness/` is governance config
# (routing/*, audit-policy.json, prompts/*, project.json ...): a staged edit there
# changes how the harness BEHAVES. Excluded, not allow-listed, so a config file
# added later is covered by default -- an allow-list is exactly what leaked the
# executors.json card flip a seat could not see and block-voted on (the agy
# coding-lane false BLOCK, audit-10f1ce2771a9).
_HARNESS_TRANSIENT = (".harness/runs/", ".harness/context/", ".harness/state/",
                      ".harness/state-store/", ".harness/handoff/", ".harness/runtime/")


def _harness_config_diff(root: Path, budget: int) -> tuple[str, list[str]]:
    """Staged `.harness/` GOVERNANCE-config diff, as packet CONTEXT (never the
    audited surface): the config a commit stages that sits OUTSIDE `surfaceRoots`
    and is therefore absent from `changed`. Transient bookkeeping subtrees are
    dropped. Bounded per file with the same line-boundary cut as the main diff."""
    names = (_git(root, "diff", "--cached", "--name-only", "--", ".harness").stdout
             or "").splitlines()
    paths = [p for p in names if p and not p.startswith(_HARNESS_TRANSIENT)]
    if not paths:
        return "", []
    per_file = max(1500, budget // len(paths))
    parts = [_diff_excerpt(_git(root, "diff", "--cached", "--", p).stdout or "",
                           per_file, p)[0] for p in paths]
    return "".join(parts), paths


def run_audit(root: Path | str, *, plan_brief: str | None = None, seats: int | None = None,
              executor: str | None = None, dry_run: bool = False,
              lane_seat: str | None = None) -> dict:
    """Assemble the packet from the STAGED surface and launch N governed seats
    DETACHED (D2: `audit collect` is the separate settle step -- a foreground
    long-runner gets hard-killed, the gate-staged lesson). `--dry-run` builds the
    packet and RESOLVES every seat argv through the governed spawn seam without
    launching anything. `lane_seat` (`role:model:effort`) is who PRODUCED the staged
    change -- the attribution the BLOCK defect capsules are minted against."""
    root = Path(root)
    _refuse_unregistered_iteration(root)
    ctx = _staged_context(root)
    if not ctx["changed"]:
        raise HarnessError("audit run: staged surface unchanged vs HEAD; nothing to audit")
    base = _audit_dir(root, ctx["fp"])
    prior = _existing_run(base)
    if _rerun_is_noop(prior):
        # audit-run-not-idempotent: a REAL run already launched seats for this fp.
        # Re-running would spawn a second trio into the SAME seat dirs and orphan the
        # first (racing VERDICT.md/result.json). Recovery is `collect`'s job
        # (liveness+respawn); a genuine fresh run deletes run.json by hand.
        return {"status": "noop",
                "reason": "audit run already on record for this staged fingerprint; "
                          "a re-run would orphan its live seats. Recover with "
                          "`audit collect`, or delete this dir's run.json for a fresh run.",
                "dir": _rel(root, base), "packet": _rel(root, base / "packet.md"),
                "stagedFingerprint": prior.get("stagedFingerprint", ctx["fp"]),
                "plannedSeats": prior.get("plannedSeats"), "seats": prior.get("seats"),
                "band": prior.get("band"), "vendorFallback": prior.get("vendorFallback"),
                "degradeReason": prior.get("degradeReason"),
                "seatRows": [{k: v for k, v in r.items() if k != "argv"}
                             for r in prior.get("seatRows") or []],
                "next": "python scripts/harness.py audit collect"}
    reach = validation_stamp.reckon_reach(root, ctx["changed"])
    plan = seat_plan(root, ctx["policy"], ctx["changed"], ctx["req"], reach)
    planned = int(plan["seats"])
    count = int(seats) if seats else planned
    if count <= 0:
        raise HarnessError(f"audit run: {plan['reason']}; audit n/a for this surface "
                           f"(pass --seats N to audit it anyway)")
    degrade = (f"running {count} seat(s) below the planned {planned} ({plan['band']} band)"
               if count < planned else None)
    chosen, vendor_fallback = seat_executors(root, count, executor)
    # Seat sizing v1: the band decides the class VECTOR, the class decides each seat's
    # model/effort. Read from the INDEX blob like every other policy read (a disk-only
    # relaxation is invisible to the fingerprint, so it must be invisible here too).
    index_policy = load_audit_policy(root)
    classes = seat_class_plan(index_policy, plan["band"], count)
    # Per-file balanced excerpts (seat-2 F1, 2026-08-03): one alphabetically-first
    # large file must never eat the whole budget and hide the rest of the diff
    # from the seats -- every changed file gets its own bounded slice.
    per_file = max(2000, MAX_DIFF_CHARS // max(1, len(ctx["changed"])))
    parts: list[str] = []
    diff_cut: list[str] = []
    for path in ctx["changed"]:
        body, was_cut = _diff_excerpt(_git(root, "diff", "--cached", "--", path).stdout or "",
                                      per_file, path)
        if was_cut:
            diff_cut.append(path)
        parts.append(body)
    diff = "".join(parts)
    # Staged `.harness/` governance config rides the packet as CONTEXT: outside the
    # audited surface, so absent from `diff`, but a seat blind to it block-votes
    # "you never updated X" when X is staged (audit-10f1ce2771a9).
    harness_diff, harness_paths = _harness_config_diff(root, MAX_DIFF_CHARS)
    base.mkdir(parents=True, exist_ok=True)
    packet_ctx = {"fp": ctx["fp"], "changed": ctx["changed"], "req": ctx["req"], "reach": reach,
                  "plan": plan, "planBrief": plan_brief, "diff": diff, "diffCut": diff_cut,
                  "harnessDiff": harness_diff, "harnessPaths": harness_paths,
                  "verdictRel": "<seat>/VERDICT.md"}
    (base / "packet.md").write_text(build_packet(root, packet_ctx), encoding="utf-8")
    rows: list[dict] = []
    manifest = {"schemaVersion": "1.0", "stagedFingerprint": ctx["fp"], "at": now(),
                "plannedSeats": planned, "seats": count, "band": plan["band"],
                "bandReason": plan["reason"], "signals": plan["signals"],
                "planBrief": plan_brief, "vendorFallback": vendor_fallback,
                # The producing seat rides the manifest so `collect` can attribute the
                # BLOCK capsules without an argv of its own (the respawn/pin idiom).
                "laneSeat": lane_seat,
                # The operator pin rides the manifest so a respawn honors it instead
                # of picking elsewhere (collect has no argv of its own).
                "executorPin": executor,
                "degradeReason": degrade, "dryRun": dry_run,
                "affectedReach": ({"affected": reach["affected"], "total": reach["total"]}
                                  if reach else None),
                "seatRows": rows}
    # The manifest exists BEFORE seat 1 launches and is rewritten after EVERY row
    # (seat2-F4): an exception on seat k used to orphan the k-1 already-detached
    # seats -- no manifest, `collect` refusing, nobody holding their pids or logs.
    _persist_run(base, manifest)
    for index, (seat_executor, cls) in enumerate(zip(chosen, classes), start=1):
        seat_dir = base / f"seat-{index}"
        packet_ctx["verdictRel"] = _rel(root, seat_dir / "VERDICT.md")
        task = build_packet(root, packet_ctx)
        row = {"seat": index, "respawns": 0, "class": cls,
               "verdictFile": _rel(root, seat_dir / "VERDICT.md")}
        override, class_note = class_override(index_policy, cls, seat_executor)
        if class_note:
            row["classNote"] = class_note
        if dry_run:
            seat_dir.mkdir(parents=True, exist_ok=True)
            (seat_dir / "task.md").write_text(task, encoding="utf-8")
            argv, resolved, _env = seat_argv(task, seat_executor, seat_dir / "prompt.md", override)
            row.update({"executor": resolved, "pid": None, "argv": argv, "dryRun": True})
        else:
            row.update(_launch_seat(root, seat_dir, task, seat_executor, override))
        rows.append(row)
        _persist_run(base, manifest)
    _persist_run(base, manifest)
    return {"status": "dry-run" if dry_run else "launched", "dir": _rel(root, base),
            "packet": _rel(root, base / "packet.md"), "stagedFingerprint": ctx["fp"],
            "plannedSeats": planned, "seats": count, "band": plan["band"],
            "vendorFallback": vendor_fallback, "degradeReason": degrade,
            "seatRows": [{k: v for k, v in r.items() if k != "argv"} for r in rows],
            "next": "python scripts/harness.py audit collect"}


def _result_text(path: Path) -> str:
    """A WORKER_RESULT (the `openai_worker._coerce` shape) rendered back into seat
    text: `summary` FIRST -- the packet tells an API seat to open it with the verdict
    line -- then the schema fields that actually carry prose (`findings`
    title/evidence/recommendation, `nextStep`). Nothing is invented and
    `parse_verdict` gains no leniency: a summary that does not open with
    `VERDICT: SHIP|BLOCK` is still a contract failure."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    parts = [str(doc.get("summary") or "")]
    for item in doc.get("findings") or []:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("title") or ""))
        parts += [str(x) for x in (item.get("evidence") or [])]
        parts.append(str(item.get("recommendation") or ""))
    parts.append(str(doc.get("nextStep") or ""))
    return "\n".join(p for p in parts if p.strip())


def _claude_result_text(log_path: Path) -> str:
    """A claude CLI seat's FINAL answer, pulled from its `--output-format stream-json`
    log: the LAST `{"type":"result", "result": "..."}` envelope, JSON-decoded so its
    `VERDICT:` line becomes a real line-start. A headless claude seat on the read-only
    `review` profile cannot get its Write approved (default-ask mode, no interactive
    grant), so it emits the verdict in this message instead of a VERDICT.md -- and that
    message never sits at line 0 of the raw log (which opens with hook events). The LAST
    `type:result` wins = the newest run: `launch_detached` opens the log "wb" (truncated
    per launch), so a respawn never leaves a stale prior envelope to mis-read. Returns
    "" for a non-stream-json log (codex, a plain-stdout seat) so the caller falls
    through to the raw-log channel unchanged."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    final = ""
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result" and obj.get("result"):
            final = str(obj["result"])
    return final



def _materialize_verdict(root: Path, row: dict, text: str) -> None:
    """Persist a recovered verdict to the seat's VERDICT.md when the seat did not author
    it (an http seat writes result.json; a headless CLI seat's Write is permission-denied
    -- both carry the verdict through another channel). Only the SEAT subprocess is
    read-only (the packet's "do NOT write" is its contract); `collect_audit` is the
    harness-side settle/stamp step that ALREADY writes -- run.json, quality-state, the
    respawn unlinks -- so a write into the transient seat dir is in-contract, not a
    read-only violation. The HARNESS writes this copy from the text `_seat_text` already
    parsed; a header marks it non-seat-authored (the authentic record stays result.json /
    seat.log). An existing file (a seat that DID write) or empty text -> no-op, and an
    OSError never fails collect."""
    if not (text or "").strip():
        return
    verdict_file = Path(root) / row["verdictFile"]
    if verdict_file.is_file():
        return
    try:
        verdict_file.parent.mkdir(parents=True, exist_ok=True)
        verdict_file.write_text(
            "<!-- harness-materialized from the seat's output channel "
            "(result.json / seat.log stream-json); the seat did not author this file. -->\n"
            + text, encoding="utf-8")
    except OSError:
        pass  # a legibility copy must never wedge collect


def collect_audit(root: Path | str, *, note: str | None = None) -> dict:
    """Parse every seat verdict, apply the quorum, stamp. C3: the verdict is computed
    HERE, before any overseer reads the seats -- overseer disagreement goes through
    `audit record`, which is never auto-rescued."""
    root = Path(root)
    ctx = _staged_context(root)
    base = _audit_dir(root, ctx["fp"])
    try:
        manifest = json.loads((base / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HarnessError(f"audit collect: no audit run on record for the staged surface "
                           f"({ctx['fp'][:12]}). fix: {AUDIT_RUN_COMMAND}")
    parsed_rows = [(row, (text := _seat_text(root, row)), parse_verdict(text))
                   for row in manifest.get("seatRows") or []]
    # A seat with no verdict yet is not a failed seat while its process is STILL
    # ALIVE (2026-08-03: collect ran early, read the empty slots as contract
    # failures, and unlinked + respawned seats that were mid-answer). Judged BEFORE
    # the loop on purpose -- the loop's first destructive step must not run for one
    # row while another row is still working.
    running = [row["seat"] for row, _text, parsed in parsed_rows
               if parsed["verdict"] is None and processes.pid_alive(row.get("pid"))]
    if running:
        return {"status": "running", "seats": running, "dir": _rel(root, base),
                "reason": "seat(s) still running; re-run audit collect when they settle"}
    results: list[dict] = []
    invalid: list[dict] = []
    respawned: list[int] = []
    for row, text, parsed in parsed_rows:
        if parsed["verdict"] is None:
            seat_dir = base / f"seat-{row['seat']}"
            if int(row.get("respawns") or 0) < 1 and not manifest.get("dryRun"):
                # Invalid seat handling (owner ruling): re-spawn ONCE -- and on a
                # DIFFERENT vendor when one is reachable, because re-pinning the
                # executor that just failed the contract mostly re-buys the same
                # failure (the 2026-08-03 seats). Both stale slots go: whichever
                # file the respawn does not rewrite must not be re-read as its answer.
                task = (seat_dir / "task.md").read_text(encoding="utf-8")
                (seat_dir / "VERDICT.md").unlink(missing_ok=True)
                (seat_dir / "result.json").unlink(missing_ok=True)
                previous = row.get("executor")
                # ...and not on a vendor a SIBLING seat is already sitting on (owner,
                # 2026-08-03: "try not to repeat the seat"): the gas-ordered live[0]
                # kept landing on an already-seated vendor, so a respawn shrank the
                # cross-vendor spread exactly when the reachable pool was small.
                # Best-effort by construction -- seat_executors yields the exclusion
                # whole when it would leave nothing, and an operator pin outranks it.
                seated = {other.get("respawnExecutor") or other.get("executor")
                          for other, _text, _parsed in parsed_rows if other is not row}
                exclude = {e for e in ({previous} | seated) if e}
                pin = manifest.get("executorPin")
                picked, _reason = seat_executors(root, 1, pin, exclude=exclude or None)
                if picked[0] == previous and not pin and exclude != {previous}:
                    # seat_executors yields the WHOLE exclusion when it would leave
                    # nothing reachable, so a cornered pool can hand back the vendor
                    # that just failed. Yield the SIBLING half first: sitting two
                    # seats on one vendor still beats re-buying the same failure.
                    picked, _reason = seat_executors(root, 1, pin, exclude={previous})
                # The seat's CLASS rides the respawn, but the override is resolved for
                # the REPLACEMENT executor: the class is a sizing tier, not a vendor
                # pin, and the replacement may have no entry at all (recorded, never
                # silently sized as if it did).
                override, class_note = class_override(load_audit_policy(root),
                                                      row.get("class"), picked[0])
                row.pop("classNote", None)  # the OLD executor's note is not the new one's
                if class_note:
                    row["classNote"] = class_note
                row.update(_launch_seat(root, seat_dir, task, picked[0], override))
                if picked[0] != previous:
                    row["respawnExecutor"] = picked[0]
                row["respawns"] = int(row.get("respawns") or 0) + 1
                respawned.append(row["seat"])
            else:
                invalid.append({"seat": row["seat"], "error": parsed["error"]})
            continue
        # The verdict came through a non-file channel (an http result.json, or a headless
        # CLI seat's permission-denied Write forcing the verdict into its final message):
        # the HARNESS persists it to the seat's VERDICT.md so the on-disk artifact exists
        # without the seat ever holding write. A seat-authored file is left untouched.
        _materialize_verdict(root, row, text)
        results.append({"seat": row["seat"], "executor": row.get("executor"), **parsed})
    if respawned:
        _persist_run(base, manifest)
        return {"status": "respawned", "seats": respawned, "dir": _rel(root, base),
                "reason": "seat verdict failed the output contract; re-spawned once "
                          "(re-run `audit collect` when it settles)"}
    outcome = quorum(results)
    if outcome["verdict"] is None:
        return {"status": "incomplete", "dir": _rel(root, base), "invalidSeats": invalid,
                "reason": outcome["reason"], "fix": AUDIT_RECORD_COMMAND}
    degrade = manifest.get("degradeReason")
    if invalid:  # second contract failure: degrade to N-1, RECORDED
        degrade = ((degrade + "; ") if degrade else "") + \
            f"{len(invalid)} seat(s) degraded after a second contract failure"
    stamped = _stamp(root, ctx, verdict=outcome["verdict"], note=note,
                     seats=len(results), findings_count=outcome["findingsCount"],
                     dissent=outcome["dissent"], vendor_fallback=manifest.get("vendorFallback"),
                     degrade_reason=degrade, invalid_seats=invalid,
                     planned_seats=manifest.get("plannedSeats"))
    # The BLOCK is on the durable ledger NOW, so the streak below counts this cycle.
    iteration = (dict(_iteration_escalation(root, ctx),
                      **_mint_defects(root, base, manifest, results))
                 if outcome["verdict"] == "block" else {})
    return {"status": "recorded", "verdict": outcome["verdict"], "reason": outcome["reason"],
            "dir": _rel(root, base), "seats": len(results), "dissent": outcome["dissent"],
            "blockerSeats": outcome["blockerSeats"], "findingsCount": outcome["findingsCount"],
            "invalidSeats": invalid, "degradeReason": degrade, **iteration,
            "stagedFingerprint": stamped["stagedFingerprint"]["sha256"],
            "findings": [f for s in results for f in s["findings"]]}


def _stamp(root: Path, ctx: dict, *, verdict: str, note: str | None, seats: int,
           findings_count: int, dissent: list[str] | None = None,
           vendor_fallback: str | None = None, degrade_reason: str | None = None,
           invalid_seats: list[dict] | None = None, planned_seats: int | None = None,
           reason: str | None = None) -> dict:
    """The ONE stamp path for every audit verdict (collect / record / waive), so the
    row shape cannot drift between them. SPEC-161: the auditor actor rides along."""
    at = now()
    reach = validation_stamp.reckon_reach(root, ctx["changed"])
    payload = {
        "verdict": verdict, "at": at, "note": note, "reason": reason,
        "seats": seats, "plannedSeats": planned_seats, "findingsCount": findings_count,
        "dissent": list(dissent or []), "vendorFallback": vendor_fallback,
        "degradeReason": degrade_reason, "invalidSeats": list(invalid_seats or []),
        **({"affectedReach": {"affected": reach["affected"], "total": reach["total"]}}
           if reach else {}),
        "stagedFingerprint": {"sha256": ctx["fp"], "fileCount": len(ctx["staged"])},
        "actor": responsibility.make_actor("auditor"),
        "_runStart": at, "_lockStaleSeconds": ctx["policy"].get("lockStaleSeconds", 300),
    }
    validation_stamp.stamp_audit(root, dict(payload))
    return payload


def record_audit(root: Path | str, verdict: str, note: str | None = None) -> dict:
    """Manual fallback / overseer ARBITER path (C3), the `cmd_reckon` shape: stamp
    against the CURRENT staged fingerprint. A recorded `block` is never auto-rescued."""
    root = Path(root)
    if verdict not in ("ship", "block"):
        raise HarnessError("audit record: --verdict must be ship or block")
    ctx = _staged_context(root)
    _stamp(root, ctx, verdict=verdict, note=note, seats=0, findings_count=0,
           reason="recorded by hand (no seat quorum)")
    return {"status": "recorded", "verdict": verdict, "stagedFingerprint": ctx["fp"]}


def waive_audit(root: Path | str, reason: str) -> dict:
    """Decision (c), the `oracle waive` mirror: record the overseer's judged tolerance
    for the CURRENT staged surface -- the audit block becomes a WARN carrying this
    reason. An empty reason is refused: a waiver without a stated judgement is just a
    silent bypass."""
    root = Path(root)
    reason = (reason or "").strip()
    if not reason:
        raise HarnessError("audit waive: --reason must be non-empty (the judged tolerance is "
                           f"the whole record). fix: {AUDIT_WAIVE_COMMAND}")
    ctx = _staged_context(root)
    _stamp(root, ctx, verdict="waived", note=None, seats=0, findings_count=0, reason=reason)
    return {"status": "waived", "reason": reason, "stagedFingerprint": ctx["fp"]}


# --- CLI ---------------------------------------------------------------------

def cmd_audit(args) -> None:
    """`harness.py audit run|collect|record|check|waive` (the fourth commit-join leg)."""
    global _reprobe
    action = getattr(args, "action", "check") or "check"
    as_json = getattr(args, "json", False)
    if action in ("run", "collect") and _reprobe is None:
        # The seat pick may find every reading STALE; from the CLI it is allowed to
        # spend one full probe before believing a dark fleet (item 6). Wired here,
        # not at import: an in-process caller (scenarios, GUI) keeps the seam None.
        from harness_lib import vendor_fuel
        _reprobe = vendor_fuel.probe_all
    if action == "check":
        status, reason = validation_stamp.check_audit(ROOT)
        print(json.dumps({"status": status, "reason": reason}, ensure_ascii=False)
              if as_json else f"{status}: {reason}")
        raise SystemExit(0 if status in ("pass", "warn") else 1)
    if action == "run":
        emit(run_audit(ROOT, plan_brief=getattr(args, "plan", None),
                       seats=getattr(args, "seats", None),
                       executor=getattr(args, "executor", None),
                       dry_run=getattr(args, "dry_run", False),
                       lane_seat=getattr(args, "lane_seat", None)))
        return
    if action == "collect":
        emit(collect_audit(ROOT, note=getattr(args, "note", None)))
        return
    if action == "record":
        emit(record_audit(ROOT, getattr(args, "verdict", None) or "",
                          getattr(args, "note", None)))
        return
    if action == "waive":
        emit(waive_audit(ROOT, getattr(args, "reason", None) or ""))
        return
    raise HarnessError(f"audit: unknown action {action!r}")


def _selfcheck() -> None:
    """Hermetic proof of the two pieces of non-trivial logic that no other check
    reaches without a git repo: the verdict parser and the quorum rule."""
    ship = parse_verdict("VERDICT: SHIP\n\nno findings\n")
    assert ship["verdict"] == "ship" and ship["findings"] == [], ship
    # The real seat shape (audit-rule6-doors-opus5-VERDICT.md:9), markdown and all.
    real = parse_verdict("VERDICT: BLOCK\n\n**F1 — major — `scripts/x.py:465`**\nprose\n"
                         "**F2 — blocker — `tools/y.py:12`** evidence here\n")
    assert real["verdict"] == "block" and len(real["findings"]) == 2, real
    assert real["findings"][0]["file"] == "scripts/x.py" and real["findings"][0]["line"] == 465
    assert real["findings"][1]["severity"] == "blocker", real
    assert parse_verdict("VERDICT: BLOCK\nno findings at all\n")["verdict"] is None, \
        "findings-less BLOCK must be refused"
    assert parse_verdict("I think it is fine\n")["verdict"] is None, "off-contract must fail"
    # A headless CLI seat whose Write was permission-denied emits its verdict BEHIND a
    # preamble (the 2026-08-06 audit seats): the line is no longer line 0 and must still
    # parse. A raw stream-json line (VERDICT JSON-escaped mid-line) must still NOT.
    pre = parse_verdict("The Write is permission-gated; per the API-seat fallback:\n\n"
                        "**VERDICT: SHIP**\n\nF1 — nit — a.py:1 — narrow but honest\n")
    assert pre["verdict"] == "ship" and len(pre["findings"]) == 1, pre
    assert parse_verdict('{"type":"result","result":"x VERDICT: SHIP y"}')["verdict"] is None, \
        "a raw stream-json line has no line-start VERDICT and must not parse"
    # A seat stating BOTH a line-start SHIP and BLOCK is a contract failure (F1@806):
    # NON-vacuous -- both `**VERDICT: ...**` lines DO match `_VERDICT_RE`, so this reverts
    # red if the conflict guard is removed (a `verdicts[0]` first-match regression ships
    # this block; block-dominates blocks a ship). The preamble variant above (one match)
    # still parses -- only genuinely-conflicting line-start verdicts are refused.
    conflict = parse_verdict("**VERDICT: SHIP**\n\nOn reflection:\n\n**VERDICT: BLOCK**\n"
                             "F1 — blocker — a.py:1 — boom\n")
    assert conflict["verdict"] is None and "conflicting" in (conflict["error"] or ""), conflict

    seats = [{"seat": 1, "verdict": "ship", "findings": []},
             {"seat": 2, "verdict": "ship", "findings": [{"severity": "nit"}]}]
    assert quorum(seats)["verdict"] == "ship" and quorum(seats)["dissent"] == []
    split = [*seats[:1], {"seat": 2, "verdict": "block",
                          "findings": [{"severity": "blocker"}]}]
    out = quorum(split)
    assert out["verdict"] == "block" and out["dissent"] == ["seat-1:ship"], out
    assert out["blockerSeats"] == ["seat-2"], out
    assert quorum([])["verdict"] is None

    # The dual ladder's two inputs (fix seat1-F1), pure and hermetic: `seat_plan`
    # keeps whichever band owes MORE seats, so a same-commit relaxation cannot
    # lower the bar. agq-9 drives the resolution itself against a real repo.
    doc = {"enabled": True, "trivialFloor": {"minChangedFiles": 3, "minDiffLines": 80,
                                             "minAffectedScenarios": 5},
           "bands": {"defaultSeats": 2, "single": {"seats": 1, "maxDiffLines": 100,
                                                   "maxChangedFiles": 2}}}
    signals = {"changedFiles": 6, "diffLines": 300, "affectedScenarios": 0,
               "testOnlySurface": False}
    head_band = _band(doc, signals, 6, 300, 0)
    index_band = _band(None, signals, 6, 300, 0)  # staged policy disabled/absent
    assert head_band["seats"] == 2 and head_band["band"] == "default", head_band
    assert index_band["seats"] == 0 and index_band["band"] == "unadopted", index_band
    assert max(head_band["seats"], index_band["seats"]) == 2, "disabling must not lower the bar"
    assert _band(doc, signals, 6, 300, 0) == head_band, "equal docs must agree"
    tiny = {"changedFiles": 1, "diffLines": 2, "affectedScenarios": 0, "testOnlySurface": False}
    assert _band(doc, tiny, 1, 2, 0)["seats"] == 0, "below the floor under BOTH docs stays 0"

    # Fix B's pure predicate (agq-15 drives the pick itself): an empty tank and a
    # dark CLI vendor are UNREACHABLE, while an http vendor whose probe came back
    # fresh stays eligible with no quota number at all.
    api = {"value": {"baseUrl": "https://x/v1"}, "effectiveStatus": "fresh"}
    assert _unreachable("codex", {}, {}) == "no fresh CLI reading"
    assert _unreachable("codex", {"codex": 0.0}, {}) == "empty tank"
    assert _unreachable("claude", {"claude": 40.0}, {}) is None
    assert _unreachable("nvidia-compat", {}, {"nvidia-compat": api}) is None
    assert _unreachable("nvidia-compat", {}, {"nvidia-compat": dict(api, effectiveStatus="stale")})

    # Seat sizing v1 (audit-seat-sizing): the class ladder is PURE config. The band's
    # vector pads with its LAST entry (--seats above the plan), truncates below it, and
    # a policy without the block leaves every seat on the `review` profile's own pins.
    sizing = {"seatClasses": {"bands": {"ceiling": ["xhigh", "high", "high"]},
                              "classes": {"xhigh": {}, "high": {"claude": {"effort": "high"}}}}}
    assert seat_class_plan(sizing, "ceiling", 5) == ["xhigh", "high", "high", "high", "high"]
    assert seat_class_plan(sizing, "ceiling", 2) == ["xhigh", "high"]
    assert seat_class_plan(sizing, "default", 2) == [None, None], "no vector -> profile defaults"
    assert seat_class_plan(None, "ceiling", 1) == [None], "no policy -> profile defaults"
    assert class_override(sizing, "high", "claude") == ({"effort": "high"}, None)
    # The two ways to MISS an override are one typed, recorded path -- an executor
    # the class does not name, and a class name a band vector invented. Neither may
    # crash the leg. The EMPTY reference class (`xhigh: {}`) is exempt: defaults
    # are its intent, so it notes nothing.
    for cls, executor in (("high", "kimi"), ("nope", "claude")):
        override, note = class_override(sizing, cls, executor)
        assert override == {} and note == f"class {cls}: no override for {executor}; " \
            "profile defaults", (cls, executor, override, note)
    assert class_override(sizing, "xhigh", "claude") == ({}, None), "reference class: no note"
    assert class_override(sizing, None, "claude") == ({}, None), "classless seat: no note"

    # The ITERATION ledger (decision 2): the TRAILING run of block rows, a ship or
    # waived row resets it, garbage/non-dict rows are skipped like _audit_row_for_fp
    # does, and a missing ledger is not a streak (fail-open). agq-25/26 drive the
    # escalation itself; this is the reader, pure and hermetic.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        assert block_streak(tmp_root) == {"count": 0, "firstFp": None, "lastFp": None}, \
            "a missing ledger must never wedge the leg"
        led = tmp_root / ".harness" / "runs" / "audit-results.jsonl"
        led.parent.mkdir(parents=True, exist_ok=True)

        def _rows(*rows: str) -> None:
            led.write_text("".join(r + "\n" for r in rows), encoding="utf-8")

        block_a, ship_b = ('{"verdict": "block", "stagedFingerprint": "%s"}' % ("a" * 64),
                           '{"verdict": "ship", "stagedFingerprint": "%s"}' % ("b" * 64))
        block_c, block_d = ('{"verdict": "block", "stagedFingerprint": "%s"}' % ("c" * 64),
                            '{"verdict": "block", "stagedFingerprint": "%s"}' % ("d" * 64))
        _rows(block_a, "{not json", ship_b, block_c, "[]", block_d)
        streak = block_streak(tmp_root)
        assert streak == {"count": 2, "firstFp": "c" * 64, "lastFp": "d" * 64}, streak
        assert iteration_escalation_id(streak) == "audit-iteration:" + "c" * 12, streak
        _rows(block_c, block_d, '{"verdict": "waived", "stagedFingerprint": "e"}')
        assert block_streak(tmp_root)["count"] == 0, "a waived row closes the iteration"
        _rows(block_a, ship_b, block_c)
        assert block_streak(tmp_root)["count"] == 1, "a ship row resets, it does not subtract"
    assert iteration_escalation_id({}) == "audit-iteration:", "no streak, no head"

    # The two refusals that fire BEFORE any git/state read (no fixture needed):
    # a waiver without a stated judgement is a silent bypass, and the arbiter's
    # verdict vocabulary is closed.
    for call in (lambda: waive_audit(Path("."), "   "),
                 lambda: record_audit(Path("."), "maybe")):
        try:
            call()
            raise AssertionError("refusal expected")
        except HarnessError:
            pass

    # packet-diff-truncation-midtoken: the excerpt cuts on a line boundary and flags
    # itself NOT-source, so a clipped token can never read as content a repo-blind seat
    # then fabricates a BLOCK against. The sentinel is sized so the limit falls INSIDE
    # `.hexdigest()` — `body[:20]` ends in `.hexd`; the `.hexd not in clip` assert only
    # holds because the boundary cut strips it, so deleting the boundary logic re-fails
    # the check (not the vacuous >limit sentinel that passed against any implementation).
    whole, cut0 = _diff_excerpt("short\n", 100, "f.py")
    assert whole == "short\n" and cut0 is False, whole
    clip, cut1 = _diff_excerpt("keep\n" + "x" * 10 + ".hexdigest()\n", 20, "f.py")
    assert cut1 and clip.startswith("keep\n") and "NOT source" in clip, clip
    assert ".hexd" not in clip and clip.endswith("]\n"), clip

    # build_packet is pure over ctx: the truncation instruction reaches the seat, and
    # a reach-less ctx must not crash (the `or {}` at the reach read guards the .get).
    pkt = build_packet(Path("."), {"fp": "a" * 64, "changed": ["f.py"], "diff": "@@ d",
                                   "diffCut": ["f.py"], "verdictRel": "seat-1/VERDICT.md",
                                   "planBrief": None})
    assert "NEVER judge text at or near a marker as source" in pkt, pkt
    assert "```diff" in pkt and "ADVERSARIAL" in pkt, pkt[:120]

    # audit-run-not-idempotent: run.json absent/corrupt/non-dict reads as None (a fresh
    # run may proceed); a written object record round-trips its dryRun flag.
    with tempfile.TemporaryDirectory() as td:
        b = Path(td)
        assert _existing_run(b) is None, "absent -> None"
        (b / "run.json").write_text("{not json", encoding="utf-8")
        assert _existing_run(b) is None, "corrupt -> None (fresh run proceeds)"
        (b / "run.json").write_text("[]", encoding="utf-8")
        assert _existing_run(b) is None, "valid-but-non-dict -> None (no AttributeError)"
        (b / "run.json").write_text('{"dryRun": false, "seats": 3}', encoding="utf-8")
        assert (_existing_run(b) or {}).get("dryRun") is False, "real run round-trips"
    # the guard DECISION (pure): a REAL prior run no-ops a bare re-run; a dry-run preview
    # and an absent/corrupt record never block.
    assert _rerun_is_noop({"dryRun": False}) is True, "real run no-ops a bare re-run"
    assert _rerun_is_noop({"dryRun": True}) is False, "a dry preview never blocks"
    assert _rerun_is_noop(None) is False, "no record -> a fresh run proceeds"
    print("audit_leg self-check: ok", flush=True)


if __name__ == "__main__":
    _selfcheck()
