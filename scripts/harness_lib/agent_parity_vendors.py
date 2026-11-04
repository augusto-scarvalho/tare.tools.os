"""SPEC-113 vendor collectors + codex agent-profile rendering (extracted from
agent_parity.py for the QA.3 self-lib-file-budget).

Pure, read-only helpers that read each vendor's real on-disk surface
(.claude / .codex) and derive the codex agent-profile toml / exec params from
the canonical .claude/agents/*.md frontmatter. Every collector
try/except-degrades to {}/[] so a missing or corrupt vendor file never sinks the
audit that imports these. No harness-bound state: each function takes root/home
Paths explicitly, so this stays a pure module (no bind(env)). agent_parity.py
re-exports these names, so external callers keep using
agent_parity.collect_claude(...) etc. unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib  # py>=3.11
except ImportError:  # pragma: no cover - codex profiles/mcp just degrade to {}
    tomllib = None

try:
    from .common import read_json, read_text
except ImportError:  # run directly / flat sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import read_json, read_text


CLAUDE_SETTINGS_REL = ".claude/settings.json"
CODEX_HOOKS_REL = ".codex/hooks.json"
CODEX_CONFIG_REL = ".codex/config.toml"
CLAUDE_AGENTS_REL = ".claude/agents"
# KIMI-V1 follow-up 1 (shipped 2026-08-07): kimi project-scope discovery surfaces.
# Hooks stay USER-scope only (~/.kimi-code/config.toml, operator-owned, NEVER
# pair-rendered) — only skills/agents live here.
KIMI_SKILLS_REL = ".kimi-code/skills"
KIMI_AGENTS_REL = ".kimi-code/agents"
# Antigravity CLI / AGY workspace discovery surfaces (AGY-V1).
ANTIGRAVITY_HOOKS_REL = ".agents/hooks.json"
ANTIGRAVITY_SKILLS_REL = ".agents/skills"
ANTIGRAVITY_AGENTS_REL = ".agents/agents"
ANTIGRAVITY_RULES_REL = ".agents/rules"
ANTIGRAVITY_MCP_REL = ".agents/mcp_config.json"

# SPEC-113 bet E: Claude effort labels pass through to codex
# `model_reasoning_effort` unchanged except the single non-codex label.
_EFFORT_MAP = {"max": "xhigh"}
# Optional kimi agent frontmatter fields (kimi-code 0.29.2+; KIMI-V1-FU).
# Pass-through ONLY — presence is source-declared, never synthesized.
# `model`/`effort` are deliberately NOT in this set: model choice is the
# executor route-tuple's at spawn time, and kimi's `model_preference` is a
# primary/secondary binding, not a tier — never translate claude tiers into it.
KIMI_AGENT_OPTIONAL_FIELDS = ("whenToUse", "override", "model_preference",
                              "disallowedTools", "subagents")
# A trust key is "<abs hooks.json>:<event>:<i>:<j>"; the path holds the drive
# colon, so we rsplit off exactly the trailing event:i:j. Only drive-letter /
# POSIX-absolute paths are real filesystem hooks.json locations — plugin keys
# like "ponytail@ponytail:hooks/..." are not files and must be ignored.
_ABS_HOOK_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/)")


def _command_for(hook: dict[str, Any], vendor: str = "codex") -> str:
    """Reconstruct the full hook command string for one vendor.

    The invocation form is VENDOR wiring, not per-hook data (the manifest's old
    per-hook `runner` strings were 18 copies of the same fact and are ignored):
    - claude: the bash wrapper, cwd-robust $CLAUDE_PROJECT_DIR-absolute — hook
      commands run in the session shell's drifting cwd (2026-07-22 guard-layer
      skip; hcs-3/hcs-4 pin this form).
    - codex: bash-free — NO bash can start inside codex's Windows
      restricted-token sandbox (System32 bash is the WSL launcher, dies with
      E_ACCESSDENIED; Git Bash dies in CreateFileMapping; measured 2026-07-27/28,
      row codex-stop-hook-fails-silently), so every hook routes through the
      py-run.sh twin tools/agent-sync/py_run.py (HARNESS_PYTHON-first).
    """
    script = str(hook.get("script") or "").strip()
    args = [str(a) for a in (hook.get("args") or [])]
    if vendor == "claude":
        parts = ['bash "$CLAUDE_PROJECT_DIR/tools/agent-sync/py-run.sh"',
                 f'"$CLAUDE_PROJECT_DIR/{script}"', *args]
    else:
        parts = ["python tools/agent-sync/py_run.py", script, *args]
    return " ".join(p for p in parts if p)


def _script_of(command: Any) -> str:
    """Last .py path token in a hook command — the identity used for matching.
    Tokens are unquoted and a $CLAUDE_PROJECT_DIR/ prefix canonicalizes to the
    repo-relative form: hook commands went cwd-robust-absolute on 2026-07-22
    (owner-reported guard-layer skip), and the parity identity must not change
    with the invocation form."""
    toks = [t.strip('"\'') for t in str(command or "").replace("\\", "/").split()]
    py = [t for t in toks if t.endswith(".py")]
    if not py:
        return ""
    script = py[-1]
    for pref in ("$CLAUDE_PROJECT_DIR/", "${CLAUDE_PROJECT_DIR}/"):
        if script.startswith(pref):
            return script[len(pref):]
    return script


# --------------------------------------------------------------------------- #
# collectors (all never-crash)
# --------------------------------------------------------------------------- #
def _normalize_settings_hooks(data: Any) -> list[dict[str, Any]]:
    """Flatten a Claude/Codex hooks file into {event, matcher, script} rows."""
    out: list[dict[str, Any]] = []
    hooks = (data or {}).get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher")
            for hk in entry.get("hooks") or []:
                if not isinstance(hk, dict):
                    continue
                script = _script_of(hk.get("command"))
                if script:
                    out.append({"event": event, "matcher": matcher, "script": script})
    return out


def _claude_user_skills(home: Path) -> list[dict[str, str]]:
    """User-scope Claude skills (name + SKILL.md path). Layout varies, so we
    glob recursively under plugins/ and skills/ and dedupe by skill name,
    preferring the canonical copy (fewest hidden dirs, then shortest path) over
    alternate packagings like `.openclaw/`."""
    base = home / ".claude"
    cands: dict[str, list[Path]] = {}
    for pattern in ("plugins/**/skills/*/SKILL.md", "skills/*/SKILL.md"):
        try:
            for p in base.glob(pattern):
                if p.is_file() and p.parent.name:
                    cands.setdefault(p.parent.name, []).append(p)
        except OSError:
            continue

    def rank(p: Path) -> tuple[int, int, str]:
        parts = p.relative_to(base).parts
        return (sum(1 for s in parts if s.startswith(".")), len(parts), str(p))

    return [{"name": n, "path": str(min(ps, key=rank))} for n, ps in sorted(cands.items())]


def collect_claude(root: Path, home: Path | None = None) -> dict[str, Any]:
    home = home or Path.home()
    out: dict[str, Any] = {"hooks": [], "agents": [], "userSkills": [], "userPlugins": []}
    try:
        out["hooks"] = _normalize_settings_hooks(read_json(root / CLAUDE_SETTINGS_REL, {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        out["agents"] = sorted(p.stem for p in (root / ".claude" / "agents").glob("*.md"))
    except OSError:
        pass
    try:
        out["userSkills"] = _claude_user_skills(home)
    except Exception:  # noqa: BLE001
        pass
    try:
        plug = home / ".claude" / "plugins"
        out["userPlugins"] = sorted(d.name for d in plug.iterdir() if d.is_dir()) if plug.is_dir() else []
    except OSError:
        pass
    return out


def _toml(root_path: Path) -> dict[str, Any]:
    if tomllib is None or not root_path.is_file():
        return {}
    try:
        with root_path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError, ValueError):  # type: ignore[union-attr]
        return {}


def collect_codex(root: Path, home: Path | None = None) -> dict[str, Any]:
    home = home or Path.home()
    out: dict[str, Any] = {"hooks": [], "profiles": [], "mcpServers": {}, "prompts": [], "userSkills": []}
    try:
        out["hooks"] = _normalize_settings_hooks(read_json(root / CODEX_HOOKS_REL, {}))
    except Exception:  # noqa: BLE001
        pass
    cfg = _toml(root / CODEX_CONFIG_REL)
    if isinstance(cfg.get("profiles"), dict):
        out["profiles"] = sorted(cfg["profiles"].keys())
    if isinstance(cfg.get("mcp_servers"), dict):
        out["mcpServers"] = cfg["mcp_servers"]
    try:
        out["prompts"] = sorted(p.name for p in (root / "codex" / "prompts").glob("*.md"))
    except OSError:
        pass
    try:
        codex_home = home / ".codex"
        if codex_home.is_dir():
            out["userSkills"] = sorted({p.parent.name for p in codex_home.rglob("SKILL.md")})
    except Exception:  # noqa: BLE001
        pass
    return out


def _trust_key_path(key: str) -> str:
    """Extract the hooks.json path from a "<path>:<event>:<i>:<j>" trust key."""
    return str(key).rsplit(":", 3)[0]


def collect_codex_trust(root: Path, home: Path) -> list[dict[str, Any]]:
    """Machine-local, ADVISORY-ONLY codex hook-trust staleness findings.

    Codex 0.144 trusts a project's hooks per ABSOLUTE hooks.json PATH + per HASH
    in `~/.codex/config.toml` [hooks.state]. A repo folder move silently breaks
    that trust: the new path has no entry (hooks never fire) while the old path's
    entries rot as orphans. We detect ABSENCE and ORPHANHOOD only — NOT
    trusted_hash correctness: the hash recipe is undocumented, so a present entry
    is assumed fine and absence/orphanhood is the reliable signal.

    Every failure degrades silently to no findings (codex not installed, config
    missing/unparseable) so this never sinks the audit. Findings carry
    scope="machine-local": they are advisory and must never fail a CI/fresh-clone
    audit exit code.
    """
    cfg = _toml(home / ".codex" / "config.toml")
    state = cfg.get("hooks", {}).get("state") if isinstance(cfg.get("hooks"), dict) else None
    if not isinstance(state, dict):
        return []
    norm = lambda s: str(s).replace("\\", "/").lower()  # noqa: E731
    findings: list[dict[str, Any]] = []

    hooks_json = (root / CODEX_HOOKS_REL).resolve()
    if hooks_json.is_file() and not any(norm(_trust_key_path(k)) == norm(hooks_json) for k in state):
        findings.append({
            "severity": "medium", "vendor": "codex", "capability": "codex-trust-missing",
            "kind": "trust", "status": "advisory", "scope": "machine-local",
            "detail": f"no codex hook-trust entry for this repo's {CODEX_HOOKS_REL} ({hooks_json}); "
                      "codex hooks were likely never trusted at this location (e.g. after a repo folder move)",
            "fix": "open one interactive codex session in this repo and accept the hooks trust prompt",
        })

    orphans = sorted({p for k in state
                      if _ABS_HOOK_PATH_RE.match(p := _trust_key_path(k)) and not Path(p).is_file()})
    if orphans:
        findings.append({
            "severity": "medium", "vendor": "codex", "capability": "codex-trust-orphan",
            "kind": "trust", "status": "advisory", "scope": "machine-local",
            "detail": "codex hook-trust entries point at hooks.json paths that no longer exist "
                      "(stale ghosts from old repo locations): " + "; ".join(orphans),
            "fix": "harmless; codex re-prompts at the live path — optionally remove the stale "
                   "[hooks.state.'<path>...'] blocks from ~/.codex/config.toml",
        })
    return findings


def collect_instruction_files(root: Path) -> dict[str, list[str]]:
    """Per market-registry family (knownAgentInstructionFiles): which listed
    files actually exist in this repo."""
    try:
        from harness_lib import protected_files
        sources = protected_files.known_agent_instruction_sources(root)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for src in sources if isinstance(sources, list) else []:
        if not isinstance(src, dict):
            continue
        present: list[str] = []
        for f in src.get("files", []) or []:
            f = str(f)
            try:
                if any(ch in f for ch in "*?["):
                    if any(root.glob(f)):
                        present.append(f)
                elif (root / f).is_file():
                    present.append(f)
            except OSError:
                continue
        out[str(src.get("tool", ""))] = present
    return out


# --------------------------------------------------------------------------- #
# agent profiles (SPEC-113 bet E): .claude/agents/*.md -> .codex/agents/*.toml
#
# Canon = the Claude frontmatter files; codex surfaces are RENDERED (same
# render-from-canonical invariant as the hook wiring). EXP-19 measured that
# .toml profiles do NOT load under `codex exec`, so on that surface parity rides
# in call params (`codex_exec_call_params`); the tomls serve interactive/
# app-server codex. Agent names are lowercase/digits/underscores only — a hyphen
# is rejected by codex (the invisible blocker of EXP-19 v1-v3).
# --------------------------------------------------------------------------- #
def _parse_agent_md(text: str) -> dict[str, Any] | None:
    """Parse a `.claude/agents/*.md`: simple `key: value` frontmatter + body.
    stdlib-only (no YAML dep) — the frontmatter is a flat scalar block. Returns
    None when there is no `---` frontmatter or no `name` (caller degrades)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text or "", re.DOTALL)
    if not m:
        return None
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    if not fm.get("name"):
        return None
    tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()]
    out: dict[str, Any] = {"name": fm["name"], "description": fm.get("description", ""),
                           "model": fm.get("model", ""), "effort": fm.get("effort", ""),
                           "tools": tools, "body": m.group(2)}
    optional = {k: fm[k] for k in KIMI_AGENT_OPTIONAL_FIELDS if fm.get(k)}
    if optional:
        out["kimi_optional"] = optional
    return out


