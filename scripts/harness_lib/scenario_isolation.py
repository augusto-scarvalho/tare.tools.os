#!/usr/bin/env python3
"""Runner-level scenario isolation for the `scenarios` gate.

Snapshot the volatile on-disk state BEFORE each scenario and restore it AFTER, so
the gate is order-independent and hermetic: no scenario's mutations leak into the
next (the flakiness where a different subset failed each run). This is the SYSTEMIC
replacement for per-scenario opt-in `_lib.push_isolated_state` path lists -- it
snapshots whole state DIRS, so a state file a new scenario touches is covered
automatically, no hardcoded per-file list to maintain.

Dirs are snapshotted by copy (small JSON state); append-only run logs by
length+truncate (never copy a large log ~100x per gate); single files by copy.

ponytail: rmtree(ignore_errors) + copytree(dirs_exist_ok) is a best-effort exact
restore; under a hard OneDrive file lock a scenario-added extra could survive. If
that ever bites, escalate to a move-aside-and-swap. Not worth it until measured.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from harness_lib import event_ledger
from harness_lib.common import HarnessError, ROOT



_BACKLOG_REL = "docs/IMPLEMENTATION_BACKLOG.md"
_STORE_REL = ".harness/state/tasks.json"
# Paths the gate must see at their STAGED content rather than at HEAD: they are
# what the SPEC-154/155 guards VALIDATE, so a closure staged in the same commit
# has to be visible or the closure lockstep is unsatisfiable by construction.
# The store joined the list when the guards started reading it (backlog-json-
# canonical Phase 3) — it lives inside the held `.harness/state` dir, so without
# this it would run at HEAD and `tasks close` could never satisfy bc-1.
_REGISTRY_REL = ".harness/routing/playbook-registry.json"
# The playbook registry joined for the SAME reason, measured 2026-07-27: `.harness/
# prompts/` is NOT held, so a newly added playbook FILE is visible to scenarios,
# while `.harness/routing` IS held, so the registry that would list it runs at HEAD.
# pr-7 then reports the new file as an `orphan` — meaning ANY commit adding a
# playbook is red by construction, exactly the shape the store had for `tasks close`.
_REGISTRY_LOCK_REL = ".harness/routing/playbook-registry.lock.json"
# The lock rides WITH the registry, never separately: pr-7 compares the two, so
# materializing one at staged content and the other at HEAD manufactures exactly
# the `lock-drift` it is meant to detect. Measured 2026-07-27 — adding only the
# registry turned the `orphan` finding into a `lock-drift` finding.
_METAMODEL_REL = ".harness/routing/role-metamodel.json"
# SPEC-173 rule 6: the unified metamodel rides with the registry for the same
# reason the lock does — `playbook_registry.load` resolves THROUGH it, so leaving
# it at HEAD (or, for a not-yet-committed file, absent) while the registry runs at
# staged content silently changes the chain every scenario and the injecting hook
# resolve, and only inside the gate's sandbox where it is hardest to notice.
_VALIDATED_DOCS = (_BACKLOG_REL, _STORE_REL, _REGISTRY_REL, _REGISTRY_LOCK_REL, _METAMODEL_REL)


def _targets(root: Path) -> dict[str, list[Path]]:
    return {
        # whole-dir snapshot => any state file a scenario mutates is covered. Includes
        # .harness/context (CONTEXT.md/NEXT_STEPS.md are rewritten by workflow finalize) --
        # surfaced by the pre-fix baseline git-status, the reason a hardcoded file list rots.
        # F4a: .harness/runtime holds executor-circuit-*.json while the circuit breaker
        # is globally enabled -- a scenario hard-killed by the gate timeout skips its
        # finally and leaks an OPEN circuit that refuses `workflow start` in every later
        # run. Snapshotting the whole dir restores it regardless of the finally running.
        # .harness/routing: model-routing.json / model-cards.json are written only by owner
        # GUI actions -- snapshotting the dir keeps a scenario's routing mutation from
        # leaking into the next (fixes mr_model_routing) alongside the owner's live edits.
        # .harness/handoff: workflow finalize / generate_handoff REGENERATES handoff.md/
        # handoff.json/next-agent-prompt.md AND derives NEXT_STEPS (context) from them.
        # Excluding it created a hold-set asymmetry (2026-07-20 rt6 audit): during the
        # scenarios gate, context ran at HEAD while handoff stayed live, so an in-gate
        # workflow finalize rewrote the held NEXT_STEPS from LIVE handoff state -> new
        # tracked dirt mid-gate + scenario mutations leaking into the owner's live handoff.
        "dirs": [root / ".harness" / "state", root / ".harness" / "workflows" / "active",
                 root / ".harness" / "context", root / ".harness" / "runtime",
                 root / ".harness" / "routing", root / ".harness" / "handoff"],
        "files": [root / "docs" / "IMPLEMENTATION_BACKLOG.md"],
        # append-only logs: cheap length+truncate, not a copy
        "logs": [root / ".harness" / "runs" / "events.jsonl",
                 root / ".harness" / "runs" / "harness-trace.jsonl"],
    }


def snapshot_volatile_state(root: Path | str, hold: Path | str) -> dict[str, Any]:
    """Copy the volatile state into `hold` and return a restore token."""
    root, hold = Path(root), Path(hold)
    hold.mkdir(parents=True, exist_ok=True)
    t = _targets(root)
    token: dict[str, Any] = {"dirs": [], "files": [], "logs": []}
    for i, d in enumerate(t["dirs"]):
        dest = hold / f"d{i}"
        existed = d.exists()
        if existed:
            shutil.copytree(d, dest)
        token["dirs"].append((str(d), str(dest), existed))
    for i, f in enumerate(t["files"]):
        dest = hold / f"f{i}"
        existed = f.exists()
        if existed:
            shutil.copy2(f, dest)
        token["files"].append((str(f), str(dest), existed))
    for lg in t["logs"]:
        token["logs"].append((str(lg), lg.stat().st_size if lg.exists() else 0))
    # Derived caches: not snapshotted, wiped on restore so a scenario's rows don't
    # linger with a preserved mtime (records.db reads its own mtime -- SPEC-112 hazard).
    token["derived"] = [str(root / ".harness" / "state-store")]
    return token


def restore_volatile_state(token: dict[str, Any]) -> None:
    """Restore exactly what `snapshot_volatile_state` captured (call in `finally`)."""
    for d_s, dest_s, existed in token["dirs"]:
        d = Path(d_s)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        if existed:
            shutil.copytree(Path(dest_s), d, dirs_exist_ok=True)
    for f_s, dest_s, existed in token["files"]:
        f = Path(f_s)
        if existed:
            f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(dest_s), f)
        elif f.exists():
            f.unlink()
    for lg_s, length in token["logs"]:
        lg = Path(lg_s)
        if lg.exists() and lg.stat().st_size > length:
            with lg.open("rb+") as fh:
                fh.truncate(length)
    for p_s in token.get("derived", []):  # guard old tokens
        shutil.rmtree(Path(p_s), ignore_errors=True)


# --- Gate-level dirty-baseline envelope -------------------------------------
# The scenarios gate runs against the LIVE .harness tree, so pre-existing dirt
# (owner GUI edits to routing/state, runtime cruft) turns scenarios red even
# though HEAD is clean. hold_dirty_baseline moves that dirt aside and materializes
# HEAD content for the gate's duration; release_dirty_baseline puts it back
# byte-identically in a finally. A hard-kill that skips the finally is recovered
# on the next gate run by _recover_stale_holds (dirt is never deleted, only moved).

_HOLD_README = """\
Pre-gate .harness working-tree state is held here.

