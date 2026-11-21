"""Offline tests for SPEC-OS-SHIP-001 AC-08 and AC-09 (rule-based CI triage and redacted untrusted receipts)."""
import hashlib
import json

import pytest

from relay import ci_watch, ship_triage


@pytest.mark.parametrize('kwargs,expected_class,expected_rule', [
    ({'log': 'Checkout\tSubmodules\t2026-11-20T01:00:00Z fatal: remote error: upload-pack: not our ref deadbeef'},
     'pinned_ref_missing', 'upload_pack_not_our_ref'),
    ({'run': {'databaseId': 11, 'conclusion': 'cancelled', 'workflowName': 'CI', 'headBranch': 'feat/x'},
      'newer_run_exists': True},
     'superseded', 'cancelled_with_newer_run'),
    ({'run': {'databaseId': 12, 'conclusion': 'startup_failure'}},
     'infrastructure', 'startup_failure'),
    ({'actions_incident': 'Degraded performance on GitHub Actions runners'},
     'infrastructure', 'github_actions_incident'),
    ({'log': 'build\tSet up job\t2026-11-20T01:00:00Z ##[error]The runner has received a shutdown signal.'},
     'infrastructure', 'setup_job_failure'),
    ({'log': 'ci\tInstall\t2026-11-20T01:00:00Z curl: (6) Could not resolve host: github.com'},
     'infrastructure', 'dns_or_tls_error'),
    ({'run': {'status': 'queued'}, 'queued_past_limit': True, 'queued_reason': 'no matching runner'},
     'queued', 'queued_past_limit'),
    ({'missing_required_checks': ['Tests (windows)', 'changelog']},
     'required_check_missing', 'missing_required_check'),
    ({'log': 'ci\tPublish\t2026-11-20T01:00:00Z HttpError: Resource not accessible by integration (HTTP 403)'},
     'permission', 'permission_denied'),
    ({'hook_message': 'Preflight refused: commit 12345678: author email me@example.org is not noreply'},
     'hook_refused', 'hook_refused'),
    ({'conflict_files': ['relay/ci_watch.py']},
     'conflict', 'unresolvable_conflict'),
    ({'log': 'ci\tTest\t2026-11-20T01:00:00Z FAILED tests/test_a.py::test_one - AssertionError',
      'base_failing_ids': ['tests/test_a.py::test_one']},
     'base_red', 'base_branch_failing'),
    ({'log': 'ci\tTest\t2026-11-20T01:00:00Z FAILED tests/test_a.py::test_one - AssertionError',
      'passed_same_tree': True},
     'flaky', 'same_tree_passed_before'),
    ({'log': 'ci\tTest\t2026-11-20T01:00:00Z FAILED tests/test_a.py::test_one - AssertionError\n'
             'ci\tTest\t2026-11-20T01:00:01Z FAILED tests/test_b.py::test_two - KeyError'},
     'regression', 'failed_test_ids'),
    ({'log': 'ci\tStep\t2026-11-20T01:00:00Z mysterious exit code 137'},
     'unclassified', 'unmatched'),
])
def test_triage_classifies_all_eleven_rules_and_unclassified(kwargs, expected_class, expected_rule):
    result = ship_triage.classify(**kwargs)
    assert result['class'] == expected_class
    assert result['rule'] == expected_rule
    assert result['untrusted'] is True
    assert result['evidence']
    raw = '\n'.join(result['evidence']).encode('utf-8')
    assert result['excerpt_bytes'] == len(raw)
    assert result['excerpt_sha256'] == hashlib.sha256(raw).hexdigest()


def test_triage_precedence_first_rule_in_spec_order_wins():
    # pinned_ref_missing beats permission and regression when all signals appear together.
    mixed = (
        'ci\tCheckout\t2026-11-20T01:00:00Z fatal: remote error: upload-pack: not our ref abc123\n'
        'ci\tStep\t2026-11-20T01:00:01Z HTTP 403 Resource not accessible by integration\n'
        'ci\tTest\t2026-11-20T01:00:02Z FAILED tests/test_a.py::test_x - AssertionError\n'
    )
    assert ship_triage.classify(log=mixed, passed_same_tree=True)['class'] == 'pinned_ref_missing'
    # base_red beats flaky and regression.
    only_fail = 'ci\tTest\t2026-11-20T01:00:02Z FAILED tests/test_a.py::test_x - AssertionError\n'
    assert ship_triage.classify(
        log=only_fail, base_failing_ids=['tests/test_a.py::test_x'], passed_same_tree=True
    )['class'] == 'base_red'


