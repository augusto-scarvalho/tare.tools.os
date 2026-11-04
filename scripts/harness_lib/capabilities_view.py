#!/usr/bin/env python3
"""CAP.1 — capabilities snapshot backend + `agents skills` / `agents mcp` CLI.

One deterministic inventory of the capability surface, grouped by the three
scopes the separation policy names (roadmap `screens-capabilities.md`):

  * harness  — declared in `.harness/capabilities.json` (`scope: "repo"`);
  * user     — declared there with `scope: "user"`, presence-tracked in the
               operator profile, dieted out of workers by design;
  * target/<n> — declared in that target's `target.json` (CAP.3 adds the
               undeclared-discovery scan; this slice lists declarations).

Reuses the SPEC-113 parity engine (`agent_parity`) — statuses come from
`_skill_status`, never re-derived. `referencedBy` is a deterministic
name-grep over agents/prompts/workflow-profiles matching EXACT invocation
forms only (`/name`, `skills/<name>/`, `prompts/<name>.md`) — misses are
accepted over noise (roadmap risk #3). MCP rows merge the five declaration
points (capabilities.json, codex config, repo mcp manifest, per-target
target.json, and svc-registry `services.json` `mcp` entries); env is shown
as KEY NAMES only, values never echoed.

`skill_file` is a trust boundary: it resolves strictly inside the skill's
own directory and refuses escapes.

GUI consumption (`/api/capabilities`, card grids) is CAP.2 — not here.
Self-check: `python scripts/harness_lib/capabilities_view.py`.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import agent_parity  # noqa: E402
from harness_lib.common import HarnessError, ROOT, read_json  # noqa: E402

_REF_SOURCES = (".claude/agents", ".harness/prompts", "codex/prompts")


def _registered_targets(root: Path):
    """Yield (name, entry, profile, target_root) per registered target profile.

    CAP.3: the shared iterator both snapshots use to reach a governed target's
    OWN capability tree. Per-entry degrade — a broken profile is skipped, never
    fatal. `target_root` is bound to the passed `root` (not module ROOT) so the
    scan stays hermetic under test."""
    targets_dir = root / ".harness" / "targets"
    if not targets_dir.is_dir():
        return
    for profile in sorted(targets_dir.glob("*/target.json")):
        try:
            entry = read_json(profile, {}) or {}
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or profile.parent.name)
        raw = str(entry.get("path") or "")
        troot = Path(raw) if Path(raw).is_absolute() else (root / raw)
        yield name, entry, profile, troot


def referenced_by(root: Path, name: str) -> list[dict[str, str]]:
    """Deterministic consumers of a skill name: exact invocation forms only."""
    root = Path(root)
    forms = (f"/{name}", f"skills/{name}/", f"prompts/{name}.md")
    out: list[dict[str, str]] = []
    for rel in _REF_SOURCES:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(f in text for f in forms):
                kind = "agent" if "agents" in rel else "prompt"
                out.append({"kind": kind, "ref": path.relative_to(root).as_posix()})
    profiles = root / ".harness" / "workflows" / "workflow-profiles.json"
    try:
        text = profiles.read_text(encoding="utf-8", errors="ignore")
        if any(f in text for f in forms):
            out.append({"kind": "workflow", "ref": profiles.relative_to(root).as_posix()})
    except OSError:
        pass
    return out


def skills_snapshot(root: Path | str) -> list[dict[str, Any]]:
    """One row per declared skill (harness + user scopes; target scan = CAP.3)."""
    root = Path(root)
    manifest = agent_parity.load_manifest(root)
    claude = agent_parity.collect_claude(root)
    rows: list[dict[str, Any]] = []
    for skill in manifest.get("skills") or []:
        if not isinstance(skill, dict) or not skill.get("name"):
            continue
        name = str(skill["name"])
        scope = "harness" if str(skill.get("scope")) == "repo" else "user"
        statuses: dict[str, str] = {}
        for vendor in (skill.get("vendors") or {}):
            status, _detail = agent_parity._skill_status(skill, vendor, root, claude)
            statuses[vendor] = status
        skill_dir = root / ".claude" / "skills" / name
        has_scripts = skill_dir.is_dir() and any(
            p.is_file() and p.name != "SKILL.md" for p in skill_dir.rglob("*"))
        rows.append({
            "id": f"{scope}:{name}", "name": name, "scope": scope,
            "path": str((skill.get("vendors") or {}).get("claude") or ""),
            "statuses": statuses, "hasScripts": bool(has_scripts),
            "referencedBy": referenced_by(root, name),
        })
    # user-profile skills not declared in the manifest: visible, never granted.
    declared = {r["name"] for r in rows}
    for entry in claude.get("userSkills") or []:
        name = str(entry.get("name") or "")
        if name and name not in declared:
            rows.append({"id": f"user:{name}", "name": name, "scope": "user",
                         "path": str(entry.get("path") or ""),
                         "statuses": {"claude": "undeclared"},
                         "hasScripts": False, "referencedBy": []})
    # CAP.3 (advisory): discover each target's OWN skills (target_root/.claude/
    # skills/) and flag any absent from that target's separation policy
    # (target.json `skills`) with undeclared=True. Reuses the CAP.1 skill
    # discovery (agent_parity._claude_user_skills) — nothing re-implemented. A
    # target with no skill tree degrades to zero rows. Read-only; no content is
    # read here (names only), the file viewer stays the trust boundary.
    for tname, entry, _profile, troot in _registered_targets(root):
        policy = {str(s) for s in (entry.get("skills") or [])}
        for sk in agent_parity._claude_user_skills(troot):
            name = str(sk.get("name") or "")
            if not name:
                continue
            skill_dir = Path(sk.get("path") or "").parent
            has_scripts = skill_dir.is_dir() and any(
                p.is_file() and p.name != "SKILL.md" for p in skill_dir.rglob("*"))
            rows.append({
                "id": f"target/{tname}:{name}", "name": name, "scope": f"target/{tname}",
                "path": str(sk.get("path") or ""), "statuses": {},
                "hasScripts": bool(has_scripts), "referencedBy": [],
                "undeclared": name not in policy})
    return rows


def _mask_env(env: Any) -> list[str]:
    return sorted(str(k) for k in env) if isinstance(env, dict) else []


def mcp_snapshot(root: Path | str) -> list[dict[str, Any]]:
    """One row per declared MCP server across the five declaration points."""
    root = Path(root)
    rows: list[dict[str, Any]] = []

    def add(scope: str, name: str, declared_in: str, entry: Any,
            accessible_by: str, undeclared: bool = False) -> None:
        entry = entry if isinstance(entry, dict) else {}
        rows.append({
            "id": f"{scope}:{name}", "name": str(name), "scope": scope,
            "declaredIn": declared_in,
            "command": entry.get("command"),
            "url": (entry.get("mcp") or {}).get("url") or entry.get("url"),
            "envKeys": _mask_env(entry.get("env")),  # names only, never values
            "accessibleBy": accessible_by,
            "undeclared": bool(undeclared),  # CAP.3 advisory badge (target scope)
        })

    manifest = agent_parity.load_manifest(root)
    for name, entry in (manifest.get("mcpServers") or {}).items():
        add("harness", name, agent_parity.MANIFEST_REL, entry,
            "harness sessions + workers on harness work")
    codex = agent_parity.collect_codex(root)
    for name, entry in (codex.get("mcpServers") or {}).items():
        add("harness", name, agent_parity.CODEX_CONFIG_REL, entry,
            "codex sessions (adapter)")
    for name in sorted(agent_parity._repo_mcp_servers(root)):
        add("harness", name, agent_parity.REPO_MCP_REL, {}, "agent-sync manifest")
    services = (read_json(root / ".harness" / "services.json", {}) or {}).get("services") or {}
    for name, entry in services.items():
        if isinstance(entry, dict) and entry.get("mcp"):
            add("harness", name, ".harness/services.json", entry,
                "svc-registry lifecycle (harness scope)")
    for tname, t_entry, profile, troot in _registered_targets(root):
        try:  # a broken target profile must not kill the inventory
            policy = (t_entry.get("mcp") or {}).get("servers") or {}
            for name, entry in policy.items():
                add(f"target/{tname}", name, profile.relative_to(root).as_posix(),
                    entry, f"workers spawned for target {tname} only (strict-mcp-config)")
            # CAP.3 (advisory): discover the target's OWN .mcp.json and flag any
            # server absent from the target.json separation policy. Env is masked
            # to key NAMES by _mask_env (via add) — a secret VALUE is never read
            # or echoed, same rule as CAP.2. A target with no .mcp.json degrades
            # to nothing (read_json returns {}).
            discovered = read_json(troot / ".mcp.json", {}) or {}
            for name, entry in (discovered.get("mcpServers") or {}).items():
                if name in policy:
                    continue  # declared AND present → already a row above
                add(f"target/{tname}", name, f"{tname}/.mcp.json", entry,
                    f"discovered in target {tname} — not declared in its target.json",
                    undeclared=True)
        except Exception:
            continue
    return rows


def skill_file(root: Path | str, skill_id: str, rel: str | None = None) -> str:
    """Read SKILL.md (or a script under the skill dir). TRUST BOUNDARY: the
    resolved path must stay inside that skill's directory — escapes refuse."""
    root = Path(root)
    name = skill_id.split(":", 1)[-1]
    row = next((r for r in skills_snapshot(root) if r["name"] == name), None)
    if row is None:
        raise HarnessError(f"unknown skill: {skill_id}")
    if row["scope"] == "harness":
        base = (root / ".claude" / "skills" / name).resolve()
    else:
        path = Path(row["path"] or "")
        if not path.is_absolute() or not path.exists():
            raise HarnessError(f"skill {skill_id} has no readable local path")
        base = path.parent.resolve()
    candidate = (base / (rel or "SKILL.md")).resolve()
    if base != candidate and base not in candidate.parents:
        raise HarnessError(f"path escapes the skill directory: {rel}")
    if not candidate.is_file():
        raise HarnessError(f"no such file under {skill_id}: {rel or 'SKILL.md'}")
    return candidate.read_text(encoding="utf-8", errors="ignore")


