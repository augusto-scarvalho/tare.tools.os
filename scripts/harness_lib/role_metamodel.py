#!/usr/bin/env python3
"""Unified role metamodel (SPEC-173 Phase 1, rules 6-9).

One document — `.harness/routing/role-metamodel.json` — subsumes BOTH taxonomies:
the 32 `playbook-registry.json` roles and the 12 `task-profiles.json` profiles.
The join key is NAME IDENTITY (every profile name already exists as a role name),
so no bridge table is created; `generate()` REFUSES a profile with no matching
role instead of inventing one (rule 6).

The artifact is GENERATED, never hand-edited: `--generate` writes it from the two
legacy files, `--check` regenerates in memory and reports drift. The legacy files
stay on disk byte-identical as frozen projections (`registry_view` /
`profiles_view`) for the ~10 out-of-scope readers until the Phase 3/4 repoint —
machine-enforced by `check()`, so they cannot drift silently.

`jsonschema` is NOT a repo dependency, so validation is plain-code checks in
`validate()`; stdlib only. This module NEVER imports `playbook_registry` at module
level — the import direction is one-way (playbook_registry -> role_metamodel).
Self-check: python scripts/harness_lib/role_metamodel.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib.common import HarnessError, read_json, write_json  # noqa: E402

METAMODEL_REL = ".harness/routing/role-metamodel.json"
WARMUP_REL = ".harness/prompts/overseer-warmup.md"  # the one budgeted source today
REGISTRY_REL = ".harness/routing/playbook-registry.json"      # legacy view (frozen)
PROFILES_REL = ".harness/routing/task-profiles.json"          # legacy view (frozen)
SCHEMA_VERSION = 1
# The legacy task-profiles schemaVersion is a STRING and the metamodel document
# carries no field for it, so the projection restores this literal; a divergence
# is caught as drift by check(), never silently rewritten.
PROFILES_SCHEMA_VERSION = "1.0"
CLASSES = ("compiled", "explicitly-external")
RISKS = ("low", "medium", "high", "critical")
# SPEC-173 rule 15: the budgets the INJECTING code enforces, carried by the
# metamodel instead of living only in a scenario assert. `sessionStartBytes` is
# the SessionStart hook's aggregate ceiling (pe-1 pins it equal to the hook's
# TOTAL_BUDGET constant); `sources` are the SPEC-138 per-file numbers that were
# checked nowhere but osw_overseer_warmup.py until now.
BUDGETS: dict[str, Any] = {
    "sessionStartBytes": 9800,
    "sources": {
        WARMUP_REL: {"maxBytes": 3200, "maxLines": 40},
    },
}
INJECT_MODES = ("list", "compose")  # mirrors playbook_registry.INJECT_MODES (one-way import)

# Rule 9: every one of the 26 audited injection surfaces
# (docs/research/audit-playbook-injection-surfaces.md) carries a class + a reason.
SURFACES: dict[str, dict[str, str]] = {
    "S1": {"source": "AGENTS.md", "class": "compiled",
           "reason": "root of every chain (agent-base)"},
    "S2": {"source": "CLAUDE.md", "class": "explicitly-external",
           "reason": "vendor-ambient adapter shim, Claude-native load, never assembler-injected"},
    "S3": {"source": ".harness/prompts/overseer-warmup.md", "class": "compiled",
           "reason": "hook-injected discipline doc; role-scoping lands Phase 3 (rule 17)"},
    "S4": {"source": ".harness/prompts/overseer-playbook.md", "class": "compiled",
           "reason": "chain member (overseer)"},
    "S5": {"source": ".harness/prompts/overseer-loop-playbook.md", "class": "compiled",
           "reason": "chain member (loop-overseer)"},
    "S6": {"source": ".harness/prompts/research-playbook.md", "class": "compiled",
           "reason": "chain member (research)"},
    "S7": {"source": ".harness/prompts/subagent-contract.md", "class": "compiled",
           "reason": "chain member (worker + 20 worker-class roles)"},
    "S8": {"source": ".harness/prompts/testing-playbook.md", "class": "compiled",
           "reason": "chain member (implementer, review)"},
    "S9": {"source": ".harness/prompts/implementer-packet.md", "class": "compiled",
           "reason": "chain member (implementer)"},
    "S10": {"source": ".harness/prompts/backlog-groom-playbook.md", "class": "compiled",
            "reason": "chain member (groom-miner)"},
    "S11": {"source": ".harness/prompts/router-playbook.md", "class": "compiled",
            "reason": "chain member (router)"},
    "S12": {"source": ".harness/prompts/room-overseer.md", "class": "compiled",
            "reason": "chain member (room-overseer)"},
    "S13": {"source": ".harness/prompts/ui-overseer-playbook.md", "class": "compiled",
            "reason": "chain member (ui-overseer)"},
    "S14": {"source": ".harness/prompts/security-auditor-playbook.md", "class": "compiled",
            "reason": "chain member (security-auditor)"},
    # RE-HOMED, not deleted (D055/OQ4b-1). The Phase 0 audit called these dead
    # surfaces; the file:line evidence says otherwise, so rule 19's "deleted OR
    # re-homed" resolves to re-home and the files keep their `unmanaged` listing.
    "S15": {"source": ".harness/prompts/front-desk.md + .harness/prompts/harness-operator.md",
            "class": "explicitly-external",
            "reason": "live chat conversational prompts (chat_engines PROMPT_REL / "
                      "PROMPT_BY_ROLE, codex_appserver PROMPT_REL); never chain-injected"},
    "S16": {"source": ".harness/prompts/task/00-start-here.md, 01-execute-task.md, 02-review.md",
            "class": "explicitly-external",
            "reason": "on-demand task templates, never role-injected"},
    "S17": {"source": "canonical state files (context_checkpoint.REQUIRED_RELS)",
            "class": "explicitly-external",
            "reason": "session STATE, not doctrine; context_checkpoint owns caps"},
    "S18": {"source": "tools/hooks/acceptances_session_surface.py",
            "class": "explicitly-external",
            "reason": "dynamic state surface, hook-computed"},
    "S19": {"source": "tools/hooks/prompt_slots_session_surface.py",
            "class": "explicitly-external", "reason": "drift alarm, dynamic"},
    "S20": {"source": "tools/hooks/overseer_model_guard.py",
            "class": "explicitly-external", "reason": "dynamic guard output"},
    "S21": {"source": "tools/hooks/spec_intake_triage.py",
            "class": "explicitly-external", "reason": "per-turn conditional hook output"},
    "S22": {"source": "PreToolUse guard reminders (10 hooks)",
            "class": "explicitly-external", "reason": "dynamic tool-result text"},
    "S23": {"source": ".claude/agents/*.md (10 profiles)", "class": "explicitly-external",
            "reason": "vendor adapter spawn profiles (.claude/ is never canonical); "
                      "passage-level dedup deferred per spec Ceilings"},
    "S24": {"source": "build_prompt() composed packet", "class": "compiled",
            "reason": "assembler output; its profile inputs resolve through the metamodel (rule 7)"},
    "S25": {"source": "task-profiles.json tokenEconomy line", "class": "compiled",
            "reason": "rendered from the metamodel profile block after this phase"},
    "S26": {"source": "vendor SubagentStart persona", "class": "explicitly-external",
            "reason": "vendor-side, not repo-controlled (spec Applicability names it)"},
}


def generate(root: Path | str) -> dict[str, Any]:
    """The metamodel merged from the two legacy files — a pure function of them
    plus SURFACES (same inputs -> same document).

    Roles and `unmanaged` come from the registry verbatim; each profile block is
    embedded UNMODIFIED under `roles[<name>]["profile"]`. Typed refusal when a
    profile name has no matching role: name identity IS the join (rule 6)."""
    root = Path(root)
    registry = read_json(root / REGISTRY_REL, None)
    if not isinstance(registry, dict) or not isinstance(registry.get("roles"), dict):
        raise HarnessError(
            f"missing or malformed {REGISTRY_REL}\n"
            f"fix: restore the registry (an object with a 'roles' map)")
    profiles_doc = read_json(root / PROFILES_REL, None)
    if not isinstance(profiles_doc, dict) or not isinstance(profiles_doc.get("profiles"), dict):
        raise HarnessError(
            f"missing or malformed {PROFILES_REL}\n"
            f"fix: restore the task profiles (an object with a 'profiles' map)")
    roles: dict[str, Any] = {name: {k: v for k, v in role.items() if k != "profile"}
                             for name, role in registry["roles"].items()}
    for name, block in profiles_doc["profiles"].items():
        if name not in roles:
            raise HarnessError(
                f"task profile {name!r} has no role of the same name in {REGISTRY_REL}\n"
                f"fix: name identity is the join key — add the role or rename the profile")
        roles[name]["profile"] = block
    return {"schemaVersion": SCHEMA_VERSION,
            "defaultProfile": profiles_doc.get("defaultProfile"),
            "profilesDescription": profiles_doc.get("description"),
            "roles": roles,
            "unmanaged": list(registry.get("unmanaged") or []),
            "surfaces": {key: dict(entry) for key, entry in SURFACES.items()},
            "budgets": {"sessionStartBytes": BUDGETS["sessionStartBytes"],
                        "sources": {rel: dict(entry)
                                    for rel, entry in BUDGETS["sources"].items()}}}


def _has_cycle(roles: dict[str, Any], name: str) -> bool:
    """True when walking `extends` from `name` revisits a role (bounded by the
    role count — `seen` grows every step)."""
    seen: set[str] = set()
    cur: Any = name
    while isinstance(cur, str) and cur in roles:
        if cur in seen:
            return True
        seen.add(cur)
        cur = roles[cur].get("extends") if isinstance(roles[cur], dict) else None
    return False


def validate(doc: dict[str, Any]) -> list[str]:
    """Problems with `doc`; [] means valid. Plain-code checks (no jsonschema)."""
    roles = doc.get("roles")
    if not isinstance(roles, dict):
        return ["roles: not an object"]
    problems: list[str] = []
    for name, role in roles.items():
        if not isinstance(role, dict):
            problems.append(f"roles.{name}: not an object")
            continue
        parent = role.get("extends")
        if parent is not None and parent not in roles:
            problems.append(f"roles.{name}.extends: undefined role {parent!r}")
        elif _has_cycle(roles, name):
            problems.append(f"roles.{name}.extends: cycle")
        playbooks = role.get("playbooks", [])
        if not isinstance(playbooks, list) or not all(isinstance(p, str) for p in playbooks):
            problems.append(f"roles.{name}.playbooks: not a list of str")
        if role.get("inject") not in (None, *INJECT_MODES):
            problems.append(f"roles.{name}.inject: unknown mode {role.get('inject')!r}")
        profile = role.get("profile")
        if profile is None:
            continue
        if not isinstance(profile, dict):
            problems.append(f"roles.{name}.profile: not an object")
        elif profile.get("risk") not in RISKS:
            problems.append(f"roles.{name}.profile.risk: {profile.get('risk')!r} not in {RISKS}")
    bearers = {n for n, r in roles.items() if isinstance(r, dict) and "profile" in r}
    if doc.get("defaultProfile") not in bearers:
        problems.append(f"defaultProfile: {doc.get('defaultProfile')!r} names no profile-bearing role")
    surfaces = doc.get("surfaces")
    if not isinstance(surfaces, dict):
        problems.append("surfaces: not an object")
    else:
        missing = sorted(set(SURFACES) - set(surfaces))
        unknown = sorted(set(surfaces) - set(SURFACES))
        if missing:
            problems.append(f"surfaces: missing {missing}")
        if unknown:
            problems.append(f"surfaces: unknown {unknown}")
        for key in sorted(set(SURFACES) & set(surfaces)):
            entry = surfaces[key]
            if not isinstance(entry, dict):
                problems.append(f"surfaces.{key}: not an object")
            elif entry.get("class") not in CLASSES:
                problems.append(f"surfaces.{key}.class: {entry.get('class')!r} not in {CLASSES}")
            elif not str(entry.get("reason") or "").strip():
                problems.append(f"surfaces.{key}.reason: empty")
    unmanaged = doc.get("unmanaged", [])
    if not isinstance(unmanaged, list) or not all(isinstance(u, str) for u in unmanaged):
        problems.append("unmanaged: not a list of str")
    problems += _budget_problems(doc.get("budgets"))
    return problems


def _positive_int(value: Any) -> bool:
    """bool is an int subclass in Python; a budget of `True` is a typo, not a 1."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _budget_problems(budgets: Any) -> list[str]:
    """SPEC-173 rule 15 shape checks. `budgets` is OPTIONAL: absent means no
    enforcement, which is what every hermetic fixture (and pm-8's hand-written
    diverged doc) relies on to keep validating."""
    if budgets is None:
        return []
    if not isinstance(budgets, dict):
        return ["budgets: not an object"]
    problems: list[str] = []
    if not _positive_int(budgets.get("sessionStartBytes")):
        problems.append(f"budgets.sessionStartBytes: {budgets.get('sessionStartBytes')!r} "
                        f"is not a positive int")
    sources = budgets.get("sources")
    if not isinstance(sources, dict):
        problems.append("budgets.sources: not an object")
        return problems
    for rel, entry in sources.items():
        if not isinstance(entry, dict):
            problems.append(f"budgets.sources.{rel}: not an object")
            continue
        for key in ("maxBytes", "maxLines"):
            if not _positive_int(entry.get(key)):
                problems.append(f"budgets.sources.{rel}.{key}: {entry.get(key)!r} "
                                f"is not a positive int")
    return problems


