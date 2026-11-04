# SPEC-OS-ADAPTIVE-OBSERVATION-001: Read-only behavioral profile, experimental scaffolding and WATCH items

- **Status:** PROPOSED / RESEARCH (not a product prerequisite)
- **Governing ADR:** pending; based on [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md)
- **Target Repository:** `tare.tools.os` (view derived from evidence); `tare.tools.local-labs` optional
- **Version:** 0.1.0
- **Derived from:** FLOW-18, FLOW-20, FLOW-23, FLOW-24

---

## 1. Context & Objective

Impressions such as "GPT over-engineers", "Anthropic explores more", "Google creates
synthetic greens" are valuable operational knowledge, and dangerous if
crystallized as rules. This SPEC treats them as behavioral hypotheses per
regime, translated into observables, derived from normal work, without ever
blocking Work. Principle: **learning can lag behind execution; work cannot
lag behind learning.** Optional measurement is fail-open; Authority is not.

Profile unit: `model × runtime × effort × task class × work format ×
tools × project × verification regime`, never `vendor`.
Mandatory progression: S0 historical reconstruction → S1 instrumentation of new
runs → S2 read-only reducer → S3 shadow routing → S4 one canary. Bandits, OPE
and learned routing come only afterwards.

---

## 2. Interface Contract & Data Structures

Episode (reconstructible from existing receipts, with no new primitive):

```yaml
episode:
  run_id, work_id, deployment, task_class, verification_regime
observed:
  model_calls, tool_calls, wall_ms, cost
  files_changed, unexpected_files_changed, new_dependencies, new_public_surface
  tests_changed, tests_weakened, fixtures_replacing_production, mocks_added
  corrective_required, audit_findings: [unnecessary_abstraction, scope_expansion, ...]
  human_interventions
```

`ImplementerProfileView` (materialized view, read-only): per profile key,
initial dimensions `scope_discipline`, `test_integrity`, `corrective_rate`,
`resource_efficiency`, `cost_to_trust`, `creative_recovery` (experimental),
each with a count, Beta-Binomial (`alpha`, `beta`) for binary events,
`freshness` per model version and `attribution_level` in
`OBSERVATIONAL | CONTRASTIVE | INTERVENTION_SUPPORTED | CAUSAL_QUALIFIED`.

---

## 3. Verifiable Acceptance Criteria

* **`AC-01` Derived view, not a primitive (FLOW-18):** the profile is computed by a reducer from run, settlement, telemetry and audit-finding receipts; no new canonical store; the view can be deleted and regenerated.
* **`AC-02` Fail-open:** a missing or stale profile, or an unavailable reducer, never changes eligibility or blocks a run; a test brings the reducer down during a run.
* **`AC-03` Observables, not adjectives:** each dimension maps measurable signals (files outside the blast radius, new abstractions and dependencies, weakened assertions, fixtures replacing production, correctives after audit); "vendor X is creative" is not a field.
* **`AC-04` Confidence and drift:** a new model version starts with an inherited prior and low `freshness`; the sample unit is the Work, not the call; a small sample size is declared, never hidden.
* **`AC-05` Descriptive review (OBSERVE):** every 10 settled Work, a report on coverage, absences, class, project, requested versus observed runtime and settlement causes; no minimum quota to authorize Work.
* **`AC-06` Scaffolding, not personality (FLOW-20):** an AB8 experiment varies one guidance or verification mechanism with the same model (minimality audit, held-out or mutation check); a cheap intervention that reduces material correctives enters the method; an ineffective one is removed without condemning the vendor.
* **`AC-07` Durable backend only against a gap (FLOW-23, WATCH):** an experimental adapter only after a gap demonstrated by FAULT in the native mechanism; it passes the Semantic Capture Test and the Replay Independence Test; adoption is opt-in with an exit test.
* **`AC-08` Method mining (FLOW-24, WATCH):** offline search (AFlow and similar) produces a candidate recipe compared with the manual method on held-out data; promotion only through the existing authority; never self-promotion to a production Workflow.

---

## 4. Causal Traceability Anchoring (SpecGraph)

* **Parent ADR:** pending.
* **Graph nodes:** `flow-18-*`, `flow-20-*`, `flow-23-*`, `flow-24-*`.
* **Pytest test markers:** `@pytest.mark.verifies("SPEC-OS-ADAPTIVE-OBSERVATION-001", "AC-0n")`.
* **Target files:** `scripts/round_table/report_round_table_telemetry.py` (source of episodes), `relay/profile_view.py` (new, reducer), `docs/guides/INFERENCE_TELEMETRY.md`, `tests/test_profile_view.py`.
* **Precedents kept only as semantics:** `adaptive_router_shadow.py` (`ARCHIVE_RESEARCH`), `role_calibration.py` (observe-only), utility study `U` (DEFER). No module imported.
