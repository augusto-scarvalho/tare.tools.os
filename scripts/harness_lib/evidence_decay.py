"""W29 bet 1 — "evidence that rots": evidence freshness decay, observe-only.

Evidence anchored to a commit sha rots as the repo moves past it. `assess`
is pure git-derived and returns a CLASS, never an invented [0,1] scalar:

  fresh      — evidence sha == current HEAD
  aging      — HEAD moved, but no declared path changed since the sha
  stale      — a declared path changed since the sha (evidence no longer
               describes the code it vouches for)
  unanchored — no/unknown sha (pre-W29 artifacts): decay can't be computed,
               and "can't compute" must never read as "fresh"

Committed state only — working-tree dirt is the CURRENT change being judged,
not decay of past evidence. Consumers today: evidence_bundle (per-workflow,
paths=() so only fresh/aging/unanchored apply there). Round:
docs/research/w29-evidence-gated-assurance-round.md. Self-check: run this file.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes  # noqa: E402

_OVERLAP_CAP = 20  # handles-not-bodies: never ship an unbounded file list


def _git(root: Path, *args: str) -> str | None:
    """stdout on rc 0, None otherwise (missing git/sha degrades, never raises)."""
    try:
        proc = processes.run_quiet(["git", "-C", str(root), *args], timeout=60,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace")
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def head_sha(root: Path | str) -> str | None:
    out = _git(Path(root), "rev-parse", "HEAD")
    return out.strip() if out else None


def assess(root: Path | str, sha: str | None,
           paths: Iterable[str] = ()) -> dict[str, Any]:
    """Freshness of evidence anchored at `sha`, relative to current HEAD."""
    root = Path(root)
    head = head_sha(root)
    prefixes = tuple(str(p).replace("\\", "/").rstrip("/") for p in paths if p)
    base: dict[str, Any] = {"sha": sha or None, "headSha": head,
                            "commitsBehind": None, "changedSince": None,
                            "overlap": []}
    if not sha or head is None or _git(root, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        return {**base, "class": "unanchored"}
    if sha == head:
        return {**base, "class": "fresh", "commitsBehind": 0, "changedSince": 0}
    behind_raw = _git(root, "rev-list", "--count", f"{sha}..HEAD")
    changed_raw = _git(root, "diff", "--name-only", sha, "HEAD")
    if behind_raw is None or changed_raw is None:  # e.g. sha not an ancestor after rebase
        return {**base, "class": "unanchored"}
    changed = [ln.strip().replace("\\", "/") for ln in changed_raw.splitlines() if ln.strip()]
    overlap = [f for f in changed
               if any(f == p or f.startswith(p + "/") for p in prefixes)]
    return {**base,
            "class": "stale" if overlap else "aging",
            "commitsBehind": int(behind_raw.strip() or 0),
            "changedSince": len(changed),
            "overlap": overlap[:_OVERLAP_CAP]}


def _demo() -> None:
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = lambda *a: sp.run(["git", "-C", str(root), *a], capture_output=True,  # noqa: E731
                                text=True, encoding="utf-8", errors="replace", timeout=60)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "b.py").write_text("y = 1\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        sha0 = head_sha(root)
        assert sha0 and assess(root, sha0)["class"] == "fresh"

        (root / "b.py").write_text("y = 2\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "touch b")
        aging = assess(root, sha0, paths=["a.py"])
        assert aging["class"] == "aging" and aging["commitsBehind"] == 1, aging
        stale = assess(root, sha0, paths=["b.py"])
        assert stale["class"] == "stale" and stale["overlap"] == ["b.py"], stale
        assert assess(root, None)["class"] == "unanchored"
        assert assess(root, "0" * 40)["class"] == "unanchored"
    print("evidence_decay self-check: ok")


if __name__ == "__main__":
    _demo()