The scenarios gate runs against a materialized-HEAD baseline so pre-existing
dirt (owner GUI edits, runtime state) does not turn scenarios red. Your real
state was MOVED here and the live tree reset to HEAD content for the gate's
duration. On a clean finish the gate moves everything back and deletes this dir.

If the gate crashed (timeout hard-kill) and this dir survived, the NEXT scenarios
run auto-recovers it: your dirt is moved back to the live tree, the gate's clean
HEAD tree is parked here as `displaced-*`, and this dir is renamed `*-recovered`
and kept. Nothing is ever deleted on the recovery path.

Manual recovery: for each entry in hold.json move the hold copy (e0, e1, ...)
back to its live path, then delete this dir.
"""


def _gate_hold_root(root: Path) -> Path:
    return root / ".harness" / "runs" / "gate-hold"


def hold_dirty_baseline(root: Path | str) -> Path:
    """Move the dirty .harness baseline aside and materialize HEAD for the gate.

    Recovers any abandoned hold first, then: creates a hold dir, writes its
    manifest+README BEFORE moving anything, moves each target dir + the backlog
    file into the hold (logs stay in place), `git restore`s the tracked pathspecs
    to HEAD (worktree only -- the index/staged room work is never touched), and
    wipes the derived state-store cache. Returns the hold path for release."""
    from harness_lib import processes  # mediated bounded spawn (raw-subprocess ratchet)

    root = Path(root)
    _recover_stale_holds(root)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hold = _gate_hold_root(root) / f"{ts}-{os.getpid()}"
    hold.mkdir(parents=True, exist_ok=True)
    t = _targets(root)
    # workflows/active is workflow RUNTIME, not owner GUI dirt, and _run_isolated_scenario
    # already snapshot/restores it per scenario. Materializing it to HEAD ({.gitkeep}) lets a
    # scenario's push_isolated_state -> `workflow scrub --force` (which deletes an emptied
    # active/) -> pop_isolated_state crash on the missing dir (m4_status_html). Leave it live.
    active_dir = root / ".harness" / "workflows" / "active"
    hold_dirs = [d for d in t["dirs"] if d != active_dir]
    live_paths = hold_dirs + list(t["files"])  # logs stay in place
    entries = [[str(p), f"e{i}"] for i, p in enumerate(live_paths)]
    relpaths = [str(p.relative_to(root)).replace("\\", "/") for p in live_paths]
    manifest = {"created": ts, "pid": os.getpid(), "entries": entries}
    (hold / "hold.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (hold / "README.txt").write_text(_HOLD_README, encoding="utf-8")
    # Staged-vs-HEAD shadow (measured 2026-07-18, rs:env-filtered): these paths run
    # at HEAD content during the scenarios gate, so a STAGED change under them is
    # NOT what scenarios exercise until it lands in HEAD. Warn loudly instead of
    # letting the mismatch surface one commit later as a "mystery" gate red.
    try:
        # The validated docs are re-materialized at their STAGED content below (the
        # SPEC-154/155 closure/entry guards must see the about-to-commit rows), so they
        # are excluded from this HEAD-shadow warning; the .harness runtime dirs still
        # run at HEAD. The store needs the second filter as well as the first: it lives
        # INSIDE the held `.harness/state` dir, which stays under warning on its own.
        warn_paths = [rp for rp in relpaths if rp not in _VALIDATED_DOCS]
        staged = (processes.run_quiet(["git", "diff", "--cached", "--name-only", "--", *warn_paths],
                                      cwd=str(root), capture_output=True, text=True,
                                      timeout=30).stdout.split() if warn_paths else [])
        staged = [s for s in staged if s not in _VALIDATED_DOCS]
        if staged:
            print(f"!! scenarios gate note: {len(staged)} STAGED path(s) under the held .harness "
                  f"dirs run at HEAD content during scenarios; asserts on this config only see "
                  f"the staged version AFTER commit: {', '.join(staged[:5])}", flush=True)
    except Exception:  # noqa: BLE001 -- advisory only, never break the gate
        pass
    try:
        for live_s, name in entries:
            live = Path(live_s)
            if live.exists():
                shutil.move(str(live), str(hold / name))
        ls = processes.run_quiet(["git", "ls-files", "-z", "--", *relpaths],
                                 cwd=str(root), capture_output=True, text=True, check=True, timeout=30)
        tracked = [p for p in ls.stdout.split("\x00") if p]
        kept = [rp for rp in relpaths if any(f == rp or f.startswith(rp + "/") for f in tracked)]
        if kept:
            processes.run_quiet(["git", "restore", "--worktree", "--source=HEAD", "--", *kept],
                                cwd=str(root), capture_output=True, text=True, check=True, timeout=30)
        # SPEC-154/155: the backlog document and the task store are VALIDATED docs, not
        # runtime state. `validate --staged` must check their STAGED rows so the closure/
        # entry guards (bc_backlog_closure / eg_entry_groom / sp_store_parity) see the
        # about-to-commit close, not HEAD. Re-materialize from the index (:0) over the
        # HEAD copy; nothing staged -> :0 == HEAD (unchanged). The real working-tree
        # content is preserved in the hold + restored on release.
        revalidate = [rp for rp in _VALIDATED_DOCS if rp in kept or rp in tracked]
        for rp in revalidate:  # the store's parent is recreated by the restore above
            (root / rp).parent.mkdir(parents=True, exist_ok=True)
        if revalidate:
            processes.run_quiet(["git", "checkout-index", "-f", "--", *revalidate],
                                cwd=str(root), capture_output=True, text=True, timeout=30)
    except Exception:
        release_dirty_baseline(hold)
        raise
    for d in t["dirs"]:  # recreate any dir with no tracked content
        d.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(root / ".harness" / "state-store", ignore_errors=True)
    return hold


def release_dirty_baseline(hold: Path | str) -> None:
    """Restore the held dirty baseline over the gate's HEAD tree (call in finally).

    Per entry whose hold copy still exists: drop the live (HEAD) path and move the
    held copy back -- per-entry try/except so one failure can't lose the rest. On
    ANY failure the hold dir is KEPT and its path printed loudly (owner dirt is
    never discarded); it is removed only when every entry returned."""
    hold = Path(hold)
    root = hold.parents[3]  # root/.harness/runs/gate-hold/<name>
    manifest = json.loads((hold / "hold.json").read_text(encoding="utf-8"))
    all_ok = True
    for live_s, name in manifest["entries"]:
        held = hold / name
        if not held.exists():
            continue
        live = Path(live_s)
        try:
            if live.is_dir() and not live.is_symlink():
                shutil.rmtree(live, ignore_errors=True)
            elif live.exists() or live.is_symlink():
                live.unlink()
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(held), str(live))
        except Exception as e:  # noqa: BLE001 -- keep going, keep the hold
            all_ok = False
            print(f"!! release_dirty_baseline: could not restore {live}: {e}", flush=True)
    # gate-scenarios-leak-wf-fixture-dirs(+tmp): active/ is EXCLUDED from the hold (per-scenario
    # snapshot/restore owns it), so a scenario that planned a workflow on the REAL root and
    # outlived its restore parks residue here. This is the gate-level counterpart of that
    # per-scenario net -- sweep the leaked fixtures now the scenarios run is done.
    sweep_fixture_workflows(root / ".harness" / "workflows" / "active")  # self-guarded (never raises)
    shutil.rmtree(root / ".harness" / "state-store", ignore_errors=True)
    if all_ok:
        shutil.rmtree(hold, ignore_errors=True)
    else:
        print(f"!! release_dirty_baseline: KEEPING your held state at {hold} "
              f"-- restore it manually (see README.txt) before deleting.", flush=True)


def _recover_stale_holds(root: Path) -> None:
    """Recover holds left behind by a hard-killed gate. Never deletes anything.

    For each abandoned hold: the live (clean HEAD) tree is displaced into the hold
    as `displaced-<name>`, the held dirty copy is moved back to live, and the hold
    dir is renamed `<name>-recovered` and kept with a loud warning."""
    gate_hold = _gate_hold_root(root)
    if not gate_hold.exists():
        return
    for manifest_path in sorted(gate_hold.glob("*/hold.json")):
        hold = manifest_path.parent
        if hold.name.endswith("-recovered"):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- unreadable manifest: leave it, don't crash the gate
            continue
        # N3-F2 (ref-arch round): a hold whose recorded pid is ALIVE is a RUNNING
        # gate, not an abandoned hold — refuse loudly instead of yanking live state
        # from under it. This is also the missing mutex between concurrent gates.
        # ponytail: pid recycling can false-positive; the error names the manual out.
        try:
            from harness_lib import processes
        except ImportError:  # run directly for the __main__ self-check
            import processes  # type: ignore
        try:
            holder = int(manifest.get("pid") or 0)
        except (TypeError, ValueError):
            holder = 0
        # Own-pid holds are recoverable: after a real crash the dead gate's pid is
        # gone, so a same-pid hold only means THIS process abandoned it earlier
        # (or a scenario simulating that crash) — it cannot be racing itself.
        if holder and holder != os.getpid() and processes.pid_alive(holder):
            raise RuntimeError(
                f"scenario_isolation: gate-hold {hold.name} belongs to LIVE pid {holder} "
                f"-- another gate is still running; refusing to recover its hold or start "
                f"a second gate. Wait for it to settle, or delete {hold} only if you are "
                f"certain that gate is gone.")
        for live_s, name in manifest["entries"]:
            held = hold / name
            if not held.exists():
                continue
            live = Path(live_s)
            try:
                if live.exists():
                    displaced = hold / f"displaced-{name}"
                    if not displaced.exists():
                        shutil.move(str(live), str(displaced))
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(held), str(live))
            except Exception as e:  # noqa: BLE001 -- best-effort, never delete
                print(f"!! _recover_stale_holds: could not recover {live}: {e}", flush=True)
        recovered = hold.parent / f"{hold.name}-recovered"
        try:
            hold.rename(recovered)
        except Exception:  # noqa: BLE001
            recovered = hold
        print(f"!! scenario_isolation: recovered abandoned gate-hold -> {recovered} "
              f"(dirty baseline restored to the live tree; displaced clean tree kept inside). "
              f"Remove it once you've confirmed your .harness state.", flush=True)


# cli-gate-hold-write-guard (owner-approved 2026-07-29): CLI verbs whose write
# would land on the materialized-HEAD tree during a LIVE hold and be discarded
# on release (the 3x 2026-07-23 loss class + the 2026-07-29 masked read).
# ONE list, living with the hold's owner — the GUI's _gate_in_flight and this
# guard are the two consumers of the same doctrine (protection-lists lesson:
# a second drifting list is the disease, so the GUI keeps its action-flag
# shape and THIS is the verb-shaped source).
HOLD_WRITE_SENSITIVE = (
    "tasks", "intake", "checkpoint", "delegation", "log", "decide", "experiment",
    "review", "reckon", "oracle", "fuel", "keys", "routing", "models", "targets",
    "event", "handoff", "research", "workflow", "self-review", "anomaly-card",
    # defect-sink-lost-under-gate-hold (measured 2026-07-31): `defect` writes the
    # SPEC-173 telemetry sink and was DROPPED silently during a hold — the incident
    # this row exists for. `route` appends route-ledger.jsonl; `chat` appends the
    # cost-metrics chat-turn row. All three landed on materialized-HEAD and vanished.
    "defect", "route", "chat",
    # audit collect/record/waive write quality-state.json#lastAudit (the 4th join
    # leg); the durable audit-results.jsonl survives under .harness/runs/, but the
    # stamp half is exactly the loss class this tuple guards.
    "audit",
)
# NEVER guarded: gate-staged/validate (the gate itself), hold-recover (the
# recovery verb), verify-status/status/doctor and every read verb.


def live_holds(root: Path | str) -> list[dict[str, Any]]:
    """Read-only: gate-holds whose recorded pid is ALIVE (a gate is running).
    The complement of abandoned_holds — same manifest shape, same skips
    (`*-recovered`, unreadable = not provably live), same import fallback."""
    out: list[dict[str, Any]] = []
    gate_hold = _gate_hold_root(Path(root))
    if not gate_hold.exists():
        return out
    try:
        from harness_lib import processes
    except ImportError:  # run directly for the __main__ self-check
        import processes  # type: ignore
    for manifest_path in sorted(gate_hold.glob("*/hold.json")):
        hold = manifest_path.parent
        if hold.name.endswith("-recovered"):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pid = int(manifest.get("pid") or 0)
        except Exception:  # noqa: BLE001 — unreadable: not provably live
            continue
        if pid > 0 and pid != os.getpid() and processes.pid_alive(pid):
            out.append({"name": hold.name, "path": str(hold), "pid": pid,
                        "created": manifest.get("created")})
    return out


def _holding(root: Path | str) -> bool:
    """common.gate_holding with the standalone-self-check import fallback. The ONE
    "is the tree held" predicate the CLI guard and the data-layer backstops now share
    (audit, sonnet 2026-07-31): a live OR abandoned-but-unrecovered hold counts; a
    `*-recovered` remnant does NOT (the tree was restored). `live_holds` (pid-alive)
    stays only for ENRICHING the refusal with a pid/name — it must not decide the gate,
    or an abandoned hold's writes leak past it (that was the `chat` silent-loss gap)."""
    try:
        from .common import gate_holding
    except ImportError:  # run directly for the __main__ self-check
        from common import gate_holding  # type: ignore
    return gate_holding(Path(root))


