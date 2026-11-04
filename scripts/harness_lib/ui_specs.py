#!/usr/bin/env python3
"""SPEC-134 — the specs snapshot: the SDD inventory at a glance, by scope dir.

A CLOSED read-only view over `specs/**/*.md`. `specs_snapshot(root)` reuses
`harness_lib.spec_index.collect` (the same parser the `spec-index` CLI verb and
the SPEC-116 conformance gate share — scope, title, `Scenario:[id]`s,
legacy-frozen) and adds an mtime + a gherkin-id COUNT per row, ordered so scopes
group (00-universal / 40-features / templates / …). No parsing is re-implemented
here; a spec with broken markdown simply degrades to an empty title / 0 ids.

`spec_file(root, rel)` serves ONE spec's full markdown body — the `/api/records`
discipline: refuse any path NOT present in the snapshot inventory (a closed set,
so traversal / free paths can't resolve) with `{"error": …}`, never a crash.
Specs are tracked public docs, so no redaction — but the closed-set rule is
absolute.

Consumed by `GET /api/specs` + `/api/specs/file` and the Specs panel view; CLI
parity = the top-level `spec-index` verb (same `collect` source). Self-check:
`python scripts/harness_lib/ui_specs.py`.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import spec_index  # noqa: E402


def specs_snapshot(root: Path | str) -> dict[str, Any]:
    """Every spec as {id: rel-path, scope, title, gherkinIds: count, legacyFrozen,
    mtime}, ordered by (scope, id) so scope groups are contiguous and stable."""
    root = Path(root)
    specs_dir = root / "specs"
    rows: list[dict[str, Any]] = []
    try:
        inventory = spec_index.collect(root)
    except Exception as exc:  # never a 500 — degrade the whole view calmly
        return {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "specs": [], "error": f"{type(exc).__name__}"}
    for r in inventory:
        try:
            mtime = dt.datetime.fromtimestamp(
                (specs_dir / r["path"]).stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:  # per-row degrade — a stat that fails is not a crash
            mtime = None
        rows.append({"id": r["path"], "scope": r["scope"], "title": r["title"],
                     "gherkinIds": len(r["scenarioIds"]), "legacyFrozen": r["legacyFrozen"],
                     "mtime": mtime})
    rows.sort(key=lambda x: (x["scope"], x["id"]))
    return {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "specs": rows}


def spec_file(root: Path | str, rel: str) -> dict[str, Any]:
    """Full markdown body of ONE spec — closed to the snapshot inventory."""
    root = Path(root)
    inventory = {r["id"] for r in specs_snapshot(root)["specs"]}
    if rel not in inventory:  # traversal / unknown / absolute all fall here
        return {"error": f"unknown spec: {rel}", "path": rel}
    path = root / "specs" / rel
    try:
        return {"path": rel, "body": path.read_text(encoding="utf-8", errors="ignore")}
    except OSError:
        return {"error": "spec not present on this machine", "path": rel}


def _demo() -> None:
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "specs" / "00-universal").mkdir(parents=True)
        (root / "specs" / "40-features").mkdir(parents=True)
        (root / ".harness").mkdir(parents=True)
        (root / "specs" / "00-universal" / "alpha.md").write_text(
            "# SPEC-900 — Alpha\nbody\n", encoding="utf-8")
        (root / "specs" / "40-features" / "beta.md").write_text(
            "# SPEC-901 — Beta\n```gherkin\nScenario: [x-1] one\nScenario: [x-2] two\n```\n",
            encoding="utf-8")
        (root / "specs" / "40-features" / "broken.md").write_text(
            "no heading, no gherkin, just prose\n", encoding="utf-8")  # parse-broken → calm degrade
        (root / ".harness" / "project.json").write_text(
            json.dumps({"specConformance": {"legacy": ["beta.md"]}}), encoding="utf-8")

        snap = specs_snapshot(root)
        by_id = {r["id"]: r for r in snap["specs"]}
        # grouped + stable: 00-universal sorts before 40-features
        assert [r["scope"] for r in snap["specs"]] == ["00-universal", "40-features", "40-features"], snap
        assert by_id["40-features/beta.md"]["gherkinIds"] == 2, by_id
        assert by_id["40-features/beta.md"]["legacyFrozen"] is True, by_id
        assert by_id["00-universal/alpha.md"]["title"].endswith("Alpha"), by_id  # full H1 kept
        # parse-broken degrades calm: still listed, empty title, 0 ids, no crash
        assert by_id["40-features/broken.md"]["title"] == "" and by_id["40-features/broken.md"]["gherkinIds"] == 0, by_id

        # closed-set full view: a real inventory path serves its body …
        got = spec_file(root, "40-features/beta.md")
        assert "Scenario: [x-1]" in got["body"] and "error" not in got, got
        # … traversal / unknown / absolute all refuse (never leave the inventory)
        assert "error" in spec_file(root, "../../etc/passwd"), "traversal must refuse"
        assert "error" in spec_file(root, "40-features/ghost.md"), "unknown must refuse"
        assert "error" in spec_file(root, str(root / "specs" / "00-universal" / "alpha.md")), "absolute must refuse"
    print("ui_specs self-check: ok")


if __name__ == "__main__":
    _demo()