def collect_claude_agents(root: Path) -> list[dict[str, Any]]:
    """Parse every `.claude/agents/*.md` (canonical agent profiles). Never-crash:
    a missing dir yields []; an unparseable file yields a `{'degraded': ...}` row
    (skipped, not fatal) — same degrade-not-die stance as the other collectors."""
    out: list[dict[str, Any]] = []
    try:
        files = sorted((root / CLAUDE_AGENTS_REL).glob("*.md"))
    except OSError:
        return out
    for f in files:
        try:
            profile = _parse_agent_md(read_text(f, ""))
        except Exception:  # noqa: BLE001 — a corrupt profile must not sink the audit
            profile = None
        if profile:
            profile["source"] = f.name
            out.append(profile)
        else:
            out.append({"name": f.stem, "source": f.name, "degraded": "no parseable frontmatter"})
    return out


def _normalize_body(body: str) -> str:
    """Markdown body verbatim, whitespace-normalized at LINE ENDS only (no
    reflow): rstrip each line, drop leading/trailing blank lines."""
    return "\n".join(ln.rstrip() for ln in str(body).splitlines()).strip("\n")


def codex_agent_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """The ONE shared canonical->codex derivation used by both legs (toml render
    and exec call params) so they are parity by construction. Emits, in order:
    name (codex naming law), description, developer_instructions, optional
    model_reasoning_effort, sandbox_mode. NO `model` key (see _AGENT_UNPORTABLE)."""
    name = re.sub(r"[^a-z0-9_]", "_", str(profile.get("name", "")).lower())
    tools = profile.get("tools") or []
    writes = any(t in ("Edit", "Write") for t in tools)  # S3 sandbox derivation
    out: dict[str, Any] = {
        "name": name,
        "description": str(profile.get("description", "")),
        "developer_instructions": _normalize_body(profile.get("body", "")),
    }
    effort = str(profile.get("effort", "")).strip()
    if effort:
        out["model_reasoning_effort"] = _EFFORT_MAP.get(effort, effort)
    out["sandbox_mode"] = "workspace-write" if writes else "read-only"
    return out


