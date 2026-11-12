# FLOW-04: cancellation, limits and recovery

This map qualifies the existing native Work path. OS owns authority and
settlement, Kernel owns process containment and durable effect receipts, and
Agent Runtime owns the tactical loop and consumed call/retry limits. It adds
no execution engine, daemon, provider retries or model admission.

The 6 fault points below each have 3 controlled schedules on Windows and Linux:
36 required JUnit outcomes. These are recovery traces over real Git, coordinator
and filesystem/process operations with deterministic inference, not 36 paid
model runs. A missing, skipped or failed case leaves its cell unqualified.
Schedules are chosen for the affected boundary; they are not arbitrary repeated
runs of one test or a universal interleaving proof.

## Fault map

Names below are exact pytest function names, including parameter values. Modules
are under `tests/`; each row is required on both platforms.

| Fault point | Controlled schedule | Module and case |
| --- | --- | --- |
| Reservation before dispatch | Workspace prepared before plan dispatch | `test_work_recovery::test_resume_before_acceptance_preserves_session_effects_and_limits[workspace]` |
| Reservation before dispatch | Reserved attempt before plan execution | `test_work_recovery::test_resume_before_acceptance_preserves_session_effects_and_limits[before_plan]` |
| Reservation before dispatch | Cancel after source preparation before model dispatch | `test_work_run::test_native_resume_after_cancelled_source_preparation_keeps_initial_binding[False]` |
| Effect before receipt | Edit changed bytes; runtime result absent at handoff | `test_work_run::test_native_handoff_cannot_clear_unrecorded_edit_result` |
| Effect before receipt | External verification result absent at recovery | `test_work_recovery::test_resume_refuses_revocation_and_unknown_effect_without_reexecution[unknown_effect]` |
| Effect before receipt | Durable Kernel edit result before runtime acknowledgement | `test_native_effect_recovery::test_resume_after_edit_checkpoint_crash_preserves_effect_and_updates_activity[False]` |
| Receipt before transition | Edit receipt before host transition | `test_work_recovery::test_resume_before_acceptance_preserves_session_effects_and_limits[edit_receipt]` |
| Receipt before transition | Candidate observation durable before host acknowledgement | `test_native_settlement::test_native_resume_crash_cuts_reuse_candidate_without_more_model_calls[observation]` |
| Receipt before transition | Claim committed before acknowledgement | `test_native_settlement::test_native_resume_crash_cuts_reuse_candidate_without_more_model_calls[claim_ack]` |
| Landing before backlog | Git landed before graph write | `test_work_resume::test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution[git_landed]` |
| Landing before backlog | Graph written before caller acknowledgement | `test_work_resume::test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution[graph_written]` |
| Landing before backlog | Later Git descendant before completion reconstruction | `test_work_resume::test_resume_after_confirmed_landing_returns_result_or_refuses_inconsistent_evidence[descendant]` |
| Expiry/rebind | Handoff with uninterrupted rebind acknowledgement | `test_work_run::test_native_handoff_continues_saved_edit_and_survives_lost_ack[none]` |
| Expiry/rebind | Death before rebind journal commit | `test_work_run::test_native_handoff_continues_saved_edit_and_survives_lost_ack[before_rebind]` |
| Expiry/rebind | Death after rebind journal commit | `test_work_run::test_native_handoff_continues_saved_edit_and_survives_lost_ack[after_rebind]` |
| Cancellation/late result | Predecessor arrives before candidate verification | `test_flow04_late_claim::test_late_native_proposal_after_handoff_cannot_accept_or_land[before_candidate]` |
| Cancellation/late result | Predecessor arrives after candidate verification | `test_flow04_late_claim::test_late_native_proposal_after_handoff_cannot_accept_or_land[after_candidate]` |
| Cancellation/late result | Handoff after last host check, before atomic claim | `test_flow04_late_claim::test_late_native_proposal_after_handoff_cannot_accept_or_land[during_claim]` |

The last three cases specifically guard the race between OS's control check
and coordinator claim admission. They require the successor to remain current,
the stale claim to remain unaccepted, the source HEAD to remain at the base, one
edit and five original fixture model calls, and no extra model/tool call on
predecessor Resume. Resume may append a diagnostic attempt record; it cannot
change the original identity, deadline, proposal or tactical journal.

Supplemental checks cover lease expiry during process death, token rotation
with lost acknowledgement, call/time exhaustion, changed identity/evidence and
unavailable custody. See `test_work_recovery`, `test_work_resume` and
`test_native_model_recovery`. `test_console_cancel_continuation` confirms that
Cancel/Quit wait for the in-flight operation's receipt and prevent the next
stage, including cancellation before a queued worker starts.

Public ConPTY dogfood exposed a keyboard dispatch defect: the focused composer
consumed Ctrl+X as Cut, leaving inference running. The fix makes that binding
take priority while busy; idle Cut and existing modal/direct-terminal handling
are retained. `test_flow04_cancel_key` reproduces the old failure and verifies
both busy cancellation with a queued draft and idle text cutting. Cancel still
requests a stop; it does not itself prove that cleanup finished.

