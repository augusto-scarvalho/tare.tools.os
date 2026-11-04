# Graph audit: reconciliation, trace and complete differences

Revalidated on 2026-10-23 against the current Backlog Graph owner and OS graph.
This delivery qualifies the read-only audit and defines the prerequisite for a
future cutover. It does not perform or authorize an operational cutover.

## Delivered contract

- Complete differences retain unknown and nested fields, provenance and original
  outcomes. Ambiguous list IDs cannot hide changes. Explicit field exclusions
  are disclosed as filtered output, never as a complete comparison.
- Reconciliation reports canonical system, ID and revision, observation/run
  metadata, completion and declared staleness, with separate planning and
  operational readiness reasons. Closed nodes remain in history and are excluded
  from pending operational divergences.
- Trace preserves the complete node, declared sources, all claim IDs and original
  evidence. Missing links remain explicit; source metadata does not establish
  content, anchor or execution verification.
- The offline `shadow-parity` gate requires validated canonical/shadow snapshot
  pairs, at least two strictly increasing timezone-aware observations spanning a
  caller-declared positive finite minimum, and zero complete semantic changes.
  No ignored fields are accepted. Capture cadence and duration must be chosen
  before collection. The gate cannot establish continuous coverage or capture
  authenticity, and always retains `authority: NONE` and `cutover_performed: false`.

The owner implementation and public CLI contract are in
[Backlog Graph 1.3.1](https://github.com/augusto-scarvalho/tare.tools.backlog-graph),
including `docs/CLI_REFERENCE.md` and the validation, CLI, diff and mutation-runner
regression tests. OS consumes the owner through its committed satellite pin.

[Owner PR #6](https://github.com/augusto-scarvalho/tare.tools.backlog-graph/pull/6)
passed its required CI and merged as
`074692bb5b40ec1b388e5948b14d8786d33aa3c0`; its tree matches the locally checked
candidate above. Consumer CI and managed activation are checked separately.

## Observed validation

The final Windows run passed **36 targeted tests**, including the required
mutation canaries, with reader-thread warnings treated as errors. Public TUI
dogfood produced the implementation and corrected the new test's collection
error. A subsequent TUI repair made child-process output consistently UTF-8 and
preserved PASS/KILLED/ERROR/TIMEOUT distinctions; it passed the Unicode and
classification controls before the final combined run.

A read-only comparison against the prior owner implementation used the current
236-node OS graph, with SHA-256
`03e098fa48e1cc2022f2d105e5e098e22112f3b3338b1b3cbec88874c4a511b4`:

| Control | Previous implementation | Current implementation |
| --- | ---: | ---: |
| Closed nodes incorrectly included as pending divergences | 105 | 0 |
| Total declared operational divergences | 131 | 26 |
| Reported differences for a changed duplicate-ID list | 0 | 1 |

The graph's bytes were unchanged. The target trace retained the node, all five
source references and all original claims; three unbound claim links remained
visible as gaps. The remaining 26 divergences describe declared active
impediments, not newly suggested tasks or verified external execution failures.

No real operational parity window was collected. The implementation and negative
fixtures qualify the checker and its prerequisite definition; a real captured
window remains required before any future cutover. Normal owner/consumer CI and
managed activation remain the integration gates.

Private raw terminal and run records have a 16 MiB ceiling and a retention review
on 2026-11-22. Their durable destination is this compact report and the owner
regressions; private histories and transcripts are not shipped with the product.
