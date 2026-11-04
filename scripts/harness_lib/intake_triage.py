"""SPEC-117 intake-triage: deterministic SPEC-116 routing at the moment of a request.

`triage_packet(root, prompt)` returns "" (silence) or a <=10-line advisory packet:
the classified task profile + covered-doc candidates (records + doc-find over the
prompt's salient terms) + the three-exit reminder. It NEVER raises (any internal
failure -> "", invariant 1), NEVER calls an LLM, and writes nothing (invariant 5).
Thresholds live in `.harness/project.json` -> `intakeTriage` (invariant 5), not code.

Classifier reuse (invariant 3): `classify` lives in `scripts/harness.py` — the router
itself — so importing it drags the whole router; SPEC-117 says subprocess in that case.
We run `harness.py classify --task` under HARNESS_AGENT_OUTPUT=compact (cheaper JSON).
Covered-doc: `records.search` is imported directly (clean, root-scoped); `doc-find`
lives in discovery.py behind harness.py's bind() and is not cleanly importable, so it
is subprocessed too. Both lookups run ONLY for prompts that clear the filter (R27).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_HARNESS_PY = Path(__file__).resolve().parents[1] / "harness.py"

_DEFAULTS = {"minPromptChars": 40, "silentProfiles": ["scan", "debug", "cheap"], "maxHits": 3}

# Enough stopwords to surface the salient nouns of a request (en + a few pt-br fillers
# common in this repo's prompts). ponytail: static set, not a linguistics project.
_STOP = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at", "by",
    "with", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "have", "has", "had", "this", "that", "these", "those", "it", "its", "as", "from",
    "into", "if", "then", "we", "you", "they", "them", "our", "your", "can", "will",
    "should", "would", "could", "add", "make", "need", "want", "please", "let", "use",
    "using", "que", "de", "da", "do", "para", "com", "uma", "os", "no", "na", "por", "em",
}

_REMINDER = ("three exits (SPEC-116): no behavior change -> no artifact; covered -> "
             "records/doc-find recap + versioned amendment; new -> intake template "
             "(specs/templates/intake-refinement.md)")


def _config(root: Path) -> dict[str, Any]:
    cfg: Any = {}
    try:
        data = json.loads((root / ".harness" / "project.json").read_text(encoding="utf-8"))
        cfg = data.get("intakeTriage", {})
    except Exception:
        cfg = {}
    out = dict(_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in _DEFAULTS if k in cfg})
    return out


def _terms(prompt: str, limit: int = 6) -> list[str]:
    """Top non-stopword tokens by frequency (deterministic: -count then alpha)."""
    counts: dict[str, int] = {}
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", prompt.lower()):
        if tok not in _STOP:
            counts[tok] = counts.get(tok, 0) + 1
    return sorted(counts, key=lambda t: (-counts[t], t))[:limit]


def _classify_profile(root: Path, prompt: str) -> str | None:
    env = {**os.environ, "HARNESS_AGENT_OUTPUT": "compact"}
    proc = subprocess.run(
        [sys.executable, str(_HARNESS_PY), "classify", "--task", prompt],
        cwd=str(root), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=25,
    )
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout).get("taskProfile")


def _record_hits(root: Path, terms: list[str], limit: int) -> list[str]:
    try:
        from harness_lib import records
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from harness_lib import records
    rows = records.search(root, " ".join(terms), limit=limit)
    return [f"  - rec/{r.get('kind', '?')}: {r.get('title', '')}".rstrip() for r in rows[:limit]]


def _doc_hits(root: Path, terms: list[str], limit: int) -> list[str]:
    proc = subprocess.run(
        [sys.executable, str(_HARNESS_PY), "doc-find", *terms],
        cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=25,
    )
    if proc.returncode != 0:
        return []
    hits = json.loads(proc.stdout).get("hits", [])
    return [f"  - doc: {h.get('path')}" for h in hits[:limit] if h.get("path")]


def triage_packet(root: Any, prompt: str) -> str:
    """SPEC-117: "" for silence, else a <=10-line advisory triage packet. Never raises."""
    try:
        root = Path(root)
        text = (prompt or "").strip()
        cfg = _config(root)
        # invariant 2: command-shaped / too-short -> silence, before any lookup cost
        if not text or text[0] in "/!" or len(text) < int(cfg["minPromptChars"]):
            return ""
        profile = _classify_profile(root, text)
        if not profile or profile in set(cfg["silentProfiles"]):
            return ""  # non-feature profile (scan/debug/cheap) -> silence
        # invariant 3/5: two covered-doc lookups, only now that the prompt passed the filter
        maxhits = int(cfg["maxHits"])
        terms = _terms(text)
        lines = [f"[intake-triage] profile: {profile} (feature-shaped request)"]
        hits: list[str] = []
        if terms:
            for lookup in (_record_hits, _doc_hits):
                try:
                    hits += lookup(root, terms, maxhits)
                except Exception:
                    pass
        lines.append("covered-doc candidates:" if hits
                     else "covered-doc candidates: none found (likely a NEW door)")
        lines += hits
        lines.append(_REMINDER)
        return "\n".join(lines[:10])
    except Exception:
        return ""  # invariant 1: any internal failure -> emit nothing (silent pass-through)


if __name__ == "__main__":  # ponytail: filters are deterministic; one real classify e2e
    ROOT = Path(__file__).resolve().parents[2]
    # deterministic filter branches (no subprocess) — invariant 2
    assert triage_packet(ROOT, "/status --html") == ""
    assert triage_packet(ROOT, "!doc-find gates and quality") == ""
    assert triage_packet(ROOT, "hi there") == ""            # shorter than minPromptChars
    assert triage_packet(ROOT, "") == ""
    assert _terms("Add a NEW settings page settings validation") == ["settings", "new", "page", "validation"]
    assert _config(ROOT)["silentProfiles"]  # config read or defaults, never crashes
    # invariant 1: a failing classifier lookup collapses to silence, no raise
    _real = _classify_profile
    globals()["_classify_profile"] = lambda r, p: (_ for _ in ()).throw(RuntimeError("boom"))
    assert triage_packet(ROOT, "add a full user profile settings page with save and validation") == ""
    # non-feature profile -> silence (dirty-tree quirk aside, scan is always silent)
    globals()["_classify_profile"] = lambda r, p: "scan"
    assert triage_packet(ROOT, "run the smoke gate and all scenarios to confirm green") == ""
    globals()["_classify_profile"] = _real
    # real end-to-end: never raises, and a feature prompt yields a well-formed packet
    pkt = triage_packet(ROOT, "add a full user profile settings page with a save button and field validation")
    assert isinstance(pkt, str)
    assert pkt == "" or (pkt.startswith("[intake-triage] profile: ")
                         and "three exits" in pkt and len(pkt.splitlines()) <= 10), pkt
    print("intake_triage self-check OK")
