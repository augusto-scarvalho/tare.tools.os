"""Pre-commit validation-gate stamp: fingerprint + policy + stamp + decision (SPEC-137).

Single home shared by the pre-commit hook (`tools/hooks/precommit_validation_gate.py`)
and the CLI (`harness.py validate --staged`), so the fingerprint, the profile map,
the stamp shape, and the block decision have ONE implementation and cannot drift.

Everything is root-parameterized (no module constant bound to the harness repo):
the hook validates the git worktree it is invoked in, the CLI validates the harness
repo, and the hermetic scenario validates a scratch temp repo — all through the same
functions. Pure stdlib; git is read through `git ls-files -s` / `git ls-tree` so the
manifest is built from index/tree blobs, never disk mtimes.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_lib import processes, responsibility
from harness_lib.common import ROOT, now, parse_iso_datetime, write_json

SURFACE_DEFAULT = ["scripts", "tools", "testing", "specs"]
FIX_COMMAND = "python scripts/harness.py validate --staged"
RECKON_FIX_COMMAND = "python scripts/harness.py reckon --record --verdict no-blocker"
MUTATE_FIX_COMMAND = "python scripts/harness.py oracle mutate"
MUTATE_WAIVE_COMMAND = 'python scripts/harness.py oracle waive --reason "…"'
# Files hashed into validator_version: a change to the gate runner or the policy
# config stales every old stamp (OVERSEER-NARROWED to the gate logic + config, so
# unrelated harness_lib edits do NOT re-stale every stamp).
# The last three are the gate's CONTROL-PLANE (row gate-controlplane-excluded,
# 2026-07-27): the git-hook shims that `exec` the gate, the hook body itself, and
# THIS module -- which defines the manifest, the exclude matcher and the profile
# map. All three could be edited without staling a single stamp until now; the
# 3-line shim in particular could be replaced by `exit 0`, killing gate + reckon +
# post-commit audit at once. `tools/git-hooks/**` also left the policy `exclude`.
VALIDATOR_INPUTS = [
    "scripts/spec_test_gate.py",
    "scripts/harness-test.py",
    "scripts/harness_lib/gate_*.py",
    ".harness/project.json",
    # The audit leg's band ladder + trivial floor (owner decision a, 2026-08-03):
    # a policy edit that changes how many seats a surface owes MUST stale the
    # stamps taken under the old numbers, exactly like project.json does.
    ".harness/audit-policy.json",
    "scripts/harness_lib/validation_stamp.py",
    "tools/hooks/precommit_validation_gate.py",
    "tools/git-hooks/*",
]


# --- paths (root-relative) ---------------------------------------------------

def _quality(root: Path) -> Path:
    return root / ".harness" / "state" / "quality-state.json"


def _runs(root: Path) -> Path:
    return root / ".harness" / "runs" / "validation-results.jsonl"


def _audit(root: Path) -> Path:
    return root / ".harness" / "runs" / "commit-audit.jsonl"


def _reckon_runs(root: Path) -> Path:
    return root / ".harness" / "runs" / "reckon-results.jsonl"


def _audit_results(root: Path) -> Path:
    # NOT `_audit` (that name is the OVERRIDE ledger, commit-audit.jsonl, right
    # above) and NOT under .harness/state: the scenarios gate-hold moves state
    # aside, so a verdict written there is erased by the gate it audits.
    return root / ".harness" / "runs" / "audit-results.jsonl"


def _bypass(root: Path) -> Path:
    return root / ".harness" / "runs" / "gate-bypass.jsonl"


def _git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    # Route through the processes.py mediation layer (bounded timeout + hidden
    # console + tree-kill); a raw subprocess.run here trips the spawn ratchet.
    return processes.run_quiet(["git", *args], timeout=timeout, cwd=str(root),
                               capture_output=True, text=True, encoding="utf-8", errors="replace")


def _rev_ok(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", ref).returncode == 0


# --- policy ------------------------------------------------------------------

def load_policy(root: Path) -> dict | None:
    """`.harness/project.json#/precommitValidation`; None when absent or disabled.

    Disabled/absent IS the migration window (requirement 8): the gate treats it as
    'not adopted' and passes. A corrupt project.json also degrades to None (fail-open;
    the surface-unchanged short-circuit and the disabled window both pass anyway)."""
    try:
        proj = json.loads((root / ".harness" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pv = proj.get("precommitValidation")
    if not isinstance(pv, dict) or not pv.get("enabled"):
        return None
    return pv


# --- manifest + fingerprint --------------------------------------------------

EVIDENCE_EXCLUSIONS = [
    ".harness/state/execution-receipts/*",
    ".harness/state/commit-transactions.jsonl",
    ".harness/runs/*",
]



def _excluded(path: str, policy: dict) -> bool:
    # fnmatchcase (not fnmatch) so the POSIX path is matched verbatim on every OS
    # (fnmatch normalizes case AND separators on Windows, which would break globs).
    if any(fnmatch.fnmatchcase(path, g) for g in EVIDENCE_EXCLUSIONS):
        return True
    return any(fnmatch.fnmatchcase(path, g) for g in (policy.get("exclude") or []))


def staged_manifest(root: Path, policy: dict) -> list[str]:
    """Canonical `"<mode> <sha>\t<path>"` lines from the git INDEX under the
    surface roots, exclusions dropped, sorted. Index blobs => immune to OneDrive
    mtime noise; a real content change moves the sha, nothing else does."""
    roots = policy.get("surfaceRoots") or SURFACE_DEFAULT
    proc = _git(root, "ls-files", "-s", "--", *roots)
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        meta, tab, path = raw.partition("\t")
        if not tab:
            continue
        fields = meta.split()  # [mode, sha, stage]
        if len(fields) < 2 or _excluded(path, policy):
            continue
        lines.append(f"{fields[0]} {fields[1]}\t{path}")
    return sorted(lines)



def _tree_manifest(root: Path, ref: str, policy: dict) -> list[str]:
    """Same canonical form as staged_manifest, from a committed tree (ls-tree)."""
    roots = policy.get("surfaceRoots") or SURFACE_DEFAULT
    proc = _git(root, "ls-tree", "-r", ref, "--", *roots)
    if proc.returncode != 0:
        return []
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        meta, tab, path = raw.partition("\t")
        if not tab:
            continue
        fields = meta.split()  # [mode, type, sha]
        if len(fields) < 3 or _excluded(path, policy):
            continue
        lines.append(f"{fields[0]} {fields[2]}\t{path}")
    return sorted(lines)


def head_manifest(root: Path, policy: dict) -> list[str]:
    return _tree_manifest(root, "HEAD", policy)


def manifest_fingerprint(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _changed_paths(staged: list[str], head: list[str]) -> list[str]:
    diff = set(staged).symmetric_difference(set(head))
    return sorted({line.split("\t", 1)[1] for line in diff if "\t" in line})


def validator_version(root: Path) -> str:
    """sha256 over the sorted git index entries of the gate runner + gate_*.py +
    project.json. A validator or config change stales old stamps (requirement 5)."""
    proc = _git(root, "ls-files", "-s", "--", *VALIDATOR_INPUTS)
    lines = sorted(l for l in proc.stdout.splitlines() if l.strip())
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def validator_version_from_tree(root: Path, tree_ish: str) -> str | None:
    """The FROZEN gate_version: `validator_version` computed from a committed tree
    (the frozen candidate P) instead of the live index, so a post-freeze index/
    worktree mutation of a validator input cannot move the skip-cache key and enable
    an unsound skip of a scenario belonging to P. Emits the SAME canonical
    `"<mode> <sha> <stage>\\t<path>"` lines as `validator_version` (stage 0), so the
    two agree byte-for-byte when the tree's validator inputs match the index's.

    Returns None on ANY git failure — NEVER the live `validator_version(root)`: a
    candidate-bound caller that cannot prove P's version must fail closed or disable
    skipping, not silently switch the skip-cache subject to the live index."""
    try:
        proc = _git(root, "ls-tree", "-r", tree_ish, "--", *VALIDATOR_INPUTS)
    except Exception:  # noqa: BLE001 -- cannot read P -> None (caller decides; no live fallback)
        return None
    if proc.returncode != 0:
        return None
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        meta, tab, path = raw.partition("\t")
        if not tab:
            continue
        fields = meta.split()  # [mode, type, sha]
        if len(fields) >= 3:
            lines.append(f"{fields[0]} {fields[2]} 0\t{path}")
    return hashlib.sha256(("\n".join(sorted(l for l in lines if l.strip())) + "\n").encode("utf-8")).hexdigest()


def required_profile(changed_paths: list[str], policy: dict) -> dict:
    """Union of the profiles whose roots match a changed path -> {names, gates}."""
    profiles = policy.get("profiles") or {}
    names: list[str] = []
    gates: list[str] = []
    for path in changed_paths:
        for pname, pcfg in profiles.items():
            proots = pcfg.get("roots") or []
            if any(path == r or path.startswith(r + "/") for r in proots):
                if pname not in names:
                    names.append(pname)
                for g in (pcfg.get("gates") or []):
                    if g not in gates:
                        gates.append(g)
    return {"names": sorted(names), "gates": sorted(gates)}


# --- quality-state read ------------------------------------------------------

def _read_quality(root: Path) -> dict | None:
    """Parsed quality-state, or None when missing OR corrupt (both BLOCK when the
    surface changed — an unreadable stamp is not a validated stamp)."""
    try:
        return json.loads(_quality(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- decision (the hook's stamp check) ---------------------------------------

def _block_message(reason: str, changed: list[str], fp: str, lv: dict | None,
                   req: dict) -> str:
    out = ["pre-commit validation gate: BLOCKED", f"reason: {reason}",
           f"changed surface paths ({len(changed)}):"]
    out += [f"  - {p}" for p in changed[:10]]
    if len(changed) > 10:
        out.append(f"  ... and {len(changed) - 10} more")
    if isinstance(lv, dict) and lv:
        sf = ((lv.get("stagedFingerprint") or {}).get("sha256") or "")
        out.append(f"last validation: gate={lv.get('gate')} status={lv.get('status')} "
                   f"at={lv.get('validatedAt')} fingerprint={sf[:12]}")
    else:
        out.append("last validation: none on record")
    out.append(f"current staged fingerprint: {fp[:12]} (required gates: "
               f"{', '.join(req.get('gates') or []) or 'none'})")
    out.append(f"fix: {FIX_COMMAND}")
    out.append("git commit --no-verify skips this gate; the bypass is recorded by "
               "the post-commit audit and surfaces in the next gate run")
    return "\n".join(out)


def _bypass_key(kind: str, head_sha: str, fp: str | None, paths: list[str]) -> str:
    """Identity of ONE would-be bypass. The staged path set is part of it: the surface
    fingerprint is by definition unchanged across every bypass at the same HEAD, so
    keying on it alone would swallow a second, different out-of-surface change."""
    digest = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()[:12]
    return f"{kind}|{head_sha}|{fp or '-'}|{digest}"


def log_bypass(root: Path, kind: str, staged: list[str] | None) -> None:
    """Append ONE row per gate short-circuit to a durable, hold-immune ledger.

    Row gate-bypass-ledger (2026-07-27): `"staged surface unchanged vs HEAD"` and
    the not-adopted branch were a tuple and a print -- nothing was persisted -- so
    four registrations of the SAME root cause in five days were each found by hand,
    by archaeology. `delivery_bar_advisor` sees these paths but its exit code is
    always 0. This is the missing signal, not a fix: the surface itself must not be
    redesigned until the ledger has data (SPEC-158 / ToC -- can't exploit a
    constraint you can't see).

    `.harness/runs`, NOT `.harness/state`, for the same reason `_ledger_verdict_for_fp`
    documents at length: the scenarios gate-hold moves `.harness/state` aside, so the
    gate would erase its own audit trail (and a stranded hold would drop it entirely).
    Cost: local-only, `.gitignore:38`.

    Nothing staged is NOT a bypass, just an idle tree -- those return before writing,
    so every row in the ledger is a real would-be commit that the gate waved through.
    Deduped on `_bypass_key` against the tail: check_staged runs on every commit AND
    on every `verify-status`, and one commit must not read as N bypasses.
    FAIL-OPEN on every error -- an unwritable ledger never wedges a commit."""
    try:
        # What actually slipped through. With staged==head every staged path is by
        # definition either outside surfaceRoots or excluded -- both are bypasses.
        proc = _git(root, "diff", "--cached", "--name-only")
        paths = sorted(p for p in (ln.strip() for ln in (proc.stdout or "").splitlines()) if p)
        if not paths:
            return
        head_sha = (_git(root, "rev-parse", "HEAD").stdout or "").strip() or "-"
        fp = manifest_fingerprint(staged) if staged is not None else None
        key = _bypass_key(kind, head_sha, fp, paths)
        path = _bypass(root)
        # Tail window, not just the last line: one hook run calls check_staged AND
        # check_reckon, so the previous row for THIS kind is never the last one. A
        # bounded window is deliberate -- it suppresses the repeat-polling of a
        # commit in progress, and a bypass that recurs many rows later is a genuinely
        # new occurrence that deserves its own row.
        try:
            with path.open("rb") as f:
                f.seek(max(0, path.stat().st_size - 8192))
                tail = f.read().decode("utf-8", "replace").splitlines()
            for raw in tail:  # a seek can land mid-line; unparsable lines just miss
                try:
                    if json.loads(raw).get("key") == key:
                        return
                except ValueError:
                    continue
        except OSError:
            pass
        row = {"at": now(), "kind": kind, "key": key, "headSha": head_sha[:12],
               "stagedFingerprint": fp, "bypassedCount": len(paths),
               "bypassedPaths": paths[:20]}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — observability must never block a commit
        pass


def check_staged(root: Path) -> tuple[str, str]:
    """('pass'|'block'|'warn', reason). Fail closed ONLY on real inconsistency."""
    policy = load_policy(root)
    if policy is None:
        # The CHEAPEST bypass of all -- logged too, else it stays outside the record.
        log_bypass(root, "gate:not-adopted", None)
        return ("warn", "pre-commit validation gate not adopted; migration window")
    staged = staged_manifest(root, policy)
    head = head_manifest(root, policy)
    if staged == head:
        log_bypass(root, "gate:surface-unchanged", staged)
        return ("pass", "staged surface unchanged vs HEAD")
    changed = _changed_paths(staged, head)
    fp = manifest_fingerprint(staged)
    req = required_profile(changed, policy)
    qs = _read_quality(root)
    lv = qs.get("lastValidation") if isinstance(qs, dict) else None
    if qs is None:
        reason = "quality-state.json missing or corrupt"
    elif not isinstance(lv, dict) or not lv.get("stagedFingerprint"):
        reason = "no staged-snapshot validation on record"
    elif lv.get("status") != "pass":
        reason = f"last validation status is {lv.get('status')!r}, not pass"
    elif (lv.get("stagedFingerprint") or {}).get("sha256") != fp:
        reason = "staged fingerprint does not match the last validation"
    elif lv.get("policyVersion") != policy.get("policyVersion"):
        reason = "policyVersion changed since the last validation"
    elif lv.get("validatorVersion") != validator_version(root):
        reason = "validatorVersion changed since the last validation"
    elif not set(req["gates"]).issubset(set((lv.get("executedProfile") or {}).get("gates") or [])):
        reason = "executed gates do not cover the required profile"
    else:
        return ("pass", f"staged surface validated ({fp[:12]})")
    return ("block", _block_message(reason, changed, fp, lv if isinstance(lv, dict) else None, req))


# --- reckon dimension (the second commit-ledger half, SPEC-157) --------------
# The RECKON gate MIRRORS check_staged but keys on `lastReckon` (a reviewer's
# "no-blocker" verdict) instead of `lastValidation` (a gate pass). It is
# CONSERVATIVE: a reckon is required only when the staged surface touches a
# profile flagged `reckon: true` in the policy (the risk-bearing roots -- engine
# / gate-internals / hooks). Everything else (tests, specs/docs) is zero-friction.

def reckon_required(policy: dict, req: dict) -> bool:
    """True when any profile matched by the staged change carries `reckon: true`.
    No flagged profile matched (or none flagged at all) => not required => the
    reckon gate passes (the migration window: the owner opts a profile in later)."""
    profiles = policy.get("profiles") or {}
    return any((profiles.get(name) or {}).get("reckon") for name in (req.get("names") or []))


def reckon_reach(root: Path, changed: list[str]) -> dict | None:
    """Blast radius of the staged surface for the reckon brief (row
    reckon-affected-map-input, owner 2026-07-22): affected-scenario reach from
    the SAME gate_affected map the skip machinery uses, so the reviewer VERIFIES
    blast radius instead of re-deriving it by reading. Fail-open: None on any
    doubt (no graph, import error) — the brief simply omits the reach block; the
    reckon gate itself never depends on this."""
    try:
        from harness_lib import gate_affected
        graph = gate_affected.load_graph()
        if graph is None:
            return None
        aff = gate_affected.affected_scenarios(root, changed, graph)
        if not aff:
            return None
        hit = sorted(s for s, v in aff.items() if v)
        return {"affected": len(hit), "total": len(aff), "sample": hit[:8]}
    except Exception:  # noqa: BLE001 — advisory input, never wedges the gate
        return None


def _ledger_verdict_for_fp(root: Path, fp: str) -> str | None:
    """Latest recorded reckon verdict for the EXACT staged fingerprint, read from the
    durable ledger `.harness/runs/reckon-results.jsonl`. None when no row matches.

    reckon-results.jsonl lives OUTSIDE the scenarios gate-hold set: scenario_isolation
    holds `.harness/state` (home of quality-state.json / lastReckon) but never
    `.harness/runs`, so this ledger survives the hold that can desync lastReckon (moved
    aside for the gate's duration; a write during that window -- or a hard-kill that
    strands the hold -- drops lastReckon). It is the SAME evidence detect_reckon_override
    already trusts. Append-only chronological order => the last matching row wins."""
    verdict: str | None = None
    try:
        lines = _reckon_runs(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("stagedFingerprint") == fp and row.get("verdict") in ("no-blocker", "blocker"):
            verdict = row["verdict"]
    return verdict


def _reckon_block_message(reason: str, changed: list[str], fp: str, lr: dict | None,
                          req: dict, reach: dict | None = None) -> str:
    out = ["pre-commit reckon gate: BLOCKED", f"reason: {reason}",
           f"risk-bearing changed surface paths ({len(changed)}):"]
    out += [f"  - {p}" for p in changed[:10]]
    if len(changed) > 10:
        out.append(f"  ... and {len(changed) - 10} more")
    if reach:
        sample = ", ".join((reach.get("sample") or [])[:5])
        out.append(f"affected reach (gate_affected map): {reach['affected']}/{reach['total']} "
                   f"scenarios{' — e.g. ' + sample if sample else ''}")
        out.append("  -> hand this reach to the reviewer: it VERIFIES blast radius instead "
                   "of deriving it; calibrate depth by reach (small -> hunk-only, large -> deep)")
    if isinstance(lr, dict) and lr:
        sf = ((lr.get("stagedFingerprint") or {}).get("sha256") or "")
        out.append(f"last reckon: verdict={lr.get('verdict')} at={lr.get('at')} "
                   f"fingerprint={sf[:12]}")
    else:
        out.append("last reckon: none on record")
    out.append(f"current staged fingerprint: {fp[:12]} (risk profile(s): "
               f"{', '.join(req.get('names') or []) or 'none'})")
    out.append(f"fix: {RECKON_FIX_COMMAND}")
    out.append("git commit --no-verify skips this gate; the bypass is recorded by "
               "the post-commit audit and surfaces in the next gate run")
    return "\n".join(out)


def check_reckon(root: Path) -> tuple[str, str]:
    """('pass'|'block'|'warn', reason). FAIL-OPEN by design: block ONLY when the
    staged surface is POSITIVELY risk-bearing AND changed AND lacks a matching
    no-blocker reckon. Not-adopted / policy-absent / unreadable state all degrade
    to warn/pass -- a broken reckon gate must never wedge a commit (the validation
    gate already fails CLOSED on missing state, so this half stays additive)."""
    policy = load_policy(root)
    if policy is None:
        log_bypass(root, "reckon:not-adopted", None)
        return ("warn", "pre-commit reckon gate not adopted; migration window")
    staged = staged_manifest(root, policy)
    head = head_manifest(root, policy)
    if staged == head:
        log_bypass(root, "reckon:surface-unchanged", staged)
        return ("pass", "staged surface unchanged vs HEAD")
    changed = _changed_paths(staged, head)
    req = required_profile(changed, policy)
    if not reckon_required(policy, req):
        return ("pass", "staged surface not risk-bearing; no reckon required")
    fp = manifest_fingerprint(staged)
    qs = _read_quality(root)
    if qs is None:
        # The gate's OWN state is unreadable -> degrade (the validation gate blocks
        # on this same condition, so the commit is still gated; this half never
        # fails closed on its own error).
        return ("warn", "quality-state.json missing or corrupt; reckon gate degraded")
    lr = qs.get("lastReckon")
    lr_ok = isinstance(lr, dict) and bool(lr.get("stagedFingerprint"))
    # An explicit on-record blocker verdict blocks regardless of the durable ledger --
    # a reviewer saying "there IS a blocker" must never be rescued away.
    explicit_blocker = lr_ok and lr.get("verdict") not in (None, "no-blocker")
    if lr_ok and lr.get("verdict") == "no-blocker" \
            and (lr.get("stagedFingerprint") or {}).get("sha256") == fp:
        producer = lr.get("producer") or {}
        mode = producer.get("mode") or ("MECHANIZED_RECKON_PROOF" if lr.get("actor") == "harness-kernel" else "MANUAL_ATTESTATION")
        if mode == "MANUAL_ATTESTATION" and not policy.get("allowManualReckonAttestation"):
            return ("warn", f"staged surface reckoned no-blocker ({fp[:12]}; MANUAL_ATTESTATION)")
        return ("pass", f"staged surface reckoned no-blocker ({fp[:12]}; mode {mode})")

    # lastReckon (quality-state.json) does NOT speak to THIS surface (absent, or keyed
    # to a stale fingerprint). The scenarios gate-hold moves quality-state.json aside for
    # its duration, so a write in that window -- or a hard-kill that strands the hold --
    # can desync lastReckon from the reckon that really happened, which SURVIVES in the
    # durable, hold-immune ledger. Rescue from it, but never past an explicit blocker.
    if not explicit_blocker and _ledger_verdict_for_fp(root, fp) == "no-blocker":
        return ("pass", f"staged surface reckoned no-blocker ({fp[:12]}; durable ledger)")
    if not lr_ok:
        reason = "no reckon on record for the staged surface"
    elif lr.get("verdict") != "no-blocker":
        reason = f"last reckon verdict is {lr.get('verdict')!r}, not no-blocker"
    else:
        reason = "staged fingerprint does not match the last reckon"
    return ("block", _reckon_block_message(reason, changed, fp,
                                           lr if isinstance(lr, dict) else None, req,
                                           reach=reckon_reach(root, changed)))


# --- mutate dimension (the THIRD commit-ledger leg) --------------------------
# The mutation-probe oracle shipped observe-only and reserved this: "a future gate
# check is the control phase". This is that phase. It MIRRORS check_reckon --
# same manifest/fingerprint/profile map, same conservative scope (only surfaces
# that already require a reckon), same fail-open -- and reads the shared defect
# sink as DATA: importing mutation_probe here would be an import cycle (that
# module imports THIS one for the fingerprint).
# Deliberately does NOT call log_bypass: the bypass ledger's kind-set is asserted
# exactly by pvg-6, and a third dimension of the same short-circuit adds noise,
# not signal (the gate/reckon rows already record every waved-through commit).

def _defect_sink(root: Path) -> Path:
    return root / ".harness" / "state" / "defect-telemetry.jsonl"


def _mutate_rows(root: Path, fp: str) -> tuple[dict | None, dict | None]:
    """(newest mutate-run, newest mutate-waiver) for the EXACT staged fingerprint.
    Append-only chronological => the last matching row wins (the
    _ledger_verdict_for_fp idiom). An ABSENT sink is 'never probed here' (a block,
    not an error); an unreadable or malformed one RAISES so the caller degrades to
    warn -- a gate that cannot read its evidence must not pretend to a verdict."""
    try:
        lines = _defect_sink(root).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return (None, None)
    run: dict | None = None
    waiver: dict | None = None
    for raw in lines:
        if not raw.strip():
            continue
        row = json.loads(raw)  # ValueError -> malformed sink -> caller warns
        if not isinstance(row, dict) or row.get("stagedFingerprint") != fp:
            continue
        if row.get("kind") == "mutate-run":
            run = row
        elif row.get("kind") == "mutate-waiver":
            waiver = row
    return (run, waiver)


def _mutate_block_message(reason: str, changed: list[str], fp: str, req: dict) -> str:
    out = ["pre-commit mutate gate: BLOCKED", f"reason: {reason}",
           f"risk-bearing changed surface paths ({len(changed)}):"]
    out += [f"  - {p}" for p in changed[:10]]
    if len(changed) > 10:
        out.append(f"  ... and {len(changed) - 10} more")
    out.append(f"current staged fingerprint: {fp[:12]} (risk profile(s): "
               f"{', '.join(req.get('names') or []) or 'none'})")
    out.append(f"fix: {MUTATE_FIX_COMMAND}")
    out.append(f"or record the judged tolerance: {MUTATE_WAIVE_COMMAND}")
    return "\n".join(out)


def check_mutate(root: Path) -> tuple[str, str]:
    """('pass'|'block'|'warn', reason). FAIL-OPEN by design (mirror of
    check_reckon): block ONLY when the staged surface is POSITIVELY risk-bearing
    AND changed AND either was never probed on this exact fingerprint or carries
    unwaived survivors. Not-adopted, non-risk surfaces, and ANY internal error
    (unreadable or malformed sink) degrade to pass/warn -- a broken mutate gate
    must never wedge a commit. The verb `oracle mutate` still never fails: the
    enforcement lives HERE, in the commit join."""
    try:
        policy = load_policy(root)
        if policy is None:
            return ("warn", "pre-commit mutate gate not adopted; migration window")
        staged = staged_manifest(root, policy)
        head = head_manifest(root, policy)
        if staged == head:
            return ("pass", "staged surface unchanged vs HEAD")
        changed = _changed_paths(staged, head)
        req = required_profile(changed, policy)
        if not reckon_required(policy, req):
            return ("pass", "staged surface not risk-bearing; no mutation probe required")
        fp = manifest_fingerprint(staged)
        run, waiver = _mutate_rows(root, fp)
        if run is None:
            return ("block", _mutate_block_message(
                "no oracle mutate run against the staged surface", changed, fp, req))
        survived = int((run.get("counts") or {}).get("survived") or 0)
        if survived == 0:
            return ("pass", f"staged surface probed, no mutants survived ({fp[:12]})")
        if waiver is not None:
            return ("warn", f"{survived} surviving mutant(s) WAIVED for this surface "
                            f"({fp[:12]}): {waiver.get('reason')}")
        return ("block", _mutate_block_message(
            f"{survived} mutant(s) SURVIVED the linked scenario on the staged surface",
            changed, fp, req))
    except Exception as exc:  # noqa: BLE001 — fail-open: never wedge a commit
        return ("warn", f"mutate gate degraded ({type(exc).__name__}); not blocking")


# --- audit dimension (the FOURTH commit-ledger leg) --------------------------
# The mechanized pre-merge audit (adversarial N-seat review) joined to the commit
# gate. MIRRORS check_reckon: same manifest/fingerprint/profile map, same
# fail-open posture, verdict keyed on the staged fingerprint so a restage stales
# the record for free. It is CONDITIONAL twice over: the surface must be
# risk-bearing (reckon_required) AND above the trivial floor of
# `.harness/audit-policy.json` -- below either, the leg costs nothing.
# The seat machinery lives in harness_lib/audit_leg.py and is imported LAZILY
# below (audit_leg imports THIS module at import time; the lazy direction breaks
# the cycle, the reckon_reach/log_rotation idiom).
# Deliberately does NOT call log_bypass, for the same reason check_mutate does
# not: pvg-6 asserts the bypass ledger's exact kind-set, and a fourth dimension
# of the same short-circuit adds noise, not signal.

AUDIT_FIX_COMMAND = "python scripts/harness.py audit run   (then: audit collect)"
AUDIT_RECORD_COMMAND = "python scripts/harness.py audit record --verdict ship"
AUDIT_WAIVE_COMMAND = 'python scripts/harness.py audit waive --reason "…"'


def _audit_row_for_fp(root: Path, fp: str) -> dict | None:
    """Latest recorded audit row for the EXACT staged fingerprint, from the durable
    ledger `.harness/runs/audit-results.jsonl`. None when no row matches.

    Same hold-immunity argument as _ledger_verdict_for_fp: quality-state.json
    (home of lastAudit) is moved aside by the scenarios gate-hold, this ledger is
    not. Append-only chronological order => the last matching row wins."""
    row_out: dict | None = None
    try:
        lines = _audit_results(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("stagedFingerprint") != fp:
            continue
        if row.get("verdict") in ("ship", "block", "waived"):
            row_out = row
    return row_out


def _audit_block_message(reason: str, changed: list[str], fp: str, la: dict | None,
                         plan: dict, streak: dict | None = None) -> str:
    out = ["pre-commit audit gate: BLOCKED", f"reason: {reason}",
           f"risk-bearing changed surface paths ({len(changed)}):"]
    out += [f"  - {p}" for p in changed[:10]]
    if len(changed) > 10:
        out.append(f"  ... and {len(changed) - 10} more")
    out.append(f"seat band: {plan.get('band')} -> {plan.get('seats')} seat(s) "
               f"({plan.get('reason')}); signals: "
               f"{json.dumps(plan.get('signals') or {}, ensure_ascii=False)}")
    if isinstance(la, dict) and la:
        sf = ((la.get("stagedFingerprint") or {}).get("sha256") or "")
        out.append(f"last audit: verdict={la.get('verdict')} at={la.get('at')} "
                   f"fingerprint={sf[:12]}")
    else:
        out.append("last audit: none on record")
    out.append(f"current staged fingerprint: {fp[:12]}")
    if streak and streak.get("count"):
        # WHICH cycle of this iteration this is: a fix moves the fingerprint, so
        # without the streak head every block reads like the first one.
        line = f"iteration: cycle {streak['count']} of {streak['k']} consecutive BLOCK(s)"
        if int(streak["count"]) >= int(streak["k"]):
            line += (f"; escalation {streak['escId']} is MANDATORY "
                     f"(registered: {'yes' if streak.get('registered') else 'no'})")
        out.append(line)
    out.append(f"fix: {AUDIT_FIX_COMMAND}")
    out.append(f"or record the arbiter's verdict: {AUDIT_RECORD_COMMAND}")
    out.append(f"or record the judged tolerance: {AUDIT_WAIVE_COMMAND}")
    return "\n".join(out)


def check_audit(root: Path) -> tuple[str, str]:
    """('pass'|'block'|'warn', reason). FAIL-OPEN by design (the check_reckon
    mirror): block ONLY when the staged surface positively OWES an audit (risk-
    bearing AND above the trivial floor) and lacks a matching ship verdict. Policy
    absent, non-risk or trivial surfaces, and ANY internal error degrade to
    pass/warn -- a broken audit gate must never wedge a commit.

    Per decision (e) the tuple stays 3-valued: `stale` is a block reason and `n/a`
    a pass reason; verify-status prints the derived label.

    The audit POLICY is a second surface here (see below): an untouched policy costs
    two extra `git show` calls and changes nothing, an edited one moves the
    fingerprint every verdict is keyed on."""
    try:
        policy = load_policy(root)
        if policy is None:
            return ("warn", "pre-commit audit gate not adopted; migration window")
        staged = staged_manifest(root, policy)
        head = head_manifest(root, policy)
        from harness_lib import audit_leg
        # The audit policy lives OUTSIDE surfaceRoots, so a policy-ONLY commit leaves
        # staged == head and used to take the fast-path below: the relaxation shipped
        # with zero seats owed and no trace, and commit 2 was judged under it
        # (seat3-F1, 2026-08-03). An edited policy therefore rides the fingerprint
        # (audit_leg.audit_fp) and owes an explicit verdict of its own. ONE shared
        # predicate with audit_fp -- adoption is not an edit, deletion is.
        policy_edited = audit_leg.policy_edited(root)
        if staged == head and not policy_edited:
            return ("pass", "staged surface unchanged vs HEAD")
        changed = _changed_paths(staged, head)
        req = required_profile(changed, policy)
        plan = audit_leg.seat_plan(root, policy, changed, req)
        policy_only = policy_edited and staged == head
        if not policy_only:  # a policy-only commit owes a verdict whatever the band says
            if plan["band"] == "unadopted":
                return ("warn", f"pre-commit audit gate not adopted; {plan['reason']}")
            if int(plan["seats"]) <= 0:
                return ("pass", f"{plan['reason']}; audit n/a")
        fp = audit_leg.audit_fp(root, manifest_fingerprint(staged))
        qs = _read_quality(root)
        if qs is None:
            # The gate's OWN state is unreadable -> degrade (check_staged already
            # blocks on this same condition, so the commit stays gated).
            return ("warn", "quality-state.json missing or corrupt; audit gate degraded")
        la = qs.get("lastAudit")
        la_ok = isinstance(la, dict) and bool(la.get("stagedFingerprint"))
        matches = la_ok and (la.get("stagedFingerprint") or {}).get("sha256") == fp
        if matches and la.get("verdict") == "ship":
            producer = la.get("producer") or {}
            mode = producer.get("mode") or ("MECHANIZED_AUDIT_PROOF" if int(la.get("seats") or 0) > 0 else "MANUAL_ATTESTATION")
            if mode == "MANUAL_ATTESTATION" and not policy.get("allowManualAuditAttestation"):
                return ("block", f"audit stamp is MANUAL_ATTESTATION ({fp[:12]}): mechanized audit proof with real seat evidence required (run `harness audit run` or provide explicit policy waiver)")
            return ("pass", f"staged surface audited SHIP ({fp[:12]}; "
                            f"{la.get('seats')} seat(s), mode {mode}, band {plan['band']})")
        if matches and la.get("verdict") == "waived":
            return ("warn", f"audit WAIVED for this surface ({fp[:12]}): {la.get('reason')}")
        # An explicit on-record block verdict FOR THIS FINGERPRINT blocks
        # regardless of the durable ledger -- a seat quorum (or the arbiter)
        # saying BLOCK is never rescued. A block for an OLD fingerprint must NOT
        # suppress the ledger rescue of the current one (seat-2 F2, 2026-08-03:
        # the stale fp1 block was short-circuiting past fp2's legitimate ship).
        explicit_block = matches and la.get("verdict") not in (None, "ship", "waived")
        if not explicit_block:
            row = _audit_row_for_fp(root, fp) or {}
            if row.get("verdict") == "ship":
                producer = row.get("producer") or {}
                mode = producer.get("mode") or ("MECHANIZED_AUDIT_PROOF" if int(row.get("seats") or 0) > 0 else "MANUAL_ATTESTATION")
                if mode == "MANUAL_ATTESTATION" and not policy.get("allowManualAuditAttestation"):
                    return ("block", f"durable audit stamp is MANUAL_ATTESTATION ({fp[:12]}): mechanized audit proof with real seat evidence required")
                return ("pass", f"staged surface audited SHIP ({fp[:12]}; durable ledger, mode {mode})")
            if row.get("verdict") == "waived":
                return ("warn", f"audit WAIVED for this surface ({fp[:12]}; durable ledger): "
                                f"{row.get('reason')}")

        if policy_only:
            # `audit run` refuses here (changed is empty, nothing to audit) and that
            # is INTENDED: relaxing the gate's own policy is an arbiter/owner call,
            # recorded by hand against this combined fingerprint.
            return ("block", f"audit policy edited in a policy-only commit ({fp[:12]}): "
                             f"the policy edit owes an explicit verdict -- fix: "
                             f"{AUDIT_RECORD_COMMAND} or {AUDIT_WAIVE_COMMAND}")
        if not la_ok:
            reason = "no audit on record for the staged surface"
        elif not matches:
            reason = "staged fingerprint does not match the last audit"
        else:
            reason = f"last audit verdict is {la.get('verdict')!r}, not ship"
        # Iteration visibility (audit-iteration-closure): consecutive BLOCKs are a
        # CYCLE the owner eventually has to decide on. Inside the existing try, so
        # the fail-open posture holds; `iteration_registered` compacts the escalation
        # ledger, so it is asked ONLY at K, where the answer changes the message.
        streak = audit_leg.block_streak(root)
        if streak["count"]:
            streak["k"] = audit_leg.max_block_cycles(root)
            streak["escId"] = audit_leg.iteration_escalation_id(streak)
            streak["registered"] = (streak["count"] >= streak["k"]
                                    and audit_leg.iteration_registered(root, streak["escId"]))
        return ("block", _audit_block_message(reason, changed, fp,
                                              la if isinstance(la, dict) else None, plan, streak))
    except Exception as exc:  # noqa: BLE001 — fail-open: never wedge a commit
        return ("warn", f"audit gate degraded ({type(exc).__name__}); not blocking")


def audit_leg_label(status: str, reason: str) -> str:
    """The DISPLAY label for the audit leg (decision e): the 3-status tuple stays
    the contract, `n/a` and `stale` are derived from the reason for the reader.
    ponytail: substring match on our own two reason strings; if a third derived
    label ever appears, key the reasons instead of the text."""
    if status == "pass" and reason.endswith("audit n/a"):
        return "n/a"
    if status == "block" and "does not match the last audit" in reason:
        return "stale"
    return status


# --- stamp (concurrency-guarded read-merge-write) ----------------------------

def _acquire_lock(lock: Path, stale_seconds: int) -> None:
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(str(lock)) > stale_seconds:
                    os.unlink(str(lock))
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError("quality-state.json.lock busy")
            time.sleep(0.1)


def stamp_staged(root: Path, payload: dict) -> None:
    """Concurrency-guarded merge of the staged-snapshot keys INTO lastValidation +
    one summary JSONL row. CAS: refuse to clobber a validation newer than this run."""
    run_start = parse_iso_datetime(payload.pop("_runStart", None))
    lock_secs = int(payload.pop("_lockStaleSeconds", 300) or 300)
    quality = _quality(root)
    quality.parent.mkdir(parents=True, exist_ok=True)
    lock = quality.with_name(quality.name + ".lock")
    _acquire_lock(lock, lock_secs)
    try:
        qs = _read_quality(root)
        if not isinstance(qs, dict):
            qs = {"schemaVersion": "1.0"}
        lv = qs.get("lastValidation")
        if not isinstance(lv, dict):
            lv = {}
        existing_at = parse_iso_datetime(lv.get("validatedAt"))
        # CAS: only refuse against a newer STAGED validation (a competing `validate --staged`,
        # which carries stagedFingerprint). Option-3 runs the gate in the working tree, so the
        # gate's own plain quality-state write DURING this run (no stagedFingerprint, validatedAt
        # after run_start) must NOT trip the guard, or validate --staged can never stamp.
        if run_start and existing_at and existing_at > run_start and lv.get("stagedFingerprint"):
            print("validate --staged: refusing to stamp; quality-state was validated "
                  f"more recently ({lv.get('validatedAt')}) than this run started "
                  f"({payload.get('validatedAt')})", file=sys.stderr)
            raise SystemExit(1)
        lv.update(payload)
        qs["lastValidation"] = lv
        qs.setdefault("schemaVersion", "1.0")
        try:
            write_json(quality, qs)
        except PermissionError:  # OneDrive/AV can briefly hold the file
            time.sleep(0.2)
            write_json(quality, qs)
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass

    # ER-12: Mint ValidationExecutionReceipt
    from harness_lib import governance_receipt
    fp_str = (payload.get("stagedFingerprint") or {}).get("sha256") or ""
    if fp_str:
        rc = governance_receipt.GovernanceExecutionReceipt(
            execution_id=f"EXEC-VAL-{fp_str[:12]}",
            kind="validation",
            run_id="validate-staged",
            fingerprint=fp_str,
            controller_identity="harness-governance-controller",
            started_at=payload.get("validatedAt") or governance_receipt.now(),
            ended_at=payload.get("validatedAt") or governance_receipt.now(),
            exit_code=0 if payload.get("status") == "pass" else 1,
            termination_reason="exited_cleanly",
            status="completed" if payload.get("status") == "pass" else "failed",
            parsed_result=payload
        )
        rc.write(governance_receipt.get_receipt_path(root, "validation", fp_str))

    # Durable validation-history row (what requirement 4's audit searches).
    from harness_lib import log_rotation
    runs = _runs(root)
    runs.parent.mkdir(parents=True, exist_ok=True)
    log_rotation.spill_if_over(runs)
    with runs.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "gate": "validate-staged", "status": payload.get("status"),
            "subject": "self", "snapshot": "staged-index",
            "stagedFingerprint": (payload.get("stagedFingerprint") or {}).get("sha256"),
            "policyVersion": payload.get("policyVersion"),
            "validatorVersion": payload.get("validatorVersion"),
            "requiredProfile": payload.get("requiredProfile"),
            "executedProfile": payload.get("executedProfile"),
            "at": payload.get("validatedAt"),
        }, ensure_ascii=False) + "\n")


def stamp_reckon(root: Path, payload: dict) -> None:
    """Concurrency-guarded merge of the reckon verdict INTO lastReckon + one
    reckon-history JSONL row. MIRRORS stamp_staged (same lock/CAS/merge); the
    verdict is keyed on the staged fingerprint, so a later surface change stales
    the record for free. CAS: refuse to clobber a reckon newer than this run."""
    run_start = parse_iso_datetime(payload.pop("_runStart", None))
    lock_secs = int(payload.pop("_lockStaleSeconds", 300) or 300)
    quality = _quality(root)
    quality.parent.mkdir(parents=True, exist_ok=True)
    lock = quality.with_name(quality.name + ".lock")
    _acquire_lock(lock, lock_secs)
    try:
        qs = _read_quality(root)
        if not isinstance(qs, dict):
            qs = {"schemaVersion": "1.0"}
        lr = qs.get("lastReckon")
        if not isinstance(lr, dict):
            lr = {}
        existing_at = parse_iso_datetime(lr.get("at"))
        if run_start and existing_at and existing_at > run_start and lr.get("stagedFingerprint"):
            print("reckon --record: refusing to stamp; a reckon was recorded more "
                  f"recently ({lr.get('at')}) than this run started "
                  f"({payload.get('at')})", file=sys.stderr)
            raise SystemExit(1)
        lr.update(payload)
        qs["lastReckon"] = lr
        qs.setdefault("schemaVersion", "1.0")
        try:
            write_json(quality, qs)
        except PermissionError:  # OneDrive/AV can briefly hold the file
            time.sleep(0.2)
            write_json(quality, qs)
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass

    # ER-10: Mint ReckonExecutionReceipt ONLY when mode is MECHANIZED_RECKON_PROOF
    from harness_lib import governance_receipt
    fp_str = (payload.get("stagedFingerprint") or {}).get("sha256") or ""
    mode = (payload.get("producer") or {}).get("mode") or ("MECHANIZED_RECKON_PROOF" if payload.get("actor") == "harness-kernel" else "MANUAL_ATTESTATION")
    if fp_str and mode == "MECHANIZED_RECKON_PROOF":
        rc = governance_receipt.GovernanceExecutionReceipt(
            execution_id=f"EXEC-REC-{fp_str[:12]}",
            kind="reckon",
            run_id="reckon-run",
            fingerprint=fp_str,
            controller_identity="harness-governance-controller",
            started_at=payload.get("at") or governance_receipt.now(),
            ended_at=payload.get("at") or governance_receipt.now(),
            exit_code=0 if payload.get("verdict") == "no-blocker" else 1,
            termination_reason="exited_cleanly",
            status="completed" if payload.get("verdict") == "no-blocker" else "failed",
            parsed_result=payload
        )
        rc.write(governance_receipt.get_receipt_path(root, "reckon", fp_str))

    # Durable reckon-history row (mirror of the validation-results row).
    from harness_lib import log_rotation
    runs = _reckon_runs(root)
    runs.parent.mkdir(parents=True, exist_ok=True)
    log_rotation.spill_if_over(runs)
    with runs.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "dimension": "reckon", "verdict": payload.get("verdict"),
            "subject": "self",
            "stagedFingerprint": (payload.get("stagedFingerprint") or {}).get("sha256"),
            "note": payload.get("note"), "at": payload.get("at"),
            # SPEC-161: the reviewer actor rides the durable row too (None for legacy callers).
            "actor": payload.get("actor"),
            # reckon-affected-map-input: the reach the reviewer judged against
            # (None for legacy callers / graphless roots).
            "affectedReach": payload.get("affectedReach"),
        }, ensure_ascii=False) + "\n")


def stamp_audit(root: Path, payload: dict) -> None:
    """Concurrency-guarded merge of the audit verdict INTO lastAudit + one
    audit-history JSONL row. MIRRORS stamp_reckon EXACTLY (same lock/CAS/merge,
    same PermissionError retry); the verdict is keyed on the staged fingerprint,
    so a later surface change stales the record for free. CAS: refuse to clobber
    an audit newer than this run."""
    run_start = parse_iso_datetime(payload.pop("_runStart", None))
    lock_secs = int(payload.pop("_lockStaleSeconds", 300) or 300)
    quality = _quality(root)
    quality.parent.mkdir(parents=True, exist_ok=True)
    lock = quality.with_name(quality.name + ".lock")
    _acquire_lock(lock, lock_secs)
    try:
        qs = _read_quality(root)
        if not isinstance(qs, dict):
            qs = {"schemaVersion": "1.0"}
        la = qs.get("lastAudit")
        if not isinstance(la, dict):
            la = {}
        existing_at = parse_iso_datetime(la.get("at"))
        if run_start and existing_at and existing_at > run_start and la.get("stagedFingerprint"):
            print("audit: refusing to stamp; an audit was recorded more "
                  f"recently ({la.get('at')}) than this run started "
                  f"({payload.get('at')})", file=sys.stderr)
            raise SystemExit(1)
        la.update(payload)
        qs["lastAudit"] = la
        qs.setdefault("schemaVersion", "1.0")
        try:
            write_json(quality, qs)
        except PermissionError:  # OneDrive/AV can briefly hold the file
            time.sleep(0.2)
            write_json(quality, qs)
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass
    # Durable audit-history row (mirror of the reckon-results row). `dissent` rides
    # even when the quorum resolved SHIP (owner ruling C6: it feeds the RF.1
    # matched-budget bakeoff); `vendorFallback`/`degradeReason` make a forced
    # single-vendor or below-plan run auditable from this file alone (C4/C5).
    from harness_lib import log_rotation
    runs = _audit_results(root)
    runs.parent.mkdir(parents=True, exist_ok=True)
    log_rotation.spill_if_over(runs)
    with runs.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "dimension": "audit", "verdict": payload.get("verdict"), "subject": "self",
            "stagedFingerprint": (payload.get("stagedFingerprint") or {}).get("sha256"),
            "seats": payload.get("seats"), "plannedSeats": payload.get("plannedSeats"),
            "findingsCount": payload.get("findingsCount"),
            "dissent": payload.get("dissent") or [],
            "vendorFallback": payload.get("vendorFallback"),
            "degradeReason": payload.get("degradeReason"),
            "invalidSeats": payload.get("invalidSeats") or [],
            "note": payload.get("note"), "reason": payload.get("reason"),
            "actor": payload.get("actor"),
            "affectedReach": payload.get("affectedReach"),
            "at": payload.get("at"),
        }, ensure_ascii=False) + "\n")


# --- per-scenario verdict cache (SPEC-159 Phase 1, additive) -----------------
# A separate class-D store keyed by gate_affected.scenario_cache_key (scenario-id +
# import-closure hash + validatorVersion). MEASURE-ONLY in Phase 1: the shadow layer
# reads it to detect false-skips and refreshes it each run; NOTHING consults it to
# skip yet. Kept OUT of quality-state.json on purpose so check_staged/check_reckon are
# untouched. Mirrors the stamp_staged lock/merge pattern.

_SCENARIO_CACHE_CAP = 4000  # ponytail: bound growth; drop oldest by 'at'. Raise if churn needs it.


def _scenario_cache(root: Path) -> Path:
    return root / ".harness" / "runs" / "scenario-verdicts.json"


def read_scenario_verdicts(root: Path) -> dict:
    """key → {scenario, verdict, closureHash, gateVersion, at}. Fail-open: a missing
    or corrupt store reads as empty (→ all cache misses → everything runs)."""
    try:
        doc = json.loads(_scenario_cache(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_scenario_verdicts(root: Path, fresh: dict, lock_stale_seconds: int = 300) -> None:
    """Lock-guarded read-merge-write of fresh {key: entry} into the store (mirror of
    stamp_staged's lock). Additive; never raises past the lock cleanup."""
    if not fresh:
        return
    path = _scenario_cache(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    _acquire_lock(lock, lock_stale_seconds)
    try:
        cur = read_scenario_verdicts(root)
        cur.update(fresh)
        if len(cur) > _SCENARIO_CACHE_CAP:  # keep the newest by 'at'
            keep = sorted(cur.items(), key=lambda kv: str(kv[1].get("at") or ""),
                          reverse=True)[:_SCENARIO_CACHE_CAP]
            cur = dict(keep)
        try:
            write_json(path, cur)
        except PermissionError:  # OneDrive/AV can briefly hold the file
            time.sleep(0.2)
            write_json(path, cur)
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass


# --- override observability --------------------------------------------------

def _stamped_fingerprints(root: Path) -> set[str]:
    out: set[str] = set()
    qs = _read_quality(root)
    if isinstance(qs, dict):
        sf = ((qs.get("lastValidation") or {}).get("stagedFingerprint") or {}).get("sha256")
        if sf:
            out.add(sf)
    try:
        tail = _runs(root).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        tail = []
    for raw in tail:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        fpv = row.get("stagedFingerprint")
        if isinstance(fpv, str):
            out.add(fpv)
    return out


def detect_override(root: Path) -> dict | None:
    """The fresh HEAD commit changed the surface without a matching stamped
    fingerprint => an override (likely `git commit --no-verify`). None otherwise."""
    policy = load_policy(root)
    if policy is None or not _rev_ok(root, "HEAD"):
        return None
    head = head_manifest(root, policy)
    if _rev_ok(root, "HEAD~1") and _tree_manifest(root, "HEAD~1", policy) == head:
        return None  # this commit did not touch the surface
    fp = manifest_fingerprint(head)
    if fp in _stamped_fingerprints(root):
        return None
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    return {"sha": sha, "fingerprint": fp, "validated": False, "at": now()}


def record_override(root: Path) -> dict | None:
    """Append-only override audit. Never raises, never blocks (post-commit safety)."""
    try:
        ov = detect_override(root)
        if not ov:
            return None
        audit = _audit(root)
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ov, ensure_ascii=False) + "\n")
        return ov
    except Exception:  # noqa: BLE001 — post-commit must stay harmless
        return None


def _reckoned_fingerprints(root: Path) -> set[str]:
    out: set[str] = set()
    qs = _read_quality(root)
    if isinstance(qs, dict):
        lr = qs.get("lastReckon") or {}
        if lr.get("verdict") == "no-blocker":
            sf = (lr.get("stagedFingerprint") or {}).get("sha256")
            if sf:
                out.add(sf)
    try:
        tail = _reckon_runs(root).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        tail = []
    for raw in tail:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("verdict") == "no-blocker" and isinstance(row.get("stagedFingerprint"), str):
            out.add(row["stagedFingerprint"])
    return out


def detect_reckon_override(root: Path) -> dict | None:
    """The fresh HEAD commit changed a RISK-BEARING surface without a matching
    no-blocker reckon => a reckon bypass (`git commit --no-verify`). None
    otherwise. MIRRORS detect_override but keyed on the reckon fingerprints and
    scoped to the risk-bearing profiles only (the common path is never audited)."""
    policy = load_policy(root)
    if policy is None or not _rev_ok(root, "HEAD"):
        return None
    head = head_manifest(root, policy)
    prev = _tree_manifest(root, "HEAD~1", policy) if _rev_ok(root, "HEAD~1") else []
    if _rev_ok(root, "HEAD~1") and prev == head:
        return None  # this commit did not touch the surface
    changed = _changed_paths(head, prev)
    if not reckon_required(policy, required_profile(changed, policy)):
        return None  # non-risk surface: no reckon was owed
    fp = manifest_fingerprint(head)
    if fp in _reckoned_fingerprints(root):
        return None
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    return {"sha": sha, "fingerprint": fp, "dimension": "reckon",
            "reckoned": False, "at": now()}


def record_reckon_override(root: Path) -> dict | None:
    """Append-only reckon-override audit. Never raises, never blocks (mirror)."""
    try:
        ov = detect_reckon_override(root)
        if not ov:
            return None
        audit = _audit(root)
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ov, ensure_ascii=False) + "\n")
        return ov
    except Exception:  # noqa: BLE001 — post-commit must stay harmless
        return None


