#!/usr/bin/env python3
"""Overseer review toolkit — the mechanical half of the review ritual (ort).

Steps 1-4 of the overseer-loop playbook's per-completion ritual, as
deterministic checks over the working tree vs HEAD, so a cheaper overseer
(Opus 4.8 per owner decision 2026-07-13) runs the same safety net Fable ran
by hand. Every signature here was caught live that day:

  * footprint      — worker touched files outside its plan's HARD boundary
  * blank-fraud    — line-budget "paid" by stripping blank lines (72 moved
                     lines cannot explain a 384-line reduction)
  * mojibake-delta — UTF-8→CP1252 rewrites, HEAD-attributed so pre-existing
                     artifacts are never blamed on the worker
  * gaming         — deleted assert/check()/Scenario lines

Advisory by design: `review` always exits 0 — the verb reports, the overseer
judges (measurement before control). Registered via cli_registry.
Self-check: python scripts/harness_lib/overseer_review.py.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes, telemetry_sink  # noqa: E402

MOJIBAKE = re.compile("â€|Ã©|Ã£|Ã§|Â§|â†|Ã¢")  # mojibake-ok: detector definition
# Lines carrying this marker hold INTENTIONAL telltale patterns (detector
# definitions, grep examples) and are excluded from the delta count.
MOJIBAKE_EXEMPT = "mojibake-ok"
GAMING_REMOVED = re.compile(r"^-.*(?:\bassert\s|check\(|Scenario:\s*\[)")
BLANK_REMOVED = re.compile(r"^-\s*$")
FOOTPRINT_HEAD = "## File footprint"
# Words a brief may decorate a section title with. Anything else in the heading means
# it is PROSE about the section, not the section — see _paths_under_heading.
HEAD_DECOR = frozenset({"hard", "final", "canonical", "approved"})
SCRATCH_HEAD = "## Cleanup contract"
# A write grant under this prefix is scratch by nature: nothing durable lives in
# the run dir, and the gate's structural checks (release-hygiene, json,
# directory-guide) walk it — a lane that leaves artifacts there turns a green
# gate red 6 minutes later while blaming release-hygiene (incident 2026-07-28).
SCRATCH_PREFIX = ".harness/runs/"
GIT_TIMEOUT = 60


def _git(root: Path, *args: str) -> str:
    proc = processes.run_quiet(["git", "-C", str(root), *args], timeout=GIT_TIMEOUT,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
    return proc.stdout or ""


def _paths_under_heading(plan_path: Path, heading: str) -> list[str]:
    """Repo-relative paths under an `## <heading>` section: backticked anywhere on
    the line, or a plain `- path` bullet (2026-07-20: two real briefs used
    unbackticked bullets and silently parsed to ZERO paths — the footprint check
    SKIPped; tolerate both, the D038 advisory still nudges backticks)."""
    if not plan_path.exists():
        return []
    lines = plan_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    paths: list[str] = []
    inside = False

    def _pathlike(m: str) -> bool:
        return "/" in m or m.endswith((".py", ".md", ".json"))

    # The heading is matched by its WORDS, not by a literal prefix: briefs really get
    # written as `## 3. HARD file footprint` (numbered, adjectived), and a boundary that
    # silently goes UNCHECKED because of a section number is a guard that only fires when
    # it is not needed (measured twice on 2026-07-30/31 — the playbook already asked for
    # the canonical heading, so the paperwork was not the missing part).
    # WORDS, and only the LAST ones: substring containment shipped first (2026-07-31) and
    # an audit caught it — `## Risks to the file footprint` would have opened the section
    # and WIDENED the allowed boundary with whatever paths that prose mentions. A guard
    # that can be opened by a sentence about itself is worse than no guard.
    want = heading.lstrip("# ").casefold().split()

    def _is_head(line: str) -> bool:
        """The heading's words ARE the wanted phrase, plus decoration only. Numbering
        is dropped (letters-only), the phrase must appear consecutively, and every other
        word must be one a brief actually decorates a section title with — never prose.
        That is what separates `3. HARD file footprint` (a footprint) from `Risks to the
        file footprint` (a sentence about one)."""
        words = re.findall(r"[a-z]+", line.lstrip("# ").casefold())
        runs = [words[i:i + len(want)] for i in range(len(words) - len(want) + 1)]
        return want in runs and all(w in HEAD_DECOR for w in words if w not in want)

    for line in lines:
        if line.startswith("## "):
            inside = _is_head(line)
            continue
        if not inside:
            continue
        ticked = [m.replace("\\", "/") for m in re.findall(r"`([^`\s]+)`", line) if _pathlike(m)]
        if ticked:
            paths.extend(ticked)
            continue
        bullet = re.match(r"\s*[-*]\s+(\S+)\s*(?:\(.*\))?\s*$", line)
        if bullet and _pathlike(bullet.group(1)):
            paths.append(bullet.group(1).replace("\\", "/"))
    return list(dict.fromkeys(paths))


def parse_footprint(plan_path: Path) -> list[str]:
    """Paths under the plan's `## File footprint` heading."""
    return _paths_under_heading(plan_path, FOOTPRINT_HEAD)


