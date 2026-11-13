# Native deployments

Local profiles name `http://local-gateway:8080`; tare resolves it to this machine's model gateway
(`TARE_LOCAL_GATEWAY`, then `local-gateway.json` in tare's storage with `{"origin": "http://host:port"}`,
else `http://127.0.0.1:8080`). A host name works there too and its models count as local, like any endpoint
on a private or Tailscale address, a name without a dot or one ending in `.local`, `.internal`, `.lan`,
`.home.arpa`, `.localhost` or `.ts.net` (`relay/challenger.py` `local`, no DNS query). The slop.cpp fleet profiles
use `slop-fleet-v1`.
`local-qwen` (and the `qwen38` alias) is the native default and resolves to `qwen38-gsq.json`; the slop
Qwen3.8 Q4_K_XL profiles (`qwen38.json`, `qwen38-no-cache.json`) were retired on 2026-09-29. Bedrock Opus
uses the direct Converse profile.
Use one with `tare-ops agent --native-deployment <file>` and an explicit
scope/session as described in the entry contract.

Window, template and request settings were qualified on 2026-09-10.
See results, deployment and limitations.
Changing the server window or template requires reviewing the profile; drift
is refused, never silently corrected by the consumer. The endpoint selects
and may load the requested model, replacing the resident model.

HauhauCS (Qwen3.8-derived),
Fable-TC and Qwen3.6 MoE retain their explicit instruct canary settings and a
1024-token output reserve. These are bounded operational profiles, not a
claim that these settings maximize each model's coding quality.

Existing sessions retain their saved profile, including its cache choice, on resume. Cache reuse
does not shrink context occupancy and is not guaranteed after eviction, model switching or compaction.

`swift-next.json` reaches Swift 1.5 Flash-Next (Strata, 128K) through the same gateway with estimated
counting; it holds ~43 GB of host RAM while loaded. `local-auto.json` is the `qwen38-gsq` profile plus
`native_host_modes`: a new session asks the gateway's `/v1/fleet/recommend` once and uses `swift-next`
only while its host RAM is free, otherwise (or if the gateway does not answer) Qwen3.8 GSQ. The session
saves the resolved profile; resume never asks again.

`qwen38-gsq.json` and `swift27b.json` reach Qwen3.8 27B and Swift 1.5 27B GSQ-RCO IQ3_S on NInfer-3090
0.11 through the same gateway, with 128K windows and estimated counting (NInfer has no `/tokenize`).
Qwen3.8 keeps the low-effort request (the server caps thinking at 2,048 tokens); Swift runs at
`xhigh` without a cap, as UkisAI evaluates it, with a 16K output reserve.

`tare-auto.json` is the TUI's **tare auto** entry: the `qwen38-gsq` profile plus `native_auto_routes`.
A new session runs on the first available implementer route of the `tare serve` policy (local, then
cheap, then intermediate API price; `<storage>/serve/routing.json` overrides it) and records the tier.
When a turn ends blocked, exhausted or rejected, the TUI continues the conversation on the next tier
through provider continuation, without the review panel an explicit provider change shows.

There is no private tokenizer tunnel or credential in these files. The
counter and generation use the same public endpoint. Usage fields absent
from the provider remain unknown. See the current installation and named-profile commands in
the migration record.
