# I2I-06 — bounded CURRENT managed-product qualification

This is an executable **finite qualification**, equivalent in purpose to the
bounded-model option in issue26, not a TLA+/Alloy installation or an unbounded
proof. It follows the existing I2I-04 exact-rotation observer and I2I-05 independent
two-installation composition. It does not replace their live evidence, re-run a
rotation, or certify their present live state. The graph's mathematical/unbounded
and “100% recovery” language is strictly wider than the result here.

## Run and compact evidence

From the repository root, using the existing Python environment:

```text
python scripts/ops/model_product_transition.py
python -m pytest -q tests/test_product_transition_model.py tests/test_product_transition_faults.py
```

The first command emits one small work-linked JSON summary: counts, bounds,
assumptions, property results and shortest negative-control prefixes only. Keep
that summary with the work if needed; do not save the graph or create a collector.
The tests rerun this qualification for future hardening. No dependencies, model
calls, provider, scheduler, SSH or live product storage are required.

## Declared space and what is checked

Three release **identities** A=0, B=1, C=2; two competing updater slots; one
transient crash/lost-acknowledgement budget shared by the installation. B may be
rejected by qualification or pass qualification and fail startup. C is an
available qualified healthy successor. Numeric ordering is used only for the
publication rejection barrier (A then B then C), never as a monotone selected
version invariant. Manual rollback is explicit, pauses automatic selection, and
explicit resume is available. A retained ready release remains ready in this
space. No filesystem corruption is silently repaired by the model.

Breadth-first exploration deduplicates immutable states, with a 60,000-state cap
and 12 seconds per exploration; exceeding either raises an incomplete-check
error, never success. Edges and reverse edges are retained only within that cap
(finitely branching, two workers and fixed operations); there is no disk graph.
Unbounded looping is represented by revisiting states rather than increasing a
history counter. Crash can occur at each owned phase, including after sealing;
replay can occur repeatedly, but there is at most one transient crash per path.
Preparation/ready, pinned qualification, bootstrap/launcher and effect cuts are
coalesced where they leave the same abstract authority state. Real tests split
them below. Arbitrary corrupt evidence and permanent unavailability are outside
successful recovery assumptions, not successful outcomes.

Safety oracles inspect independent pointer, receipt, qualification masks and
owner bits (bad combinations are representable):

* at most one installation owner across competing effects;
* pointer/receipt predecessor and destination agree after each visible seal;
* changed current/previous pairs cannot authorize delayed replay; every changed
  selection records its actual causal predecessor;
* selected destinations were ready and passed the abstract qualification;
* rejection or failed B startup installs the publication barrier; rollback can
  decrease the selected identity without violating safety.

Selected and serving identities are separate. A current C with serving A during
drain is permitted. No running TUI is rewritten or globally migrated.

After full exploration, reverse reachability checks that **every reachable
prefix has some finite continuation** to selected **and serving C**, with no
installation owner held. Healthy A alone is not a goal. This is recovery
reachability (CTL-style AG EF), not a proof that every schedule eventually
upgrades. Fairness/availability assumptions are: eventual successful IO and task
start for C, a drain that eventually finishes, scheduling that permits recovery,
and explicit operator resume if manual rollback paused selection. Arbitrarily
repeated operator rollback, starvation, permanently busy work, absent tasks,
unavailable publications, permanent faults and a failing C do not imply eventual
upgrade. Weak fairness alone is not claimed to prove universal termination here.

## Model → current owner → real boundary tests

All owner functions are in `relay/native_product.py`; no runtime code or public
installation semantics are changed.

| Abstract operation/property | Existing owner | Executable boundary evidence |
| --- | --- | --- |
| acquire, competing effects, release | `exclusive`, `install`, `auto_update`, `restart_serve` | New fault test nested real OS-lock refusal and reacquisition after each exception; existing `test_product_recovery.test_automatic_recovery_holds_same_lock_against_manual` and `test_install_lock_excludes_another_process` |
| preparation and ready bit | `_install_locked`, `check_release` | New `test_one_shot_install_cut_replays_to_qualified_selected_healthy_target`: prepare/ready one-shot failures, retry incomplete unselected release; existing failed-pip and tamper tests in `test_native_product` |
| pinned trial | `qualify_transition`, `run_catalog_trial`, `_install_locked` | New trial/pinned-write cuts and pinned SHA check; existing `test_product_canary` shadow/canary rejection, incumbent INCONCLUSIVE, worker identity/cleanup and automatic rejected-barrier replay tests |
| pre-seal bootstrap/launcher | `_install_locked` copy/replace and launcher write | New bootstrap/launcher cuts; existing `test_launcher_binds_conversations_outside_version_directory` executes bootstrap with command runner mocked |
| seal and causal lineage | `_require_pair`, `write_activation`, `write_json` | New fsync/replace/ack-loss cuts, stale-pair refusal and deliberately broken boundary falsifiers; existing `test_atomic_activation_failure_preserves_pointer_bytes` and `test_activation_seals_pointer_and_receipt_in_one_replace` inspect update and rollback replacements |
| drain/stop/start | `_known_server`, `_move_serve` | New drain/stop failures attempt release of the same owner's lease; stop taking effect before lost acknowledgement is replayed safely. Start failure, including a start that took effect before acknowledgement loss, reaches explicit healthy rollback. Foreign/legacy/unavailable defer without control/start. Existing `test_native_product_update` covers busy deadline, malformed stop, foreign identity and selected-first replay |
| recovery/barrier | `_restart_locked`, `write_activation`, `latest_published`, `_auto_update_locked` | New start-failure barrier and repeated replay without oscillation; existing `test_product_recovery.test_A_also_fails_and_replay_never_oscillates`, manual pause/resume and rejected B allowing later C; existing `test_product_canary.test_auto_qualification_refusal_keeps_selection_and_bounds_failed_storage` |