def test_regression_receipt_builds_pytest_or_declared_verification_checks():
    log = (
        'ci\tTest\t2026-11-20T01:00:00Z FAILED tests/test_a.py::test_one - AssertionError\n'
        'ci\tTest\t2026-11-20T01:00:01Z FAILED tests/test_b.py::test_two[x] - ValueError\n'
        'ci\tTest\t2026-11-20T01:00:02Z FAILED tests/test_a.py::test_one - AssertionError\n'
    )
    default_triage = ship_triage.classify(log=log)
    assert default_triage['class'] == 'regression'
    assert default_triage['verification_checks'] == [
        ['python', '-m', 'pytest', 'tests/test_a.py::test_one', 'tests/test_b.py::test_two[x]', '-q']
    ]
    custom_triage = ship_triage.classify(log=log, verify_command=['npm', 'test', '--'])
    assert custom_triage['verification_checks'] == [
        ['npm', 'test', '--', 'tests/test_a.py::test_one', 'tests/test_b.py::test_two[x]']
    ]


def test_triage_excerpt_redacts_tokens_and_bounds_lines_and_bytes(tmp_path):
    # AC-09: token-like strings stripped to [redacted], max 40 lines, max byte size, marked untrusted.
    gh_pat = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6'
    openai_key = 'sk-proj-' + 'xY9zW8vU7tS6rQ5pO4nM3lK2jI1hG0fE'
    bearer = 'Bearer ' + 'eyJhbGciOiJIUzI1NiJ9.payload1234567890'
    url_cred = 'https://x-access-token:ghs_SecretToken1234567890123456@github.com/o/r.git'
    lines = [
        f'ci\tStep\t2026-11-20T01:00:00Z Error: failed with {gh_pat} and {openai_key}',
        f'ci\tStep\t2026-11-20T01:00:01Z Error: Authorization: {bearer} url={url_cred}',
    ] + [f'ci\tStep\t2026-11-20T01:00:{i:02d}Z Error: extra failure line {i} ' + ('z' * 400) for i in range(60)]
    triage = ship_triage.classify(log='\n'.join(lines))
    assert triage['untrusted'] is True
    assert len(triage['evidence']) <= ship_triage.MAX_EVIDENCE_LINES
    assert triage['excerpt_bytes'] <= ship_triage.MAX_EXCERPT_BYTES
    joined = '\n'.join(triage['evidence'])
    for secret in (gh_pat, openai_key, 'eyJhbGciOiJIUzI1NiJ9', 'ghs_SecretToken1234567890123456'):
        assert secret not in joined
    # Verify redaction directly on the first two lines as well.
    redacted_two = ship_triage.bound_evidence(lines[:2])
    text_two = '\n'.join(redacted_two['evidence'])
    assert text_two.count('[redacted]') >= 4
    for secret in (gh_pat, openai_key, 'eyJhbGciOiJIUzI1NiJ9', 'ghs_SecretToken1234567890123456'):
        assert secret not in text_two

    path = ship_triage.write_receipt(tmp_path, repo='acme/os', pr=42, run_id=9001, attempt=2,
                                     head_sha='deadbeef', triage=triage, transition='WAITING->RED')
    assert path == tmp_path/'receipts'/'9001-2.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['schema'] == ship_triage.RECEIPT_SCHEMA
    assert saved['repo'] == 'acme/os' and saved['pr'] == 42 and saved['run_id'] == 9001 and saved['attempt'] == 2
    assert saved['head_sha'] == 'deadbeef' and saved['untrusted'] is True and saved['transition'] == 'WAITING->RED'
    assert saved['excerpt_sha256'] == triage['excerpt_sha256']


def test_shepherd_and_settle_persist_redacted_triage_receipts_when_failures_are_seen(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: None)
    secret = 'ghp_' + '9876543210abcdefghijKLMNOPQRSTUV'
    views = iter([{'mergeStateStatus': 'BLOCKED'}, {'state': 'MERGED'}])

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'acme/os', 'isArchived': False})
        if args[:2] == ['pr', 'view']:
            return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feat',
                               'headRefOid': 'abc1234', 'baseRefName': 'main', 'url': 'https://github.com/acme/os/pull/9',
                               'title': 'Fix', **next(views)})
        if args[:2] == ['run', 'list']:
            return json.dumps([{'databaseId': 55, 'attempt': 1, 'status': 'completed', 'conclusion': 'failure',
                                'workflowName': 'CI', 'headBranch': 'feat', 'event': 'pull_request',
                                'url': 'https://github.com/acme/os/actions/runs/55',
                                'createdAt': '2026-11-20T01:00:00Z', 'displayTitle': 'Fix'}])
        if args[:2] == ['run', 'view']:
            return f'ci\tRun tests\t2026-11-20T01:00:00Z FAILED tests/test_x.py::test_fail - token={secret}\n'
        return ''

    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd(tmp_path, 9, sleep=lambda s: None) == 'MERGED'
    receipt_file = tmp_path/'ci'/'receipts'/'55-1.json'
    assert receipt_file.is_file()
    receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
    assert receipt['class'] == 'regression' and receipt['rule'] == 'failed_test_ids'
    assert receipt['pr'] == 9 and receipt['head_sha'] == 'abc1234' and receipt['untrusted'] is True
    assert receipt['verification_checks'] == [['python', '-m', 'pytest', 'tests/test_x.py::test_fail', '-q']]
    assert secret not in json.dumps(receipt) and '[redacted]' in json.dumps(receipt)