def hold_write_guard(verb: str, root: Path | str) -> str | None:
    """Refusal reason when `verb` writes .harness state and a hold holds the tree;
    None = proceed. Fail-open: any error reads as no hold (the guard must never wedge
    the CLI). Inside a gate scenario the snapshot/restore IS the contract (same
    exemption as ui_actions._gate_in_flight)."""
    try:
        if verb not in HOLD_WRITE_SENSITIVE:
            return None
        if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
            return None
        if not _holding(root):
            return None
        live = live_holds(root)
        where = (f"hold {live[0]['name']}, pid {live[0]['pid']}" if live
                 else "an unrecovered gate-hold — run hold-recover")
        return (f"a harness gate is in flight ({where}): "
                f"`{verb}` writes .harness state, and the hold would silently discard "
                "it on release — retry in a few minutes (verify-status shows the gate; "
                "reads during the hold may reflect the materialized-HEAD tree, not "
                "your live state)")
    except Exception:  # noqa: BLE001 — guard must never wedge the CLI
        return None


def hold_read_warning(verb: str, root: Path | str) -> str | None:
    """The READ twin of hold_write_guard: a warning (not a refusal) when a
    non-write verb runs while a hold holds the tree. The visible `.harness/state` is
    the materialized-HEAD tree, not the parked live state, so counts read SMALLER than
    reality — the exact trap behind the 2026-07-31 false "16 records lost" report.
    None when the verb is a write (hold_write_guard refuses those), inside a
    scenario, or no hold. Fail-open — a warning must never wedge the CLI."""
    try:
        if verb in HOLD_WRITE_SENSITIVE:
            return None
        if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
            return None
        if not _holding(root):
            return None
        return ("a harness gate is in flight: this reads the materialized-HEAD "
                ".harness/state, NOT your live parked state — counts can look "
                "smaller than reality; recheck after verify-status shows the gate done")
    except Exception:  # noqa: BLE001 — warning must never wedge the CLI
        return None