def changed_files(root: Path) -> list[str]:
    out: list[str] = []
    for line in _git(root, "status", "--porcelain").splitlines():
        if len(line) > 3:
            out.append(line[3:].strip().strip('"').replace("\\", "/"))
    return out


def check_footprint(root: Path, plan_path: Path) -> list[dict[str, Any]]:
    allowed = parse_footprint(plan_path)
    if not allowed:
        # An unparseable boundary is UNREVIEWABLE, not clean. This row used to be
        # "skip", so the verdict read "clean: mechanical checks green" while the
        # boundary check had silently never run (observed 2026-07-29, SPEC-173
        # rounds). WARN is advisory as always — rc stays 0, the overseer judges.
        return [{"check": "footprint", "status": "WARN",
                 "detail": f"no paths parsed from {plan_path.name} — the boundary is "
                           f"UNCHECKED, not clean: either the `{FOOTPRINT_HEAD}` heading is "
                           f"missing or misnamed (prefix match, so `{FOOTPRINT_HEAD} (HARD)` "
                           "also works), or its bullets are not path-like (need a `/` or a "
                           ".py/.md/.json suffix)"}]
    # Globstar entries (`ui/dist/**`, `ui/dist/*`) normalize to their dir prefix —
    # briefs write them naturally and the 2026-07-21 wb5 review false-WARNed on it.
    allowed = [re.sub(r"/\*{1,2}$", "/", a) for a in allowed]
    changed = changed_files(root)
    # state/handoff churn is the loop's own bookkeeping, never a violation
    exempt = (".harness/state/", ".harness/context/", ".harness/handoff/")

    def _covered(path: str) -> bool:
        """Exact entry, OR under an allowed dir entry (`ui/dist/`), OR a porcelain
        untracked-DIR entry (`docs/design/`) that covers an allowed file (2026-07-20:
        a rebuilt ui/dist bundle false-WARNed as out-of-boundary)."""
        if path in allowed:
            return True
        for a in allowed:
            if a.endswith("/") and path.startswith(a):
                return True
            if path.endswith("/") and a.startswith(path):
                return True
        return False

    def _touched(entry: str) -> bool:
        return any(c == entry or (entry.endswith("/") and c.startswith(entry))
                   or (c.endswith("/") and entry.startswith(c))  # untracked-dir porcelain entry
                   for c in changed)

    violations = [c for c in changed
                  if not _covered(c) and not c.startswith(exempt)]
    untouched = [a for a in allowed if not _touched(a)]
    rows = [{"check": "footprint", "status": "WARN" if violations else "ok",
             "detail": ("changed outside the plan boundary: " + ", ".join(violations[:8]))
                       if violations else f"{len(changed)} change(s), all inside the boundary"}]
    if untouched:
        rows.append({"check": "footprint:untouched", "status": "info",
                     "detail": "allowed but unchanged: " + ", ".join(untouched[:8])})
    rows.extend(check_scratch(root, plan_path, allowed))
    return rows


def parse_scratch(plan_path: Path) -> list[str]:
    """Repo-relative paths DECLARED under `## Cleanup contract`.

    Stricter grammar than the footprint on purpose: only a path that OPENS its
    line (bare or backticked, with an optional bullet) is a declaration. A
    cleanup section is prose — it explains what may be written and why it must
    go — and the greedy any-backtick-on-the-line rule swallowed a `.harness/runs/`
    mentioned mid-sentence, which then reported as leftover because the run dir
    always exists. Declarations open lines; mentions do not."""
    if not plan_path.exists():
        return []
    out: list[str] = []
    inside = False
    for line in plan_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("## "):
            inside = line.startswith(SCRATCH_HEAD)
            continue
        if not inside:
            continue
        m = re.match(r"\s*(?:[-*]\s+)?`?([^`\s]+)`?", line)
        cand = m.group(1).replace("\\", "/") if m else ""
        if "/" in cand or cand.endswith((".py", ".md", ".json")):
            out.append(cand)
    return list(dict.fromkeys(out))