def _toml_basic(value: Any) -> str:
    """A TOML basic string. json.dumps escaping IS valid TOML basic-string
    escaping (\\\", \\\\, \\n, \\uXXXX) — stdlib has no toml writer to lean on."""
    return json.dumps(str(value), ensure_ascii=False)


def _toml_dev_instructions(value: str) -> str:
    """Single-line -> basic string; multiline -> a `\"\"\"` multiline basic string
    with backslashes and quotes escaped (raw newlines preserved). TOML trims the
    one newline right after the opening delimiter, so the leading `\\n` here is
    not part of the value — round-trips exactly (asserted in the self-check)."""
    text = str(value)
    if "\n" not in text:
        return _toml_basic(text)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"""\n' + escaped + '"""'


def render_codex_agent_toml(cp: dict[str, Any]) -> str:
    """Deterministic, byte-stable TOML text for a codex agent profile dict (the
    output of `codex_agent_profile`). Field order mirrors the .codex/agents
    convention: name, description, developer_instructions, [model_reasoning_effort],
    sandbox_mode."""
    lines = [
        f"name = {_toml_basic(cp['name'])}",
        f"description = {_toml_basic(cp.get('description', ''))}",
        f"developer_instructions = {_toml_dev_instructions(cp.get('developer_instructions', ''))}",
    ]
    if "model_reasoning_effort" in cp:
        lines.append(f"model_reasoning_effort = {_toml_basic(cp['model_reasoning_effort'])}")
    lines.append(f"sandbox_mode = {_toml_basic(cp.get('sandbox_mode', 'read-only'))}")
    return "\n".join(lines) + "\n"


