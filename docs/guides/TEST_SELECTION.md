# Test selection: usage and limits

## SpecGraph X5

SpecGraph implements `recommend_from_receipt` through a trusted Python composition in the executor (`c5_evidence`, `c5_current` and `attestation_verifier`). The proof binds the hashes of the capture, project, target and changes with lines, the selected union and the mandatory tests. Before selecting, it revalidates the snapshot, dependencies, configuration, environment and artifacts. The union includes trace, impact and mandatory tests.

The executor's verifier must observe the real Kernel receipt and the artifacts and validate the subject and state. Booleans supplied by the model do not authorize a selection. Without proof, or when something diverges, the result is `FULL_SUITE`; the CLI keeps the full suite by default.

A pure fixture demonstrates a reduced selection that preserves the mandatory tests and is invalidated when the target or an artifact changes. This proves the mechanism, not its adoption in general projects.

## Pilot in the existing verifier

The pilot starts in observation: it computes candidates before a requested check
and runs the full configured command once. The analysis uses the project's
Python; it does not query models or start tests to fill in metrics.
`applicability: CANDIDATE` is a structural recommendation for an experiment,
with unknown transitive dependencies and no authorization to execute or
approval of the target. The C5 gate above remains separate.

Choose a validation command you would already use in the project. For example:

```powershell
tare test-selection start --root $PWD.Path -- python -m pytest tests/test_orders.py -q
```

The command prepares the pilot, without running tests, and returns `wrapper_argv`.
In the TUI, open **Settings → Execution options**, paste this array into
**Verification argv for a new conversation** and start a new conversation.
The check stays explicit: the agent uses the named verifier and manual
review runs the same wrapper. Conversations already saved keep their commands.
Terminal commands that do not use this wrapper are left out of the collection.

Mandatory tests can be declared with `--mandatory tests/test_security.py::test_access`
before `--`. The full comparison scope is the configured command,
not a claim about every test in the repository.

**--mode canary** is an opt-in comparator: after the initial capture, it runs the
proposed subset and the full audit of the same target. It tries the subset only
when the subset is smaller and the arguments can be preserved safely. A
failure in the candidate ends the trial before the audit, without declaring the full
check passed. The cost of both runs is observed; the comparison is not an
operational saving and does not enable selection on its own.

## Where the data goes and how long it stays

There is one current pilot per operator storage, in
`<tare storage>/test-selection`. The total ceiling is **4 MiB**, counting temporary
files and atomic writes. The capture is at most **1 MiB**; each
compact observation, **16 KiB**. No stdout, transcripts or project files
are copied into the summaries.

In the initial capture and in later ones, an incomplete capture is still refused.
`HOOK_INCOMPLETE` and, when present, the producer's known reason are separate
codes in stderr and in the compact observation; they remain in the diagnostics of
`summarize`, in `reviews.json` and in the `keep-full` review, even after the
capture is discarded. Only the singular `reason` field, with the literal string `TEXT_LIMIT`,
`BYTE_LIMIT`, `NODEID_LIMIT`, `UNKNOWN_NODEID`, `INVALID_DURATION` or `PAIR_LIMIT`,
adds a reason. A missing value, null, unknown values, malformed types and arbitrary
text are not propagated. The codes follow `[A-Z_]{1,64}`, up to 16 per
observation; the union across observations does not get a new global ceiling. This does not
enable selection or change the limits, the safe fallback, the full configured
command or its exit status.

Collection ends after **five verifications or seven days**, whichever comes first.
That limit is a budget, not a quantity required for qualification.
A review can happen at any time. A confirmed omission, an incomplete
capture or an invalidation stops the instrumentation; the next checks
stay full while the data review is pending. The known result
of the check is preserved. A run with an unknown result requires
inspection and review before the command is repeated.

`/usage` shows the pilot's project, destination, deadline, observed costs, limits and next
action, even after switching projects. The same review is
available through the CLI:

```text
tare test-selection status
tare test-selection review --decision keep-full
```

When the pilot ends, the executor saves the summary and discards the bulky capture; it keeps
at most eight decision summaries, within the ceiling. Expiration is checked when
the pilot is used or its state is opened, with no background task. Missing data
stays unknown. A green suite does not prove that the selection is adequate.
`investigate-canary` records only a next investigation; it does not enable
selection automatically or complete the X5 Work.

## Qualification and costs

X5 remains `PARTIAL` pending the real economic qualification Q04. The C5 mechanism implementation and the Q05 decision were delivered; a real net gain has not been demonstrated yet. The current technical decision is not to adopt it broadly in this cycle.

Observed costs (capture, real runs, confirmations and setup/cleanup), projections and delivery costs are separate fields. Recapture, invalidation, retry, correction and audits must be explicit; missing values are `UNKNOWN`. Reused commands cannot be counted twice. Repeating the same cases measures stability; it does not widen the proof of adoption.

Normal and weekly CI continue. The expensive X5 campaign is opt-in only, with `x5_campaign=true` in the `ci.yml` workflow of `augusto-scarvalho/tare.tools.specgraph`: use it only with a new hypothesis or new evidence, or a concrete need to confirm stability. It is not a requirement for everyday use.

## Conversation checks in the TUI

A command configured in **Verification argv for a new conversation** accompanies
planning and delegated work. The coordinator still only reads and
orchestrates; this command does not grant it direct execution. The executor preserves
the argv and the configured gates in the delivery verification.

Empty checks do not inject the whole pytest run into edit-only or command-enabled conversations. A configured verifier keeps its exact argv; structured sessions keep the gates. The mandatory suite is preserved.

The OS's current CI selection has its own evidence and is not automatically equivalent to the X5 qualification. Operational adoption requires proof of scope, a comparable full cost, an audit and an immediate fallback.

## References

- [Specification maintained by the owner](https://github.com/augusto-scarvalho/tare.tools.specgraph/blob/main/docs/specs/SPEC-SPECGRAPH-X5.md).
- [Implementation in SpecGraph 0.7.4](https://github.com/augusto-scarvalho/tare.tools.specgraph/pull/23), validated in [Windows/Linux CI](https://github.com/augusto-scarvalho/tare.tools.specgraph/actions/runs/38657172622) with the native core and the real executor.
