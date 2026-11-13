# Temporary inference policy

The options below apply only to the current instance and its subprocesses. They do not
change global preferences. New instances do not reactivate `*_only`, `prefer_*`
or `disable_judge` written in old records.

## Syntax

`--api_only`, `--local_only`, `--subscription_only` and `--prefer_api`,
`--prefer_local`, `--prefer_subscription` accept optional lists. The aliases
`--cli_only` and `--prefer_cli` are equivalent to the subscription options. Spellings
with a hyphen instead of an underscore are also accepted.

An entry is `vendor/model@level` or `backend/model@level`; a vendor
alone allows its registered models. Separate entries with a space or `;`.
Commas also separate entries, but after `@` they can add levels:
`ninfer/qwen3.8-gsq@low,medium,xhigh`. Duplicates and permutations do not change the
allowed set: the list does not set an order or trigger several calls.

```text
tare C:/project --prefer_cli anthropic openai
tare console C:/project --api_only openrouter anthropic
tare C:/project --api_only=google --coordinator google/gemini-3.8-flash@high
tare C:/project --local_only=ninfer/qwen3.8-gsq@low,medium,xhigh --coordinator ninfer/qwen3.8-gsq@low
tare C:/project --coordinator strata/qwen-next@medium
```

For a PROJECT whose name equals a vendor, use an explicit path (`./google`)
or `--root`. The attached form `--api_only=google` delimits the list without ambiguity.
Missing flags and legacy switches without a list keep their previous behavior;
they do not introduce a default allowlist. An explicit empty list is an error.

## Roles, reasoning and pin

`*_only` restricts all work to the chosen family. `prefer_api` and `prefer_cli`
keep the per-role priority, but their lists filter the family in every
role. `prefer_local` keeps execution local and planning/auditing external.
Until checks run in the sandbox by default, `local_only` implements only with an
Action review (sandboxed checks) and refuses before planning a delivery without one.
`local_only` is the one mode where the local model also plans and audits
(`local_only_roles` in `relay/role_deployments.json`): the owner's admission by
choice of that mode, not a quality claim. With no explicit level it plans at
`xhigh`, its lowest registered level at or above high, and audits at the level
nearest above the `auditor_effort` default. Its audit of a Qwen-family
implementer is not independent: tare labels it **same-family audit, not
independent** and, in the TUI, asks **Apply this change?** before applying. When
that audit does not accept but leaves only open items (no findings) after the
checks passed, the same question says so, lists the items and starts on **Do not
apply**; a yes is recorded as the user's decision, never as an accepted audit.
Findings take the usual correction path. An ACCEPT that still lists open items is
read as not accepted on this path only; every other auditor's such delivery stays
invalid. Without a TUI the candidate stays unapplied, as when no independent auditor exists.
Inherited restrictions are intersected; a fallback does not widen them. Pro stays
opt-in and exclusive to the planner. JEV and search APIs keep their exceptions;
model inference used in research remains subject to the work policy.
`local_only` admits only the local judge, which reviews a protected command only while it is
already loaded and idle; otherwise tare asks you about each command, or once per turn in YOLO
before running commands without review. `disable_judge` asks you about each protected
command, unless the project is in YOLO (Edit + system commands), where no model reviews it
and nothing asks; Codex review, when selected, still runs.

`--coordinator` requires exactly one model and one level. It does not choose the planner
or the auditor. Opus 5.5, GPT-6.1 Sol and Gemini 3.8 use high on eligible connections;
the AGY identifier `gemini-3.8-flash-high` stands for Gemini 3.8 high. Google-only
users can select it without needing another vendor. Without an explicit
selection, the existing automatic rules stay in place.

Qwen3.8 GSQ/NInfer and Swift Next/Strata declare only low/medium/xhigh. Low
remains the local baseline; high/max are not fabricated. Local effort changes
only `chat_template_kwargs.reasoning_effort`, preserving cache controls,
preserve_thinking, enable_thinking and token limits. Swift Next depends on the
existing memory/context admission and lease, with no speculative loading or preemption.

