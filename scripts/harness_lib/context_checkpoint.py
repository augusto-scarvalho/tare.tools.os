#!/usr/bin/env python3
"""Context checkpoint + post-compact reinjection (ckpt).

When a coding agent's context window fills and gets compacted, its in-flight
state (current item, phase, design decisions, verify incantations) evaporates.
The harness already had both halves of the cure and neither worked: the
SessionStart hook READ .harness/context/*.md and printed only a one-line
banner (whatever a hook prints is what enters the fresh context window — the
content was read and discarded), and nothing ever produced fresh state into
STATE.md/NEXT_STEPS.md, so they rotted as bootstrap templates. Two seams:

  * write_checkpoint / clear_checkpoint — the producer: rewrites ONE bounded
    `<!-- inflight:start/end -->` block in .harness/context/NEXT_STEPS.md
    (latest item/phase/verify + the NEWEST trail entry; older entries are
    folded into the append-only .harness/context/checkpoint-trail.md, which is
    never reinjected), redacted at write via secret_scan.redact_text (both
    files are tracked; the note is agent text);
  * render_reinjection — the delivery: the in-flight block whole plus ONE typed
    pointer line per other canonical file; the SessionStart hook prints THIS.

Agent-agnostic on purpose: plain files, no agent-specific memory. Registered
as the `checkpoint` verb via cli_registry (zero harness.py edits).
Self-check: python scripts/harness_lib/context_checkpoint.py.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402
from harness_lib.common import HarnessError  # noqa: E402

NEXT_STEPS_REL = ".harness/context/NEXT_STEPS.md"
HANDOFF_REL = ".harness/handoff/handoff.md"
# Hydration is SINGLE-CHANNEL (v5, measured 2026-07-28): this payload used to dump a
# head+tail slice of every canonical file (~3.2k tok) while AGENTS.md's startup read
# told the agent to open the same six files IN FULL (~5.3k tok) — CONTEXT/STATE/
# NEXT_STEPS arrived twice, ~8.5k tok per session. A head+tail dump is strictly worse
# than a summary of the same file (EXP-2 §5.5.1) and replaces no read. So: the
# in-flight block stays INLINE (it exists nowhere else), everything else becomes a
# typed pointer the agent opens on demand.
INLINE_RELS = (NEXT_STEPS_REL,)
POINTER_RELS = (".harness/context/CONTEXT.md", ".harness/context/STATE.md",
                ".harness/context/LEDGER_HEAD.md", HANDOFF_REL,
                ".harness/state/task-state.json", ".harness/state/quality-state.json")
# Unchanged by the pointer flip: LEDGER_HEAD (records-produced), handoff.md
# (workflow-produced) and both state JSONs stay optional; the first three are REQUIRED.
REQUIRED_RELS = INLINE_RELS + POINTER_RELS[:2]
START, END = "<!-- inflight:start -->", "<!-- inflight:end -->"
TRAIL_MAX = 1  # INLINE trail entries; older history is APPENDED, never deleted, to TRAIL_ARCHIVE_REL
TRAIL_ARCHIVE_REL = ".harness/context/checkpoint-trail.md"
TRAIL_ARCHIVE_HEADER = ("# Checkpoint trail archive "
                        "(append-only; newest entry lives inline in NEXT_STEPS.md)")
# Vendor inline ceiling: Claude Code injects hook stdout inline only below 10k
# chars — larger payloads are persisted to a file the session never reads
# (code.claude.com/docs/en/hooks). The WHOLE-payload budget lives in the
# SessionStart hook + the hib scenario; only NEXT_STEPS.md is rendered as a body,
# so these two budgets bound the whole state share (the rest is one line each).
NEXT_STEPS_CAP = 2200  # inflight fields always rendered; trail render-capped to fit
BLOCK_CAP = 1300       # inflight block render share: fields whole + newest trail entries
SUMMARY_CAP = 100      # chars of the pointer's one-line lead


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="minutes")


_BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)


def read_checkpoint(root: Path | str) -> str | None:
    path = Path(root) / NEXT_STEPS_REL
    if not path.exists():
        return None
    m = _BLOCK_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
    return m.group(0) if m else None


def _field(block: str | None, name: str) -> str:
    m = re.search(rf"^- {name}: (.*)$", block or "", re.M)
    return m.group(1).strip() if m else ""


def _trail(block: str | None) -> list[str]:
    return re.findall(r"^  - .*$", block or "", re.M)


def _fold_trail(root: Path, displaced: list[str]) -> int:
    """Append displaced trail entries to the append-only archive; return the TOTAL
    archived history (pre-existing entries included — a fresh block after `--clear`
    still points at everything already folded). Append-only: the prior text is read,
    the new text built whole, then written once. Never reinjected (not in either
    of INLINE_RELS / POINTER_RELS)."""
    path = root / TRAIL_ARCHIVE_REL
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if displaced:  # empty -> never create the file (fresh repo / post-clear write)
        text = text or (TRAIL_ARCHIVE_HEADER + "\n\n")
        # redact again at append: idempotent, but the archive is tracked and the
        # redact-at-write rule has no exceptions.
        text += "".join(secret_scan.redact_text(line) + "\n" for line in displaced)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return len(re.findall(r"^  - ", text, re.M))


def write_checkpoint(root: Path | str, note: str, *, item: str = "",
                     phase: str = "", verify: str = "") -> dict[str, Any]:
    """Rewrite the in-flight block; omitted item/phase/verify carry over."""
    note = secret_scan.redact_text(" ".join(note.split()))
    if not note:
        raise HarnessError('checkpoint needs a note\n'
                           'fix: harness.py checkpoint "what just happened / next" '
                           '[--item X] [--phase spec|test|impl|verify|commit] [--verify "cmd"]')
    root = Path(root)
    old = read_checkpoint(root)
    item = secret_scan.redact_text(item.strip()) or _field(old, "Item")
    phase = phase.strip() or _field(old, "Phase")
    verify = secret_scan.redact_text(verify.strip()) or _field(old, "Verify")
    full = _trail(old) + [f"  - {_now()} [{phase or '-'}] {note}"]
    trail = full[-1:]                       # newest entry stays inline (reinjected)
    archived = _fold_trail(root, full[:-1])  # everything older is folded into the archive
    lines = [START, f"## In-flight checkpoint (updated {_now()})", ""]
    # no Note line: the trail's last entry IS the note (was a verbatim duplicate)
    lines += [f"- {label}: {val}" for label, val in
              (("Item", item), ("Phase", phase), ("Verify", verify)) if val]
    header = f"Trail: {archived} older entries in {TRAIL_ARCHIVE_REL}" if archived else "Trail:"
    lines += ["", header, *trail, END]
    block = "\n".join(lines)
    path = root / NEXT_STEPS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else "# Next Steps\n"
    if _BLOCK_RE.search(text):
        text = _BLOCK_RE.sub(lambda _m: block, text)  # lambda: block may hold regex escapes
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
    return {"path": NEXT_STEPS_REL, "item": item, "phase": phase,
            "verify": verify, "note": note, "trailLen": len(trail), "archived": archived}


def clear_checkpoint(root: Path | str) -> dict[str, Any]:
    """Remove the block (end of an iteration); calm no-op when absent."""
    path = Path(root) / NEXT_STEPS_REL
    if not path.exists():
        return {"cleared": False}
    text = path.read_text(encoding="utf-8", errors="ignore")
    new = _BLOCK_RE.sub("", text)
    if new == text:
        return {"cleared": False}
    path.write_text(re.sub(r"\n{3,}", "\n\n", new), encoding="utf-8")
    return {"cleared": True}


def merge_preserving_inflight(new_text: str, old_text: str) -> str:
    """Carry the inflight block into a wholesale rewrite of NEXT_STEPS.md.

    workflow_lifecycle regenerates the file from a template on every reduce;
    without this, an in-flight checkpoint dies silently mid-iteration."""
    m = _BLOCK_RE.search(old_text or "")
    if not m or _BLOCK_RE.search(new_text):
        return new_text
    return new_text.rstrip("\n") + "\n\n" + m.group(0) + "\n"


def _cap(text: str, cap: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text.rstrip("\n")
    head = raw[:max(int(cap * 0.6), 200)].decode("utf-8", errors="ignore")
    tail = raw[-max(int(cap * 0.4), 100):].decode("utf-8", errors="ignore")
    return f"{head}\n… [capped: file is {len(raw)} bytes] …\n{tail}".rstrip("\n")


def _cap_block(block: str) -> str:
    """Fields (Item/Phase/Verify) always whole — EXP-2 measured them falling
    into _cap's dropped middle. The trail is history: render the newest entries
    that fit BLOCK_CAP; the full trail stays in the file."""
    if len(block.encode("utf-8")) <= BLOCK_CAP:
        return block
    # The header carries the archive pointer ("Trail: N older entries in …"), so match
    # the LINE, not the literal "Trail:\n" — and re-emit it verbatim below.
    m = re.search(r"(?m)^Trail.*\n", block)
    if not m:  # hand-edited block without a trail header: cap it, never crash the render
        return _cap(block, BLOCK_CAP)
    pre, tail_part = block[:m.start()], block[m.end():]
    entries = [ln for ln in tail_part.splitlines() if ln.startswith("  - ")]
    kept: list[str] = []
    size = len(pre) + 80
    for entry in reversed(entries):
        if size + len(entry) + 1 > BLOCK_CAP:
            break
        kept.insert(0, entry)
        size += len(entry) + 1
    kept = kept or entries[-1:]  # never drop the newest entry
    dropped = len(entries) - len(kept)
    note = f" [{dropped} older entries render-capped; full trail in the file]" if dropped else ""
    header = m.group(0).rstrip("\n") + note
    return f"{pre}{header}\n" + "\n".join(kept) + f"\n{END}"


def _render_next_steps(text: str) -> str:
    m = _BLOCK_RE.search(text)
    if not m:
        return _cap(text, NEXT_STEPS_CAP)
    block = _cap_block(m.group(0))
    rest = (text[:m.start()] + text[m.end():]).strip("\n")
    room = max(NEXT_STEPS_CAP - len(block.encode("utf-8")), 400)
    return _cap(rest, room) + "\n\n" + block


def _mtime(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="minutes")


def _summary(text: str) -> str:
    """First non-empty, non-heading line, capped. Deterministic on purpose: a
    hand-written summary field would be one more thing to keep fresh, and a stale
    one is worse than none."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:SUMMARY_CAP]
    return ""


