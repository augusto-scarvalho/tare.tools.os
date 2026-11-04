# Multi-Agent Relay Mesh (MARM) Protocol Manual
**Distributed, Asynchronous Multi-Agent Swarm Orchestration over Relay Filesystem**

## v3 Control-Plane Contracts (TRAIN-06 / RELAY-CONTROL-PLANE-01)

1. **FSM + revision CAS.** Every transition goes through the explicit matrix in
   `relay_mesh.FSM` (unknown states/edges refuse). `revision` is monotonic
   (`len(history)` for legacy trains); pass `expected_revision` for
   compare-and-swap — concurrent writers get one success, one explicit
   conflict. Evidence gates: `PLAN_APPROVED` needs a task-bound
   `PLAN_AUDIT_REPORT.md` with `PLAN_VERDICT: APPROVED`; strict candidates use
   `READY_FOR_LANDING` only after an exact `ADMISSION_VERDICT` recomputation.
   Optional/required model audits bind the candidate and must come from a
   different roster `bias_domain`; cross-vendor is preferred. Legacy
   `READY_FOR_HUMAN_APPROVAL` remains compatible. `IN_PROGRESS_*` needs the
   current role lease. `LANDED` is terminal.
2. **Scoped landing capability.** `land_all_pending` is REMOVED. A smart READY
   transition binds candidate/evidence/identity/verdict digests and needs no
   human nonce; `python relay/relay_mesh.py admit --train X` recomputes and
   settles it. The legacy human READY transition remains bound to
   (train, ready revision, packet/diff/audit digests, nonce). Local human landing:
   `land --train X` (requires the local `human_gatekeeper` credential outside
   the synced tree). Mobile landing: `APPROVED_<train>.md` CONTAINING the
   nonce. Generic markers land nothing. Single use via status/revision CAS —
   no consumed-nonce store.
3. **Leases are credentialed.** Role credentials live in `~/.tare-relay-credentials`
   (or `RELAY_CRED_DIR`), outside the synced tree. Non-owner release/renew
   refuses. Long audits heartbeat (`renew-lease`); TTL exceeds audit timeouts.
4. **Typed idempotent message plane.** Messages carry an envelope with a
   durable action key `(train, revision, action, role)`; subject is display
   only. Handling moves messages to `processed/` (ACK) or `quarantine/` after
   3 bounded attempts. `human`/`antigravity` inboxes are NEVER auto-consumed.
   Readers accept UTF-8 BOM; writers emit UTF-8 without BOM (relay_io).
5. **One supervisor, 15s floor.** `watcher_daemon.py` v3 is the only effectful
   automation: heartbeat in `history/daemon-heartbeat.json`, duplicate
   supervisors hold fail-closed, all watchers poll >= 15s, level-triggered.
   Audits delegate to LLM critics via `RELAY_PLAN_AUDIT_CMD` /
   `RELAY_CODE_AUDIT_CMD`; absence holds the gate and pings the human once.
6. **Compact board.** `BOARD.json`/`CURRENT_BOARD.md` list only nonterminal
   trains, active leases, alerts, approvals + landed count/recent tombstones.
   Renders are digest-guarded: unchanged state preserves bytes AND mtime.
   Landed trains are archived once to `history/` and tombstoned in place
   (train_id/task_ids/status/revision/evidence digests retained).
7. **Kill switch.** Stop the supervisor; keep `human_watcher.py` (read-only,
   level-triggered, ntfy phone push) as the fallback notifier. Never restore
   wildcard landing or free-form transitions.
8. **Telemetry** is observational (`history/telemetry.jsonl`); it never
   advances or blocks a gate. Context capsules (`relay_mesh.py capsule`) are
   indexes — gates always reopen authoritative sources.

- **Scope:** Multi-machine, multi-runtime agent coordination (`Antigravity`, `Claude`, `ChatGPT`, `Gemini`, `Cursor`, `Copilot`).
- **Transport:** Google Drive may replicate idempotent messages and finished artifacts. The workspace, `.git`, locks, WAL, streams, cursors and active state stay in local storage; Drive is never an operational filesystem or a lock service.
- **Human Policy:** **Exception & Sign-Off Only** (Human is only pinged for critical alerts / deadlock or 1-click release train landing).

