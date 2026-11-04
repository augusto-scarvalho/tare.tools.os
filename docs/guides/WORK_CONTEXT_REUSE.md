# Reusing reviewed exploration

Related Works can consume the same reviewed exploration without starting another
explorer. Each Work still owns its scope, observations, verifiers, acceptance and
settlement. A saved sequence provides explicit grouping; it does not create an
atomic transaction across repositories.

In the TUI:

1. Select **EXPLORE · independent questions**, describe the questions, and run the
   bounded exploration. The call/time limits apply to each native session.
2. Select **Preview findings**, review sources and remaining uncertainty, then
   **Attach reviewed findings**. Attaching consumes no model call. PLAN remains
   the initial destination for compatibility.
3. In **Optional flow**, select **Session**, **IMPLEMENT** or **SEQUENCE**. The
   **Reviewed findings (optional)** and **Findings SHA-256** fields retain the
   selection. Start the new task normally.

The equivalent public commands accept `--findings <manifest>` and
`--findings-sha256 <digest>` on `tare agent`, `tare run` and `tare sequence`.
Both values are required together. The existing `tare findings --kind parallel
--source <batch>` preview/save path supplies them. Research and session-plan
packets remain supported by their existing planning owner; they are not accepted
as current-source project exploration by this execution path.

Before a new dispatch, Tare checks the selected immutable manifest, its historical
snapshots and every cited current file. A changed or missing file omits its whole
report while retaining unrelated reports. Unsafe paths, links/junctions, changed
historical evidence, mismatched citations and oversized inputs refuse execution.
The saved run records reused reports and omitted paths. It never refreshes them
by calling a model. An empty eligible selection leaves ordinary direct reads
available.

The advisory projection includes attribution, review caveats, summaries,
uncertainties and source hashes, not source-file copies. Its total JSON budget is
32 KiB; each current source has a 2 MiB read ceiling. Existing native Work
protected-context budgets also apply. Oversized input is refused rather than
silently dropping obligations. No new store, service, periodic job, prewarming or
automatic retention copy is introduced; the existing reviewed findings manifest
owns its historical snapshots.

Resume keeps the original context, deployment, journal, cache identity and
remaining execution limits. It does not revalidate against files modified by the
Work itself or replace context from today's UI. A sequence validates findings
for each previously unstarted Work; an already prepared parallel implementation
keeps its own frozen proposal context. New saved formats refuse unsupported older
readers, rather than silently losing these constraints after rollback.

For native `openai-codex-responses`, a fresh related session with eligible reports
gets an opaque cache key scoped by project, advisory prefix, role, native profile
and selected account. Explicit operator keys take precedence. Other connections
retain their existing behavior. Cross-provider reuse transfers factual advisory
data; provider KV caches are not transferred. The prefix precedes the direct
task, while Work criteria and current read-before-edit checks remain separate.

A stable key and identical prefix are eligibility, not a hit or a discount.
Use returned per-call cache counters and keep unreported values unknown. Do not
substitute prompt byte equality, fixtures or configured tariffs for actual cache
usage or subscription quota accounting.

The 2026-10-20 account-route pilot returned zero cached input tokens in both
fresh related sessions (2,448 and 2,454 input tokens). A separate bounded native
adapter probe with an explicit content breakpoint received HTTP 400 before any
completed response; its error did not identify the rejected field. That candidate
was not enabled or integrated, and no request was retried after stripping it.
The [official API guide](https://developers.openai.com/api/docs/guides/prompt-caching)
explains why a shared prefix without an eligible cache boundary may miss. Those
API features are not presumed compatible with the Codex subscription route.
Cross-session provider cache savings remain unproven; reuse of the reviewed
exploration is delivered independently.

Independent code audit remains available through the existing TUI auditor
selection / `tare run --auditor-deployment auto` (or an explicit registered auditor). Existing
routing requires distinct model and bias domain; vendor diversity is recorded
separately. This change does not pass explorer reasoning into the independent
code auditor or turn reviewed findings into acceptance evidence. Cache affinity
also separates roles.

Maintained qualification: [WORK_CONTEXT_REUSE_QUALIFICATION.json](WORK_CONTEXT_REUSE_QUALIFICATION.json).
Offline tests cover real session/Work/sequence/preparation paths, stale-report
omission, effective cache-profile inspection, exact resume, malformed evidence
and public CLI limits. The bounded live TUI record is descriptive; it does not
establish representative cost or delivery-quality savings.
