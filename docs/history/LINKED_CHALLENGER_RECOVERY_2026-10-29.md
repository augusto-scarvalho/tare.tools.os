# Linked challenger stop and preserved-candidate recovery

The public ConPTY TUI delivered the linked-challenger stop correction through a separate planner, implementer,
host checks and independent review. Cancelling or interrupting the primary publishes its existing cooperative
stop signal before reference publication. Normal completion leaves the trial running. Duplicate notifications,
existing results, snapshot/stop failures and dispatch boundaries have offline regression coverage. Saved results
remain evidence; a stop request does not prove that an in-flight provider call ended instantly.

The initial Sol 6.1 high implementation used 22 model calls. Its first Opus 5.5 high audit found no code, test or
documentation defect but was inconclusive: the packet omitted the planner's execution selection and the task
mixed functional acceptance with an unfinished FLOW-19 comparison. The candidate stayed unapplied. The planner
then separated the still-open measurement from the functional deliverable. The accepted recovery used zero new
implementer calls, retained the same patch/producer, reran host checks, and obtained an independent Opus 5.5 high
ACCEPT before the TUI applied exactly the four planned files. Planner and implementer were Sol 6.1 high through
OpenAI API, with PRO disabled. The prior inconclusive review remains part of the cost and failure history.

The dogfood exposed three host integration defects:

- A `delegated_leaf` guard excluded ordinary delegated implementers from optional challenger admission. They
  now reach the existing trial path; role/project admission, the trial child marker and finite budgets remain.
- Review packets omitted the planner. They now derive its model, requested reasoning and PRO setting from the
  completed host custody bound to the plan artifact hash. Invalid, missing or unrelated custody stays unknown.
  This records host configuration, not provider attestation of internal reasoning, and exports no credentials.
- A criteria revision retained the old candidate as `prior_delivery` but execution ignored it. Recovery now
  reuses only its unverified patch. Changed outputs are refused. Changed read dependencies require matching
  sources in the new plan and are disclosed to the auditor. No prior approval or check result becomes current.

Host mistakes are recorded separately: one premature interruption happened during valid preflight checks and
used zero implementer calls. Later, custom pytest scratch folders containing nested fixture repositories polluted
snapshot admission; those folders were moved out of the project and the scratch naming pattern is now ignored.
An attempted replacement implementer was stopped before new model calls. None of these attempts is a valid pair.

The delegated trial regression failed before the admission repair and passed after it. Planner custody tests
cover missing, tampered, unrelated and unfinished evidence. Candidate recovery tests cover actual review-only
dispatch, changed outputs, missing/wrong proof and removed dependencies. They use offline fixtures; no provider
calls, operator credentials, model downloads or shared GPU work are part of those tests.

FLOW-19 remains unqualified by this functional delivery. No challenger ran in these attempts, and no paired cost
or quality winner is claimed. A first controlled repetition from the original source baseline is a separate
measurement. Absent prices, normalized usage or subscription opportunity costs remain unknown, not zero. The
broader FLOW-04 and X5 economic qualification are not closed by this repair.

Final local validation: 302 offline tests passed, including the challenger, delegated admission, planner custody,
candidate recovery, conversation review, native session and staged disposition-manifest checks. This is local
Windows evidence; CI, merge and product publication are separately observed gates.
