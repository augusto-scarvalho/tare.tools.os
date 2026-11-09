# Why a route cannot start

Selecting **tare auto** keeps the configured route order and uses the first
eligible route. When none can start, the TUI shows each rejected route with a
code, explanation and next step. The request stays in the composer so the user
can select another available model or review setup, then send it again.

| Category | Examples | Meaning |
| --- | --- | --- |
| Policy | `MACHINE_POLICY`, `LOCAL_GPU_DISABLED` | The saved machine or conversation choices exclude the route |
| Dependency | `CLI_UNAVAILABLE`, `CREDENTIAL_UNAVAILABLE`, `GATEWAY_UNREACHABLE` | A required executable, credential or service is unavailable |
| Resource | `GPU_BUSY`, `GPU_RESERVED`, `QUOTA_EXHAUSTED` | An observed holder or usage limit prevents dispatch now |
| Configuration | `PROFILE_UNAVAILABLE`, `ROLE_NOT_CONFIGURED`, `NOT_QUALIFIED_PIN_ONLY` | The selected profile or role needs configuration, or the route is reached only by an exact pin until qualified |
| Unknown | `GPU_STATUS_UNKNOWN`, `RESOURCE_OBSERVATION_INVALID` | Available evidence does not establish a specific cause |

An image job waiting for the shared GPU is resource contention; it is distinct
from a container image being unavailable. Text queued behind text remains
eligible under the gateway's existing queue contract. A gateway without a fleet
endpoint keeps its existing behavior.

The diagnostic explains an existing admission decision. Resource observations
are snapshots; reservation and runtime transport failures retain their own
owners. A `retryable` hint never starts a retry or changes a policy.

## Contract and custody

The [UnschedulableReason schema](../../contracts/v1/unschedulable-reason.schema.json)
defines `code`, `category`, `message`, `action`, `retryable` and an optional bounded
`retry_after_seconds`. Codes are assigned where the policy or resource check
makes the decision. The fixed projection interprets exact codes, including the
Kernel's existing GPU observations, rather than parsing exception messages.

`relay/scheduling_diagnostics.py` owns the pure projection and bounded report.
Existing string reason fields remain usable. Native role candidate receipts
and automatic-route skips also carry their structured diagnostic. Unknown codes
use `UNKNOWN`; arbitrary error text and diagnostic payloads are not copied into
the explanation.

A report retains up to 64 rejected routes and explicitly counts additional
entries. Native CLI JSON carries the report in `unschedulable`; the TUI records
it in its existing demand history. Thus the former 300/256-character error
slices cannot remove the structured explanation. There is no additional store
or polling service.

`execution=agent_not_started` and `model_calls=0` require a new request refused
before entering the Work agent owner, without an existing native session origin.
Errors after owner entry and existing sessions do not acquire an invented zero.
The TUI clears the unused session reference and its delivery warning only after
this explicit preflight result and a check that no native origin exists. The
draft is restored without overwriting a newer draft.

## Qualification: res-ip-b, 2026-10-17

Windows qualification recorded 188 distinct passing cases after fail-fast
replay: contract/schema, routing owners, preflight CLI, TUI presentation and
lifecycle, demand persistence and source-manifest compliance. CI qualifies its
own checked-out revision independently on Windows and Linux.

The public TUI reproduction disabled all provider classes in an isolated setup.
Before the fix, it displayed a generic blocked response, truncated the route
list and tried to escalate a native session that did not exist. After the fix,
it displayed all 14 rejected routes, preserved the request and recorded zero
model calls. The final replay created no native session or usage file and left
no stale delivery warning. This is a controlled routing case, not a model
quality benchmark or a real completed Work cohort.

The implementation conversation was piloted through the public model picker,
permission form and composer, using GPT-6.1 Sol native Responses at medium with
18 calls and a 600-second ceiling. It produced the pure module, schema and their
tests; the host integrated and verified the existing owners. An initial setup
attempt made one short Bedrock call before the unsaved configuration was caught.
Subsequent diagnostic replays ran with provider classes disabled.

Maintained failure cases live in `tests/test_scheduling_diagnostics.py` and
`tests/test_unschedulable_dispatch.py`. The raw evidence of this run (local JUnit
reports, small terminal frames, cleanup receipts and the demand receipt reference)
is not published. Platform results must come from
their actual local or CI runs. Routing policy, model histories, quota and
execution authority remain with their existing owners; Kernel changes were
unnecessary for this confirmed gap in OS composition.