def codex_exec_call_params(root: Path, agent_name: str) -> dict[str, Any] | None:
    """SPEC-113 exec-surface bridge: the call-params translation a `codex exec`
    fork-join spawn would pass (tomls don't load under exec, EXP-19). Same
    `codex_agent_profile` derivation as the toml, minus `description`. Matches on
    the codex-legal (derived) name. Returns None if no such profile exists."""
    for profile in collect_claude_agents(root):
        if profile.get("degraded"):
            continue
        cp = codex_agent_profile(profile)
        if cp["name"] == agent_name:
            params = {"name": cp["name"],
                      "developer_instructions": cp["developer_instructions"],
                      "sandbox_mode": cp["sandbox_mode"]}
            if "model_reasoning_effort" in cp:
                params["model_reasoning_effort"] = cp["model_reasoning_effort"]
            return params
    return None


# --------------------------------------------------------------------------- #
# kimi surfaces (KIMI-V1 follow-up 1, shipped 2026-08-07): .kimi-code/skills/
# <name>/SKILL.md and .kimi-code/agents/*.md rendered from the claude-format
# TRANSITIONAL compatibility source (debt canonical-ir-vendor-adapters: the
# vendor-neutral canonical IR does not exist yet; .claude/* stands in for it —
# do NOT call it "canon" without that qualifier). Kimi is NEAR-CLAUDE (probe
# 2026-07-28): skills are YAML frontmatter + body and agents take
# name/description/tools + body, so no adaptation header is injected — the
# codex `_adapter_content` header would corrupt SKILL.md frontmatter. Skill
# render = canonical text fidelity (LF-normalized) of the source file; agent
# render = frontmatter + normalized body. NO model/effort is rendered (same
# law as _AGENT_UNPORTABLE: model choice is the executor route-tuple's at
# spawn time; kimi's own `model_preference` is a primary/secondary binding,
# not a tier — never translate claude tiers into it). The kimi agent schema is
# LARGER than {name, description, tools, body}: the optional fields below are
# passed through verbatim when the source frontmatter declares them, so a
# future canonical IR can feed them without a renderer change.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Optional kimi agent frontmatter fields live at the top of this module
# (KIMI_AGENT_OPTIONAL_FIELDS, next to _EFFORT_MAP). The kimi agent schema is
# LARGER than {name, description, tools, body}: optional fields are passed
# through verbatim when the source frontmatter declares them, so a future
# canonical IR can feed them without a renderer change.
# --------------------------------------------------------------------------- #


