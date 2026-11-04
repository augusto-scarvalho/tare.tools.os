#!/usr/bin/env python3
"""tasks-board-read (A0) — derived read-only Kanban snapshot + the `tasks` verb.

There is NO canonical task store yet (that is tasks-store-cli / owner decision
2026-07-13 #3, the next increment): this collector DERIVES the board on every
call from signals already on disk and stores nothing —

  * backlog rows   — `|`-leading table rows in docs/IMPLEMENTATION_BACKLOG.md
                     (tolerant per-row; a garbage line is skipped, never a crash);
  * bug-ish rows   — the self-review inbox table + pending escalations
                     (.harness/state/escalations.json, raised minus resolved);
  * live rows      — active workflow workers via chat_hud.read_workers
                     (lane `review` for review/security profiles, else `running`).

Lanes: backlog | running | review | done (struck/✅ rows). Categories are
derived from the row's section: Deferred/Ideation → ideation; self-review
inbox → chore (budget/burndown findings) or bug; everything else → feature.

SPEC-110 per-target grouping (read-only): harness rows carry scope="harness";
each registered target (`.harness/targets/<name>/target.json`) contributes a
group scoped to its own tasks. The per-target source is the roadmap-decided
placement `.harness/state/targets/<name>/tasks.json` — no writer populates it
yet, so target groups render as calm empty boards (`perTargetSource: false`).

CLI `tasks list [--category] [--lane] [--json]` registers via cli_registry
(zero harness.py edits). Agent-facing output reuses the TE.5 compact path
(owner decision #3 rider): under HARNESS_AGENT_OUTPUT=compact the rows emit as
TSV via common.emit, never pretty JSON. Read-only, rc 0.

Self-check: `python scripts/harness_lib/tasks_board.py`.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import chat_hud  # noqa: E402
from harness_lib import common  # noqa: E402
from harness_lib.common import HarnessError, read_json  # noqa: E402

BACKLOG_REL = "docs/IMPLEMENTATION_BACKLOG.md"
TARGETS_REL = ".harness/targets"               # SPEC-110 registry (one dir per target)
TARGET_STATE_REL = ".harness/state/targets"    # per-target generated state (mirrors quality-state)
LANES = ("backlog", "running", "review", "done")
# First-cell tokens that mark a table HEADER row (lowercased). Everything else
# with >= 2 cells is data; separator rows (---) are matched structurally.
_HEADER_CELLS = {"id", "slug", "item", "finding", "#", "phase", "fase", "source", "lane",
                 "decisão", "decision", "directive (line)", "subject"}
_SEP_CELL = re.compile(r"^:?-+:?$")
_SIZE = re.compile(r"^(S|M|L|XL)\b")
_PRI = re.compile(r"^(P\d|high|med|low|owner|open)\b", re.IGNORECASE)
_REVIEW_PROFILES = {"review", "security"}


def _clean(cell: str) -> str:
    # `⤴` marks a row CONSOLIDATED into another; like ~~ and ✅ it is status
    # decoration, not identity. Leaving it in split `wf-policy-self-coverage`
    # into two rows — the plain id in the store and the decorated one in the
    # document — so a consolidation read as a brand-new task (measured 2026-07-27).
    return (cell.replace("**", "").replace("~~", "").replace("`", "")
            .replace("✅", "").replace("⤴", "").strip())


# deps-as-tags (backlog-groom P1): inline dependency tokens in today's backlog
# free text — `blocks:ID` / `depends-on:ID` / `relates-to:ID` / `duplicates:ID`
# — derive a per-row `deps` map with NO schema migration. IDs match the row-id
# charset (alnum . _ -). Only blocks/depends-on are ORDER edges (see topo_order);
# relates-to/duplicates are advisory. `\b` guards against substrings (roadblocks:).
_DEP_TOKEN = re.compile(
    r"\b(blocks|depends-on|relates-to|duplicates)\s*:\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_DEP_KEY = {"blocks": "blocks", "depends-on": "dependsOn",
            "relates-to": "relatesTo", "duplicates": "duplicates"}


def _parse_deps(text: str) -> dict[str, list[str]]:
    deps: dict[str, list[str]] = {"blocks": [], "dependsOn": [], "relatesTo": [], "duplicates": []}
    for m in _DEP_TOKEN.finditer(text or ""):
        key, val = _DEP_KEY[m.group(1).lower()], m.group(2)
        if val not in deps[key]:
            deps[key].append(val)
    return deps


# Three NON-task shapes live in the backlog document and must never become store
# rows: the transient "LOOP QUEUE" ordering view; the GUI phase-MATRIX (a pointer
# table into GUI_IMPLEMENTATION_PLAN.md whose rows parse as ids `0 Fundação`..
# `7 Activity`); and the auto-generated self-review inbox (pre-backlog TRIAGE — a
# finding becomes a real row only when a human promotes it). Deliberately NOT keyed
# on the `GUI / front-end track` section: the document has no `##` header between
# line 12 and line 118, so every row registered since 2026-07-20 parses into that
# section — excluding it would blind the guards to the newest work.
_PHASE_ID = re.compile(r"^\**\s*\d+\s")
_NON_TASK_SECTIONS = ("loop queue", "self-review inbox (auto-generated")


def is_task_row(row: dict[str, Any]) -> bool:
    """True when a parsed row (or a store row — same shape) is a real task.
    ONE definition, reused by the importer and the entry-groom guard, so the
    scope-out cannot drift between them."""
    section = (row.get("section") or "").lower()
    rid = row.get("id") or ""
    return (not any(s in section for s in _NON_TASK_SECTIONS)
            and not _PHASE_ID.match(rid)
            # a row whose id IS a header word is a mis-parsed header (`Fase` rode
            # in as a task for months because only the English `phase` was listed)
            and _clean(rid).lower() not in _HEADER_CELLS)


def _backlog_rows(root: Path) -> list[dict[str, Any]]:
    path = root / BACKLOG_REL
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    section = ""
    for line in lines:
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        if not line.lstrip().startswith("|"):
            continue
        try:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or all(_SEP_CELL.match(c) for c in cells if c):
                continue
            if _clean(cells[0]).lower() in _HEADER_CELLS:
                continue
            raw_id, raw_title = cells[0], cells[1]
            if not _clean(raw_id) and not _clean(raw_title):
                continue
            done = "~~" in raw_id or "✅" in raw_id or "✅" in raw_title
            low_sec = section.lower()
            if "self-review inbox" in low_sec:
                text = " ".join(cells).lower()
                category = ("chore" if any(t in text for t in ("budget", "burndown", "lines"))
                            else "bug")
            elif "deferred" in low_sec or "ideation" in low_sec:
                category = "ideation"
            else:
                category = "feature"
            size = next((m.group(1) for c in cells[2:]
                         for m in [_SIZE.match(_clean(c))] if m), None)
            pri = next((_clean(c) for c in cells[2:] if _PRI.match(_clean(c))), None)
            rows.append({
                "id": _clean(raw_id)[:60] or "(row)",
                "title": _clean(raw_title)[:160],
                # Full prose, UNCAPPED and un-cleaned (markdown kept): `title` is the
                # 160-char display line, `body` is EVERY cell after the id. Importing
                # only the capped title discarded 79% of the corpus (measured
                # 2026-07-27: 130030 -> 27166 chars over 198 rows) — a cap that
                # DISCARDS work instead of carrying it (wr-schema-discards-work's
                # class). Taking only cell 1 repeated the bug one column over: the
                # ideation/deferred tables are `| Item | Why | Signal |`, and their
                # third column (which carries `no-deps` markers) vanished. Any
                # reconciliation that only checks len(body) >= len(title) passes
                # while a whole column is missing — so carry them ALL.
                "body": " | ".join(c for c in cells[1:] if c).strip(),
                "category": category, "lane": "done" if done else "backlog",
                "size": size, "priority": pri,
                "section": section[:80], "source": BACKLOG_REL,
                "deps": _parse_deps(" ".join(cells)),  # backlog-groom P1: deps-as-tags
            })
        except Exception:
            continue  # never-crash collector idiom (ui_panel precedent)
    return rows


def task_rows(root: Path | str) -> list[dict[str, Any]]:
    """The AUTHORITATIVE task rows: the canonical store when it exists, the
    document parse when it does not (a fresh root, or a scenario fixture).

    One definition for every guard. SPEC-154 closure, SPEC-155 entry-groom and the
    delivery-bar advisor each used to parse the document directly while the board
    served the store — so the teeth and the operational surface reasoned about
    different row sets, and the 2026-07-17..27 divergence was invisible to both.
    Under `validate --staged` the store is re-materialized from the index
    (scenario_isolation._VALIDATED_DOCS), so a close staged in the same commit is
    visible here — that is what makes the closure lockstep satisfiable."""
    from harness_lib import tasks_store  # local import: store also imports us

    root = Path(root)
    stored = tasks_store.board_rows(root)
    rows = stored if stored is not None else _backlog_rows(root)
    return [r for r in rows if is_task_row(r)]


def _escalation_rows(root: Path) -> list[dict[str, Any]]:
    state = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
    resolved = set(state.get("resolvedIds", []))
    rows: list[dict[str, Any]] = []
    for esc_id, item in (state.get("raised", {}) or {}).items():
        if esc_id in resolved:
            continue
        rows.append({
            "id": str(esc_id)[:60],
            "title": str((item or {}).get("reason") or "escalation")[:160],
            "category": "bug", "lane": "backlog", "size": None,
            "priority": (item or {}).get("criticality"),
            "section": "Escalations (pending)", "source": "escalation",
        })
    return rows


def _worker_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        workers = chat_hud.read_workers(root)
    except Exception:
        return rows
    for w in workers:
        profile = str(w.get("profile") or "")
        rows.append({
            "id": f"{w.get('workflow')}:{w.get('workerId')}"[:60],
            "title": str(w.get("unit") or w.get("workerId") or "worker")[:160],
            "category": "feature",
            "lane": "review" if profile in _REVIEW_PROFILES else "running",
            "size": None, "priority": None,
            "section": f"profile {profile}" if profile else "workflow",
            "source": "workflow",
        })
    return rows


def _dispatch_lane(root: Path, row: dict[str, Any]) -> str:
    """Derived lane for a store row that was route-dispatched (task modal
    'Send to Agent'): live overseer pid -> running; ended with a HARNESS_RESULT
    in its log -> done; otherwise the stored lane stands (decision #3: the
    store never holds an in-execution lane — this is poll-time derivation).
    ponytail: log-tail marker, no result parsing — `tasks close` persists done
    when the owner confirms; a rotated log just falls back to the stored lane."""
    disp = row.get("dispatch") or {}
    from harness_lib.async_state import workflow_process_alive
    try:
        if workflow_process_alive(int(disp.get("pid") or 0)):
            return "running"
    except (TypeError, ValueError):
        pass
    log = disp.get("log")
    if log:
        try:
            with open(root / str(log), "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 8192))
                if b"HARNESS_RESULT" in fh.read():
                    return "done"
        except OSError:
            pass
    return str(row.get("lane") or "backlog")


def _counts(rows: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """Lane + category tallies for a row set (reused for harness and per-target)."""
    lane_counts: dict[str, int] = {lane: 0 for lane in LANES}
    category_counts: dict[str, int] = {}
    for r in rows:
        lane_counts[r["lane"]] = lane_counts.get(r["lane"], 0) + 1
        category_counts[r["category"]] = category_counts.get(r["category"], 0) + 1
    return lane_counts, category_counts


def _registered_targets(root: Path) -> list[str]:
    """SPEC-110 target names, discovered root-relative so the snapshot stays
    hermetic (mirrors targets.load_targets' `*/target.json` glob, but rooted at
    the passed root rather than the module's fixed HARNESS)."""
    names: list[str] = []
    for profile in sorted((root / TARGETS_REL).glob("*/target.json")):
        raw = read_json(profile, None)
        names.append(str((raw or {}).get("name") or profile.parent.name)
                     if isinstance(raw, dict) else profile.parent.name)
    return names


def _target_task_rows(root: Path, name: str) -> tuple[list[dict[str, Any]], str | None]:
    """A target's own tasks, from the roadmap-decided per-target store placement
    `.harness/state/targets/<name>/tasks.json` (screens-tasks-queue.md A3, mirrors
    quality-state). NO writer populates it yet — tasks-store-cli is harness-scoped —
    so today this is always absent → ([], None): a calm empty per-target board.
    This is the grouping SEAM, not a fabricated source; it lights up unchanged the
    day a per-target task writer ships.
    ponytail: reuse tasks_store's row shape inline (footprint is this file only)."""
    store = root / TARGET_STATE_REL / name / "tasks.json"
    doc = read_json(store, None)
    if not isinstance(doc, dict) or not doc.get("tasks"):
        return [], None
    src = f"{TARGET_STATE_REL}/{name}/tasks.json"
    rows = [{"id": t.get("id"), "title": t.get("title"),
             "category": t.get("category") or "feature",
             "lane": t.get("lane") or "backlog",
             "size": t.get("size"), "priority": t.get("priority"),
             "section": t.get("section") or "store", "source": src,
             "scope": "target", "target": name}
            for t in doc.get("tasks", [])]
    return rows, src


# session-start ordering: the store's priority values are historically messy
# (P1..P3, HIGH/MED/low, owner-gated, open, none). Interleaved rank so P-scale
# and word-scale sort together: P1=0 high=1 P2=2 med=3 P3=4 low=5 P4+=6+,
# any other non-empty token=7, no priority=9. Stable sort keeps store order
# within a rank.
_PRI_WORD = {"high": 1, "med": 3, "medium": 3, "low": 5}


def _pri_rank(priority: Any) -> int:
    p = str(priority or "").strip().lower()
    if not p:
        return 9
    m = re.match(r"^p(\d+)$", p)
    if m:
        return 2 * (int(m.group(1)) - 1)
    return _PRI_WORD.get(p, 7)


def topo_order(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Kahn (1962) topological sort of the board over ORDER edges only:
    `A dependsOn B` => B before A (edge B->A); `A blocks B` => A before B (edge
    A->B). relates-to/duplicates are advisory, never edges. A referenced id that
    is not a row is ignored (never-crash collector idiom). Ids that never reach
    indegree 0 are left OUT of `order` and reported in `cycles` — a mutual
    dependency in the backlog is a hard-detected BUG, not a rendering detail.
    ponytail: leftover ids report as one cycle group; split into SCCs only if
    separating independent cycles ever matters."""
    from collections import deque

    ids = list(dict.fromkeys(r.get("id") for r in rows))  # unique, first-seen order
    idset = set(ids)
    adj: dict[Any, list[Any]] = {i: [] for i in ids}
    indeg: dict[Any, int] = {i: 0 for i in ids}
    seen: set[tuple[Any, Any]] = set()

    def _edge(a: Any, b: Any) -> None:  # a before b
        if a in idset and b in idset and a != b and (a, b) not in seen:
            seen.add((a, b))
            adj[a].append(b)
            indeg[b] += 1

    for r in rows:
        rid = r.get("id")
        deps = r.get("deps") or {}
        for b in deps.get("dependsOn", []):
            _edge(b, rid)  # the dependency must come first
        for b in deps.get("blocks", []):
            _edge(rid, b)  # the blocker must come first
    queue = deque(i for i in ids if indeg[i] == 0)
    order: list[Any] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    cyclic = [i for i in ids if i not in set(order)]  # never reached indegree 0
    return {"order": order, "cycles": [cyclic] if cyclic else []}


def tasks_snapshot(root: Path | str) -> dict[str, Any]:
    """The derived board: harness rows + per-target groups, all read-only.

    A1 (owner decision #3): once the canonical store exists, its rows REPLACE
    the backlog-markdown parse (the tables are frozen history after `tasks
    import`); live running/review rows stay derived per poll either way.

    SPEC-110 grouping: harness rows carry scope="harness"; each registered
    target contributes a group (scope="target"). `perTargetSource` is False when
    no target store is populated — the grouping is a seam awaiting the source."""
    from harness_lib import tasks_store  # local import: store also imports us

    root = Path(root)
    stored = tasks_store.board_rows(root)
    base = stored if stored is not None else _backlog_rows(root)
    # Declare the file actually read. The payload used to report `backlogPath:
    # docs/IMPLEMENTATION_BACKLOG.md` while serving 265 of 265 rows from the store —
    # a surface naming a source it does not read, which is why nine days of drift
    # went unseen. The markdown path appears here only on the genuine storeless
    # fallback (a fresh root, or a SPEC-110 target with no store yet).
    source_path = tasks_store.STORE_REL if stored is not None else BACKLOG_REL
    for r in base:
        if r.get("dispatch"):  # task-modal dispatch: derive running/done per poll
            r["lane"] = _dispatch_lane(root, r)
    rows = base + _escalation_rows(root) + _worker_rows(root)
    for r in rows:
        r["scope"] = "harness"  # SPEC-110: harness-scoped, distinct from target groups
        if not r.get("deps"):  # P1: uniform empty deps (escalation/worker rows, dep-less store rows)
            r["deps"] = _parse_deps("")
    lane_counts, category_counts = _counts(rows)
    target_groups: list[dict[str, Any]] = []
    per_target_source = False
    for name in _registered_targets(root):
        trows, src = _target_task_rows(root, name)
        per_target_source = per_target_source or src is not None
        g_lanes, g_cats = _counts(trows)
        target_groups.append({
            "name": name, "scope": "target", "source": src, "count": len(trows),
            "laneCounts": g_lanes, "categoryCounts": g_cats, "rows": trows,
        })
    return {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sourcePath": source_path,
        "derived": True,  # A0: nothing stored; tasks-store-cli is the next increment
        "count": len(rows),
        "laneCounts": lane_counts,
        "categoryCounts": category_counts,
        "rows": rows,
        "targetGroups": target_groups,
        # False today: no per-target task writer exists (seam awaiting the source).
        "perTargetSource": per_target_source,
    }


def cmd_tasks(args) -> int:
    action = getattr(args, "action", "list") or "list"
    if action != "list":
        return _cmd_tasks_write(args, action)
    snap = tasks_snapshot(common.ROOT)
    rows = snap["rows"]
    if getattr(args, "category", None):
        rows = [r for r in rows if r["category"] == args.category]
    if getattr(args, "lane", None):
        rows = [r for r in rows if r["lane"] == args.lane]
    if getattr(args, "sort", None) == "priority":
        rows = sorted(rows, key=lambda r: _pri_rank(r.get("priority")))
    topo = topo_order(rows) if getattr(args, "topo", False) else None
    if common.agent_output_compact():
        if topo is not None:  # P1: compact topo view — pos + id, cyclic rows flagged
            flat = [{"pos": n, "id": i} for n, i in enumerate(topo["order"])]
            flat += [{"pos": "cycle", "id": i} for c in topo["cycles"] for i in c]
            common.emit(flat, tsv=True)
            return 0
        common.emit(rows, tsv=True)  # TE.5 rider (owner decision 2026-07-13 #3)
        return 0
    if getattr(args, "json", False):
        payload = {**snap, "rows": rows, "count": len(rows)}
        if topo is not None:
            payload["topo"] = topo
        common.emit(payload)
        return 0
    if topo is not None:  # P1: `tasks list --topo` — Kahn execution order + cycles
        print(f"Tasks topo order (Kahn) — {len(topo['order'])} ordered, "
              f"{len(topo['cycles'])} cycle(s)")
        for n, i in enumerate(topo["order"], 1):
            print(f"  {n:>3}. {i}")
        for c in topo["cycles"]:
            print(f"  cycle (mutual deps): {', '.join(c)}")
        return 0
    lanes = " | ".join(f"{lane} {snap['laneCounts'].get(lane, 0)}" for lane in LANES)
    cats = " | ".join(f"{c} {n}" for c, n in sorted(snap["categoryCounts"].items()))
    print(f"Tasks board (derived, read-only) — {snap['count']} rows")
    print(f"  lanes: {lanes}")
    print(f"  categories: {cats}")
    for r in rows:
        print(f"  {r['lane']:<8} {r['category']:<9} {(r.get('priority') or '-'):<5} "
              f"{r['id'][:36]:<36} {r['title'][:64]}")
    return 0


def _stdin_text(value: str | None) -> str | None:
    """`-` reads the value from STDIN. Multi-KB prose does not belong in argv (the
    `keys set NAME` precedent, for the same reason plus shell quoting), and a task
    body is routinely kilobytes. Module-level so it is testable without spawning a
    CLI: the CLI always resolves `common.ROOT` from its install, so a subprocess
    cannot be pointed at a temp root and would write the LIVE store instead."""
    if value != "-":
        return value
    # Read BYTES and decode UTF-8 explicitly. `sys.stdin.read()` decodes with the
    # console codepage on Windows (cp1252 here), which turned every accented word
    # of a piped body into mojibake — found by registering a real task through this
    # path. Prose is the whole point of `--body -`; a lossy read defeats it.
    raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
    return raw.decode("utf-8", errors="replace")


def _cmd_tasks_write(args, action: str) -> int:
    """The store-writing verbs (A1): import | add | move | close. The `tasks`
    CLI is the ONLY writer of .harness/state/tasks.json (owner decision #3)."""
    from harness_lib import tasks_store

    params = list(getattr(args, "params", []) or [])
    try:
        if action == "import":
            migrate = bool(getattr(args, "migrate", False))
            out = tasks_store.import_backlog(common.ROOT, migrate=migrate)
            print(f"imported: +{out['added']} added, {out['updated']} updated, "
                  f"{out['skipped']} skipped, {out['total']} total "
                  f"(backlog tables frozen at {out['frozenBacklogAt']})")
            if migrate:  # the one-time reconciliation, reported line by line
                print(f"migrate: {out['propagated']} lane(s) propagated from struck "
                      f"rows, {out['archived']} archived-done, {len(out['purged'])} "
                      f"parse artifact(s) purged, {len(out['orphans'])} orphan(s)")
        elif action == "add":
            if not params:
                raise HarnessError(
                    "usage: tasks add <title> [--id ID] [--category C] [--size S] "
                    "[--priority P] [--body - (prose on stdin)]")
            task = tasks_store.add(
                common.ROOT, " ".join(params),
                body=_stdin_text(getattr(args, "body", None)),
                task_id=getattr(args, "id", None),
                category=getattr(args, "category", None) or "feature",
                size=getattr(args, "size", None),
                priority=getattr(args, "priority", None))
            print(f"added {task['id']} (lane {task['lane']})")
        elif action == "dep":
            if not params:
                raise HarnessError(
                    "usage: tasks dep <id> <depends-on|blocks|relates-to|duplicates> <otherId>"
                    "  |  tasks dep <id> no-deps")
            task = tasks_store.set_dep(common.ROOT, params[0], params[1] if len(params) > 1 else "",
                                       params[2] if len(params) > 2 else None)
            print(f"{task['id']}: deps={task.get('deps')} noDeps={task.get('noDeps')}")
        elif action == "show":
            # The terminal read path for ONE row's full prose. `tasks list` prints
            # titles; once the document tables retire, without this the only way to
            # read a row's body outside the GUI is raw JSON — a real regression, and
            # cheaper to close here than by generating a markdown document nobody
            # writes to (which is the renderer the plan declined for good reasons).
            if len(params) != 1:
                raise HarnessError("usage: tasks show <id>")
            task = _task_or_raise(Path(common.ROOT), params[0])
            if common.agent_output_compact() or getattr(args, "json", False):
                common.emit(task)
                return 0
            deps = {k: v for k, v in (task.get("deps") or {}).items() if v}
            print(f"{task['id']}  [{task.get('lane')}]  {task.get('category')} "
                  f"{task.get('size') or '-'}/{task.get('priority') or '-'}")
            print(f"  {task.get('title')}")
            if deps or task.get("noDeps"):
                print(f"  deps: {deps or 'none (assessed)'}")
            for label in ("body", "notes", "plan"):
                if task.get(label):
                    print(f"\n--- {label} ---\n{task[label]}")
        elif action == "set":
            if len(params) != 1:
                raise HarnessError(
                    "usage: tasks set <id> [--priority P] [--size S] [--category C]")
            task = tasks_store.set_meta(
                common.ROOT, params[0],
                priority=getattr(args, "priority", None),
                size=getattr(args, "size", None),
                category=getattr(args, "category", None))
            print(f"set {task['id']}: priority={task.get('priority')} "
                  f"size={task.get('size')} category={task.get('category')}")
        elif action == "move":
            if len(params) != 2:
                raise HarnessError("usage: tasks move <id> <lane>  "
                                   f"(lanes: {'|'.join(tasks_store.MANUAL_LANES)})")
            task = tasks_store.move(common.ROOT, params[0], params[1])
            print(f"moved {task['id']} -> {task['lane']}")
        elif action == "close":
            if len(params) != 1:
                raise HarnessError("usage: tasks close <id>")
            task = tasks_store.close(common.ROOT, params[0])
            print(f"closed {task['id']}")
        elif action == "delete":
            if len(params) != 1:
                raise HarnessError("usage: tasks delete <id>")
            task = tasks_store.remove(common.ROOT, params[0])
            print(f"deleted {task['id']}")
        elif action == "note":
            if len(params) != 1 or getattr(args, "text", None) is None:
                raise HarnessError("usage: tasks note <id> --text <notes>  "
                                   "(--text - reads the notes from stdin)")
            task = tasks_store.set_note(common.ROOT, params[0], _stdin_text(args.text))
            print(f"noted {task['id']} ({len(task.get('notes') or '')} chars)")
        elif action == "plan":
            if len(params) != 1 or not getattr(args, "from_run", None):
                raise HarnessError("usage: tasks plan <id> --from-run <task-refine-*.plan.md>")
            task = _plan_from_run(common.ROOT, params[0], args.from_run)
            print(f"plan updated for {task['id']} ({len(task.get('plan') or '')} chars, "
                  f"source {task.get('planSource')})")
        elif action == "refine":
            if len(params) != 1:
                raise HarnessError("usage: tasks refine <id> [--model M] [--effort E] [--executor X]")
            return _refine(common.ROOT, params[0], getattr(args, "model", None),
                           getattr(args, "effort", None), getattr(args, "executor", None))
        elif action == "dispatch":
            if len(params) != 1:
                raise HarnessError("usage: tasks dispatch <id> [--executor X]")
            return _dispatch(common.ROOT, params[0], getattr(args, "executor", None))
        return 0
    except (ValueError, KeyError) as exc:
        # store refusals (unknown id, derived lane, dup id) exit 2 via main()
        raise HarnessError(str(exc)) from exc


_RUN_FILE_RE = re.compile(r"^task-refine-[A-Za-z0-9._-]+\.plan\.md$")
REFINE_TIMEOUT_S = 600


def _task_or_raise(root: Path, task_id: str) -> dict[str, Any]:
    from harness_lib import tasks_store
    doc = tasks_store.load(root)
    return tasks_store._find(doc, task_id)  # KeyError -> HarnessError upstream


def _plan_from_run(root: Path, task_id: str, run_name: str) -> dict[str, Any]:
    """Approval-as-record: the accepted refinement must be a REAL proposal file
    `tasks refine` wrote under .harness/runs/ — never arbitrary browser text."""
    from harness_lib import tasks_store
    if not _RUN_FILE_RE.match(run_name):
        raise ValueError(f"invalid proposal file name: {run_name!r}")
    path = root / ".harness" / "runs" / run_name
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        raise ValueError(f"proposal file not found: {run_name}")
    return tasks_store.append_plan(root, task_id, text, source=run_name)


def refine_model_run(root: Path, prefix: str, slug: str, packet_lines: list[str],
                     model: str | None, effort: str | None, executor: str | None) -> int:
    """Shared bounded READ-ONLY refine engine (tasks + experiments): write the
    prompt packet under .harness/runs/, spawn the plan-profile agent, write the
    proposal to <prefix>-<slug>-<ts>.plan.md and print PROPOSAL_FILE + text.
    Executor defaults to claude (the active `generic` echo stub is never a
    planner); codex runs under its native --sandbox read-only."""
    import harness  # noqa: E402 -- reuse the real spawn/template seams
    from harness_lib import processes, stream_json
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)[:40]
    runs = root / ".harness" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    packet = runs / f"{prefix}-{slug}-{ts}.prompt.md"
    packet.write_text("\n".join(packet_lines), encoding="utf-8", newline="\n")
    override = {k: v for k, v in (("model", model), ("effort", effort)) if v}
    argv = harness.workflow_spawn_command_for_prompt(
        str(packet), "plan", executor or "claude", override or None,
        write_allowed=False,
        pointer_prompt=(f"Read `{packet}` and produce ONLY the refined implementation "
                        "plan it asks for (markdown). Read-only: modify nothing."))
    proc = processes.run_quiet(argv, timeout=REFINE_TIMEOUT_S, cwd=str(root),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    parsed = stream_json.parse(proc.stdout or "")
    # ponytail: claude -> stream-json finalText; codex/other -> raw stdout (may
    # carry banner noise); upgrade to parse_codex_stream if it ever matters.
    text = (parsed.get("finalText") or "").strip() or (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        raise HarnessError(f"refine run failed (rc {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout or '')[-400:]}")
    proposal = runs / f"{prefix}-{slug}-{ts}.plan.md"
    proposal.write_text(text, encoding="utf-8", newline="\n")
    print(f"PROPOSAL_FILE: {proposal.name}")
    print()
    print(text)
    return 0


def route_dispatch(root: Path, demand: str, executor: str | None) -> tuple[dict[str, Any] | None, str]:
    """Shared porteiro shipper (tasks + experiments): run `route --task <demand>
    --dispatch` with all its guardrails, print the output tail, and return
    ({pid, log, route}, out) on a real detached launch, (None, out) when the
    route was WITHHELD; raise on rc!=0/REFUSED."""
    import sys as _sys
    from harness_lib import processes
    proc = processes.run_quiet(
        [_sys.executable, "scripts/harness.py", "route", "--task", demand,
         "--executor", executor or "claude", "--dispatch"],
        timeout=300, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(out[-4000:])
    pid_m = re.search(r"dispatched \(detached\): pid (\d+)", out)
    log_m = re.search(r"^(\.harness[\\/]runs[\\/]route-dispatch-\S+\.log)\s*$", out, re.M)
    if pid_m:
        return ({"pid": int(pid_m.group(1)),
                 "log": log_m.group(1).replace("\\", "/") if log_m else None,
                 "route": "route --task --dispatch"}, out)
    if "WITHHELD" in out:
        return (None, out)
    if proc.returncode != 0 or "REFUSED" in out:
        raise HarnessError(f"dispatch failed (rc {proc.returncode}); see output above")
    return (None, out)


def _refine(root: Path, task_id: str, model: str | None, effort: str | None,
            executor: str | None) -> int:
    """One bounded READ-ONLY model run that proposes a refined implementation
    plan for the task (backlog info + owner notes + current plan). Writes the
    proposal to .harness/runs/task-refine-*.plan.md and prints it; the store is
    NOT touched — acceptance is a separate owner-approved `tasks plan` write."""
    task = _task_or_raise(root, task_id)
    return refine_model_run(root, "task-refine", task_id, [
        "# Task plan refinement (read-only)",
        "You are a read-only planning agent. Explore the repository as needed;",
        "do NOT modify any file, do NOT commit.",
        "",
        "## Task",
        f"- id: {task['id']}",
        f"- title: {task.get('title')}",
        f"- category: {task.get('category')} | size: {task.get('size')} | "
        f"priority: {task.get('priority')} | section: {task.get('section')}",
        "",
        "## Owner notes",
        str(task.get("notes") or "(none)"),
        "",
        "## Current plan",
        str(task.get("plan") or "(none yet)"),
        "",
        "## Deliverable",
        "Output ONLY the refined implementation plan in markdown (no preamble):",
        "concrete steps, file footprint, and a verification section. It will be",
        "appended to the task's plan upon owner approval.",
    ], model, effort, executor)


def _dispatch(root: Path, task_id: str, executor: str | None) -> int:
    """Task-modal 'Send to Agent': ship card info + plan through the SAME
    porteiro path a chat demand takes (`route --task ... --dispatch`, all its
    guardrails included — WITHHELD/REFUSED are relayed, not routed around).
    A real detached launch is recorded on the task (lane -> queued; the board
    derives running/done from the pid/log per poll)."""
    from harness_lib import tasks_store
    from harness_lib.async_state import workflow_process_alive
    task = _task_or_raise(root, task_id)
    # Double-dispatch guard (incident a6c9af5: only the EXP start path got the
    # pid-lock; this sibling stayed open): the recorded pid IS the lock — a live
    # previous dispatch refuses re-launch instead of clobbering its pid/log.
    prev = task.get("dispatch") or {}
    try:
        prev_alive = workflow_process_alive(int(prev.get("pid") or 0))
    except (TypeError, ValueError):
        prev_alive = False
    if prev_alive:
        raise HarnessError(
            f"cannot dispatch {task_id}: previous dispatch pid {prev['pid']} is still "
            f"running (log: {prev.get('log') or 'n/a'})")
    # ponytail: demand travels as one argv element — cap the plan slice so the
    # Windows 32K command line never overflows; the full plan stays on the task.
    parts = [str(task.get("title") or task_id)]
    if task.get("notes"):
        parts.append("Owner notes:\n" + str(task["notes"])[:6000])
    if task.get("plan"):
        parts.append("Approved plan:\n" + str(task["plan"])[:12000])
    info, _out = route_dispatch(root, "\n\n".join(parts), executor)
    if info:
        tasks_store.set_dispatch(root, task_id, info)
        print(f"\n-- task {task_id} dispatched: lane queued "
              "(board derives running while the overseer lives, done on HARNESS_RESULT)")
    elif "WITHHELD" in _out:
        print(f"\n-- task {task_id} NOT dispatched: route WITHHELD -> "
              "owner decision inbox (harness.py decide); task lane unchanged")
    return 0


def _demo() -> None:
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "IMPLEMENTATION_BACKLOG.md").write_text(
            "# Backlog\n\n## Features\n"
            "| ID | Item | Size |\n|---|---|---|\n"
            "| F.1 | build the thing | M |\n"
            "| ~~F.2~~ ✅ | shipped thing | S |\n"
            "garbage | not a table row\n"
            "## Self-review inbox\n"
            "| finding | observed |\n|---|---|\n"
            "| lines-budget | 1470 lines over budget |\n",
            encoding="utf-8")
        (root / ".harness" / "state").mkdir(parents=True)
        (root / ".harness" / "state" / "escalations.json").write_text(json.dumps({
            "raised": {"e1": {"reason": "needs human"}, "e2": {"reason": "old"}},
            "resolvedIds": ["e2"]}), encoding="utf-8")
        # SPEC-110: register two targets — one with a per-target store, one without.
        (root / ".harness" / "targets" / "T1").mkdir(parents=True)
        (root / ".harness" / "targets" / "T1" / "target.json").write_text(
            json.dumps({"name": "T1", "path": "."}), encoding="utf-8")
        (root / ".harness" / "targets" / "T2").mkdir(parents=True)
        (root / ".harness" / "targets" / "T2" / "target.json").write_text(
            json.dumps({"name": "T2", "path": "."}), encoding="utf-8")
        (root / ".harness" / "state" / "targets" / "T1").mkdir(parents=True)
        (root / ".harness" / "state" / "targets" / "T1" / "tasks.json").write_text(
            json.dumps({"tasks": [{"id": "t1-a", "title": "target task", "lane": "ready"}]}),
            encoding="utf-8")

        snap = tasks_snapshot(root)
        by_id = {r["id"]: r for r in snap["rows"]}
        assert by_id["F.1"]["lane"] == "backlog" and by_id["F.1"]["category"] == "feature"
        assert by_id["F.1"]["size"] == "M", by_id["F.1"]
        assert by_id["F.2"]["lane"] == "done", by_id["F.2"]
        assert by_id["lines-budget"]["category"] == "chore", by_id["lines-budget"]
        assert by_id["e1"]["category"] == "bug" and "e2" not in by_id
        assert snap["laneCounts"]["done"] == 1 and snap["laneCounts"]["backlog"] == 3
        assert all(r["scope"] == "harness" for r in snap["rows"]), "harness scope"
        groups = {g["name"]: g for g in snap["targetGroups"]}
        assert set(groups) == {"T1", "T2"}, groups
        assert groups["T1"]["count"] == 1 and groups["T1"]["rows"][0]["scope"] == "target"
        assert groups["T1"]["rows"][0]["target"] == "T1"
        assert groups["T2"]["count"] == 0 and groups["T2"]["source"] is None  # calm empty
        assert snap["perTargetSource"] is True, snap["perTargetSource"]

    # N3-F1 (a6c9af5 sibling): a task whose previous dispatch pid is ALIVE
    # refuses re-dispatch before any launch — the pid is the lock.
    import os
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".harness" / "state").mkdir(parents=True)
        (root / ".harness" / "state" / "tasks.json").write_text(json.dumps({
            "tasks": [{"id": "t-dup", "title": "dup", "lane": "queued",
                       "dispatch": {"pid": os.getpid(), "log": "x.log"}}]}),
            encoding="utf-8")
        try:
            _dispatch(root, "t-dup", None)
            raise AssertionError("live-pid dispatch must refuse")
        except HarnessError as e:
            assert "still running" in str(e), e

    # backlog-groom P1: deps-as-tags derive per-row deps; topo_order orders a
    # linear chain, hard-detects a mutual-dependency cycle, and a ghost reference
    # never crashes (never-crash collector idiom).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "IMPLEMENTATION_BACKLOG.md").write_text(
            "# Backlog\n\n## Features\n"
            "| ID | Item | Size |\n|---|---|---|\n"
            "| C1 | root step | S |\n"
            "| C2 | mid `depends-on:C1` `blocks:C3` | S |\n"
            "| C3 | leaf depends-on:C2 relates-to:C1 duplicates:C1 | S |\n"
            "| CY1 | mutual depends-on:CY2 | S |\n"
            "| CY2 | mutual depends-on:CY1 | S |\n"
            "| BAD | ghost ref depends-on:NOPE | S |\n",
            encoding="utf-8")
        snap = tasks_snapshot(root)
        by_id = {r["id"]: r for r in snap["rows"]}
        assert by_id["C2"]["deps"]["dependsOn"] == ["C1"], by_id["C2"]["deps"]
        assert by_id["C2"]["deps"]["blocks"] == ["C3"], by_id["C2"]["deps"]
        assert by_id["C3"]["deps"]["relatesTo"] == ["C1"], by_id["C3"]["deps"]
        assert by_id["C3"]["deps"]["duplicates"] == ["C1"], by_id["C3"]["deps"]
        topo = topo_order(snap["rows"])
        pos = {i: n for n, i in enumerate(topo["order"])}
        assert pos["C1"] < pos["C2"] < pos["C3"], topo["order"]  # chain respects deps
        assert "BAD" in pos, topo["order"]                       # ghost edge ignored, node kept
        cyc = {i for c in topo["cycles"] for i in c}
        assert cyc == {"CY1", "CY2"}, topo["cycles"]             # mutual dep hard-detected
        assert "C1" not in cyc, topo["cycles"]                   # acyclic nodes not flagged
    # --sort priority: messy real-world values rank P1 > high > P2 > med > P3 >
    # low > other-token > none; sort is stable within a rank.
    ranked = sorted(["", "owner-gated", "low", "P3", "MED", "P2", "high", "P1", "P4"],
                    key=_pri_rank)
    assert ranked == ["P1", "high", "P2", "MED", "P3", "low", "P4", "owner-gated", ""], ranked

    print("tasks_board self-check: ok")


if __name__ == "__main__":
    _demo()
