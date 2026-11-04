"""SPEC-113: agent capability parity across vendor CLIs (Claude, Codex; kimi
skills/agents rendered since the KIMI-V1 follow-up shipped 2026-08-07 — kimi
hooks remain user-scope-only and are NEVER pair-rendered).

One canonical manifest (`.harness/capabilities.json`) declares the hooks, MCP
servers and skills every agent CLI should expose. `audit` reads each vendor's
real surface (never writes) and reports drift/missing/unportable gaps; `pair`
renders the vendor adapters back from the manifest — idempotent, root-scoped
writes only. Mirrors the render-from-canonical precedent in
`targets_lib.sync_adapter` and the shape/hash drift precedent in
`gate_generic.check_protected_instructions`; findings ride the existing
self-review funnel, no second funnel.

Every collector try/except-degrades to {}/[]: a missing or corrupt vendor file
must never sink the audit or the self-review loop it feeds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib  # py>=3.11
except ImportError:  # pragma: no cover - codex profiles/mcp just degrade to {}
    tomllib = None

try:
    from .common import read_json, read_text, write_json, write_text
    from .agent_parity_vendors import (  # noqa: F401 -- some names re-exported for callers
        CLAUDE_AGENTS_REL, CLAUDE_SETTINGS_REL, CODEX_CONFIG_REL, CODEX_HOOKS_REL,
        KIMI_AGENTS_REL, KIMI_SKILLS_REL,
        ANTIGRAVITY_AGENTS_REL, ANTIGRAVITY_HOOKS_REL, ANTIGRAVITY_SKILLS_REL,
        _claude_user_skills, _command_for, _lf, _normalize_settings_hooks, _script_of,
        antigravity_agent_profile, codex_agent_profile, codex_exec_call_params,
        collect_antigravity, collect_claude, collect_claude_agents, collect_codex,
        collect_codex_trust, collect_instruction_files, collect_kimi,
        kimi_agent_profile, kimi_skill_source_text, render_antigravity_agent_md,
        render_antigravity_hooks_json, validate_antigravity_hooks_schema,
        render_codex_agent_toml, render_kimi_agent_md)
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import read_json, read_text, write_json, write_text
    from harness_lib.agent_parity_vendors import (  # noqa: F401
        CLAUDE_AGENTS_REL, CLAUDE_SETTINGS_REL, CODEX_CONFIG_REL, CODEX_HOOKS_REL,
        KIMI_AGENTS_REL, KIMI_SKILLS_REL,
        ANTIGRAVITY_AGENTS_REL, ANTIGRAVITY_HOOKS_REL, ANTIGRAVITY_SKILLS_REL,
        _claude_user_skills, _command_for, _lf, _normalize_settings_hooks, _script_of,
        antigravity_agent_profile, codex_agent_profile, codex_exec_call_params,
        collect_antigravity, collect_claude, collect_claude_agents, collect_codex,
        collect_codex_trust, collect_instruction_files, collect_kimi,
        kimi_agent_profile, kimi_skill_source_text, render_antigravity_agent_md,
        render_antigravity_hooks_json, validate_antigravity_hooks_schema,
        render_codex_agent_toml, render_kimi_agent_md)


MANIFEST_REL = ".harness/capabilities.json"
CODEX_AGENTS_REL = ".codex/agents"
REPO_MCP_REL = "tools/agent-sync/mcp.manifest.json"
# Recorded once in the agents audit (item 3): Claude model tiers do not translate
# to codex model ids, so no `model` key is rendered — the executor route-tuple
# (C13) owns codex model choice at spawn time.
_AGENT_UNPORTABLE = ("model: Claude tier names (opus/haiku/fable/sonnet) do not map "
                     "to codex model ids; codex model choice is owned by the executor "
                     "route-tuple at spawn time (C13), not the profile toml")
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
# SPEC-113 §8.4 (LQ7-C3): declared capability support states. Additive — a
# capability with no `supportState` is treated as `native` (retro-compat).
_SUPPORT_STATES = ("native", "emulated", "degraded", "unsupported")


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest(root: Path) -> dict[str, Any]:
    """capabilities.json; corrupt/missing degrades to {}."""
    try:
        data = read_json(root / MANIFEST_REL, {}) or {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _find_capability(caps: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Locate a declared capability by identity: hook `id` (or its script) or a
    skill `name`. Used only by the supportState reader — cheap linear scan over a
    tiny manifest."""
    if not isinstance(caps, dict):
        return None
    for hook in caps.get("hooks") or []:
        if isinstance(hook, dict) and (hook.get("id") or hook.get("script")) == name:
            return hook
    for skill in caps.get("skills") or []:
        if isinstance(skill, dict) and skill.get("name") == name:
            return skill
    return None


def _resolve_state(raw: Any, vendor: str | None) -> str:
    """Normalize a raw `supportState` value to one of `_SUPPORT_STATES`.

    A dict is a per-vendor declaration (Q2: states may diverge per capability ×
    vendor); a scalar applies to every vendor. Unknown/absent -> `native`."""
    if isinstance(raw, dict):
        raw = raw.get(vendor, "native") if vendor is not None else "native"
    s = str(raw).strip().lower()
    return s if s in _SUPPORT_STATES else "native"


def capability_support_state(caps: dict[str, Any], name: str,
                             vendor: str | None = None) -> str:
    """SPEC-113 §8.4 supportState for a declared capability, default `native`.

    Semantics: native=direct; emulated=passes the same observable tests;
    degraded=changes one guarantee (not implicitly selectable); unsupported=out
    of the route set. Additive/retro-compat — a capability with no `supportState`
    is `native` (no re-render). Per Q2 the field may be a scalar (all vendors) or
    a per-vendor dict `{vendor: state}` when legs diverge (e.g. the gate-wait hook
    is native on claude, degraded on codex); pass `vendor` to resolve one leg.
    Consumed by `audit`; available to C16b (accounting-semantics) and R5 (3-lanes
    needs native-vs-emulated per vendor)."""
    entry = _find_capability(caps, name)
    return _resolve_state((entry or {}).get("supportState", "native"), vendor)


def _audit_agents(root: Path) -> dict[str, Any]:
    """Per canonical profile: present (rendered text == existing toml bytes) /
    drifted (exists, differs) / missing. Tomls not derived from any canonical
    profile are `extra` and NEVER touched (EXP-19 reader_a/b/c fixtures). Returns
    a matrix section plus the medium-severity gap rows (missing/drifted); extra
    is info-only and never a gap."""
    statuses: dict[str, str] = {}
    derived: set[str] = set()
    degraded: list[str] = []
    gaps: list[dict[str, Any]] = []
    for profile in collect_claude_agents(root):
        if profile.get("degraded"):
            degraded.append(str(profile.get("source") or profile.get("name")))
            continue
        cp = codex_agent_profile(profile)
        name = cp["name"]
        derived.add(name)
        target = root / CODEX_AGENTS_REL / f"{name}.toml"
        expected = render_codex_agent_toml(cp).encode("utf-8")
        try:
            status = ("present" if target.read_bytes() == expected else "drifted") \
                if target.is_file() else "missing"
        except OSError:
            status = "missing"
        statuses[name] = status
        if status != "present":
            gaps.append({
                "severity": "medium", "vendor": "codex", "capability": name,
                "kind": "agent", "status": status,
                "detail": (f"expected .codex/agents/{name}.toml rendered from "
                           f".claude/agents/{profile.get('source')}"
                           + ("" if status == "missing" else " (content drifted)")),
                "fix": "run python scripts/harness.py agents pair --apply "
                       "(renders codex agent profile tomls from .claude/agents/*.md)",
            })
    extra: list[str] = []
    try:
        extra = sorted(t.name for t in (root / CODEX_AGENTS_REL).glob("*.toml")
                       if t.stem not in derived)
    except OSError:
        pass
    return {"matrix": {"profiles": statuses, "extra": extra, "degraded": degraded,
                       "unportable": _AGENT_UNPORTABLE},
            "gaps": gaps}