def _audited_fingerprints(root: Path) -> set[str]:
    out: set[str] = set()
    qs = _read_quality(root)
    if isinstance(qs, dict):
        la = qs.get("lastAudit") or {}
        if la.get("verdict") in ("ship", "waived"):
            sf = (la.get("stagedFingerprint") or {}).get("sha256")
            if sf:
                out.add(sf)
    try:
        tail = _audit_results(root).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        tail = []
    for raw in tail:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("verdict") in ("ship", "waived") and isinstance(row.get("stagedFingerprint"), str):
            out.add(row["stagedFingerprint"])
    return out


def detect_audit_override(root: Path) -> dict | None:
    """The fresh HEAD commit changed a surface that OWED an audit without a matching
    ship/waived record => an audit bypass (`git commit --no-verify`). None otherwise.
    MIRRORS detect_reckon_override, but the "owed" test is the band ladder (a trivial
    or non-risk surface owed nothing and is never audited-by-omission)."""
    policy = load_policy(root)
    if policy is None or not _rev_ok(root, "HEAD"):
        return None
    head = head_manifest(root, policy)
    prev = _tree_manifest(root, "HEAD~1", policy) if _rev_ok(root, "HEAD~1") else []
    if _rev_ok(root, "HEAD~1") and prev == head:
        return None  # this commit did not touch the surface
    changed = _changed_paths(head, prev)
    from harness_lib import audit_leg
    plan = audit_leg.seat_plan(root, policy, changed, required_profile(changed, policy))
    if int(plan.get("seats") or 0) <= 0:
        return None  # nothing was owed: never an override
    fp = manifest_fingerprint(head)
    if fp in _audited_fingerprints(root):
        return None
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    return {"sha": sha, "fingerprint": fp, "dimension": "audit",
            "audited": False, "seatsOwed": int(plan["seats"]), "at": now()}


