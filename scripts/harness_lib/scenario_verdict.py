"""Two-channel scenario verdict: the rc alone is not evidence.

Measured 2026-07-28 (codex sol xhigh lane, PID split re-measured 3/3 by the
overseer): on Windows the venv `python.exe` that `Popen` returns is a LAUNCHER
process, while the interpreter that writes the scenario scorecard is a
DIFFERENT PID. `processes.run_process_tree_bounded` combines the LAUNCHER's
`returncode` with the DESCENDANT's captured pipe, and both gate paths then
classified pass/fail from that returncode alone. Controlled inversion, 3/3: the
interpreter printed FAILED checks and exited 1 while the launcher terminated
with code 0 — the runner recorded PASS, ran no retry and wrote no forensics
(those fire only on a non-zero rc). So `rc == 0` was never evidence that the
process which printed the scorecard exited 0.

This module is the single place both the parallel and the serial path ask what
a (rc, stdout) pair MEANS, so the two cannot drift apart again.

CEILING, deliberate: a scorecard that is ABSENT still falls back to trusting the
rc. 198 of 204 scenarios emit the canonical line, but three (`ce1_containment`
and two with private summaries) legitimately never do, so treating absence as
failure would redden the gate for scenarios that are perfectly healthy. The
proven hole — a FAILED scorecard read as a pass — is what closes here. Upgrade
path if the remaining gap ever matters: a structured per-scenario receipt
(exit-status written by the interpreter itself), which removes the text coupling
noted below.

TEXT COUPLING, accepted: this reads the `_lib.summary()` line. If that format
changes, verdicts degrade to today's rc-only behaviour (a missing parse is not a
failure), never to a false green.
"""
from __future__ import annotations

import re

# `_lib.summary()` prints: "SCENARIO: <n>/<m> checks passed" then either
# " — all green" or "; failed: a, b".
SCORECARD_RE = re.compile(r"^SCENARIO: (\d+)/(\d+) checks passed(.*)$", re.M)
SKIP_MARKER = "SCENARIO-SKIP:"


def scorecard(stdout: str) -> tuple[bool, str] | None:
    """(all_green, line) from the LAST canonical scorecard in `stdout`, or None.

    The LAST one is the terminal verdict: a scenario that shells out to sibling
    scenarios (hk_hook_selfchecks) emits their scorecards first and its own
    last."""
    matches = list(SCORECARD_RE.finditer(stdout or ""))
    if not matches:
        return None
    m = matches[-1]
    passed, total, tail = int(m.group(1)), int(m.group(2)), m.group(3)
    return (passed == total and "failed:" not in tail), m.group(0).strip()


def classify(rc: int | None, stdout: str) -> tuple[str, str]:
    """Return (verdict, contradiction) — verdict is `pass` | `fail` | `skip`.

    `contradiction` is non-empty ONLY when the two channels disagree, so callers
    can capture forensics and report the disagreement instead of silently
    trusting one channel. Fail-closed: a disagreement is never a pass.
    """
    text = stdout or ""
    if rc is None:  # timeout: no completed process to trust
        return "fail", ""
    if rc == 0 and SKIP_MARKER in text:  # self-skip protocol, unchanged
        return "skip", ""
    card = scorecard(text)
    if card is not None:
        green, line = card
        if rc == 0 and not green:  # THE false green this module exists for
            return "fail", f"rc 0 but the scorecard reports failures: {line}"
        if rc != 0 and green:  # the false red actually observed (wv, 2026-07-28)
            return "fail", f"rc {rc} but the scorecard is green: {line}"
    return ("pass" if rc == 0 else "fail"), ""


def run_with_retry(run_attempt) -> tuple:
    """Run one scenario, classify it, retry ONCE on failure. Returns
    `(proc, verdict, contradiction, recovered)`.

    `run_attempt(attempt: str)` performs one isolated run. Both gate paths call
    this so the retry-once net and the two-channel verdict cannot drift apart —
    and so `spec_test_gate.py` keeps shrinking (the modular-boundary invariant
    `gs-7` enforces; this extraction paid for the verdict wiring that broke it).

    A recovered pass is still just a pass HERE: whether a retry should be
    allowed to certify over a first-attempt failure is a policy question tracked
    separately — this function only stops the rc from lying about which one it
    was."""
    proc = run_attempt("")
    verdict, contradiction = classify(
        getattr(proc, "returncode", None), getattr(proc, "stdout", "") or "")
    recovered = False
    if verdict == "fail":
        proc = run_attempt("-retry")
        verdict, retry_contradiction = classify(
            getattr(proc, "returncode", None), getattr(proc, "stdout", "") or "")
        contradiction = contradiction or retry_contradiction
        recovered = verdict == "pass"
    return proc, verdict, contradiction, recovered


def _selfcheck() -> int:  # pragma: no cover — exercised as a script
    cases = [
        # (rc, stdout, expected verdict, expects contradiction)
        (0, "SCENARIO: 3/3 checks passed — all green", "pass", False),
        (1, "SCENARIO: 2/3 checks passed; failed: b", "fail", False),
        (0, "SCENARIO: 2/3 checks passed; failed: b", "fail", True),   # false GREEN
        (1, "SCENARIO: 3/3 checks passed — all green", "fail", True),  # false RED
        (0, "SCENARIO-SKIP: re-entrant guard", "skip", False),
        (0, "no scorecard here at all", "pass", False),  # ce1-style: rc is all we have
        (1, "no scorecard here at all", "fail", False),
        (None, "SCENARIO: 3/3 checks passed — all green", "fail", False),  # timeout
        # a nested scenario's scorecard must not outrank the terminal one
        (0, "SCENARIO: 9/9 checks passed — all green\nSCENARIO: 1/2 checks passed; failed: x",
         "fail", True),
    ]
    bad = []
    for rc, out, want, want_contra in cases:
        verdict, contra = classify(rc, out)
        ok = verdict == want and bool(contra) == want_contra
        print(f"{'PASS' if ok else 'FAIL'}  rc={rc} -> {verdict}"
              + (f" ({contra})" if contra else ""))
        if not ok:
            bad.append((rc, out[:40], verdict, contra))
    print(f"\nscenario_verdict self-check: {len(cases) - len(bad)}/{len(cases)}"
          + (" — all green" if not bad else f"; FAILED: {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
