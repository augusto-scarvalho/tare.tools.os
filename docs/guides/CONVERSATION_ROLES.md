# One chat, separate execution roles

New TUI conversations use a coordinator. The user describes an outcome in ordinary
language; the coordinator reads project facts, reuses findings and asks only about
consequential choices that the project cannot answer. No role selection or task
form is required for each message.

| Request | Execution |
| --- | --- |
| Explanation or progress | Answer; preserve any active demand. |
| Investigation | Read/search or delegate bounded exploration; report findings. |
| Run an existing configured check | Host verifier, in a private candidate; no implementation. |
| Plan only | Separate read-only planner; stop with the saved plan. |
| Deliver a change | Planner → implementer → host checks → independent auditor → apply. |
| Implement the previous plan | Continue the same demand and use its saved plan. |
| Fix an audit finding | Reuse the plan and rejected candidate while criteria still match. |
| Change the goal or criteria | Revise the demand and plan the changed scope. |

Exploration is available during discovery and correction. Existing findings are
passed as evidence; the coordinator and children inspect only missing or changed
facts. The coordinator has lookup and routing tools, not edit, shell or backlog
mutation tools. A small edit still gets a small separate plan and review.

A question may make up to eight direct project lookups per user message without
recording anything, so an answer reads the files it needs. Further lookups require
recording the requested outcome. Once a delivery or plan is recorded without a saved
plan, three lookups are enough: the next step goes to the separate planner. This allowance is counted from saved effects across
resume, without another classifier call. Recorded read-only investigations and
work with a saved plan retain source access. The host returns the missing
routing step; the user does not configure permissions or fill an intake form.

For one valid existing graph explicitly targeted by a planned Backlog check,
the private implementer receives the existing `backlog_read`/`backlog_record`
tools. Their owner maintains completion fields and the graph revision. This
temporary candidate binding changes neither the conversation's saved backlog
nor its edit grant. Invalid, new or ambiguous graph targets keep file tools
and the normal validation failures; they are not silently repaired or completed.

A completed inconclusive audit with no findings and explicit unread evidence
can continue once after a fresh human request, using the same frozen candidate
and auditor. This continuation runs no implementer, retains the first review
and charges, and shares the normal parent limits. Its result is cached even if
still inconclusive. Accepted or adverse decisions and unknown dispatches cannot
use this path. CI, application and publication remain subsequent host gates.

`/delegation` selects the separate planner for new chats. Without an explicit
planner choice, the host selects an available configured planner with at least
high reasoning. Explicit planner choices and provider-spending restrictions remain in
force. The implementer uses the existing eligible role routes; independent review
uses the existing frontier and producer-exclusion policy. If no independent
auditor is available, implementation waits before spending on the implementer.
Pro models are not selected automatically by this flow.

A conversation's selected connection can also be named for its separate
implementer when absent from the default ladder. Availability, billing and
temporary restrictions still apply; PRO remains exclusive to planning.

The planner supplies literal target files, criteria and relevant existing or new
tests. The host derives the executable unit from that artifact; the coordinator
cannot replace its scope inline. A plan cannot expand project edit permissions.
Plan-only intent cannot be promoted to delivery within the same user input,
including by opening another demand. A later user instruction can promote it
without another planning request.

Delegated planning uses at most six calls and 180 seconds within the parent's
remaining limits. Remote planners reserve time and the last call for delivery;
budget notices leave room for one local typed-proposal correction. A Git-ignored
output is checked before the child finishes, so a planner can correct the field
without losing its reads or renewing its budget. A repeated rejection or exhausted
allowance remains a failure.

After three failed planning requests, a fresh human continuation with an explicit
`--prefer_api` or `--prefer_subscription` transport change may authorize one
bounded recovery. The previous transport must be known and different; a valid
plan, applied delivery, unknown/running attempt or previous recovery blocks this
exception. Earlier calls and spend reservations still count against the same
parent limits. No preference change or automatic retry renews an allowance.

Implementation uses the existing private Git candidate, including the project's
uncommitted input. Ordinary folders use a temporary Git candidate bounded to
2,000 files / 32 MiB, excluding environments and caches; no `.git` is added to the
original folder. Select the specific project when a workspace exceeds that bound.
There is one project per candidate, with no multi-project atomicity claim.

