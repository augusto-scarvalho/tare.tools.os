# Behavioral profiles from Work evidence and existing audits

Open `/usage` in the TUI and select **Behavioral profiles**. This optional view
reuses the existing control-owner Work review and its closed usage snapshots,
and includes a bounded, packaged projection of existing audit evidence.
Opening it reads no provider, writes no receipt or preference, and never controls Work
admission or routing. An empty history shows zero recorded executions; a failed
projection leaves the other usage views and ordinary execution available.

## Link deliveries, audit decisions and use

The first section links each completed managed Work to its exact owner run ids,
candidate commits, implementer call identities, saved audit artifact and optional
operator review. A common title, model alias or Work id alone is insufficient.
Explicit resume lineage retains failed attempts and their recorded consumption.
Auditor models remain separate from implementer models. Invalid joins, conflicting
copies and stale reviews are reported as gaps; current settings never repair old data.

New managed attempts record a compact `runtime_observation`: the running controller's
agent-runtime distribution version and the frozen requested model, protocol, deployment
and explicitly configured effort. Native direct-session outcomes retain the same
observation per invocation; their last observation appears in conversation usage.
This describes that controller attempt, not an attested provider model version or
the runtime of reused historical calls. It adds no prompt content or cache-key change.
Direct sessions and controlled experiments do not become ordinary completed Works.

With **Behavioral profiles** selected, **Review delivery** opens an optional review
of a recorded candidate. Record acceptance and material rework as yes, no or unknown,
give a short supporting reference, and resolve each existing finding as unresolved,
confirmed, corrected or rejected. A corrected finding also needs a correction reference.
The cause may remain unknown or be recorded as implementation, Tare tools, guidance or
external. This avoids treating a tool failure as an implementation defect.

Saving uses the existing Work writer lock and checks the exact preview against the
current receipt. A changed candidate/audit/receipt or an active writer requires reopening
Usage. Only `delivery_review` is added/replaced in that same receipt, capped at 16 KiB;
there is no new ledger, transcript copy or retention process. These are attributed
operator assertions, not independently verified facts or cryptographic identity.
Opening, cancelling or viewing the panel writes no review and makes no model call.

The view describes three signals with their unknowns and denominators:

- Acceptance without material rework, only when explicitly recorded. Earlier corrective
  work cannot disappear behind a clean final attempt; unknown earlier work stays unknown.
- Confirmed/corrected findings by recorded cause, with unresolved claims kept separately.
- Recorded call intents, tokens, cache reads/writes and model charges/estimates across
  attributed attempts and audits. Partial counts and unpriced calls remain visible;
  human/infrastructure cost and full delivery cost stay unknown.

Comparison groups require the same explicit project and task class and retain exact
requested/observed implementer model, controller runtime version, effort and protocol.
Missing or mixed dimensions exclude the delivery from a group without hiding it.
These small observational groups do not establish causality or a universal ranking.
They inform a local operator choice; this view never activates a preference or changes
execution permissions. Existing audit/CI verdicts by themselves remain insufficient.

The [2026-10-21 attribution observation](FLOW18_ATTRIBUTION_OBSERVATION_2026-10-21.json)
reuses 25 existing owner receipts and six completed executions. None has enough
recorded dimensions for a comparison group or a linked ordinary audit snapshot;
the four historical confirmed/corrected findings remain separately visible.
No provider call or receipt rewrite was used to inspect that historical population.
New collection and optional reviews improve future evidence without pretending
to recover missing historical facts.

## Current p30 boundary: withdrawing an operator assertion

In the same **Review delivery** panel, **Withdraw review** opens a short reason
and an explicit **Confirm withdrawal** action. Opening or cancelling writes nothing.
The action is disabled without an eligible saved review or when already excluded.
A busy writer or changed full receipt requires reopening Usage; no source is deleted.
The original assertions, unknown values, references and attribution remain in the
same bounded `delivery_review`, with one compact withdrawal record, not a second
transcript, ledger or timestamp log. Only a later explicit **Save review** can
replace/reactivate it. Withdrawal is reversible exclusion, not physical erasure.
The withdrawn form uses schema v2: an older product reader rejects it as unknown
instead of silently counting the original assertion after rollback. The original
schema is retained alongside the reason. A v2 record missing its withdrawal
metadata is also ineligible; explicit Save writes an active v1 review again.

Derived delivery quality counts exclude withdrawn assertions, including those with
malformed withdrawal metadata. Audit claims remain unresolved/unknown rather than
passing or disappearing. Withdrawal state/reason is displayed separately; attempts,
usage, audit claims and stored candidate stay unchanged. A withdrawal is not a
verified cure and does not undo earlier work or its consumption.

This is one frugal slice of the old p30 portfolio proposal through FLOW-18, not
implementation of that broad proposal. Experience remains source-backed advisory
evidence, not memory injected into agents or automatic prompt memory. Managed
evolution reuses the existing [product transition qualification](PRODUCT_TRANSITION_MODEL.md)
and I2I-01..06 controls.
Cryptographic signing, identity lineage, a crypto ledger, a memory service, global
automatic learning and a self-evolution engine are not delivered. There is no
ranking, preference/routing, authority, prompt prefix/cache change or model call.