def kimi_agent_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Claude-format agent profile (transitional source) -> kimi agent dict.
    Names pass through unchanged — kimi accepts hyphens (near-claude), so the
    codex underscore law does NOT apply here. Optional kimi fields declared in
    the source frontmatter ride along verbatim."""
    out = {
        "name": str(profile.get("name", "")),
        "description": str(profile.get("description", "")),
        "tools": [str(t) for t in (profile.get("tools") or [])],
        "body": _normalize_body(profile.get("body", "")),
    }
    extra = profile.get("kimi_optional") or {}
    for key in KIMI_AGENT_OPTIONAL_FIELDS:
        if extra.get(key):
            out[key] = str(extra[key])
    return out


def render_kimi_agent_md(cp: dict[str, Any]) -> str:
    """Deterministic, byte-stable kimi agent markdown: `---` frontmatter
    (name/description/tools + any declared optional fields, plain scalars
    exactly as the source writes them) + blank line + body. Round-trips
    through `_parse_agent_md` to the same name/description/tools (asserted in
    the self-check)."""
    lines = ["---", f"name: {cp['name']}", f"description: {cp.get('description', '')}"]
    if cp.get("tools"):
        lines.append("tools: " + ", ".join(cp["tools"]))
    for key in KIMI_AGENT_OPTIONAL_FIELDS:
        if cp.get(key):
            lines.append(f"{key}: {cp[key]}")
    lines.append("---")
    body = str(cp.get("body", ""))
    return "\n".join(lines) + "\n\n" + (body + "\n" if body else "")


def _lf(text: str) -> str:
    """LF-normalized text for cross-platform byte-compare (write_text pins
    newline='\\n'; a CRLF user-scope source must not drift-loop the audit)."""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def kimi_skill_source_text(skill: dict[str, Any], root: Path,
                           claude: dict[str, Any]) -> str | None:
    """The canonical text a kimi skill adapter is rendered FROM, LF-normalized.

    Repo-scoped skills: the claude repo path declared in the manifest
    (`vendors.claude`, e.g. .claude/skills/research/SKILL.md). User-scoped
    skills: the installed user-scope SKILL.md. None when the source is
    unreadable/absent (caller reports `unportable`/`manual`, never fabricates).
    """
    if str(skill.get("scope")) == "repo":
        decl = str((skill.get("vendors") or {}).get("claude") or "")
        if decl and "/" in decl:
            try:
                p = root / decl
                if p.is_file():
                    return _lf(p.read_text(encoding="utf-8"))
            except OSError:
                return None
        return None
    name = skill.get("name")
    for u in claude.get("userSkills", []):
        if u.get("name") == name:
            try:
                p = Path(str(u.get("path") or ""))
                if p.is_file():
                    return _lf(p.read_text(encoding="utf-8"))
            except OSError:
                return None
    return None


def collect_kimi(root: Path) -> dict[str, Any]:
    """Read the kimi project-scope surface (.kimi-code/). Never-crash: a missing
    dir yields empty lists, so a fresh clone degrades cleanly."""
    out: dict[str, Any] = {"skills": [], "agents": []}
    try:
        out["skills"] = sorted(p.parent.name for p in (root / KIMI_SKILLS_REL).glob("*/SKILL.md"))
    except OSError:
        pass
    try:
        out["agents"] = sorted(p.stem for p in (root / KIMI_AGENTS_REL).glob("*.md"))
    except OSError:
        pass
    return out


_AGY_MODEL_TIER_MAP = {
    "opus": "pro", "max": "pro", "xhigh": "pro",
    "haiku": "flash", "low": "flash",
    "sonnet": "inherit", "high": "inherit"
}

_AGY_TOOL_MAP = {
    "read": ["view_file"],
    "edit": ["replace_file_content", "multi_replace_file_content"],
    "write": ["write_to_file"],
    "grep": ["grep_search"],
    "glob": ["list_dir"],
    "bash": ["run_command"],
}


def _translate_agy_tools(raw_tools: list[Any]) -> list[str]:
    agy_tools: list[str] = []
    for t in raw_tools:
        clean_t = str(t).strip().lower()
        if clean_t in _AGY_TOOL_MAP:
            for mapped in _AGY_TOOL_MAP[clean_t]:
                if mapped not in agy_tools:
                    agy_tools.append(mapped)
        else:
            if t and str(t) not in agy_tools:
                agy_tools.append(str(t))
    return agy_tools


def antigravity_agent_profile(profile: dict[str, Any]) -> dict[str, Any]:
    raw_model = (profile.get("model") or "").strip().lower()
    raw_effort = (profile.get("effort") or "").strip().lower()
    tier = _AGY_MODEL_TIER_MAP.get(raw_model) or _AGY_MODEL_TIER_MAP.get(raw_effort) or "inherit"
    return {
        "name": str(profile.get("name", "")),
        "description": str(profile.get("description", "")),
        "tools": _translate_agy_tools(profile.get("tools") or []),
        "model": tier,
        "body": _normalize_body(profile.get("body", "")),
    }



KNOWN_AGY_EVENTS = {
    "PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop"
}


def render_antigravity_hooks_json() -> str:
    """Render official AGY named-hook schema for .agents/hooks.json."""
    data = {
        "file-protection-gate": {
            "PreToolUse": [
                {
                    "matcher": "write_to_file|replace_file_content|multi_replace_file_content|run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/protect_files.py",
                            "timeout": 10
                        },
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/workflow_write_guard.py",
                            "timeout": 10
                        }
                    ]
                }
            ]
        },
        "command-security-gate": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/deny_hitl_flags.py",
                            "timeout": 10
                        },
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/cwd_guard.py",
                            "timeout": 10
                        }
                    ]
                }
            ]
        },
        "search-governance-gate": {
            "PreToolUse": [
                {
                    "matcher": "grep_search|list_dir|read_url_content",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/graphify_search_guard.py",
                            "timeout": 10
                        }
                    ]
                }
            ]
        },
        "spawn-economy-gate": {
            "PreToolUse": [
                {
                    "matcher": "invoke_subagent|define_subagent|run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python tools/agent-sync/py_run.py tools/hooks/agent_spawn_economy.py",
                            "timeout": 10
                        }
                    ]
                }
            ]
        },
        "session-stop-gate": {
            "Stop": [
                {
                    "type": "command",
                    "command": "python tools/agent-sync/py_run.py tools/hooks/validate_before_stop.py",
                    "timeout": 15
                }
            ]
        },
        "context-hydration-gate": {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": "python tools/agent-sync/py_run.py tools/hooks/reload_context_after_compact.py",
                    "timeout": 15
                }
            ]
        }
    }
    return json.dumps(data, indent=2) + "\n"


def validate_antigravity_hooks_schema(data: Any) -> tuple[bool, str]:
    """Validate AGY named-hook schema.
    
    Top-level keys MUST NOT be 'version' or 'hooks' (reserved harness convention
    to prevent legacy format regressions). Every top-level value must be a dict
    (HookSpec object). Each key in a HookSpec must be a known AGY event (or 'enabled'),
    with handler definitions matching AGY event specs.
    """
    if not isinstance(data, dict):
        return False, "Root must be a JSON object"

    if "version" in data or "hooks" in data:
        return False, "Top-level 'version' and 'hooks' keys are reserved/invalid in AGY named-hook schema"

    if not data:
        return False, "Named-hook schema root cannot be empty"

    for gate_name, spec in data.items():
        if not isinstance(gate_name, str) or not gate_name.strip():
            return False, "Gate name must be a non-empty string"
        if not isinstance(spec, dict):
            return False, f"Gate '{gate_name}' must be a HookSpec object (dict)"
        
        for key, val in spec.items():
            if key == "enabled":
                if not isinstance(val, bool):
                    return False, f"Gate '{gate_name}' enabled field must be a boolean"
                continue
            if key not in KNOWN_AGY_EVENTS:
                return False, f"Gate '{gate_name}' has unknown AGY event '{key}'"

            if not isinstance(val, list):
                return False, f"Event '{key}' in gate '{gate_name}' must be an array of handlers/groups"

            if not val:
                return False, f"Event '{key}' in gate '{gate_name}' list cannot be empty"

            for idx, item in enumerate(val):
                if not isinstance(item, dict):
                    return False, f"Item {idx} in event '{key}' (gate '{gate_name}') must be an object"
                
                # PreToolUse / PostToolUse tool events require matcher + hooks array
                if key in ("PreToolUse", "PostToolUse"):
                    if "matcher" not in item or not isinstance(item["matcher"], str) or not item["matcher"].strip():
                        return False, f"Item {idx} in '{key}' (gate '{gate_name}') must have non-empty string 'matcher'"
                    if "hooks" not in item or not isinstance(item["hooks"], list) or not item["hooks"]:
                        return False, f"Item {idx} in '{key}' (gate '{gate_name}') must have non-empty 'hooks' array"
                    for h_idx, hk in enumerate(item["hooks"]):
                        if not isinstance(hk, dict):
                            return False, f"Hook {h_idx} in item {idx} of '{key}' must be an object"
                        cmd = hk.get("command")
                        if not isinstance(cmd, str) or not cmd.strip():
                            return False, f"Hook {h_idx} in item {idx} of '{key}' must have non-empty 'command'"
                        htype = hk.get("type")
                        if htype is not None and htype != "command":
                            return False, f"Hook {h_idx} in item {idx} of '{key}' type must be 'command' when specified"
                else:
                    # Lifecycle events: direct handler dicts or group dicts with 'hooks'
                    if "hooks" in item:
                        if not isinstance(item["hooks"], list) or not item["hooks"]:
                            return False, f"Item {idx} in '{key}' (gate '{gate_name}') must have non-empty 'hooks' array"
                        for h_idx, hk in enumerate(item["hooks"]):
                            if not isinstance(hk, dict):
                                return False, f"Hook {h_idx} in item {idx} of '{key}' must be an object"
                            cmd = hk.get("command")
                            if not isinstance(cmd, str) or not cmd.strip():
                                return False, f"Hook {h_idx} in item {idx} of '{key}' must have non-empty 'command'"
                            htype = hk.get("type")
                            if htype is not None and htype != "command":
                                return False, f"Hook {h_idx} in item {idx} of '{key}' type must be 'command' when specified"
                    else:
                        cmd = item.get("command")
                        if not isinstance(cmd, str) or not cmd.strip():
                            return False, f"Item {idx} in '{key}' (gate '{gate_name}') must have non-empty 'command'"
                        htype = item.get("type")
                        if htype is not None and htype != "command":
                            return False, f"Item {idx} in '{key}' (gate '{gate_name}') type must be 'command' when specified"

    return True, "Valid AGY named-hook schema"


def render_antigravity_agent_md(cp: dict[str, Any]) -> str:
    lines = ["---", f"name: {cp['name']}", f"description: {cp.get('description', '')}"]
    if cp.get("tools"):
        lines.append("tools: " + ", ".join(cp["tools"]))
    if cp.get("model"):
        lines.append(f"model: {cp['model']}")
    lines.append("---")
    body = str(cp.get("body", ""))
    return "\n".join(lines) + "\n\n" + (body + "\n" if body else "")


def collect_antigravity(root: Path) -> dict[str, Any]:
    """Read the Antigravity workspace surface (.agents/). Never-crash: missing
    files yield empty lists."""
    out: dict[str, Any] = {"skills": [], "agents": [], "hooks": []}
    try:
        out["skills"] = sorted(p.parent.name for p in (root / ANTIGRAVITY_SKILLS_REL).glob("*/SKILL.md"))
    except OSError:
        pass
    try:
        out["agents"] = sorted(p.stem for p in (root / ANTIGRAVITY_AGENTS_REL).glob("*.md"))
    except OSError:
        pass
    try:
        hooks_json = root / ANTIGRAVITY_HOOKS_REL
        if hooks_json.is_file():
            data = json.loads(hooks_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for gate_name, spec in data.items():
                    if isinstance(spec, dict):
                        for ev, hlist in spec.items():
                            if ev == "enabled" or not isinstance(hlist, list):
                                continue
                            for h in hlist:
                                if isinstance(h, dict):
                                    if "hooks" in h and isinstance(h["hooks"], list):
                                        matcher = str(h.get("matcher", ""))
                                        for sub_h in h["hooks"]:
                                            if isinstance(sub_h, dict) and sub_h.get("command"):
                                                out["hooks"].append({
                                                    "gate": gate_name,
                                                    "event": ev,
                                                    "matcher": matcher,
                                                    "command": sub_h["command"]
                                                })
                                    elif h.get("command"):
                                        out["hooks"].append({
                                            "gate": gate_name,
                                            "event": ev,
                                            "matcher": str(h.get("matcher", "")),
                                            "command": h["command"]
                                        })
    except Exception:
        pass
    return out