The host runs declared checks before and after implementation. Test edits are
included in independent review. Planned Python test modules under `tests/` are included even if omitted from the
planner's separate test list. A missing promised test is a red baseline, and it
must exist and pass after implementation. Git-ignored new target files require
a corrected versioned plan path before a worker starts; the host never silently
omits them or changes ignore rules to force private outputs into the patch.
Only a passing, accepted candidate is applied; the source files and scoped
evidence must still match. Rejected, interrupted,
stale and stopped candidates stay pending. When command protection is selected,
both child commands and host checks use it; refusal never enables a raw fallback.
Without command protection, existing checks retain their ordinary execution
permissions. A working directory is not a command sandbox.
The sandbox denies loopback networking. Python's `socket.socketpair`, and with
it `asyncio`, still works there through an AF_UNIX pair tare provides; tests
that open real TCP or UDP servers on 127.0.0.1 still fail. An observed MXC
timeout creating a loopback socket pair (Python started with `-I`, `-E` or `-S`,
or a library that builds its own) is reported as inconclusive execution
compatibility, and so is a pytest check whose every failure is a connection
the sandbox denied (`ISOLATION_NETWORK_DENIED`); one other failure in it keeps
the check red. Inspect check scope or choose a compatible policy explicitly;
this never enables host networking or waives required tests. Like a timeout,
an inconclusive session check settles nothing: a change whose own checks pass
is then applied only when its unit's tests cover every changed file.

F3 includes the host-applied files, recorded candidate checks and their observed
freshness. Existing file/turn preview and explicit undo use the same hashed
snapshots. Undo never overwrites a later external edit or calls a model.

An auditor's acceptance requires an observed source reference. An operational or
format failure can be retried once on a later user turn, against the retained
candidate and the same frozen auditor profile, without another implementation.
Acceptance and rejection are not resampled. Unknown dispatches retain their
reservation and require reconciliation.

New/revised intent boundaries can use the already configured cheap Jev service.
One input's assessment is reused; ordinary tool calls do not invoke a classifier.
Disagreement requests clarification. Missing Jev retains coordinator interpretation
and the same host stage/permission gates; it never selects a frontier classifier.
New hosted intent assessments pin Jev 1.14.0 and distinguish versioned reports/data from chat-only investigation.
Named fields preserve the original request, recorded goal, pending question and recent user replies; folder-consent
annotations stay separate. An oversized complete request is not tail-truncated or sent to the classifier.
The recorded decision for an existing input is reused. Other Jev functions retain their configured model versions.

The compact demand record is `conversation-tasks.json` (64 KiB / 16 entries).
Completed non-current metadata can be evicted; the normal journals and child
artifacts retain history. There is no duplicate transcript store or new daemon.
Planner, implementer, explorer and auditor calls count against the conversation
budget. Explicit inference-spend limits share the remaining allowance; missing
usage retains reservations. `/usage` includes these child sessions once.

Conversation tools, prompt capsule, selected profiles and provider history stay
frozen across resumes. A plan-to-implementation continuation does not reset that
history or regenerate a plan to manufacture a cold comparison. Actual cache
discounts remain provider-reported evidence, not a guarantee inferred from reuse.
A host-proven change to a saved plan's cited read-only facts can admit one bounded
planning repair after a fresh human continuation. The previous plan stays attached
as advisory input. Charged calls and shared limits remain consumed; unchanged
facts, background retries and repeated repairs of the same source state do not
renew the planning allowance.
Existing conversations keep the tools and permissions they began with; start a
new chat for the coordinated flow. Formal `/plan` and `/work` admission, settlement
and governance remain with their existing owners.

Interactive host commands remain available through the operator's explicit
`/session` flow, which is a direct native session. Choosing that flow does not
give the assistant coordinator terminal or file-editing tools, and the workers it
starts never get the terminal either; only `/session` tasks and
`tare agent --terminal` do. That terminal is not sandboxed or reviewed: its commands run directly on this machine as the user,
with network access and the user's files and credentials in reach. An Action
review in `/permissions` runs commands in the sandbox instead. A local model never
gets the terminal; its session runs without it and says so.