def record_audit_override(root: Path) -> dict | None:
    """Append-only audit-override record. Never raises, never blocks (mirror)."""
    try:
        ov = detect_audit_override(root)
        if not ov:
            return None
        audit = _audit(root)
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ov, ensure_ascii=False) + "\n")
        return ov
    except Exception:  # noqa: BLE001 — post-commit must stay harmless
        return None


def classify_commit_governance(root: Path, sha: str | None = None) -> dict[str, Any]:
    """Classify the governance status of a commit (HEAD or specified SHA) (GK-4 & GK-5).

    Conceptual states:
      - COMMITTED_GOVERNED: valid stamps for validation, reckon, mutate, and audit.
      - EMERGENCY_BYPASS: recorded emergency commit bypass in EventLedger.
      - COMMITTED_UNGOVERNED: missing mandatory stamps / created via raw git bypass.
    """
    ref = sha or "HEAD"
    policy = load_policy(root)
    if policy is None or not _rev_ok(root, ref):
        return {"status": "COMMITTED_UNGOVERNED", "reason": "No policy or invalid git ref", "governed": False}

    head = _tree_manifest(root, ref, policy)
    fp = manifest_fingerprint(head)

    val_ok = fp in _stamped_fingerprints(root)
    rec_ok = fp in _reckoned_fingerprints(root)
    aud_ok = fp in _audited_fingerprints(root)

    from harness_lib import event_ledger
    events = event_ledger.read_events(root=root)
    has_emergency_bypass = any(
        e.get("type") == "emergency_commit_bypass" and e.get("payload", {}).get("stagedFingerprint") == fp
        for e in events
    )

    if val_ok and rec_ok and aud_ok:
        return {"status": "COMMITTED_GOVERNED", "fingerprint": fp, "governed": True, "reason": "All mandatory stamps present and valid"}
    elif has_emergency_bypass:
        return {"status": "EMERGENCY_BYPASS", "fingerprint": fp, "governed": False, "reason": "Explicit emergency bypass recorded in EventLedger"}
    else:
        try:
            event_ledger.append_critical("ungoverned_commit_detected", {
                "commitSha": sha or _git(root, "rev-parse", ref).stdout.strip(),
                "fingerprint": fp,
                "missingStamps": [
                    k for k, ok in [("validation", val_ok), ("reckon", rec_ok), ("audit", aud_ok)] if not ok
                ],
            }, root=root)
        except Exception:
            pass
        return {"status": "COMMITTED_UNGOVERNED", "fingerprint": fp, "governed": False, "reason": "Missing mandatory stamps; preventive governance lost"}