def _pointer(path: Path, rel: str) -> str:
    """One typed line per canonical file: freshness, weight and a lead — enough to
    decide whether to open it, cheap enough to never be worth deduping. Absence
    reads calmly, exactly as the old per-file render did."""
    if not path.exists():
        return f"- {rel} — absent"
    lead = _summary(path.read_text(encoding="utf-8", errors="ignore"))
    return f'- {rel} (updated {_mtime(path)}, {path.stat().st_size} B) — "{lead}"; read on demand'


def render_reinjection(root: Path | str) -> str:
    """The string the SessionStart hook prints — i.e. what the agent gets.

    Pointer-first: the in-flight block is rendered whole because it exists nowhere
    else; every other canonical file is a pointer, because the agent can open it and
    a head+tail dump neither replaces that read nor beats a summary (EXP-2 §5.5.1).
    """
    root = Path(root)
    parts = ["=== Harness canonical context (reinjected on session start / after compact) ==="]
    for rel in INLINE_RELS:
        path = root / rel
        if not path.exists():
            parts.append(f"\n## {rel} — absent")
            continue
        body = _render_next_steps(path.read_text(encoding="utf-8", errors="ignore"))
        parts.append(f"\n## {rel} (updated {_mtime(path)})\n{body}")
    parts.append("\n## Canonical state — pointers (this payload is the authority; open a "
                 "file only when the task needs its body)\n"
                 + "\n".join(_pointer(root / rel, rel) for rel in POINTER_RELS))
    try:  # heartbeat fuel line (SPEC-168): the overseer sees per-vendor gas here and
        from harness_lib import vendor_fuel  # balances routing — read-only, never probes.
        fuel = vendor_fuel.fuel_summary(root)
        if fuel:
            parts.append("\n" + fuel)
    except Exception:  # noqa: BLE001 — a fuel read must never break the reinjection render
        pass
    parts.append("\nTrust the freshest timestamp; the in-flight checkpoint block, when present, "
                 "is the current work state. Maintain it with `python scripts/harness.py checkpoint`.")
    return "\n".join(parts)


