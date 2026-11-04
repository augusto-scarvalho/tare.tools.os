#!/usr/bin/env python3
"""Tie receipts: every normal non-merge commit names the mechanism that now
enforces what it shipped -- or says honestly that none is owed.

Two entry points, one grammar:
  * the `commit-msg` hook (this file run as a direct script) blocks a missing or
    malformed receipt at the edge, one typed `fix:` line per error;
  * doctor's `commit-tie` check (repo_health) audits a bounded recent window for
    what `--no-verify` and history rewrites let through.

The receipt lives in Git itself: no parallel state file, no ambient-budget cost,
identical for loop and interactive sessions, and it survives compaction.

STDLIB ONLY, with ZERO harness_lib imports on purpose. The hook runs this file as
a script from the repo top-level with nothing of `scripts/` on sys.path, while
repo_health imports it as `from harness_lib import commit_tie`. Both paths only
work while this module stays self-contained.

And ZERO process spawns, which is the same constraint from the other side. The
no-raw-subprocess ratchet (spawn_ratchet.py) fails on a new unmediated spawn site
and its remedy is `processes.py` — which this file may not import. So the spawns
went away instead: the merge probe resolves `.git` by reading it, and the audit's
`git log` lives in repo_health (already mediated-or-baselined there) and hands the
records here for PARSING. The hook path shells out to nothing at all, which is
also the right shape for something git runs on every commit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KINDS: tuple[str, ...] = ("hook", "gate", "doctor", "playbook", "backlog", "scenario")
SCISSORS = "# ------------------------ >8 ------------------------"  # git --verbose marker

# D3: anchor the audit by TIMESTAMP, not by the ship sha. A timestamp is knowable
# at write time (a sha anchor would need a self-referential two-step ship commit),
# it survives the history rewrites that orphan a sha, and its only cost -- a forged
# pre-anchor committer date escaping the audit -- is acceptable for a WARN-only
# backstop. It also buys day-one silence: every commit that predates the receipt
# rule sits before the anchor and never cries wolf. Verified before shipping,
# `git log -1 --format=%cI` was 2026-07-27T21:48:28-03:00, earlier than the anchor.
AUDIT_SINCE = "2026-07-28T00:00:00-03:00"
AUDIT_LIMIT = 20  # bounded window: this is a backstop, not a history report

_TIE = re.compile(r"^Tie:\s*(?P<payload>.*)$")  # exact-case prefix; kind case matters below

_ERR_MISSING = (
    "tie-missing: no Tie: trailer in the final trailer paragraph (every normal "
    "non-merge commit carries >=1)"
    "\nfix: append 'Tie: <hook|gate|doctor|playbook|backlog|scenario>:<locator>' "
    "or 'Tie: none:<reason>' as the message's last paragraph")
_ERR_BAD_KIND = (
    "tie-bad-kind: {line!r} — kind must be one of "
    "hook|gate|doctor|playbook|backlog|scenario, or none"
    "\nfix: rewrite as 'Tie: <kind>:<locator>' (lowercase kind) or 'Tie: none:<reason>'")
_ERR_EMPTY_LOCATOR = (
    "tie-empty-locator: {line!r} names a kind but no locator"
    "\nfix: name the mechanism, e.g. 'Tie: scenario:ct-commit-tie'")
_ERR_EMPTY_NONE_REASON = (
    "tie-empty-none-reason: {line!r} — 'Tie: none:' requires a non-empty reason"
    "\nfix: say why no mechanism is owed, e.g. 'Tie: none:typo-only; no capability, "
    "procedure, or discovery changed'")
_ERR_NONE_MIXED = (
    "tie-none-mixed: 'Tie: none' cannot share a commit with mechanism Tie lines"
    "\nfix: keep either the mechanism line(s) or the none line — a receipt is a "
    "mechanism or an honest exemption, never both")
_ERR_OUTSIDE_TRAILERS = (
    "tie-outside-trailers: 'Tie:' line(s) found outside the final trailer paragraph"
    "\nfix: move them into the last paragraph of the message (git's trailer block)")

_BLOCK_HEAD = "commit-msg tie gate: BLOCKED"
_BLOCK_FOOT = ("git commit --no-verify skips this gate; the bypass surfaces in "
               "doctor's commit-tie audit")


def _trailer_split(text: str) -> tuple[list[str], list[str]]:
    """(trailer-block lines, all lines of every EARLIER paragraph).

    Git's trailer rule is "the last paragraph", and a message reaches us with
    everything the editor left behind: CRLF from a Windows editor, `#` comment
    lines, and under `commit --verbose` an entire diff below the scissors line
    that can carry literal `Tie:` text. All three are stripped BEFORE the split,
    or a diff hunk gets a vote on the receipt.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in normalized.split("\n"):
        if line == SCISSORS:
            break
        # ponytail: default comment char only; core.commentChar is unset in this
        # repo and honoring it would mean reading git config from the parser.
        if line.startswith("#"):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in kept:
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    if not paragraphs:
        return ([], [])
    return (paragraphs[-1], [ln for p in paragraphs[:-1] for ln in p])