# --- CLI handler -------------------------------------------------------------

def unstaged_surface_paths(root: Path, policy: dict) -> list[str]:
    """Surface files that differ between the working tree and the index (git diff,
    worktree vs index). Option-3 validates the working tree in place, so the staged
    surface MUST equal the worktree surface or we would validate something other than
    what is committed. Untracked files are not in the staged snapshot, so ignored."""
    roots = policy.get("surfaceRoots") or SURFACE_DEFAULT
    proc = _git(root, "diff", "--name-only", "--", *roots)
    return sorted(p for p in (ln.strip() for ln in (proc.stdout or "").splitlines())
                  if p and not _excluded(p, policy))


def run_production_snapshot_validation(snapshot, gates: list[str], *, verifier=None) -> dict:
    """Run fixed verifier authority over the frozen subject, never candidate authority."""
    from harness_lib.trusted_verifier import TrustedVerifier, load_trusted_verifier_record
    owned = verifier is None
    record = load_trusted_verifier_record(snapshot.source_root) if owned else None
    authority = verifier or (TrustedVerifier.from_record(snapshot.source_root, record) if record else None)
    if authority is None:
        return {"ok": False, "gates": [], "reason": "STRICT_VALIDATION_INELIGIBLE: CURRENT_HEAD_IS_NOT_IMPLICIT_TRUST_ROOT",
                "executionAuthority": "TEST_ONLY", "proofEligibility": "INELIGIBLE"}
    try:
        result = authority.validate(snapshot, gates)
        return {**result, "snapshotId": snapshot.snapshot_id,
                "candidateTreeId": snapshot.candidate_tree_id, "baseHead": snapshot.base_head}
    finally:
        if owned:
            authority.cleanup()


