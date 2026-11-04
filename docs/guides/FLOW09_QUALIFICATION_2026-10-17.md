# FLOW-09 qualification, 2026-10-17

The bounded delivery represents the current static Work recipe as inspectable
data and adapts it to its existing owner. It does not establish a universal
workflow language or ratify the wider proposed SPEC.

## Evidence

- Pure compiler: **337 Windows tests passed**. This includes 12 named
  representative plans and 200 deterministic seeded DAGs. Each seeded case
  checks preserved node identities, contracts, obligations, effects and phase
  order, then injects both a cycle and an absent dependency and compares the
  rejection with the frozen FLOW-10 IR owner.
- Integration: real fixture repositories exercise existing verification,
  admission and landing. A legal recipe cannot make an invalid implementation
  pass. Invalid recipes are rejected before Work custody or an agent callback.
- Recovery: saved recipe data/hash are retained without recompiling or repeating
  model calls; historical origins without this metadata keep their original
  interpretation; changed saved data are refused before landing.
- Public TUI: a GPT-6.1 Sol native Responses conversation implemented the three
  scoped compiler/schema/test files. F3 displayed its recorded edits. An
  independent terminal replay selected IMPLEMENT, entered the real FLOW-09 ID,
  and inspected all five phases, obligations, effects and admission requirement.
  Inspection created **zero native sessions and zero model calls**.
- TUI regression checks cover manual ID inspection and literal Windows paths at
  80×24 and 150×52. They verify Enter does not dispatch Work.
- Targeted owner regression: **232 passed, 1 skipped** across integration,
  Work execution/recovery, FLOW-10 IR/reducer, Work console, TUI and disposition
  integrity checks. Together with the pure compiler, **569 distinct tests passed**.
  The single skip checks a POSIX executable-bit change, unavailable on Windows.

The initial manual-ID route did not trigger inspection. The fix handles Enter
on that field explicitly and names its read-only behavior in the interface;
the same public terminal route was replayed successfully. Failed test setup
attempts and their corrected replays remain in the local JUnit evidence.

## Cost and custody

The implementation conversation used **20 calls in 162.2 seconds**, within its
20-call/600-second ceiling. Its existing history and native Responses session
were retained. Provider receipts report 427,033 input and 9,907 output tokens,
including 689 reasoning tokens, and zero cache-read/write tokens. No cache
savings, monetary price, independent review or paid benchmark is inferred.

The raw evidence of this run is not published: public terminal frames and
their hashes, `public-tui-result.json`, `implementation-cost-summary.json`,
JUnit reports and the ordinary owner session. Each terminal trace had a 12 MiB
ceiling; there is no new collector or retained project snapshot. The versioned
contract, tests, guide and this record are the durable destination for findings.

## Integration boundary

The source worktree is based on FLOW-10 PR #131. Its Linux CI completed 3,954
regression tests; Windows reached the former 55-minute job deadline after 3,790
passes, without an assertion failure recorded. The parent PR corrects that
bounded job window to 75 minutes, retaining all tests, with a local monitor for
the new execution. This is not a Windows CI pass.

The FLOW-09 PR must also pass its own CI before protected-main integration.
Local Windows results and controlled TUI replay support evidence grade C;
independent ratification, Linux qualification of this change and product-release
installation are not asserted by this record. The ordinary local installation
was not replaced for dogfood.

## Reconciliation with current main, 2026-11-01

PR #133 was still unmerged: the compiler and recipe schema were absent from
`main` at `1959e445`. Its original implementation is retained and reconciled
with OS 0.1.132, Kernel 0.9.88 and Runtime 0.14.7. Shared Work context and cache
metadata, current literal rendering and graph-free public CLI commands are
preserved; the closed descriptor and FLOW-10 owners are unchanged.

Directed revalidation covers the compiler, real owner integration/recovery,
Enter inspection at both terminal sizes, Work selection and paired-runtime CLI
startup. Initial failures in the regenerated manifest membership and a CLI test
fixture that dropped its paired dependencies were corrected and rechecked.
Raw local receipts are under `pr-reconciliation-20261101`; they retain earlier
failures. No paid model rerun was needed for this integration. Fresh Windows
and Linux CI remain required before merging this revision; the old CI result
does not qualify the reconciled tree.
