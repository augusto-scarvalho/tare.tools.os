#!/usr/bin/env python3
"""tasks-store-cli (A1) — the canonical task store (owner decision 2026-07-13 #3).

`.harness/state/tasks.json` is the OPERATIONAL TRUTH for task lanes/status/
priority/wfid, written ONLY through the `tasks` CLI verb (single-write-path
rule, mirroring targets register). The backlog markdown stays the human prose
document: after `tasks import` its tables are HISTORY — the board collector
(tasks_board) stops reading them and reads this store instead (structural
freeze; nothing rewrites the markdown).

Lanes: manual rows live in idea|backlog|ready|queued|done. `running`/`review`
are DERIVED per poll from live workers (tasks_board) and are refused as manual
move targets — in-execution state is never stored (decision #3).

`import_backlog` is idempotent, keyed by row id: re-import updates
title/size/priority metadata but PRESERVES an existing row's lane, wfid and
timestamps (a manual move survives re-import); struck rows import as done;
rows from the transient "LOOP QUEUE" ordering section are skipped (they
duplicate roadmap rows). Nothing here runs against the real repo until the
owner runs `tasks import` themselves.

Self-check: `python scripts/harness_lib/tasks_store.py`.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import functools
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import epoch  # noqa: E402
from harness_lib.common import read_json, write_json  # noqa: E402

STORE_REL = ".harness/state/tasks.json"
MANUAL_LANES = ("idea", "backlog", "ready", "queued", "done")
DERIVED_LANES = ("running", "review")  # never stored; derived per poll
_LOCK_STALE_S = 30.0  # a lock older than this had its holder die; break it loudly
_LOCK_DEPTH = 0       # in-process re-entrancy depth (close -> move nests)

# SPEC-155 entry-groom: a standalone `no-deps` marker (same slug charset family,
# whole-token, so a `depends-on:` tag never matches it).
_NODEPS = re.compile(r"(?<![A-Za-z0-9._-])no-deps(?![A-Za-z0-9._-])", re.IGNORECASE)


def _maybe_shipped(root: Path, slug: str) -> list[str]:
    """Heuristic shipped-check (SPEC-155): same-named acceptance scenario or
    harness_lib module files (the task may already be done -> close via SPEC-154).
    Fail-open: any filesystem error reads as no match."""
    token = re.sub(r"[^A-Za-z0-9]+", "", slug).lower()
    if len(token) < 3:  # too short to be a meaningful name match
        return []
    hits: list[str] = []
    try:
        for rel in ("testing/scenarios", "scripts/harness_lib"):
            for f in sorted((Path(root) / rel).glob("*.py")):
                if token in re.sub(r"[^A-Za-z0-9]+", "", f.stem).lower():
                    hits.append(f"{rel}/{f.name}")
    except OSError:
        pass
    return hits


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def store_path(root: Path | str) -> Path:
    return Path(root) / STORE_REL


def load(root: Path | str) -> dict[str, Any]:
    """The store document, or an empty shell when absent."""
    return read_json(store_path(root), None) or {"schemaVersion": "1.0", "tasks": []}


@contextlib.contextmanager
def store_lock(root: Path | str, timeout: float = 10.0):
    """Serialize whole-document read-modify-write across processes.

    Every verb does load -> mutate -> `_save`, so two concurrent writes lose one.
    Two agents editing different lines of a markdown table both survived; the
    single canonical JSON does not, and this plan deliberately multiplies writers
    onto that path — so the race stops being theoretical and becomes the expected
    traffic. `O_CREAT|O_EXCL` is the portable primitive (this runs on Windows).
    A lock whose owner pid is gone is broken through after `_LOCK_STALE_S`, loudly.
    ponytail: one global lock; per-id merge only if contention is ever measured."""
    global _LOCK_DEPTH
    if _LOCK_DEPTH:  # re-entrant within one process (close -> move would self-deadlock)
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    path = Path(root) / STORE_REL
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                holder = lock.read_text(encoding="utf-8").strip()
            except OSError:
                continue  # vanished between the two calls: retry immediately
            if age > _LOCK_STALE_S:
                print(f"warning: breaking a stale tasks-store lock ({age:.0f}s old, "
                      f"holder {holder or 'unknown'})", file=sys.stderr)
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"tasks store is locked by {holder or 'another process'} "
                    f"({age:.0f}s); retry, or remove {lock} if that process is gone")
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _LOCK_DEPTH = 1
        yield
    finally:
        _LOCK_DEPTH = 0
        lock.unlink(missing_ok=True)


def _locked(fn):
    """Every store MUTATION runs under the lock (the read verbs stay lock-free)."""
    @functools.wraps(fn)
    def wrapper(root, *args, **kwargs):
        with store_lock(root):
            return fn(root, *args, **kwargs)
    return wrapper


def _save(root: Path | str, doc: dict[str, Any]) -> None:
    doc.setdefault("schemaVersion", "1.0")
    doc.setdefault(
        "purpose",
        "Canonical task store (owner decision 2026-07-13 #3): lanes/status/priority/wfid "
        "operational truth, written ONLY by the `tasks` CLI verb. Backlog markdown tables "
        "are frozen history after `tasks import`. running/review lanes are derived per "
        "poll from live workers, never stored here.")
    write_json(store_path(root), doc)


def _in_archive(text: str, task_id: str) -> bool:
    """The id appears as a whole token in docs/backlog-archive.md (a substring
    match would let `wiki-screen` answer for `wiki-screen-port`)."""
    return bool(re.search(r"(?<![A-Za-z0-9._-])" + re.escape(task_id)
                          + r"(?![A-Za-z0-9._-])", text))


@_locked
def import_backlog(root: Path | str, *, migrate: bool = False) -> dict[str, Any]:
    """Idempotent import of the backlog tables into the store (freeze point).

    `migrate=True` is the ONE-TIME reconciliation (backlog-json-canonical Phase 2)
    and adds three things the steady-state import must never do: it REFUSES to
    write while a duplicate id is unresolved, it PROPAGATES a struck document row
    onto a store row still marked open, and it retires store rows the document no
    longer carries (archived -> done+archivedAt; the rest reported as orphans).
    Steady-state preservation of a manual lane move is deliberate and stays."""
    from harness_lib import tasks_board  # local import: board also imports us

    root = Path(root)
    doc = load(root)
    existing = {t["id"]: t for t in doc["tasks"]}
    added = updated = skipped = propagated = 0
    seen: dict[str, int] = {}  # ids met in THIS parse -> collision detection
    for row in tasks_board._backlog_rows(root):
        if not tasks_board.is_task_row(row):
            skipped += 1  # loop-queue view / GUI phase matrix / auto-inbox triage
            continue
        rid = row["id"]
        seen[rid] = seen.get(rid, 0) + 1
        # `body` + `deps` ride along with the metadata: importing only the capped
        # title dropped the prose, and omitting deps left topo_order/dep_graph/
        # provenance running on an EMPTY graph (measured 2026-07-27: 77 of 198
        # rows carry dep tags, 0 of 284 board rows did).
        meta = {"title": row["title"], "category": row["category"],
                "size": row.get("size"), "priority": row.get("priority"),
                "section": row.get("section"), "source": row.get("source"),
                "body": row.get("body"), "deps": row.get("deps")}
        if rid in existing:
            task = existing[rid]
            if any(task.get(k) != v for k, v in meta.items()):
                task.update(meta)
                task["updatedAt"] = _now()
                updated += 1
            # Invariant 2 preserves a manual lane move across re-imports — right
            # while the document mirrored the store, wrong for the one-time
            # migration: a struck row IS the owner closing it, and 15 such closures
            # sat stranded as `backlog` in the store (measured 2026-07-27).
            if migrate and row["lane"] == "done" and task.get("lane") != "done":
                task["lane"] = "done"
                task.setdefault("closedAt", _now())
                task["updatedAt"] = _now()
                propagated += 1
            continue  # lane/wfid/createdAt preserved — a manual move survives re-import
        existing[rid] = {"id": rid, **meta,
                         "lane": "done" if row["lane"] == "done" else "backlog",
                         "wfid": None, "createdAt": _now(), "updatedAt": _now()}
        added += 1
    # A doc-side id collision makes the LAST row silently win — in a table a human
    # can see the twin, in the store it is one row. Report, never swallow; and on a
    # MIGRATION refuse outright, before anything is written.
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    if duplicates:
        print(f"warning: duplicate backlog id(s), last row wins per id: "
              f"{', '.join(duplicates)}", file=sys.stderr)
    if migrate and duplicates:
        raise ValueError("duplicate backlog id(s) — resolve them in the document "
                         f"before migrating: {', '.join(duplicates)}")
    archived = 0
    orphans: list[str] = []
    purged: list[str] = []
    if migrate:
        # Rows the document no longer carries. Retiring them is what `import` never
        # did (additive-only), which is how 84 rows archived in the 2026-07-19 grooms
        # stayed on the board. Parse artifacts that earlier imports minted as tasks
        # are DROPPED (they were never tasks); the rest are archived-done or reported.
        arch = root / "docs" / "backlog-archive.md"
        try:
            arch_text = arch.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            arch_text = ""
        for task in list(existing.values()):
            tid = task["id"]
            if not tasks_board.is_task_row(task):
                purged.append(tid)
                del existing[tid]
                continue
            if tid in seen:
                continue
            if _in_archive(arch_text, tid):
                if task.get("lane") != "done":
                    task["lane"] = "done"
                task.setdefault("closedAt", _now())
                task.setdefault("archivedAt", _now())
                task["updatedAt"] = _now()
                archived += 1
            # Neither in the document nor the archive. Harmless in two shapes: a row
            # already CLOSED (history, e.g. an id carrying a `(safe half)` suffix),
            # and a row `tasks add` created that never had a document counterpart —
            # the store-only case the design intends. What must never pass silently
            # is an OPEN row still claiming the document as its source.
            elif task.get("lane") != "done" and task.get("source") == tasks_board.BACKLOG_REL:
                orphans.append(tid)
        orphans.sort()
        if orphans:
            print(f"warning: {len(orphans)} store row(s) in neither the document nor "
                  f"the archive — triage: {', '.join(orphans)}", file=sys.stderr)
    doc["tasks"] = list(existing.values())
    doc["frozenBacklogAt"] = doc.get("frozenBacklogAt") or _now()
    _save(root, doc)
    return {"added": added, "updated": updated, "skipped": skipped,
            "total": len(doc["tasks"]), "frozenBacklogAt": doc["frozenBacklogAt"],
            "duplicates": duplicates, "propagated": propagated,
            "archived": archived, "purged": purged, "orphans": orphans}


@_locked
def add(root: Path | str, title: str, *, task_id: str | None = None,
        category: str = "feature", size: str | None = None,
        priority: str | None = None, lane: str = "backlog",
        body: str | None = None) -> dict[str, Any]:
    if lane not in MANUAL_LANES:
        raise ValueError(f"lane must be one of {MANUAL_LANES}, got {lane!r}")
    doc = load(root)
    rid = (task_id or title.lower().replace(" ", "-")[:40]).strip()
    if not rid:
        raise ValueError("empty task id")
    if any(t["id"] == rid for t in doc["tasks"]):
        raise ValueError(f"task id already exists: {rid}")
    # A row's prose is the row. REFUSED over cap, never truncated: the store exists
    # because the 160-char title cap silently discarded 79% of the corpus, and a
    # degradation that drops work is the defect this migration removes.
    body = (body or "").strip() or None
    if body and len(body) > BODY_CAP:
        raise ValueError(f"body is {len(body)} chars, over the {BODY_CAP} cap — split the "
                         f"task or move the detail to `tasks plan {rid}`")
    # SPEC-155 entry-groom: nothing enters un-assessed. A task added with no
    # deps-as-tag in its title or body records the assessment STRUCTURALLY
    # (`noDeps`), which is also the only home a store-only row has for it.
    from harness_lib import tasks_board  # local import: board also imports us
    text = title.strip()
    deps = tasks_board._parse_deps(f"{text}\n{body or ''}")
    task = {"id": rid, "title": text[:160], "category": category,
            "size": size, "priority": priority, "section": "manual",
            "source": "tasks add", "lane": lane, "wfid": None,
            "body": body, "deps": deps, "noDeps": not any(deps.values()),
            "createdAt": _now(), "updatedAt": _now()}
    doc["tasks"].append(task)
    _save(root, doc)
    # SPEC-155 shipped-check WARNING (heuristic, non-blocking): a same-named
    # scenario/module may mean this is already shipped -> verify + close (SPEC-154).
    shipped = _maybe_shipped(Path(root), rid)
    if shipped:
        print(f"warning: shipped-check: task {rid} may already be shipped -- matches "
              f"{', '.join(shipped)}; verify + close via SPEC-154", file=sys.stderr)
    return task


@_locked
def set_dep(root: Path | str, task_id: str, kind: str, other: str | None = None) -> dict[str, Any]:
    """Record a dependency ASSESSMENT on a store row (SPEC-155 entry-groom).

    `kind` is one of the deps-as-tags kinds, or the literal `no-deps` for the
    explicit "assessed, and there are none" marker. Document rows carry the
    assessment in their prose (`body`); a row created by `tasks add` has no prose
    to carry it, which is why the marker also needs a structural home."""
    from harness_lib import tasks_board  # local import: board also imports us

    doc = load(root)
    task = _find(doc, task_id)
    if kind == "no-deps":
        task["noDeps"] = True
    else:
        key = tasks_board._DEP_KEY.get(kind.lower())
        if not key:
            raise ValueError(f"dep kind must be one of "
                             f"{'|'.join(tasks_board._DEP_KEY)}|no-deps, got {kind!r}")
        if not other:
            raise ValueError(f"usage: tasks dep <id> {kind} <otherId>")
        deps = task.get("deps") or tasks_board._parse_deps("")
        if other not in deps[key]:
            deps[key].append(other)
        task["deps"] = deps
        task["noDeps"] = False
    task["updatedAt"] = _now()
    _save(root, doc)
    return task


@_locked
def set_meta(root: Path | str, task_id: str, *, priority: str | None = None,
             size: str | None = None, category: str | None = None) -> dict[str, Any]:
    """Re-grade an existing row (priority/size/category) through the single
    write path — until this verb the only options were delete+re-add (loses
    notes/plan/dispatch) or hand-editing the store (violates decision #3).
    Values are free-form on purpose: the board's priority rank tolerates the
    corpus (P1..Pn, high/med/low, owner-gated…), so normalization is ordinary
    grooming with this verb, not a schema migration."""
    if priority is None and size is None and category is None:
        raise ValueError("usage: tasks set <id> --priority P|--size S|--category C "
                         "(at least one)")
    doc = load(root)
    task = _find(doc, task_id)
    for key, val in (("priority", priority), ("size", size), ("category", category)):
        if val is not None:
            task[key] = val.strip() or None  # `--priority ""` clears the field
    task["updatedAt"] = _now()
    _save(root, doc)
    return task


@_locked
def move(root: Path | str, task_id: str, lane: str) -> dict[str, Any]:
    if lane in DERIVED_LANES:
        raise ValueError(f"lane {lane!r} is derived from live workers, never stored")
    if lane not in MANUAL_LANES:
        raise ValueError(f"lane must be one of {MANUAL_LANES}, got {lane!r}")
    doc = load(root)
    for task in doc["tasks"]:
        if task["id"] == task_id:
            task["lane"] = lane
            task["updatedAt"] = _now()
            if lane == "done":
                task["closedAt"] = _now()
            _save(root, doc)
            return task
    raise KeyError(f"unknown task id: {task_id}")


def close(root: Path | str, task_id: str) -> dict[str, Any]:
    return move(root, task_id, "done")


def _find(doc: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in doc["tasks"]:
        if task["id"] == task_id:
            return task
    raise KeyError(f"unknown task id: {task_id}")


NOTES_CAP = 16000
PLAN_CAP = 64000
# The row's own prose. Same ceiling as NOTES_CAP; the largest row migrated from the
# document was 8,959 chars, so the corpus fits with room. Over cap is REFUSED, never
# truncated (the 160-char title cap that discarded 79% of the corpus is the reason
# this field exists at all).
BODY_CAP = 16000


@_locked
def set_note(root: Path | str, task_id: str, text: str) -> dict[str, Any]:
    """Owner's complementary notes for the task modal (planner input)."""
    doc = load(root)
    task = _find(doc, task_id)
    task["notes"] = str(text)[:NOTES_CAP]
    task["updatedAt"] = _now()
    _save(root, doc)
    return task


@_locked
def append_plan(root: Path | str, task_id: str, text: str,
                source: str | None = None) -> dict[str, Any]:
    """Increment the task's plan with an owner-approved refinement. The plan
    field IS the refinement link the demand requires: it stays on the row
    through done, so the task never loses its plan lineage."""
    text = str(text).strip()
    if not text:
        raise ValueError("empty plan text")
    doc = load(root)
    task = _find(doc, task_id)
    existing = str(task.get("plan") or "")
    task["plan"] = ((existing + "\n\n---\n\n" if existing else "") + text)[:PLAN_CAP]
    task["planRefinedAt"] = _now()
    if source:
        task["planSource"] = str(source)
    task["updatedAt"] = _now()
    _save(root, doc)
    return task


@_locked
def remove(root: Path | str, task_id: str) -> dict[str, Any]:
    doc = load(root)
    task = _find(doc, task_id)  # unknown id raises before any write
    doc["tasks"] = [t for t in doc["tasks"] if t["id"] != task_id]
    _save(root, doc)
    return task


@_locked
def set_dispatch(root: Path | str, task_id: str, info: dict[str, Any],
                 *, force_epoch: bool = False) -> dict[str, Any]:
    """Record a route-dispatch on the task and park it in `queued` — the board
    DERIVES running/done from the dispatch's live pid/log per poll (decision #3:
    in-execution state is never stored as a lane).

    SPEC-149 fencing (regra 3/5): the card records the dispatching run's
    `dispatchEpoch`. A caller whose epoch is LOWER than the recorded one is a
    stale run re-dispatching over a newer owner -> refuse (unless force_epoch,
    which overrides loudly, regra 4). A caller WITHOUT an epoch is a legacy
    writer -> advisory `epochMissing`, never blocked (ratchet, regra 5)."""
    doc = load(root)
    task = _find(doc, task_id)
    caller = epoch.caller_epoch()
    prior = task.get("dispatchEpoch")
    if caller is None:
        if isinstance(prior, int):
            task["epochMissing"] = True
    else:
        if isinstance(prior, int) and caller < prior:
            if not force_epoch:
                raise ValueError(
                    f"task {task_id} was dispatched by epoch {prior}; this run is epoch "
                    f"{caller} (stale — a newer run took ownership). Pass force_epoch=True "
                    f"to override and re-dispatch.")
            epoch.record_override(root, taskId=task_id, target="task",
                                  priorEpoch=prior, callerEpoch=caller)
        task["dispatchEpoch"] = caller
        task.pop("epochMissing", None)
    task["dispatch"] = {"pid": info.get("pid"), "log": info.get("log"),
                        "at": _now(), "route": info.get("route")}
    task["lane"] = "queued"
    task["updatedAt"] = _now()
    _save(root, doc)
    return task


def board_rows(root: Path | str) -> list[dict[str, Any]] | None:
    """Store rows in the board's row shape, or None when no store exists yet
    (the board then falls back to parsing the backlog markdown — pre-freeze)."""
    path = store_path(root)
    if not path.exists():
        return None
    doc = read_json(path, None) or {}
    rows: list[dict[str, Any]] = []
    for t in doc.get("tasks", []):
        rows.append({"id": t.get("id"), "title": t.get("title"),
                     "category": t.get("category") or "feature",
                     "lane": t.get("lane") or "backlog",
                     "size": t.get("size"), "priority": t.get("priority"),
                     "section": t.get("section") or "store",
                     "source": STORE_REL, "wfid": t.get("wfid"),
                     # body = the row's full prose; deps = the structured dep map
                     # consumed AS-IS by topo_order/dep_graph (never re-parsed from
                     # free text). Absent deps stay falsy — tasks_snapshot fills the
                     # uniform empty shape for every row source in one place.
                     "body": t.get("body"), "deps": t.get("deps"),
                     "noDeps": t.get("noDeps"),  # SPEC-155: explicit "assessed, none"
                     # task-modal fields: notes/plan feed the card dialog; dispatch
                     # feeds the derived running/done lanes (tasks_board).
                     "notes": t.get("notes"), "plan": t.get("plan"),
                     "planRefinedAt": t.get("planRefinedAt"),
                     "dispatch": t.get("dispatch")})
    return rows


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "IMPLEMENTATION_BACKLOG.md").write_text(
            "## Features\n| ID | Item | Size |\n|---|---|---|\n"
            "| F.1 | build | M |\n| ~~F.2~~ ✅ | shipped | S |\n"
            "### LOOP QUEUE 2026-01-01\n| # | item |\n|---|---|\n| 1 | dup |\n",
            encoding="utf-8")
        out = import_backlog(root)
        assert out["added"] == 2 and out["skipped"] == 1, out
        # idempotent: no dupes, manual move survives.
        move(root, "F.1", "ready")
        out2 = import_backlog(root)
        assert out2["added"] == 0 and out2["total"] == 2, out2
        rows = {r["id"]: r for r in board_rows(root)}
        assert rows["F.1"]["lane"] == "ready" and rows["F.2"]["lane"] == "done", rows
        add(root, "new manual task", task_id="man-1")
        try:
            move(root, "man-1", "running")
            raise AssertionError("derived lane accepted")
        except ValueError:
            pass
        try:
            add(root, "dup", task_id="man-1")
            raise AssertionError("dup id accepted")
        except ValueError:
            pass
        assert close(root, "man-1")["lane"] == "done"
        # task-modal fields: notes, plan increments (link survives done), dispatch, remove.
        assert set_note(root, "F.1", "needs the auth seam")["notes"] == "needs the auth seam"
        assert append_plan(root, "F.1", "step 1")["plan"] == "step 1"
        p2 = append_plan(root, "F.1", "refined", source="run-x.plan.md")
        assert p2["plan"] == "step 1\n\n---\n\nrefined" and p2["planSource"] == "run-x.plan.md"
        try:
            append_plan(root, "F.1", "   ")
            raise AssertionError("empty plan accepted")
        except ValueError:
            pass
        d = set_dispatch(root, "F.1", {"pid": 4242, "log": ".harness/runs/x.log"})
        assert d["lane"] == "queued" and d["dispatch"]["pid"] == 4242
        # SPEC-149 fencing: no env epoch -> advisory (no dispatchEpoch, no block);
        # with an epoch a lower caller is refused, equal/higher and force pass.
        assert "dispatchEpoch" not in d and "epochMissing" not in d, d
        import os as _os
        _os.environ[epoch.ENV_EPOCH] = "5"
        try:
            assert set_dispatch(root, "F.1", {"pid": 1})["dispatchEpoch"] == 5
            _os.environ[epoch.ENV_EPOCH] = "3"
            try:
                set_dispatch(root, "F.1", {"pid": 2})
                raise AssertionError("stale epoch dispatch accepted")
            except ValueError:
                pass
            assert set_dispatch(root, "F.1", {"pid": 3}, force_epoch=True)["dispatchEpoch"] == 3
            _os.environ[epoch.ENV_EPOCH] = "9"
            assert set_dispatch(root, "F.1", {"pid": 4})["dispatchEpoch"] == 9
        finally:
            _os.environ.pop(epoch.ENV_EPOCH, None)
        assert close(root, "F.1")["lane"] == "done"
        row = {r["id"]: r for r in board_rows(root)}["F.1"]
        assert row["plan"] and row["notes"] and row["dispatch"], row  # link kept through done
        assert remove(root, "man-1")["id"] == "man-1"
        try:
            remove(root, "man-1")
            raise AssertionError("double remove accepted")
        except KeyError:
            pass
    print("tasks_store self-check: ok")


if __name__ == "__main__":
    _demo()
