"""Wave-shared context digest (TASK-002 E1 / SPEC-119 v3).

One deterministic, NON-AUTHORITATIVE digest of a workflow's required-read files,
built once at plan time and referenced by every worker packet in opted-in
profiles (``sharedContextDigest: true``). Stdlib only, no LLM. The canonical
files remain the source of truth (same invariant as Graphify structural
discovery); workers open originals on demand. The WORKER_RESULT contract section
is copied VERBATIM so the result contract is never lossy (a lossy contract yields
invalid worker results, which is cost-negative).

Deterministic extraction rule (headings-based): each file's ATX-heading sections
become ``heading + first-paragraph lead`` (capped), except sections whose heading
matches ``_VERBATIM`` which are copied whole. The subagent contract is the one
role-sliced source: digest consumers are workers only, so only the WORKER_RESULT
section is embedded (verbatim) and every other contract section — reducer,
reviewer, HARNESS_RESULT, overlays — is dropped as role-inapplicable. Every
source is stamped with its mtime + sha256 so a stale digest cannot propagate
silently.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

DIGEST_NAME = "context-digest.md"

# Heading substrings whose sections are copied verbatim (never lead-truncated).
_VERBATIM = ("WORKER_RESULT for workflow workers",)
_CONTRACT_REL = ".harness/prompts/subagent-contract.md"
_LEAD_CHARS = 200  # per-section lead cap for non-verbatim sections
_HEADING_RE = re.compile(r"^#{1,6}\s")


def required_reads(workflow_type: str) -> list[str]:
    """Canonical required-read set injected into every worker packet.

    Single source of truth shared by the packet renderer (write_worker_prompt),
    the digest builder, and the token-audit requiredReads estimate.
    """
    return [
        "AGENTS.md",
        ".harness/workflows/WORKFLOWS.md",
        ".harness/prompts/subagent-contract.md",
        "specs/00-universal/structural-discovery.md",
        "specs/00-universal/agentic-map-reduce.md" if workflow_type == "map-reduce"
        else "specs/00-universal/agentic-fork-join.md",
    ]


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading_line, body) by ATX headings. Text before the
    first heading is returned under an empty heading."""
    out: list[tuple[str, list[str]]] = []
    head, body = "", []
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            out.append((head, body))
            head, body = line, []
        else:
            body.append(line)
    out.append((head, body))
    return [(h, "\n".join(b).strip()) for h, b in out if h or "\n".join(b).strip()]


def _lead(body: str, cap: int = _LEAD_CHARS) -> str:
    """First non-empty paragraph, whitespace-collapsed and capped. Deterministic."""
    para = next((blk.strip() for blk in re.split(r"\n\s*\n", body) if blk.strip()), "")
    para = " ".join(para.split())
    return (para[:cap].rstrip() + " …") if len(para) > cap else para


def _digest_for(rel: str, text: str) -> str:
    lines = [f"### source: `{rel}`", ""]
    for head, body in _split_sections(text):
        if not head:  # preamble before first heading
            lead = _lead(body)
            if lead:
                lines += [lead, ""]
            continue
        htext = head.lstrip("#").strip()
        lines.append(head)
        if any(v in htext for v in _VERBATIM):
            lines += ["", body, ""]  # verbatim contract
        else:
            lead = _lead(body)
            if lead:
                lines.append(lead)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _contract_slice(rel: str, text: str) -> str | None:
    """Role slice of the subagent contract: the WORKER_RESULT section VERBATIM and
    nothing else. Keep-list, not drop-list — a contract section added tomorrow stays
    out of the worker digest by construction. Returns ``None`` when the heading is
    absent (renamed contract) so the caller can fail open to the whole-file digest.
    Same stance as ``tools/openai_worker._read_contract`` (SPEC-119 v10).
    ponytail: worker slice hardcoded — digest consumers are workers only today;
    add a per-role heading map when a second consumer (reducer/reviewer) appears.
    """
    for head, body in _split_sections(text):
        if head and _VERBATIM[0] in head:
            return f"### source: `{rel}`\n\n{head}\n\n{body}\n"
    return None


def build_digest(root: Path, base: Path, workflow_type: str) -> Path:
    """Build ``<base>/context-digest.md`` from the required-read files. Returns
    the path. Overwrites any prior digest (rebuilt each plan)."""
    reads = required_reads(workflow_type)
    header = [
        "# Wave-shared context digest (NON-AUTHORITATIVE)",
        "",
        "Deterministic, headings-based summary of this wave's required-read files, "
        "built once at plan time (stdlib only, no LLM). **The canonical files below "
        "remain the source of truth** — this digest may lag them, so open any "
        "original on demand to verify (same invariant as Graphify structural "
        "discovery, `specs/00-universal/structural-discovery.md`). Only the worker's "
        "result-contract section of the subagent contract is embedded, verbatim.",
        "",
        "## Generated from",
        "",
        "| source | mtime (UTC) | sha256 (first 16) |",
        "|---|---|---|",
    ]
    bodies: list[str] = []
    trace: list[str] = []
    for rel in reads:
        p = root / rel
        if not p.exists():
            header.append(f"| `{rel}` | (missing) | (missing) |")
            continue
        raw = p.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header.append(f"| `{rel}` | {mtime} | `{digest}` |")
        text = raw.decode("utf-8", "replace")
        sliced = _contract_slice(rel, text) if rel == _CONTRACT_REL else None
        if sliced is None and rel == _CONTRACT_REL:
            # fail open: planning must never break on a renamed contract heading.
            trace.append(f'> trace: role-slice fallback — heading "## {_VERBATIM[0]}" '
                         f"not found in {rel}; whole contract embedded")
        bodies.append(sliced if sliced is not None else _digest_for(rel, text))
    if trace:  # blockquote, never a `|` row — check_digest_drift parses 5-cell rows here.
        header += ["", *trace]
    content = "\n".join(header) + "\n\n---\n\n" + "\n\n---\n\n".join(bodies) + "\n"
    out = base / DIGEST_NAME
    out.write_text(content, encoding="utf-8")
    return out