---

## 🎭 1. The 4 Dynamic Agent Roles

When interacting with a work train, each agent adopts a specific role:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. PLANNER / ARCHITECT (e.g. ChatGPT / Claude)                         │
│    • Queries work-graph.json (frontier / next).                        │
│    • Bundles 1-3 tasks into an Execution Train (relay/trains/TRAIN-XX).│
│    • Writes PACKET.md with contract-driven exit criteria.              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Dispatches to PLAN-AUDITOR inbox — NOT
                                    │  straight to implementer; owner ruling
                                    │  2026-08-16 after TRAIN-03 skip incident)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 1b. PLAN AUDITOR (external agent, role "plan-auditor") — FINAL WORD    │
│    • Claims lease; reviews PACKET.md against CURRENT code/specs.       │
│    • Checks: real vs assumed gaps, footprint minimality, disjointness, │
│      no authority broadening, exit criteria falsifiable.               │
│    • Has the LAST word on the plan: NEVER returns it to the planner    │
│      for rebuttal. Edits PACKET.md in place to the corrected version   │
│      (logging changes in a "Plan audit corrections" section), then     │
│      ALWAYS: dispatch --next-role "implementer"                        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Notifies implementer inbox)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. IMPLEMENTER (e.g. Antigravity / Cursor on Machine A)                │
│    • Claims lease: python relay/relay_mesh.py claim ...                │
│    • Reads PACKET.md, writes code, creates unit tests.                 │
│    • Verifies local test suite passes (100% green).                    │
│    • Generates DIFF.patch, dispatches to auditor:                      │
│      python relay/relay_mesh.py dispatch --train TRAIN-XX ...          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Notifies auditor inbox)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. ADVERSARIAL AUDITOR (e.g. Gemini / Test Agent on Machine B)         │
│    RULE: must be an EXTERNAL agent claiming via the relay — never the  │
│    implementer's own session or its subagents (owner ruling 2026-08-16)│
│    • Claims audit lease.                                               │
│    • Runs mutation tests, fuzzing, property tests, DAG linting.        │
│    • Tries to break the implementation or find missing edge cases.     │
│    • IF FAIL ──► Dispatches rejection back to implementer or alerts.   │
│    • IF PASS ──► Signs AUDIT_REPORT.md and marks READY_FOR_APPROVAL:   │
│      python relay/relay_mesh.py audit --train TRAIN-XX --status PASS   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Generates Approval Dossier)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. HUMAN GATEKEEPER (The User)                                         │
│    • Reads relay/approvals/APPROVAL_REQUEST_TRAIN-XX.md.               │
│    • 1-Click approval: python relay/relay_mesh.py land --train TRAIN-XX│
│    • Automatically marks tasks DONE in work-graph.json and commits!    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 2. Step-by-Step CLI Commands for Agents

All commands use standard Python 3 with **zero dependencies**:

### A. Check What's Happening Across All Machines
```bash
python relay/relay_mesh.py board
```
👉 Inspect `relay/CURRENT_BOARD.md` to see active leases, in-progress trains, and pending mail.

---

### B. Planner: Initialize a New Release Train
```bash
python relay/relay_mesh.py init-train --id TRAIN-01-BOOTSTRAP --title "Phase-T Bootstrap & Process Repair" --tasks "PROCESS-REPAIR-01,GRAPH-BACKLOG-02" --agent "chatgpt"
```
* Creates `relay/trains/TRAIN-01-BOOTSTRAP/PACKET.md` and notifies the implementer inbox.

---

### C. Implementer: Claim & Work on a Train
```bash
# 1. Claim lease (prevents other agents from modifying this train)
python relay/relay_mesh.py claim --train TRAIN-01-BOOTSTRAP --agent "antigravity_pc1" --role "implementer"

# 2. Implement the code and run local tests

# 3. When code is complete and tests pass, dispatch to the Auditor:
python relay/relay_mesh.py dispatch --train TRAIN-01-BOOTSTRAP --next-role "auditor" --agent "antigravity_pc1" --notes "Implemented PROCESS-REPAIR-01 envelope and graph_backlog_adapter.py. 49/49 tests pass."
```