def abandoned_holds(root: Path | str) -> list[dict[str, Any]]:
    """Read-only: gate-holds whose recorded gate pid is PROVABLY dead.

    Row gate-hold-abandoned-detector: after a mid-gate crash the owner's dirty
    `.harness` state sits safely inside the hold while the live tree shows the
    HEAD-materialized baseline -- indistinguishable from data loss to whoever is
    looking at the screen. This names where the state is WITHOUT touching it;
    moving trees stays with _recover_stale_holds, invoked deliberately
    (`harness.py hold-recover`) or by the next gate run -- never as a side
    effect of an unrelated verb.

    `*-recovered` holds, unreadable manifests and live/unprobeable pids are all
    skipped: the detector reports only what it can positively prove abandoned.
    # ponytail: a recycled pid false-quiets the advisory; bounded by the
    # gate_hold_guard 30-min staleness warning, which still fires on tool calls.
    """
    out: list[dict[str, Any]] = []
    gate_hold = _gate_hold_root(Path(root))
    if not gate_hold.exists():
        return out  # common case: one existence check, ~zero overhead
    try:
        from harness_lib import processes
    except ImportError:  # run directly for the __main__ self-check
        import processes  # type: ignore
    for manifest_path in sorted(gate_hold.glob("*/hold.json")):
        hold = manifest_path.parent
        if hold.name.endswith("-recovered"):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pid = int(manifest.get("pid") or 0)
        except Exception:  # noqa: BLE001 -- unreadable manifest: not provably abandoned
            continue
        if pid <= 0 or processes.pid_alive(pid):
            continue
        out.append({"name": hold.name, "path": str(hold), "pid": pid,
                    "created": manifest.get("created")})
    return out