def check_scratch(root: Path, plan_path: Path, allowed: list[str]) -> list[dict[str, Any]]:
    """The question the footprint check never asked: did the lane EMPTY its box?

    `footprint` answers "did the worker write outside its boundary?" — on
    2026-07-28 it correctly said no while the lane left a repo tarball, an
    extracted worktree and a UTF-16 JSON inside its declared boundary. Four
    structural gate checks went red 6 minutes later, none of them saying "a
    worker left scratch behind". The obligation existed only as playbook prose,
    i.e. in the overseer's memory, and the overseer forgot.

    Two rows, both WARN (this verb reports; the overseer judges — see module
    docstring), inert unless the brief opts in:
      scratch:leftover — a declared scratch path that still EXISTS on disk;
      scratch:undeclared — a write grant under `.harness/runs/` with no
        `## Cleanup contract` section, i.e. the omission itself.
    Auto-sweeping was rejected: deleting a worker's output before the overseer
    reads it destroys evidence (the 2026-07-28 measurement file survived only
    because the artifacts were still there when the diagnosis needed them).
    """
    declared = parse_scratch(plan_path)
    rows: list[dict[str, Any]] = []
    ungranted = [a for a in allowed
                 if a.replace("\\", "/").startswith(SCRATCH_PREFIX) and a not in declared]
    if ungranted:
        rows.append({"check": "scratch:undeclared", "status": "WARN",
                     "detail": f"write grant under {SCRATCH_PREFIX} with no `{SCRATCH_HEAD}` "
                               "entry: " + ", ".join(ungranted[:8])
                               + " — declare it so its cleanup is checkable"})
    if declared:
        leftover = [d for d in declared if (root / d).exists()]
        rows.append({"check": "scratch:leftover", "status": "WARN" if leftover else "ok",
                     "detail": ("still on disk after the lane: " + ", ".join(leftover[:8])
                                + " — scrub before integrating; the gate walks this tree")
                               if leftover else
                               f"{len(declared)} declared scratch path(s), all cleaned"})
    return rows


def check_blank_fraud(root: Path) -> dict[str, Any]:
    diff = _git(root, "diff", "HEAD")
    removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    blanks = sum(1 for l in removed if BLANK_REMOVED.match(l))
    total = len(removed)
    fraud = blanks > 50 and total and blanks / total > 0.30
    return {"check": "blank-fraud", "status": "WARN" if fraud else "ok",
            "detail": f"{blanks} blank of {total} removed line(s)"
                      + (" — the burndown-fraud signature: verify the claimed extraction adds up"
                         if fraud else "")}


def _mojibake_count(text: str) -> int:
    return sum(len(MOJIBAKE.findall(line)) for line in text.splitlines()
               if MOJIBAKE_EXEMPT not in line)


def check_mojibake_delta(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in changed_files(root):
        path = root / rel
        if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".toml", ".txt", ".yaml"}:
            continue
        now = _mojibake_count(path.read_text(encoding="utf-8", errors="ignore"))
        if not now:
            continue
        before = _mojibake_count(_git(root, "show", f"HEAD:{rel}"))
        if now > before:
            rows.append({"check": "mojibake-delta", "status": "WARN",
                         "detail": f"{rel}: +{now - before} new mojibake pattern(s) vs HEAD "
                                   f"(HEAD={before}, now={now})"})
    return rows or [{"check": "mojibake-delta", "status": "ok",
                     "detail": "no new mojibake vs HEAD in changed files"}]


def check_gaming(root: Path) -> dict[str, Any]:
    diff = _git(root, "diff", "HEAD")
    hits = [l[1:].strip()[:80] for l in diff.splitlines() if GAMING_REMOVED.match(l)]
    return {"check": "gaming", "status": "WARN" if hits else "ok",
            "detail": (f"{len(hits)} removed assertion/check/Scenario line(s), e.g. "
                       + " | ".join(hits[:3])) if hits else "no assertions or checks removed"}