def reinjection_loss(root: Path | str) -> list[str]:
    """Checkpoint fields present in NEXT_STEPS.md but MISSING from the rendered
    reinjection. EXP-2 (shipped 2026-07-17) measured Item/Verify falling into
    _cap's dropped middle; _render_next_steps now renders the block whole, so a
    non-empty result means that guarantee regressed. Pure read; [] = healthy."""
    block = read_checkpoint(Path(root))
    if not block:
        return []
    rendered = render_reinjection(Path(root))
    return [name for name in ("Item", "Verify")
            if _field(block, name) and f"- {name}: {_field(block, name)}" not in rendered]


def _groom_reminder(root: Path) -> str:
    """A wind-down guard, NOT a blocker: closing a loop (`checkpoint --clear`) with a
    non-empty intake queue and no groom TODAY violates the overseer-loop mandate
    ('a loop never closes leaving the queue un-groomed', backlog-groom §1). Returns a
    reminder line, or '' when the queue is empty or already groomed today. Never
    raises — a bad read must not break the clear."""
    try:
        from harness_lib import intake_queue
        pending = len(intake_queue.pending(root))
    except Exception:  # noqa: BLE001
        return ""
    if pending == 0:
        return ""
    try:
        today = dt.date.today().isoformat()
    except Exception:  # noqa: BLE001 — date.today() is banned in workflow scripts; tolerate
        return ""
    grooms = sorted((root / "docs" / "research").glob("backlog-groom-*.md"))
    groomed_today = any(today in p.name for p in grooms)
    if groomed_today:
        return ""
    return (f"WIND-DOWN REMINDER: intake queue has {pending} pending and no groom today "
            f"({today}). The overseer-loop mandate is that a loop never closes un-groomed "
            f"(backlog-groom §1): spawn the report-only queue-slice miner, audit its ledger "
            f"vs git, apply via `intake decide`, write docs/research/backlog-groom-{today}.md. "
            f"(Reminder only — the clear proceeded.)")


