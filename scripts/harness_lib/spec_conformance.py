#!/usr/bin/env python3
"""SPEC-116 D — feature-spec template + Gherkin conformance (spec-pack logic).

`check_feature_specs(root, legacy)` returns gate-style check dicts
(`{"name","status","detail"}`, the baseline_catalog shape) for every
`specs/40-features/*.md` NOT in the frozen `legacy` set:

  - `:sections` — the template's required headings are present (SPEC-116 inv. 3);
  - `:gherkin`  — every `Scenario: [<id>]` inside a ```gherkin block resolves to
                  a `check("<id>"…)` string literal in a `testing/scenarios/*.py`
                  file the spec's **Validation** section references (inv. 4).

Gherkin is enforced only when a block EXISTS — the UI-required judgment stays
human (inv. 4); the gate enforces the mapping of scenarios that exist, not their
existence. Legacy-listed specs emit one `skip` (exempt until amended, inv. 3).
Pure stdlib, no runtime deps; `__main__` runs a self-check.
"""
from __future__ import annotations

import re
from pathlib import Path

# The heading phrases the template defines and this gate requires (inv. 3).
REQUIRED_SECTIONS = [
    "Goal",
    "Applicability",
    "Requirements / invariants",
    "Rationale & sources",
    "Test strategy",
    "Validation",
]

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
_GHERKIN = re.compile(r"```gherkin\b(.*?)```", re.DOTALL)
_SCENARIO_ID = re.compile(r"Scenario:\s*\[([^\]]+)\]")
_SCENARIO_REF = re.compile(r"testing/scenarios/([A-Za-z0-9_.-]+\.py)")


def _headings(text: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(text)]


def _missing_sections(text: str) -> list[str]:
    heads = [h.lower() for _lvl, h in _headings(text)]
    return [want for want in REQUIRED_SECTIONS
            if not any(want.lower() in h for h in heads)]


def _validation_body(text: str) -> str:
    """Text under the first heading starting with 'Validation', to the next
    heading of the same or higher level (or end of file)."""
    lines = text.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(2).strip().lower().startswith("validation"):
            start, level = i, len(m.group(1))
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1:]:
        m = _HEADING.match(line)
        if m and len(m.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def _gherkin_ids(text: str) -> list[str]:
    ids: list[str] = []
    for block in _GHERKIN.findall(text):
        ids.extend(_SCENARIO_ID.findall(block))
    return ids


def _referenced_files(root: Path, validation: str) -> list[Path]:
    seen: list[Path] = []
    for name in _SCENARIO_REF.findall(validation):
        path = root / "testing" / "scenarios" / name
        if path not in seen:
            seen.append(path)
    return seen


def _check(name: str, ok: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def check_feature_specs(root: Path | str, legacy: set[str]) -> list[dict[str, str]]:
    root = Path(root)
    feature_dir = root / "specs" / "40-features"
    out: list[dict[str, str]] = []
    if not feature_dir.is_dir():
        return out
    for spec in sorted(feature_dir.glob("*.md")):
        # Intake-refinement checklists (SPEC-116 door NEW) live beside the spec they
        # seed but are NOT specs — they follow specs/templates/intake-refinement.md,
        # so they never carry the spec template's sections. Skip them, don't fail them.
        if spec.name.endswith(".intake.md"):
            continue
        stem = spec.stem
        base = f"feature-spec-conformance:{stem}"
        if spec.name in legacy:
            out.append({"name": base, "status": "skip", "detail": "legacy (exempt until amended)"})
            continue
        text = spec.read_text(encoding="utf-8", errors="ignore")

        missing = _missing_sections(text)
        out.append(_check(f"{base}:sections", not missing,
                          "all required sections present" if not missing
                          else "missing section(s): " + ", ".join(missing)))

        ids = _gherkin_ids(text)
        if not ids:
            out.append(_check(f"{base}:gherkin", True, "no gherkin scenarios"))
            continue
        refs = _referenced_files(root, _validation_body(text))
        blobs = {p.name: (p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None)
                 for p in refs}
        orphans = [i for i in ids if not any(t is not None and i in t for t in blobs.values())]
        searched = ", ".join(sorted(blobs)) or "(no testing/scenarios/*.py referenced in Validation)"
        out.append(_check(f"{base}:gherkin", not orphans,
                          f"{len(ids)} scenario id(s) resolve in {searched}" if not orphans
                          else f"orphan id(s) {', '.join(orphans)} — not a check() in {searched}"))
    return out


def _selfcheck() -> None:
    import tempfile

    good_sections = (
        "# SPEC-900 — Demo\n## Goal\ng\n## Applicability\na\n"
        "## Requirements / invariants (numbered, testable)\n1. r\n"
        "## Rationale & sources\n| d | s |\n## Test strategy\nt\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        feat = root / "specs" / "40-features"
        scen = root / "testing" / "scenarios"
        feat.mkdir(parents=True)
        scen.mkdir(parents=True)
        (scen / "sc_fixture.py").write_text('check("ui:demo-check", True)\n', encoding="utf-8")

        # conformant + resolving gherkin id
        (feat / "good.md").write_text(
            good_sections
            + "## Gherkin scenarios (UI surfaces only)\n```gherkin\n"
            "Scenario: [ui:demo-check] it works\n```\n"
            "## Validation\nsee testing/scenarios/sc_fixture.py\n",
            encoding="utf-8")
        # missing Test strategy
        (feat / "bad.md").write_text(
            good_sections.replace("## Test strategy\nt\n", "") + "## Validation\nv\n",
            encoding="utf-8")
        # gherkin id that resolves nowhere
        (feat / "orphan.md").write_text(
            good_sections + "```gherkin\nScenario: [ui:nope] x\n```\n"
            "## Validation\ntesting/scenarios/sc_fixture.py\n",
            encoding="utf-8")
        # broken but legacy-exempt
        (feat / "legacy.md").write_text("# broken\nno sections\n", encoding="utf-8")
        # non-UI: no gherkin block, conformant sections
        (feat / "plain.md").write_text(good_sections + "## Validation\nv\n", encoding="utf-8")
        # intake-refinement checklist: has none of the spec sections, must be skipped
        (feat / "demo.intake.md").write_text("# Intake\n## Request\nx\n", encoding="utf-8")

        res = {r["name"]: r for r in check_feature_specs(root, {"legacy.md"})}
        assert not any("demo.intake" in k for k in res), "intake files must be skipped: " + str(sorted(res))
        st = {k: v["status"] for k, v in res.items()}
        assert st["feature-spec-conformance:good:sections"] == "pass", st
        assert st["feature-spec-conformance:good:gherkin"] == "pass", st
        assert st["feature-spec-conformance:bad:sections"] == "fail", st
        assert "Test strategy" in res["feature-spec-conformance:bad:sections"]["detail"], res
        assert st["feature-spec-conformance:orphan:gherkin"] == "fail", st
        assert "ui:nope" in res["feature-spec-conformance:orphan:gherkin"]["detail"], res
        assert st["feature-spec-conformance:legacy"] == "skip", st
        assert st["feature-spec-conformance:plain:gherkin"] == "pass", st
    print("spec_conformance self-check: ok")


if __name__ == "__main__":
    _selfcheck()
