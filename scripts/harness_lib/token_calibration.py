"""Local, gitignored charsPerToken calibration override (isolation for the learned value).

The committed ``.harness/project.json`` charsPerToken is the canonical SEED (3.1,
hand-tuned). The self-review estimator learns a per-machine / per-workload value from
real cost samples — that learned value is DERIVED, local state and must not live in a
tracked file, the same canonical-vs-derived split the records ledger already uses
(canonical tracked JSON + gitignored derived index).

This module owns the gitignored override so a gate or test run can never mutate the
committed calibration: the writer (self-review recalibration) writes HERE, and the
readers (workflow budget sizing + the estimator-drift comparison) prefer this value over
the project.json seed when it exists. Stdlib only.

Self-check: ``python scripts/harness_lib/token_calibration.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CALIBRATION_REL = ".harness/state/token-calibration.json"


def _path(root: Path, subject: str | None = None) -> Path:
    """Per-subject calibration file (per-target-token-calibration): a target's
    learned value lives under ITS state dir, so one verbose target's reply
    shape can never move another subject's budget inputs. subject None/"self"
    = the harness's own file (every existing caller unchanged)."""
    if subject and subject != "self":
        return root / ".harness" / "state" / "targets" / str(subject) / "token-calibration.json"
    return root / CALIBRATION_REL


def read_override(root: Path, subject: str | None = None) -> float | None:
    """The locally-learned charsPerToken for one subject, or ``None`` when no
    override exists / is valid (callers fall back to the committed seed)."""
    try:
        data = json.loads(_path(root, subject).read_text(encoding="utf-8"))
        value = float(data.get("charsPerToken"))
        return value if value > 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_override(root: Path, chars_per_token: float, *, observed: float | None = None,
                   samples: int | None = None, at: str | None = None,
                   subject: str | None = None) -> Path:
    """Persist the learned charsPerToken to the subject's gitignored override
    file. Returns its path."""
    path = _path(root, subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "charsPerToken": round(float(chars_per_token), 2),
        "observedCharsPerToken": observed,
        "samples": samples,
        "updatedAt": at,
        "subject": subject or "self",
        "note": "LOCAL learned override (gitignored). The committed project.json charsPerToken is the seed.",
    }, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":  # ponytail: self-check — round-trip + absent/corrupt → None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert read_override(root) is None, "no file → None"
        write_override(root, 2.63, observed=2.6, samples=25, at="2026-07-12T00:00:00Z")
        assert read_override(root) == 2.63, read_override(root)
        # corrupt / non-positive → None (never crashes a budget read)
        (root / CALIBRATION_REL).write_text("{broken", encoding="utf-8")
        assert read_override(root) is None
        write_override(root, 0)
        assert read_override(root) is None, "non-positive override is ignored"
        # per-target-token-calibration: subjects resolve DISJOINT files; a
        # target write never moves the harness value (or another target's).
        write_override(root, 2.5, subject="tA")
        write_override(root, 4.0, subject="tB")
        assert read_override(root, "tA") == 2.5 and read_override(root, "tB") == 4.0
        assert read_override(root) is None, "harness file untouched by target writes"
        assert read_override(root, "self") is None and _path(root, "self") == _path(root)
        print("token_calibration self-check OK")
