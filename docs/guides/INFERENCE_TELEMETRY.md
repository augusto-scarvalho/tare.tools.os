# Inference telemetry

```bash
tare-ops telemetry --html telemetry.html --json telemetry.json
# Equivalent from the OS checkout:
python tare_ops.py telemetry --html telemetry.html --json telemetry.json
```

The HTML opens directly in the browser, with no server or external dependencies.
The filters by model, origin, run and cache recompute the indicators; each
row opens the receipt details. The selection can be exported as JSON. Run
the command again to include new calls. There is no polling or inference.

By default, it reads the OS's `relay/round_tables` and the workspace's `.reconciliation`.
Use `--source path` repeatedly to select directories or JSON receipts;
explicit sources replace these defaults. This lets you keep production,
staging and a specific round apart without mixing their metrics. The command does not
run audits, does not call APIs and does not export prompts or conversations.

## What is measured

| Indicator | Interpretation |
|---|---|
| Observed calls | Individual calls recorded by the local explorer, research jobs and, in older history, the retired managed Aider. |
| Aggregate runs | CLI audits, seat/mediation/scribe attempts and old explorations; they are not converted into a number of calls. |
| Input and output | Counters present in the receipt, with explicit coverage. No estimate from character counts. |
| HIT / MISS / no data | Positive cache read / zero explicitly reported / missing counter. A HIT can be partial. |
| Reused input | Sum of tokens read from cache / sum of total input, only where both are known. |
| Reported cost | Field returned by the provider; it does not replace reconciliation with the invoice. |
| Additional estimate | Estimates only for records without a reported cost, kept separate from the reported value. |
| Median and P95 | Duration of the individual calls that have a measurement; aggregate runs are left out. Includes measured failures. |

Missing counters stay unknown, including in the vendor CLIs that
do not record them. Local inference and subscriptions do not get a fictitious zero price.
A cache percentage is not a percentage of financial savings. Local compute
cost and time to first token are not measured in this report.

Repeated receipts are deduplicated by the session/call ID, the exploration
request or the Round Table attempt. An exploration present in the parent's state and
in its own result appears once. Distinct retries are still counted.
Aggregates of audits that used several models are not attributed entirely
to the main model. Invalid/missing files stay visible in the notes.

New calls include the UTC start, duration and usage when available. The history
stays readable, but fields it never recorded cannot be recovered.
The private observations do not feed the auditor's context. The generated JSON uses
the `tare.tools/inference-telemetry/1` schema and contains only the selected run
metadata, counters, costs and receipt provenance.

## Sources and data contract

| Origin | Receipt consumed | Granularity |
|---|---|---|
| `managed` | `session.json`, schema `tare.tools/aider-session/1`, `model_observations` (sessions saved before the managed Aider was retired) | One row per call observation. |
| `explorer` | the session's `delegation_observations` and `result.json` with schema `tare.tools/audit-exploration-result/*` | New calls; an aggregate run for old receipts. |
| `audit` | `result.json`, schema `tare.tools/audit-result/1`, `metrics` | One aggregate run per case/run. |
| `round_table` | `seat_*.json`, `mediation.json`, `scribe.json` | Executed attempts; circuit skips and deterministic compilation are not calls. |
| `qualification` | Explicit receipt `tare.tools/managed-cache-qualification/1` | Calls from `final_routes`, without duplicating preliminary trials. |
| `research` | `result.json`, schema `tare.tools/research-result/1`, `model_observations` | One row per map/reduce call of `tare-ops research`; the group is the job id. |

Directories are walked by the receipt names above; nested Git repositories,
directory links and folders such as `.git`, `.venv` and `node_modules` are skipped.
The read limit is 32 MiB per receipt. Copies with the same identity use the
version with the latest mtime. An individual exploration observation replaces its
legacy aggregate. Missing timestamps stay missing; "most recent" sorts
the available start times first, without turning mtime into an inference time.

The full JSON document contains `schema`, `generated_at`, `summary`, `rows`,
`warnings` and `notes`. Each row has an identity (`id`, `group`, `origin`,
`model`, `role`, `provider`, `unit`), state/duration/start, input,
output and cache read/write counters, reported/estimated costs, prompt change,
usage source and the receipt's relative path. `unit` tells `call` apart from `attempt`.
Costs are decimal strings or `null`; missing counters are `null`.
The numeric sums in `summary` must be read together with the coverage fields
(`input_records`, `cache_records`, `cost_records` etc.): a zero sum with zero
coverage does not prove zero consumption. The browser's filtered export contains
`rows`, notes and warnings, without reusing the global summary of another selection.

`session.json.cache_summary` and `outcome.json.cache` stay restricted to the main
agent. `session.json.telemetry_summary` and `outcome.json.telemetry` consolidate
the cache of the parent and the observed delegations; `delegated_calls` counts measured calls and
`delegation_requests` counts exploration requests. These summaries are not the
full cost report. The report uses the individual observations and the
final receipts, preserving the provenance of every number.

## Build a report from your own history

From a checkout of this repository, point `--source` at the folders that hold receipts. For an installed product
the history folder is `%LOCALAPPDATA%\tare.tools\product\state` unless you chose another one with
`--storage-root` or `TARE_STORAGE_ROOT`; round-table runs, if you use them, are in `relay/round_tables`:

```powershell
python tare_ops.py telemetry --source "$env:LOCALAPPDATA\tare.tools\product\state" --source relay/round_tables `
  --html telemetry/index.html --json telemetry/telemetry.json
```

Open `telemetry/index.html` in a browser. The totals cover whatever receipts the sources contain, so they are
neither your provider invoice nor the cost of one task; they change as new receipts are written. The report
reads files only: it makes no API call and exports no prompt or conversation.

## Maintenance and evidence

The Kernel captures usage in `compute_plane/agent_worker.py` and
`compute_plane/audit_exploration.py`; `compute_plane/agent_session.py` keeps the
private custody and the aggregate. The OS exposes the command in `tare_ops.py` and generates the report in
`scripts/round_table/report_round_table_telemetry.py`, with the neighboring template
`telemetry_report.html`. Sessions saved by the retired managed Aider are still read, with no second collector or
new service.

The OS tests are in `tests/test_inference_telemetry.py`; the Kernel tests in
`tests/test_audit_exploration.py` and
`tests/test_managed_prompt_cache.py`. Validation covers deduplication, unknowns,
costs, weighted cache, failures, context privacy and escaped data in the HTML.
The browser view was checked with Edge/Playwright on desktop and mobile.

No paid calls were made to implement or validate the panel; the real cache qualification
belongs to the previous step.