New tests reuse `bundle`, `fake_install` and `server` from existing tests.
`fake_install` mocks wheel/environment preparation and returns PASS qualification;
therefore these fault tests do not re-prove dependency compatibility or the
catalog protocol. Existing canary tests own those checks. Real install, ready
validation, persistence, pointer sealing, restart and automatic rollback functions
execute against temporary homes. Real external dependencies, task start, health,
control and sleeping are mocked. The initial current durable history/cache bytes
are seeded and checked unchanged. The install-cut health observation is a mocked
identity-bound target, not evidence that a real new process started; separate
control-cut tests drive the real restart/rollback path using a task adapter.

OS lock crash release is modeled from the OS-lock contract, not from a new
process-killing experiment. New tests check exception release and actual lock
exclusion without spawning or killing processes. They do not prove power-loss
parent-directory durability: `write_json` flushes/fsyncs the file then replaces,
but does not promise a directory fsync. Bootstrap and launcher are not in the
active.json atomic transaction. Earlier failures can leave a new bootstrap with
the old selection; replay repairs it. No promise covers a partially written
launcher under arbitrary permanent IO failure.

## Negative controls and a small witness

The model independently mutates owner exclusion, stale-pair guarding, atomic
receipt sealing, B's rejection barrier, crash lock release, and availability of
any upgrade path. Each must fail an oracle or reverse reachability. BFS returns
a shortest prefix to the violation/unrecoverable prefix, not a full graph. An
empty prefix for the disabled-upgrade control is meaningful: healthy initial A
has no continuation to C. This specifically prevents vacuous “old is healthy”
liveness. A shortest atomic witness is:

```text
u0: acquire 1 → u0: prepare/ready → u0: pinned trial PASS → u0: atomic seal
```

At that cut the deliberately unsealed receipt still names A while the pointer
names B. The two new real-boundary negative controls independently remove
`_require_pair` or tear the persisted receipt via `write_json`, then verify that
the lineage/agreement oracle detects the damage in the actual activation path.
They are test-local sabotage, not product changes. No impossible-state datatype
is used to make the agreement check true by construction.

## Evidence and limits

The initial implementation verifier passed 21 tests; that result predates the
receipt-operation fidelity correction described below and is not its verification.

Final focused qualification passed **149 checks with one platform-specific skip**,
including **29 new model/fault cases** and the mapped existing product tests.
The current model explored **8,431 states and 18,456 edges**; all six negative
controls failed the expected safety or reachability check. Its retained JSON
summary is about 1.4 KiB. Exhausted state/time bounds raise an incomplete-check
error; they never become PASS. A persistent activation-storage failure also stays
unqualified across bounded caller retries and preserves the old pointer/data;
that case is not counted as successful automatic recovery.
The mapped existing product modules were rerun within this focused selection;
the whole OS suite was not run on the laptop. No new live rotation,
two-node observation or paid benchmark was performed. This qualification adds
recurring bounded checks after I2I-04/05, not a universal 100%-recovery claim.

### Receipt-operation fidelity correction

The abstract state retains the actual last receipt operation separately from
policy. A failed manually selected B cannot automatically fall back when its
receipt says rollback or auto_rollback, including after policy resume. Explicit
resume clears both paused and publication barrier, not the receipt. Another
explicit manual rollback is available even while paused. Recovery reachability
therefore permits an explicit operator rollback away from a bad manual target;
it does not claim automatic recovery from that choice. The model regression
`test_failed_manual_target_never_automatically_undoes_operator_receipt` checks the
C→bad B cut before and after resume. The real mocked-boundary regression
`test_failed_manual_target_preserves_operator_receipt_even_after_resume` maps it
to `_restart_locked`: failed task start raises while selection and receipt remain
byte-identical on repeated replay. These cases are included in the final focused
result above. No runtime behavior is changed.
