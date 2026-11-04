"""Size-gated write-path rotation for the unbounded runtime JSONL logs (R1 / gap G1).

The global runtime logs — ``.harness/runs/harness-trace.jsonl`` and
``validation-results.jsonl`` — are append-only and grew without bound; rotation was a
deferred self-review NAG, not code. This adds a deterministic, no-daemon spill: when a
log crosses a byte cap, the whole file is renamed (archive-before-recycle) into a
gitignored ``runs/archive/`` dir and the writer starts a fresh file on its next append.
Eviction != deletion — the segment is moved, never dropped.

Per the round's critique consensus:
- the sealed segment is passed through the existing secret-scan and the verdict recorded
  in a sidecar ``*.manifest.json`` (hit-bearing => ``quarantine`` flag, never a silent
  archive — the manifest is the non-silent record; a records.add_entry pointer is the
  upgrade path if a hit is ever seen);
- the rename fails OPEN on any OSError (Windows/OneDrive can hold a lock): a rotation
  that cannot happen never blocks or loses a log write — the file just keeps growing
  until the next append succeeds in rotating it;
- scope is the two persistent ``runs/*.jsonl`` logs ONLY. ``events.jsonl`` is
  SPEC-109-transient/compacted (owned elsewhere) and ``worklog-archive.json`` is a
  rewritten JSON array, not appendable JSONL — both are excluded.

Self-check: ``python scripts/harness_lib/log_rotation.py``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # run as a script → put scripts/ on the path for harness_lib
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness_lib import secret_scan

# ponytail: 5 MiB bounds each live log while keeping rotation a rare event (few
# segments, negligible recurring scan cost). Tune per-caller if a log proves chatty.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def spill_if_over(path: Path, max_bytes: int | None = None) -> dict | None:
    """Rotate ``path`` into ``<parent>/archive/`` if it exceeds ``max_bytes``.

    ``max_bytes`` defaults to ``DEFAULT_MAX_BYTES`` resolved at call time (so the cap
    stays tunable). Returns the manifest dict on rotation, ``None`` when no rotation
    happened (small file, missing file, or a fail-open locked rename). Safe to call
    before every append.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return None
    except OSError:
        return None
    try:
        archive = path.parent / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = archive / f"{path.stem}.{ts}{path.suffix}"
        raw = path.read_bytes()
        path.replace(target)  # atomic archive-before-recycle; writer re-creates path
    except OSError:
        return None  # fail-open: a locked/racy rename never blocks the log write
    hits = secret_scan.scan(raw.decode("utf-8", "replace"))
    manifest = {
        "segment": target.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rotatedAt": ts,
        "secretScanHits": len(hits),
        "quarantine": bool(hits),
        "scanVerdict": "hits" if hits else "clean",
    }
    (archive / f"{target.name}.manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":  # ponytail: self-check — spill boundary, manifest, quarantine, fail-open
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        runs.mkdir()
        log = runs / "harness-trace.jsonl"

        # under cap → no rotation, file untouched
        log.write_text('{"a":1}\n', encoding="utf-8")
        assert spill_if_over(log, max_bytes=1024) is None, "small file must not rotate"
        assert log.exists() and not (runs / "archive").exists(), "no archive for a small file"

        # over cap → rotate: segment moves to archive, live path is recycled, manifest clean
        log.write_text("x" * 2048 + "\n", encoding="utf-8")
        m = spill_if_over(log, max_bytes=1024)
        assert m and m["scanVerdict"] == "clean" and not m["quarantine"], m
        seg = runs / "archive" / m["segment"]
        assert seg.exists() and not log.exists(), "segment archived, live path recycled (writer re-creates on next append)"
        assert (runs / "archive" / (m["segment"] + ".manifest.json")).exists(), "manifest written"

        # over cap AND secret-bearing → quarantine flag set (never a silent archive)
        log.write_text('{"k":"sk-FAKEKEYdonotleak0123456789abcdef"}\n' + "y" * 2048, encoding="utf-8")
        m2 = spill_if_over(log, max_bytes=1024)
        assert m2 and m2["quarantine"] is True and m2["secretScanHits"] == 1, m2
        print("log_rotation self-check OK")
