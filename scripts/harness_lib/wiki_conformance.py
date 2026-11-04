#!/usr/bin/env python3
"""SPEC-167 — wiki-spec-guard: docs/wiki conformance + repo-wide spec-ref guard.

`check_wiki(root)` returns gate-style check dicts (`{"name","status","detail"}`,
the spec_conformance shape) that the spec-pack gate consumes:

  - `wiki-conformance:<relpath>` — one per `docs/wiki/**.md` page (invariants 1-4):
      1. the summarize-and-link marker is present;
      2. every relative markdown link resolves;
      3. every `SPEC-###` textual ref is defined-or-whitelisted;
      4. the page is <= 80 lines (else fail with "summarize and link, don't inline").
  - `spec-ref-guard` — ONE repo-wide check (invariant 5): every `SPEC-###` ref under
    `specs/**` and `docs/**` resolves to a defined spec, is whitelisted, or is exempt.

A spec is "defined" if some file under `specs/` declares it via a `# SPEC-###`
heading or a `Status: SPEC-###` line (the SPEC-116 template shape). Pure stdlib,
no runtime deps; `__main__` runs a tempdir self-check in <1s. Every compute
function takes `root` — no module-level ROOT on the compute paths.
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE_LINE_CAP = 80
WIKI_MARKER = "<!-- wiki-sources: summarize-and-link"
# Frozen whitelist: refs that legitimately do not resolve to a spec definition.
# SPEC-105 = documented gap (the interactive panel shipped as SPEC-114); SPEC-122
# = the EN-default guard, tracked by name in gate-structure-checks, no own file.
SPEC_REF_WHITELIST = frozenset({"SPEC-105", "SPEC-122"})

_SPEC_REF = re.compile(r"SPEC-\d{3}(?!\d)")
_SPEC_DEF = re.compile(r"(?:^#{1,6}\s+SPEC-(\d{3})|Status:\s*SPEC-(\d{3}))(?!\d)", re.MULTILINE)
_MD_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _resolved(ref: str, defined: set[str]) -> bool:
    # SPEC-000 (template) and SPEC-9xx (self-check demo specs) are always exempt.
    return (ref in defined or ref in SPEC_REF_WHITELIST
            or ref == "SPEC-000" or ref.startswith("SPEC-9"))


def defined_specs(root: Path) -> set[str]:
    """Every SPEC-### declared under specs/ via a heading or a `Status:` line."""
    out: set[str] = set()
    specs = Path(root) / "specs"
    if not specs.is_dir():
        return out
    for md in specs.rglob("*.md"):
        for a, b in _SPEC_DEF.findall(md.read_text(encoding="utf-8", errors="ignore")):
            out.add("SPEC-" + (a or b))
    return out


def _bad_refs(text: str, defined: set[str]) -> list[str]:
    return sorted({r for r in _SPEC_REF.findall(text) if not _resolved(r, defined)})


def _bad_links(page: Path) -> list[str]:
    bad: list[str] = []
    for link in _MD_LINK.findall(page.read_text(encoding="utf-8", errors="ignore")):
        if link.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        target = link.split("#", 1)[0].strip()
        if not target:
            continue
        if not (page.parent / target).resolve().exists():
            bad.append(link)
    return bad


def _page_check(page: Path, root: Path, defined: set[str]) -> dict[str, str]:
    rel = page.relative_to(root).as_posix()
    text = page.read_text(encoding="utf-8", errors="ignore")
    problems: list[str] = []
    if WIKI_MARKER not in text:
        problems.append(f"missing marker '{WIKI_MARKER} ...'")
    bad_links = _bad_links(page)
    if bad_links:
        problems.append("broken link(s): " + ", ".join(bad_links[:5]))
    bad_refs = _bad_refs(text, defined)
    if bad_refs:
        problems.append("undefined SPEC ref(s): " + ", ".join(bad_refs))
    n = len(text.splitlines())
    if n > PAGE_LINE_CAP:
        problems.append(f"{n} lines > {PAGE_LINE_CAP}-line cap — summarize and link, "
                        "don't inline canonical text")
    return {"name": f"wiki-conformance:{rel}",
            "status": "fail" if problems else "pass",
            "detail": "; ".join(problems) if problems
                      else f"marker, links, spec-refs, <={PAGE_LINE_CAP} lines ok"}


