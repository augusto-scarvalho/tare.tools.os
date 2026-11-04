# EXP-5: bounded evidence-scheduling replay

Retain current scheduling. This offline retrospective comparator is neither a
production scheduler nor an agent benchmark. It adds no dispatch, admission,
lease, model, cache or session hooks and needs no provider or model download.

## Current owners and experiment boundary

`relay/workflow_ir.py` owns the explicit DAG contract (at most 128 nodes).
The comparator projects IDs/dependencies into `compile_workflow` with the existing
`work.execute/v1` operator; duplicate IDs, missing dependencies and cycles are
rejected there. `relay/workflow_reducer.py` selects at most one action in frozen
plan order: unknown effects veto dispatch/recovery, and recovery precedes fresh
execution. `relay/work_parallel.py` separately bounds two isolated preparations,
requiring readiness, disjoint known paths/resources and existing model-capacity
controls; Work retains serial incorporation. None of these contracts change.
See also [CI feedback](CI_FEEDBACK.md) for the existing validation owner and scope.

## Offline use

From the repository root, using existing installed dependencies:

```text
python -m scripts.research.compare_evidence_scheduling docs/operations/EVIDENCE_SCHEDULING_CI_SAMPLE_20261021.json
python -m pytest tests/test_evidence_scheduling.py
```

The supervisor runs focused tests with paired dependencies; no subprocesses,
network calls or paid benchmarks are needed by this experiment. The CLI reads
only its local input, never executes commands or resolves `source_ref`, writes
no files, emits compact JSON to stdout and reports malformed input to stderr
with exit status 2. `compare(data)` is pure and raises `ValueError` for invalid
input. Missing/null durations return `INSUFFICIENT_DATA`, explicit gaps and no
timing comparison, even for background work. Validation is not skipped for gaps.

Minimal synthetic input (not observed evidence):

```json
{
  "schema": "tare.tools/evidence-scheduling-replay/1",
  "basis": "fixture",
  "capacities": {"cpu": 1},
  "tasks": [
    {"id": "background", "depends_on": [], "pool": "cpu", "duration_ms": 8, "source_ref": "fixture:background"},
    {"id": "gate", "depends_on": [], "pool": "cpu", "duration_ms": 2, "source_ref": "fixture:gate"}
  ],
  "required": ["gate"]
}
```

Both `fixture` and `observed_durations` mean counterfactual closed-batch replay.
Capacities are positive integer slots; tasks explicitly name a pool and nonempty
source reference. `required` is a nonempty unique list of present IDs: the whole
set must finish, not just the first gate. Only `duration_ms` may be absent from a
task. Optional `provenance` is a descriptive JSON object, never authority.
Other unsupported fields/schema, booleans as numbers and invalid evidence fail.
Durations must be exact whole milliseconds, 0 through 10^12 inclusive; integral
floats are accepted, fractional values are rejected, never rounded. Zero is
valid, null/absence is unknown. NaN/infinity and duplicate JSON keys are rejected.

Input is bounded to 256 KiB both as raw CLI bytes and canonical sorted compact
ASCII JSON; output is bounded to 2 MiB. Deep/non-JSON Python values are rejected.
Reports identify the full input (including metadata) by SHA-256 of that canonical
encoding. Identity, basis, hashes and source references do not authenticate
observations. Pool-key order does not change the report. Large strings/metadata
consume the same input budget.

## Policies and metrics

All roots are admitted at t=0. Every policy runs the same successful work with
fixed service times, dependency gates and pool capacities; execution is stable,
deterministic and nonpreemptive. A busy pool never blocks a different free pool.
All simultaneous completions are applied before the next selections (including
zero-duration chains). Background tasks always finish.

- **plan_order**: input order among ready tasks, the reference priority contract.
- **shortest_ready**: shortest ready duration, a throughput proxy, not a claim
  about the current production policy.
- **evidence_critical_path**: first the ancestor closure of required evidence,
  then descending remaining duration-weighted path within that closure. A target's
  nonrequired descendants cannot inflate its score. All ties use input order.

Timelines list each task's start/end in input order. `makespan_ms` ends all work;
`time_to_required_evidence_ms` ends the complete required set; mean completion
time averages all end times from t=0. `total_service_ms` sums the same fixed
work, not paid tokens or compute savings. Contended selection opportunities are
pool-fill events with at least one free slot and more dependency-ready pending
tasks than free slots; the report gives time, pool, eligible count and available
slots. It does not count waiting at a full pool as a new choice.

The focused synthetic tradeoff has two required gates and background work:
plan-order evidence time is 21 ms, shortest-ready 11 ms and critical-path 10 ms;
shortest-ready has lower mean completion time than critical-path. All finish the
same 21 ms service. Such fixtures demonstrate mechanics/tradeoffs, not measured
Work performance or a universal winning scheduler.

## Captured CI sample: scope and result

The read-only `docs/operations/EVIDENCE_SCHEDULING_CI_SAMPLE_20261021.json`
records successful Linux/WSL2 and Windows jobs from run **39630158317**, head
`1f2591e65732d8737a77430043b060d0b205b012`, retrieved 2026-10-21. Its provenance
contains job URLs, response hash and timestamps. Durations are completed minus
started, including setup, **not inference time**: 1,317,000 and 2,530,000 ms.

Under its one-slot-per-platform, both-ready-at-zero replay assumptions, all
three policies have **zero contended choices**, identical timelines, makespan
and complete-evidence time **2,530,000 ms**, mean completion **1,923,500 ms** and
total service **3,847,000 ms**. These are replay results computed from captured
durations, not measured policy A/B gains. Both runners (label `aaaaa`) run on the
same physical machine; duration invariance under a different ordering is unproven. This is not
a complete Work cohort. There is no priority decision to learn from this sample.