## P2.1 uses this same evidence projection

The portfolio proposal **P2.1 Reputation / Qualification from evidence** shares
this FLOW-18 implementation and `/usage` view. There is no second reputation
engine, score store or model qualification gate.

The deterministic calculation is the existing `behavioral_profile_review`:

1. Read the owner's closed Work receipts and attributed usage; retain at most
   ten completed execution groups and preserve explicit resume lineage.
2. Deduplicate identities, retain failed attempts and expose conflicting or
   missing declarations. Attribute implementer models only from that stage;
   do not transfer an auditor's identity to the implementer.
3. Independently project bounded audit snapshots and packaged source reviews
   through `project_work_audits` and `project_audit_evidence`. Identical record
   identities count once; contradictory identities are excluded and reported.
4. Count coverage, recorded verdicts and findings within their own populations
   and verification regimes. Source declarations and hash syntax are validated;
   the view does not reopen citations or independently attest their bytes.
5. Compare exact recorded model declarations with the selected configuration.
   Unmeasured quality, independence, representative qualification and a quality
   preference remain **Unknown**. A closed receipt or a valid hash string is
   insufficient to award reputation or a passing quality result.

The same inputs produce the same projection without provider calls or writes.
It supplies advisory evidence for operator choices and future experience views;
it cannot mint identity, verification authority, eligibility or permissions.
Selection and existing approval/sandbox checks keep their existing owners.
The outstanding representative-cohort qualification remains tracked by FLOW-18,
including the comparison with the operator's preference. P2.1 does not duplicate
that obligation or claim to deliver the older P3.0 experience-ledger proposal.

The unit is a completed execution of a Work, not an inference call. Explicit
resume lineage deduplicates deliveries and retains failed attempts. The view
shows exact project, task-class, runtime and recorded-verdict declarations,
including missing or differing declarations. Only calls explicitly attributed
to `implementer` describe implementer models; audit and other stages remain
separate. Runtime version and effort remain unknown when not separately
established; current settings never fill historical gaps. Legacy stage names
are not silently relabeled.

The baseline is the explicitly selected model profile, whitelisted to requested
model, protocol, deployment and effort. Exact model agreement or mismatch is a
descriptive configuration comparison. It establishes neither runtime equivalence
nor quality, a vendor advantage or a recommended preference. Recorded `SETTLED`,
`ACCEPT` and `ADMITTED` establish closure or a verdict, not adjudicated quality or
audit independence. Full cost and missing evidence stay unknown.

## Existing audits are evidence

The first implementation queried only ordinary Work metadata. That omission
did not establish that the ecosystem lacked audit findings. The view now also
includes [the derived audit record](../../relay/behavioral_evidence.json), shipped
with the product so it works without a developer checkout or private archives.
This is a reconstructible observation artifact, not a new decision store.

The initial, explicitly bounded slice contains:

- Sixteen FLOW-20 source reviews on eight historical repairs, grouped by the
  frozen A/B condition: A completed and passed 8/8, B 7/8. The remaining B
  candidate passed its checks but exhausted its budget before closing delivery.
  Material terminal defects and unjustified scope were explicitly assessed as
  absent in these reviews. Recorded calls were 48/50 and total tokens
  420,937/442,874; these are observations, not prices or ordinary Work counts.
  Explicit custody links, checked against each candidate patch hash, recover
  requested and provider-reported GPT-6.1 Sol identities for all 98 calls. Each
  review cites its original bounded usage snapshot hash. Runtime package version
  and vendor-independent quality remain unknown.
- Four historical confirmed/corrected findings from context and audit-custody
  receipts. The original finding/fix identifiers, claim, correction reference,
  final bounded review hash and auditor are retained.
- Four earlier adversarial re-audit claims without a verified disposition in
  this slice. Their current source status and delivery/model attribution remain
  unknown; they are not counted as current defects or confirmed failures.

Each record retains a source id, byte hash and original pointer. The curated
source references are citations only: the TUI never follows their paths.
The single adjacent JSON input is size/count bounded and whitelisted. Missing
or malformed audit evidence leaves valid ordinary Work data available; missing
or malformed ordinary Work receipts do not hide separately readable audits.
Duplicate identities cannot inflate results; contradictory copies remain
visible as conflicts rather than becoming an arbitrary quality verdict.

These are distinct populations; audit records do not add ordinary completed Works. Historical
correction does not attest to current source bytes. The auditor's model is
never used as the implementer's model, and the configured/requested model is
never silently substituted for an observed runtime. The selected-profile
comparison remains exact and descriptive. The A/B result supports keeping the
current guidance baseline for this bounded experiment; it does not rank vendors
or establish a representative ordinary-delivery preference.