def _audit_kimi_agents(root: Path) -> dict[str, Any]:
    """Kimi leg of agent parity (KIMI-V1 follow-up 1, shipped 2026-08-07): per
    claude-format TRANSITIONAL source profile (debt canonical-ir-vendor-
    adapters), the rendered `.kimi-code/agents/<name>.md` is
    present (bytes == render) / drifted / missing. Names pass through unchanged
    (kimi accepts hyphens). Files not derived from any source profile are
    `extra`, info-only and NEVER touched. NO model/effort is rendered — model
    choice is the executor route-tuple's at spawn time (same law as
    _AGENT_UNPORTABLE; kimi's model_preference is a primary/secondary binding,
    not a tier name)."""
    statuses: dict[str, str] = {}
    derived: set[str] = set()
    degraded: list[str] = []
    gaps: list[dict[str, Any]] = []
    for profile in collect_claude_agents(root):
        if profile.get("degraded"):
            degraded.append(str(profile.get("source") or profile.get("name")))
            continue
        kp = kimi_agent_profile(profile)
        name = kp["name"]
        derived.add(name)
        target = root / KIMI_AGENTS_REL / f"{name}.md"
        expected = render_kimi_agent_md(kp).encode("utf-8")
        try:
            status = ("present" if target.read_bytes() == expected else "drifted") \
                if target.is_file() else "missing"
        except OSError:
            status = "missing"
        statuses[name] = status
        if status != "present":
            gaps.append({
                "severity": "medium", "vendor": "kimi", "capability": name,
                "kind": "agent", "status": status,
                "detail": (f"expected {KIMI_AGENTS_REL}/{name}.md rendered from "
                           f".claude/agents/{profile.get('source')}"
                           + ("" if status == "missing" else " (content drifted)")),
                "fix": "run python scripts/harness.py agents pair --apply "
                       "(renders kimi agent markdown from .claude/agents/*.md)",
            })
    extra: list[str] = []
    try:
        extra = sorted(t.name for t in (root / KIMI_AGENTS_REL).glob("*.md")
                       if t.stem not in derived)
    except OSError:
        pass
    return {"matrix": {"profiles": statuses, "extra": extra, "degraded": degraded},
            "gaps": gaps}


def _audit_antigravity_agents(root: Path) -> dict[str, Any]:
    """Antigravity leg of agent parity (AGY-V1): per claude-format TRANSITIONAL
    source profile, rendered `.agents/agents/<name>.md` is present/drifted/missing."""
    statuses: dict[str, str] = {}
    derived: set[str] = set()
    degraded: list[str] = []
    gaps: list[dict[str, Any]] = []
    for profile in collect_claude_agents(root):
        if profile.get("degraded"):
            degraded.append(str(profile.get("source") or profile.get("name")))
            continue
        ap = antigravity_agent_profile(profile)
        name = ap["name"]
        derived.add(name)
        target = root / ANTIGRAVITY_AGENTS_REL / f"{name}.md"
        expected = render_antigravity_agent_md(ap).encode("utf-8")
        try:
            status = ("present" if target.read_bytes() == expected else "drifted") \
                if target.is_file() else "missing"
        except OSError:
            status = "missing"
        statuses[name] = status
        if status != "present":
            gaps.append({
                "severity": "medium", "vendor": "antigravity", "capability": name,
                "kind": "agent", "status": status,
                "detail": (f"expected {ANTIGRAVITY_AGENTS_REL}/{name}.md rendered from "
                           f".claude/agents/{profile.get('source')}"
                           + ("" if status == "missing" else " (content drifted)")),
                "fix": "run python scripts/harness.py agents pair --apply "
                       "(renders antigravity agent markdown from .claude/agents/*.md)",
            })
    extra: list[str] = []
    try:
        extra = sorted(t.name for t in (root / ANTIGRAVITY_AGENTS_REL).glob("*.md")
                       if t.stem not in derived)
    except OSError:
        pass
    return {"matrix": {"profiles": statuses, "extra": extra, "degraded": degraded},
            "gaps": gaps}



