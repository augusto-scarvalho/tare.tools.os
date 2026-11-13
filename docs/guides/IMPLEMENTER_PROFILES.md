# FLOW-19: implementer profiles

Policy v1, approved by the operator on 2026-11-01. Cheap, Intermediate and Smart
group model and reasoning settings for choosing the implementer.
They are operational preferences and do not depend on CLI quota percentages.
They are not a ranking of quality or of proven savings.

| Model | Cheap | Intermediate | Smart |
| --- | --- | --- | --- |
| Claude Haiku 5.5 | xhigh | — | — |
| GPT-6 Luna | xhigh | — | — |
| Gemini 3.8 Flash | — | medium, high | — |
| Claude Sonnet 5.5 | — | medium | high, xhigh |
| GPT-6.1 Sol | — | medium | high, xhigh |
| Claude Opus 5.5 | — | — | medium, high |
| Gemini 4 Argon | — | low | medium, high |
| Local Qwen3.8 GSQ / Swift and Qwen Next Flash | Validated native configuration | — | — |

Local reasoning is whatever the backend profile admits; do not translate another
vendor's `xhigh` into an arbitrary local thinking budget. Local execution
still depends on memory, availability and lease. No API billing does not
mean no cost in energy, time or resource occupancy.

The additional API alternatives and their roles are in the
[OpenRouter catalog](OPENROUTER_MODELS.md). Admitting these connections does not
give them comparative quality or a measured Cheap/Intermediate/Smart category.

## Use with the existing options

This table is the approved catalog for choosing the implementer. It does not create a
`--profile` flag or a new automatic selector, and it does not change default routing.
Among the options in the table, the existing allowlists, availability and
policies still apply. Exhausted quota can make a route unavailable; a
dynamic percentage does not redefine the model's category.

Examples with subscription CLIs, one explicit candidate per run:

```powershell
# Cheap
tare . --prefer_subscription --implementer openai/codex-cli-gpt-6-luna@xhigh
# Intermediate
tare . --prefer_subscription --implementer openai/codex-cli-gpt-6-1-sol@medium
# Smart
tare . --prefer_subscription --implementer anthropic/claude-opus-5-5-cli@medium
```

The profile applies only to the implementer. Coordinator, planner, explorer,
researcher, judges and auditor keep their rules and settings. Checks,
isolation, financial limits and audit independence still apply.

The local implementer can be pinned in every mode, as automatic routing already
uses it: `tare models --role implementer --via local` lists it, and
`--implementer ninfer/qwen38-gsq@low` (or `@medium`, `@xhigh`) selects it. Only
`--local_only` also lets a local model plan and audit; see
[TUI daily use](TUI_DAILY_USE.md#choose-connections-for-one-launch).

Choose the configuration before dispatch and preserve the saved deployment,
reasoning, history and cache policy. Changing profile is an explicit decision
for a new run or revision; it does not reclassify old attempts or swap
the binding of a call in progress. Do not restart accepted work to change
a label. The real per-role records remain the source of attribution.

## Closing FLOW-19 by scope revision

The operator decided to close the item with these three profiles and the usage policy,
replacing the comparative campaign as the condition for this closure. Request
(translated from Portuguese): "Shall we set up 3 profiles, then? Cheap, intermediate,
smart. Combinations of reasoning and models in a table. That way we close flow 19 with this."

The original design of eight tasks under two treatments (AB8), its criteria,
negative/inconclusive results and limitations remain historical. The current
decision does not claim that 16 runs took place or that a model saved money while
keeping quality. OPE stays out of scope; recorded deterministic selection is 1/0,
never confidence converted into probability.

The limited pair Sol high / Haiku high and the Sonnet medium attempts remain
observations of those models, tasks and limits. They are not transferred to
other reasoning levels or used to declare general winners. Challenger mode
remains optional and off by default, without activating new spending or studies.

Criteria of the adopted scope:

1. Publish the three profiles with explicit model and reasoning.
2. Restrict the choice to the implementer, without depending on quota percentage, and
   preserve the existing policies, leases, bindings and cache.
3. Show usage with the existing interface and keep the current automatic behavior
   when no specific argument is given.
4. Record the scope revision and preserve the earlier research, without claiming
   comparative validation or promoting an automatic economic preference.

References: [challenger mode](CHALLENGER_MODE.md) and
[delegation policy](DELEGATION_POLICY.md).
