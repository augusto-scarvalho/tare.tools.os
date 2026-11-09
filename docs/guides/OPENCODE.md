# OpenCode: models through the Go subscription

[OpenCode](https://opencode.ai) serves many vendors' models behind one API key: the OpenAI chat and Responses APIs
and the Anthropic Messages API, one family per model. Tare reaches it as an HTTP API, independent of the OpenCode
app. The **Go** subscription ($10 a month; Go Plus $40) covers each model up to a monthly dollar allowance, with at
most 20% of it in any 5 hours and 50% in a week. Pay-as-you-go use from the account balance (the Console) comes in a
later change.

## Set it up

Create a key in the [OpenCode Console](https://opencode.ai/console) and store it in this machine's vault. Tare reads
it from standard input, never from the command line:

```powershell
tare setup --credential OPENCODE_API_KEY
tare setup --use api:OPENCODE_API_KEY
```

In the Console, keep **Extra Usage off**. With it on, a model whose allowance is spent keeps answering and charges
the balance instead of refusing.

## Choose a model

```powershell
tare models --provider opencode --role implementer
tare . --implementer opencode/opencode-go-glm-5-3@high
tare . --explorer opencode/opencode-go-deepseek-v4-1-flash@low
```

Every Go model that OpenCode serves with tools on those three APIs has a profile named `opencode-go-<model>`, and
the picker lists it under OpenCode with the connection "OpenCode Go". Each profile is an implementer and explorer
reserve. Planner and auditor roles wait for a live qualification of the model through tare. The reasoning levels
are the ones OpenCode lists for that model; a model with none uses its default.

The author is the model's vendor, not the gateway. GLM 5.3 through OpenCode has the same author identity as GLM 5.3
through OpenRouter, so one never audits the other as an independent reviewer.

Left out for now:

- **Gemini 4 Argon**, which OpenCode serves only through the Gemini API, which tare does not speak yet.
- **Free models**, which OpenCode refuses to clients other than its own app.
- **Muse Spark Contributor** models are listed but wait for the data-use choice in **Settings > Connections**
  (`tare setup --meta-contributor on`), as on OpenRouter: Meta may use prompts, code and responses to improve its
  products. OpenCode also blocks them until its own privacy setting allows endpoints that train on request data.

## Subscription or API

OpenCode is a plan and a balance at once. For now tare counts it as a subscription: `--subscription_only` and
`--prefer_subscription` include it, and `--api_only` leaves it out. The Go models draw on one plan account,
`opencode-go`, so a session on Go delegates within Go and quotes a minimal call as plan usage, not dollars. A limit
recorded for one model leaves the other Go models usable, since each has its own allowance.

## What tare sends and records

Each request carries `x-opencode-session`, the tare session's opaque key, so OpenCode can route and cache by
conversation. It also carries `User-Agent: tare/<release>`. On the final call, a delegated role normally names the
delivery tool. It does so only for models whose OpenCode entry accepts a named `tool_choice`, and the others are
reminded by the budget notice.

Usage comes from OpenCode's own counters (`usage.opencode.tokens`: input without cache, cache reads and writes,
output with reasoning). The charge is `usage.cost`, which is $0 for Go use within the allowance. The receipt keeps
the billing source (`go`, `go-plus`, `balance`, ...), the list price and the allowance each call used.

## Refresh the profiles

OpenCode changes its list often. `scripts/ops/opencode_profiles.py --plan go` rewrites the profiles from the public
list and adds roster records and role rows for new models; a role row edited by hand is kept. Review the diff
before committing: a profile is metadata admission, not measured delivery quality.

Sources: [OpenCode Go](https://opencode.ai/docs/go) and [inference API](https://opencode.ai/docs/console/api/inference).