def warn_abandoned_holds(root: Path | str, stream: Any = None) -> int:
    """Loud read-only advisory per abandoned hold (returns the count).

    Called on EVERY harness CLI entry, so it must be near-zero when no hold
    exists and must NEVER raise or write to stdout (verbs keep stdout
    parseable; a broken advisory must never break a command)."""
    try:
        found = abandoned_holds(root)
        out = stream or sys.stderr
        for h in found:
            print(
                f"!! abandoned gate-hold detected: {h['name']} (gate pid {h['pid']} is dead).\n"
                f"   Your pre-gate .harness state is SAFE at {h['path']} -- nothing was lost.\n"
                f"   Restore it now: python scripts/harness.py hold-recover  "
                f"(or the next gate run restores it automatically).",
                file=out, flush=True)
        return len(found)
    except Exception:  # noqa: BLE001 -- an advisory must never break a verb
        return 0


def recover_abandoned_holds(root: Path | str) -> int:
    """Deliberate recovery (`harness.py hold-recover`): run the same
    _recover_stale_holds the next gate would, without starting a gate. Returns
    the number of provably-abandoned holds it acted on; a hold owned by a LIVE
    pid still raises (that is a running gate, not an abandoned hold).
    # ponytail: refuses when abandoned_holds() sees nothing, so the verb and the
    # advisory that points at it always agree; a hold only the age heuristic
    # would catch waits for the next gate run, exactly as before this verb.
    """
    root = Path(root)
    n = len(abandoned_holds(root))
    if n == 0:
        print("hold-recover: no abandoned gate-hold found (nothing to recover).", flush=True)
        return 0
    _recover_stale_holds(root)
    return n