The concrete realization pins the model, connection and effort in the conversation without changing the
original deployment. Reopening preserves that pin and the historical bytes; it does not
restore old temporary permissions. The reopened pin lives in the conversation state
and in the environment of its own subprocess, never in the TUI's global environment.
A new demand does not inherit the pin of another demand. `--coordinator` at
TUI launch sets only the default for new demands in this instance, separate from the
temporary flags; reopening a demand preserves its own pin, even if it differs
from that default, under the current restrictions. An unavailable or incompatible pin
blocks inference, without silently switching models. Conflicts, invalid
names, an ambiguous connection and the lack of an eligible level/candidate require correcting
the selection, not relaxing the policy. The picker/header shows the selection and a summary
of the restrictions; the temporary choice does not write planner preferences.

## Exact per-role pins and offline discovery

`--planner`, `--implementer`, `--explorer`, `--researcher` and `--auditor`
require `vendor/deployment@reasoning`. The deployment identifies an exact
connection, not a search by model name. `--auditor` also applies to
`plan-auditor`; independence of identity and bias domain is still
checked against the real producer, except that `--local_only` admits a
same-family auditor as described above. The planner requires high or above. Research
remains limited to qualified connections with read-only web tools;
without a flag, it keeps medium.

```text
tare models --role planner --via cli
tare models --role coordinator --via cli
tare models --role plan-auditor --via cli
tare models --role researcher --include-unconfigured --json
tare models --vendor OpenAI --provider openai
tare console ./project --planner openai/codex-cli@high
```

Copy selectors from the local listing. `--role coordinator` reuses the existing explicit
policy: only approved models, high for hosted models and the declared levels
for local ones. Each listed `--coordinator` argument can be copied into the public
launch command; it is not a new temporary pin for the five roles. Its conversation selection
is still preserved on reopening as described above. `--role plan-auditor`
shows `--auditor` arguments, never a new `--plan-auditor` flag.

The exact pin takes precedence over defaults and arguments generated by the policy;
an explicitly incompatible route or effort is an error before the call, not
an authorization to overwrite the pin. The producer of an earlier candidate
remains the saved real producer, including for checking independence.

Copy selectors from the local listing. Empty, incomplete, unknown,
conflicting pins, or pins incompatible with only, allowlists, PRO and configuration are
refused before inference. A limit, expiration or unavailability does not
authorize another model, connection, level, tier or overflow. Roles without a flag keep
their selection. JEV, YOLO, PRO, sandbox and permissions are not widened.

These pins are transient: they apply only to this instance and its children;
reopening does not reactivate them from old records. They do not rewrite
preferences, producers, histories, plans, journals, caches or candidates.

`tare models` offers `--role`, `--via cli|api|local`, `--vendor` (author),
`--provider` (connection), `--json` and `--include-unconfigured`. The projection uses the
existing profiles/templates and policies. Unconfigured entries are not
executable. Configuration does not prove availability, valid credentials or
quota. Listing does not resolve secrets, query providers, run inference,
load models or write preferences.

## Offline evidence

The recorded evidence is the aggregate receipt of the host's `pytest-tests` verifier,
with `exit_code=0` and `scratch_removed=true`. This verifier regenerates the fingerprints
derived from the manifest and runs the focused tests. Four separate batches,
directories or receipts are not required for operation or delivery, nor is it assumed that
separate runs took place.
Tests use offline fixtures: they do not prove live availability, AB8 or
model superiority, nor do they authorize paid traffic, downloads or global changes.

## Why keep the local baseline

The research of 2026-10-30 verified the GSQ-RCO IQ3_S deployments on NInfer and Swift
Flash Next GSQ-RCO IQ3_XXS on Strata. The corresponding models and templates
declare `low`, `medium` and `xhigh`; these names are not equivalent to the levels of other
vendors. See the model cards of [Qwen3.8 27B](https://huggingface.co/Qwen/Qwen3.8-27B)
and [Swift Flash Next](https://huggingface.co/ukisai/Swift1.5-Qwen3.8-Flash-Next).

The BF16 results published by the Swift author do not improve monotonically
between low and medium. They do not qualify our quantizations as coordinators.
Effort alone also does not widen the thinking budget configured in the backend.
That is why we keep low and the existing cache controls; raising the effort
is an explicit selection, with no claim of a quality gain before measuring the real task.