def cmd_validate(args) -> None:
    """`harness.py validate --staged` (item 3). --check = decision only (hook logic)."""
    root = ROOT
    as_json = getattr(args, "json", False)
    if not getattr(args, "staged", False):
        print("validate: --staged is required (the only mode today)", file=sys.stderr)
        raise SystemExit(2)

    if getattr(args, "check", False):
        status, reason = check_staged(root)
        print(json.dumps({"status": status, "reason": reason}, ensure_ascii=False)
              if as_json else f"{status}: {reason}")
        raise SystemExit(0 if status in ("pass", "warn") else 1)

    policy = load_policy(root)
    if policy is None:
        msg = "precommitValidation disabled or absent; nothing to validate (migration window)"
        print(json.dumps({"status": "skip", "reason": msg}, ensure_ascii=False) if as_json else msg)
        return

    staged = staged_manifest(root, policy)
    head = head_manifest(root, policy)
    fp = manifest_fingerprint(staged)
    if staged == head:
        print(json.dumps({"status": "pass", "reason": "surface unchanged", "fingerprint": fp},
                         ensure_ascii=False) if as_json else "surface unchanged")
        return

    changed = _changed_paths(staged, head)
    req = required_profile(changed, policy)
    run_start = now()
    from harness_lib import governance_campaign
    campaign, snapshot = governance_campaign.GovernanceCampaign.create(root, required_gates=req["gates"])
    try:
        result, _receipt = campaign.run_leg(
            "validation", lambda frozen: run_production_snapshot_validation(
                frozen, req["gates"], verifier=campaign._trusted_verifier))
    finally:
        snapshot.cleanup()
        campaign.close()
    if not result["ok"]:
        if result.get("gates"):
            failed = next(row for row in result["gates"] if row["exitCode"])
            print(f"validate --staged: gate '{failed['gate']}' FAILED", file=sys.stderr)
            sys.stderr.write(failed["stdout"][-3000:] + failed["stderr"][-3000:])
        else:
            print("validate --staged: " + result.get("reason", "strict validation ineligible"), file=sys.stderr)
        raise SystemExit(1)

    stamp_staged(root, {
        "status": "pass", "validatedAt": now(), "gate": "validate-staged",
        "stagedFingerprint": {"sha256": fp, "fileCount": len(staged)},
        "policyVersion": policy.get("policyVersion"),
        "validatorVersion": validator_version(root),
        "requiredProfile": req, "executedProfile": req, "snapshot": "staged-index",
        "candidateTreeId": campaign.candidate_tree_id, "snapshotId": campaign.snapshot_id,
        "baseHead": campaign.base_head,
        "_runStart": run_start, "_lockStaleSeconds": policy.get("lockStaleSeconds", 300),
    })
    print(json.dumps({"status": "pass", "fingerprint": fp, "gates": req["gates"]},
                     ensure_ascii=False) if as_json
          else f"validate --staged: pass ({fp[:12]}; gates: {', '.join(req['gates'])})")


