#!/usr/bin/env python3
"""Role -> playbook resolution + registry health (SPEC-170 W1).

`.harness/routing/playbook-registry.json` is the single source of truth for
WHICH prompt files a role operates under. Each role optionally `extends` ONE
parent and adds its own `playbooks`; `resolve(role)` walks that single-parent
chain base-first and returns the ordered, de-duped path list an agent of that
role reads. `playbook_compiler.compile_role(role)` turns that chain into the one
served artifact (SPEC-173 rule 19 retired the concat assembler that lived here).
`verify()` is the honesty gate: it reports dangling
(registered file missing), orphan (a `.harness/prompts/**` file neither chained
nor listed in `unmanaged`), broken-ref (a relative markdown link inside a
registered playbook that does not resolve), and pin-duplication (a role carrying
a model `card` pin — model routing owns pins, not this registry), plus a
sha256/mtime digest per existing registered file.

W2 (SPEC-170 slice A) adds two inspection layers on top: a per-role chain
lockfile (`write_lock` -> `.harness/routing/playbook-registry.lock.json`, a
sha256 over each chain file's LF-NORMALIZED bytes so a CRLF checkout does not
drift the hash) and an advisory H2-heading collision linter (measure-only, never
affects rc; SPEC-173 moved the compiler-resolvable ones to `deduped`). The
`playbook` verb is registered via cli_registry and its full-contract flag is
`--compose` — `--render`/`--compile` retired with the concat path in SPEC-173
Phase 4b. stdlib-only. Self-check: python scripts/harness_lib/playbook_registry.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import role_metamodel  # noqa: E402  (one-way: it never imports back)
from harness_lib.common import HarnessError, read_json, write_json  # noqa: E402

REGISTRY_REL = ".harness/routing/playbook-registry.json"
LOCK_REL = ".harness/routing/playbook-registry.lock.json"
PROMPTS_REL = ".harness/prompts"
COLLISION_OK = "<!-- collision-ok -->"  # a heading line ending with this is not linted
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")  # [text](target) inline link


def load(root: Path | str) -> dict[str, Any]:
    """Load + shallow-validate the registry. Typed refusal (fix: line) on a
    missing or malformed file, so a broken registry never resolves silently.

    SPEC-173 rule 6: the unified role metamodel is the source when the root has
    one. The legacy read below is KEPT (D055/OQ4b-5): it is a READER serving
    legacy-shape hermetic fixtures, not the concat SERVING path rule 19 retired,
    so it outlived Phase 4 on purpose. Deleting it would force a full metamodel
    document into every temp root in pbc_lock_stale / pbc_compile / _demo for no
    user-visible gain."""
    doc = role_metamodel.load(root)
    if doc is not None:
        return role_metamodel.registry_view(doc)
    data = read_json(Path(root) / REGISTRY_REL, None)
    if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
        raise HarnessError(
            f"missing or malformed {REGISTRY_REL}\n"
            f"fix: restore the registry (an object with a 'roles' map)")
    return data


def resolve(role: str, registry: dict[str, Any]) -> list[str]:
    """Ordered, de-duped playbook paths for `role`, base parent first.

    Walks the single `extends` chain to the root, then lays down each role's
    `playbooks` base-first. Typed refusal on an unknown role, an `extends` to an
    undefined role, or an `extends` cycle."""
    roles = registry.get("roles", {})
    if role not in roles:
        raise HarnessError(
            f"unknown role {role!r}\nfix: one of {', '.join(sorted(roles)) or '(none defined)'}")
    chain: list[str] = []  # leaf -> root
    seen: set[str] = set()
    cur: str | None = role
    while cur is not None:
        if cur in seen:
            raise HarnessError(
                f"extends cycle at role {cur!r}\nfix: break the extends cycle in {REGISTRY_REL}")
        if cur not in roles:
            raise HarnessError(
                f"role {role!r} extends undefined role {cur!r}\nfix: define {cur!r} in {REGISTRY_REL}")
        seen.add(cur)
        chain.append(cur)
        cur = roles[cur].get("extends")
    out: list[str] = []
    for r in reversed(chain):  # base first
        for pb in roles[r].get("playbooks", []) or []:
            if pb not in out:
                out.append(pb)
    return out


# SPEC-173 rule 19 (Phase 4b): compose(), _origin_header() and render() were
# DELETED with the concat world they served — not archived in place. compile_role()
# is now the only assembler of full playbook bytes, and `--compose` is the single
# full-contract verb (`--render`/`--compile` retired with it; the hook's head
# directive already names --compose, so no prompt text moved). Rollback from here
# is `git revert`: rule 18's flag-flip promise held only until retirement completed.

INJECT_MODES = ("list", "compose")


def inject_mode(role: str, registry: dict[str, Any]) -> str:
    """The role's spawn-injection mode (SPEC-170 rule 18): 'compose' only when
    declared in the registry; absent = 'list' (the Q2 status quo). Unknown
    values are graded by verify(), not here."""
    return registry.get("roles", {}).get(role, {}).get("inject") or "list"


def _enforce_source_budgets(role: str, registry: dict[str, Any], root: Path) -> None:
    """SPEC-173 rules 15-16 on the SPAWN path: refuse — typed, with NO payload —
    when a chain source blows the budget the metamodel declares for it.

    METAMODEL-GATED: a root without a metamodel declares no budgets and behaves
    byte-identically (every hermetic fixture in the scenarios relies on that). A
    present-but-invalid metamodel is graded by `role_metamodel --check`, never by
    the spawn path, so its refusal is swallowed here rather than blocking a spawn."""
    try:
        doc = role_metamodel.load(root)
    except HarnessError:
        return
    if not doc:
        return
    for rel in resolve(role, registry):
        p = root / rel
        if not p.exists():
            continue
        violation = role_metamodel.source_budget_violation(
            doc, rel, p.read_text(encoding="utf-8", errors="replace"))
        if violation:
            raise HarnessError(
                f"spawn refused: {violation}\n"
                f"fix: shrink the source or raise its budget in role_metamodel.BUDGETS")


def spawn_compose(role: str, registry: dict[str, Any], root: Path | str) -> str:
    """SPEC-170 rule 19 spawn payload for a compose-marked role: chainHash header
    over the role's compiled chain. This IS a spawn input — the stamped hash makes
    the delivered chain version auditable in session transcripts (rule 20 snapshot
    semantics).

    SPEC-173 rules 15-16: a source over its metamodel budget refuses HERE, in the
    injecting path, before any payload exists — silent truncation is prohibited.

    SPEC-173 rule 19: the body is the compiled Effective Playbook, unconditionally
    — the concat branch and its flag retired with Phase 4b. The SPEC-170 head line
    is unchanged, so transcript auditability survived the whole migration."""
    from harness_lib import playbook_compiler  # late: it imports this module

    root = Path(root)
    _enforce_source_budgets(role, registry, root)
    lock = _role_lock(role, registry, root)
    head = (f"# SPEC-170 composed chain injection — role: {role}\n"
            f"# chainHash: {lock['chainHash']}")
    return f"{head}\n\n{playbook_compiler.compile_role(role, registry, root)}"


def _registered_paths(registry: dict[str, Any]) -> list[str]:
    """Union of every role's playbook paths, first-seen order."""
    out: list[str] = []
    for role in registry.get("roles", {}).values():
        for pb in role.get("playbooks", []) or []:
            if pb not in out:
                out.append(pb)
    return out


