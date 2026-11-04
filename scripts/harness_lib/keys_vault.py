#!/usr/bin/env python3
"""config-keys phase 2 — OS-native key vault via `keyring` (owner decision #2).

Sole secret backend = the OS vault (Windows Credential Manager / macOS
Keychain / Linux Secret Service) through the `keyring` lib (pinned 25.6.0,
justified by owner decision 2026-07-13 #2). Read cascade: **process env →
vault**, nothing else. `inject_vault_keys()` runs at harness startup and fills
only registry env keys absent from the environment.

The `.env` tier was REMOVED (owner decision 2026-07-24, keys-keyring v2): a
plaintext secret file is a second provisioning surface with none of the vault's
protections, and the vault made it redundant. `keyring` moved from
requirements-optional to requirements — the project's first hard runtime
dependency (owner 2026-07-24). Its absence does not crash the harness: key
resolution warns ONCE and returns empty, so key-free commands still run and a
key-needing command fails at its own point with its own message. CI and
containers provision through real process env vars, checked first regardless.

Writes go through `keys set NAME` which reads the value from STDIN (masked
via getpass on a TTY), NEVER argv — no secret lands in shell history or
process lists. Every vault write/delete leaves a records-ledger entry with the
key NAME + timestamp only, never the value. Non-secret metadata (`lastRotated`,
backend) lives in `.harness/state/keys-meta.json`; the `doctor` verb WARNs on
keys not rotated in STALE_DAYS.

No surface (result, log, argv, records) ever carries a key VALUE — only NAMES
and value-free source labels (`environ|keyring|missing`).

Self-check: `python scripts/harness_lib/keys_vault.py` (fake backend only —
it never touches the real vault).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import config_keys, records  # noqa: E402
from harness_lib.common import HarnessError, ROOT, read_json, write_json  # noqa: E402

SERVICE = "universal-agent-harness"  # one vault namespace; keys are user-global
META_REL = ".harness/state/keys-meta.json"
STALE_DAYS = 90  # ponytail: fixed threshold; promote to a knob only on demand
# Decision #3: value-free cascade labels, in resolution order. resolved_source()
# is the pinnable single read-order function; nothing logs a VALUE, only these.
RESOLVE_SOURCES = ("environ", "keyring", "missing")
_KEYRING_WARNED = False  # warns at most ONCE per process when the vault is unreachable


def _keyring():
    """The keyring module, or None when unavailable (headless/CI degrade)."""
    try:
        import keyring  # noqa: PLC0415 — optional dep, import only on use
        return keyring
    except Exception:
        return None


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def env_key_names() -> list[str]:
    """The env-kind names from the SPEC-130 registry — the only keys the
    cascade tries; the vault is not a dumping ground for arbitrary names."""
    return [name for name, kind, *_ in config_keys.CONFIG_KEYS if kind == "env"]


def _meta_path(root: Path | str) -> Path:
    return Path(root) / META_REL


def _load_meta(root: Path | str) -> dict[str, Any]:
    return read_json(_meta_path(root), None) or {}


def _save_meta(root: Path | str, meta: dict[str, Any]) -> None:
    write_json(_meta_path(root), meta)


def vault_get(name: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, name)
    except Exception:
        return None


def set_key(root: Path | str, name: str, value: str) -> None:
    """Vault write + lastRotated metadata + a value-free records entry."""
    if not value:
        raise HarnessError(f"empty value for {name} — nothing written")
    kr = _keyring()
    if kr is None:
        raise HarnessError("keyring backend unavailable — install `keyring` "
                           "(pinned 25.6.0) or provision via env/.env")
    kr.set_password(SERVICE, name, value)
    meta = _load_meta(root)
    meta[name] = {"backend": "keyring", "lastRotated": _now()}
    _save_meta(root, meta)
    records.add_entry(Path(root), "change", f"key set: {name}",
                      body="vault write via `keys set` (value never recorded)",
                      tags=["keys", "vault"])


def unset_key(root: Path | str, name: str) -> None:
    kr = _keyring()
    if kr is None:
        raise HarnessError("keyring backend unavailable")
    try:
        kr.delete_password(SERVICE, name)
    except Exception as exc:
        raise HarnessError(f"{name} is not in the vault") from exc
    meta = _load_meta(root)
    meta.pop(name, None)
    _save_meta(root, meta)
    records.add_entry(Path(root), "change", f"key removed: {name}",
                      body="vault delete via `keys unset`", tags=["keys", "vault"])


def resolved_source(name: str, root: Path | str = ROOT) -> str:
    """Where a key resolves from, in cascade order — VALUE-FREE (decision #3): only
    the source label, never the value. Mirrors the runtime cascade
    os.environ → keyring → absent. `root` is unused since the .env tier was
    removed; kept so the many value-free callers need no signature churn."""
    import os

    if os.environ.get(name):
        return "environ"
    if vault_get(name) is not None:
        return "keyring"
    return "missing"


def _warn_no_vault_once() -> bool:
    """Emit ONE stderr line per process when the vault is unreachable AND an expected
    key is absent from the environment — i.e. the key is genuinely unobtainable.

    keys-keyring v2 re-pointed this: it used to fire only when .env could cover the
    gap, so with .env removed the same trigger would have gone permanently silent and
    a keyring-less host would resolve NO keys with NO warning. Now the trigger IS the
    gap. Value-free; returns True iff it warned.
    """
    global _KEYRING_WARNED
    if _KEYRING_WARNED:
        return False
    import os

    if all(os.environ.get(n) for n in env_key_names()):
        return False  # every expected key already in the environment — no gap, stay quiet
    _KEYRING_WARNED = True
    print("keyring backend unavailable and vendor keys are not in the environment — "
          "they cannot be resolved (install: pip install keyring, then `harness.py keys set NAME`). "
          "Key values are never logged.", file=sys.stderr)
    return True


def inject_vault_keys() -> list[str]:
    """The cascade seam (env → VAULT): fill registry env keys absent from
    os.environ from the vault. Never raises — a missing backend warns once and
    returns empty, so a harness command that needs no key still runs. The
    resolution is VALUE-FREE (nothing here logs a key value, only NAMES).

    HARNESS_NO_VAULT=1 suppresses injection: fixtures that must exercise the
    key-ABSENT path (a refusal test, post-clone onboarding) need ambient keys to
    stay out. This guard inherits the job HARNESS_NO_DOTENV used to do for free —
    it sat before this call inside load_env_file, so removing that function
    without this would let a real operator vault turn those refusal tests green.
    """
    import os

    injected: list[str] = []
    if os.environ.get("HARNESS_NO_VAULT") == "1":
        return injected
    kr = _keyring()
    if kr is None:
        _warn_no_vault_once()
        return injected
    for name in env_key_names():
        if os.environ.get(name):
            continue
        try:
            value = kr.get_password(SERVICE, name)
        except Exception:
            continue
        if value:
            os.environ[name] = value
            injected.append(name)
    return injected


def rows(root: Path | str) -> list[dict[str, Any]]:
    """`keys list` rows: presence + backend + lastRotated; values stay masked
    (reusing the SPEC-130 masking, never echoed raw)."""
    import os

    root = Path(root)
    meta = _load_meta(root)
    out: list[dict[str, Any]] = []
    for name in env_key_names():
        env_val = os.environ.get(name) or ""
        vault_val = vault_get(name)  # one read; derive both in_vault and diverges
        in_vault = vault_val is not None
        if env_val:
            backend, display = "env", config_keys._mask(env_val)
        elif in_vault:
            backend, display = "keyring", "in vault"
        else:
            backend, display = "", ""
        out.append({"name": name, "backend": backend,
                    "set": bool(env_val or in_vault),
                    "lastRotated": (meta.get(name) or {}).get("lastRotated"),
                    "display": display,
                    "diverges": bool(env_val) and in_vault and env_val != vault_val})
    return out


def stale_keys(root: Path | str, now: dt.datetime | None = None) -> list[tuple[str, str]]:
    """(name, lastRotated) for vault keys older than STALE_DAYS — doctor feed."""
    now = now or dt.datetime.now().astimezone()
    out: list[tuple[str, str]] = []
    for name, info in _load_meta(root).items():
        rotated = (info or {}).get("lastRotated")
        try:
            age = now - dt.datetime.fromisoformat(str(rotated))
        except Exception:
            continue
        if age.days > STALE_DAYS:
            out.append((name, str(rotated)))
    return sorted(out)


def shadow_divergences() -> list[str]:
    """Registry env keys whose PROCESS env value differs from the vault value
    for the SAME name — the stale-env-shadows-vault 401 incident class,
    SURFACED never fixed. VALUE-FREE: NAMES only, registry order. env-set-but-
    vault-empty is NOT a divergence (legit CI/container provisioning). Compares
    RAW values (no .strip): a trailing-newline env value that 401s while the
    vault is clean IS the incident — normalising would mask it. Never raises.

    HARNESS_NO_VAULT=1 suppresses it, mirroring inject_vault_keys(): the vault
    is off harness-wide, so there is nothing to shadow (and the rh scenario
    relies on this for a deterministic ok). Reflects only the CURRENT process
    env, not persisted Windows User/Machine scope (HKCU\\Environment)."""
    import os

    if os.environ.get("HARNESS_NO_VAULT") == "1":
        return []
    out: list[str] = []
    for name in env_key_names():
        env_val = os.environ.get(name)
        if not env_val:
            continue  # env-only/absent side is never a divergence
        vault_val = vault_get(name)  # only env-SET keys hit the vault
        if vault_val is not None and env_val != vault_val:
            out.append(name)
    return out


def cmd_keys(args) -> int:
    """`harness.py keys list|set|unset` — the single vault write path."""
    from harness_lib import common

    action = getattr(args, "action", "list") or "list"
    name = getattr(args, "name", None)
    if action == "list":
        data = rows(ROOT)
        if common.agent_output_compact():
            common.emit(data, tsv=True)  # TE.5: agent-facing lists stay tabular
            return 0
        if getattr(args, "json", False):
            common.emit(data)
            return 0
        width = max(len(r["name"]) for r in data)
        for r in data:
            print(f"{r['name']:<{width}}  {r['backend'] or '-':<8}  "
                  f"{'set' if r['set'] else 'unset':<6}  "
                  f"{r['lastRotated'] or '':<25}  {r['display']}"
                  f"{'  != vault' if r.get('diverges') else ''}")
        kr = "available" if _keyring() else "NOT installed — REQUIRED (pip install keyring)"
        print(f"\nvault backend: {kr}")
        return 0
    if action == "set":
        if not name:
            raise HarnessError("usage: keys set NAME  (value read from stdin, never argv)")
        if sys.stdin.isatty():
            import getpass
            value = getpass.getpass(f"value for {name} (input hidden): ")
        else:
            value = sys.stdin.readline().rstrip("\r\n")
        set_key(ROOT, name, value)
        print(f"stored {name} in the OS vault (service {SERVICE}); value not echoed")
        return 0
    if action == "unset":
        if not name:
            raise HarnessError("usage: keys unset NAME")
        unset_key(ROOT, name)
        print(f"removed {name} from the OS vault")
        return 0
    raise HarnessError(f"unknown keys action: {action}")


def _demo() -> None:
    import os
    import tempfile
    import types

    store: dict[tuple[str, str], str] = {}
    fake = types.SimpleNamespace(
        set_password=lambda s, n, v: store.__setitem__((s, n), v),
        get_password=lambda s, n: store.get((s, n)),
        delete_password=lambda s, n: store.pop((s, n)))
    real_keyring = globals()["_keyring"]
    globals()["_keyring"] = lambda: fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            set_key(root, "GEMINI_API_KEY", "fake-value-123")
            assert vault_get("GEMINI_API_KEY") == "fake-value-123"
            meta = _load_meta(root)
            assert meta["GEMINI_API_KEY"]["lastRotated"], meta
            # records entry carries the NAME, never the value.
            worklog = (root / ".harness" / "state" / "worklog.json").read_text(encoding="utf-8")
            assert "key set: GEMINI_API_KEY" in worklog and "fake-value-123" not in worklog
            # cascade: env wins; vault fills the hole; injection is idempotent.
            os.environ.pop("GEMINI_API_KEY", None)
            injected = inject_vault_keys()
            assert "GEMINI_API_KEY" in injected and os.environ["GEMINI_API_KEY"] == "fake-value-123"
            assert "GEMINI_API_KEY" not in inject_vault_keys()  # env now wins
            os.environ.pop("GEMINI_API_KEY", None)
            # stale detection.
            meta["GEMINI_API_KEY"]["lastRotated"] = "2020-01-01T00:00:00+00:00"
            _save_meta(root, meta)
            assert stale_keys(root) == [("GEMINI_API_KEY", "2020-01-01T00:00:00+00:00")]
            unset_key(root, "GEMINI_API_KEY")
            assert vault_get("GEMINI_API_KEY") is None and not _load_meta(root)
            # shadow-divergence detector (value-free): env differs from vault -> NAME only.
            os.environ.pop("HARNESS_NO_VAULT", None)
            os.environ["GEMINI_API_KEY"] = "env-side"
            set_key(root, "GEMINI_API_KEY", "vault-side")          # env != vault
            assert shadow_divergences() == ["GEMINI_API_KEY"]
            srow = next(r for r in rows(root) if r["name"] == "GEMINI_API_KEY")
            assert srow["diverges"] is True and srow["backend"] == "env"
            assert "env-side" not in repr(srow) and "vault-side" not in repr(srow)
            assert "!= vault" not in srow["display"]               # marker never on the masked value
            os.environ["HARNESS_NO_VAULT"] = "1"                   # guard short-circuits
            assert shadow_divergences() == []
            os.environ.pop("HARNESS_NO_VAULT", None)
            set_key(root, "GEMINI_API_KEY", "env-side")            # env == vault -> no divergence
            assert shadow_divergences() == []
            os.environ.pop("GEMINI_API_KEY", None)                 # env-only gone, vault set -> not reported
            assert shadow_divergences() == []
            unset_key(root, "GEMINI_API_KEY")
            # keys-keyring v2: a planted .env is INERT — no tier reads it any more.
            (root / ".env").write_text("NVIDIA_API_KEY=nv-secret-xyz\n", encoding="utf-8")
            os.environ.pop("NVIDIA_API_KEY", None)
            assert resolved_source("NVIDIA_API_KEY", root) == "missing"
            assert "NVIDIA_API_KEY" not in inject_vault_keys()
            assert not os.environ.get("NVIDIA_API_KEY")
            assert not any(r["set"] for r in rows(root) if r["name"] == "NVIDIA_API_KEY")
    finally:
        globals()["_keyring"] = real_keyring
    # warn-once latch, v2 trigger: vault unreachable AND the key absent from the
    # environment (the gap itself). A planted .env must NOT silence it — that
    # regression is exactly what removing the tier could have introduced.
    import contextlib
    import io
    global _KEYRING_WARNED
    globals()["_keyring"] = lambda: None
    _KEYRING_WARNED = False
    try:
        with tempfile.TemporaryDirectory() as tmp2:
            (Path(tmp2) / ".env").write_text("GEMINI_API_KEY=should-not-print\n", encoding="utf-8")
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ["GEMINI_API_KEY"] = "x"    # keyring absent -> vault_get None -> not a divergence
            assert shadow_divergences() == []
            os.environ.pop("GEMINI_API_KEY", None)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                first, second = _warn_no_vault_once(), _warn_no_vault_once()
            out = buf.getvalue()
            assert first is True and second is False
            assert "should-not-print" not in out and out.count("keyring backend unavailable") == 1
    finally:
        globals()["_keyring"] = real_keyring
        _KEYRING_WARNED = False
    print("keys_vault self-check: ok")


if __name__ == "__main__":
    _demo()
