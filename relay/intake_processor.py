#!/usr/bin/env python3
"""relay/intake_processor.py — asynchronous human intake queue
(RELAY-INTAKE-DOGFOODING-CONSENSUS-01, TRAIN-10).

Zero-touch operator UX: drop a `.md` or `.json` demand into
`relay/intake/human/` (Drive-synced) and it is parsed, registered into the
canonical work graph, and routed to the Planner — no chat typing.

Fail-closed and bounded, mirroring the daemon's message plane:
- parsing NEVER throws on malformed input (defaults fill missing fields);
- ingestion is IDEMPOTENT by a content-derived id (a re-dropped or Drive-
  resurrected file yields zero duplicate graph nodes);
- graph admission goes through `graph_ops` (provenance via `intake`, canonical
  admission via `add-node --save`) — this module never hand-edits the graph;
- a file that cannot be parsed/registered after MAX_ATTEMPTS is quarantined
  with a diagnostic receipt, never silently dropped.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import relay_io  # noqa: E402
import relay_mesh  # noqa: E402  — paths + planner notification (follows rebind_root)

MAX_ATTEMPTS = relay_io.MAX_ATTEMPTS
DEFAULT_CLUSTER = "08 Relay / Work convergence"
DEFAULT_PRIORITY = "P2"
DEFAULT_HORIZON = "H1"


# graph_ops lives in the REAL repo tree; tests rebind relay_mesh.RELAY_ROOT to a
# scratch root, but the tool itself is always here next to us.
_REAL_RELAY_ROOT = _HERE.parent


def _graph_ops() -> Path:
    candidates = [
        _REAL_RELAY_ROOT / "scripts" / "harness_lib" / "graph_ops.py",
        _REAL_RELAY_ROOT / "satellites" / "tare.tools.backlog-graph" / "graph_ops.py",
        _REAL_RELAY_ROOT.parent / "tare.tools.backlog-graph" / "graph_ops.py",
        _REAL_RELAY_ROOT / "side_projects" / "tare.tools.graph-backlog" / "graph_ops.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def intake_root() -> Path:
    return relay_mesh.RELAY_DIR / "intake"


def _dirs() -> Dict[str, Path]:
    root = intake_root()
    return {"human": root / "human", "dogfooding": root / "dogfooding",
            "processed": root / "processed", "quarantine": root / "quarantine",
            "proposed": root / "proposed"}


def ensure_dirs() -> None:
    for d in _dirs().values():
        d.mkdir(parents=True, exist_ok=True)
    # dogfooding sub-structure is owned by consensus_engine; create the root only.


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").upper()
    return s[:limit] or "ITEM"


def _clean(text: str) -> str:
    """Strip control chars (incl. NUL) that a human paste/transcript may carry —
    they break the graph_ops subprocess argv and JSON. Keep normal whitespace."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text or "").strip()