def source_budget_violation(doc: dict[str, Any], rel: str, text: str) -> str | None:
    """SPEC-173 rule 16: ONE line naming the source, what it measures and the
    budget it blew — or None when `rel` carries no budget or fits.

    PURE (no I/O) so the two injecting paths — the SessionStart hook and
    spawn_compose — format the same verdict from the same code instead of each
    growing its own arithmetic. Exactly-at-budget is NOT a violation."""
    entry = ((doc or {}).get("budgets") or {}).get("sources", {})
    entry = entry.get(rel) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return None
    size, lines = len(text.encode("utf-8")), len(text.splitlines())
    if _positive_int(entry.get("maxBytes")) and size > entry["maxBytes"]:
        return f"{rel}: {size}B > maxBytes {entry['maxBytes']} (SPEC-173 rule 16)"
    if _positive_int(entry.get("maxLines")) and lines > entry["maxLines"]:
        return f"{rel}: {lines} lines > maxLines {entry['maxLines']} (SPEC-173 rule 16)"
    return None


def load(root: Path | str) -> dict[str, Any] | None:
    """The committed metamodel, or None when the file is absent (the caller falls
    back to the legacy files).

    Present-but-invalid is a TYPED refusal listing every problem — a malformed
    metamodel must never fall back silently, which would resurrect the
    two-taxonomy world invisibly."""
    path = Path(root) / METAMODEL_REL
    if not path.exists():
        return None
    fix = "fix: regenerate it (python scripts/harness_lib/role_metamodel.py --generate)"
    try:
        doc = read_json(path, None)
    except ValueError as exc:  # JSONDecodeError
        raise HarnessError(f"malformed {METAMODEL_REL}: {exc}\n{fix}") from exc
    problems = validate(doc) if isinstance(doc, dict) else ["document: not an object"]
    if problems:
        raise HarnessError(f"invalid {METAMODEL_REL}:\n  " + "\n  ".join(problems) + f"\n{fix}")
    return doc


