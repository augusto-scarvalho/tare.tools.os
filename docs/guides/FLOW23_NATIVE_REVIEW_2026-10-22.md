# FLOW-23 native review observation - 2026-10-22

Compact evidence for [the conditional decision](FLOW23_BACKEND_DECISION.md). This is documentation, not runtime configuration. Historical qualification, current checks and input-driver correction have separate scopes.

```json
{
  "schema": "tare.tools/backend-native-review/1",
  "observed_at": "2026-10-22",
  "decision": "PRESERVE_NATIVE",
  "external_trial": "NOT_TRIGGERED",
  "backend_adoption": "NOT_PROPOSED",
  "current_scope": "Prepared Work DAG, work.execute/v1, serial incorporation and existing owner recovery.",
  "current_os_commit": "75ed084055dd7e72bb71765f9441b14c198a61f1",
  "source_sha256_git_blob": {
    "relay/work_sequence.py": "490aa24c2a25363f5ead1ed588026064946383b3f7ffa032c6bedc807255c88a",
    "relay/workflow_reducer.py": "97a37809fdf5f547b793ac15ce23921c2f8a1c4822390dfab11dff2368e38b91",
    "relay/workflow_ir.py": "fbc1e8eba14acb130344e247057fa7d95a649effdf8a67f0a1311bb859e52960",
    "relay/console_recovery.py": "99f96063faea20a84bc47c52f0b18e0c0614ba0eda0b5b58d91e245556584b18",
    "relay/console_mutation.py": "da048d1ad7faee6ebbd0d90684028e3e7f06bf09ddee124d03326e7fdc08c825",
    "relay/native_agent.py": "adda78e818d0a11d8af15f73b6b3f1a45bb1a188fd7b9632ce476fd1f26d4679",
    "tests/test_workflow_dispatch.py": "4651e93bd05d43ae8baf5abb8385f96f8e61c16fe3cf708fbf7514a0e1785c90",
    "tests/test_native_effect_recovery.py": "3b4e75e2bdf8d611ddb4bacd167996db4c383f1e0ed3900a9fcc9968380a0ac8"
  },
  "historical_fault_qualification": {
    "run_id": 39089721808,
    "url": "https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/39089721808",
    "candidate": "9410309c3586325a3e64024a64eebdf094751c0a",
    "merge_commit": "ba7bfe7e86249f58f5b2f9a66bbfc8f872b524da",
    "points": [
      "reservation-before-dispatch",
      "effect-before-receipt",
      "receipt-before-transition",
      "landing-before-backlog",
      "expiry-rebind",
      "cancel-late-result"
    ],
    "schedules": [
      "resume",
      "cancel-before-resume",
      "contended-resume"
    ],
    "platforms": {
      "windows-host": {
        "passed": 18,
        "artifact_id": 12123876441,
        "artifact_sha256": "0b3865f26b7157524802e0bf9ae29a24b866cfc17d32b393f1222d6e5ff22e44",
        "junit_sources": [
          {
            "name": "quick.xml",
            "sha256": "a4c23018067f18b5b1c15c17a8d5c8b79ddffe4c1847ffbb93638d701b8dab99"
          },
          {
            "name": "regression.xml",
            "sha256": "d5d261c44e5ebb4e45deaf641c89bae4165aa5fa9280d8bab8c7d81f18687045"
          }
        ],
        "case_manifest_sha256": "db7deb3e7b3df4e82d12479df366ef9ff5c8d93f0515a83350b7f786e8030276"
      },
      "linux-wsl2": {
        "passed": 18,
        "artifact_id": 12123140998,
        "artifact_sha256": "8e261e5c35218e5526bc464d90c9a2c3b26c9bdedbf02103e4a6883daf8d066b",
        "junit_sources": [
          {
            "name": "quick.xml",
            "sha256": "eb4622512daae9c4779336768898ac2134a81b4627ef6054c4d06db5389ba11f"
          },
          {
            "name": "regression.xml",
            "sha256": "eb7b41019755e99ed96bd439f3119a97865798d64df36b72a789b19c544d3ba4"
          }
        ],
        "case_manifest_sha256": "db7deb3e7b3df4e82d12479df366ef9ff5c8d93f0515a83350b7f786e8030276"
      }
    },
    "scope": "Actual PR131 JUnit proves its own historical candidate, not a new current two-platform matrix."
  },
  "current_windows_named_checks": {
    "interrupted-cut-replay": {
      "passed": 1,
      "junit_sha256": "ab425e42ce203516589102073af5893fd8c8e94a8e1b0374bb1659678ad2fae1"
    },
    "negative-and-recovery": {
      "passed": 47,
      "junit_sha256": "36e84ad5bcddf338193b7ddac3dd3c71bb59c512fef794b3c42c1e8c55197cf8"
    },
    "public-seed": {
      "passed": 1,
      "junit_sha256": "d5ba4a07f3bc511c2cd53516a5789a16c22dee82ee979f07ff937150ab2f7222"
    }
  },
  "interrupted_current_matrix": {
    "status": "INCOMPLETE_WRAPPER_TIMEOUT",
    "outer_timeout_seconds": 300,
    "reported_passing_markers": 17,
    "named_per_case_receipts_available": false,
    "native_assertion_failure_recorded": false,
    "full_matrix_claim_from_this_batch": false,
    "interrupted_case_replayed": "cancel-late-result-contended-resume",
    "case_selection_basis": "Explicit parameter decorator ordering in the unchanged selected function; no reorder plugin.",
    "root_correction": "Private test driver now keeps verbose case names and a 1800s phase budget; per-test pytest timeouts remain in force.",
    "scope": "Qualification-driver budget failure, not evidence of a native persistence defect."
  },
  "public_tui": {
    "status": "PASS",
    "method": "public installed TUI via ConPTY keys; local filtered assertions",
    "synthetic_case": true,
    "state": "BLOCKED",
    "pending_work": "work-one",
    "preserved_settled_work": "work-two",
    "owner_receipts_unchanged": true,
    "journals_and_usage_unchanged": true,
    "workflow_unchanged": true,
    "scripted_provider_calls_seed": 5,
    "new_provider_calls": 0
  },
  "native_safety_boundary": "UNKNOWN effects and mismatched receipts require owner reconciliation, not automatic retry or a new execution.",
  "cost": {
    "external_services_added": 0,
    "external_prototype_executed": false,
    "native_vs_external_total_cost_usd": null,
    "permanent_external_cost_usd": null,
    "reason": "No reproduced unmet native invariant selects a backend or justifies a costed disposable adapter yet."
  },
  "reopen_when": "One reproducible unmet native recovery invariant or ordinary-delivery obligation remains after a bounded owner fix assessment; then compare one disposable candidate on matching traces, history export/removal and total install/operate/migrate/exit cost.",
  "limits": [
    "No universal no-gap or external exactly-once claim.",
    "No backend equivalence, price comparison or SDK-independent export was executed.",
    "The initial local matrix lacks a final named JUnit report and is not counted as 18 current passing cases."
  ],
  "retention": {
    "maintained_destination": "This observation, FLOW23_BACKEND_DECISION.md and versioned native regressions",
    "private_archive_ceiling_bytes": 67108864,
    "review_after": "2026-11-22",
    "remote_artifact_zips_retained": false
  },
  "windows_input_correction": {
    "owner": "OS console_windows_input",
    "cause": "Parser timeout ran before already queued split escape records were consumed.",
    "change": "Consume queued console records first; advance idle timeout only when no records are available.",
    "regression_failed_before_fix": true,
    "windows_input_tests_passed": 6,
    "before_junit_sha256": "3eb897ac9e8bd2cf0a5c43dccf668643b222dc82a9be6f044f5b8f9575dcc81f",
    "after_junit_sha256": "7b7fc30935e41af22a746cb8aca1e2442ec9c068c6a1751d548d7df826136147",
    "public_source_console": {
      "status": "PASS",
      "method": "Public source console, ConPTY paste and visible Start control",
      "request_sha256_utf8": "a75ac66acebc0ec3c91c41eacda0e321c5207d5572b414fc5515f63a2abe9d2d",
      "persisted_request_matches_complete_draft": true,
      "model": "gpt-6.1-sol",
      "model_calls": 5,
      "model_call_ceiling": 8,
      "invocations": 1,
      "earlier_unacknowledged_attempt_native_sessions": 0,
      "delivery_status": "PROPOSED",
      "session_id": "5478991b1da246e2b66d09fcbcd33bfd",
      "invocation_id": "39aea8d8207241d7a11b5f502fe1d43b",
      "request_ack": "Native invocation persisted before monitor starts",
      "scope": "Source console with queued-input fix and pinned installed dependencies; not yet installed-product cutover."
    },
    "first_unacknowledged_console_cleanup": {
      "natural_exit": false,
      "cleanup_verified": true
    },
    "source_console_cleanup": {
      "natural_exit": true,
      "cleanup_verified": true
    },
    "final_focused_checks": {
      "passed": 40,
      "scope": "Windows input, paste, navigation, native console and official disposition compliance.",
      "junit_sha256": "3a2e018b5d554845a489afeab62c3a31a4b1182e086c6807719c8f39f706adde",
      "includes_the_six_windows_input_tests": true
    }
  }
}
```
