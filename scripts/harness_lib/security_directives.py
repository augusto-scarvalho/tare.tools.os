#!/usr/bin/env python3
"""security-directive-map (Phase 3) — no prose-only security directive merges
silently.

The six security universal specs stay the single source of DIRECTIVES; this
module derives a stable id per `## Invariants` bullet
(`<stem>:<sha1(normalized text)[:8]>`) and checks every id against the
tracked mapping file `testing/security-directive-map.json`, whose entries
declare — explicitly and diff-reviewably — how each directive is enforced:

  status "check:<gate-check-name>"  a deterministic check covers it (may be
                                    annotated `(partial)` — honest labeling);
  status "trigger:<mechanism>"      a review trigger covers it (evadable but
                                    named);
  status "prose"                    accepted UNENFORCED — visible debt, never
                                    a silent gap.

The gate check `security-directive-map` FAILS when a directive has no mapping
(a new/edited directive forces an explicit enforcement decision in the same
commit) or when a mapping is stale (its directive no longer exists — remove
the entry with the directive). Mapping-file-only: the six specs are NEVER
edited (the roadmap's recommended answer to its own open decision).

Self-check: `python scripts/harness_lib/security_directives.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))

SECURITY_SPECS = (
    "specs/00-universal/secure-engineering.md",
    "specs/00-universal/api-and-interface-security.md",
    "specs/00-universal/dependency-and-supply-chain.md",
    "specs/00-universal/secrets-and-configuration.md",
    "specs/00-universal/data-protection-and-privacy.md",
    "specs/00-universal/ai-agent-safety.md",
)
MAP_REL = "testing/security-directive-map.json"
_STATUS_RE = re.compile(r"^(check:.+|trigger:.+|prose)$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def directive_id(stem: str, text: str) -> str:
    return f"{stem}:{hashlib.sha1(_norm(text).encode('utf-8')).hexdigest()[:8]}"


def directives(root: Path | str) -> list[dict[str, str]]:
    """One row per `## Invariants` bullet across the six security specs."""
    root = Path(root)
    out: list[dict[str, str]] = []
    for rel in SECURITY_SPECS:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"^##\s+Invariants\s*$", text, re.MULTILINE)
        if not m:
            continue
        tail = text[m.end():]
        nxt = re.search(r"^##\s", tail, re.MULTILINE)
        block = tail[: nxt.start()] if nxt else tail
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("- ") and len(line) > 4:
                bullet = line[2:].strip()
                out.append({"id": directive_id(path.stem, bullet),
                            "file": rel, "text": bullet})
    return out


def load_map(root: Path | str) -> dict[str, Any]:
    try:
        doc = json.loads((Path(root) / MAP_REL).read_text(encoding="utf-8"))
        return doc.get("map") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def evaluate(root: Path | str) -> dict[str, Any]:
    """{count, unmapped, stale, invalid, byStatus} — the gate consumes this."""
    root = Path(root)
    rows = directives(root)
    mapping = load_map(root)
    ids = {r["id"] for r in rows}
    unmapped = [r for r in rows if r["id"] not in mapping]
    stale = sorted(set(mapping) - ids)
    invalid = sorted(i for i, entry in mapping.items()
                     if not (isinstance(entry, dict)
                             and _STATUS_RE.match(str(entry.get("status") or ""))))
    by_status: dict[str, int] = {}
    for i, entry in mapping.items():
        if i in ids and isinstance(entry, dict):
            kind = str(entry.get("status") or "?").split(":", 1)[0]
            by_status[kind] = by_status.get(kind, 0) + 1
    return {"count": len(rows), "unmapped": unmapped, "stale": stale,
            "invalid": invalid, "byStatus": by_status}


def seed_entries(root: Path | str) -> dict[str, dict[str, str]]:
    """Skeleton entries for every unmapped directive (status left `prose` for
    the human to upgrade) — the helper `python security_directives.py --seed`
    prints them; nothing is written automatically."""
    report = evaluate(root)
    return {r["id"]: {"text": r["text"][:100], "status": "prose"}
            for r in report["unmapped"]}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec_dir = root / "specs" / "00-universal"
        spec_dir.mkdir(parents=True)
        (spec_dir / "secure-engineering.md").write_text(
            "# X\n\n## Invariants\n\n- Directive one.\n- Directive two.\n\n"
            "## Agent behavior\n\n- not a directive\n", encoding="utf-8")
        rows = directives(root)
        assert len(rows) == 2 and all(r["id"].startswith("secure-engineering:") for r in rows)
        assert directive_id("s", "  A   b\n") == directive_id("s", "a B")  # normalized
        report = evaluate(root)
        assert len(report["unmapped"]) == 2 and report["stale"] == []
        (root / "testing").mkdir()
        (root / MAP_REL).write_text(json.dumps({"map": {
            rows[0]["id"]: {"text": "d1", "status": "check:security-baseline"},
            rows[1]["id"]: {"text": "d2", "status": "prose"},
            "ghost:00000000": {"text": "gone", "status": "prose"},
            }}), encoding="utf-8")
        report = evaluate(root)
        assert report["unmapped"] == [] and report["stale"] == ["ghost:00000000"]
        assert report["byStatus"] == {"check": 1, "prose": 1}, report
        (root / MAP_REL).write_text(json.dumps({"map": {
            rows[0]["id"]: {"status": "banana"}}}), encoding="utf-8")
        assert evaluate(root)["invalid"] == [rows[0]["id"]]
    print("security_directives self-check: ok")


if __name__ == "__main__":
    if "--seed" in sys.argv:
        repo = Path(__file__).resolve().parents[2]
        print(json.dumps(seed_entries(repo), indent=2, ensure_ascii=False))
    else:
        _demo()