def registry_view(doc: dict[str, Any]) -> dict[str, Any]:
    """The legacy `playbook-registry.json` shape projected out of the metamodel:
    the roles minus their `profile` block, plus `unmanaged`."""
    return {"schemaVersion": doc.get("schemaVersion", SCHEMA_VERSION),
            "roles": {name: {k: v for k, v in role.items() if k != "profile"}
                      for name, role in (doc.get("roles") or {}).items()},
            "unmanaged": list(doc.get("unmanaged") or [])}


def profiles_view(doc: dict[str, Any]) -> dict[str, Any]:
    """The legacy `task-profiles.json` shape projected out of the metamodel: the
    profile block of every profile-bearing role, under the carried description +
    defaultProfile."""
    return {"schemaVersion": PROFILES_SCHEMA_VERSION,
            "description": doc.get("profilesDescription"),
            "defaultProfile": doc.get("defaultProfile"),
            "profiles": {name: role["profile"]
                         for name, role in (doc.get("roles") or {}).items()
                         if isinstance(role, dict) and "profile" in role}}


def check(root: Path | str) -> list[str]:
    """Drift between the committed metamodel, its two legacy projections and the
    committed legacy files. [] = in sync. Compares PARSED documents, never bytes
    (write_json key ordering is not a contract)."""
    root = Path(root)
    problems: list[str] = []
    generated = generate(root)
    committed = read_json(root / METAMODEL_REL, None)
    if not isinstance(committed, dict):
        problems.append(f"{METAMODEL_REL}: absent or not an object (run --generate)")
        committed = generated
    elif committed != generated:
        problems.append(f"{METAMODEL_REL}: drifted from the legacy sources (run --generate)")
    if read_json(root / REGISTRY_REL, None) != registry_view(committed):
        problems.append(f"{REGISTRY_REL}: differs from the metamodel registry projection")
    if read_json(root / PROFILES_REL, None) != profiles_view(committed):
        problems.append(f"{PROFILES_REL}: differs from the metamodel profiles projection")
    return problems


