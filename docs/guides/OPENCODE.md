# OpenCode: subscription and pay-per-token API

[OpenCode](https://opencode.ai) serves many vendors' models behind one API key: the OpenAI chat and Responses APIs
and the Anthropic Messages API, one family per model. Tare reaches it as an HTTP API, independent of the OpenCode
app, as two providers:

| Provider | Pays with | Profiles | Pin | Execution flags |
| --- | --- | --- | --- | --- |
| OpenCode Subscription | a plan (Go, Go Plus, ...): each model has a monthly dollar allowance, at most 20% of it in any 5 hours and 50% in a week | `opencode-sub-<model>` | `opencode-sub/<profile>@<level>` | a subscription: `--subscription_only`, `--prefer_subscription` |
| OpenCode API | the account balance, per token | `opencode-api-<model>` | `opencode-api/<profile>@<level>` | an API: `--api_only`, spend caps |

Each reply says which plan it was billed to (`billing_source`). The subscription profiles accept `go`, `go-plus` and
`free`; a plan that reports another name (OpenCode does not document one for Teams) stops the session after one call
with a notice naming it: add that name to `BILLING` in `scripts/ops/opencode_profiles.py` and regenerate.

## Set it up

Create a key in the [OpenCode Console](https://opencode.ai/console) and store it in this machine's vault. Tare reads
it from standard input, never from the command line:

```powershell
tare setup --credential OPENCODE_API_KEY
```

In the Console, keep **Extra Usage off**. With it on, a subscription model whose allowance is spent keeps answering
and charges the balance instead of refusing. Two account settings gate some models: DeepSeek is hosted outside
Europe and the United States and needs the **Global** region, and models that train on request data need **Allow
paid endpoints that train on request data**.

## The models

Tare registers only the operator's selection, kept in `scripts/ops/opencode_profiles.py` (`MODELS`):

| Provider | Models |
| --- | --- |
| Subscription | Claude Haiku 5.5, DeepSeek V4.1 Flash, GLM 5.3 Flash, GPT-6 Luna, Grok 4.7, Kimi K3, MiMo V2.6 Flash; Gemini 4 Argon waits for a Gemini API codec |
| API | Claude Fable 5.2, Claude Sonnet 5.5, Gemini 3.8 Flash, GPT-6 Astra, GPT-6.1 Sol, Mistral Large 4, Muse Ember 1.0: selected, without profiles until the account has a balance |

```powershell
tare models --provider opencode-sub --role implementer
tare . --implementer opencode-sub/opencode-sub-glm-5-3-flash@high
```

Each profile is an implementer and explorer reserve marked `qualified: false`: an exact pin reaches it, and automatic
selection skips it (`NOT_QUALIFIED_PIN_ONLY`) until a live qualification through tare flips the mark. The reasoning
levels are the ones OpenCode lists for that model. A profile starts at `high` when the model lists it; otherwise it
runs at the provider's default until you choose a level.

The author is the model's vendor, not the gateway. GLM 5.3 Flash through OpenCode has the same author identity as
GLM 5.3 Flash through OpenRouter, so one never audits the other as an independent reviewer.

Free models (`-free`) are left out: OpenCode serves them only to its own app ("Free usage limit reached" from the
API, "Model is unavailable" from the subscription endpoint). Their paid versions are the same models.

## Subscription accounting

The subscription models draw on one plan account, `opencode-sub`, so a session on the subscription delegates within
it and quotes a minimal call as plan usage, not dollars. OpenCode documents a dollar allowance per model, but its
5-hour window is the plan's: once it was spent on one model, every paid model answered the same refusal with the same
reset, and only free models still worked (observed 2026-11-09). Plan usage takes no dollar reservation under tare's
spend caps, which holds only with Extra Usage off.

When the window is spent, OpenCode refuses with HTTP 429 `GoUsageLimitError` and a Retry-After of hours. Tare then
leaves every subscription model until the Retry-After time (`PLAN_USAGE_LIMIT`: "The opencode-sub account behind this
model has no usage left until HH:MM"); `tare auto` and `tare serve` skip them and an audit falls back to its next
route. The subscription profiles declare the billing sources they may be charged to
(`free`, `go`, `go-plus`): if Extra Usage is on and a reply is billed to the balance anyway, that one call is charged,
the session stops there, and the subscription is left for five hours, the shortest allowance window. This holds for a context
summary as for any model call. API profiles will carry the list price as a
tariff when they ship, so spend caps can reserve dollars for them.

Sessions saved on the earlier `opencode-go-*` profiles cannot be resumed (their author records are gone and they
would be taken for the API); start them again on an `opencode-sub-*` profile.

## What tare sends and records

Each request carries `x-opencode-session`, the tare session's opaque key, so OpenCode can route and cache by
conversation. It also carries `User-Agent: tare/<release>`. On the final call, a delegated role normally names the
delivery tool. It does so only for models whose OpenCode entry accepts a named `tool_choice`; the others are reminded
by the budget notice. A Messages profile (Claude Haiku) also marks its prompt for caching (`cache_control`), which
that API requires; the other families cache on their own.

Usage comes from OpenCode's own counters (`usage.opencode.tokens`: input without cache, cache reads and writes,
output with reasoning). The charge is `usage.cost`: $0 for subscription use within the allowance, the balance charge
for API use. The receipt keeps the billing source, the list price and the allowance each call used.

## Refresh the profiles

`python scripts/ops/opencode_profiles.py --plan sub` (or `--plan api`) rewrites that provider's profiles from
OpenCode's public list for the models `MODELS` names, removes the profiles it no longer names with their role rows,
and adds roster records and role rows for new ones; a role row edited by hand is kept. Roster records stay: remove by
hand a record nothing else uses. A list without any named model changes nothing. Review the diff before committing: a profile
is metadata admission, not measured delivery quality.

Sources: [OpenCode Go](https://opencode.ai/docs/go) and [inference API](https://opencode.ai/docs/console/api/inference).