def cmd_agents_skills(args) -> int:
    from harness_lib import common

    action = getattr(args, "action", "list") or "list"
    if action == "show":
        if not getattr(args, "id", None):
            raise HarnessError("usage: agents skills show <id> [--file <rel>]")
        print(skill_file(ROOT, args.id, getattr(args, "file", None)))
        return 0
    rows = skills_snapshot(ROOT)
    flat = [{"id": r["id"], "scope": r["scope"],
             "statuses": ",".join(f"{v}={s}" for v, s in sorted(r["statuses"].items())),
             "refs": len(r["referencedBy"]), "hasScripts": r["hasScripts"]}
            for r in rows]
    if common.agent_output_compact():
        common.emit(flat, tsv=True)
    elif getattr(args, "json", False):
        common.emit(rows)
    else:
        for r in flat:
            print(f"{r['id']:<28} {r['statuses']:<40} refs={r['refs']} "
                  f"scripts={'y' if r['hasScripts'] else '-'}")
        print(f"\n{len(flat)} skill(s); `agents skills show <id>` prints SKILL.md")
    return 0


def cmd_agents_mcp(args) -> int:
    from harness_lib import common

    rows = mcp_snapshot(ROOT)
    if common.agent_output_compact():
        common.emit([{**r, "envKeys": ",".join(r["envKeys"])} for r in rows], tsv=True)
    elif getattr(args, "json", False):
        common.emit(rows)
    elif not rows:
        print("no MCP servers declared in any scope.")
        print("grant paths: .harness/capabilities.json mcpServers{} (harness) | "
              ".harness/targets/<n>/target.json mcp.servers (target, strict-mcp-config) | "
              ".harness/services.json mcp entries (svc-registry lifecycle)")
    else:
        for r in rows:
            print(f"{r['id']:<28} {r['declaredIn']:<44} env={','.join(r['envKeys']) or '-'}")
        print(f"\n{len(rows)} server(s); access is deny-by-default per scope")
    return 0


