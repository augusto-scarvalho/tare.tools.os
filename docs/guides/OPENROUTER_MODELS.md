# OpenRouter: models and routes by role

Selection authorized by the operator on 2026-11-01. These are connection contracts
and role preferences; registering a model does not demonstrate comparative delivery quality.
The profiles use the OpenRouter key already stored in the system vault.

| Model | Deployment | Roles | Registered reasoning |
| --- | --- | --- | --- |
| Kimi K3.1 Pro | `openrouter-kimi-k3-1-pro` | Planner, plan auditor and auditor | high |
| Muse Wonder | `openrouter-muse-wonder` | Planner, plan auditor and auditor | high |
| DeepSeek V4.1 Flash 1017 | `openrouter-deepseek-v4-1-flash-1017` | Implementer, explorer and researcher | low, high, max |
| MiMo V2.6 Pro | `openrouter-mimo-v2-6-pro` | Implementer, explorer and researcher | default, native |
| GLM 5.3 | `openrouter-glm-5-3` | Implementer, explorer and researcher | low, high, max |
| GLM 5.3 Flash | `openrouter-glm-5-3-flash` | Implementer, explorer and researcher | high |
| Mistral Large 4 | `openrouter-mistral-large-4` | Implementer, explorer and researcher | high |
| Step 5 Preview | `openrouter-step-5-preview` | Implementer, explorer and researcher | high |
| Muse Spark 1.3 Contributor | `openrouter-muse-spark-1-3-contributor` | Implementer, explorer and researcher | high; requires a data-use choice |
| Grok 4.7 | `openrouter-grok-4-7` | Implementer, explorer and researcher | high |
| Ling 3.1 Flash | `openrouter-ling-3-1-flash` | Implementer, explorer and researcher | default, native |
| Qwen3.8 Omni Flash | `openrouter-qwen3-8-omni-flash` | Implementer, explorer and researcher | default, native |

The initial registration is conservative on levels. `high` alone does not mean the
vendor offers no other levels; other values need a source and a qualification
of that connection. `default` keeps the native reasoning, without sending an invented
`effort`. Planners still require at least `high`. These registrations do not
widen the coordinator's model policy or the Round Table chairs.

```powershell
tare models --provider openrouter --role implementer
tare . --prefer_subscription --implementer openrouter/openrouter-deepseek-v4-1-flash-1017@high
tare . --prefer_subscription --researcher openrouter/openrouter-mimo-v2-6-pro@default
tare . --prefer_subscription --planner openrouter/openrouter-kimi-k3-1-pro@high
```

An explicit pin chooses that connection, within the run's restrictions. It
does not change the coordinator or admit a fallback to another model. `--subscription_only`
still excludes these APIs. The registered vendor (Moonshot, Meta, etc.) is
distinct from the OpenRouter gateway: audit independence uses the vendor and
the lineage, including between two models that go through the same gateway.

## Provider and precision

Each initial profile restricts the call to a specific endpoint. DeepSeek uses
the `1017` snapshot, keeping the old profile for sessions already saved. The profiles
do not change persisted deployments. Context limits are those of the chosen
endpoint: for example, Ling uses the 1M Novita route and does not inherit 1M when
you manually choose another 262K endpoint.

Check and change the local choice with the existing profile commands:

```powershell
tare profiles route openrouter-deepseek-v4-1-flash-1017
tare profiles route openrouter-deepseek-v4-1-flash-1017 --provider relace/fp4 --quantization fp4
tare profiles route openrouter-deepseek-v4-1-flash-1017 --reset
```

Changes apply to new selections. Use the full catalog slugs;
`deepinfra` can cover several variants, while `deepinfra/fp8` restricts the variant.
You can repeat `--provider` and enable `--allow-fallbacks` to allow recovery
within the list. Without it, the initial policy keeps the endpoint and reports the
unavailability. The command saves preferences; it does not run inference or prove
that the chosen combination is available.

FP4/FP8 can be the model's original configuration, including from training or QAT.
There is no general quality order `BF16 > FP8 > FP4`. Training format,
checkpoint, additional quantization, activations and KV cache are separate facts.
An endpoint label does not prove those origins. Kimi is admitted on the vendor's
MXFP4 route; when the origin is not documented, it stays unknown.
The observations separate the requested precisions from the provider reported in the response;
they do not claim to have measured the precision of the remote weights.

## Cache, research and data

The Kernel sends a stable session identity to OpenRouter. Muse Wonder and
Contributor also use a stable per-profile key to group the application
prefix, following the Meta contract. Grok uses the existing Responses codec, with a
per-conversation key and replay of the opaque reasoning. No new keys are created
on each call. The API
researcher uses the same Runtime, history and tool schema across questions.
Its only effects are search and URL reads; completion uses `tare_deliver`
with the structured report. Sources and anchors still go through the host's read.
It uses the search APIs already configured; registering a model does not give it
built-in search. There is no edit tool or project command in this role.

Cache still depends on prefix, endpoint, TTL and vendor support.
Switching endpoints can result in a cold cache. Read and write rates and counters
are separate; missing fields stay unknown. Live prices
of pinned routes come from the selected endpoint, without using the lowest price of
a different quantization as if it were its price. No change forces
`cache_control` on a model without a documented contract for it.

In the synthetic qualification of 2026-11-01, all 12 models read a reference through
a tool and delivered the observed marker. The Meta key raised cache reads
to about 96% of the second input; Grok Responses also reused about
96%, against 1,152 tokens on the Chat path. These are transport/cache observations,
not a comparison of delivery quality. Sources:
[Meta](https://dev.meta.ai/docs/prompt-caching) and
[xAI](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits).

Muse Spark Contributor stays out of selection until the explicit choice in
**Settings > Connections**: Meta may use prompts, code and responses to
improve its products. This operator's authorization does not become the default for
other users. You can also set `tare setup --meta-contributor on`
or `off`. The exception uses `data_collection: allow` only for Contributor; the
other profiles request `deny`. OpenRouter account rules still take precedence.

Official sources: [routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[cache](https://openrouter.ai/docs/guides/best-practices/prompt-caching) and
[Contributor](https://openrouter.ai/meta/muse-spark-1.3-contributor).
