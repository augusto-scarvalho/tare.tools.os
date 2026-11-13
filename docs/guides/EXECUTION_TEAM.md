# See who performed the work

Open **Team** beside **Sessions**, or enter `/team`. The view reads the selected
conversation's saved executions and refreshes every five seconds while open.
It makes no model calls and changes no project or execution files.

Each participant shows its role, requested model, requested reasoning level,
connection and latest recorded state. Select a row for its mission, exact model
ID and route. Coordinator, planner, explorer, implementer and auditor are separate
roles even when two of them use the same model. Only roles with recorded execution
metadata are listed; configured preferences do not establish who ran.

New executions also appear directly in the conversation as one compact activity
row per invocation. A **Result ready** row means a model returned a proposal. The
implementation and audit owners still determine application and acceptance. A
failed or inconclusive audit never becomes a successful delivery through this view.

Old conversations retain their actual settings. Missing reasoning is **Not
recorded**, and a historical planner that used low or medium is marked **Below
minimum high**. Reading Team does not regenerate the plan or rewrite its history.
Candidate reuse does not create a fictitious new implementer execution.

New planning requires **high** reasoning or greater. The planner picker offers
capable connections at that level. Delegated planning validates the same minimum
before dispatch, including requests made through an older saved tool schema. With
no explicit level, a planner takes the lowest level its connection registers at or
above high: the local models register low, medium and xhigh, so they plan at
xhigh (only in `--local_only` mode, where they may plan at all). A
saved planner below that minimum cannot resume with low reasoning; a new planner
can use its retained findings without changing the earlier conversation. The
coordinator, explorer, implementer and auditor retain their own settings. Pro mode
is not enabled by the reasoning floor.

In `--local_only` mode an audit by the implementer's own model family is recorded
with the label **same-family audit, not independent**, in the review, its receipt
and the delivery result, and the change is applied only after you confirm it. If
that audit did not accept and you applied the change anyway, the delivery records
it as your decision (`user_decision`), with the audit's own verdict and open items.

On Claude API connections, the level is sent through `output_config.effort`,
independently of the thinking configuration, as defined in the
[Claude effort documentation](https://platform.claude.com/docs/en/build-with-claude/effort).
Custom endpoints do not inherit provider capabilities from a similar URL.
