# Saved Sonnet delivery recovery — 2026-10-30

The public TUI recovered task `task-37c8b518080c` in its original saved conversation. The final host receipt records `VERIFIED`, independent `ACCEPT`, `applied: true`, and the demand projection records `DELIVERED`. This delivers the bounded executed-route telemetry change, not the broad FLOW-19 comparative cohort.

## Roles and usage

- Planning and scope repair: Claude Opus 5.5 **high**, Claude CLI. Saved technical work and criteria were reused.
- Implementation: Claude Sonnet 5.5 **medium**, Claude Messages API, adaptive thinking with `output_config.effort=medium`.
- Independent audit: GPT-6.1 Sol **high**, Codex CLI. Its real findings drove corrections to executor identity and the persisted plan-auditor fallback. It accepted the final candidate after the operational evidence gap was resolved.
- Recovery: public TUI keyboard/mouse actions. `/recheck` runs checks and audit with **zero implementer or coordinator calls**. `/retry_delivery` deliberately runs the implementer for a concrete correction. `/repair_plan` preserves the candidate while repairing the executable scope.

Three metered worker invocations settled 12, 5 and 7 calls for USD **0.2554900**, **0.0738769**, and **0.1050047**: USD **0.4343716** against the operator's aggregate USD **7** authorization. Subsequent profiles deducted previously settled usage rather than renewing that authorization. No reservations remained unresolved. Recorded totals: 96 uncached input tokens, 511,271 cache-read tokens, 87,861 cache-write tokens and 16,340 output tokens. These are provider-reported usage valued at the recorded tariff, not an invoice. Subscription CLI usage is separate; unknown historical usage stays unknown.

The final audit packet projected 20 existing journals, 133 model requests, and conservative shared accounting of 33 coordinator requests plus 108 child calls committed or reserved. This was within the explicit cumulative 200-call grant. Active time was 3,810.49 of 7,200 seconds. Original 30-call/900-second limits remain recorded as history; grants did not reset consumption. Across those journals, 1,320,395 reported cache-read tokens and 1,219,381 cache-write tokens were observed; ten requests lacked complete token observations. No claim of a cache hit for every request is made.

## Root causes corrected

1. Host subprocess checks inherited frontend profile/approval variables while worker checks used an isolated environment. Both now use the existing environment boundary; protected check ports retain ownership of their own environment.
2. Explicit shared call grants reached OS accounting but not the runtime continuation ceiling. Runtime 0.14.5 carries finite monotonic grants without changing tactical identity, prior calls, cancellation or uncertain invocations.
3. Review writers and readers disagreed on receipt size. They now share the same 1 MiB bound. Completed receipts replay without another inference call.
4. Reviews saw the latest continuation but lacked earlier API authorization and cumulative operational evidence. The host supplies bounded authenticated instructions, current controls, metered worker outcomes, and a compact projection from existing hash-chained journals and usage reports. Private prompts and reasoning are not copied. Missing evidence remains unknown.
5. A completed planner artifact was rejected because its inline handoff exceeded 16 KiB. The canonical 64 KiB artifact remains intact; large handoffs return its digest and location. Legacy recovery checks the original recorded model delivery and current cited sources. Failed planning no longer appears as a successful repair.
6. The OS plan omitted the generated manifest required by its own criteria. Admission now requests a scope repair before execution. The project-owned checker regenerates metadata and stages only the admitted relay modules and manifest, using the existing focused test runner.
7. Existing host source edits left unrelated generated fingerprints stale. Refresh happens in the private baseline and is disclosed to the auditor; authored manifest changes cannot be discarded. Post-check derived edits are captured without changing the attested index scope. Application compares with the original project baseline and rejects concurrent source changes.

Kernel 0.9.84 (PR [91](https://github.com/augusto-scarvalho/tare.tools.kernel/pull/91)) also preserves the Anthropic history cache boundary ahead of changing budget reminders and adds bounded Claude CLI repair diagnostics. Runtime 0.14.5 (PR [11](https://github.com/augusto-scarvalho/tare.tools.agent-runtime/pull/11)) owns call-grant replay. Both owner PRs and their main CI passed before the OS pins moved. Real Sonnet CLI implementer protocol reliability remains unqualified; this delivery used the explicitly authorized API route.

## Evidence and boundaries

The raw evidence of this run is not published; it includes the final receipt of unit `task-37c8b518080c-4c49a0` and the independent review `2f6f261cba7cc242263ae5a2`. Focused test receipts include explicit exit codes and scratch cleanup. The repository CI supplies Linux/Windows regression evidence for the final integration revision.

Executed-route records describe deterministic choices and observed fallbacks. OPE is unsupported; non-executed models have no observed performance or savings. Broad FLOW-19 cohort qualification, sustained Sonnet CLI reliability, CI, merge and managed installation are distinct gates and are not inferred from the TUI's delivery receipt.