def check_digest_drift(root: Path, base: Path) -> list[dict[str, str]]:
    """Re-hash the sources a digest was built from and report any that changed.

    Closes the loop the ``## Generated from`` stamp table opens (SPEC-119 v3):
    ``build_digest`` records each source's sha256[:16] but nothing re-reads it, so
    a stale digest could steer a whole wave silently. This re-hashes each stamped
    source and returns one entry per drifted source — deterministic, stdlib-only,
    no re-summarization. Warn-only by contract: callers surface it, never block on
    it (the canonical files remain the source of truth). Returns ``[]`` when the
    digest is absent or every source still matches its stamp.
    """
    digest = base / DIGEST_NAME
    if not digest.exists():
        return []
    drift: list[dict[str, str]] = []
    for line in digest.read_text(encoding="utf-8", errors="replace").splitlines():
        cells = [c.strip() for c in line.split("|")]
        # table rows render as: | `src` | mtime | `sha` |  → 5 cells incl. edges
        if len(cells) != 5 or not (cells[1].startswith("`") and cells[1].endswith("`")):
            continue
        rel = cells[1].strip("`")
        stamped = cells[3].strip("`")
        if stamped == "(missing)":  # already absent when the digest was built
            continue
        p = root / rel
        if not p.exists():
            drift.append({"source": rel, "stamped": stamped, "current": "(missing)", "reason": "removed"})
            continue
        current = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        if current != stamped:
            drift.append({"source": rel, "stamped": stamped, "current": current, "reason": "changed"})
    return drift


if __name__ == "__main__":  # ponytail: self-check — verbatim contract + hash stamp survive extraction
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(__file__).resolve().parents[2]
        base = Path(td)
        d = build_digest(root, base, "fork-join")
        txt = d.read_text(encoding="utf-8")
        assert "NON-AUTHORITATIVE" in txt, "missing non-authoritative note"
        assert "## Generated from" in txt and "sha256" in txt, "missing hash stamps"
        # verbatim WORKER_RESULT schema keys must survive whole
        for key in ("workflowId", "workerId", "sourceFilesVerified", "graphify"):
            assert key in txt, f"verbatim contract lost {key}"
        # role slice: every contract section a worker cannot act on stays out
        for head in ("## HARNESS_RESULT", "## REDUCE_RESULT for reducers",
                     "## REVIEWER_RESULT for post-reduce reviewers",
                     "## Controlled write workflow overlay",
                     "## Verification policy for implementers (token economy)"):
            assert head not in txt, f"role-inapplicable contract section leaked: {head}"
        assert len(txt) < sum(len((root / r).read_bytes()) for r in required_reads("fork-join")), "digest not smaller than sources"
        # drift: a fresh digest is drift-free; corrupting one stamped sha surfaces exactly that source
        assert check_digest_drift(root, base) == [], "fresh digest must show no drift"
        rel0 = required_reads("fork-join")[0]
        good = hashlib.sha256((root / rel0).read_bytes()).hexdigest()[:16]
        d.write_text(txt.replace(f"| `{good}` |", "| `deadbeefdeadbeef` |", 1), encoding="utf-8")
        drift = check_digest_drift(root, base)
        assert [x["source"] for x in drift] == [rel0] and drift[0]["reason"] == "changed", f"drift not detected: {drift}"
        # fail open: a renamed WORKER_RESULT heading embeds the whole contract + a trace line
        # (never a raise — build_digest runs inside `workflow plan`). Mutates a COPY only.
        fake, fbase = Path(td) / "fakeroot", Path(td) / "fallback"
        fbase.mkdir()
        for rel in required_reads("fork-join"):
            (fake / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, fake / rel)
        c = fake / _CONTRACT_REL
        c.write_text(c.read_text(encoding="utf-8").replace(f"## {_VERBATIM[0]}", "## WORKER_RESULTX renamed", 1),
                     encoding="utf-8")
        ftxt = build_digest(fake, fbase, "fork-join").read_text(encoding="utf-8")
        assert "## REDUCE_RESULT for reducers" in ftxt, "fallback did not embed the whole contract"
        assert "> trace: role-slice fallback" in ftxt, "fallback trace line missing"
        assert check_digest_drift(fake, fbase) == [], "fallback trace line broke the stamp table"
        print(f"OK digest {len(txt)} chars from {sum(len((root / r).read_bytes()) for r in required_reads('fork-join'))} source chars; drift-check ok")