def cmd_reckon(args) -> None:
    """`harness.py reckon --record --verdict no-blocker|blocker [--note ...]`
    (SPEC-157): stamp the reviewer's verdict against the CURRENT staged
    fingerprint, so the commit-gate can enforce gate=pass AND reckon=no-blocker."""
    root = ROOT
    as_json = getattr(args, "json", False)
    if not getattr(args, "record", False):
        print("reckon: --record is required (the only mode today)", file=sys.stderr)
        raise SystemExit(2)
    verdict = getattr(args, "verdict", None)
    if verdict not in ("no-blocker", "blocker"):
        print("reckon --record: --verdict must be no-blocker or blocker", file=sys.stderr)
        raise SystemExit(2)
    policy = load_policy(root)
    if policy is None:
        msg = "precommitValidation disabled or absent; reckon not recorded (migration window)"
        print(json.dumps({"status": "skip", "reason": msg}, ensure_ascii=False) if as_json else msg)
        return
    staged = staged_manifest(root, policy)
    fp = manifest_fingerprint(staged)
    at = now()
    # reckon-affected-map-input: the reach the reviewer judged against rides the
    # ledger row (additive; None omitted) — reach drift between reckon and commit
    # becomes auditable from reckon-results.jsonl alone.
    reach = reckon_reach(root, _changed_paths(staged, head_manifest(root, policy)))
    stamp_reckon(root, {
        "verdict": verdict, "at": at, "note": getattr(args, "note", None),
        **({"affectedReach": {"affected": reach["affected"], "total": reach["total"]}}
           if reach else {}),
        "stagedFingerprint": {"sha256": fp, "fileCount": len(staged)},
        # SPEC-161 N-AUTHCHAIN: credit the reviewer -- closes the "reviewer sem
        # reviewer". Flows into lastReckon (merge) and the JSONL row; additive.
        "actor": responsibility.make_actor("reviewer"),
        "_runStart": at, "_lockStaleSeconds": policy.get("lockStaleSeconds", 300),
    })
    print(json.dumps({"status": "recorded", "verdict": verdict, "fingerprint": fp},
                     ensure_ascii=False) if as_json
          else f"reckon recorded: {verdict} ({fp[:12]})")


