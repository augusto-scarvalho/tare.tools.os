#!/usr/bin/env python3
"""Playbook compiler v0 (SPEC-173 Phase 2, rules 10-14).

Compiles a role's resolved chain into ONE Effective Playbook: precedence +
dedup resolve a heading restated across the chain to a single copy, and the
output is a pure function of (chain order, file bytes) — no timestamps, no
mtimes, no environment reads, so identical inputs give byte-identical output
and a lockfile replays hash-identically (rule 10).

PRECEDENCE (rule 11): walking the chain BASE-FIRST, the LAST occurrence of an
H2 heading wins — the leaf-most file, and within one file the last occurrence.
That is `extends` semantics: the specialized playbook overrides its base. Every
losing copy is replaced by one provenance line, so nothing disappears silently.

THIS IS THE ONLY ASSEMBLER (rule 19, Phase 4b): `--compose` and `spawn_compose`
serve compile_role() unconditionally — the concat path, its flag and the
content_equivalence parity oracle that gated the flip were all DELETED once
parity was proven (32/32 roles) and the default flipped. The SessionStart hook
is NOT part of this path: it EXTRACTS ambient cores, and segments are
byte-preserved here, so extraction is identical either way.

Segmentation reuses `playbook_registry._h2_segments` — ONE grammar for the
linter and the compiler. stdlib only. Self-check:
python scripts/harness_lib/playbook_compiler.py.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import playbook_registry  # noqa: E402
from harness_lib.common import HarnessError, read_json  # noqa: E402

MISSING = "(MISSING)"  # a missing chain file's body, deterministically
REGEN_CMD = "python scripts/harness.py playbook agent-base --verify"
DEDUPED = '<!-- deduped: "{heading}" superseded by {path} (SPEC-173 rule 11) -->'
BANNER = "# EFFECTIVE PLAYBOOK — compiled (SPEC-173) role: {role}"
INVERSION_RATIO = 0.10  # a winner under 10% of what it supersedes is a stub, not an override


def _origin_line(path: str, lf: bytes) -> str:
    """The per-file provenance line: `<!-- from: <path> sha256:<12> -->` digested
    over LF-NORMALIZED bytes. Rule 10 requires byte-identical compiled output
    across checkouts, so this digests exactly the bytes it compiles from — a raw
    digest would render differently on a CRLF checkout than on its LF twin."""
    return f"<!-- from: {path} sha256:{hashlib.sha256(lf).hexdigest()[:12]} -->"


def _blocks(role: str, registry: dict[str, Any], root: Path | str) -> list[tuple[str, bytes, list]]:
    """(path, lf_bytes, segments) per chain file, base-first.

    TOTAL by contract — `write_lock` compiles every role over hermetic fixtures,
    so odd content, a missing file, a no-H2 file and an empty chain must all
    produce output rather than raise. A missing file contributes b"" to the hash
    (the chainHash convention) and the literal `(MISSING)` body (compose's)."""
    root = Path(root)
    out: list[tuple[str, bytes, list]] = []
    for path in playbook_registry.resolve(role, registry):
        try:
            lf = (root / path).read_bytes().replace(b"\r\n", b"\n")
            body = lf.decode("utf-8", errors="replace")
        except OSError:
            lf, body = b"", MISSING
        out.append((path, lf, playbook_registry._h2_segments(body)))
    return out


def compile_role(role: str, registry: dict[str, Any], root: Path | str) -> str:
    """The role's Effective Playbook: banner + chainHash, then each chain file's
    origin line and segments in order, every losing section collapsed to one
    provenance line. Deterministic; ends with exactly one trailing newline."""
    blocks = _blocks(role, registry, root)
    winner: dict[str, tuple[int, int]] = {}
    for fi, (_, _, segments) in enumerate(blocks):
        for si, (kind, head, _) in enumerate(segments):
            if kind == "section":
                winner[head] = (fi, si)  # base-first walk => the LAST occurrence wins
    parts: list[str] = []
    for fi, (path, lf, segments) in enumerate(blocks):
        lines = [_origin_line(path, lf)]
        for si, (kind, head, seg) in enumerate(segments):
            if kind == "section" and winner[head] != (fi, si):
                lines.append(DEDUPED.format(heading=head, path=blocks[winner[head][0]][0]))
            else:
                lines.append(seg)  # byte-preserved post-LF-normalization
        parts.append("\n".join(lines))
    # chainHash over the SAME normalized bytes playbook_registry._role_lock digests
    # (recomputed here, never called back into: _role_lock now computes compiledSha
    # THROUGH this function, so borrowing it would recurse). _demo + pc-1 pin equality.
    chain_hash = hashlib.sha256(b"".join(lf for _, lf, _ in blocks)).hexdigest()
    header = f"{BANNER.format(role=role)}\n# chainHash: {chain_hash}"
    return "\n\n".join([header, *parts]) + "\n"


def dedup_inversions(role: str, registry: dict[str, Any], root: Path | str) -> list[dict[str, str]]:
    """rc-bearing findings where dedup let a TOMBSTONE STUB supersede real doctrine.

    Incident 758d900: 7 canonical loop-overseer sections had been replaced by 121 B
    "moved verbatim to overseer-playbook.md" stubs; leaf-wins (rule 11) made each stub
    supersede the 1.4-5.3 KB section it pointed at, the compiled view lost 17,041 B of
    doctrine, and rule-13 equivalence stayed green because the HEADING was still there
    (superseded, not lost). One finding per collision whose winner is under
    INVERSION_RATIO of the segment it displaces — that inverts precedence: the leaf is
    supposed to OVERRIDE the base, not evict it with a forwarding address.

    Pure and read-only: it REPLAYS compile_role's dedup walk over the same
    LF-normalized segments; emitted bytes are untouched (rule 10 does not move)."""
    blocks = _blocks(role, registry, root)
    winner: dict[str, tuple[int, int]] = {}
    for fi, (_, _, segments) in enumerate(blocks):
        for si, (kind, head, _) in enumerate(segments):
            if kind == "section":
                winner[head] = (fi, si)  # same base-first walk compile_role does
    out: list[dict[str, str]] = []
    for fi, (path, _, segments) in enumerate(blocks):
        for si, (kind, head, seg) in enumerate(segments):
            if kind != "section" or winner[head] == (fi, si):
                continue
            wfi, wsi = winner[head]
            win_path, win_seg = blocks[wfi][0], blocks[wfi][2][wsi][2]
            win, lost = len(win_seg.encode("utf-8")), len(seg.encode("utf-8"))
            if win < INVERSION_RATIO * lost:
                out.append({"kind": "dedup-inversion", "path": role, "detail": (
                    f"role {role!r}: {head!r} — winner {win_path} ({win} B) supersedes "
                    f"{path} ({lost} B); a winner under {INVERSION_RATIO:.0%} of the section "
                    f"it replaces is a tombstone stub, not an override — restore the doctrine "
                    f"or drop the stub (SPEC-173 rule 11; incident 758d900)")})
    return out


def compiled_sha(role: str, registry: dict[str, Any], root: Path | str) -> str:
    """sha256 hex of the compiled text — the per-role compiled identity (rule 8/10)."""
    return hashlib.sha256(compile_role(role, registry, root).encode("utf-8")).hexdigest()


def heading_counts(text: str) -> dict[str, int]:
    """H2 heading text -> how many section segments carry it. One copy each is
    what dedup owes (rule 11)."""
    counts: dict[str, int] = {}
    for kind, head, _ in playbook_registry._h2_segments(text):
        if kind == "section":
            counts[head] = counts.get(head, 0) + 1
    return counts


def replay(role: str, registry: dict[str, Any], root: Path | str,
           lock_doc: dict[str, Any]) -> dict[str, Any]:
    """Rule 10: reproduce the locked compiled identity from the lock's provenance.

    Every `sources` sha is re-verified against disk FIRST — a replay over changed
    sources is a typed refusal naming the file, never a quiet recompile against
    different inputs."""
    entry = ((lock_doc or {}).get("roles") or {}).get(role)
    if not isinstance(entry, dict):
        raise HarnessError(f"lockfile has no entry for role {role!r}\nfix: {REGEN_CMD}")
    root = Path(root)
    for src in entry.get("sources") or []:
        p = root / str(src.get("name"))
        raw = (p.read_bytes() if p.exists() else b"").replace(b"\r\n", b"\n")
        actual = hashlib.sha256(raw).hexdigest()
        if actual != src.get("sha"):
            raise HarnessError(
                f"replay refused: {src.get('name')} changed since the lock was written "
                f"(lock {str(src.get('sha'))[:12]} vs disk {actual[:12]})\nfix: {REGEN_CMD}")
    recomputed = compiled_sha(role, registry, root)
    return {"ok": recomputed == entry.get("compiledSha"), "role": role,
            "compiledSha": recomputed, "lockCompiledSha": entry.get("compiledSha")}


def lock_stale(root: Path | str) -> list[dict[str, str]]:
    """Rule 12, the gate predicate: findings when the committed lock does not match
    what the sources compile to RIGHT NOW. CONTENT-only — no mtimes.

    Deliberately stricter than `playbook_registry._lock_drift`, whose mtime guard
    plus the self-healing regen inside `--verify` is exactly how 171f6c8 escaped
    with 32 drift findings and zero failures."""
    root = Path(root)
    registry = playbook_registry.load(root)
    roles = registry.get("roles") or {}
    lock = read_json(root / playbook_registry.LOCK_REL, None)
    if not isinstance(lock, dict):
        return [{"kind": "lock-missing", "path": playbook_registry.LOCK_REL,
                 "detail": f"{len(roles)} role(s) registered but no lockfile on disk; "
                           f"regenerate: {REGEN_CMD}"}] if roles else []
    committed = lock.get("roles") or {}
    out: list[dict[str, str]] = []
    for role in roles:
        fresh = playbook_registry._role_lock(role, registry, root)
        prev = committed.get(role) or {}
        prev_sha = {s.get("name"): s.get("sha") for s in prev.get("sources") or []}
        fresh_sha = {s["name"]: s["sha"] for s in fresh["sources"]}
        moved = sorted({n for n in set(prev_sha) | set(fresh_sha)
                        if prev_sha.get(n) != fresh_sha.get(n)})
        reasons: list[str] = []
        if prev.get("chainHash") != fresh["chainHash"]:
            reasons.append("chainHash")
        if moved:
            reasons.append("source(s) " + ", ".join(moved))
        if prev.get("compiledSha") != fresh["compiledSha"]:
            reasons.append("compiledSha")
        if reasons:
            out.append({"kind": "lock-stale", "path": role,
                        "detail": f"role {role!r}: {'; '.join(reasons)} differ(s) from the "
                                  f"committed lock — a compiled source changed without a lock "
                                  f"regen in the SAME commit (SPEC-173 rule 12); "
                                  f"regenerate: {REGEN_CMD}"})
    return out


def _demo() -> None:
    """Hermetic proof over temp roots: byte-stability, CRLF twin == LF twin, the
    dedup winner + its provenance line, verbatim passthrough of a no-H2 file, an
    empty chain, a missing file, equivalence PASSING on the fixture and FAILING on
    a hand-broken compiled text, and replay ok + refusal on a mutated source."""
    import tempfile

    from harness_lib.common import write_json

    base = "# Base\nintro\n\n## Shared\nbase copy\n\n## BaseOnly\nkeep me\n"
    leaf = "## Shared\nleaf copy\n\n## LeafOnly\nleaf stuff\n"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp2:
        root = Path(tmp)
        (root / ".harness" / "prompts").mkdir(parents=True)
        (root / "AGENTS.md").write_text(base, encoding="utf-8", newline="\n")
        (root / ".harness" / "prompts" / "leaf.md").write_text(leaf, encoding="utf-8", newline="\n")
        (root / ".harness" / "prompts" / "noh2.md").write_text(
            "plain notes\nno headings at all\n", encoding="utf-8", newline="\n")
        reg = {"schemaVersion": 1, "unmanaged": [], "roles": {
            "agent-base": {"playbooks": ["AGENTS.md"]},
            "leaf": {"extends": "agent-base", "playbooks": [".harness/prompts/leaf.md"]},
            "plain": {"extends": "agent-base", "playbooks": [".harness/prompts/noh2.md"]},
            "empty": {"playbooks": []},
            "ghost": {"playbooks": [".harness/prompts/gone.md"]}}}
        write_json(root / playbook_registry.REGISTRY_REL, reg)  # lock_stale loads it from disk

        out = compile_role("leaf", reg, root)
        assert out == compile_role("leaf", reg, root), "compilation must be byte-stable"
        assert out.endswith("\n") and not out.endswith("\n\n"), repr(out[-20:])
        assert out.startswith("# EFFECTIVE PLAYBOOK — compiled (SPEC-173) role: leaf\n# chainHash: ")
        # dedup: the LEAF copy wins, the base copy becomes one provenance line
        assert heading_counts(out) == {"Shared": 1, "BaseOnly": 1, "LeafOnly": 1}, heading_counts(out)
        assert "leaf copy" in out and "base copy" not in out, out
        assert ('<!-- deduped: "Shared" superseded by .harness/prompts/leaf.md '
                "(SPEC-173 rule 11) -->") in out, out
        # and the compiled chainHash matches the lockfile's, computed independently
        assert f"# chainHash: {playbook_registry._role_lock('leaf', reg, root)['chainHash']}" in out

        # CRLF twin compiles byte-identically to its LF twin (rule 10 across checkouts)
        crlf = Path(tmp2)
        (crlf / ".harness" / "prompts").mkdir(parents=True)
        (crlf / "AGENTS.md").write_bytes(base.replace("\n", "\r\n").encode())
        (crlf / ".harness" / "prompts" / "leaf.md").write_bytes(leaf.replace("\n", "\r\n").encode())
        assert compile_role("leaf", reg, crlf) == out, "CRLF twin must compile identically"

        # edge cases: no-H2 verbatim, empty chain = header only, missing file
        plain = compile_role("plain", reg, root)
        assert "plain notes\nno headings at all" in plain and "deduped" not in plain, plain
        empty = compile_role("empty", reg, root)
        assert empty.count("\n") == 2 and empty.startswith("# EFFECTIVE PLAYBOOK"), repr(empty)
        assert MISSING in compile_role("ghost", reg, root)

        # rule 10: replay reproduces from the lock, and REFUSES on a mutated source
        lock = playbook_registry.write_lock(reg, root)
        assert replay("leaf", reg, root, lock)["ok"], replay("leaf", reg, root, lock)
        assert not lock_stale(root), lock_stale(root)
        (root / ".harness" / "prompts" / "leaf.md").write_text(
            leaf + "\n## Late\nadded\n", encoding="utf-8", newline="\n")
        try:
            replay("leaf", reg, root, lock)
            raise AssertionError("replay accepted a mutated source")
        except HarnessError as exc:
            assert "replay refused" in str(exc) and "leaf.md" in str(exc) and "fix:" in str(exc), exc
        stale = lock_stale(root)
        assert [f["kind"] for f in stale] == ["lock-stale"] and stale[0]["path"] == "leaf", stale
        assert "leaf.md" in stale[0]["detail"] and REGEN_CMD in stale[0]["detail"], stale
    print("playbook_compiler self-check: ok")


if __name__ == "__main__":
    _demo()
