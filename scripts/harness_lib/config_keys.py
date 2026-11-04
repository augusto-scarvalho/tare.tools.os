#!/usr/bin/env python3
"""Read-only, MASKED inventory of expected env/config keys: the `config` verb.

Presence-only: reports WHICH expected keys are set, never their values.
Masking is a trust boundary — an env value is always reduced to
first-2-chars + "…" + length; a full value prints only for a non-secret
`.harness/project.json` knob explicitly whitelisted (`plain=True`) in
CONFIG_KEYS, and status() masks env keys unconditionally whatever the flag
says. Phase 1 is read-only (`keys`, `list`, always exit 0); the `config-set`
write action is phase 2 and is NOT here. Registered via
cli_registry.register() with zero harness.py edits
(spec: specs/40-features/config-keys.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from harness_lib.common import ROOT, read_json

# Curated literal registry: (name, kind `env`|`project`, required, plain,
# description). Every entry is a key the harness ACTUALLY reads today —
# env keys from os.environ.get() call sites, project knobs from their
# .harness/project.json consumers. `plain=True` (full value shown) is only
# honored for `project` knobs; env keys are masked no matter what.
CONFIG_KEYS: list[tuple[str, str, bool, bool, str]] = [
    ("GEMINI_API_KEY", "env", True, False,
     "Gemini free-tier discovery/enrichment key (knowledgeGraph.llmAssisted.requiresEnv; discover, graph-build --gemini-doc)"),
    ("GOOGLE_API_KEY", "env", False, False,
     "fallback alias for GEMINI_API_KEY on the same call sites"),
    ("NVIDIA_API_KEY", "env", True, False,
     "NVIDIA Build free-credits discovery fallback key (knowledgeGraph.apiAssistedProviders.nvidia.requiresEnv)"),
    ("OPENAI_API_KEY", "env", False, False,
     "chat --engine openai default --api-key-env (optional for local servers)"),
    ("OPENAI_BASE_URL", "env", False, False,
     "chat --engine openai default endpoint"),
    ("AWS_BEARER_TOKEN_BEDROCK", "env", False, False,
     "Amazon Bedrock API bearer used by Round Table Aider fallbacks"),
    ("OPENROUTER_API_KEY", "env", False, False,
     "OpenRouter API key used by Round Table Aider transport fallbacks"),
    ("HARNESS_ALLOW_NETWORK_EXPORT", "env", False, False,
     "opt-in for workflow trace-push --send outside loopback"),
    ("knowledgeGraph.llmAssisted.enabled", "project", False, True,
     "Gemini text/image enrichment opt-in (discover, graph-build)"),
    ("knowledgeGraph.apiAssistedProviders.nvidia.enabled", "project", False, True,
     "NVIDIA Build discovery-chain opt-in (discover)"),
    ("workflows.workerEnvFilter", "project", False, True,
     "filter worker process env down to the allowlist (gate/worker spawn)"),
    ("workflows.budgets.tokenBudget.charsPerToken", "project", False, True,
     "token estimator canonical seed (token_calibration)"),
    ("coverage.enabled", "project", False, True,
     "coverage gate enforcement (spec_test_gate)"),
    ("intakeTriage.minPromptChars", "project", False, True,
     "SPEC-117 intake-triage prompt-length threshold"),
]


def _mask(value: str) -> str:
    """first2 + \"…\" + length. A short value gets NO prefix — leaking 2 of
    5 chars is not masking."""
    prefix = value[:2] if len(value) >= 6 else ""
    return f"{prefix}…{len(value)}"


def _project_value(project: dict, dotted: str) -> tuple[bool, object]:
    node: object = project
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def status(root: Path | str) -> list[dict]:
    """One row per registry entry: name, kind, required, set, display.
    Raw values NEVER appear: env displays are masked (or the value-free
    marker `in vault`); project displays print full only when the
    registry whitelists the knob as plain."""
    root = Path(root)
    # lazy: keys_vault imports THIS module, so a module-level import would cycle.
    from harness_lib import keys_vault
    project = read_json(root / ".harness" / "project.json", {}) or {}
    rows: list[dict] = []
    for name, kind, required, plain, _desc in CONFIG_KEYS:
        if kind == "env":
            value = os.environ.get(name) or ""
            # keys-keyring v2: the .env name-parse tier is gone; a key not in the
            # process env is `set` iff the OS vault holds it (presence only).
            is_set = bool(value) or keys_vault.vault_get(name) is not None
            display = _mask(value) if value else ("in vault" if is_set else "")
        else:
            found, value = _project_value(project, name)
            is_set = found and value is not None
            text = json.dumps(value, ensure_ascii=False) if is_set else ""
            display = (text if plain else _mask(text)) if is_set else ""
        rows.append({"name": name, "kind": kind, "required": required,
                     "set": is_set, "display": display})
    return rows


def cmd_config(args) -> int:
    """`harness.py config keys|list`: read-only, masked, always rc 0."""
    as_json = getattr(args, "json", False)
    if getattr(args, "action", "keys") == "list":
        entries = [{"name": n, "kind": k, "required": r, "plain": p, "description": d}
                   for n, k, r, p, d in CONFIG_KEYS]
        if as_json:
            print(json.dumps(entries, indent=2, ensure_ascii=False))
        else:
            for e in entries:
                flags = e["kind"] + (", required" if e["required"] else "")
                print(f"{e['name']}  [{flags}]  {e['description']}")
        return 0
    rows = status(ROOT)
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        mark = "set" if r["set"] else ("MISSING" if r["required"] else "unset")
        print(f"{r['name']:<{width}}  {r['kind']:<7}  {mark:<7}  {r['display']}")
    print(f"\n{sum(1 for r in rows if r['set'])}/{len(rows)} keys set")
    return 0