def alive_python_processes() -> str:
    """Best-effort PID+cmdline dump of live python processes (F4b survivor scan)."""
    from harness_lib import processes  # local import: this module must stay import-light
    try:
        if os.name == "nt":
            out = processes.run_quiet(["wmic", "process", "where", "name like '%python%'",
                                       "get", "ProcessId,CommandLine", "/format:list"],
                                      capture_output=True, text=True, timeout=15).stdout
        else:
            raw = processes.run_quiet(["ps", "-eo", "pid,args"],
                                      capture_output=True, text=True, timeout=15).stdout
            out = "\n".join(ln for ln in raw.splitlines() if "python" in ln.lower())
        return out.strip() or "(none)"
    except Exception:
        return "(survivor scan failed)"


def capture_scenario_forensics(root: Path, stem: str, proc: Any) -> None:
    """F4b: on scenario FAILURE, preserve the leaked run-logs + supervisor logs + event
    tail + surviving python processes into a gitignored forensics dir BEFORE
    restore_volatile_state wipes them. Fail-open: a capture error must never break the gate."""
    try:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = root / ".harness" / "runs" / "scenario-forensics" / f"{stem}-{ts}"
        dest.mkdir(parents=True, exist_ok=True)
        active = root / ".harness" / "workflows" / "active"
        for src in list(active.glob("*/run-logs/*")) + list(active.glob("*/async/supervisor.*.log")):
            try:
                shutil.copy2(src, dest / f"{src.parent.parent.name}--{src.name}")
            except OSError:
                pass
        events = root / ".harness" / "runs" / "events.jsonl"
        if events.exists():
            lines = events.read_text(encoding="utf-8", errors="ignore").splitlines()
            (dest / "events-tail.jsonl").write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
        (dest / "survivors.txt").write_text(
            f"scenario={stem} rc={getattr(proc, 'returncode', '?')}\n\n{alive_python_processes()}\n",
            encoding="utf-8")
        # The check-level detail (e.g. rs:e2e-smoke per-worker rc/stderr from F5) lives in the
        # scenario's own stdout, which the gate otherwise truncates to its last line -- keep the
        # full stdout so a real flake self-diagnoses its worker-drop cause.
        stdout = getattr(proc, "stdout", "") or ""
        if stdout:
            (dest / "scenario-stdout.log").write_text(stdout, encoding="utf-8")
    except Exception:
        pass


def sweep_fixture_workflows(active_dir: Path | str) -> int:
    """Backstop that removes scenario residue a gate SCENARIO left in `active_dir`.

    The per-scenario snapshot/restore is the PRIMARY net, but a scenario that plans a
    workflow on the REAL root (serial lane, copy-skip recovery, serial fallback) can
    outlive the restore under a OneDrive lock -- that is the gate-scenarios-leak-wf-
    fixture-dirs(+tmp) leak. Called in the scenarios-gate finally. Removes exactly three
    residue classes, and ONLY these:
      (A) WF-* carrying a `.scenario-fixture` sidecar -- workflow_plan drops it under
          HARNESS_SCENARIO_ISOLATED, marking every workflow a SCENARIO plans WITHOUT
          touching workflow.json (the workflow contract stays identical to a real plan);
      (B) WF-* with NO workflow.json -- trace-only partials from a refused/aborted plan
          (mirrors spec_test_gate.is_fixture_workflow's incomplete-residue rule);
      (C) tmp* directories -- _writable_tempdir(active) scratch trees; nothing legitimate
          under active/ is named tmp*.
    A real owner round and the EXP-21 canaries have a valid workflow.json and NO sidecar
    (they are planned outside the gate env), so both are spared. Fail-open: never raises
    into the finally. Returns the count removed."""
    active = Path(active_dir)
    if not active.exists():
        return 0
    n = 0
    try:  # whole-body guard: a best-effort backstop MUST NOT raise into the gate's release/
        try:  # finally (that would skip the caller's shadow_ledger.commit + perf flush)
            from harness_lib import processes
        except ImportError:  # run directly for the __main__ self-check
            import processes  # type: ignore
        for wf in sorted(active.glob("WF-*")):
            if not wf.is_dir():
                continue  # a stray WF-* FILE is not a workflow dir (and rmtree could not remove it)
            if (wf / ".scenario-fixture").exists():
                pass  # (A) a gate scenario planned it -- sweep
            elif not (wf / "workflow.json").exists():
                pass  # (B) trace-only partial from a refused/aborted plan -- sweep
            else:
                continue  # a real workflow (owner round / EXP-21 canary) -- spare it
            try:  # kill any recorded worker tree before removing its state dir
                processes.cleanup_recorded_tree(wf, "scenario_isolation.sweep_fixture_workflows")
            except Exception:  # noqa: BLE001 -- best-effort; rmtree still proceeds
                pass
            shutil.rmtree(wf, ignore_errors=True)
            n += 1
        for scratch in sorted(active.glob("tmp*")):  # _writable_tempdir(active) leftovers
            if scratch.is_dir():
                shutil.rmtree(scratch, ignore_errors=True)
                n += 1
    except Exception:  # noqa: BLE001 -- honor the "never raises" contract even if glob/import surprises
        pass
    return n