def validate_message(text: str) -> list[str]:
    """Empty list = valid. Each entry is one typed error carrying its own fix: line.

    Semantic adequacy is never machine-judged: whether `Tie: doctor:commit-tie` is
    the RIGHT receipt for a given diff is a human call, and a parser that guessed
    would only teach people to write receipts that please the parser.
    """
    trailer, earlier = _trailer_split(text)
    tie_lines = [ln for ln in trailer if _TIE.match(ln)]
    if not tie_lines:
        # A receipt the author THINKS they wrote is worse than a missing one, so a
        # stray `Tie:` in the body gets its own typed error instead of silence.
        if any(_TIE.match(ln) for ln in earlier):
            return [_ERR_OUTSIDE_TRAILERS]
        return [_ERR_MISSING]

    errors: list[str] = []
    has_none = False
    for line in tie_lines:
        match = _TIE.match(line)
        assert match is not None  # tie_lines was filtered on this very match
        kind, sep, value = match.group("payload").strip().partition(":")
        if not sep or (kind not in KINDS and kind != "none"):
            errors.append(_ERR_BAD_KIND.format(line=line))
            continue
        if kind == "none":
            has_none = True
            if not value.strip():
                errors.append(_ERR_EMPTY_NONE_REASON.format(line=line))
        elif not value.strip():
            errors.append(_ERR_EMPTY_LOCATOR.format(line=line))
    # `none` is an exemption, not a mechanism, so it cannot share a commit with any
    # other tie line -- a malformed one included, because the author meant something
    # by it and the honest exemption is no longer honest next to it.
    if has_none and len(tie_lines) > 1:
        errors.append(_ERR_NONE_MIXED)
    return errors


# The git log format repo_health runs for the audit. It lives HERE, next to the
# parser that consumes it, so the two can never drift apart across files: \x1e
# separates commits and \x1f the three fields, because a message body contains
# newlines (all of them) and would otherwise be mistaken for a record break.
LOG_FORMAT = "%x1e%H%x1f%s%x1f%B"
LOG_ARGS: tuple[str, ...] = ("log", "--no-merges", f"--since={AUDIT_SINCE}",
                             "-n", str(AUDIT_LIMIT), f"--format={LOG_FORMAT}")


def audit_records(stdout: str) -> list[dict]:
    """Parse `git LOG_ARGS` output; return one row per commit whose message fails
    validate_message. PURE -- the caller owns the spawn (see the module docstring).

    Row: {"sha": <40-hex>, "subject": <first line>, "errors": list[str]}.
    Unparseable input yields [] -- this feeds a WARN-only doctor check, and an
    audit that could not read is not an audit that found something.
    """
    rows: list[dict] = []
    for record in stdout.split("\x1e"):
        if not record.strip():
            continue
        fields = record.split("\x1f")
        if len(fields) < 3:
            continue
        errors = validate_message(fields[2])
        if errors:
            rows.append({"sha": fields[0].strip(), "subject": fields[1],
                         "errors": errors})
    return rows


def _git_dir(cwd: Path) -> Path | None:
    """The repo's git dir, resolved by READING rather than spawning `git rev-parse`
    (module docstring: this file spawns nothing). git runs hooks with cwd = the
    working tree's top level, so `.git` is right here: a directory in an ordinary
    clone, and a FILE holding `gitdir: <path>` in a worktree or submodule."""
    dot = cwd / ".git"
    try:
        if dot.is_dir():
            return dot
        if dot.is_file():
            line = dot.read_text(encoding="utf-8", errors="replace").strip()
            if line.startswith("gitdir:"):
                target = Path(line.split(":", 1)[1].strip())
                return target if target.is_absolute() else (cwd / target)
    except OSError:
        return None
    return None


def _is_merge(cwd: Path | None = None) -> bool:
    """MERGE_HEAD present => git is composing a merge, and a merge message carries
    no receipt of its own. commit-msg DOES fire for merges, so the exemption has to
    live here. An unresolvable git dir means "not a merge" and the message gets
    validated, which is the safe direction."""
    git_dir = _git_dir(Path(cwd) if cwd else Path.cwd())
    try:
        return git_dir is not None and (git_dir / "MERGE_HEAD").exists()
    except OSError:
        return False


def _say(text: str) -> None:
    """stderr, never raising.

    The reconfigure is the measured half: on a cp437 console the em dash in these
    messages otherwise arrives as a literal `\\u2014` (stderr defaults to
    backslashreplace, so it degrades rather than raises), and a commit-time error
    nobody can read gets solved with --no-verify. The swallow is the cheap half:
    a closed stderr pipe raising BrokenPipeError out of the fail-open path would
    turn "passing fail-open" into a blocked commit.
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def _hook_main(argv: list[str]) -> int:
    try:
        if _is_merge():
            return 0
        # utf-8 pinned (D8). git hands us the path it wrote, usually
        # .git/COMMIT_EDITMSG relative to the top-level cwd it sets for hooks.
        text = Path(argv[0]).read_bytes().decode("utf-8", errors="replace")
        errors = validate_message(text)
    except Exception as exc:
        # A broken guard must never wedge every commit in the repo: an UNEXPECTED
        # failure passes fail-open and says so, and doctor's commit-tie audit is
        # the backstop. Blocking happens only on typed validation errors below.
        _say(f"commit-msg tie gate: internal error ({exc}); passing fail-open")
        return 0
    if not errors:
        return 0
    # Reporting deliberately sits OUTSIDE the fail-open try: once the message is
    # typed-invalid the verdict is 1 even if printing the reason fails.
    _say("\n".join([_BLOCK_HEAD, *errors, _BLOCK_FOOT]))
    return 1


if __name__ == "__main__":
    raise SystemExit(_hook_main(sys.argv[1:]))
