# Delegated planning

New TUI chats always use a separate planner for changes. Open `/delegation`,
choose **Choose planner model…**, and save. This selects a planner for new native conversations
without changing their coordinator model. To use a different provider or
allowance, also enable **Allow delegated planners and implementers to use other
providers or allowances**. Existing conversations keep their saved tools/routes.
The existing `/routes` planner list remains the ordered fallback configuration;
the model picker uses that same role configuration for new planning flows.

Open **Review settings…** here, or `/routes`, to choose **Automatic · another
provider, frontier models**. Formal PLAN reviews, automatic Work audits and code
turn audits share the same role admission and independence checks. The planner
is the producer of a plan; the implementer is the producer of a code change.
Changing the planner does not make that model eligible to independently audit its own work.
The preview names the current plan reviewer; live eligibility is checked at
dispatch. Delegated planning advice continues to be assessed by the coordinator;
this setting chooses the auditor for existing reviews and adds no review stage.

The automatic pool admits Sol 6.1, Astra 6, Opus 5.5, Fable 5.1 and Gemini 4 Argon,
using the installed Codex/Claude/AGY connections or enabled native HTTP APIs.
Another model vendor comes first, then subscription allowance before metered API,
then Sol before Astra and Opus before Fable within the same allowance class.
Codex Direct precedes its CLI alias to preserve the native cache route. Pro API
modes, small helpers and older models are not automatic frontier reviewers.
Unavailable credentials, invalid local Codex login, disabled connections and known
quota exhaustion exclude a route. An API key does not prove remote authentication
or a known balance; missing cost remains unknown. The selection itself uses no LLM.

**Use my auditor routes** retains an explicit list and the independence gate.
Previously saved auditor lists stay manual until their owner chooses automatic
review. An independently registered lineage of the same vendor remains a fallback
when no eligible different vendor is available; the receipt and preview identify
that case. Another vendor is a diversity policy, not proof of unbiased judgment.
Saved formal-plan reviews and their retries retain the original exact profile;
changing settings never resamples an accepted or rejected verdict.

The default is a preference when an independent reviewer is available. If none
is eligible, PLAN retains its existing separate-session planner review and marks
it as **not independently attested**. It never promotes that fallback to an
independent audit. Choose **Require an independent plan reviewer** (CLI:
`--reviewer-deployment auto`) to block before spending when no eligible route
exists. Canonical Work admission gates remain unchanged; a code-turn review with
no independent candidate remains explicitly unaudited.
New [coordinated chats](CONVERSATION_ROLES.md) require an independent auditor
before applying an implementation candidate; they do not use that legacy fallback.

The coordinator can call `delegate_plan` with a specific `instruction`, optional
bounded `context` containing findings already known, a configured `route`, and
optional `effort`. A small change uses a small separate plan in the coordinated
chat; the legacy optional-delegation switch applies to direct sessions. The child uses the
existing planner role, native session and observed-source artifact validator.
It may read/list/search existing project roots; it cannot edit, run commands,
delegate, admit Works or apply the returned advice. The coordinator reviews the
handoff and continues in its original conversation. Its prompt/history is not
rewritten to pass the planning mission.
Effort overrides use the model picker's existing connection mappings; an
unsupported level is refused before inference. New planning leaves distinguish
files actually read from searched or proposed targets in their evidence list.

Each request allows up to six model calls and 180 seconds, cut to the parent's
remaining budget while reserving two coordinator calls. A session can request
planning at most three times (per demand in coordinated chats). Child calls also constrain later exploration and
implementation. An explicit parent spend cap requires matching child tariff
evidence; unfinished or unpriced calls keep their spend owner's reservation.
Local planners use the existing local slot and GPU availability checks.

Custody is `planning/<effect digest>/`: dispatch intent precedes inference,
and the child retains its normal journal, raw usage and artifact. Replaying a
completed effect returns that handoff; interruption never silently repeats a
potentially charged call. The returned artifact is advice with `authority: NONE`.
`/usage` includes coordinator, exploration, planning and implementer unit
sessions, deduplicated by their existing session/call identities. Missing prices
and counters stay unknown.
The TUI includes planning-only and implementer children even without an explorer.
Incomplete provider replies retain their stop reason; a missing older reason is
unknown and never permission to repeat a possibly charged request automatically.

Compare equivalent planning demands using the same source revision, coordinator
model/effort, tools and limits. Count the entire journey including child calls,
cache reads/writes, failed attempts and coordinator synthesis. Rotate execution
order, retain warm-cache follow-ups, and assess source accuracy, concrete steps,
dependencies, checks and open risks. A bounded pilot describes these tasks; it
does not establish a universal quality or cost ranking. Paid trials are separate
operator-authorized runs; ordinary repository tests stay offline.
Keep dated results and raw receipts in a bounded private lot with a review date
and a concise report as their destination. When cache detail is unreported,
present a token-price interval rather than filling in zero. The price of the
same tokens without cache discounts can be calculated without making cold calls.