def cmd_verify_status(args) -> None:
    """`harness.py verify-status [--json]` (SPEC-157): the join view -- gate
    (validation) + reckon + mutate + audit decisions over the CURRENT staged
    surface, the staged fingerprint, and readyToCommit (all FOUR dimensions
    non-blocking). `auditLeg` is the derived display label (decision e): the
    status tuple stays 3-valued, `n/a`/`stale` are reason-encoded."""
    root = ROOT
    as_json = getattr(args, "json", False)
    policy = load_policy(root)
    gate_status, gate_reason = check_staged(root)
    reckon_status, reckon_reason = check_reckon(root)
    mutate_status, mutate_reason = check_mutate(root)
    audit_status, audit_reason = check_audit(root)
    audit_label = audit_leg_label(audit_status, audit_reason)
    fp = manifest_fingerprint(staged_manifest(root, policy)) if policy is not None else None
    ready = "block" not in (gate_status, reckon_status, mutate_status, audit_status)
    payload = {
        "gate": gate_status, "gateReason": gate_reason,
        "reckon": reckon_status, "reckonReason": reckon_reason,
        "mutate": mutate_status, "mutateReason": mutate_reason,
        "audit": audit_status, "auditReason": audit_reason, "auditLeg": audit_label,
        "stagedFingerprint": fp, "readyToCommit": ready,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(f"gate:   {gate_status} — {gate_reason}")
    print(f"reckon: {reckon_status} — {reckon_reason}")
    print(f"mutate: {mutate_status} — {mutate_reason}")
    print(f"audit:  {audit_label} — {audit_reason}")
    print(f"staged fingerprint: {(fp or '')[:12] or 'n/a'}")
    print(f"readyToCommit: {ready}")