def _demo() -> None:
    rows = skills_snapshot(ROOT)
    by_name = {r["name"]: r for r in rows}
    assert "research" in by_name and by_name["research"]["scope"] == "harness", sorted(by_name)
    assert any(ref["kind"] == "prompt" for ref in by_name["research"]["referencedBy"]), \
        by_name["research"]["referencedBy"]
    text = skill_file(ROOT, "harness:research")
    assert "research" in text.lower()
    try:
        skill_file(ROOT, "harness:research", "../../../AGENTS.md")
        raise AssertionError("escape accepted")
    except HarnessError as exc:
        assert "escapes" in str(exc)
    assert isinstance(mcp_snapshot(ROOT), list)  # empty today; shape holds

    # CAP.3: target discovery tags a target's own skill + .mcp.json server with
    # scope target/<name> + undeclared, masks env to NAMES, and degrades calmly.
    import json as _json
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    (tmp / ".harness" / "targets" / "tX").mkdir(parents=True)
    (tmp / ".harness" / "targets" / "tX" / "target.json").write_text(_json.dumps(
        {"name": "tX", "path": str(tmp)}), encoding="utf-8")  # no declared skills/mcp
    (tmp / ".claude" / "skills" / "gizmo").mkdir(parents=True)
    (tmp / ".claude" / "skills" / "gizmo" / "SKILL.md").write_text("# gizmo\n", encoding="utf-8")
    (tmp / ".mcp.json").write_text(_json.dumps(
        {"mcpServers": {"stray": {"command": ["stray"], "env": {"STRAY_TOKEN": "SECRET-99"}}}}),
        encoding="utf-8")
    tskills = {r["id"]: r for r in skills_snapshot(tmp)}
    tmcp = {r["id"]: r for r in mcp_snapshot(tmp)}
    assert tskills.get("target/tX:gizmo", {}).get("undeclared") is True, sorted(tskills)
    assert tmcp.get("target/tX:stray", {}).get("undeclared") is True, sorted(tmcp)
    assert tmcp["target/tX:stray"]["envKeys"] == ["STRAY_TOKEN"], tmcp["target/tX:stray"]
    assert "SECRET-99" not in _json.dumps(list(tmcp.values())), "secret VALUE leaked"
    bare = Path(tempfile.mkdtemp())  # a repo with no targets degrades to no target rows
    assert not any(r["scope"].startswith("target/") for r in skills_snapshot(bare))
    print("capabilities_view self-check: ok")


if __name__ == "__main__":
    _demo()