No new inference, rerun or benchmark was needed to recover this evidence.
The [maintained follow-up observation](FLOW18_EXISTING_AUDITS_2026-10-18.json)
records the projection, baseline, source
hashes, current public-TUI checks and known limitations. It supplements the
original six-Work snapshot below, whose pure reconstruction remains supported.

For new managed Works, the existing audit owner now retains a small validated
`audit.observation` in its run receipt, before accepting or rejecting the review.
It contains the original verdict, summary, findings, cited source hashes and
native artifact hash. Rejected findings therefore survive resume without a new
model call. `/usage` reads this snapshot from its existing bounded receipt input;
it never opens a receipt-supplied `artifact_file` or session path. Exact run and
resolved execution ids supply the link to Work lineage. These are recorded
auditor claims, not human adjudication or an implementation-model identity.
Legacy receipts without the snapshot remain unknown and are not rewritten.

## Observation on 2026-10-18

The ordinary owner custody contains 25 receipts: seven settled receipts resolve
to **six completed executions**, plus twelve incomplete execution groups and one
excluded record with unresolved identity. Forty-seven calls are explicitly
attributed to completed Work: **38 implementer calls and nine plan-auditor calls**.
The latest recorded closure is 2026-09-23. The native session implementing this
view and FLOW-20's controlled replays are excluded from the ordinary cohort.

Only one completed execution has a recorded project. No completed execution has
a declared task class, exact runtime declaration, recorded outcome metadata or
adjudicated quality in this historical sample. Current producers already record
optional declarations for new managed Works; no legacy metadata was fabricated.
All monetary costs remain unknown. One execution has implementer model evidence
that differs from the selected GPT-6.1 Sol / medium baseline; five are unknown.
This does not compare delivery quality or justify changing the selected model.

The public TUI query displayed these six executions and the selected baseline
with **zero new model calls** and byte-identical native journals. The implementation
used one bounded GPT-6.1 Sol / medium TUI session, 14 calls, zero summary calls and
no model retries; its native custody was preserved when closing the console.
The maintainer also repaired the empty-history view and checked additive failure
isolation. This is maintainer evidence, not an independent quality cohort.

## A small findings vocabulary

The view's automatic dictionary covers observable **data gaps**:

| Signal | Meaning |
| --- | --- |
| `metadata_missing` | At least one attempt lacks an explicit declaration. |
| `metadata_conflict` | Exact declarations differ between attempts; inspect their history. |
| `implementer_identity_missing` | No implementer model evidence, or an incomplete model identity. |
| `audit_verdict_recorded` | A verdict was recorded; it is not an adjudicated quality finding. |

For a manual review of original delivery audits, use three optional quality
categories: **correctness**, **scope discipline**, and **test integrity**. Retain
the original finding, affected source/version, auditor and verification regime,
then record whether human review confirmed, rejected or left it unresolved.
Cost and human intervention are separate observations, not quality labels.
An absent finding or an unaudited delivery is unknown, never a passing case.
This vocabulary adds no form, identity system, canonical store or required gate.
No adjudicated quality findings are established for those six ordinary
executions. This does not erase the separately attributed source reviews and
historical findings above.

## Reconstruct and revisit

Reconstruct the existing-audit observation offline:

```python
import json
from pathlib import Path
from relay.behavioral_profiles import project_audit_evidence

record = json.loads(Path("docs/guides/FLOW18_EXISTING_AUDITS_2026-10-18.json").read_text(encoding="utf-8"))
assert project_audit_evidence(record["audit_input"], record["baseline"]) == record["audit_review"]
```

The compact [observation record](FLOW18_OBSERVATION_2026-10-18.json) retains the
typed projection input and baseline, not prompts, journals, credentials or raw
sessions. Reconstruct it offline from the repository root:

```python
import json
from pathlib import Path
from relay.behavioral_profiles import behavioral_profile_review

record = json.loads(Path("docs/guides/FLOW18_OBSERVATION_2026-10-18.json").read_text(encoding="utf-8"))
assert behavioral_profile_review(record["projection_input"], record["baseline"]) == record["review"]
```

Reopen `/usage` as ordinary deliveries accumulate. Every ten completed Works is
a descriptive review cadence, **not** a qualification threshold or permission to
execute. Before comparing preferences, review a representative set with original
audits, adjudicated findings and comparable project, task, realization/version,
effort and verification regime. Preserve missingness, contradictory evidence
and small sample sizes. Public benchmarks, role configuration, fixtures and
administrative settlement do not replace that review.

The original ordinary sample leaves representative quality-based model
preferences inconclusive. The audit integration, findings dictionary and
reconstructible comparison use the evidence already available; new observations
can extend them during normal use. Ten is a review cadence, never a requirement
to start this analysis or claim the current integration is unfinished.

The destination is this maintained guide, the compact record and the existing
backlog node. Private controller/native custody is capped at 64 MiB, each VT
trace at 12 MiB; it is eligible for cleanup after 2026-11-18 once exact-head CI
and the Git artifacts are verified and no recovery is active. Ordinary owner
receipts and prompt-cache/session custody are not deleted by this change.