def review(root: Path | str, plan_path: Path | str | None = None) -> dict[str, Any]:
    root = Path(root)
    rows: list[dict[str, Any]] = []
    if plan_path:
        rows.extend(check_footprint(root, Path(plan_path)))
    rows.append(check_blank_fraud(root))
    rows.extend(check_mojibake_delta(root))
    rows.append(check_gaming(root))
    warns = [r for r in rows if r["status"] == "WARN"]
    # SPEC-173 Phase 0 rule 3: the defect series, one sink discriminated by `kind`.
    # Anchored to the `root` PARAMETER, never module ROOT: the self-check and orv
    # run review() against throwaway repos where WARNs are planted BY DESIGN.
    sink = root / ".harness" / "state" / "defect-telemetry.jsonl"
    for w in warns:
        telemetry_sink.append_jsonl(sink, {"kind": "review-warn",
                                           "plan": str(plan_path) if plan_path else None, **w})
    return {"rows": rows, "warnings": len(warns),
            "verdict": "REVIEW-WARN: judge the WARN rows before integrating" if warns
                       else "clean: mechanical checks green — proceed to divergence judgment"}


def cmd_review(args) -> int:
    from harness_lib import common

    report = review(common.ROOT, getattr(args, "plan", None))
    if getattr(args, "json", False) or common.agent_output_compact():
        common.emit(report)
        return 0
    for row in report["rows"]:
        print(f"{row['status'].upper():<5} {row['check']}: {row['detail']}")
    print(f"\n{report['verdict']}")
    return 0


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = lambda *a: processes.run_quiet(["git", "-C", str(root), *a], timeout=60,  # noqa: E731
                                             capture_output=True, text=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (root / "mod.py").write_text("def f(x):\n    assert x > 0\n\n    return x\n", encoding="utf-8")
        (root / "clean.md").write_text("# ok — fine\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "base")

        # worker "edits": strips the blank line + the assert, adds mojibake, touches a file
        # outside the footprint
        (root / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
        (root / "clean.md").write_text("# ok â€” fine\n", encoding="utf-8")  # mojibake-ok: planted fixture
        (root / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
        plan = root / "plan.md"
        plan.write_text("# PLAN\n\n## File footprint (HARD)\n\n- `mod.py`\n\n## Constraints\n",
                        encoding="utf-8")

        report = review(root, plan)
        by = {}
        for r in report["rows"]:
            by.setdefault(r["check"], r)
        assert by["footprint"]["status"] == "WARN" and "sneaky.py" in by["footprint"]["detail"]
        assert by["gaming"]["status"] == "WARN" and "assert" in by["gaming"]["detail"]
        assert by["mojibake-delta"]["status"] == "WARN" and "clean.md" in by["mojibake-delta"]["detail"]
        assert by["blank-fraud"]["status"] == "ok"  # 1 blank removed — under thresholds
        assert report["warnings"] >= 3 and "REVIEW-WARN" in report["verdict"]

        # clean tree → all green
        run("add", "-A")
        run("commit", "-q", "-m", "wip")
        clean = review(root, plan)
        assert clean["warnings"] == 0 and "clean" in clean["verdict"]

        # plain-bullet footprints parse too (the 2026-07-20 silent-SKIP class):
        # unbackticked `- path` bullets, a parenthesized annotation, prose ignored.
        plan.write_text("# PLAN\n\n## File footprint\n\n- mod.py\n- ui/dist/ (rebuilt bundle)\n"
                        "\nNothing else. No scenario edits.\n\n## Constraints\n", encoding="utf-8")
        parsed = parse_footprint(plan)
        assert parsed == ["mod.py", "ui/dist/"], parsed

        # dir-prefix boundary matching: a file UNDER an allowed dir entry is inside
        # the boundary; a porcelain untracked-DIR entry covering an allowed file is
        # too; an unrelated file still WARNs (the 2026-07-20 dist false-WARN class).
        (root / "ui" / "dist" / "assets").mkdir(parents=True)
        (root / "ui" / "dist" / "assets" / "index-abc.js").write_text("x\n", encoding="utf-8")
        (root / "outside.py").write_text("x = 1\n", encoding="utf-8")
        fp_rows = check_footprint(root, plan)
        head = fp_rows[0]["detail"]
        assert "outside.py" in head and "index-abc.js" not in head, head

        # globstar entries normalize to the dir prefix (the 2026-07-21 wb5
        # false-WARN class): `ui/dist/**` covers the rebuilt hashed bundle.
        plan.write_text("# PLAN\n\n## File footprint\n\n- `mod.py`\n- `ui/dist/**`\n\n"
                        "## Constraints\n", encoding="utf-8")
        fp_rows = check_footprint(root, plan)
        head = fp_rows[0]["detail"]
        assert "outside.py" in head and "index-abc.js" not in head, head
    print("overseer_review self-check: ok")


if __name__ == "__main__":
    _demo()
