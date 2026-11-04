"""Common dependency-free helpers shared by harness runtime modules."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / ".harness"

# mkdtemp/mkstemp retry TMP_MAX times on a name collision, and on Windows CPython
# sets TMP_MAX = 2147483647 -- effectively unbounded. That matters because mkdtemp
# treats PermissionError as a collision whenever `os.access(dir, W_OK)` is True,
# and on Windows os.access reads only the read-only ATTRIBUTE, never the ACL. A
# directory carrying a deny-write ACE WITHOUT the read-only bit therefore turns
# every mkdtemp under it into ~2.1 billion failing syscalls at 100% of a core
# instead of a 0.3ms raise (measured 2026-07-24; a leaked probe burned 81,395s of
# CPU exactly this way). The harness has 30+ mkdtemp sites, two of them in the
# gate hot path, so the bound goes HERE once instead of at every call site.
# 1000 is absurdly generous for a real collision (8 random chars of 62) and caps
# the pathological case near 0.3s: fast and loud beats infinite and silent.
tempfile.TMP_MAX = 1000


class HarnessError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def import_native_retry(*names: str, delays: tuple[float, ...] = (1.0, 3.0),
                        _import=importlib.import_module) -> tuple:
    """Import native-extension modules, retrying a Windows Smart App Control (SAC)
    cold-cache block. SAC runs an ASYNC cloud-reputation check on the FIRST touch of
    an unknown .pyd/.dll: a freshly-installed wheel with a new ABI blocks ONCE, then
    the reputation warms within seconds and the same import succeeds. We retry only
    on that signature and give up after a bounded ceiling, so a GENUINELY unsigned or
    broken binary still degrades via the caller's `except` path instead of hanging.

    Match is CPython's unlocalized literal "DLL load failed" (dynload_win.c) — NOT
    the localized "Application Control policy" tail (breaks on a pt-BR machine) and
    NOT WinError 4551 (absent from ImportError). That prefix is emitted only on
    Windows, so this is zero added cost on POSIX (a Linux ImportError never contains
    it, so it re-raises at once) and needs no os.name guard. Over-match (a corrupt or
    missing-dependency DLL also says "DLL load failed") costs <= sum(delays)s once,
    then degrades — bounded and harmless. Retry does NOT help a permanently-unsigned
    binary (ruff.exe, NotSigned): that is a subprocess launch, not an import, and is
    handled by graceful degrade + the SAC PostToolUse hook, not here.

    Returns the tuple of imported modules (all also cached in sys.modules). `_import`
    is the test seam. ponytail: seam for the gate check; not a public knob."""
    attempt = 0
    while True:
        try:
            return tuple(_import(n) for n in names)
        except ImportError as exc:
            if "DLL load failed" not in str(exc) or attempt >= len(delays):
                raise  # non-SAC (or ceiling reached) -> let the caller degrade
            time.sleep(delays[attempt])
            attempt += 1


# Replay classification (article Section 8.2): every trajectory event row carries
# a replay class so downstream tooling can reason about determinism. ADDITIVE
# only -- no L3 consumer branches on it (observe-first). Encoded as a name/token
# table verified against the real event-name inventory. External is checked
# first so a boundary-crossing effect always dominates; unknown names never
# crash and never classify as `exact` -- they fall through to `approximate`.
_REPLAY_EXTERNAL_TOKENS = (
    "_started", "_finished", "_dispatched", "_spawn", "_launch",
    "_timeout", "_killed", "http", "vendor", "_push", "_export",
)
_REPLAY_EXACT_TOKENS = (
    "_prepared", "_updated", "_cleaned", "_locked", "_merged",
    "_recovered", "checkpoint",
)


def replay_class(event_name: str) -> str:
    """Classify a trajectory event by replay semantics (article Section 8.2).

    external    -> crossed a process/network boundary or ran a model/vendor
                   effect; a replay re-executes the effect.
    exact       -> deterministic state/bookkeeping transition, replayable from
                   inputs.
    approximate -> default, including every unknown name (never crashes,
                   never `exact`).
    """
    name = str(event_name or "")
    for tok in _REPLAY_EXTERNAL_TOKENS:
        if tok in name:
            return "external"
    for tok in _REPLAY_EXACT_TOKENS:
        if tok in name:
            return "exact"
    return "approximate"


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def rmtree_robust(path: Path) -> None:
    """rmtree that survives transient Windows handles (OneDrive/AV) and read-only files.

    Promoted from harness.py workflow_scrub when its Deferred trigger fired
    (a swallowed transient failure in the gate cleanup left an empty dir that
    tripped release-hygiene — SPEC-109 execution note).
    """
    import shutil
    import time

    def _onexc(fn, p, _exc):  # noqa: ANN001
        os.chmod(p, 0o700)
        fn(p)

    # `onexc` is 3.12+; 3.11 (in the declared support matrix, caught by the
    # FIRST CI run 2026-07-29) only has `onerror`, whose handler receives
    # exc_info instead of the exception — same body either way.
    kw = ({"onexc": _onexc} if sys.version_info >= (3, 12)
          else {"onerror": lambda fn, p, ei: _onexc(fn, p, ei[1])})
    for attempt in range(3):
        try:
            if path.exists():
                shutil.rmtree(path, **kw)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


def write_text(path: Path, text: str) -> None:
    # SPEC-108 H2: newline="\n" keeps writes byte-identical across platforms
    # (.gitattributes pins eol=lf; default text mode writes CRLF on Windows).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _retry_windows_lock(fn):
    """Bounded retry for the nt sharing-violation class (errno 13 / WinError 5):
    os.replace with a concurrently-open reader — or an open() racing a replace —
    denies one side for a moment. Reproduced live 2026-07-16: rs:e2e-smoke
    stranding one worker (reader side) and a `workflow run` crash mid-wave
    (writer side), both on workflow.json. 5 tries x 30ms; the last error raises."""
    last: Exception | None = None
    for _ in range(5):
        try:
            return fn()
        except PermissionError as exc:
            last = exc
            time.sleep(0.03)
    raise last  # type: ignore[misc]


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default

    def _read() -> Any:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    return _retry_windows_lock(_read)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: concurrent readers (workflow await vs the async
    # supervisor writing AT-*.json) must see the old or the new document,
    # never a torn write — a mid-write read crashed workflow_await with
    # JSONDecodeError, found live 2026-07-13. os.replace is atomic on the
    # same volume on nt and posix alike.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    _retry_windows_lock(lambda: os.replace(tmp, path))


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except Exception:
        return None


def gate_holding(root: Path) -> bool:
    """True while the SPEC-137 gate holds the tree — a gate-hold dir with at least one
    NON-recovered entry.

    A `*-recovered` remnant is the audit trail of an ALREADY-recovered hold:
    `scenario_isolation` renamed it and restored the dirty baseline to the live tree,
    so it no longer holds anything and MUST NOT count. Otherwise a writer that gates on
    this (the delegation/defect/route ledgers) would refuse FOREVER after any historical
    crashed-and-recovered gate — a permanent, silent false-refusal (audit, sonnet
    2026-07-31). A live hold and an abandoned-but-unrecovered hold both DO hold the tree
    and both count.

    The hold swaps the live tree, so anything written to `.harness/` during it is
    DISCARDED. Never raises: a missing dir is simply "not holding".
    """
    try:
        return any(not p.name.endswith("-recovered")
                   for p in (root / ".harness" / "runs" / "gate-hold").iterdir())
    except OSError:
        return False


def truncate_text(text: str | None, max_chars: int) -> tuple[str, bool]:
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = f"\n[truncated by harness at {max_chars} chars]\n"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def bounded_timeout(env_var: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env_var, str(default))))
    except Exception:
        return default


# Verbs that STAMP a verdict. A gate stamped from an interpreter the operator
# did not declare is a false gate -- the same class gate_parallel._pin_sha
# already fails on ("a fingerprint whose code never executed").
VERDICT_VERBS = frozenset({"gate-staged", "validate"})


def _same_interpreter(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def declared_python_mismatch(executable: str, declared: str | None,
                             verb: str) -> tuple[str, str] | None:
    """Pure core: does this process contradict the operator's DECLARED interpreter?

    `.harness/project.json` names HARNESS_PYTHON as `runtime.python.envOverride`
    and lists `private-executor-virtualenvs` under `forbiddenFallbacks`; both
    `tools/agent-sync/py-run.sh` and AGENT_SYNC.md forbid probing a project
    `.venv`. So this NEVER looks for a venv and never hardcodes a path. Unset
    HARNESS_PYTHON means the operator declared nothing and every PATH
    interpreter >= 3.10 is sanctioned -> silence (CI provisions this way).

    The gap it closes (found 2026-07-24): py-run.sh honors HARNESS_PYTHON, but a
    direct `python scripts/harness.py ...` did not, so a staged gate ran on
    system 3.12 while HARNESS_PYTHON named the 3.13 interpreter. A non-path
    HARNESS_PYTHON (`py -3`, a bare launcher name -- py-run.sh allows these) is
    not comparable, so it also stays silent rather than crying wolf.

    Returns (severity, message) or None; severity is "fail" for VERDICT_VERBS,
    "warn" otherwise.
    """
    if not declared or not os.path.isfile(declared):
        return None
    if _same_interpreter(executable, declared):
        return None
    severity = "fail" if verb in VERDICT_VERBS else "warn"
    tail = ("re-run it with that interpreter (or via tools/agent-sync/py-run.sh, which "
            "honors HARNESS_PYTHON); set HARNESS_PYTHON_OK=1 to override deliberately.")
    return severity, (f"HARNESS_PYTHON declares {declared}, but this process is {executable}. "
                      f"{'A verdict from an undeclared interpreter is a false verdict: ' if severity == 'fail' else ''}"
                      f"{tail}")


REEXEC_SENTINEL = "HARNESS_PYTHON_REEXEC"


def enforce_declared_python(argv: list[str], *, runner: Any = None,
                            script: str | None = None) -> None:
    """Thin seam over `declared_python_mismatch`: HAND OFF to the declared
    interpreter rather than making the caller re-issue the command.

    Failing loud only converts a wrong interpreter into a wasted round-trip --
    the caller reads the error, then types the same command again. So on a
    mismatch this re-runs the harness under the DECLARED interpreter and exits
    with the child's code. It stays a hand-off, never an inference: an unset or
    non-path HARNESS_PYTHON still does nothing at all.

    `REEXEC_SENTINEL` makes the hand-off strictly once. If the child STILL sees a
    mismatch, the paths cannot be reconciled (a comparison edge, not a transient),
    so it degrades to the old behavior -- fail on a verdict verb, warn otherwise
    -- instead of spawning forever. `runner`/`script` are injection seams for the
    acceptance scenario; production passes neither.
    """
    if os.environ.get("HARNESS_PYTHON_OK") == "1":
        return
    declared = os.environ.get("HARNESS_PYTHON")
    verb = next((a for a in argv if not a.startswith("-")), "")
    hit = declared_python_mismatch(sys.executable, declared, verb)
    if hit is None:
        return
    severity, message = hit
    if os.environ.get(REEXEC_SENTINEL) == "1":
        if severity == "fail":
            raise SystemExit(f"harness: handed off once and still mismatched. {message}")
        print(f"harness: handed off once and still mismatched. {message}", file=sys.stderr)
        return
    if runner is None:  # local import: keeps common.py's charter dependency-free
        from harness_lib import processes
        runner = processes.run_passthrough
    print(f"harness: running under the declared HARNESS_PYTHON ({declared}) instead of "
          f"{sys.executable}; set HARNESS_PYTHON_OK=1 to keep this one.", file=sys.stderr)
    env = dict(os.environ, **{REEXEC_SENTINEL: "1"})
    raise SystemExit(runner([str(declared), script or sys.argv[0], *argv], env=env))


def load_ambient_keys() -> list[str]:
    """Fill registry secret keys absent from os.environ, from the OS vault.

    keys-keyring v2 (owner decision 2026-07-24) removed the `.env` tier this
    used to be named for (`load_env_file`): the cascade is now process env →
    vault, full stop. Returns the NAMES injected — never a value. Suppress with
    HARNESS_NO_VAULT=1 (fixtures exercising the key-absent path).
    """
    try:
        from harness_lib import keys_vault
        return keys_vault.inject_vault_keys()
    except Exception:
        return []


# -- TE.5: agent-facing compact/tabular output -------------------------------
AGENT_OUTPUT_ENV = "HARNESS_AGENT_OUTPUT"


def agent_output_compact() -> bool:
    """True when an agent is the consumer: chat-engine agent paths export
    HARNESS_AGENT_OUTPUT=compact. Absent (humans, existing JSON parsers) keeps
    today's indent=2 output byte-identical."""
    return os.environ.get(AGENT_OUTPUT_ENV) == "compact"