def _selfcheck() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / ".harness" / "state"
        active = root / ".harness" / "workflows" / "active"
        runs = root / ".harness" / "runs"
        context = root / ".harness" / "context"
        runtime = root / ".harness" / "runtime"
        docs = root / "docs"
        for d in (state, active, runs, context, runtime, docs):
            d.mkdir(parents=True)
        (state / "escalations.json").write_text('{"pending": [1, 2]}', encoding="utf-8")
        (active / "wf-real").mkdir()
        (runs / "events.jsonl").write_text("e1\ne2\n", encoding="utf-8")
        (context / "NEXT_STEPS.md").write_text("real next steps\n", encoding="utf-8")
        (runtime / "executor-circuit-local-llama.json").write_text('{"state": "closed"}', encoding="utf-8")
        (docs / "IMPLEMENTATION_BACKLOG.md").write_text("real backlog\n", encoding="utf-8")
        state_store = root / ".harness" / "state-store"
        state_store.mkdir(parents=True)
        (state_store / "records.db").write_text("stale-rows", encoding="utf-8")

        hold = root / ".hold"
        token = snapshot_volatile_state(root, hold)

        # A scenario pollutes: mutate a state file, add a new state file, create a
        # workflow, append to the log, trip a circuit breaker open, and clobber the backlog.
        (state / "escalations.json").write_text('{"pending": []}', encoding="utf-8")
        (state / "brand-new.json").write_text("{}", encoding="utf-8")
        (active / "wf-scenario-junk").mkdir()
        with (runs / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("e3-scenario-noise\n")
        (context / "NEXT_STEPS.md").write_text("CLOBBERED steps\n", encoding="utf-8")
        (runtime / "executor-circuit-local-llama.json").write_text('{"state": "open"}', encoding="utf-8")
        (runtime / "executor-circuit-leaked.json").write_text('{"state": "open"}', encoding="utf-8")
        (docs / "IMPLEMENTATION_BACKLOG.md").write_text("CLOBBERED\n", encoding="utf-8")
        (state_store / "records.db").write_text("scenario-rows-with-stale-mtime", encoding="utf-8")

        restore_volatile_state(token)

        assert (state / "escalations.json").read_text(encoding="utf-8") == '{"pending": [1, 2]}', "state file not restored"
        assert not (state / "brand-new.json").exists(), "scenario-added state file not removed"
        assert (active / "wf-real").exists(), "real workflow lost"
        assert not (active / "wf-scenario-junk").exists(), "scenario workflow not scrubbed"
        assert (runs / "events.jsonl").read_text(encoding="utf-8") == "e1\ne2\n", "log not truncated back"
        assert (context / "NEXT_STEPS.md").read_text(encoding="utf-8") == "real next steps\n", "context not restored"
        assert (runtime / "executor-circuit-local-llama.json").read_text(encoding="utf-8") == '{"state": "closed"}', "runtime circuit not restored"
        assert not (runtime / "executor-circuit-leaked.json").exists(), "scenario-leaked circuit not removed"
        assert (docs / "IMPLEMENTATION_BACKLOG.md").read_text(encoding="utf-8") == "real backlog\n", "backlog not restored"
        assert not state_store.exists(), "derived state-store cache not wiped on restore"

    # N3-F2: a hold owned by ANOTHER live pid refuses recovery (concurrent-gate
    # mutex); the parent shell's pid stands in for "another gate still running".
    # Own-pid holds stay recoverable (crash-simulation path, si-3c).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        live_hold = root / ".harness" / "runs" / "gate-hold" / "20990101T000000Z-live"
        live_hold.mkdir(parents=True)
        (live_hold / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": os.getppid(), "entries": []}), encoding="utf-8")
        try:
            _recover_stale_holds(root)
            raise AssertionError("live-pid hold must refuse recovery")
        except RuntimeError as e:
            assert "LIVE pid" in str(e), e

    # ...while a dead-pid hold is still recovered exactly as before (renamed, kept).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dead_hold = root / ".harness" / "runs" / "gate-hold" / "20990101T000001Z-dead"
        dead_hold.mkdir(parents=True)
        (dead_hold / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": 2_000_000_000, "entries": []}), encoding="utf-8")
        _recover_stale_holds(root)
        assert not dead_hold.exists(), "dead-pid hold not recovered"
        assert (dead_hold.parent / f"{dead_hold.name}-recovered").exists(), "recovered rename missing"

    # Detector (row gate-hold-abandoned-detector): dead pid reported; live pid,
    # `-recovered` and garbage manifests silent; the deliberate verb recovers.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gh = root / ".harness" / "runs" / "gate-hold"
        import io
        assert abandoned_holds(root) == [], "no gate-hold dir must report nothing"
        dead = gh / "20990101T000002Z-dead"
        dead.mkdir(parents=True)
        (dead / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": 2_000_000_000, "entries": []}), encoding="utf-8")
        live = gh / "20990101T000003Z-live"
        live.mkdir(parents=True)
        (live / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": os.getpid(), "entries": []}), encoding="utf-8")
        rec = gh / "20990101T000004Z-x-recovered"
        rec.mkdir(parents=True)
        (rec / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": 2_000_000_001, "entries": []}), encoding="utf-8")
        garbage = gh / "20990101T000005Z-garbage"
        garbage.mkdir(parents=True)
        (garbage / "hold.json").write_text("{not json", encoding="utf-8")
        found = abandoned_holds(root)
        assert [h["name"] for h in found] == [dead.name], found
        buf = io.StringIO()
        assert warn_abandoned_holds(root, stream=buf) == 1
        advisory = buf.getvalue()
        assert dead.name in advisory and "hold-recover" in advisory and "SAFE" in advisory, advisory
        # the verb recovers what the advisory named; live/-recovered/garbage stay put
        live.rename(gh / "gone")  # a live-pid hold would (rightly) refuse recovery
        shutil.rmtree(gh / "gone")
        assert recover_abandoned_holds(root) == 1
        assert (gh / f"{dead.name}-recovered").exists(), "verb did not recover"
        assert recover_abandoned_holds(root) == 0, "second run must find nothing"

    # cli-gate-hold-write-guard (owner 2026-07-29; audit fix 2026-07-31): a sensitive
    # verb refuses whenever a hold HOLDS the tree — live OR abandoned-but-unrecovered
    # (gate_holding), the live pid only enriching the message; read/recovery/gate verbs
    # and the in-gate env pass; a *-recovered remnant is NOT held and passes. The env is
    # popped for the fires-arm because THIS self-check runs inside gate scenarios with it set.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert hold_write_guard("tasks", root) is None, "no hold dir must not guard"
        gh = root / ".harness" / "runs" / "gate-hold"
        hold = gh / "20990101T000006Z-guard"
        hold.mkdir(parents=True)
        (hold / "hold.json").write_text(json.dumps(
            {"created": "t", "pid": os.getppid(), "entries": []}), encoding="utf-8")
        saved_env = os.environ.pop("HARNESS_SCENARIO_ISOLATED", None)
        try:
            reason = hold_write_guard("tasks", root)
            assert reason and "gate" in reason and "discard" in reason, reason
            assert "20990101T000006Z-guard" in reason, "reason must name the hold"
            assert hold_write_guard("status", root) is None, "read verb never guarded"
            assert hold_write_guard("hold-recover", root) is None, "recovery verb exempt"
            assert hold_write_guard("gate-staged", root) is None, "the gate itself exempt"
            os.environ["HARNESS_SCENARIO_ISOLATED"] = "1"
            assert hold_write_guard("tasks", root) is None, "in-gate contract exempt"
            del os.environ["HARNESS_SCENARIO_ISOLATED"]
            # abandoned (dead-pid) hold STILL holds the tree, so it now REFUSES — it
            # used to be a live-only miss, the `chat` silent-loss gap (audit 2026-07-31).
            # live_holds is empty, so the message names an unrecovered hold, not a pid.
            (hold / "hold.json").write_text(json.dumps(
                {"created": "t", "pid": 2_000_000_000, "entries": []}), encoding="utf-8")
            ab = hold_write_guard("tasks", root)
            assert ab and "unrecovered" in ab, f"abandoned hold must refuse: {ab!r}"
            # a *-recovered remnant is NOT held (the tree was restored) -> allow, or the
            # ledger writers would refuse FOREVER after any historical crashed gate.
            hold.rename(gh / "20990101T000006Z-guard-recovered")
            assert hold_write_guard("tasks", root) is None, "recovered remnant not held"
        finally:
            if saved_env is not None:
                os.environ["HARNESS_SCENARIO_ISOLATED"] = saved_env
    print("scenario_isolation self-check: ok")


def cmd_hold(args: Any) -> None:
    """CLI handler for `harness hold` (PA-12)."""
    action = getattr(args, "action", "list") or "list"
    root = ROOT
    if action == "list":
        holds = live_holds(root)
        if getattr(args, "json", False):
            print(json.dumps(holds, indent=2))
        else:
            if not holds:
                print("No active or unrecovered gate-holds found.")
            for h in holds:
                print(f"Hold: {h['name']} | PID: {h['pid']} | Created: {h['created']}")
    elif action == "recover":
        cnt = recover_abandoned_holds(root)
        print(f"Recovered {cnt} abandoned gate-hold(s).")
    elif action in ("inspect", "abandon"):
        target = getattr(args, "name", None)
        if not target:
            raise HarnessError(f"hold {action}: hold name or ID required")
        gh_dir = _gate_hold_root(Path(root))
        match_dir = None
        if gh_dir.exists():
            for d in gh_dir.glob("*"):
                if d.name == target or target in d.name:
                    match_dir = d
                    break
        if not match_dir:
            raise HarnessError(f"Hold '{target}' not found under {gh_dir}")
        if action == "inspect":
            m_path = match_dir / "hold.json"
            if m_path.exists():
                print(m_path.read_text(encoding="utf-8"))
            else:
                print(f"Hold directory exists: {match_dir.name}")
        elif action == "abandon":
            rec = match_dir.parent / f"{match_dir.name}-abandoned"
            match_dir.rename(rec)
            event_ledger.append_critical("gate_hold_abandoned_manually", {"holdName": match_dir.name, "abandonedTo": rec.name}, root=root)
            print(f"Marked hold {match_dir.name} as abandoned -> {rec.name}")


if __name__ == "__main__":
    _selfcheck()