# --------------------------------------------------------------------------- #
# supportState (SPEC-113 §8.4 / LQ7-C3)
# --------------------------------------------------------------------------- #
def _support_state_matrix(manifest: dict[str, Any],
                          gaps: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Report `{capability: {vendor: state[, degradationReason]}}` for every
    declared hook/skill (native default surfaces here). Appends a medium
    `support-state` gap for any `unsupported` leg the manifest still wires for
    that vendor — that is a capability a route depends on but the vendor cannot
    support; degraded is never a gap."""
    declared: list[tuple[str, dict[str, Any], set[str]]] = []
    for hook in manifest.get("hooks", []) or []:
        if not isinstance(hook, dict):
            continue
        hid = str(hook.get("id") or hook.get("script") or "")
        wired = {v for v in ("claude", "codex")
                 if isinstance((hook.get("events") or {}).get(v), dict)}
        declared.append((hid, hook, wired))
    for skill in manifest.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue
        wired = {v for v in ("claude", "codex") if (skill.get("vendors") or {}).get(v)}
        declared.append((str(skill.get("name") or ""), skill, wired))

    out: dict[str, dict[str, str]] = {}
    for cid, entry, wired in declared:
        if not cid:
            continue
        raw = entry.get("supportState", "native")
        row: dict[str, str] = {}
        for vendor in ("claude", "codex"):
            state = _resolve_state(raw, vendor)
            row[vendor] = state
            if state == "unsupported" and vendor in wired:
                gaps.append({
                    "severity": "medium", "vendor": vendor, "capability": cid,
                    "kind": "support-state", "status": "unsupported",
                    "detail": f"{cid} is declared supportState=unsupported for {vendor} "
                              "but the manifest still wires it for that vendor (a route depends on it)",
                    "fix": "drop the vendor wiring or raise the supportState "
                           "(emulated/degraded) in .harness/capabilities.json",
                })
        reason = entry.get("degradationReason")
        if reason and any(row[v] != "native" for v in ("claude", "codex")):
            row["degradationReason"] = str(reason)
        out[cid] = row
    return out


# --------------------------------------------------------------------------- #
# audit (read-only)
# --------------------------------------------------------------------------- #
def _hook_status(hook: dict[str, Any], spec: dict[str, Any],
                 vendor_hooks: list[dict[str, Any]]) -> tuple[str, str]:
    script = hook.get("script")
    exp_event, exp_matcher = spec.get("event"), spec.get("matcher")
    want = f"{exp_event}/{exp_matcher or '*'}"
    matches = [vh for vh in vendor_hooks if vh.get("script") == script]
    if not matches:
        return "missing", f"expected {want}, hook not wired"
    for vh in matches:
        if vh.get("event") == exp_event and vh.get("matcher") == exp_matcher:
            return "present", want
    found = matches[0]
    return "drifted", f"expected {want}, found {found.get('event')}/{found.get('matcher') or '*'}"


def _skill_status(skill: dict[str, Any], vendor: str, root: Path,
                  claude: dict[str, Any]) -> tuple[str, str]:
    decl = (skill.get("vendors") or {}).get(vendor)
    name = skill.get("name")
    repo_scoped = str(skill.get("scope")) == "repo"
    # A repo-scoped skill (e.g. research) declares a real adapter path for EVERY
    # vendor incl. claude; only user-scoped claude skills live in the user profile.
    if vendor == "claude" and not repo_scoped:
        present = any(u.get("name") == name for u in claude.get("userSkills", []))
        return ("present", str(decl or "user skill")) if present else ("missing", "user-scope skill absent")
    if vendor == "kimi":
        # KIMI-V1 follow-up 1 (shipped 2026-08-07): the kimi adapter carries
        # canonical text fidelity to its source skill (near-claude format, NO
        # header — a codex-style header would corrupt the YAML frontmatter),
        # so parity is an LF-normalized text compare against the source.
        if not decl or "/" not in str(decl):
            return "unportable", "no repo adapter declared for this vendor"
        source = kimi_skill_source_text(skill, root, claude)
        if source is None:
            return ("unportable", f"canonical source for {decl} unreadable "
                    "(nearest equivalent: install/author the canonical skill, then run agents pair --apply)")
        target = root / str(decl)
        try:
            if not target.is_file():
                return "missing", f"no adapter {decl}"
            if _lf(target.read_text(encoding="utf-8")) == source:
                return "present", str(decl)
            return "drifted", f"adapter {decl} differs from its canonical source"
        except OSError:
            return "missing", f"no adapter {decl}"
    if decl and "/" in str(decl):  # repo adapter path (e.g. codex/prompts/ponytail.md, .claude/skills/research/SKILL.md)
        if (root / str(decl)).is_file():
            return "present", str(decl)
        return "unportable", f"no adapter {decl} (nearest equivalent: run agents pair --apply)"
    return "unportable", "no repo adapter declared for this vendor"


def audit(root: Path, home: Path | None = None) -> dict[str, Any]:
    home = home or Path.home()
    manifest = load_manifest(root)
    claude, codex = collect_claude(root, home), collect_codex(root, home)
    collected = {"claude": claude, "codex": codex}
    matrix: dict[str, Any] = {"claude": {"hooks": {}, "skills": {}}, "codex": {"hooks": {}, "skills": {}},
                              "kimi": {"hooks": {}, "skills": {}}}
    gaps: list[dict[str, Any]] = []

    for hook in manifest.get("hooks", []) or []:
        hid = hook.get("id") or hook.get("script")
        for vendor in ("claude", "codex"):
            spec = (hook.get("events") or {}).get(vendor)
            if not isinstance(spec, dict):
                continue
            status, detail = _hook_status(hook, spec, collected[vendor].get("hooks", []))
            matrix[vendor]["hooks"][hid] = status
            if status != "present":
                gaps.append({
                    "severity": "high" if status == "missing" else "medium",
                    "vendor": vendor, "capability": hid, "kind": "hook", "status": status,
                    "detail": detail,
                    "fix": "run python scripts/harness.py agents pair --apply "
                           f"(renders {vendor}'s hook wiring from .harness/capabilities.json)",
                })

    for skill in manifest.get("skills", []) or []:
        name = skill.get("name")
        # kimi leg is decl-gated (KIMI-V1): only a skill that declares a kimi
        # adapter path is audited for kimi, so a manifest with no kimi decls
        # never grows fabricated kimi gaps.
        vendors = ("claude", "codex", "kimi") if (skill.get("vendors") or {}).get("kimi") \
            else ("claude", "codex")
        for vendor in vendors:
            status, detail = _skill_status(skill, vendor, root, claude)
            matrix[vendor]["skills"][name] = status
            if status != "present":
                if vendor == "kimi":
                    fix = ("run python scripts/harness.py agents pair --apply "
                           "(renders the kimi adapter with canonical text fidelity to its source skill)")
                    if status == "unportable":
                        fix = ("install/author the canonical skill source, then run "
                               "python scripts/harness.py agents pair --apply")
                elif status == "missing":
                    fix = "install the user-scope skill"
                else:
                    fix = ("run python scripts/harness.py agents pair --apply "
                           "(renders a codex adapter from the user-scope SKILL.md, "
                           "or reports a manual fix if it is unreadable)")
                gaps.append({
                    "severity": "high" if status == "missing" else "medium",
                    "vendor": vendor, "capability": name, "kind": "skill", "status": status,
                    "detail": detail, "fix": fix,
                })

    manifest_mcp = set((manifest.get("mcpServers") or {}).keys())
    codex_mcp = set((codex.get("mcpServers") or {}).keys())
    repo_mcp = _repo_mcp_servers(root)
    matrix["mcpServers"] = {"manifest": sorted(manifest_mcp), "codex": sorted(codex_mcp),
                            "repoManifest": sorted(repo_mcp)}
    for name in sorted(manifest_mcp - codex_mcp):
        gaps.append({"severity": "medium", "vendor": "codex", "capability": name,
                     "kind": "mcp", "status": "missing",
                     "detail": "declared in capabilities.json but absent from .codex/config.toml [mcp_servers]",
                     "fix": "add the server to .codex/config.toml [mcp_servers]"})

    # KIMI-V1: the measured kimi contract rides the manifest verbatim (hooks are
    # user-scope-only on kimi, so there is no wiring to diff — the note IS the state).
    matrix["vendorNotes"] = manifest.get("vendorNotes") or {}

    # SPEC-113 bet E: codex agent-profile tomls rendered from .claude/agents/*.md.
    agents_section = _audit_agents(root)
    matrix["agents"] = agents_section["matrix"]
    gaps.extend(agents_section["gaps"])

    # KIMI-V1 follow-up 1 (shipped 2026-08-07): the kimi leg of agent parity
    # (.kimi-code/agents/*.md rendered from the claude-format TRANSITIONAL
    # compatibility source — debt canonical-ir-vendor-adapters). Decl-gated
    # like the skills leg: a manifest that never opted into kimi (no
    # vendorNotes.kimi, no vendors.kimi skill decl) grows NO kimi agent gaps.
    kimi_opted = bool((manifest.get("vendorNotes") or {}).get("kimi")) or any(
        isinstance(s, dict) and (s.get("vendors") or {}).get("kimi")
        for s in manifest.get("skills", []) or [])
    if kimi_opted:
        kimi_agents_section = _audit_kimi_agents(root)
        matrix["kimiAgents"] = kimi_agents_section["matrix"]
        gaps.extend(kimi_agents_section["gaps"])

    # SPEC-113 §8.4 (LQ7-C3): supportState per capability × vendor. Additive — a
    # capability with no declared state reports `native` (retro-compat). degraded
    # is an honest declaration and NEVER a gap; an `unsupported` leg the manifest
    # still wires (a route depends on it) is a real medium gap.
    matrix["supportState"] = _support_state_matrix(manifest, gaps)

    # T-ADAPTERCONF (§8.4) + C16b sections: conformance-module contract, report-only.
    matrix.update(audit_matrix_sections(root))

    # Machine-local, advisory-only: never inflates the high count, never fails CI.
    gaps.extend(collect_codex_trust(root, home))

    gaps.sort(key=lambda g: _SEVERITY_ORDER.get(g.get("severity"), 9))
    summary = {
        "gaps": len(gaps),
        "high": sum(1 for g in gaps if g.get("severity") == "high"),
        "medium": sum(1 for g in gaps if g.get("severity") == "medium"),
        "low": sum(1 for g in gaps if g.get("severity") == "low"),
        "manifestPresent": bool(manifest),
    }
    return {"matrix": matrix, "gaps": gaps, "summary": summary,
            "instructionFiles": collect_instruction_files(root)}


def _repo_mcp_servers(root: Path) -> set[str]:
    data = read_json(root / REPO_MCP_REL, {}) or {}
    if not isinstance(data, dict):
        return set()
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        return set(servers.keys())
    tools = data.get("tools")
    if isinstance(tools, list):
        return {str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")}
    return set()


# --------------------------------------------------------------------------- #
# pair (change plan; apply is idempotent + root-scoped)
# --------------------------------------------------------------------------- #
def _under_root(root: Path, rel: str) -> Path:
    """Structural safety: a parity write must resolve inside the repo root."""
    p = (root / rel).resolve()
    r = root.resolve()
    if not (p == r or str(p).startswith(str(r) + os.sep)):
        raise ValueError(f"parity write escapes repo root: {rel}")
    return p


def _is_protected(root: Path, rel: str) -> bool:
    try:
        from harness_lib import protected_files
        norm = rel.replace("\\", "/")
        return norm in {str(p).replace("\\", "/") for p in protected_files.protected_paths(root)}
    except Exception:  # noqa: BLE001
        return False


def _render_vendor_hooks(existing: dict[str, Any], manifest_hooks: list[dict[str, Any]],
                         vendor: str) -> dict[str, Any]:
    """Rebuild a vendor hooks block from the manifest. Non-managed entries are
    preserved; every managed script is re-placed at its canonical event so a
    stale placement (e.g. reload under PostCompact) is cleared, not duplicated.
    ponytail: an entry bundling a managed + non-managed hook would drop the
    sibling — our vendor files are one-hook-per-entry, so this stays simple."""
    managed = {h.get("script") for h in manifest_hooks}
    existing_hooks = existing.get("hooks") if isinstance(existing.get("hooks"), dict) else {}
    # Index the OLD inner-hook dicts by managed script: a re-render must be
    # loss-free for keys the manifest does not model (timeout, statusMessage, ... —
    # pair-renderer-metadata: a 2026-07-20 --apply silently dropped both).
    # Keyed by (script, event) FIRST — one script can be registered under two
    # legs at different events (this repo: subagent_gate_wait under SubagentStop
    # AND Stop; reckon 2026-07-21 Q5 reproduced the cross-leg collision) — with a
    # script-only fallback so a stale-placed hook (cleared to its canonical
    # event by this render) still carries its metadata over.
    old_inner: dict[Any, dict[str, Any]] = {}
    for event, entries in existing_hooks.items():
        for e in entries if isinstance(entries, list) else []:
            for hk in (e.get("hooks") or []) if isinstance(e, dict) else []:
                if isinstance(hk, dict) and _script_of(hk.get("command")) in managed:
                    old_inner.setdefault((_script_of(hk.get("command")), event), hk)
                    old_inner.setdefault(_script_of(hk.get("command")), hk)
    result: dict[str, list[Any]] = {}
    for event, entries in existing_hooks.items():
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _entry_uses_managed(e, managed)]
        if kept:
            result[event] = kept
    for hook in manifest_hooks:
        spec = (hook.get("events") or {}).get(vendor)
        if not isinstance(spec, dict):
            continue
        prev = (old_inner.get((hook.get("script"), spec.get("event")))
                or old_inner.get(hook.get("script")) or {})
        inner = {**{k: v for k, v in prev.items() if k not in ("type", "command")},
                 "type": "command", "command": _command_for(hook, vendor)}
        entry: dict[str, Any] = {"hooks": [inner]}
        if spec.get("matcher") is not None:
            entry = {"matcher": spec.get("matcher"), **entry}
        result.setdefault(spec.get("event"), []).append(entry)
    out = dict(existing)
    out["hooks"] = result
    return out


def _entry_uses_managed(entry: Any, managed: set) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(_script_of(hk.get("command")) in managed for hk in entry.get("hooks") or []
               if isinstance(hk, dict))


def _adapter_content(name: str, body: str) -> str:
    # No absolute source path in the body: it would be machine-specific and trip
    # release-hygiene (local-absolute-paths). The source is reported in the plan.
    return (f"# {name} (generated parity adapter)\n\n"
            f"Generated by `harness.py agents pair` from the user-scope Claude "
            f"`{name}` plugin skill so Codex reaches capability parity (SPEC-113). "
            f"Regenerate with `agents pair --apply`; do not hand-edit.\n\n---\n\n"
            + body.strip() + "\n")


def _user_skill_md(claude: dict[str, Any], name: str) -> str | None:
    for u in claude.get("userSkills", []):
        if u.get("name") == name:
            return u.get("path")
    return None


def pair(root: Path, home: Path | None = None, apply: bool = False) -> dict[str, Any]:
    home = home or Path.home()
    manifest = load_manifest(root)
    claude = collect_claude(root, home)
    plan: list[dict[str, Any]] = []

    # (a) hook wiring into both vendor files
    for vendor, rel in (("claude", CLAUDE_SETTINGS_REL), ("codex", CODEX_HOOKS_REL)):
        target = _under_root(root, rel)
        existing = read_json(target, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        rendered = _render_vendor_hooks(existing, manifest.get("hooks", []) or [], vendor)
        if rendered != existing:
            protected = _is_protected(root, rel)
            plan.append({"path": rel, "action": "update" if target.exists() else "create",
                         "summary": f"render {vendor} hook wiring from capabilities.json",
                         "protected": protected})
            if apply and not protected:
                write_json(target, rendered)
            elif apply and protected:
                plan[-1]["skipped"] = "protected file: apply via protect_canonical_files.py edit"

    # (b) skill adapters (unportable codex skills with a repo-adapter path)
    for skill in manifest.get("skills", []) or []:
        codex_decl = (skill.get("vendors") or {}).get("codex")
        if not codex_decl or "/" not in str(codex_decl):
            continue
        target = _under_root(root, str(codex_decl))
        if target.is_file():
            continue  # already present -> idempotent
        name = skill.get("name")
        skill_md = _user_skill_md(claude, name)
        body = read_text(Path(skill_md), "") if skill_md else ""
        if body.strip():
            plan.append({"path": str(codex_decl), "action": "create",
                         "summary": f"generate codex parity adapter for skill '{name}' from {skill_md}",
                         "protected": _is_protected(root, str(codex_decl))})
            if apply and not plan[-1]["protected"]:
                write_text(target, _adapter_content(name, body))
        else:
            plan.append({"path": str(codex_decl), "action": "manual",
                         "summary": f"skill '{name}' is unportable: user-scope SKILL.md unreadable/absent "
                                    "(nearest equivalent: install the skill or author the adapter by hand)",
                         "protected": False})

    # (b2) kimi skill adapters: canonical text fidelity (LF-normalized) to the
    # source skill (near-claude format — a codex-style header would corrupt
    # SKILL.md frontmatter). Unlike the codex leg this text-compares, so
    # content drift is repaired, not just absence. Idempotent: an up-to-date
    # adapter is a no-op.
    for skill in manifest.get("skills", []) or []:
        kimi_decl = (skill.get("vendors") or {}).get("kimi")
        if not kimi_decl or "/" not in str(kimi_decl):
            continue
        name = skill.get("name")
        source = kimi_skill_source_text(skill, root, claude)
        if source is None:
            plan.append({"path": str(kimi_decl), "action": "manual",
                         "summary": f"kimi adapter for '{name}' has no readable canonical source "
                                    "(install/author the canonical skill, then re-run pair)",
                         "protected": False})
            continue
        target = _under_root(root, str(kimi_decl))
        try:
            if target.is_file() and _lf(target.read_text(encoding="utf-8")) == source:
                continue  # present -> idempotent no-op
        except OSError:
            pass
        protected = _is_protected(root, str(kimi_decl))
        plan.append({"path": str(kimi_decl), "action": "update" if target.is_file() else "create",
                     "summary": f"render kimi skill adapter for '{name}' (canonical text fidelity to its source skill)",
                     "protected": protected})
        if apply and not protected:
            write_text(target, source)
        elif apply and protected:
            plan[-1]["skipped"] = "protected file: apply via protect_canonical_files.py edit"

    # (c) mcpServers render only when the manifest declares any (currently empty -> no-op)
    if manifest.get("mcpServers"):
        plan.append({"path": CODEX_CONFIG_REL, "action": "manual",
                     "summary": "capabilities.json declares mcpServers; wire them into "
                                ".codex/config.toml [mcp_servers] (rendering not yet automated)",
                     "protected": False})

    # (d) codex agent profile tomls rendered from .claude/agents/*.md (bet E).
    # .codex/agents/ is not protected; extra tomls (reader_a/b/c) are never touched.
    for profile in collect_claude_agents(root):
        if profile.get("degraded"):
            continue
        cp = codex_agent_profile(profile)
        rel = f"{CODEX_AGENTS_REL}/{cp['name']}.toml"
        target = _under_root(root, rel)
        expected = render_codex_agent_toml(cp)
        current = target.read_bytes() if target.is_file() else None
        if current == expected.encode("utf-8"):
            continue  # present -> idempotent no-op
        protected = _is_protected(root, rel)
        plan.append({"path": rel, "action": "update" if target.is_file() else "create",
                     "summary": f"render codex agent profile toml for '{profile.get('name')}' -> {cp['name']}",
                     "protected": protected})
        if apply and not protected:
            write_text(target, expected)
        elif apply and protected:
            plan[-1]["skipped"] = "protected file: apply via protect_canonical_files.py edit"

    # (d2) kimi agent markdown rendered from the same claude-format
    # TRANSITIONAL source (KIMI-V1 follow-up 1; debt canonical-ir-vendor-
    # adapters). Names pass through unchanged (kimi accepts hyphens); NO
    # model/effort is rendered — the route-tuple owns model choice at spawn.
    # .kimi-code/agents/ is not protected; extra .md files are never touched.
    # Decl-gated like the audit leg: a manifest that never opted into kimi gets
    # no .kimi-code tree from pair.
    kimi_opted = bool((manifest.get("vendorNotes") or {}).get("kimi")) or any(
        isinstance(s, dict) and (s.get("vendors") or {}).get("kimi")
        for s in manifest.get("skills", []) or [])
    if kimi_opted:
        for profile in collect_claude_agents(root):
            if profile.get("degraded"):
                continue
            kp = kimi_agent_profile(profile)
            rel = f"{KIMI_AGENTS_REL}/{kp['name']}.md"
            target = _under_root(root, rel)
            expected = render_kimi_agent_md(kp)
            current = target.read_bytes() if target.is_file() else None
            if current == expected.encode("utf-8"):
                continue  # present -> idempotent no-op
            protected = _is_protected(root, rel)
            plan.append({"path": rel, "action": "update" if target.is_file() else "create",
                         "summary": f"render kimi agent markdown for '{profile.get('name')}'",
                         "protected": protected})
            if apply and not protected:
                write_text(target, expected)
            elif apply and protected:
                plan[-1]["skipped"] = "protected file: apply via protect_canonical_files.py edit"

    antigravity_opted = True
    if antigravity_opted:
        rel_hooks = ANTIGRAVITY_HOOKS_REL
        target_hooks = _under_root(root, rel_hooks)
        expected_hooks = render_antigravity_hooks_json()
        current_hooks = target_hooks.read_text(encoding="utf-8") if target_hooks.is_file() else None
        if current_hooks != expected_hooks:
            plan.append({"path": rel_hooks, "action": "update" if target_hooks.is_file() else "create",
                         "summary": "render antigravity hooks JSON for .agents/hooks.json",
                         "protected": _is_protected(root, rel_hooks)})
            if apply and not plan[-1]["protected"]:
                write_text(target_hooks, expected_hooks)

        for profile in collect_claude_agents(root):
            if profile.get("degraded"):
                continue
            ap = antigravity_agent_profile(profile)
            rel = f"{ANTIGRAVITY_AGENTS_REL}/{ap['name']}.md"
            target = _under_root(root, rel)
            expected = render_antigravity_agent_md(ap)
            current = target.read_bytes() if target.is_file() else None
            if current == expected.encode("utf-8"):
                continue  # present -> idempotent no-op
            protected = _is_protected(root, rel)
            plan.append({"path": rel, "action": "update" if target.is_file() else "create",
                         "summary": f"render antigravity agent markdown for '{profile.get('name')}'",
                         "protected": protected})
            if apply and not protected:
                write_text(target, expected)
            elif apply and protected:
                plan[-1]["skipped"] = "protected file: apply via protect_canonical_files.py edit"

    return {"plan": plan, "applied": bool(apply),
            "changes": sum(1 for p in plan if p.get("action") != "manual")}



def vendor_trust_grant(root: Path, vendor: str = "codex") -> tuple[bool, str]:
    """SPEC-171: is this vendor's hook wiring provably OUR pipeline's committed
    output? The one question behind both trust decisions — codex's per-spawn
    `--dangerously-bypass-hook-trust` (R1) and claude's pre-seeded workspace
    trust (v2 R7). Hooks run BEFORE the codex sandbox is applied and the claude
    trust dialog guards the same class of surface, so a wrong grant executes
    repo-supplied commands as the user — this is the difference between
    trusting OUR pipeline and trusting whatever is on disk.

    FAIL-CLOSED on every error path — the inverse of this module's
    degrade-open collectors, on purpose: the audit observes, this arms an
    execution path. Three legs (SPEC-171 R1):
    (a) fixed point: the file re-renders to itself from capabilities.json;
    (b) every wired command's script is manifest-managed, read from RAW
        commands — the render preserves non-managed entries and the row
        normalizer drops .py-less commands, so (a) or a normalized scan would
        bless a hand-added entry (SPEC-171 R2);
    (c) both files clean vs HEAD: only gate+review+committed wiring is ours.
    """
    adapter_rel = CLAUDE_SETTINGS_REL if vendor == "claude" else CODEX_HOOKS_REL
    manifest = load_manifest(root)
    hooks = [h for h in manifest.get("hooks", []) or [] if isinstance(h, dict)]
    if not hooks:
        return False, f"deny: no hooks declared in {MANIFEST_REL}"
    try:
        existing = read_json(root / adapter_rel, None)
    except Exception:  # noqa: BLE001
        existing = None
    if not isinstance(existing, dict):
        return False, f"deny: {adapter_rel} missing/unreadable"
    if _render_vendor_hooks(existing, hooks, vendor) != existing:
        return False, f"deny: {adapter_rel} drifts from the {MANIFEST_REL} render"
    managed = {str(h.get("script")) for h in hooks}
    for entries in (existing.get("hooks") or {}).values():
        for entry in entries if isinstance(entries, list) else []:
            for hk in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                cmd = hk.get("command") if isinstance(hk, dict) else None
                if _script_of(cmd) not in managed:
                    return False, f"deny: non-manifest hook command wired: {str(cmd)[:80]!r}"
    try:
        from harness_lib import processes
        proc = processes.run_quiet(
            ["git", "diff", "--quiet", "HEAD", "--", MANIFEST_REL, adapter_rel],
            cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired, ImportError) as exc:
        return False, f"deny: git state unverifiable ({exc})"
    if proc.returncode != 0:
        return False, f"deny: {MANIFEST_REL} / {adapter_rel} not clean vs HEAD"
    return True, "grant: canonical render, all scripts manifest-managed, clean vs HEAD"


def codex_trust_grant(root: Path) -> tuple[bool, str]:
    """The codex leg of `vendor_trust_grant` (the spawn seam's entry point)."""
    return vendor_trust_grant(root, "codex")


# --------------------------------------------------------------------------- #
# claude workspace trust (SPEC-171 v2)
# --------------------------------------------------------------------------- #
CLAUDE_USER_STATE = ".claude.json"
_TRUST_KEY = "hasTrustDialogAccepted"


def _trust_path_forms(root: Path) -> list[str]:
    """Both separator spellings of the repo root.

    ~/.claude.json carries BOTH forms for the same project (measured
    2026-07-28: two PrintIntel entries, backslash and forward-slash); every
    recent entry uses forward slashes, so that is the live version's form and
    the backslash twin is legacy. Writing one form only would risk a silent
    no-op after a vendor change — the exact failure class this week was spent
    on — and both keys coexisting is already the file's observed norm.
    """
    resolved = str(root.resolve())
    return sorted({resolved.replace("\\", "/"), resolved.replace("/", "\\")})


def claude_trust_state(root: Path, home: Path | None = None) -> dict[str, Any]:
    """Report the claude workspace-trust state for this repo. Never writes.

    Harness-spawned claude lanes do NOT need this: `claude -p` skips the
    workspace trust dialog outright (`claude --help`, measured 2026-07-28).
    This is only about an INTERACTIVE session in a fresh clone/machine.
    """
    home = home or Path.home()
    state = home / CLAUDE_USER_STATE
    try:
        doc = read_json(state, None)
    except Exception:  # noqa: BLE001
        doc = None
    projects = (doc or {}).get("projects") if isinstance(doc, dict) else None
    forms = _trust_path_forms(root)
    current = {f: bool((projects or {}).get(f, {}).get(_TRUST_KEY))
               for f in forms if isinstance(projects, dict)}
    return {"statePath": str(state), "readable": isinstance(doc, dict),
            "paths": forms, "trusted": current,
            "alreadyTrusted": bool(current) and all(current.values())}


def claude_trust_apply(root: Path, home: Path | None = None,
                       apply: bool = False) -> dict[str, Any]:
    """Pre-seed `hasTrustDialogAccepted` for this repo in ~/.claude.json so a
    fresh interactive claude session needs no hand-accepted dialog (SPEC-171 v2
    R7-R9). Gated by `vendor_trust_grant(root, 'claude')`: the harness answers
    a security prompt on the operator's behalf ONLY for wiring it can prove is
    its own committed render. A denied grant writes NOTHING and leaves the
    dialog exactly as today — the fail-closed path degrades to the status quo,
    never to a blind grant.

    Writes through `write_json` (atomic temp + os.replace). Ceiling: a LIVE
    claude session holds this file's state in memory and may rewrite it, so run
    this at adoption/bootstrap time (the target case: a fresh clone with no
    session open yet); the call is idempotent, so re-running repairs a clobber.
    """
    home = home or Path.home()
    granted, why = vendor_trust_grant(root, "claude")
    state = claude_trust_state(root, home)
    out = {**state, "granted": granted, "reason": why, "applied": False, "wrote": []}
    if not granted:
        out["skipped"] = f"grant denied, nothing written — {why}"
        return out
    if not state["readable"]:
        out["skipped"] = f"{state['statePath']} missing/unreadable — open claude once to create it"
        return out
    path = Path(state["statePath"])
    doc = read_json(path, {}) or {}
    projects = doc.setdefault("projects", {})
    if not isinstance(projects, dict):
        out["skipped"] = "projects{} is not an object; refusing to rewrite it"
        return out
    wrote = [f for f in state["paths"]
             if not projects.get(f, {}).get(_TRUST_KEY)]
    out["wrote"] = wrote
    if not wrote:
        out["skipped"] = "already trusted for every path form"
        return out
    if apply:
        for f in wrote:
            entry = projects.get(f)
            projects[f] = {**entry, _TRUST_KEY: True} if isinstance(entry, dict) \
                else {_TRUST_KEY: True}
        write_json(path, doc)
        out["applied"] = True
    return out


def parity_gate_findings(root: Path, home: Path | None = None) -> list[tuple[str, str, str]]:
    """(name, status, detail) rows for the observe-only SPEC-113 gate check.

    Logic lives here (not inline in the gate) so spec_test_gate.py keeps shrinking
    per the modular-boundary invariant. status is 'pass' or 'skip' ONLY, never
    'fail': portability drift is repaired by `agents pair` and codex hook-trust is
    machine-local, so a fresh clone / CI must never go red on either.
    """
    try:
        result = audit(root, home)
    except Exception:  # noqa: BLE001 — observe-only never sinks the gate
        return [("agent-parity:audit", "skip", "OBSERVE-ONLY: audit unavailable")]
    gaps = result.get("gaps", [])
    support = result.get("matrix", {}).get("supportState", {})
    hook_gaps = [g for g in gaps if g.get("kind") == "hook"]
    agent_gaps = [g for g in gaps if g.get("kind") == "agent"]
    trust = [g for g in gaps if g.get("kind") == "trust"]
    support_gaps = [g for g in gaps if g.get("kind") == "support-state"]
    rows: list[tuple[str, str, str]] = []
    if hook_gaps:
        d = "; ".join(f"{g['vendor']}/{g['capability']}={g['status']}" for g in hook_gaps[:8])
        rows.append(("agent-parity:hooks", "skip",
                     f"OBSERVE-ONLY: {len(hook_gaps)} hook parity gap(s), run agents pair --apply — {d}"))
    else:
        rows.append(("agent-parity:hooks", "pass",
                     "manifest hooks match .claude/settings.json + .codex/hooks.json"))
    if agent_gaps:
        d = "; ".join(f"{g['capability']}={g['status']}" for g in agent_gaps[:8])
        rows.append(("agent-parity:agents", "skip",
                     f"OBSERVE-ONLY: {len(agent_gaps)} agent-profile parity gap(s), "
                     f"run agents pair --apply — {d}"))
    else:
        rows.append(("agent-parity:agents", "pass",
                     "codex agent profile tomls match .claude/agents/*.md (or none present)"))
    if trust:
        d = "; ".join(g["capability"] for g in trust)
        rows.append(("agent-parity:codex-trust", "skip",
                     f"OBSERVE-ONLY (machine-local): {d} — {trust[0].get('fix', '')}"))
    else:
        rows.append(("agent-parity:codex-trust", "pass",
                     "codex hook-trust current for this repo path (or codex absent)"))
    # SPEC-113 §8.4: degraded is an honest declaration, never a fail; only an
    # `unsupported` leg a route needs is a reportable gap.
    degraded = sorted(f"{cid}/{v}" for cid, row in (support or {}).items()
                      for v in ("claude", "codex") if row.get(v) == "degraded")
    if support_gaps:
        d = "; ".join(f"{g['vendor']}/{g['capability']}" for g in support_gaps[:8])
        rows.append(("agent-parity:support-state", "skip",
                     f"OBSERVE-ONLY: {len(support_gaps)} unsupported capability leg(s) a route needs — {d}"))
    elif degraded:
        rows.append(("agent-parity:support-state", "pass",
                     f"honest degraded declaration(s), not a gap: {', '.join(degraded)}"))
    else:
        rows.append(("agent-parity:support-state", "pass",
                     "all declared capabilities native/emulated on both vendors"))
    # KIMI-V1: one observe-only kimi row whenever the manifest declares the leg.
    if (result.get("matrix", {}).get("vendorNotes") or {}).get("kimi"):
        kimi_gaps = [g for g in gaps if g.get("vendor") == "kimi"]
        if kimi_gaps:
            rows.append(("agent-parity:kimi", "skip",
                         f"OBSERVE-ONLY: {len(kimi_gaps)} kimi adapter gap(s); hook wiring is "
                         "operator-owned user-scope, deny enforced when wired — SPEC-113 KIMI-V1"))
        else:
            rows.append(("agent-parity:kimi", "pass",
                         "kimi declared adapters present; hooks user-scope by vendor design "
                         "(SPEC-113 KIMI-V1)"))
    rows.append(gate_finding_row(result.get("matrix", {})))  # §8.4 row (observe-only)
    return rows


def parity_summary(root: Path, home: Path | None = None) -> dict[str, Any]:
    """Cheap counts for self-review; skipped entirely when the manifest is absent."""
    manifest = load_manifest(root)
    if not manifest:
        return {}
    try:
        result = audit(root, home)
    except Exception:  # noqa: BLE001
        return {}
    gaps = result.get("gaps", [])
    worst = gaps[0] if gaps else None
    return {
        "gaps": len(gaps),
        "highSeverityGaps": result.get("summary", {}).get("high", 0),
        "worstGap": (f"{worst['vendor']}/{worst['capability']} {worst['status']}" if worst else None),
    }


# --------------------------------------------------------------------------- #
# seed (agents audit --adopt)
# --------------------------------------------------------------------------- #
def _seed_manifest(root: Path) -> dict[str, Any]:
    """Derive a manifest from the live Claude wiring (the canonical baseline),
    mirroring each hook onto codex with the same event names + the ponytail
    skill declaration."""
    raw = read_json(root / CLAUDE_SETTINGS_REL, {})
    hooks: list[dict[str, Any]] = []
    for vh in _normalize_settings_hooks(raw):
        script = vh.get("script")
        args = _args_for_script(raw, script)
        ev = {"event": vh.get("event"), "matcher": vh.get("matcher")}
        entry: dict[str, Any] = {
            "id": Path(str(script)).stem.replace("_", "-"),
            "script": script,
            "events": {"claude": dict(ev), "codex": dict(ev)},
        }
        if args:
            entry["args"] = args
        hooks.append(entry)
    return {
        "schemaVersion": 1,
        "description": "SPEC-113 canonical agent-capability manifest (seeded from Claude wiring).",
        "hooks": hooks,
        "mcpServers": {},
        "skills": [{"name": "ponytail", "scope": "user", "required": True,
                    "vendors": {"claude": "user plugin/skill", "codex": "codex/prompts/ponytail.md"}}],
        "plugins": [],
        "instructionShims": {"note": "presence tracked via knownAgentInstructionFiles in protected-files.json"},
    }


def _args_for_script(raw: Any, script: str) -> list[str]:
    """Recover trailing non-path args (e.g. --check) for a script's command."""
    for event, entries in ((raw or {}).get("hooks") or {}).items() if isinstance(raw, dict) else []:
        for entry in entries if isinstance(entries, list) else []:
            for hk in entry.get("hooks") or [] if isinstance(entry, dict) else []:
                cmd = str(hk.get("command") or "")
                if _script_of(cmd) == script:
                    toks = cmd.split()
                    idx = next((i for i, t in enumerate(toks) if t.endswith(".py")), -1)
                    return toks[idx + 1:] if idx >= 0 else []
    return []


def adopt(root: Path) -> dict[str, Any]:
    """Write the seeded manifest when absent; return the seed + whether it wrote."""
    target = root / MANIFEST_REL
    if target.is_file():
        return {"seeded": False, "reason": "capabilities.json already present"}
    seed = _seed_manifest(root)
    write_json(target, seed)
    return {"seeded": True, "path": MANIFEST_REL, "hooks": len(seed["hooks"])}


# --------------------------------------------------------------------------- #
# __main__ self-check
# --------------------------------------------------------------------------- #
def _selfcheck() -> int:  # pragma: no cover - exercised as a script
    import tempfile

    checks: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        home = Path(td) / "home"
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".codex").mkdir(parents=True)
        # canonical Claude wiring
        write_json(root / CLAUDE_SETTINGS_REL, {"hooks": {
            "PreToolUse": [
                {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command",
                 "command": "bash tools/agent-sync/py-run.sh tools/hooks/protect_files.py"}]},
                {"matcher": "Bash", "hooks": [{"type": "command",
                 "command": "bash tools/agent-sync/py-run.sh tools/hooks/deny_hitl_flags.py"}]},
            ],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command",
                "command": "bash tools/agent-sync/py-run.sh tools/hooks/update_agent_handoff.py --check"}]}],
            "SessionStart": [{"hooks": [{"type": "command",
                "command": "bash tools/agent-sync/py-run.sh tools/hooks/reload_context_after_compact.py"}]}],
        }})
        # drifted Codex wiring: deny_hitl missing, reload under PostCompact, protect matcher wider
        write_json(root / CODEX_HOOKS_REL, {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|apply_patch", "hooks": [{"type": "command",
                "command": "./tools/agent-sync/py-run.sh tools/hooks/protect_files.py"}]}],
            "PostCompact": [{"hooks": [{"type": "command",
                "command": "./tools/agent-sync/py-run.sh tools/hooks/reload_context_after_compact.py"}]}],
        }})
        (root / CODEX_CONFIG_REL).write_text(
            "[mcp_servers]\n\n[profiles.scan]\nmodel = \"x\"\n[profiles.plan]\nmodel = \"y\"\n",
            encoding="utf-8")

        # adopt seeds the manifest, then re-adopt is a no-op
        seeded = adopt(root)
        ok("adopt:seeds", seeded.get("seeded") is True and (root / MANIFEST_REL).is_file())
        ok("adopt:idempotent", adopt(root).get("seeded") is False)
        manifest = load_manifest(root)
        ok("seed:has-deny-hitl", any(h["id"] == "deny-hitl-flags" for h in manifest["hooks"]))
        ok("seed:reload-sessionstart",
           any(h["id"] == "reload-context-after-compact"
               and h["events"]["codex"]["event"] == "SessionStart" for h in manifest["hooks"]))
        ok("seed:handoff-args",
           any(h.get("args") == ["--check"] for h in manifest["hooks"] if h["id"] == "update-agent-handoff"))

        # toml profiles parsed
        codex = collect_codex(root, home)
        ok("toml:profiles", codex["profiles"] == ["plan", "scan"], str(codex["profiles"]))

        # audit detects the drift + the deny_hitl absence + unportable ponytail
        a1 = audit(root, home)
        statuses = {(g["vendor"], g["capability"]): g["status"] for g in a1["gaps"]}
        ok("audit:deny-hitl-missing", statuses.get(("codex", "deny-hitl-flags")) == "missing")
        ok("audit:reload-drifted", statuses.get(("codex", "reload-context-after-compact")) == "drifted")
        ok("audit:ponytail-unportable", statuses.get(("codex", "ponytail")) == "unportable")
        ok("audit:high-gap", a1["summary"]["high"] >= 1, str(a1["summary"]))

        # pair dry-run non-empty, then apply repairs, then second apply is a no-op
        dry = pair(root, home, apply=False)
        ok("pair:dry-nonempty", len(dry["plan"]) >= 1, str(len(dry["plan"])))
        pair(root, home, apply=True)
        a2 = audit(root, home)
        ok("pair:fixes-hooks",
           all(s == "present" for s in a2["matrix"]["codex"]["hooks"].values()),
           str(a2["matrix"]["codex"]["hooks"]))
        second = pair(root, home, apply=True)
        ok("pair:idempotent", second["changes"] == 0, str(second["plan"]))

        # unportable skill: readable user SKILL.md -> adapter generated
        skill_dir = home / ".claude" / "plugins" / "mkt" / "ponytail" / "skills" / "ponytail"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Ponytail\nBe lazy.\n", encoding="utf-8")
        # remove any adapter from the prior apply to force regeneration
        adapter = root / "codex" / "prompts" / "ponytail.md"
        if adapter.exists():
            adapter.unlink()
        claude = collect_claude(root, home)
        ok("collect:user-skill-found", any(u["name"] == "ponytail" for u in claude["userSkills"]))
        pair(root, home, apply=True)
        ok("pair:adapter-generated", adapter.is_file()
           and "generated parity adapter" in adapter.read_text(encoding="utf-8"))

        # repo-scoped skill (SPEC-119): the adapter path is checked for EVERY
        # vendor incl. claude — present when the file exists, unportable when not.
        (root / ".claude" / "skills" / "research").mkdir(parents=True)
        (root / ".claude" / "skills" / "research" / "SKILL.md").write_text("# research\n", encoding="utf-8")
        repo_skill = {"name": "research", "scope": "repo",
                      "vendors": {"claude": ".claude/skills/research/SKILL.md", "codex": "codex/prompts/research.md"}}
        ok("skill:repo-scoped-claude-present", _skill_status(repo_skill, "claude", root, claude)[0] == "present")
        ok("skill:repo-scoped-claude-missing",
           _skill_status({"name": "research", "scope": "repo", "vendors": {"claude": ".claude/skills/nope/SKILL.md"}},
                         "claude", root, claude)[0] == "unportable")

        # unreadable/absent user dir degrades to no skill (no crash)
        empty = collect_claude(root, Path(td) / "nonexistent-home")
        ok("collect:missing-home-degrades", empty["userSkills"] == [])

        # out-of-root write attempt raises
        try:
            _under_root(root, "../escape.json")
            ok("safety:out-of-root-raises", False, "did not raise")
        except ValueError:
            ok("safety:out-of-root-raises", True)

        # parity_summary skips cleanly with no manifest
        ok("summary:no-manifest-empty", parity_summary(Path(td) / "empty") == {})

        # codex hook-trust staleness (machine-local, advisory). root/.codex/hooks.json
        # was written above (it exists); the ghost path is never created.
        thome = Path(td) / "trust-home"
        (thome / ".codex").mkdir(parents=True)
        cur = (root / CODEX_HOOKS_REL).resolve()
        ghost = str(Path(td) / "gone" / ".codex" / "hooks.json")

        def _twrite(*keys: str) -> None:
            body = "".join(f"[hooks.state.'{k}']\ntrusted_hash = \"sha256:x\"\n" for k in keys)
            (thome / ".codex" / "config.toml").write_text("[hooks.state]\n" + body, encoding="utf-8")

        _twrite(f"{ghost}:pre_tool_use:0:0")  # orphan present, current repo absent
        tf = {g["capability"] for g in collect_codex_trust(root, thome)}
        ok("trust:missing+orphan", {"codex-trust-missing", "codex-trust-orphan"} <= tf, str(tf))
        _twrite(f"{cur}:pre_tool_use:0:0", f"{ghost}:stop:0:0")  # current trusted, orphan lingers
        tf2 = {g["capability"] for g in collect_codex_trust(root, thome)}
        ok("trust:present-suppresses-missing",
           "codex-trust-missing" not in tf2 and "codex-trust-orphan" in tf2, str(tf2))
        ok("trust:no-config-degrades", collect_codex_trust(root, Path(td) / "no-home") == [])

        # --- SPEC-113 bet E: agent-profile parity ---------------------- #
        # A hyphenated claude profile with Edit/Write tools + effort=max.
        (root / CLAUDE_AGENTS_REL).mkdir(parents=True, exist_ok=True)
        (root / CLAUDE_AGENTS_REL / "my-editor.md").write_text(
            "---\nname: my-editor\ndescription: Tiny \"quoted\" edits.\n"
            "model: opus\neffort: max\ntools: Read, Edit, Write\n---\n\n"
            "Body line one.\nBody line two with `backtick` and -> arrow.\n",
            encoding="utf-8")
        prof = next(p for p in collect_claude_agents(root) if p.get("name") == "my-editor")
        cp = codex_agent_profile(prof)
        ok("agent:name-law", cp["name"] == "my_editor", cp["name"])
        ok("agent:sandbox-write", cp["sandbox_mode"] == "workspace-write", cp["sandbox_mode"])
        ok("agent:effort-max-maps", cp.get("model_reasoning_effort") == "xhigh",
           str(cp.get("model_reasoning_effort")))
        ok("agent:no-model-key", "model" not in cp, str(cp.keys()))
        # round-trip: tomllib parses the rendered text back to the SAME dict (item 4)
        rendered = render_codex_agent_toml(cp)
        ok("agent:toml-roundtrip", tomllib is not None and tomllib.loads(rendered) == cp,
           None if tomllib is None else str(tomllib.loads(rendered)))
        # read-only derivation: no Edit/Write, no effort -> effort key omitted
        (root / CLAUDE_AGENTS_REL / "reader.md").write_text(
            "---\nname: reader\ndescription: read only.\ntools: Read, Grep\n---\n\nJust read.\n",
            encoding="utf-8")
        rp = codex_agent_profile(next(p for p in collect_claude_agents(root) if p["name"] == "reader"))
        ok("agent:sandbox-read", rp["sandbox_mode"] == "read-only", rp["sandbox_mode"])
        ok("agent:effort-omitted", "model_reasoning_effort" not in rp, str(rp))
        # pair renders the tomls; second apply is idempotent
        ags = pair(root, home, apply=True)
        ok("agent:pair-renders",
           any(p["path"] == f"{CODEX_AGENTS_REL}/my_editor.toml" for p in ags["plan"]), str(ags["plan"]))
        ok("agent:toml-written", (root / CODEX_AGENTS_REL / "my_editor.toml").is_file())
        ok("agent:pair-idempotent",
           not any(p["path"].startswith(CODEX_AGENTS_REL) for p in pair(root, home, apply=True)["plan"]))
        # audit: rendered profiles present; a stray toml is 'extra' and untouched
        stray = root / CODEX_AGENTS_REL / "reader_x.toml"
        stray_bytes = b"name = \"reader_x\"\n"
        stray.write_bytes(stray_bytes)
        am = audit(root, home)["matrix"]["agents"]
        ok("agent:audit-present", am["profiles"].get("my_editor") == "present", str(am["profiles"]))
        ok("agent:audit-extra", "reader_x.toml" in am["extra"], str(am["extra"]))
        ok("agent:audit-unportable-note", "model" in am["unportable"])
        # drift: mutate a rendered toml -> drifted; restore -> present
        toml_path = root / CODEX_AGENTS_REL / "my_editor.toml"
        good = toml_path.read_bytes()
        toml_path.write_bytes(good + b"\n# drift\n")
        ok("agent:audit-drifted",
           audit(root, home)["matrix"]["agents"]["profiles"].get("my_editor") == "drifted")
        toml_path.write_bytes(good)
        ok("agent:audit-restored",
           audit(root, home)["matrix"]["agents"]["profiles"].get("my_editor") == "present")
        ok("agent:extra-untouched", stray.read_bytes() == stray_bytes)
        # exec call params: same name/effort/sandbox derivation as the toml (minus description)
        parsed = tomllib.loads(good.decode("utf-8")) if tomllib else {}
        params = codex_exec_call_params(root, "my_editor")
        ok("agent:exec-params-match",
           params is not None
           and params["name"] == parsed.get("name")
           and params["sandbox_mode"] == parsed.get("sandbox_mode")
           and params.get("model_reasoning_effort") == parsed.get("model_reasoning_effort")
           and "description" not in params, str(params))
        # degradation: an unparseable profile is skipped, not fatal
        (root / CLAUDE_AGENTS_REL / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
        ok("agent:degrades",
           any(p.get("degraded") for p in collect_claude_agents(root) if p["name"] == "broken"))

        # --- SPEC-113 §8.4 (LQ7-C3): capability supportState ---------------- #
        sroot = Path(td) / "support-repo"
        (sroot / ".harness").mkdir(parents=True)
        write_json(sroot / MANIFEST_REL, {
            "schemaVersion": 1, "skills": [], "mcpServers": {}, "hooks": [
                {"id": "plain", "script": "tools/hooks/plain.py", "runner": "bash r",
                 "events": {"claude": {"event": "Stop", "matcher": None}}},
                {"id": "gate-wait", "script": "tools/hooks/gate.py", "runner": "bash r",
                 "supportState": {"claude": "native", "codex": "degraded"},
                 "degradationReason": "codex fires Stop, not SubagentStop; deny advisory.",
                 "events": {"claude": {"event": "SubagentStop", "matcher": None}}},
                {"id": "gone", "script": "tools/hooks/gone.py", "runner": "bash r",
                 "supportState": {"codex": "unsupported"},
                 "events": {"codex": {"event": "Stop", "matcher": None}}},
                {"id": "emu", "script": "tools/hooks/emu.py", "runner": "bash r",
                 "supportState": "emulated",
                 "events": {"claude": {"event": "Stop", "matcher": None}}},
            ]})
        scaps = load_manifest(sroot)
        ok("support:default-native", capability_support_state(scaps, "plain", "codex") == "native")
        ok("support:missing-cap-native", capability_support_state(scaps, "nope", "codex") == "native")
        ok("support:no-vendor-native", capability_support_state(scaps, "gate-wait") == "native")
        ok("support:per-vendor-codex",
           capability_support_state(scaps, "gate-wait", "codex") == "degraded")
        ok("support:per-vendor-claude",
           capability_support_state(scaps, "gate-wait", "claude") == "native")
        ok("support:scalar-all-vendors",
           capability_support_state(scaps, "emu", "codex") == "emulated")
        ok("support:bad-value-clamps-native",
           capability_support_state({"hooks": [{"id": "x", "supportState": "weird"}]}, "x", "codex")
           == "native")
        sa = audit(sroot, home)
        sup = sa["matrix"]["supportState"]
        ok("support:audit-degraded", sup.get("gate-wait", {}).get("codex") == "degraded", str(sup))
        ok("support:audit-default-native", sup.get("plain", {}).get("claude") == "native")
        ok("support:audit-reason", "SubagentStop" in sup.get("gate-wait", {}).get("degradationReason", ""))
        sg = {(g["vendor"], g["capability"]) for g in sa["gaps"] if g["kind"] == "support-state"}
        ok("support:degraded-not-a-gap", ("codex", "gate-wait") not in sg, str(sg))
        ok("support:unsupported-wired-gap", ("codex", "gone") in sg, str(sg))
        gf = {name: status for name, status, _ in parity_gate_findings(sroot, home)}
        ok("support:gate-never-fails", all(s in ("pass", "skip") for s in gf.values()), str(gf))
        ok("support:gate-row-present", "agent-parity:support-state" in gf, str(gf))

        # --- KIMI-V1 follow-up 1 (2026-08-07): kimi skills+agents are RENDERED --- #
        # Pre-decl baseline: no kimi opt-in anywhere -> audit fabricates NO kimi gap
        # (skills leg is decl-gated; the agents leg is gated on the same opt-in).
        ok("kimi:no-decl-no-gaps",
           not any(g["vendor"] == "kimi" for g in audit(root, home)["gaps"]))
        ok("kimi:no-decl-no-agents-matrix",
           "kimiAgents" not in audit(root, home)["matrix"])
        kman = load_manifest(root)
        for sk in kman["skills"]:
            sk.setdefault("vendors", {})["kimi"] = f".kimi-code/skills/{sk['name']}/SKILL.md"
        kman["skills"].append({"name": "research", "scope": "repo", "required": True,
                               "vendors": {"claude": ".claude/skills/research/SKILL.md",
                                           "codex": "codex/prompts/research.md",
                                           "kimi": ".kimi-code/skills/research/SKILL.md"}})
        kman["vendorNotes"] = {"kimi": "hooks user-scope only; exit-2 deny enforced (probe 2026-07-28)"}
        write_json(root / MANIFEST_REL, kman)
        ka = audit(root, home)
        kg = [g for g in ka["gaps"] if g["vendor"] == "kimi"]
        # Canonical sources exist (user ponytail + repo research written above), so an
        # absent adapter is MISSING and the fix names pair — never the old follow-up.
        ok("kimi:skill-missing",
           any(g["capability"] == "ponytail" and g["status"] == "missing" for g in kg),
           str([(g["capability"], g["status"]) for g in kg]))
        ok("kimi:fix-names-pair",
           bool(kg) and all("agents pair --apply" in g["fix"] for g in kg if g["kind"] == "skill"),
           str([g.get("fix") for g in kg[:2]]))
        ok("kimi:no-hook-gaps", not any(g["kind"] == "hook" for g in kg))
        ok("kimi:vendor-notes-surfaced",
           ka["matrix"]["vendorNotes"].get("kimi", "").startswith("hooks user-scope"))
        # The opt-in also opens the agents leg: canonical my-editor/reader have no
        # kimi markdown yet -> missing gaps.
        ok("kimi:agents-missing",
           ka["matrix"]["kimiAgents"]["profiles"].get("my-editor") == "missing",
           str(ka["matrix"]["kimiAgents"]["profiles"]))
        kr = {name: status for name, status, _ in parity_gate_findings(root, home)}
        ok("kimi:gate-row-skip-when-gapped", kr.get("agent-parity:kimi") == "skip",
           str(kr.get("agent-parity:kimi")))
        # pair renders skills (canonical text fidelity, no header) + agents
        # (frontmatter, no model)
        pair(root, home, apply=True)
        pony = root / ".kimi-code" / "skills" / "ponytail" / "SKILL.md"
        ok("kimi:pair-renders-skill", pony.is_file())
        ok("kimi:skill-text-fidelity", read_text(pony) == "# Ponytail\nBe lazy.\n",
           repr(read_text(pony)[:40]))
        repo_research = root / ".kimi-code" / "skills" / "research" / "SKILL.md"
        ok("kimi:pair-renders-repo-skill",
           repo_research.is_file() and read_text(repo_research) == "# research\n")
        kmd = read_text(root / KIMI_AGENTS_REL / "my-editor.md")
        kfm = kmd.split("---")[1] if kmd.startswith("---") else ""
        ok("kimi:agent-rendered",
           kmd.startswith("---\nname: my-editor\n") and "tools: Read, Edit, Write" in kfm, kmd[:80])
        ok("kimi:agent-no-model-keys", "model:" not in kfm and "effort:" not in kfm, kfm)
        ok("kimi:agent-hyphen-name-kept",
           (root / KIMI_AGENTS_REL / "my-editor.md").is_file())
        kam = audit(root, home)["matrix"]
        ok("kimi:audit-skill-present", kam["kimi"]["skills"].get("ponytail") == "present")
        ok("kimi:audit-agents-present",
           kam["kimiAgents"]["profiles"].get("my-editor") == "present", str(kam["kimiAgents"]["profiles"]))
        ok("kimi:pair-idempotent",
           not any(p["path"].startswith(".kimi-code") for p in pair(root, home, apply=True)["plan"]))
        # drift: mutate a rendered adapter -> drifted; pair repairs it
        good = pony.read_bytes()
        pony.write_bytes(good + b"\n# drift\n")
        ok("kimi:audit-drifted", audit(root, home)["matrix"]["kimi"]["skills"].get("ponytail") == "drifted")
        pair(root, home, apply=True)
        ok("kimi:drift-repaired", audit(root, home)["matrix"]["kimi"]["skills"].get("ponytail") == "present")
        # stray kimi agent file: extra, info-only, never touched
        kstray = root / KIMI_AGENTS_REL / "stray.md"
        kstray.write_text("# not derived\n", encoding="utf-8")
        kextra = audit(root, home)["matrix"]["kimiAgents"]
        ok("kimi:agents-extra", "stray.md" in kextra["extra"], str(kextra["extra"]))
        ok("kimi:agents-extra-untouched", read_text(kstray) == "# not derived\n")
        # unportable: declared kimi adapter whose canonical source is unreadable
        ghost_skill = {"name": "ghost", "scope": "user",
                       "vendors": {"claude": "user plugin/skill", "kimi": ".kimi-code/skills/ghost/SKILL.md"}}
        ok("kimi:unportable-no-source",
           _skill_status(ghost_skill, "kimi", root, collect_claude(root, home))[0] == "unportable")
        ok("kimi:gate-row-pass-when-clean",
           {n: s for n, s, _ in parity_gate_findings(root, home)}.get("agent-parity:kimi") == "pass")
        # KIMI-V1-FU item 6: the kimi agent schema is larger than
        # {name, description, tools, body} — optional fields declared in the
        # source frontmatter pass through verbatim; model/effort never do.
        (root / CLAUDE_AGENTS_REL / "fancy.md").write_text(
            "---\nname: fancy\ndescription: optional fields.\nmodel: opus\neffort: high\n"
            "tools: Read\nwhenToUse: when fancy\nmodel_preference: secondary\n"
            "disallowedTools: Bash\n---\n\nFancy body.\n", encoding="utf-8")
        kp = kimi_agent_profile(next(p for p in collect_claude_agents(root) if p["name"] == "fancy"))
        krm = render_kimi_agent_md(kp)
        kfm2 = krm.split("---")[1]
        ok("kimi:optional-fields-passthrough",
           "whenToUse: when fancy" in kfm2 and "model_preference: secondary" in kfm2
           and "disallowedTools: Bash" in kfm2, kfm2)
        ok("kimi:optional-never-model-effort",
           "model:" not in kfm2 and "effort:" not in kfm2, kfm2)

    failed = [n for n, c, _ in checks if not c]
    print(f"\nagent_parity self-check: {len(checks) - len(failed)}/{len(checks)} passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else " — all green"))
    return 1 if failed else 0


# phase-2 split (budget 900): conformance domain in the sibling; shims keep importers whole.
from harness_lib.agent_parity_conformance import (  # noqa: F401,E402 -- re-export shims
    audit_matrix_sections, conformance_findings, conformance_report,
    executor_accounting_semantics, gate_finding_row, load_executors)


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
