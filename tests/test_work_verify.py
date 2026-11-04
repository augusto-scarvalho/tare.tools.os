"""Behavior tests for --verify parsing in tare-ops run."""
import json
import sys
from unittest import mock

import pytest

import tare_ops


def _run_cli(args):
    """Call tare_ops.main with mocked run_work; return (rc, mock_run)."""
    with mock.patch("relay.work_run.run_work", return_value={"status": "DRY_RUN"}) as mock_run, \
         mock.patch("relay.native_catalog.select", return_value="fake-profile"), \
         mock.patch("tare_tools_agent_runtime.CancellationToken"):
        rc = tare_ops.main(args)
    return rc, mock_run


def _checks(mock_run):
    """Extract the checks kwarg from the run_work call."""
    return mock_run.call_args[1]["checks"]


# ── Valid: JSON array form ──────────────────────────────────────────────

def test_json_array_preserves_spaces():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["my exe", "arg with spaces"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["my exe", "arg with spaces"]}]


def test_json_array_empty_string_element():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["exe", ""]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["exe", ""]}]


def test_json_array_trailing_empty_strings():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["exe", "", "", "last"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["exe", "", "", "last"]}]


def test_json_array_unicode():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["exe", "café", "日本語"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["exe", "café", "日本語"]}]


def test_json_array_windows_paths():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["C:/Program Files/tool.exe", "--flag"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["C:/Program Files/tool.exe", "--flag"]}]


def test_json_array_literal_quotes():
    rc, mock_run = _run_cli(['run', 'W1', '--verify', r'crit=["exe", "say \"hello\""]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["exe", 'say "hello"']}]


def test_json_array_shell_metacharacters():
    rc, mock_run = _run_cli(['run', 'W1', '--verify', 'crit=["exe", "a && b", "c | d", "*", "$HOME"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["exe", "a && b", "c | d", "*", "$HOME"]}]


def test_json_array_single_element():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["only-exec"]', "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "crit", "command": ["only-exec"]}]


# ── Valid: multiple --verify, order preserved ──────────────────────────

def test_multiple_verify_json_order_preserved():
    rc, mock_run = _run_cli([
        "run", "W1",
        "--verify", 'first=["a", "b"]',
        "--verify", 'second=["c"]',
        "--verify", 'third=["d"]',
        "--dry-run",
    ])
    assert rc == 0
    assert _checks(mock_run) == [
        {"criterion": "first", "command": ["a", "b"]},
        {"criterion": "second", "command": ["c"]},
        {"criterion": "third", "command": ["d"]},
    ]


def test_mixed_simple_and_json_order_preserved():
    rc, mock_run = _run_cli([
        "run", "W1",
        "--verify", "simple=python -m pytest",
        "--verify", 'json=["exe", "with spaces"]',
        "--dry-run",
    ])
    assert rc == 0
    assert _checks(mock_run) == [
        {"criterion": "simple", "command": ["python", "-m", "pytest"]},
        {"criterion": "json", "command": ["exe", "with spaces"]},
    ]


# ── Valid: simple (space-split) form backward compatibility ─────────────

def test_simple_form_compat():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "tests=python -m pytest -q", "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "tests", "command": ["python", "-m", "pytest", "-q"]}]


def test_simple_form_single_word():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "lint=ruff", "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) == [{"criterion": "lint", "command": ["ruff"]}]


# ── Valid: absence of --verify ─────────────────────────────────────────

def test_no_verify_gives_none():
    rc, mock_run = _run_cli(["run", "W1", "--dry-run"])
    assert rc == 0
    assert _checks(mock_run) is None


# ── Invalid: JSON form errors (exit 2, no run_work call) ──────────────

def test_json_malformed_no_fallback():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "crit=[not valid json", "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_malformed_truncated():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["a", "b"', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_empty_list():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=[]', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_element_not_string():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["exe", 42]', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_element_null():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["exe", null]', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_executable_empty():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["", "arg"]', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_json_executable_whitespace():
    rc, mock_run = _run_cli(["run", "W1", "--verify", 'crit=["   ", "arg"]', "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


# ── Invalid: criterion and format errors ───────────────────────────────

def test_criterion_empty():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "=python -m pytest", "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_criterion_whitespace_only():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "   =[\"python\"]", "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_command_empty_simple():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "crit=", "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()


def test_no_equals_sign():
    rc, mock_run = _run_cli(["run", "W1", "--verify", "just-a-string", "--dry-run"])
    assert rc == 2
    mock_run.assert_not_called()