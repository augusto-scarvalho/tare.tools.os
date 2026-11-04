# Review a bounded planning horizon

The supported contract remains an explicit scheduling review or bounded PLAN
proposal, followed by the Backlog owner's validation and revision comparison.
Preview never dispatches a Work or authorizes execution. A changed base requires
a new preview; completed results and existing dependency evidence are retained.

The scheduling preview now identifies the graph, base revision and proposal,
and presents priorities, prerequisites and blockers as separate readable rows.
It is rendered as selectable plain text: treating the owner's diff as Markdown
had collapsed those rows into a paragraph in the real terminal.

## Collection without another service

The existing demand checkpoint records `order_previewed`, its proposal digest,
base revision and selected IDs. A confirmed mutation's existing request receipt
includes `review_observation`; clearing a reviewed draft records a `withdrawn`
observation in the existing checkpoint. No graph snapshot, model prompt, new
database or recurring inference is added for this measurement.

`elapsed_ms` measures console preview readiness to the confirmation/withdrawal
action using a monotonic clock. UTC boundaries identify the observation. A new
preview starts a new interval; reopening a demand requires another preview.
An absent start remains unknown. The interval includes waiting and does not
measure attention or establish that a human operated the console. The default
actor is `unattributed`; a dogfood report must identify its automated actions.

For a small cohort, count distinct proposal digests and reconcile confirmed
requests with successful owner receipts before counting applied changes. An
unconfirmed request is not an accepted change. Withdrawn, superseded and still
pending proposals remain distinct. A confirmation establishes acceptance, while
usefulness still needs the operator's reason and review of the resulting order.
Never infer a favorable usefulness rate or a zero human cost from missing data.
The destination is the existing Work qualification record linked to its bounded
report, rather than an unattended stream of benchmark files.

## A small real cohort

Use existing obligations and a reason grounded in the current product. Load each
Work's scheduling, stage the intended priority/prerequisites, explain the change
and preview the set. Inspect the proposal before confirming. A priority change
made only to improve a benchmark score is not useful planning evidence.

Keep three useful real changes, their reasons, decisions and owner receipts in
one Work qualification record before considering a wider planning horizon.
Fixtures and proposals awaiting a decision are not delivered horizon changes.
This series qualifies a future expansion; it does not require reprioritizing
unrelated Works to deliver the existing bounded review/apply contract.

## Validation and remaining qualification

Offline owner and TUI checks cover valid application, cycle, missing reference,
duplicate prerequisite, invalid scope, stale graph/proposal, existing execution,
locks and unchanged completed Work. UI checks cover selectable diff rows,
timing after a fresh preview, withdrawal without mutation and lost-confirmation
recovery without replay. Windows public-console dogfood uses actual ConPTY keys;
no model inference is needed for scheduling changes.

The bounded delivery requires a reviewable patch, validation/review of that exact
proposal, explicit confirmation, owner revision checks and a recoverable receipt.
After a refused confirmation, the draft remains and Confirm stays disabled until
a fresh preview succeeds. Repeated clicks cannot submit the refused preview.

A complete three-change series and human decision-time observation remain
unknown until ordinary use establishes them. They are a qualification gate for
expanding the horizon, rather than unfinished implementation of the bounded
contract. Fixtures establish correctness, not usefulness or human attention.
Windows-only dogfood does not establish POSIX qualification; use the required CI
checks for that result.
