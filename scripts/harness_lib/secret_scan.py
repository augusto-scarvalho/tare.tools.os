#!/usr/bin/env python3
"""Deterministic secret-shape scan for workflow results (SPEC-119 v5 / E3-C6a).

Scans worker result / reduce output strings for structure-anchored secret shapes
(API keys, tokens, PEM headers) at the collect boundary. Every pattern is anchored
on a fixed prefix or structural marker, so a bare 64-hex sha256 digest stamp or a
git hash — legitimate in `context-digest.md` and evidence — is NEVER flagged.
Matches are redacted to first-4-chars + length; a secret's value is never echoed.

ponytail: anchored shapes only, no bare hex/base64 entropy detector — that is
exactly where sha256 stamps false-positive. Add an entropy blob detector (with a
context-digest/sha256 allowlist) only if a prefix-less secret format shows up.

Self-check: `python scripts/harness_lib/secret_scan.py`.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

# name -> pattern. Anchored on a fixed prefix/marker; bare hex/base64 never matches.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai-style-key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "nvidia-build-key": re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
    "google-api-key": re.compile(r"AIza[A-Za-z0-9_-]{35}"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "slack-token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer-token": re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "pem-private-key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
}


def redact(match: str) -> str:
    """First 4 chars + length. The full secret is never returned."""
    return f"{match[:4]}…(len={len(match)})"


def redact_text(text: str) -> str:
    """Rewrite every anchored secret shape in a text to its redacted marker —
    the server-side trust boundary the panel viewers (commits, memory) apply
    to EVERY byte before it leaves the process."""
    for pattern in _PATTERNS.values():
        text = pattern.sub(lambda m: redact(m.group(0)), text)
    return text


def _walk(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (json-path, string-leaf) for every string in a JSON-ish structure."""
    if isinstance(obj, str):
        yield path or "<root>", obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _walk(value, f"{path}[{i}]")


def scan(obj: Any) -> list[dict[str, str]]:
    """Return one hit per (location, pattern, redacted-match); never the raw secret.

    ``location`` is a JSON path (e.g. ``findings[0].evidence[1]``) so the collect
    boundary can name the finding index in a legible error.
    """
    hits: list[dict[str, str]] = []
    for location, text in _walk(obj):
        for name, pat in _PATTERNS.items():
            for match in pat.findall(text):
                hits.append({"pattern": name, "location": location, "redacted": redact(match)})
    return hits


def _demo() -> None:
    fake = "sk-FAKEKEYdonotleak0123456789abcdef"  # obvious fake, not a real secret
    hits = scan({"findings": [{"evidence": [fake, "https://example.com/paper",
                                            "scripts/harness.py:1467"]}]})
    assert len(hits) == 1, hits
    assert hits[0]["pattern"] == "openai-style-key"
    assert hits[0]["location"] == "findings[0].evidence[0]"
    assert fake not in hits[0]["redacted"] and hits[0]["redacted"].startswith("sk-F"), hits
    # false-positive guard: sha256/16-hex stamps, URLs, paths, prose must NOT trip.
    clean = {"digest": "a1b2c3d4e5f60718" * 4,            # 64-hex sha256
             "stamp": "0123456789abcdef",                  # 16-hex digest stamp
             "url": "https://example.com/x?t=abcdef123456",
             "path": "specs/40-features/research-skill.md",
             "prose": "the Bearer token scheme is common"}  # 'Bearer' w/o a long token
    assert scan(clean) == [], scan(clean)
    print("secret_scan self-check OK")


if __name__ == "__main__":
    _demo()