The [retained result](../operations/EVIDENCE_SCHEDULING_RESULT_20261021.json)
binds this small normalized sample and the comparator source by their Git-blob
SHA-256 identities. These two curated records are the maintained destination;
raw dogfood traces stay in bounded private custody (16 MiB ceiling, review on
2026-11-21). Collection runs only for an explicit experiment, with no recurring
paid load or raw-transcript ingestion into the repository.

## Expanded retrospective sample

The operator challenged the two-job sample because it contained no priority
choice. The [expanded record](EVIDENCE_SCHEDULING_SAMPLE_20261021.md) retains
**30 unique GPT-6.1 Sol native Responses session durations from 16 archive lots**
dated 2026-10-17 through 2026-10-21: 14 explorers and 16 implementers. Duplicate
session IDs, fixture/verification copies, other routes, the separate FLOW-20 A/B
population, missing outcomes, non-successful deliveries and resumed sessions
are excluded with counts. This is a selected calibration corpus, with survivor
bias, not 30 accepted Works or an independent quality comparison. Whole-ms
`elapsed_from_preparation_ms` values are retained; this is session elapsed time,
not inference-only duration, queue delay, GPU occupancy or paid compute.

One existing public-TUI exploration batch supplies actual contention: three
questions, two slots, 23.360 s maximum recorded queue delay and 51.203 s elapsed.
Replaying its successful session durations gives **51.126 s** for plan order and
critical path, versus **54.188 s** for shortest-ready. The 77 ms gap to measured
batch time is not a policy effect: orchestration and timestamp boundaries differ;
changed-order provider behavior is unknown.
All three results are required to finish this batch. Six order permutations are
sensitivity checks of these same tasks, not six independent observations.

The broader stress sample uses **30 constructed eight-task scenarios**: five
fixed-seed assignments for each of independent/single-gate, independent/all-gates,
fork/join, diamond with background work, competing chains and a serial negative
control. Fifteen use one slot and fifteen two. Every policy gets identical tasks,
durations, gates and resources. **23 scenarios contain a priority choice**; the
other seven are negative controls. The DAGs and combined arrivals are hypothetical;
their durations come from the corpus. They do not create 30 observed workloads.

Two alternatives avoid knowing a task's final duration: `structure_only` ranks
the required ancestor closure by remaining dependency depth; `past_median` uses
role medians from other archive lots ending strictly before each sampled session
starts. The latter excludes future, same-lot and current-session durations and
has a fixed 60 s estimate fallback in seven records. These are static priority
orders replayed by the existing `plan_order` implementation, with no new runtime
scheduler or model call. Oracle alternatives retain the actual final durations
and are optimistic sensitivity references, not deployable predictions.

Results below cover the **23 contended constructed scenarios**, relative to plan
order. The maintained record also includes all 30, including zero-effect controls;
their median gains are 0% for structure-only and 0.7% for past-median.

| Priority | Earlier / same / later evidence | Median evidence-time reduction | Slower whole batch |
| --- | ---: | ---: | ---: |
| Shortest-ready, final-duration oracle | 6 / 8 / 9 | 0.0% | 4/23 |
| Critical path, final-duration oracle | 17 / 6 / 0 | 11.7% | 3/23 |
| Required dependency depth only | 13 / 10 / 0 | 2.7% | 2/23 |
| Critical path with past role medians | 15 / 6 / 2 | 4.7% | 3/23 |

The historical estimate's worst evidence-time regression is **21.5%**. Prioritizing
one gate also delays other work: the maximum individual completion delay is
547.719 s with dependency depth and 1,050.281 s with past medians in this matrix.
Earlier target evidence therefore does not establish better total delivery time,
fairness, quality or cost. Reused observations/scenarios are correlated; no
confidence interval, universal winning policy or population effect is claimed.

Decision: retain production scheduling. Dependency depth is a small candidate for
future measured use; the sample does not justify duration prediction or a larger
scheduler. The next empirical comparison needs ordinary batches with actual
contention, explicit useful gates and comparable completion quality. Count one
batch once, retain queue/capacity/arrival observations and delays to other work,
and report missing monetary cost as unknown. Shadow calculations can reuse those
records; a live policy A/B needs its own finite scope before spending quota.

All input DAGs, source hashes, priority orders, estimates and result metrics remain
in the approximately 110 KiB documentation appendix. Replaying the appendix checks
dependencies, slots, full work/gate completion and invariant total service. The
comparator identity hashes the recorded commit's Git blob bytes, independent of
Windows checkout line-ending conversion. The
private analysis plus compact report is capped at 1 MiB, with review on 2026-11-21;
keep the compact appendix and drop only reproducible temporary copies when safe.
No native journal, raw transcript, download or recurring collection is added.

## Limits and reopening

Observed service times are retrospective knowledge unavailable as perfect live
predictions. The all-success closed batch says nothing about failure-driven
waste, arrivals, human decision time, cache/model quality or the entire Work
population. No ecosystem speedup or new production default follows.

Reopen only with a **real contended Work cohort** identifying every required
evidence gate, resource capacities and actual arrivals, observed costs (including
paid usage), comparable output quality, and enough provenance to compare current
priority against alternatives under defensible service-time assumptions. Measure
decision latency and costs together before changing current scheduling. No
universal scheduler, tuning service, database, registry, background telemetry or
automatic paid benchmark is introduced.
