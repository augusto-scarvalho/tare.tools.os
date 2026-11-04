# Automatic flow selection fallback

Choose a flow first uses its configured local evaluator. A known capacity or
resource refusal can now trigger **one configured vendor inference call**, with
no task tools. This also applies to eligible completed-investigation boundaries;
their source freshness checks and existing transition controls remain intact.
An explicit `/session`, `/explore`, `/plan`, or `/research` never calls a selector.

The fallback receives the complete state, question and options. Its only accepted
output is `{"choice":"<existing option key>"}`. It cannot rewrite the demand,
invent a Work ID, grant execution authority or bypass Start suggested flow.
Read-only exploration retains the explorer role; research still requires URLs.

## Configuration

Add this optional field to the existing flow assessment policy
(`TARE_STORAGE_ROOT/assessment-policies/flows.json`, or the file `TARE_FLOW_POLICY` names):

```json
"fallback": {
  "deployment": "codex-native-gpt-5-6-terra",
  "timeout_seconds": 30,
  "output_tokens": 2048
}
```

The deployment must be an installed explicit Codex Direct or vendor CLI inference
profile. The implementation uses the existing native inference/auth layer, with
empty tool specifications. It does not start a vendor agent loop or activate a
fleet GPU model. No auto-selected fallback profile, retry chain or unlimited
deadline is allowed. Existing profile spend controls remain in force. The output
allowance is capped by both policy and profile; the deadline is capped by both
policy and profile. Missing `fallback` retains the local-only behavior.

The local flow policy was activated with the configuration above. Other route
policies and installed model profiles were not changed.

### Interactive entry evaluator

The policy may additionally set `entry_command` to a non-empty array of non-empty
command arguments. It is used only for interactive entry flow selection: when it
is present, it replaces the policy's normal `command` for that one assessment.
This permits entry selection to use an already resident GPU model while refusing
to cold-load a CPU model. If no resident evaluator is available under that entry
command, the assessment abstains with `ASSESSMENT_CPU_NOT_ADMITTED` and reports
that CPU evaluation is disabled for interactive flow selection. With a configured
fallback, that known refusal triggers one vendor inference automatically. Manual
flow selection remains available when no fallback is configured or when the
fallback fails.

`entry_command` does not change completed-investigation (`after-findings`)
assessment. That boundary continues to use the original policy `command`, its
existing evaluator admission behavior, and its source-freshness and transition
controls.

## Refusal and evidence semantics

Eligible local outcomes are capacity/input overflow, insufficient RAM, busy CPU,
unavailable worker, `ASSESSMENT_CPU_NOT_ADMITTED`, or an explicit
unsupported-preference observation. `ASSESSMENT_CPU_NOT_ADMITTED` is a known
interactive admission refusal, not an attempt to cold-load CPU weights. CPU
timeout is eligible only with recorded worker exit 124 and a known CPU stage.
Cancellation, invalid distributions/checkpoints and uncertain transport/GPU
submissions never trigger another evaluator. In particular,
`ASSESSMENT_RESOURCE_UNAVAILABLE` can mean an uncertain GPU submission, so it
remains excluded.

Each boundary retains the local assessment and a separate fallback receipt,
including exact model, usage and failure evidence. Dispatch intent is persisted
before the fallback call. Interrupted boundaries are not automatically replayed;
unknown call counts/combined tokens stay unknown, with an observed call lower
bound where available. A failed fallback preserves the original demand and
reports the failure without silently starting another model.

## Real TUI validation — September 21, 2026

The original 1594-byte dogfood demand was submitted again through the public
console using Windows ConPTY keyboard/mouse input and real inference. It had
previously failed with an empty worker response after 57 seconds, then received
a fast capacity refusal after the first correction.

With the fallback enabled:

- The exact same request digest reached local preflight: **4219 ms**, capacity
  exceeded. No CPU weights were loaded for this request.
- Codex Direct **GPT-5.6 Terra, medium** chose `session` in **1578 ms**, one call,
  with **580 input / 9 output tokens**, runtime-reported model match.
- The TUI displayed the automatic session suggestion and identified the fallback
  evaluator. No manual slash command was needed. The original demand remained
  unchanged, ready for Start suggested flow. The already completed implementation
  task was not executed again as part of this selection test.
- The console exited normally with code 0 and verified cleanup.

The two recorded evaluator durations sum to 5797 ms; this is not a wall-clock UI
latency benchmark or a broad routing-quality qualification. Combined token totals
remain conservative when local usage is missing; the vendor receipt reports its
own usage independently. No dollar-cost or subscription-balance claim is made.

Portable evidence and
TUI screenshot.
Full receipts remain in the external reconciliation evidence store. Focused
fallback, flow integration and entry tests passed, including interrupted replay,
receipt-write failure, cancellation, invalid choices and resource exclusions.
Independent source audit found no remaining material issue after correction of
unknown-call accounting. No push or remote CI was performed.