def _refuses(fn, needle: str) -> bool:
    try:
        fn()
        return False
    except HarnessError as exc:
        return needle in str(exc) and "fix:" in str(exc)


def _demo() -> None:
    """Hermetic proof over a temp root: generate -> validate -> both legacy views
    round-trip deep-equal -> check() green then red on drift -> every refusal
    (profile without role, extends cycle, bad surface class, missing surface key,
    invalid committed document). Deterministic, writes only under the temp root."""
    import copy
    import tempfile

    registry = {
        "schemaVersion": 1,
        "roles": {
            "agent-base": {"playbooks": ["AGENTS.md"]},
            "worker": {"extends": "agent-base", "playbooks": [".harness/prompts/sub.md"]},
            "plan": {"extends": "worker", "playbooks": []},
            "review": {"extends": "worker", "playbooks": [], "inject": "compose"},
        },
        "unmanaged": [".harness/prompts/unm.md"],
    }
    profiles = {
        "schemaVersion": PROFILES_SCHEMA_VERSION,
        "description": "fixture router",
        "defaultProfile": "plan",
        "profiles": {
            "plan": {"description": "p", "risk": "medium", "triggers": [], "fileGlobs": [], "spawn": {}},
            "review": {"description": "r", "risk": "high", "triggers": [], "fileGlobs": [], "spawn": {}},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / REGISTRY_REL, registry)
        write_json(root / PROFILES_REL, profiles)

        assert load(root) is None, "absent metamodel must be None, not a refusal"
        doc = generate(root)
        assert validate(doc) == [], validate(doc)
        assert len(doc["roles"]) == 4, doc["roles"]
        assert sorted(n for n, r in doc["roles"].items() if "profile" in r) == ["plan", "review"]
        # rule 6 migration honesty: legacy -> metamodel -> legacy is content-identical
        assert registry_view(doc) == registry, registry_view(doc)
        assert profiles_view(doc) == profiles, profiles_view(doc)
        assert generate(root) == doc, "generate must be deterministic"

        write_json(root / METAMODEL_REL, doc)
        assert check(root) == [], check(root)
        assert load(root) == doc

        # drift: a legacy edit without a regen is reported, never absorbed
        drifted = copy.deepcopy(registry)
        drifted["roles"]["docs"] = {"extends": "worker", "playbooks": []}
        write_json(root / REGISTRY_REL, drifted)
        assert len(check(root)) == 2, check(root)  # metamodel + registry projection
        write_json(root / REGISTRY_REL, registry)

        # a profile with no role of the same name is refused, not bridged
        write_json(root / PROFILES_REL,
                   {**profiles, "profiles": {**profiles["profiles"], "ghost": {"risk": "low"}}})
        assert _refuses(lambda: generate(root), "no role of the same name")
        write_json(root / PROFILES_REL, profiles)

        # a present-but-invalid committed document refuses instead of falling back
        write_json(root / METAMODEL_REL, {**doc, "defaultProfile": "nope"})
        assert _refuses(lambda: load(root), "names no profile-bearing role")
        write_json(root / METAMODEL_REL, doc)

    cyc = copy.deepcopy(doc)
    cyc["roles"]["agent-base"]["extends"] = "review"
    assert any("cycle" in p for p in validate(cyc)), validate(cyc)
    undef = copy.deepcopy(doc)
    undef["roles"]["worker"]["extends"] = "ghost"
    assert any("undefined role" in p for p in validate(undef)), validate(undef)
    bad_class = copy.deepcopy(doc)
    bad_class["surfaces"]["S2"]["class"] = "maybe"
    assert any("surfaces.S2.class" in p for p in validate(bad_class)), validate(bad_class)
    empty_reason = copy.deepcopy(doc)
    empty_reason["surfaces"]["S9"]["reason"] = "  "
    assert any("surfaces.S9.reason" in p for p in validate(empty_reason)), validate(empty_reason)
    missing = copy.deepcopy(doc)
    del missing["surfaces"]["S26"]
    assert any("missing ['S26']" in p for p in validate(missing)), validate(missing)
    extra = copy.deepcopy(doc)
    extra["surfaces"]["S27"] = {"source": "x", "class": "compiled", "reason": "x"}
    assert any("unknown ['S27']" in p for p in validate(extra)), validate(extra)
    bad_risk = copy.deepcopy(doc)
    bad_risk["roles"]["plan"]["profile"]["risk"] = "spicy"
    assert any("profile.risk" in p for p in validate(bad_risk)), validate(bad_risk)
    assert validate({"roles": []}) == ["roles: not an object"]

    # SPEC-173 rule 15: budgets are OPTIONAL but shape-checked when present
    assert doc["budgets"]["sessionStartBytes"] == BUDGETS["sessionStartBytes"]
    no_budgets = copy.deepcopy(doc)
    del no_budgets["budgets"]
    assert validate(no_budgets) == [], validate(no_budgets)  # absent = no enforcement
    for mutate, needle in (
            (lambda b: b.update(sessionStartBytes=0), "sessionStartBytes"),
            (lambda b: b.update(sessionStartBytes=True), "sessionStartBytes"),  # bool is not an int here
            (lambda b: b.update(sources=[]), "budgets.sources"),
            (lambda b: b["sources"][WARMUP_REL].update(maxBytes=0), "maxBytes"),
            (lambda b: b["sources"][WARMUP_REL].update(maxLines="40"), "maxLines")):
        broken = copy.deepcopy(doc)
        mutate(broken["budgets"])
        assert any(needle in p for p in validate(broken)), (needle, validate(broken))

    # the pure helper: fires over budget, silent under it and for an unbudgeted rel,
    # and the EXACT boundary is not a violation (spec edge case)
    assert source_budget_violation(doc, WARMUP_REL, "x" * 3201) is not None
    over = source_budget_violation(doc, WARMUP_REL, "x" * 4100)
    assert over and over.startswith(f"{WARMUP_REL}: 4100B > maxBytes 3200"), over
    assert source_budget_violation(doc, WARMUP_REL, "x" * 3200) is None  # exactly at maxBytes
    assert source_budget_violation(doc, WARMUP_REL, "\n".join("l" for _ in range(40))) is None
    lines_over = source_budget_violation(doc, WARMUP_REL, "\n".join("l" for _ in range(41)))
    assert lines_over and "41 lines > maxLines 40" in lines_over, lines_over
    assert source_budget_violation(doc, "AGENTS.md", "x" * 99999) is None  # no budget declared
    assert source_budget_violation(no_budgets, WARMUP_REL, "x" * 99999) is None
    print("role_metamodel self-check: ok")


if __name__ == "__main__":
    from harness_lib import common

    argv = sys.argv[1:]
    if "--generate" in argv:
        _doc = generate(common.ROOT)
        _problems = validate(_doc)
        if _problems:
            raise SystemExit("generated metamodel is invalid:\n  " + "\n  ".join(_problems))
        write_json(common.ROOT / METAMODEL_REL, _doc)
        print(f"{METAMODEL_REL}: {len(_doc['roles'])} roles, "
              f"{sum('profile' in r for r in _doc['roles'].values())} profiles, "
              f"{len(_doc['surfaces'])} surfaces")
    elif "--check" in argv:
        _problems = check(common.ROOT)
        for _p in _problems:
            print(_p)
        raise SystemExit(1 if _problems else 0)
    else:
        _demo()
