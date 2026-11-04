#!/usr/bin/env python3
"""CE.4 — the argparse tree as a demand-paged tool catalog.

An agent deciding WHICH harness verb to run does not need every verb's full
--help upfront — that is the tool-disclosure overhead the CE round measured
(G2). This helper walks the REAL parser tree (the same construction main()
uses: cli_registry + the workflow tree) and exposes:

  * `catalog()` — one compact row per verb (name + first help sentence),
    workflow subverbs as `workflow <name>` rows: the names+short-descs page;
  * `full_help(verb)` — the complete --help text for ONE verb, on demand;
  * `measure()` — catalogChars vs the sum of every full help (the
    disclosure-overhead ratio, so the win is a number, not a claim).

Introspection only: nothing here registers, parses argv, or dispatches.
Registered as the `catalog` verb via cli_registry (which this module walks —
the catalog always lists itself). Self-check:
`python scripts/harness_lib/cli_catalog.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib.common import HarnessError  # noqa: E402


def _build_tree() -> tuple[argparse.ArgumentParser, Any, Any]:
    """The real parser tree, built exactly like harness.main() builds it."""
    import harness
    from harness_lib import cli_registry, cli_workflow_tree

    parser = argparse.ArgumentParser(prog="harness.py", add_help=False)
    sub = parser.add_subparsers()
    cli_registry.register(sub, harness)
    wp = sub.add_parser("workflow", help="Plan and reduce large-task workflow packets")
    wsub = wp.add_subparsers()
    cli_workflow_tree.register(wsub, harness)
    return parser, sub, wsub


def _short(text: str | None, cap: int = 120) -> str:
    first = (text or "").split(". ", 1)[0].split("\n", 1)[0].strip()
    return first[:cap]


def _rows(sub: Any, prefix: str = "") -> list[dict[str, str]]:
    helps = {a.dest: a.help for a in getattr(sub, "_choices_actions", [])}
    return [{"verb": f"{prefix}{name}", "help": _short(helps.get(name))}
            for name in sub.choices]


def catalog() -> list[dict[str, str]]:
    """The compact names+short-descs page: top-level verbs + workflow subverbs."""
    _parser, sub, wsub = _build_tree()
    rows = _rows(sub)
    rows.extend(_rows(wsub, prefix="workflow "))
    return rows


def full_help(verb: str) -> str:
    """The complete --help for one verb (demand-paged detail)."""
    _parser, sub, wsub = _build_tree()
    parts = verb.split(None, 1)
    if parts[0] == "workflow" and len(parts) == 2:
        target = wsub.choices.get(parts[1])
    else:
        target = sub.choices.get(parts[0]) if len(parts) == 1 else None
    if target is None:
        known = [r["verb"] for r in catalog()]
        raise HarnessError(f"unknown verb: {verb}\n"
                           f"fix: `catalog` lists all {len(known)} verbs")
    return target.format_help()


def measure() -> dict[str, Any]:
    """Disclosure overhead: the compact page vs every full help upfront."""
    rows = catalog()
    catalog_chars = sum(len(r["verb"]) + len(r["help"]) + 4 for r in rows)
    full_chars = sum(len(full_help(r["verb"])) for r in rows)
    return {"verbs": len(rows), "catalogChars": catalog_chars,
            "fullHelpChars": full_chars,
            "catalogShare": round(catalog_chars / full_chars, 4) if full_chars else None}


def cmd_catalog(args) -> int:
    from harness_lib import common

    verb = getattr(args, "verb", None)
    if verb:
        print(full_help(" ".join(verb) if isinstance(verb, list) else str(verb)))
        return 0
    rows = catalog()
    if common.agent_output_compact():
        common.emit(rows, tsv=True)  # TE.5: the agent-facing page stays tabular
        return 0
    if getattr(args, "json", False):
        common.emit({"catalog": rows, "measurement": measure()})
        return 0
    width = max(len(r["verb"]) for r in rows)
    for r in rows:
        print(f"{r['verb']:<{width}}  {r['help']}")
    m = measure()
    print(f"\n{m['verbs']} verbs; `catalog <verb>` pages in one full help "
          f"(this page = {m['catalogShare']:.0%} of all helps upfront)")
    return 0


def _demo() -> None:
    rows = catalog()
    verbs = {r["verb"] for r in rows}
    assert {"status", "records", "catalog", "workflow plan"} <= verbs, sorted(verbs)[:10]
    assert all(r["help"] or r["verb"] in verbs for r in rows)
    help_text = full_help("records")
    assert "usage:" in help_text and "--kind" in help_text
    wf_help = full_help("workflow plan")
    assert "usage:" in wf_help
    try:
        full_help("ghost-verb")
        raise AssertionError("unknown verb accepted")
    except HarnessError as exc:
        assert "unknown verb" in str(exc)
    m = measure()
    assert m["catalogChars"] < m["fullHelpChars"] and 0 < m["catalogShare"] < 1, m
    print(f"cli_catalog self-check: ok (catalog = {m['catalogShare']:.0%} of full helps)")


if __name__ == "__main__":
    _demo()
