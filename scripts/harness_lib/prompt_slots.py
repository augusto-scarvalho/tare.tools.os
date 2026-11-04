"""SEC.8 — non-local system-prompt slots: detect + surface + record, never edit.

The vendor injects instructions into the session system prompt from
`~/.claude.json` `.clientDataCacheSlots.<opaque-key>.data.<name>` — found
live 2026-07-26: an instruction slot that suppressed plan-role delegation
for a whole session while owner and ledger could not say why. The slot sits
outside BOTH the canonical layer (`.harness/`) and the adapter layer
(`.claude/`), so the ".harness is canonical" doctrine cannot see it.

CONTROL HERE MEANS DETECT + SURFACE + RECORD, NOT EDIT: the slot is
server-delivered cache under an opaque key and may be rewritten or vanish on
any client update — writing to it is not a durable lever. The durable lever
is owner awareness (the slots are conditional — "unless the user requested
it" — so a standing owner grant neutralizes one without fighting the cache).

Privacy floor, non-negotiable: `~/.claude.json` carries account state. This
module reads it, hashes scalar `data` values (sha256), and NEVER lets slot
TEXT out — not in the baseline, not in diffs, not in the ledger, not in any
output. Field paths, hashes and lengths only.

Surfaces: `repo_health.checks` carries the WARN (`prompt-slots`: baseline
missing -> adopt; drift -> review), and changes land one ledger note (tags
prompt-slots+sec8). Baseline: `.harness/runs/prompt-slots-baseline.json`
(class-D, machine-local). Self-check: run this file. `--snapshot` adopts the
current set as baseline; `--diff` prints the drift report.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))

CLAUDE_JSON_ENV = "HARNESS_CLAUDE_JSON"  # test/e2e override; default ~/.claude.json
BASELINE_REL = ".harness/runs/prompt-slots-baseline.json"


def claude_json_path() -> Path:
    override = os.environ.get(CLAUDE_JSON_ENV)
    return Path(override) if override else Path.home() / ".claude.json"


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def read_slots(path: Path | None = None) -> list[dict[str, Any]] | None:
    """Scalar data entries of every slot — HASHES AND SHAPES ONLY, never text.
    Tri-state: a MISSING file is [] (no slots is a valid state); an unreadable/
    unparseable file is None (the client rewrites the account file in place —
    a torn read must be a non-signal, never a mass removal). Neither raises."""
    path = path or claude_json_path()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 — torn/concurrent rewrite, perms, bad JSON
        return None
    slots = data.get("clientDataCacheSlots")
    if not isinstance(slots, dict):
        return []
    out: list[dict[str, Any]] = []
    for slot_key, slot in sorted(slots.items()):
        if not isinstance(slot, dict):
            continue
        entries = slot.get("data")
        if not isinstance(entries, dict):
            continue
        for name, value in sorted(entries.items()):
            if isinstance(value, (dict, list)):
                continue  # nested structures are not prompt text; scalars only
            out.append({
                "slot": str(slot_key),
                "name": str(name),
                "kind": type(value).__name__,
                "sha": _sha(value),
                "chars": len(value) if isinstance(value, str) else None,
                "entrypoint": str(slot.get("entrypoint") or ""),
                "model": str(slot.get("model") or ""),
            })
    return out


def _pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(r["name"], r["sha"]) for r in rows}


def snapshot(root: Path | str, path: Path | None = None) -> dict[str, Any]:
    """Adopt the CURRENT slot set as the reviewed baseline (owner act).
    Refuses on an unreadable account file — a torn read never clobbers the
    reviewed set."""
    from harness_lib.common import now, write_json
    rows = read_slots(path)
    if rows is None:
        return {"baseline": str(BASELINE_REL), "entries": 0,
                "refused": "unreadable account file"}
    payload = {"at": now(), "slots": rows}
    target = Path(root) / BASELINE_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, payload)
    return {"baseline": str(BASELINE_REL), "entries": len(rows)}


def diff(root: Path | str, path: Path | None = None) -> dict[str, Any]:
    """Drift of the live slots vs the reviewed baseline — ids/lengths only.
    Identity is the VALUE pair (name, sha), never the slot key: the client
    mints a fresh opaque `bi1-*` key per session/model and evicts old ones,
    so key churn around an identical value is the cache working, not an
    injected change. `new`/`removed` are name-level; `changed` is a live
    `name@sha` whose name the baseline knows under a different sha.
    `unreadable` marks a torn account-file read (all lists empty: non-signal)."""
    from harness_lib.common import read_json
    rows = read_slots(path)
    unreadable = rows is None
    rows = rows or []
    baseline_doc = read_json(Path(root) / BASELINE_REL, None)
    if not isinstance(baseline_doc, dict):
        return {"baselinePresent": False, "unreadable": unreadable,
                "active": len(rows), "new": [], "changed": [], "removed": []}
    if unreadable:
        return {"baselinePresent": True, "unreadable": True, "active": 0,
                "new": [], "changed": [], "removed": []}
    base_rows = [r for r in baseline_doc.get("slots") or [] if isinstance(r, dict)]
    live_names = {r["name"] for r in rows}
    base_names = {r["name"] for r in base_rows}
    changed = sorted(f"{name}@{sha}" for name, sha in _pairs(rows) - _pairs(base_rows)
                     if name in base_names)
    return {"baselinePresent": True, "unreadable": False, "active": len(rows),
            "new": sorted(live_names - base_names), "changed": changed,
            "removed": sorted(base_names - live_names)}


def alerts(delta: dict[str, Any]) -> list[str]:
    """THE drift predicate — the one interpretation both surfaces (doctor row
    and SessionStart hook) consume. Offending ids only; [] means silence."""
    if delta.get("unreadable"):
        return []
    return (list(delta.get("new") or []) + list(delta.get("changed") or [])
            + list(delta.get("removed") or []))


def record_drift(root: Path | str, delta: dict[str, Any]) -> None:
    """One ledger note per detected drift — ids only; loss never breaks anything."""
    if not (delta.get("new") or delta.get("changed") or delta.get("removed")):
        return
    try:
        from harness_lib import records
        records.add_entry(Path(root), "note",
                          "prompt-slots drift: "
                          f"new={len(delta['new'])} changed={len(delta['changed'])} "
                          f"removed={len(delta['removed'])}",
                          body=json.dumps({k: delta[k] for k in ("new", "changed", "removed")},
                                          sort_keys=True),
                          tags=["prompt-slots", "sec8"])
    except Exception:  # noqa: BLE001 — observe-only
        pass


def doctor_check(root: Path | str) -> dict[str, str]:
    """The repo_health WARN row. Baseline missing = adopt (deliberate nag: an
    unreviewed instruction channel is the disease); drift = review + re-snapshot."""
    try:
        delta = diff(root)
        if delta.get("unreadable"):
            return {"id": "prompt-slots", "status": "ok",
                    "detail": "account file unreadable this pass (non-signal)"}
        if not delta["baselinePresent"]:
            if delta["active"] == 0:  # no channel exists — nothing to review
                return {"id": "prompt-slots", "status": "ok",
                        "detail": "no vendor prompt slots (nothing to review)"}
            return {"id": "prompt-slots", "status": "warn",
                    "detail": (f"{delta['active']} vendor prompt-slot entr(ies) with NO "
                               "reviewed baseline - run `python scripts/harness_lib/"
                               "prompt_slots.py --snapshot` after reviewing (SEC.8)")}
        moved = alerts(delta)
        if moved:
            record_drift(root, delta)
            return {"id": "prompt-slots", "status": "warn",
                    "detail": ("vendor prompt-slot drift vs reviewed baseline: "
                               + ", ".join(moved[:5])
                               + " - review, then re-snapshot (SEC.8)")}
        return {"id": "prompt-slots", "status": "ok",
                "detail": f"{delta['active']} entr(ies) match the reviewed baseline"}
    except Exception as exc:  # noqa: BLE001 — a doctor check never raises
        return {"id": "prompt-slots", "status": "ok", "detail": f"unreadable: {exc!r}"}


def _demo() -> None:
    import tempfile

    secret_text = "Do not call the AgentTool unless asked"  # stands in for slot prose
    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        fake = troot / "claude.json"
        doc = {"clientDataCacheSlots": {
            "bi1-aaa": {"at": 1, "entrypoint": "cli", "model": "m", "org": "o",
                        "data": {"tengu_x": secret_text, "flag": True,
                                 "nested": {"ignored": 1}}}}}
        fake.write_text(json.dumps(doc), encoding="utf-8")
        os.environ[CLAUDE_JSON_ENV] = str(fake)
        try:
            rows = read_slots()
            assert [r["name"] for r in rows] == ["flag", "tengu_x"], rows
            assert rows[1]["chars"] == len(secret_text) and rows[1]["kind"] == "str"
            assert secret_text not in json.dumps(rows), "slot text must NEVER leak"

            (troot / ".harness" / "runs").mkdir(parents=True)
            assert diff(troot)["baselinePresent"] is False
            assert doctor_check(troot)["status"] == "warn"  # adopt nag

            snapshot(troot)
            clean = diff(troot)
            assert clean["baselinePresent"] and not (clean["new"] or clean["changed"])
            assert doctor_check(troot)["status"] == "ok"

            # key churn: SAME (name, sha) pairs migrate to a fresh opaque key —
            # the cache working, not drift. Total silence, no re-snapshot needed.
            doc["clientDataCacheSlots"] = {"bi1-zzz": doc["clientDataCacheSlots"]["bi1-aaa"]}
            fake.write_text(json.dumps(doc), encoding="utf-8")
            churn = diff(troot)
            assert not (churn["new"] or churn["changed"] or churn["removed"]), churn
            assert alerts(churn) == [] and doctor_check(troot)["status"] == "ok"

            doc["clientDataCacheSlots"]["bi1-zzz"]["data"]["tengu_x"] = "CHANGED TEXT"
            doc["clientDataCacheSlots"]["bi1-bbb"] = {"data": {"fresh": "new slot"}}
            fake.write_text(json.dumps(doc), encoding="utf-8")
            drift = diff(troot)
            assert drift["changed"] == [f"tengu_x@{_sha('CHANGED TEXT')}"], drift
            assert drift["new"] == ["fresh"], drift
            dc = doctor_check(troot)
            assert dc["status"] == "warn" and "tengu_x@" in dc["detail"]
            assert "CHANGED TEXT" not in json.dumps([drift, dc]), "text must not leak"

            doc["clientDataCacheSlots"]["bi1-zzz"]["data"].pop("flag")
            fake.write_text(json.dumps(doc), encoding="utf-8")
            assert diff(troot)["removed"] == ["flag"]  # name gone from EVERY key

            # torn account file: unreadable is a non-signal, never a mass removal,
            # and never clobbers the reviewed baseline
            baseline_bytes = (troot / BASELINE_REL).read_bytes()
            fake.write_text("{torn", encoding="utf-8")
            assert read_slots() is None
            torn = diff(troot)
            assert torn["unreadable"] and alerts(torn) == [], torn
            assert doctor_check(troot)["status"] == "ok"
            assert snapshot(troot).get("refused"), "snapshot must refuse a torn read"
            assert (troot / BASELINE_REL).read_bytes() == baseline_bytes

            assert read_slots(Path(tmp) / "absent.json") == []  # missing = empty
        finally:
            os.environ.pop(CLAUDE_JSON_ENV, None)
    print("prompt_slots self-check: ok")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--snapshot" in argv:
        print(json.dumps(snapshot(_SCRIPTS.parent), indent=2))
    elif "--diff" in argv:
        print(json.dumps(diff(_SCRIPTS.parent), indent=2, sort_keys=True))
    else:
        _demo()