def parse_demand(path: Path) -> Dict[str, Any]:
    """Best-effort structured extraction. NEVER raises: unstructured or corrupt
    text still yields a registerable node (title = filename, summary = raw)."""
    raw = ""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        raw = ""
    fields: Dict[str, Any] = {}
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                fields = data
        except (json.JSONDecodeError, ValueError):
            fields = {}  # fall through to text heuristics on malformed JSON

    def _kv(key: str) -> Optional[str]:
        m = re.search(rf"^\s*{key}\s*[:=]\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else None

    title = (str(fields.get("title")) if fields.get("title") else None) \
        or _kv("title")
    if not title:
        m = re.search(r"^\s*#\s+(.+)$", raw, re.MULTILINE)  # first markdown heading
        title = m.group(1).strip() if m else None
    if not title:
        first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        title = first[:120] or Path(path).stem

    summary = (str(fields.get("summary")) if fields.get("summary") else None) \
        or _kv("summary") or raw.strip()[:1000] or title

    priority = (str(fields.get("priority")) if fields.get("priority") else None) \
        or _kv("priority") or DEFAULT_PRIORITY
    pm = re.search(r"P[0-3]", priority.upper())
    priority = pm.group(0) if pm else DEFAULT_PRIORITY

    cluster = (str(fields.get("cluster")) if fields.get("cluster") else None) \
        or _kv("cluster") or DEFAULT_CLUSTER
    dod = fields.get("definition_of_done") or fields.get("dod") \
        or re.findall(r"^\s*-\s*\[.\]\s*(.+)$", raw, re.MULTILINE)
    if isinstance(dod, str):
        dod = [dod]

    explicit_id = fields.get("id") or _kv("id")
    if explicit_id:
        task_id = _slug(str(explicit_id))
    else:  # content-derived => same demand re-dropped is idempotent
        digest = hashlib.sha256((title + "\x1f" + summary).encode("utf-8")).hexdigest()[:10]
        task_id = f"INTAKE-{_slug(title, 24)}-{digest}"
    return {"id": task_id, "title": _clean(title)[:200] or "Untitled intake",
            "summary": _clean(summary) or _clean(title) or "No summary provided.",
            "priority": priority, "cluster": _clean(cluster) or DEFAULT_CLUSTER,
            "horizon": DEFAULT_HORIZON,
            "definition_of_done": [_clean(str(x)) for x in (dod or []) if _clean(str(x))]}


def _node_ids() -> set:
    graph = relay_io.read_json(relay_mesh.WORK_GRAPH_FILE)
    if not isinstance(graph, dict):
        return set()
    return {n.get("id") for n in graph.get("nodes", []) if isinstance(n, dict)}


def _run_graph_ops(args: list[str]) -> subprocess.CompletedProcess:
    engine = _graph_ops()
    if engine.is_file():
        cmd = [sys.executable, str(engine), "--graph", str(relay_mesh.WORK_GRAPH_FILE), *args]
    else:
        cmd = [sys.executable, "-m", "graph_backlog.cli", "--graph", str(relay_mesh.WORK_GRAPH_FILE), *args]
    return subprocess.run(cmd,
                          capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL,
                          cwd=str(_REAL_RELAY_ROOT), **relay_io.safe_subprocess_kwargs())


def ingest_file(path: Path, *, failures: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Parse + register one demand. Idempotent by task id; moves the source to
    processed/ with a receipt on success, quarantines after MAX_ATTEMPTS."""
    ensure_dirs()
    failures = failures if failures is not None else {}
    d = _dirs()
    path = Path(path)
    try:
        demand = parse_demand(path)
        task_id = demand["id"]

        if task_id in _node_ids():  # idempotent: already admitted, no duplicate
            return _finish(path, demand, d, already=True)

        # 1) provenance artifact (graph_ops intake; PROPOSED, non-canonical)
        proposed = d["proposed"] / f"{task_id}.json"
        _run_graph_ops(["intake", "--id", task_id, "--title", demand["title"],
                        "--cluster", demand["cluster"], "--priority", demand["priority"],
                        "--horizon", demand["horizon"], "--summary", demand["summary"],
                        *sum((["--dod", x] for x in demand["definition_of_done"]), []),
                        "--out", str(proposed), "--overwrite"])

        # 2) canonical admission into the work graph (add-node --save)
        admit = _run_graph_ops(["add-node", "--id", task_id, "--title", demand["title"],
                                "--cluster", demand["cluster"], "--priority", demand["priority"],
                                "--horizon", demand["horizon"], "--summary", demand["summary"],
                                "--tag", "human-intake", "--save"])
        if admit.returncode != 0 or task_id not in _node_ids():
            raise RuntimeError(f"graph admission failed: {admit.stderr.strip()[:300]}")

        relay_mesh.send_inbox_msg("planner", f"New human intake admitted: {task_id}", {
            "task_id": task_id, "title": demand["title"], "priority": demand["priority"],
            "source": path.name, "action": "PLAN_NEW_INTAKE"}, required_action="notify")
        return _finish(path, demand, d, already=False)

    except Exception as exc:  # bounded retry -> quarantine with diagnostic receipt
        name = path.name
        failures[name] = failures.get(name, 0) + 1
        if failures[name] >= MAX_ATTEMPTS:
            d["quarantine"].mkdir(parents=True, exist_ok=True)
            relay_io.write_text_atomic(d["quarantine"] / f"{name}.error.txt",
                                       f"{type(exc).__name__}: {exc}\nattempts={failures[name]}")
            if path.exists():
                path.rename(d["quarantine"] / name)
            failures.pop(name, None)
            return {"ok": False, "quarantined": True, "reason": str(exc)[:300], "source": name}
        return {"ok": False, "quarantined": False, "reason": str(exc)[:300], "source": name}


def _finish(path: Path, demand: Dict[str, Any], d: Dict[str, Path],
            *, already: bool) -> Dict[str, Any]:
    d["processed"].mkdir(parents=True, exist_ok=True)
    receipt = {"taskId": demand["id"], "title": demand["title"], "source": path.name,
               "ingestedAt": relay_mesh.now_iso(), "alreadyPresent": already,
               "priority": demand["priority"], "cluster": demand["cluster"]}
    relay_io.write_json_atomic(d["processed"] / f"{path.stem}.receipt.json", receipt)
    if path.exists():
        target = d["processed"] / path.name
        if target.exists():
            target.unlink()
        path.rename(target)
    return {"ok": True, "quarantined": False, "taskId": demand["id"], "already": already,
            "source": path.name}


def sweep_human(*, failures: Optional[Dict[str, int]] = None) -> int:
    """Process every pending demand in intake/human/. Returns count ingested."""
    ensure_dirs()
    failures = failures if failures is not None else {}
    human = _dirs()["human"]
    count = 0
    for f in sorted(human.glob("*.md")) + sorted(human.glob("*.json")):
        if f.stem.lower() == "readme" or f.name[:1] in (".", "_"):
            continue  # docs/placeholders are not demands
        out = ingest_file(f, failures=failures)
        if out.get("ok"):
            count += 1
    return count


if __name__ == "__main__":  # manual: python relay/intake_processor.py [file]
    relay_io.utf8_console()
    if len(sys.argv) > 1:
        print(json.dumps(ingest_file(Path(sys.argv[1])), indent=2, ensure_ascii=False))
    else:
        print(f"swept {sweep_human()} intake file(s)")