def _tsv_cell(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"  # json tokens so from_tsv round-trips
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    else:
        text = str(value)
    # one record per line: flatten embedded tab/newline (records bodies etc.)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def to_tsv(rows: list[dict[str, Any]]) -> str:
    """Flat list of dicts -> header (union of keys, first-seen order) + one
    tab-separated row each; nested/None cells fall back to compact-JSON text."""
    header: list[str] = []
    for row in rows:
        header.extend(k for k in row if k not in header)
    lines = ["\t".join(header)]
    lines.extend("\t".join(_tsv_cell(row.get(k)) for k in header) for row in rows)
    return "\n".join(lines)


def from_tsv(text: str) -> list[dict[str, Any]]:
    """Ingest parser — the inverse of to_tsv, for agent-written tabular data
    entering the application (TSV/JSON boundary, owner 2026-07-22). Header row
    names the keys; empty cell -> None; cells parse as JSON when they can
    (numbers, true/false/null, nested [..]/{..} from _tsv_cell) else stay str.
    Known dialect ceilings, by design: embedded tab/newline were flattened to
    spaces on render and do not round-trip; a *string* cell that looks like a
    JSON scalar ("42") comes back typed.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    rows: list[dict[str, Any]] = []
    for ln in lines[1:]:
        row: dict[str, Any] = {}
        for key, cell in zip(header, ln.split("\t")):
            if cell == "":
                row[key] = None
            else:
                try:
                    row[key] = json.loads(cell)
                except json.JSONDecodeError:
                    row[key] = cell
        rows.append(row)
    return rows


def tsv_table(value: Any) -> Any:
    """P1 ingest adapter (TSV/JSON boundary): a list passes through untouched; a str
    whose first non-empty line contains a tab parses as a from_tsv table. Any other
    value returns unchanged so downstream shape errors still surface honestly."""
    if isinstance(value, str):
        first = next((ln for ln in value.splitlines() if ln.strip()), "")
        if "\t" in first:
            return from_tsv(value)
    return value


def emit(payload: Any, *, tsv: bool = False) -> None:
    """Print a command payload. Default (env absent) = indent=2, byte-identical
    to legacy. Agent-compact: TSV an allowlisted flat list (tsv=True), else
    compact-separator JSON."""
    if not agent_output_compact():
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # On a real TTY, colorize the JSON (on demand); redirected/piped output is
        # returned unchanged by highlight(), so this stays byte-identical off-terminal.
        from harness_lib import highlight
        print(highlight.highlight(text, lang="json"))
        return
    if tsv and isinstance(payload, list) and payload and all(isinstance(r, dict) for r in payload):
        print(to_tsv(payload))
        return
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
