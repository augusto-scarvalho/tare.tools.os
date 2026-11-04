# Work pause and human reconsideration — FLOW-17

In Work mode, **Pause Work** remains available during execution and targets the
active Work, not an edited selection. **Inspect controls** reads saved state,
including after reopening. Opening or reconnecting never resumes execution.
The Pause button targets a Work running in this TUI. An idle attempt must be
resumed before that button can request another pause.

- “Pause requested. Finishing the current round.” means the host has not yet
  acknowledged a safe stop. Do not assume the runner has stopped.
- “Work paused. You can close this TUI.” means the host has completed the
  current round and durably recorded the pause under Work custody.
- **Resume** inspects current state, saves explicit resume authorization if
  paused, then starts the existing resume path only after that write succeeds
  and the selection remains unchanged. A saved authorization is consumed once.

The native host pauses at a complete-round boundary, not midway through a tool
call. Paused state needs no live TUI session. Resume preserves the original
model-call usage and remaining active-time budget; time waiting while paused
is not active execution. It does not reset limits. An originally unlimited
execution remains unlimited; a finite budget cannot become unlimited.
The saved resume authorization starts that remaining clock once. Retrying the
same authorization cannot renew it. A pause cannot undo effects already applied.
Recovery finishes the bookkeeping of an already accepted Git landing without
running the agent or applying the candidate again.

While safely paused with an unfinished agent, inspect controls, enter a note
and select **Save note**. Notes must be nonempty and at most 8192 UTF-8 bytes.
“Note saved for the next agent round.” attests storage, not agent understanding
or delivery. Resume inserts the pending note through the existing journal with
a stable, deduplicated message ID. A note grants no authority or extra scope.
An unconsumed note cannot be replaced. At stage boundaries no agent can receive
a note. The TUI hides that field; the owner CLI refuses undeliverable notes.

The owner CLI is `work-control WORK-ID [inspect|pause|resume|intervene]
--graph PATH --repository-root ROOT`. Mutations require `--expected TOKEN`
from the latest inspection; intervention also requires `--note TEXT`.
Inspection writes nothing. An absent control record means synthetic RUNNING,
not proof of a live process. Identity binds graph, repository, Work, run,
descriptor and target. Terminal or moved identities refuse. Repeating the same
last command/token/payload returns its saved result; changed or old commands
refuse. Console completion receipts recover observations, never replay writes.

For a declined human decision, open the saved decision and select
**Reconsider decision**. “Decision reopened. Review and answer again.” means a
new request digest exists. This neither approves nor starts Work. Approve and
Decline are available only for a pending decision. Old approval tokens remain
invalid; changed selections must be opened again. The equivalent CLI is
`decision WORK-ID --decision reconsider --expected CURRENT_REQUEST_SHA`.

Focused UI tests live in `tests/test_console_work_control.py`. Host pause and
recovery tests live in `tests/test_native_work_pause.py`.
