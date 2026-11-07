# Jev consent qualification — 2026-11-07

## Scope and provenance

This record documents the consent-classification correction in `relay/consent_decisions.py` and the measurement supplied with the implementation request. The reported model is **Jev 1.14.0** (`jev-1.14.0`). These model measurements were not reproduced in this session; deterministic regression tests are not a new model qualification run.

## Supplied measurements

The supplied evaluation contains **54 cases**. Preserve the different denominators:

| Variant | Decision policy | Reported result | Denominator |
| --- | --- | --- | --- |
| V0 | Original wording, uniform 0.9 threshold | 40/54 | Entire 54-case set |
| V2 | Plain-language options and multilingual instruction, uniform 0.9 threshold | 45/54 | Entire 54-case set |
| V2 asymmetric | V2 wording, allow >= 0.8 and deny >= 0.9 | 24/30 yes-pass | 30 affirmative cases, not the entire set |

The supplied maximum **non-yes allow=0.45** means that no refusal or conditional answer in the reported sample exceeded 0.45 of allow weight. It is an observation about that sample, not a universal safety bound. The 24/30 yes-pass result must not be compared as though its denominator were the 54 cases used for 40/54 and 45/54; no full-set asymmetric score is supplied here.

## Implemented contract and rationale

The judge packet still contains only `question`, `options` and `state`. The authorization question and user response are JSON data in `state`, explicitly treated as data rather than instructions. The instruction now says that users may answer in any language, often with one word: a bare affirmative means allow and a bare negative means deny. The three plain-language options describe agreement as asked, refusal, and clarification for questions, hesitation, postponement or changed conditions/limits.

After existing verdict validation, allow at 0.8 or higher returns `yes`; deny at 0.9 or higher returns `no`; everything else remains `pending`. Thus allow 0.8/0.85 releases, allow 0.79 waits, deny 0.85/0.89 waits, deny 0.9/0.95 refuses, and unclear always waits.

The asymmetry addresses affirmative responses stranded by the uniform 0.9 threshold while retaining the stricter threshold for a definitive refusal. Pending responses ask for clarification and do not grant access. The supplied non-yes maximum of 0.45 supports this bounded choice, but does not prove that future refusals or conditions cannot receive higher allow weights. Model weights are not asserted to be empirically calibrated probabilities.

The `judge` rule has no lexical authorization shortcut: even `sim` reaches the judge under the explicit and default rule, without consulting `folder_access.allows`. The separate fallback rule, isolated packet, malformed-verdict handling, unavailability behavior and hosted/local policy restrictions are unchanged.

## Regression evidence and limits

`tests/test_consent_decisions.py` checks the literal instruction and options, threshold boundaries, explicit/default judge handling of `sim` with lexical lookup forbidden, and folder grant/decline effects for hosted and local verdicts. Existing fallback, isolation, malformed-verdict and availability/policy tests remain in place. `tests/test_disposition_manifest_compliance.py` checks the generated manifest against module bytes and the Git index.

These deterministic checks establish the decision plumbing, not real-model performance. The supplied aggregate measurements do not establish universal multilingual coverage, safety against all adversarial inputs, empirical calibration of weights, or performance outside the reported sample. No raw per-case results or new live-model reproduction are claimed by this record.