The corrected public ConPTY replay recorded `CANCELLED / CANCEL_REQUESTED`
after one fixture model intent, no effect dispatch and no repeated request.
Kernel recorded Windows Job containment and verified local cleanup; remote
completion and usage remained unknown. Cancel reached its outcome in 0.78
seconds in that exercise. The TUI then quit with exit code 0 and verified
cleanup. This is a measured fixture result, not a cancellation latency SLA.

## Evidence custody and platform scope

The first 30 cells have exact JUnit receipts on both platforms in
[CI run 38258461950](https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/38258461950).
Its actual checked-out merge revision is
`a92e9455e71777a3ce3f70c197138ab4443f96ca`; source bytes for the six reused test
modules and their native recovery/settlement implementation match this
delivery's base. The composer keyboard dispatch fix receives fresh tests and
public ConPTY evidence. Kernel is `4645536ec0c0d3269f64f72b3fac00505701b353`; Runtime is
`d4c5ea9c9dfc29a2d0cbd8cc2ec684a692e520f3`. That run was focused (1,891 cases
per platform), not a complete OS regression. The last six cells require the
Windows/Linux CI receipts of this change's new test module.

Kernel's current `tests/test_native_http_cancellation.py` separately passed
31 cases on Windows and 31 on Linux/WSL. These include real loopback stalls and
dripping responses, credential resolution before dispatch, deadline expiration,
containment of descendants, unknown cleanup, and a complete reply racing Cancel.
They assert physical local cleanup and retained usage without executing a
returned tool after cancellation. They make no remote billing claim.

Private evidence stays outside the source tree: the bounded execution packet,
original failed checks and corrected replays, SHA256-correlated JUnit cells,
public ConPTY transcripts/keys, physical receipts and installation/WIP checks.
The existing public-TUI terminal replay and crash-recovery exercises are retained
as ordinary-run evidence; they are not promoted to vendor-wide qualification.

## Stop and retry policy

| Observation | Meaning and permitted next step |
| --- | --- |
| Cancel requested, operation still running | Retain running state; wait for the owned process/operation receipt; no next stage. |
| Kernel confirms owned process cleanup | Local termination is confirmed. Remote completion and billing can remain unknown. |
| Durable terminal delivery, elapsed deadline | Replay the original recorded outcome only; no renewed deadline, model/tool call or new input. Cancellation/revocation still wins. |
| Completed edit with a matching durable Kernel receipt | Reconcile that exact effect on explicit Resume; retain identity, consumed calls and original deadline. |
| Interrupted model request with confirmed local cleanup | Explicit Resume may abandon that response once if the existing call/time limits allow; preserve consumed intent and unknown usage. |
| External command/effect without a matching result receipt, or unconfirmed cleanup | Keep effect-unknown/reconciliation-required. Inspect the process, output and changed state before deciding recovery; never automatically repeat the command. |
| Confirmed retryable rejection (for example HTTP 429) | Runtime's original finite retry budget applies; no hidden adapter retries. |
| Malformed completed reply, invalid candidate, exhausted budget, stale/revoked binding | Stop; preserve diagnostics. Rebind cannot clear an uncertain effect or authorize a predecessor's late claim. |
| Git already landed and exact admission evidence remains valid | Reconcile the backlog for that candidate; no second landing, implementation, model or verifier call. |

Native enforcement currently covers calls, retries, stagnation and elapsed time.
It does not enforce a provider-dollar wallet. Unknown provider usage is not zero,
and local termination does not guarantee that charging stopped. This offline
qualification spends no provider quota and does not qualify every Claude response
format, unknown execution profile, vendor refund policy or proposed ADR/spec.

## Outside the native Work path

What now holds: on Windows, the TUI's conversation-turn and Work children run in one kill-on-close Job Object owned
by the TUI process. When the TUI process ends for any reason (Task Manager, `taskkill` without `/T`, a crash), Windows
closes the Job and ends those children and, through the Jobs the Kernel gives them, their provider processes.
`tests/test_console_child_containment.py` checks it with real processes: it kills only the owner and asserts the
child (and a grandchild in a nested Job) exits, and that the same owner without containment leaves the child running.
An abrupt end still leaves the session as it was; it is picked up by the existing recovery (reopen the conversation;
`console_recovery`).

Known limits, not covered:

- New conversations default to Unlimited calls and time, and delegated children inherit it.
- There is no ceiling above one session (per day or per machine) in calls or dollars.
- `tare serve` turns cannot be cancelled and their per-message limits renew.
- On Linux and WSL, children and provider process groups outlive a killed owner.
- Cancel and Quit wait for the child without escalating to a kill.
- Image description for models that cannot see ignores Cancel for up to 240-270 s per route.
