"""Offline tests for SPEC-OS-SHIP-001 AC-21 privileged workflow trust preflight."""
import json
from pathlib import Path

import pytest

from relay import ci_watch, workflow_trust
from test_ci_watch import NOREPLY_EMAIL, feature_checkout, on_remote, scripted_github

SAFE_WORKFLOW_RUN = """\
name: Release
on:
  workflow_run:
    workflows: ['CI']
    types: [completed]
    branches: [main]
  workflow_dispatch:

jobs:
  publish:
    if: >-
      (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')
      || (github.event.workflow_run.conclusion == 'success'
          && github.event.workflow_run.event == 'push'
          && github.event.workflow_run.head_repository.full_name == github.repository)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          ref: main
  sign:
    needs: publish
    if: needs.publish.outputs.published == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          ref: main
"""

UNSAFE_WORKFLOW_RUN_NO_SAME_REPO = """\
name: Release
on:
  workflow_run:
    workflows: ['CI']
    types: [completed]

jobs:
  publish:
    if: github.event.workflow_run.event == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
"""

UNSAFE_WORKFLOW_RUN_OR_BYPASS = """\
name: Release
on:
  workflow_run:
    workflows: ['CI']
    types: [completed]

jobs:
  publish:
    if: >-
      github.event_name == 'workflow_run'
      || (github.event.workflow_run.event == 'push'
          && github.event.workflow_run.head_repository.full_name == github.repository)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
"""

UNSAFE_WORKFLOW_RUN_TRIGGER_CHECKOUT = """\
name: Release
on:
  workflow_run:
    workflows: ['CI']
    types: [completed]

jobs:
  publish:
    if: >-
      github.event.workflow_run.event == 'push'
      && github.event.workflow_run.head_repository.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          ref: ${{ github.event.workflow_run.head_sha }}
"""

UNSAFE_PRT_HEAD_CHECKOUT = """\
name: Label PR
on: [pull_request_target]

jobs:
  inspect:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout untrusted PR head
        uses: actions/checkout@v5
        with:
          ref: ${{ github.event.pull_request.head.sha }}
"""

SAFE_PRT_BASE_CHECKOUT = """\
name: Label PR
on: pull_request_target

jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout trusted base
        uses: actions/checkout@v5
"""


def test_tare_tools_os_own_workflows_pass_trust_inspection():
    repo_root = Path(__file__).resolve().parents[1]
    workflows_dir = repo_root/'.github'/'workflows'
    files = sorted(p for p in workflows_dir.iterdir() if p.suffix in ('.yml', '.yaml'))
    assert any(p.name == 'release.yml' for p in files)
    for path in files:
        _, findings = workflow_trust.analyze_workflow(f'.github/workflows/{path.name}', path.read_text(encoding='utf-8'))
        assert findings == [], f'unexpected finding in {path.name}: {findings}'


@pytest.mark.parametrize('yaml_text,expected_rule', [
    (UNSAFE_WORKFLOW_RUN_NO_SAME_REPO, 'workflow_run_unguarded'),
    (UNSAFE_WORKFLOW_RUN_OR_BYPASS, 'workflow_run_unguarded'),
    (UNSAFE_WORKFLOW_RUN_TRIGGER_CHECKOUT, 'workflow_run_trigger_checkout'),
    (UNSAFE_PRT_HEAD_CHECKOUT, 'pull_request_target_head_checkout'),
])
def test_pr_adding_unsafe_privileged_workflow_is_refused_before_push(tmp_path, monkeypatch, yaml_text, expected_rule):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    wf = work/'.github'/'workflows'/'bad.yml'
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(yaml_text, encoding='utf-8')
    git('add', '.'); git('commit', '-qm', 'Add privileged workflow')
    calls = scripted_github(monkeypatch)
    with pytest.raises(ValueError, match='^Preflight refused:') as refused:
        ci_watch.pull_request(work)
    assert '.github/workflows/bad.yml' in str(refused.value)
    assert not on_remote(remote) and not [c for c in calls if c[0] == 'pr']
    report = workflow_trust.inspect_repo(work, base='main')
    assert report['status'] == 'REFUSED' and any(item['rule'] == expected_rule for item in report['refused'])


@pytest.mark.parametrize('yaml_text', [SAFE_WORKFLOW_RUN, SAFE_PRT_BASE_CHECKOUT])
def test_pr_adding_safe_privileged_workflow_passes_preflight(tmp_path, monkeypatch, yaml_text):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    wf = work/'.github'/'workflows'/'safe.yml'
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(yaml_text, encoding='utf-8')
    git('add', '.'); git('commit', '-qm', 'Add safe privileged workflow')
    scripted_github(monkeypatch)
    result = ci_watch.pull_request(work)
    assert result['number'] == 5 and result['workflow_trust']['status'] == 'OK'
    assert on_remote(remote)


def test_preexisting_unsafe_workflow_on_default_branch_is_reported_not_blocked_unless_modified(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    # Seed an unsafe privileged workflow on main before branching.
    git('checkout', '-q', 'main')
    wf = work/'.github'/'workflows'/'legacy.yml'
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(UNSAFE_PRT_HEAD_CHECKOUT, encoding='utf-8')
    git('add', '.'); git('commit', '-qm', 'Legacy unsafe workflow'); git('push', '-q', 'origin', 'main')

    # Unrelated PR branched from main is NOT blocked; pre-existing finding is persisted in receipt + proposed backlog item.
    git('checkout', '-qb', 'unrelated', 'origin/main')
    (work/'note.txt').write_text('hello\n', encoding='utf-8')
    git('add', '.'); git('commit', '-qm', 'Unrelated docs change')
    scripted_github(monkeypatch)
    result = ci_watch.pull_request(work)
    assert result['number'] == 5 and on_remote(remote, 'unrelated')
    wt = result['workflow_trust']
    assert wt['status'] == 'REPORTED' and wt['refused'] == []
    assert len(wt['existing']) == 1 and wt['existing'][0]['rule'] == 'pull_request_target_head_checkout'
    assert len(wt['proposed_backlog_items']) == 1
    assert 'legacy.yml:inspect' in wt['proposed_backlog_items'][0]['title']
    receipt = json.loads(Path(wt['receipt']).read_text(encoding='utf-8'))
    assert receipt['schema'] == workflow_trust.RECEIPT_SCHEMA and receipt['status'] == 'REPORTED'
    assert receipt['proposed_backlog_items'] == wt['proposed_backlog_items']

    # Modifying that flagged job without fixing it DOES refuse preflight before pushing.
    git('checkout', '-qb', 'edits-unsafe', 'origin/main')
    wf.write_text(UNSAFE_PRT_HEAD_CHECKOUT + '      - run: echo still unsafe\n', encoding='utf-8')
    git('add', '.'); git('commit', '-qm', 'Edit unsafe workflow job without fixing it')
    with pytest.raises(ValueError, match='^Preflight refused:.*legacy.yml'):
        ci_watch.pull_request(work)
    assert not on_remote(remote, 'edits-unsafe')
