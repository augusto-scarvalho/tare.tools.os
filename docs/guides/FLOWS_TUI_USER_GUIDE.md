# TUI and flows user guide

Start with [TUI daily use](TUI_DAILY_USE.md) for the current English instructions:
launching, choosing a project, permissions, coding requests, corrections, resume
and reviewing changes. The earlier guide described a prototype and is superseded.

Ordinary conversation supports file tools and, when enabled, system commands.
Graphs and flows are optional. Use `/help` for the current command list.

- [Project permissions and instructions](TUI_PROJECT_PERMISSIONS.md)
- [One chat and its execution roles](CONVERSATION_ROLES.md)
- [Why a route cannot start](SCHEDULING_DIAGNOSTICS.md)
- [Attachments](NATIVE_ATTACHMENTS.md)
- [Ready Work dispatch and recovery](WORKFLOW_DISPATCH.md)
- Current delivery boundary and implementation history
- Deferred improvements

`/backlog` binds and inspects an explicit project graph. Planning can carry that
binding through review and Apply. Individual Work execution, preparation and recovery
use that graph through landing. Sequences still require the control graph; creating
a proposal is not execution admission. See the daily-use guide for the complete flow.

## Research synthesis

Choose **SYNTHESIS · reviewed flows** under **Optional flow**. Enter a concrete
question, select **Find saved input flows**, and add between two and eight saved
research or exploration flows from this project. **Review source positions** shows
their questions, attributed reports, source hashes, omissions and open questions.
Select **Start** after reviewing that selection. A changed question or source
requires another preview.

The synthesis uses one read-only native explorer session. It freezes the selected
positions and source text, stores one copy per source hash, and preserves the
original reports. Inputs are bounded to 2 MiB including metadata and source text;
each source is checked before loading it into that budget. The synthesis has a
ceiling of 16 model calls and 300 seconds, with no automatic model retry. It does
not start the original researchers again.

The resulting packet is advisory. Its authority is `NONE`; model agreement is not
independent fact verification. Original claims, disagreements, limitations and
unknowns remain visible. Selection for synthesis does not create a selection for
PLAN or a Round Table decision. Round Table policy and quorum remain with that owner.

Saved results live under `TARE_STORAGE_ROOT/research-syntheses`. Use **Sessions**
to reopen the demand, or enter its saved synthesis path and choose **Resume** or
**Review**. Completed and partial results reopen without further model calls or
rewriting the frozen sources. An unstarted draft also retains its question and
reviewed selection when the TUI closes; an empty conversation creates no record.
A failed save leaves the draft open.

### Qualification on 2026-10-18

The local campaign `flow16-multiflow-20261017` preserves two real public-TUI
cases: the documented GPU lease contract and the boundary
between advisory findings and governance. Both combined two independently saved
exploration flows using GPT-6.1 Sol through native Responses. Their outputs retained
explicit evidence gaps (`PARTIAL`), authorship and sources. Public warm resumes
made zero additional model calls and preserved native custody and snapshot hashes.
Research-URL inputs were also exercised with offline native-runner fixtures.

The second case identified acceptance of an observed but uncollected file alongside
a valid URL citation. A failing offline regression reproduced it; the research
adapter now accepts only collected page references and the checked manifest.

This qualifies the bounded adapter, not comparative savings or independent factual
verification. The original FLOW-16 proposal permits fixtures and two real cases for
that purpose. Its optional AB8 comparison was not run; comparative benefit remains
unknown. Runtime captures and the compact qualification record stay in the
operator's private campaign archive, as required by the repository's retention
policy. The record contains attributed findings, hashes, measured call counts
and replay results; this guide records the supported contract and its limits.
