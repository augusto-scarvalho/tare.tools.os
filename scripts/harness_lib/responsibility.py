#!/usr/bin/env python3
"""Typed responsibility chain: the decision/approval/acceptance badge (SPEC-161, N-AUTHCHAIN).

Every write of a decision/approval/acceptance credits a TYPED actor -- and, where
multiple parties act, the ordered CHAIN of roles -- instead of a bare timestamp +
free prose. Makes separation-of-duties (proposer != accepter) executable and gives
the security-reviewer role (D014) a seat in the chain.

Actor record: `{role, actorType, identity, at, sessionRef?}`; `actorType` in
`ACTOR_TYPES`. `role` is free text but the canonicals are proposer|reviewer|accepter.
`automation` credits deterministic owner-gates and NEVER presents as a human.
Identity is DECLARED (env/git), not authenticated -- the same local-first ceiling as
the rest of the harness; cryptographic signing of the chain is hook C (`chain_digest`)
plus key infra, gated on the first multi-operator deploy. `chain_digest` is a LOCAL
sha256 over canonical JSON: it does NOT touch T-HASHCHAIN.

`make_actor` NEVER raises (invariant 2): inference failure yields an honest
`{actorType: "user", identity: "unknown"}`, not a crash.

ponytail: worker inference activates when the spawn sets HARNESS_WORKER_ID -- it does
today (scripts/harness.py worker_harness_vars sets it for every spawned worker). If a
future spawn path drops it, inference degrades to honest-user, never a crash; no env
of any existing spawn is changed by this module.

Self-check: python scripts/harness_lib/responsibility.py.
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
from harness_lib.common import now  # noqa: E402

ACTOR_TYPES = ("user", "worker", "overseer", "automation")


def _git_user_name() -> str:
    """`git config user.name` via the mediated spawn (raw-subprocess ratchet),
    bounded, best-effort. Empty string on any failure (no git, timeout,
    non-zero) -- inference must never raise (invariant 2)."""
    try:
        from harness_lib import processes  # local: keep module import light
        proc = processes.run_quiet(["git", "config", "user.name"], timeout=5,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001 -- inference never crashes a write
        return ""


def make_actor(role: str, actor_type: str | None = None, identity: str | None = None,
               session_ref: str | None = None) -> dict[str, Any]:
    """Build a typed actor record. Explicit params ALWAYS win over env inference.

    Type: explicit `actor_type` (if in ACTOR_TYPES) > env `HARNESS_ACTOR_TYPE` (if
    valid) > `worker` when `HARNESS_WORKER_ID` is set > `user` when
    `HARNESS_CHAT_HITL_OK=1` (the human escape hatch: a human IS at the wheel even
    inside an agent session) > `overseer` when `CLAUDE_SESSION_ID` is present (an
    agent session acting unattended must NEVER credit the human by name — the
    2026-07-21 reckon reproduced exactly that fabrication) > `user`.
    Identity: explicit > `worker:<id>` / `overseer:<sessionRef>` by resolved type >
    env `HARNESS_ACTOR_IDENTITY` > `git config user.name` > `"unknown"`.
    `sessionRef`: `session_ref` param or env `CLAUDE_SESSION_ID` (omitted if absent).
    NEVER raises (invariant 2): any inference failure yields user/unknown honestly."""
    try:
        env = os.environ
        env_type = env.get("HARNESS_ACTOR_TYPE", "").strip()
        worker_id = env.get("HARNESS_WORKER_ID", "").strip()
        session_env = (session_ref or env.get("CLAUDE_SESSION_ID", "")).strip()
        if actor_type in ACTOR_TYPES:
            a_type = actor_type
        elif env_type in ACTOR_TYPES:
            a_type = env_type
        elif worker_id:
            a_type = "worker"
        elif env.get("HARNESS_CHAT_HITL_OK", "").strip() == "1":
            a_type = "user"
        elif session_env:
            a_type = "overseer"
        else:
            a_type = "user"
        if identity:
            a_ident = str(identity)
        elif a_type == "worker" and worker_id:
            a_ident = f"worker:{worker_id}"
        elif env.get("HARNESS_ACTOR_IDENTITY", "").strip():
            a_ident = env["HARNESS_ACTOR_IDENTITY"].strip()  # deliberate declaration wins
        elif a_type == "overseer" and session_env:
            a_ident = f"overseer:{session_env}"  # derived, never the human's git name
        else:
            a_ident = _git_user_name() or "unknown"
        actor: dict[str, Any] = {"role": str(role), "actorType": a_type,
                                 "identity": a_ident, "at": now()}
        if session_env:
            actor["sessionRef"] = str(session_env)
        return actor
    except Exception:  # noqa: BLE001 -- invariant 2: inference never crashes a write
        return {"role": str(role), "actorType": "user", "identity": "unknown", "at": now()}


def stamp(chain: list[dict[str, Any]], actor: dict[str, Any]) -> list[dict[str, Any]]:
    """Append `actor` to `chain` preserving order; return the (mutated) chain."""
    chain.append(actor)
    return chain


def verify_separation(chain: list[dict[str, Any]]) -> tuple[bool, str]:
    """Two-person control (spec 7.7 R2/R3), ADVISORY: the caller decides what to do
    with a False. Returns (ok, detail); ok=False iff some role=="proposer" and some
    role=="accepter" share the SAME verifiable identity -- detail names it. Two
    "unknown"s are NOT a violation (2026-07-21 reckon: unrelated unverifiable
    identities colliding on the sentinel is a false positive); they return ok=True
    with an INDETERMINATE detail so the caller can escalate honesty separately."""
    def _idents(role_name: str) -> set[str]:
        return {str(a.get("identity")) for a in chain
                if a.get("role") == role_name and a.get("identity")
                and str(a.get("identity")) != "unknown"}

    shared = _idents("proposer") & _idents("accepter")
    if shared:
        who = sorted(shared)[0]
        return (False, f"separation-of-duties violated: {who!r} is both proposer and accepter")
    unknowns = any(str(a.get("identity", "")) in ("", "unknown") for a in chain
                   if a.get("role") in ("proposer", "accepter"))
    if unknowns:
        return (True, "indeterminate: unverifiable identity ('unknown') in the chain — "
                      "separation cannot be proven or refuted")
    return (True, "proposer and accepter are distinct identities (or the chain lacks both)")


def chain_digest(chain: list[dict[str, Any]]) -> str:
    """Hook C: sha256 of the canonical JSON (sort_keys, compact separators) of the
    chain -- deterministic and additive, reserved for the future signed binding.
    Equal chains -> equal digest; any difference -> a different digest."""
    canonical = json.dumps(chain, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _self_check() -> int:
    saved = {k: os.environ.get(k) for k in
             ("HARNESS_ACTOR_TYPE", "HARNESS_WORKER_ID", "HARNESS_ACTOR_IDENTITY",
              "CLAUDE_SESSION_ID", "HARNESS_CHAT_HITL_OK")}

    def _restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        # invariant 2: worker inference via env.
        for k in saved:
            os.environ.pop(k, None)
        os.environ["HARNESS_WORKER_ID"] = "w7"
        a = make_actor("reviewer")
        assert a["actorType"] == "worker" and a["identity"] == "worker:w7", a
        assert a["role"] == "reviewer" and a["at"] and "sessionRef" not in a, a

        # env HARNESS_ACTOR_TYPE wins over worker inference; sessionRef flows from env.
        os.environ["HARNESS_ACTOR_TYPE"] = "overseer"
        os.environ["HARNESS_ACTOR_IDENTITY"] = "carol"
        os.environ["CLAUDE_SESSION_ID"] = "sess-1"
        a = make_actor("accepter")
        assert a["actorType"] == "overseer" and a["identity"] == "carol", a
        assert a["sessionRef"] == "sess-1", a

        # explicit params ALWAYS win over env inference.
        a = make_actor("proposer", actor_type="user", identity="alice", session_ref="s2")
        assert a["actorType"] == "user" and a["identity"] == "alice" and a["sessionRef"] == "s2", a
        # an out-of-vocab explicit type falls through to env/worker/user, never crashes.
        a = make_actor("x", actor_type="martian")
        assert a["actorType"] in ACTOR_TYPES, a

        # AGENT SESSION HONESTY (2026-07-21): bare CLAUDE_SESSION_ID = unattended
        # agent -> overseer:<session>, NEVER the human's git name; the HITL escape
        # hatch (a human at the wheel inside the session) restores user.
        for k in saved:
            os.environ.pop(k, None)
        os.environ["CLAUDE_SESSION_ID"] = "sess-x"
        a = make_actor("reviewer")
        assert a["actorType"] == "overseer" and a["identity"] == "overseer:sess-x", a
        os.environ["HARNESS_CHAT_HITL_OK"] = "1"
        a = make_actor("accepter")
        assert a["actorType"] == "user", a

        # fallback never-raises: clean env yields a non-empty honest identity.
        for k in saved:
            os.environ.pop(k, None)
        a = make_actor("proposer")
        assert a["actorType"] == "user" and a["identity"] and a["role"] == "proposer", a
    finally:
        _restore()

    # stamp preserves order.
    chain: list[dict[str, Any]] = []
    p = {"role": "proposer", "actorType": "worker", "identity": "worker:w1", "at": "t1"}
    r = {"role": "reviewer", "actorType": "overseer", "identity": "carol", "at": "t2"}
    stamp(chain, p)
    stamp(chain, r)
    assert chain == [p, r], chain

    # verify_separation: distinct proposer/accepter -> ok; same identity -> violation.
    acc_ok = {"role": "accepter", "actorType": "user", "identity": "dave", "at": "t3"}
    ok, _ = verify_separation([p, r, acc_ok])
    assert ok is True
    acc_bad = {"role": "accepter", "actorType": "worker", "identity": "worker:w1", "at": "t3"}
    ok, detail = verify_separation([p, acc_bad])
    assert ok is False and "worker:w1" in detail, (ok, detail)
    # a chain lacking a proposer (or accepter) is not a violation.
    assert verify_separation([r, acc_ok])[0] is True
    # unknown==unknown is indeterminate, never a violation (sentinel collision).
    unk = [{"role": "proposer", "actorType": "user", "identity": "unknown", "at": "t1"},
           {"role": "accepter", "actorType": "user", "identity": "unknown", "at": "t2"}]
    ok, detail = verify_separation(unk)
    assert ok is True and "indeterminate" in detail, (ok, detail)

    # chain_digest: stable for equal chains, sensitive to any difference.
    assert chain_digest([p, r]) == chain_digest([p, r])
    assert chain_digest([p, r]) != chain_digest([r, p])  # order matters
    assert chain_digest([p, r]) != chain_digest([p, acc_ok])
    assert len(chain_digest([p, r])) == 64

    print("responsibility self-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