---

### D. Auditor: Audit & Adversarial Verification
```bash
# 1. Claim audit lease
python relay/relay_mesh.py claim --train TRAIN-01-BOOTSTRAP --agent "gemini_pc2" --role "auditor"

# 2. Run adversarial checks:
#    - python -m pytest tests/
#    - python side_projects/tare.tools.backlog-graph/graph_ops.py --graph work-graph.json lint

# 3. Author AUDIT_REPORT.md with the mandatory "Audit Recommendation: Approve vs Do Not Approve":
#    - Reasons to APPROVE (proofs, falsifiers, no regressions)
#    - Risks / reasons NOT TO APPROVE (untested ranges, residual risks)
#    - Auditor's executive verdict

# 4. If everything is solid:
python relay/relay_mesh.py audit --train TRAIN-01-BOOTSTRAP --status PASS --agent "gemini_pc2"

# OR if tests fail or invariants break:
python relay/relay_mesh.py audit --train TRAIN-01-BOOTSTRAP --status FAIL --reason "Mutation test detected unhandled NoneType in OID derivation" --agent "gemini_pc2"
```

---

### E. Human (or Automated CI Gateway): 1-Click Landing
When the Auditor passes a train, an approval dossier is automatically generated at `relay/approvals/APPROVAL_REQUEST_<TRAIN_ID>.md`.

**External human notification (owner ruling 2026-08-16):** the human does NOT
read agent chats — an auditor announcing "ready for approval" inside its own
session reaches nobody. The external signal is `relay/human_watcher.py`
(level-triggered, zero-dep): it polls `relay/approvals/` and `relay/alerts/`
and for anything awaiting the human it (1) PUSHES TO THE OWNER'S PHONE via
ntfy.sh (topic pinned in the script; subscribe once in the ntfy app or at
https://ntfy.sh/<topic>) and (2) fires a non-blocking Windows popup as desktop
fallback — re-reminding every 15 min while pending. Keep it running on the
owner's machine:

```powershell
pythonw C:\projects\tare-tools-relay\relay\human_watcher.py     # background, no console
# auto-start on logon:
schtasks /create /tn tare-relay-human-watcher /tr "pythonw C:\projects\tare-tools-relay\relay\human_watcher.py" /sc onlogon
```

Agents: never assume the human saw an approval; the dossier file + this
watcher ARE the notification channel.

The human simply executes:
```bash
python relay/relay_mesh.py land --train TRAIN-01-BOOTSTRAP
```
* Automatically marks `PROCESS-REPAIR-01` and `GRAPH-BACKLOG-02` as `DONE` in `work-graph.json`.
* Releases all leases.
* Recalculates the frontier to unlock downstream tasks (`TCP-01`).

---

## 👁️ 3. Session Watcher Duty (Background Vigilance)

Any agent session that claims or dispatches work on a train MUST leave a
background watcher running for the rest of its session, so role handoffs are
reacted to without human polling. Two equivalent implementations:

1. **Console watcher (human terminal):**
   ```bash
   python relay/watcher.py <role>   # polls relay/inbox/<role> every 5s and prints new mail
   ```
2. **In-session agent watcher (e.g. Claude Monitor):** a persistent poll loop
   (~15s; Google Drive sync makes faster polling pointless) that is
   **LEVEL-TRIGGERED, not edge-triggered**: each cycle it recomputes the full
   actionable state from the files as they are NOW and emits it whenever it
   differs from what was last emitted — including on the very first cycle.
   Never diff raw directory snapshots: Drive desync can replay, reorder, or
   restore files, and an edge-diff watcher silently swallows state that
   existed before the session started or reappeared after a sync glitch.
   The recomputed state per cycle:
   - every train in `relay/trains/*/STATE.json` where `current_role == <own role>` and status is not `LANDED` is flagged `<ROLE>-READY`;
   - other trains as `train_id status current_role` (catches `READY_FOR_HUMAN_APPROVAL`, `LANDED`);
   - ALL files currently in `relay/inbox/<own role>/` (present = unconsumed);
   - ALL `relay/alerts/*.md` (cagada protocol — act immediately);
   - ALL `relay/approvals/*.md` (ping the human for 1-click landing).

   The projected state MUST include audit-report PRESENCE per active train
   (plan-report/audit-report yes/no) and the revision — a superseded/retracted
   report changes no STATE.json field and is otherwise invisible (2026-08-16
   TRAIN-07 blind spot: the plan-auditor retracted its report and edge/status
   watchers saw nothing).
   Reference snippet (bash): a `state()` function printing the lines above;
   `while true; do cur=$(state); [ "$cur" != "$last" ] && printf '%s\n' "$cur"
   && last="$cur"; sleep 15; done`. First deployed by `claude_fable_acer`
   while implementer on `TRAIN-01-BOOTSTRAP` (2026-08-15).

On `AUDIT_REJECTED` the implementer re-claims and fixes. A strict candidate
normally reaches `READY_FOR_LANDING` through smart policy; only the legacy or
exception path `READY_FOR_HUMAN_APPROVAL` notifies the human gatekeeper.

---

## 🚨 4. The "Cagada" Protocol (Alert Escalations)

If any agent discovers a critical deadlock, file conflict, or irreversible error:
```bash
python relay/relay_mesh.py alert --title "Circular Lease Lock Detected" --details "Agent X and Agent Y both attempted concurrent write on seam 1." --agent "antigravity"
```
* Instantly drops a high-priority alert markdown in `relay/alerts/` and notifies the Human.

---

## 🛡️ 5. Multi-Host Coordination Immunity (TRAIN-21, opt-in)

Google Drive is an **eventually-consistent transport, not a lock service**. A
synced lock file, heartbeat, hash, or nonce cannot prove that two disconnected
hosts did not act concurrently. The relay's local protections (revision-CAS,
writer locks, role leases, typed outcome keys) are defense-in-depth — **not** a
distributed mutex. The truthful deployment-safety boundary is an explicit
**multi-host safe mode** that names ONE operator-provisioned host as the only
authorized effectful writer.

