# Evidence at completion

When it records a delivery through the conversation, the agent reads the item with `backlog_read`
and uses `backlog_record` with the returned revision. A new `DONE` requires:

- an explicit grade and justification;
- references and evidence for each exit criterion;
- an empty list of remaining obligations;
- declared provenance or registered sources. A/B require a recoverable source
  in `graph.sources`, and the item's minimum grade still applies.

The taxonomy belongs to the Backlog: A is a cryptographically verified artifact
with deterministic logs; B, automated tests with full
stdout/stderr; C, manual verification or review sign-off; D, an
informal statement. The Backlog check validates the declared fields and links.
The author of the review remains responsible for the content and the chosen grade.
Referencing a file or writing "tests passed" does not verify that content.

The same preparation validates the preview and the write under the owner's lock. A refusal
preserves the graph. The history keeps the previous completion, its references
and the original grade. `PARTIAL` progress can continue without a grade; a new
completion must declare its own, even when the item had a different one before.
The local command `python -m relay.backlog_delivery` uses the same contract.

A delivery with every criterion covered and no remaining obligations records
`completion.status=DONE` and `work_status=done`. Progress with remaining obligations
records `PARTIAL` and `IN_PROGRESS`. The recorder keeps the evidence and the
existing admission metadata; that metadata does not create a new pending
approval. If an approval is still needed to complete the item, record it in the
remaining obligations. The managed flow still checks its own
admission. PR integration and release installation are looked up in their
own owners.

Older versions wrote `delivered_pending_admission` on every complete delivery.
To fix only that label, use `--repair-statuses` instead of `--record`:

```text
python -m relay.backlog_delivery --graph ABSOLUTE_PATH --repair-statuses
python -m relay.backlog_delivery --graph ABSOLUTE_PATH --repair-statuses --apply --expected-revision PREVIEW_REVISION
```

The preview lists `repairs` and `skipped`. The write changes only `work_status` in
complete records whose evidence and criteria coverage are still intact.
Dates, grades, history and admission are preserved, including in legacy records
without a grade. Changed criteria, real obligations and managed Works are left out of the
fix. A replay with no change does not rewrite the graph. The fix uses the Backlog's lock and
CAS; it does not happen automatically during a read or at startup.

## Managed landing

Landing keeps the existing authority, candidate and admission checks.
Before moving the Git target, it prepares and validates the whole batch under the graph lock.
The closure records C for the admitted sign-off, with the capability revision,
authority and hashes of the verified artifacts; strict candidates include the
descriptor and commit links. That statement has the scope of the sign-off.
The executor's observation still has no grade and no independent attestation. An
item that requires A/B is not closed by this C sign-off.

Earlier metadata stays on the item. On resume, a surviving closure
from the same train keeps its bytes and dates. The host confirms by reading that the
candidate is still in the Git target or in its ancestry before completing
the transition; a divergence keeps the approval and the workspace for reconciliation.

## Old records and other sources

`backlog_read` keeps the response within 24 KiB. If the source records
exceed that budget, the item and its revision stay complete, with
`sources_complete=false` and the omitted count. Query the IDs in
`node.source_refs` with `backlog_read(work_id=..., source_ids=[...])`, up to eight per
call. `sources_request` provides a first selection; narrow it if the selected
records still exceed the response. This query also works when
the graph is over 2 MiB, without raising the file reader's limit. `sources_read`
remains available for reading JSON in files that fit within that limit.
The returned records are metadata: the sources and their omitted contents do not
count as reviewed. An external link
does not widen permissions; an item that exceeds the limit even without sources is refused.

`backlog verify-evidence` reports old gaps without migrating them. An unchanged legacy
completion does not block a change to another item. Revising its evidence
requires a complete statement; an exact replay does not assign grades or renew dates.
Do not fill in a grade to turn the report green without examining the
corresponding proof.

The rule applies to the actionable types of the policy, including Recovery
Bridge, TCP and I2I Works. Steps still `NOT_DONE` do not get a completion grade.
Imported documents and historical projections remain observations;
writing or importing them is not the same as executing and qualifying the work.
SpecGraph's source/criteria checks and the OS admission checks remain
responsible for their links, without turning `READY` into an evidence grade.

When the local proof is not yet in the source registry, the operator
can authorize registering it together with the review. The optional `source_files` argument
of `backlog_record` takes up to eight entries with `repository`, `path` and the `sha256`
of a successful read in the same conversation. The host checks the bytes again
and writes the sources and the progress in the same CAS transaction, adding the IDs
to the item's references. A file that was not read, that changed, that is outside the allowed access, or the
graph itself cannot be registered this way. Existing sources are not replaced;
a refusal leaves no partial registration. There is no extra copy of the proofs.

This registration identifies an observed local version. It does not prove that the whole file
was reviewed, does not run tests and does not assign or authenticate A/B. The grade and its
justification remain explicit. Registration is optional and does not happen on every
read; managed Works keep their settlement/recovery owner.

The legacy review report stays in the work's local custody, referenced
in the record of item x03, with the revision and the source hash. Logs, operator graphs and
investigation packets are not included in the distributed product.