def _broken_refs(root: Path, path: str, text: str) -> list[str]:
    """Relative markdown link targets in `text` that do not resolve from the
    file's dir. http/https/mailto/# anchors and absolute paths are skipped."""
    out: list[str] = []
    parent = Path(path).parent
    for m in _MD_LINK.finditer(text):
        target = m.group(1).strip()
        if not target:
            continue
        target = target.split()[0]  # drop an optional "title"
        if target.startswith(("http://", "https://", "#", "mailto:", "/")):
            continue
        target = target.split("#", 1)[0]  # drop an anchor fragment
        if not target:
            continue
        if not (root / parent / target).exists():
            out.append(target)
    return out


def _mtime_iso(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _role_lock(role: str, registry: dict[str, Any], root: Path | str) -> dict[str, Any]:
    """The role's chain lock: its resolved file list + a chainHash = sha256 over
    each file's LF-NORMALIZED bytes concatenated (CRLF checkouts hash identically
    to their LF twins). A missing chain file contributes b"" (deterministic).

    SPEC-173 rule 8: `sources` names every compiled source as a (name, sha) pair
    — the per-file provenance a replay consumes — computed in the SAME loop and
    under the same LF/missing-file convention. chainHash is untouched.

    SPEC-173 rule 10: `compiledSha` is the compiled identity a replay reproduces.
    The compiler import is FUNCTION-LOCAL on purpose: playbook_compiler imports
    this module at module level, so the reverse edge must never be hoisted."""
    from harness_lib import playbook_compiler  # late: breaks the import cycle

    root = Path(root)
    files = resolve(role, registry)
    h = hashlib.sha256()
    sources: list[dict[str, str]] = []
    for path in files:
        p = root / path
        raw = (p.read_bytes() if p.exists() else b"").replace(b"\r\n", b"\n")
        h.update(raw)
        sources.append({"name": path, "sha": hashlib.sha256(raw).hexdigest()})
    return {"files": files, "chainHash": h.hexdigest(), "sources": sources,
            "compiledSha": playbook_compiler.compiled_sha(role, registry, root)}


def _newest_chain_mtime(role: str, registry: dict[str, Any], root: Path | str) -> float:
    """Newest mtime among the role's existing chain files (0.0 if none exist)."""
    root = Path(root)
    mtimes = [(root / path).stat().st_mtime for path in resolve(role, registry) if (root / path).exists()]
    return max(mtimes) if mtimes else 0.0


def _lock_drift(registry: dict[str, Any], root: Path | str) -> list[dict[str, str]]:
    """lock-drift findings: an EXISTING lockfile whose stored chainHash for a role
    differs from the recomputed one AND is older than that role's newest chain file
    (compare-then-write: this is the pre-regeneration gate signal). No lockfile ->
    nothing to compare -> no findings."""
    root = Path(root)
    lock_path = root / LOCK_REL
    existing = read_json(lock_path, None)
    if not isinstance(existing, dict):
        return []
    lock_mtime = lock_path.stat().st_mtime
    out: list[dict[str, str]] = []
    for role in registry.get("roles", {}):
        recomputed = _role_lock(role, registry, root)["chainHash"]
        prev = ((existing.get("roles") or {}).get(role) or {}).get("chainHash")
        if prev is not None and prev != recomputed and lock_mtime < _newest_chain_mtime(role, registry, root):
            out.append({"kind": "lock-drift", "path": role,
                        "detail": "lockfile chainHash stale vs a newer chain file; regenerate the lock (--verify)"})
    return out


def _h2_segments(text: str) -> list[tuple[str, str | None, str]]:
    """Partition `text` into ordered `(kind, heading, segment_text)` triples.

    kind is `section` (an H2 heading line + its body up to the next H1/H2) or
    `preamble` (everything else: text before the first H2, H1 sections, and a
    heading line carrying the COLLISION_OK marker, which stays a verbatim
    passthrough — exempt from the collision grammar, as it always was).
    LOSSLESS: `"\\n".join(seg for _, _, seg in ...) == "\\n".join(text.splitlines())`.

    SPEC-173 rule 11: ONE grammar for the linter and the compiler. `_h2_sections`
    and `playbook_compiler` both read the document through here, so they can never
    disagree about where a section starts or what a suppressed heading means."""
    segments: list[tuple[str, str | None, str]] = []
    kind, head, buf = "preamble", None, []

    def flush() -> None:
        if buf:
            segments.append((kind, head, "\n".join(buf)))

    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            flush()
            # a collision-ok heading opens a PREAMBLE run: its body is not a section
            kind, head, buf = (("preamble", None, [line]) if title.endswith(COLLISION_OK)
                               else ("section", title, [line]))
        elif line.startswith("# "):
            flush()
            kind, head, buf = "preamble", None, [line]
        else:
            buf.append(line)
    flush()
    return segments


def _h2_sections(text: str) -> tuple[dict[str, str], int]:
    """Map each H2 heading text -> its section body (lines until the next `# `/`## `
    heading). A `## …` line ending with the collision-ok marker is skipped and
    counted as suppressed. Returns (sections, suppressed_count). Reads the
    document through the shared `_h2_segments` grammar."""
    sections: dict[str, str] = {}
    suppressed = 0
    for kind, head, seg in _h2_segments(text):
        if kind == "section":
            sections[head] = "\n".join(seg.splitlines()[1:]).strip()  # drop the heading line
        elif seg.startswith("## "):  # a suppressed (collision-ok) heading
            suppressed += 1
    return sections, suppressed


def _collisions(registry: dict[str, Any], root: Path | str) -> tuple[list[dict[str, Any]], int]:
    """Advisory H2-heading collisions (S2-C3, EXP-34, measure-only): a heading text
    that appears in >=2 files of ONE role's chain with DIFFERENT section content.
    Returns (advisories, suppressed_count). Never a finding; never affects rc."""
    root = Path(root)
    advisories: list[dict[str, Any]] = []
    cache: dict[str, tuple[dict[str, str], int]] = {}
    for role in registry.get("roles", {}):
        by_head: dict[str, dict[str, str]] = {}
        for path in resolve(role, registry):
            if path not in cache:
                p = root / path
                text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
                cache[path] = _h2_sections(text)
            sections, _ = cache[path]
            for head, content in sections.items():
                by_head.setdefault(head, {})[path] = content
        for head, per_file in by_head.items():
            if len(per_file) >= 2 and len(set(per_file.values())) > 1:
                advisories.append({"kind": "collision", "role": role,
                                   "heading": head, "files": sorted(per_file)})
    suppressed = sum(sup for _, sup in cache.values())
    return advisories, suppressed


def _split_collisions(advisories: list[dict[str, Any]], registry: dict[str, Any],
                      root: Path | str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """SPEC-173 rule 11 / OQ-1: split collisions into (deduped, still-advisory).

    A collision counts as RESOLVED only when the role's compiled output actually
    carries exactly one copy of that heading — asked of the compiler, never
    assumed, so disabling dedup pushes the entries back into `advisories` instead
    of quietly relabelling them. rc semantics unchanged: neither list affects rc."""
    from harness_lib import playbook_compiler  # late: it imports this module

    deduped: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for adv in advisories:
        role = adv["role"]
        if role not in counts:
            counts[role] = playbook_compiler.heading_counts(
                playbook_compiler.compile_role(role, registry, root))
        (deduped if counts[role].get(adv["heading"]) == 1 else advisory).append(adv)
    return deduped, advisory


def write_lock(registry: dict[str, Any], root: Path | str) -> dict[str, Any]:
    """Regenerate + atomically write the chain lockfile (auto-regen only, never
    hand-edited). Returns the written document."""
    root = Path(root)
    roles = {role: _role_lock(role, registry, root) for role in registry.get("roles", {})}
    doc = {"schemaVersion": 1,
           "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "roles": roles}
    write_json(root / LOCK_REL, doc)
    return doc


def verify(registry: dict[str, Any], root: Path | str) -> dict[str, Any]:
    """Registry health over `root`. Findings kinds: dangling | orphan |
    broken-ref | pin-duplication | inject-invalid | lock-drift | dedup-inversion.
    `files` carries a sha256+mtime
    digest per existing registered playbook, `roles` the per-role chain lock
    (files+chainHash), `advisories` the measure-only H2 collisions (never
    affect rc) with a `suppressed` count. ok = no findings. Read-only — the
    lockfile is written by write_lock (cmd `--verify`), never by verify.

    SPEC-173 OQ-1: collisions the compiler RESOLVES move to `deduped`;
    `advisories` keeps only the ones a reader still has to reconcile by hand.
    Amendment A1: a resolution whose WINNER is a tombstone stub is not a
    resolution — `playbook_compiler.dedup_inversions` grades it rc-bearing."""
    from harness_lib import playbook_compiler  # late: it imports this module

    root = Path(root)
    findings: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    registered = _registered_paths(registry)
    for path in registered:
        p = root / path
        if not p.exists():
            findings.append({"kind": "dangling", "path": path,
                             "detail": "registered playbook file does not exist on disk"})
            continue
        files.append({"path": path,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                      "updatedAt": _mtime_iso(p)})
        for target in _broken_refs(root, path, p.read_text(encoding="utf-8", errors="replace")):
            findings.append({"kind": "broken-ref", "path": path,
                             "detail": f"relative link {target!r} does not resolve from {Path(path).parent.as_posix()}"})
    for name, role in registry.get("roles", {}).items():
        if "card" in role:  # a model pin duplicated into the playbook registry
            findings.append({"kind": "pin-duplication", "path": name,
                             "detail": "role carries a model 'card' pin; model routing owns pins, not the playbook registry"})
        if role.get("inject") not in (None, *INJECT_MODES):  # SPEC-170 rule 18
            findings.append({"kind": "inject-invalid", "path": name,
                             "detail": f"unknown inject mode {role.get('inject')!r}; one of {INJECT_MODES}"})
    chained = set(registered)
    unmanaged = set(registry.get("unmanaged", []) or [])
    prompts = root / PROMPTS_REL
    if prompts.exists():
        for f in sorted(prompts.rglob("*.md")):
            rel = f.relative_to(root).as_posix()
            if rel not in chained and rel not in unmanaged:
                findings.append({"kind": "orphan", "path": rel,
                                 "detail": "prompt file neither chained by a role nor listed in 'unmanaged'"})
    findings += _lock_drift(registry, root)
    for name in registry.get("roles", {}):
        findings += playbook_compiler.dedup_inversions(name, registry, root)
    collisions, suppressed = _collisions(registry, root)
    deduped, advisories = _split_collisions(collisions, registry, root)
    roles_lock = {role: _role_lock(role, registry, root) for role in registry.get("roles", {})}
    return {"ok": not findings, "findings": findings, "files": files,
            "roles": roles_lock, "advisories": advisories, "deduped": deduped,
            "suppressed": suppressed}


def cmd_playbook(args) -> int:
    from harness_lib import common

    root = common.ROOT
    registry = load(root)
    role = getattr(args, "role", None)
    if getattr(args, "verify", False):
        report = verify(registry, root)  # compare-then-write: drift is read BEFORE regen
        write_lock(registry, root)       # auto-regen the lockfile (never hand-authored)
        common.emit(report)
        if report["findings"]:
            raise SystemExit(1)  # gate semantics: red registry -> nonzero
        return 0
    if not role:
        raise HarnessError(
            "playbook needs a role\n"
            "fix: harness.py playbook <role> [--list|--compose] (or --verify)")
    if getattr(args, "compose", False):
        # SPEC-173 rule 19: the ONE full-contract verb — the read the hook's head
        # directive orders every session to perform. It serves compiled bytes.
        from harness_lib import playbook_compiler
        print(playbook_compiler.compile_role(role, registry, root), end="")
        return 0
    common.emit(resolve(role, registry))  # --list (default)
    return 0


def _demo() -> None:
    """Hermetic proof of every branch over a temp root: chain order + de-dupe,
    unknown/cycle/undefined-parent refusals, the compiled spawn payload, and each verify
    finding kind (dangling/orphan/broken-ref/pin-duplication). Deterministic."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prompts = root / PROMPTS_REL
        prompts.mkdir(parents=True)
        (root / "AGENTS.md").write_text("base\n", encoding="utf-8")
        (prompts / "ov.md").write_text("overseer\n", encoding="utf-8")
        (prompts / "loop.md").write_text("loop [ok](ov.md) [bad](gone.md)\n", encoding="utf-8")
        (prompts / "unm.md").write_text("unmanaged\n", encoding="utf-8")

        reg = {
            "schemaVersion": 1,
            "roles": {
                "agent-base": {"playbooks": ["AGENTS.md"]},
                "overseer": {"extends": "agent-base", "playbooks": [f"{PROMPTS_REL}/ov.md"]},
                "loop": {"extends": "overseer", "playbooks": [f"{PROMPTS_REL}/loop.md", "AGENTS.md"]},
            },
            "unmanaged": [f"{PROMPTS_REL}/unm.md"],
        }
        # chain order, base first, de-duped (AGENTS.md appears once despite the re-add)
        assert resolve("loop", reg) == ["AGENTS.md", f"{PROMPTS_REL}/ov.md", f"{PROMPTS_REL}/loop.md"], resolve("loop", reg)

        try:  # unknown role -> typed refusal with a fix: line
            resolve("ghost", reg)
            raise AssertionError("expected refusal")
        except HarnessError as exc:
            assert "unknown role" in str(exc) and "fix:" in str(exc), exc

        cyc = {"roles": {"a": {"extends": "b"}, "b": {"extends": "a"}}}
        try:
            resolve("a", cyc)
            raise AssertionError("cycle accepted")
        except HarnessError as exc:
            assert "cycle" in str(exc), exc
        undef = {"roles": {"a": {"extends": "nope"}}}
        try:
            resolve("a", undef)
            raise AssertionError("undefined parent accepted")
        except HarnessError as exc:
            assert "undefined role" in str(exc), exc

        # verify: green over the fixture (loop.md's [ok](ov.md) resolves, its
        # [bad](gone.md) is broken -> exactly one broken-ref finding)
        rep = verify(reg, root)
        kinds = sorted(f["kind"] for f in rep["findings"])
        assert kinds == ["broken-ref"], rep["findings"]
        assert rep["ok"] is False and any(f["path"] == f"{PROMPTS_REL}/loop.md" for f in rep["findings"])
        assert len(rep["files"]) == 3 and all(len(f["sha256"]) == 64 for f in rep["files"]), rep["files"]

        # dangling: point a role at a missing file
        reg_d = {"roles": {"x": {"playbooks": [f"{PROMPTS_REL}/nope.md"]}}, "unmanaged": []}
        assert any(f["kind"] == "dangling" for f in verify(reg_d, root)["findings"])

        # orphan: an unchained/unmanaged prompt file goes red; listing it green
        (prompts / "stray.md").write_text("stray\n", encoding="utf-8")
        reg_o = {"roles": {"agent-base": {"playbooks": ["AGENTS.md"]}}, "unmanaged": [f"{PROMPTS_REL}/unm.md"]}
        orphans = [f for f in verify(reg_o, root)["findings"] if f["kind"] == "orphan"]
        assert {f["path"] for f in orphans} == {f"{PROMPTS_REL}/ov.md", f"{PROMPTS_REL}/loop.md", f"{PROMPTS_REL}/stray.md"}, orphans
        reg_o["unmanaged"] += [f"{PROMPTS_REL}/ov.md", f"{PROMPTS_REL}/loop.md", f"{PROMPTS_REL}/stray.md"]
        assert not [f for f in verify(reg_o, root)["findings"] if f["kind"] == "orphan"]

        # pin-duplication: a role carrying a model 'card' pin
        reg_p = {"roles": {"agent-base": {"playbooks": ["AGENTS.md"], "card": "fable-5"}}, "unmanaged": []}
        assert any(f["kind"] == "pin-duplication" and f["path"] == "agent-base"
                   for f in verify(reg_p, root)["findings"])

        # inject knob (SPEC-170 v5): default list, declared compose, unknown = red
        reg["roles"]["overseer"]["inject"] = "compose"
        assert inject_mode("loop", reg) == "list" and inject_mode("overseer", reg) == "compose"
        # SPEC-173 rule 19: the spawn payload is the SPEC-170 head over COMPILED
        # bytes now — unconditionally, on every root, with no flag left to consult.
        sp = spawn_compose("overseer", reg, root)
        assert sp.startswith("# SPEC-170 composed chain injection") and "chainHash:" in sp, sp
        assert "# EFFECTIVE PLAYBOOK" in sp and f"<!-- from: {PROMPTS_REL}/ov.md" in sp, sp
        reg_i = {"roles": {"agent-base": {"playbooks": ["AGENTS.md"], "inject": "full"}}, "unmanaged": []}
        assert any(f["kind"] == "inject-invalid" and f["path"] == "agent-base"
                   for f in verify(reg_i, root)["findings"])
        del reg["roles"]["overseer"]["inject"]

        # _h2_segments: LOSSLESS partition + the grammar _h2_sections reads through
        doc = ("intro\n# H1\nunder h1\n## Alpha\na1\n## Beta\nb1\n"
               f"## Gamma {COLLISION_OK}\ngamma body\n## Alpha\na2\n")
        segs = _h2_segments(doc)
        assert "\n".join(s for _, _, s in segs) == "\n".join(doc.splitlines()), segs
        assert [(k, h) for k, h, _ in segs] == [
            ("preamble", None), ("preamble", None), ("section", "Alpha"), ("section", "Beta"),
            ("preamble", None), ("section", "Alpha")], segs
        sections, sup = _h2_sections(doc)
        # collision-ok heading suppressed (and its body is not a section); last Alpha wins
        assert sup == 1 and sections["Alpha"] == "a2" and "Gamma" not in str(sections), (sections, sup)

        # deterministic
        assert verify(reg, root)["findings"] == verify(reg, root)["findings"]
    print("playbook_registry self-check: ok")


if __name__ == "__main__":
    _demo()
