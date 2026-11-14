"""Shared bounded-audit blocker instruction; not a verdict policy."""

BLOCKER_RUBRIC = (
    "Block only for a material violation of a frozen obligation on this exact candidate; "
    "cite candidate evidence, impact and the smallest sufficient falsifier or remedy. "
    "A security invariant or in-threat-model scenario suffices without a working exploit. "
    "An out-of-scope new rule is a residual, not a blocker. "
    # Owner, 2026-11-14: a cyclic audit drifts into Zeno's paradox and over-engineering (five review rounds on #162 moved
    # from realistic defects to hand-made configurations, and the fixes for those grew new mechanisms that needed fixing).
    # The conversation audit already said this (turn_engine.run_audit); now every auditor reads it.
    "So is a hypothetical or unlikely scenario, hardening beyond the stated threat model, a new feature, abstraction "
    "or test, or a style preference. "
    "When reviewing a fix, judge only whether the earlier findings were resolved and whether the fix introduced a "
    "defect or grew beyond them. "
    "Missing evidence is inconclusive, never acceptance. "
)
