# Scoped conversation checks (2026-10-27)

An ordinary X2 implementation had 45 passing selected tests, but the host also
ran the project's automatically discovered `python -m pytest -q` verifier. That
extra invocation failed during pytest's cleanup of a shared `pytest-current`
directory. The candidate was retained, independent audit/application remained
blocked, and the following attempt was cancelled before another implementer
started. No shared temporary directory was removed or granted additional access.

The compiler now narrows the exact automatically generated conversation `tests`
verifier to the Python test paths already in the saved unit. The same compiled
verifiers govern baseline, worker, post-checks and final application. An identical
generated `pytest-tests` command is not run a second time. Custom named checks
and non-conversation execution retain their obligations; this is not a full
regression claim.

Recognized unprotected host pytest checks use a candidate-owned `--basetemp`,
including named checks, without replacing an explicit `--basetemp` in either
supported spelling. Protected checks retain their selected execution port and
its owned scratch. The saved source configuration and planner artifact are not
rewritten, and neither command authorization nor filesystem grants are expanded.

An offline lifecycle regression uses a passing selected test and an unrelated
failing test, a fake implementer and real pytest subprocesses. It records every
host invocation through final application: only the selected test runs, each
uses owned scratch, and the original configuration remains unchanged. Additional
controls preserve custom checks, explicit scratch and ordinary non-conversation
behavior. Live recovery must still reverify the retained candidate and obtain
its normal independent audit; these compiler tests do not approve that delivery.
