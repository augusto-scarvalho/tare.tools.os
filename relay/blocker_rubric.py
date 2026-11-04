"""Shared bounded-audit blocker instruction; not a verdict policy."""

BLOCKER_RUBRIC = (
    "Block only for a material violation of a frozen obligation on this exact candidate; "
    "cite candidate evidence, impact and the smallest sufficient falsifier or remedy. "
    "A security invariant or in-threat-model scenario suffices without a working exploit. "
    "An out-of-scope new rule is a residual, not a blocker. "
    "Missing evidence is inconclusive, never acceptance. "
)
