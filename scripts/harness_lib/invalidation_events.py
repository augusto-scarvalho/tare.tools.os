"""W29.N2 — the single invalidation-events enum: the one shared vocabulary.

Three taxonomies were drifting apart while naming the same thing — "the event
that voids previously accepted evidence":

  * gate-affected-cache (SPEC-159 §4): the cache-key components closure-hash
    and gate-version (a moved component = invalidation by key miss);
  * THEME1 safe-skip (WF-20260720-175712 reduce, CONCEPT-002): gate-version
    bump, graph rebuild, flaky scenario, key-scheme change;
  * W29 round (docs/research/w29-evidence-gated-assurance-round.md): test-edit
    post-execution (B1-4), model-swap / secret-rotation (NVIDIA F1-INCR-01
    trust boundary), rebase / dep-change (Brief 1).

This module FOLDS them into one canonical enum. Names only — no enforcement,
no I/O: producers keep observing what they already observe, and future
consumers (N3 review-gate freshness, N6 observe-only gate checks) import
these names instead of minting new strings. `observer` records the mechanism
that can SEE the event today (None = declared-only; the gap is the point of
observe-first, never papered over).

Coverage honesty: Proof-or-Stop (arXiv:2607.14890) rejects 18 tamper classes,
but the paper's full list is not reproduced in-repo — TAMPER_CLASSES maps only
the classes the W29 round doc NAMES to their covering event, asserted complete
in the self-check and never guessed beyond the sources. Self-check: run this
file.
"""
from __future__ import annotations

import re

# canonical event -> {desc, sources folded (citations), observer today or None}
EVENTS: dict[str, dict[str, object]] = {
    "gate-version": {
        "desc": "validator_version bumped — every cached scenario verdict is void",
        "sources": ("gate-affected-cache §4", "THEME1 CONCEPT-002"),
        "observer": "gate_affected.scenario_cache_key",
    },
    "closure-hash": {
        "desc": "a blob in the scenario's import/subprocess closure changed",
        "sources": ("gate-affected-cache §4", "W29 Brief 1 (dep change)"),
        "observer": "gate_affected.closure_hash",
    },
    "key-scheme": {
        "desc": "the cache-key derivation itself changed — old keys are not comparable",
        "sources": ("THEME1 CONCEPT-002",),
        "observer": None,
    },
    "graph-rebuild": {
        "desc": "the import graph was rebuilt/stale — accumulated closure evidence resets",
        "sources": ("THEME1 CONCEPT-002", "gate-affected-cache FB-1"),
        "observer": "gate_affected (FB-1 all-affected fallback)",
    },
    "flaky-scenario": {
        "desc": "a scenario was observed flaky — its cached verdicts stop counting",
        "sources": ("THEME1 CONCEPT-002", "W29 EXP-32"),
        "observer": None,
    },
    "test-edit-post-execution": {
        "desc": "a test/scenario file was edited AFTER the evidence it vouches for ran",
        "sources": ("W29 B1-4", "W29 Brief 1 (test edit)"),
        "observer": None,
    },
    "head-move": {
        "desc": "HEAD moved past the evidence sha (rebase/new commits)",
        "sources": ("W29 Brief 1 (rebase)",),
        "observer": "evidence_decay.assess (aging/stale)",
    },
    "model-swap": {
        "desc": "the served model changed between producing and consuming evidence",
        "sources": ("W29 NVIDIA F1-INCR-01", "W29 B1-6 (enforcement deferred)"),
        "observer": None,
    },
    "secret-rotation": {
        "desc": "a credential/secret the evidence depended on was rotated",
        "sources": ("W29 NVIDIA F1-INCR-01",),
        "observer": None,
    },
}

# Proof-or-Stop tamper classes NAMED by the round doc -> the covering event.
# ponytail: only the named subset; the paper's 18-class list is not in-repo.
TAMPER_CLASSES: dict[str, str] = {
    "rebase": "head-move",
    "dep-change": "closure-hash",
    "test-edit": "test-edit-post-execution",
    "model-swap": "model-swap",
    "secret-rotation": "secret-rotation",
}


def events() -> tuple[str, ...]:
    """All canonical event names, sorted (stable for records/telemetry)."""
    return tuple(sorted(EVENTS))


def is_event(name: str) -> bool:
    return name in EVENTS


def uncovered_tamper_classes() -> list[str]:
    """Named tamper classes whose covering event is missing (must stay [])."""
    return sorted(c for c, e in TAMPER_CLASSES.items() if e not in EVENTS)


def _demo() -> None:
    # the backlog row names these four as the minimum vocabulary
    for required in ("gate-version", "closure-hash",
                     "test-edit-post-execution", "model-swap"):
        assert is_event(required), required
    # THEME1's invalidation core is folded whole, never partially
    for theme1 in ("gate-version", "graph-rebuild", "flaky-scenario", "key-scheme"):
        assert is_event(theme1), theme1
    assert uncovered_tamper_classes() == [], uncovered_tamper_classes()
    kebab = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
    for name, meta in EVENTS.items():
        assert kebab.fullmatch(name), f"non-kebab event name: {name}"
        assert meta["desc"] and meta["sources"], f"unsourced event: {name}"
    assert events() == tuple(sorted(EVENTS)) and len(events()) == len(EVENTS)
    assert not is_event("made-up-event")
    print("invalidation_events self-check: ok")


if __name__ == "__main__":
    _demo()