def _spec_ref_guard(root: Path, defined: set[str]) -> dict[str, str]:
    offenders: list[str] = []
    for base in ("specs", "docs"):
        d = root / base
        if not d.is_dir():
            continue
        for md in sorted(d.rglob("*.md")):
            bad = _bad_refs(md.read_text(encoding="utf-8", errors="ignore"), defined)
            if bad:
                offenders.append(f"{md.relative_to(root).as_posix()} -> {', '.join(bad)}")
    return {"name": "spec-ref-guard",
            "status": "fail" if offenders else "pass",
            "detail": ("; ".join(offenders[:10]) + (" ..." if len(offenders) > 10 else ""))
                      if offenders else
                      f"all SPEC-### refs in specs/** + docs/** resolve or are whitelisted "
                      f"({len(defined)} defined; whitelist {sorted(SPEC_REF_WHITELIST)})"}


def check_wiki(root: Path | str) -> list[dict[str, str]]:
    root = Path(root)
    defined = defined_specs(root)
    out: list[dict[str, str]] = []
    wiki = root / "docs" / "wiki"
    if wiki.is_dir():
        for page in sorted(wiki.rglob("*.md")):
            out.append(_page_check(page, root, defined))
    out.append(_spec_ref_guard(root, defined))
    return out


def _selfcheck() -> None:
    import tempfile

    marker = WIKI_MARKER + "; detail lives in the linked docs -->\n"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "specs" / "40-features").mkdir(parents=True)
        (root / "docs" / "wiki" / "sec").mkdir(parents=True)
        # A defined spec (heading) + one via Status line.
        (root / "specs" / "40-features" / "a.md").write_text("# SPEC-101 — A\nbody\n", encoding="utf-8")
        (root / "specs" / "40-features" / "b.md").write_text("# Flow\nStatus: SPEC-116, adopted\n", encoding="utf-8")
        w = root / "docs" / "wiki" / "sec"
        (w / "target.md").write_text("# Target\n" + marker, encoding="utf-8")

        (w / "good.md").write_text("# Good\nSee SPEC-101 and [t](target.md).\n" + marker, encoding="utf-8")
        (w / "nomarker.md").write_text("# NoMarker\nbody, no footer\n", encoding="utf-8")
        (w / "badlink.md").write_text("# BadLink\n[x](nope.md)\n" + marker, encoding="utf-8")
        (w / "deadref.md").write_text("# DeadRef\nSPEC-777 and SPEC-105 here.\n" + marker, encoding="utf-8")
        (w / "big.md").write_text("# Big\n" + "line\n" * 90 + marker, encoding="utf-8")

        res = {r["name"]: r for r in check_wiki(root)}
        st = {k: v["status"] for k, v in res.items()}
        assert st["wiki-conformance:docs/wiki/sec/good.md"] == "pass", res
        # wg-2: missing marker fails legibly
        r = res["wiki-conformance:docs/wiki/sec/nomarker.md"]
        assert r["status"] == "fail" and "missing marker" in r["detail"], r
        # wg-3: broken link fails naming it
        r = res["wiki-conformance:docs/wiki/sec/badlink.md"]
        assert r["status"] == "fail" and "nope.md" in r["detail"], r
        # wg-4: dead ref fails (SPEC-777), whitelisted (SPEC-105) does not offend
        r = res["wiki-conformance:docs/wiki/sec/deadref.md"]
        assert r["status"] == "fail" and "SPEC-777" in r["detail"] and "SPEC-105" not in r["detail"], r
        # wg-5: oversized page fails with the summarize-and-link detail
        r = res["wiki-conformance:docs/wiki/sec/big.md"]
        assert r["status"] == "fail" and "summarize and link" in r["detail"], r
        # inv-5: repo-wide guard fails while a dead ref lives, names the file
        assert st["spec-ref-guard"] == "fail", res["spec-ref-guard"]
        assert "deadref.md" in res["spec-ref-guard"]["detail"], res["spec-ref-guard"]

        # Clean the dead ref -> the whole guard + every page pass.
        (w / "deadref.md").write_text("# DeadRef\nSPEC-105 (whitelisted) and SPEC-116.\n" + marker, encoding="utf-8")
        (w / "nomarker.md").write_text("# NoMarker\n" + marker, encoding="utf-8")
        (w / "badlink.md").write_text("# BadLink\n[t](target.md)\n" + marker, encoding="utf-8")
        (w / "big.md").write_text("# Big\nshort\n" + marker, encoding="utf-8")
        res2 = {r["name"]: r for r in check_wiki(root)}
        assert all(v["status"] == "pass" for v in res2.values()), \
            [k for k, v in res2.items() if v["status"] != "pass"]
    print("wiki_conformance self-check: ok")


if __name__ == "__main__":
    _selfcheck()