### Contract

| Item | Value |
|---|---|
| Enable | `RELAY_MULTI_HOST=1` (also `true`/`yes`/`on`). **Default off** → legacy single-host behavior, byte-for-byte unchanged. |
| Credential | `<RELAY_CRED_DIR>/coordinator.json` — a JSON object `{"leaderHost": "<HOSTNAME>"}`. |
| Credential dir | `RELAY_CRED_DIR` (default `~/.tare-relay-credentials`). MUST resolve **outside** the synced relay root (canonical realpath disjointness) or every effect is refused. |
| Authorization | Predicate `relay_mesh.coordination_authorized()` — pure, read-only, re-evaluated at **every** effect boundary (no in-memory latching). Allows only when the credential exists, is readable/valid JSON, and its `leaderHost` equals this machine's hostname. |
| Never | The credential is never auto-created, copied, logged, hashed into a shared record, or accepted from Drive. |

When enabled and unauthorized, all effectful paths fail closed: `init-train`,
lease acquire/renew/release, FSM transition/dispatch/audit/land, approval
processing, the supervisor's heartbeat write, inbox sweep, reconcile, and audit
delegation. Read-only `board`, graph inspection, `capsule`, and human
notification remain available. **This is not automatic failover** — if the
authorized host dies, the pipeline halts until an operator performs the runbook.

### Manual owner transfer runbook (Host A → Host B)

1. **Stop** the old daemon on Host A.
2. **Remove/expire** Host A's local `coordinator.json` (out of band; never via Drive).
3. **Verify quiescence:** no live lease and no fresh `daemon-heartbeat.json`.
4. **Wait the quiescence floor** before activating Host B:

   > `quiescence ≥ 2 × lease_ttl + transport_sync_floor`, **minimum 120 seconds**.

   This lets Host A's final writes and deletions propagate across Drive so
   Host B does not overwrite unsynced bytes (clock-skew / premature-takeover
   guard).
5. **Provision** Host B's local `coordinator.json` with `leaderHost` = Host B's hostname (out of band).
6. **Start** the daemon on Host B.

Do **not** disable the guard, forge a credential, or edit relay state to force a
takeover. Rollback is limited to reverting the authorized code/doc/test diff in
a clean worktree; it never deletes leases, messages, evidence, or credentials.