def cmd_checkpoint(args) -> int:
    from harness_lib import common

    if getattr(args, "render", False):
        print(render_reinjection(common.ROOT))
        return 0
    if getattr(args, "clear", False):
        common.emit(clear_checkpoint(common.ROOT))
        reminder = _groom_reminder(common.ROOT)
        if reminder:
            print(reminder, file=sys.stderr)
        return 0
    note = " ".join(getattr(args, "note", None) or [])
    if not note:
        print(read_checkpoint(common.ROOT)
              or 'no in-flight checkpoint\nfix: harness.py checkpoint "note" '
                 '[--item X] [--phase impl] [--verify "cmd"] starts one')
        return 0
    common.emit(write_checkpoint(common.ROOT, note,
                                 item=getattr(args, "item", None) or "",
                                 phase=getattr(args, "phase", None) or "",
                                 verify=getattr(args, "verify", None) or ""))
    return 0


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake = "sk-FAKEKEYdonotleak0123456789abcdef"
        first = write_checkpoint(root, f"spec written {fake}", item="ckpt", phase="spec",
                                 verify="python testing/scenarios/context_checkpoint.py")
        assert first["item"] == "ckpt" and fake not in (root / NEXT_STEPS_REL).read_text(encoding="utf-8")
        for i in range(TRAIL_MAX + 2):  # carry-over + repeated displacement
            state = write_checkpoint(root, f"step {i}", phase="impl")
        assert state["item"] == "ckpt" and state["verify"].endswith(".py")
        assert state["trailLen"] == TRAIL_MAX == 1
        assert state["archived"] == TRAIL_MAX + 2  # every write but the first displaces one
        block = read_checkpoint(root)
        assert block and block.count("\n  - ") == 1 and "- Phase: impl" in block
        assert f"Trail: {state['archived']} older entries in {TRAIL_ARCHIVE_REL}" in block
        archive = (root / TRAIL_ARCHIVE_REL).read_text(encoding="utf-8")
        assert archive.count("\n  - ") == state["archived"] and fake not in archive
        assert f"step {TRAIL_MAX + 1}" in block and f"step {TRAIL_MAX + 1}" not in archive
        # over-cap render: the pointer header survives and the newest entry stays whole
        write_checkpoint(root, "pad " * 400, phase="impl")
        capped = _cap_block(read_checkpoint(root) or "")
        assert capped.count(END) == 1 and capped.count("\n  - ") == 1
        assert f"older entries in {TRAIL_ARCHIVE_REL}" in capped and "render-capped" not in capped
        (root / ".harness" / "context" / "CONTEXT.md").write_text(
            "# Heading skipped\n\nthe lead line\n" + "x" * 5000, encoding="utf-8")
        out = render_reinjection(root)
        assert out.index(NEXT_STEPS_REL) < out.index("CONTEXT.md")
        # pointer-first: mtime + size + lead, and NO body of a pointer file at all
        assert '.harness/context/CONTEXT.md (updated 2' in out and '"the lead line"' in out
        assert "[capped: file is" not in out and "xxxxx" not in out
        assert "STATE.md — absent" in out and "(updated 2" in out
        assert len(re.findall(r"(?m)^- \.harness/\S+ (?:\(updated |— absent)", out)) == len(POINTER_RELS)
        rewrite = merge_preserving_inflight("# Next Steps\n\nnew workflow summary\n",
                                            (root / NEXT_STEPS_REL).read_text(encoding="utf-8"))
        assert "new workflow summary" in rewrite and START in rewrite and "- Phase: impl" in rewrite
        assert merge_preserving_inflight("fresh", "no block here") == "fresh"
        # EXP-2 structurally closed: even an over-cap NEXT_STEPS renders the
        # inflight block whole — Item/Verify never fall into the dropped middle.
        assert reinjection_loss(root) == []
        ns = root / NEXT_STEPS_REL
        ns.write_text("# Next Steps\n" + "filler line\n" * 400, encoding="utf-8")
        write_checkpoint(root, "pad " * 200, item="exp2-item", verify="python probe.py")
        assert reinjection_loss(root) == []
        assert "- Item: exp2-item" in render_reinjection(root)
        assert "- Note:" not in read_checkpoint(root)  # trail carries the note
        assert clear_checkpoint(root) == {"cleared": True}
        assert read_checkpoint(root) is None and clear_checkpoint(root) == {"cleared": False}
        assert (root / TRAIL_ARCHIVE_REL).exists()  # clear touches NEXT_STEPS only
        with tempfile.TemporaryDirectory() as fresh:  # first write ever: nothing to archive
            fresh_root = Path(fresh)
            first_ever = write_checkpoint(fresh_root, "first ever", item="ckpt")
            assert first_ever["archived"] == 0 and not (fresh_root / TRAIL_ARCHIVE_REL).exists()
            assert "\nTrail:\n" in (read_checkpoint(fresh_root) or "")
        # groom wind-down reminder: empty queue -> silent; pending + no groom-today ->
        # fires; a groom-<today>.md -> silent again (the mandate is satisfied).
        assert _groom_reminder(root) == "", "empty intake queue must not nag"
        (root / ".harness" / "state").mkdir(parents=True, exist_ok=True)
        (root / ".harness" / "state" / "intake-queue.json").write_text(
            '{"entries": [{"id": "x1", "status": "pending", "ask": "a real open ask"}]}',
            encoding="utf-8")
        fires = _groom_reminder(root)
        assert fires and "never closes un-groomed" in fires, "pending + no groom must remind"
        research = root / "docs" / "research"
        research.mkdir(parents=True, exist_ok=True)
        (research / f"backlog-groom-{dt.date.today().isoformat()}.md").write_text("groomed", encoding="utf-8")
        assert _groom_reminder(root) == "", "a groom today satisfies the mandate -> silent"
    try:
        write_checkpoint(Path(tempfile.gettempdir()), "   ")
        raise AssertionError("empty note accepted")
    except HarnessError as exc:
        assert "needs a note" in str(exc)
    print("context_checkpoint self-check: ok")


if __name__ == "__main__":
    _demo()
