import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "relay") not in sys.path:
    sys.path.insert(0, str(_ROOT / "relay"))

from relay import round_table_engine


@pytest.fixture
def mock_round_table_env(monkeypatch, tmp_path):
    rt_dir = tmp_path / "round_tables"
    rt_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(round_table_engine, "ROUND_TABLES_DIR", rt_dir)
    return rt_dir


def test_init_round_table_and_seal(mock_round_table_env):
    proposal_text = "# ADR-999: Test Architecture Proposal\n\nThis is a formal test proposal."
    case_state = round_table_engine.init_round_table(
        case_id="RT-TEST-001",
        title="Test Architectural Proposal",
        proposal_text=proposal_text,
        author="operator"
    )

    assert case_state["case_id"] == "RT-TEST-001"
    assert case_state["status"] == "SEALED"
    assert case_state["current_version"] == "v001"
    assert "v001" in case_state["versions"]
    assert len(case_state["versions"]["v001"]["sha256"]) == 64

    # Verify ledger entry
    case = round_table_engine.RoundTableCase("RT-TEST-001")
    assert case.ledger_file.exists()
    lines = case.ledger_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["event"] == "CASE_INITIALIZED"


def test_crlf_proposal_hash_integrity(mock_round_table_env, monkeypatch):
    """Adversarial check for RT-N1 (CRLF Windows Newlines must NOT brick integrity)."""
    crlf_text = "# Header\r\n\r\nLine 1\r\nLine 2\r\n"
    case_state = round_table_engine.init_round_table(
        case_id="RT-CRLF-001",
        title="CRLF Integrity Test",
        proposal_text=crlf_text,
    )
    assert case_state["status"] == "SEALED"

    # Mock seats approving
    def mock_approve_seat(seat_id, prompt, timeout=300):
        return True, {
            "seat": seat_id,
            "verdict": "APPROVE",
            "confidence": 0.95,
            "summary": "OK",
            "strengths": ["Clear design"],
            "issues": [],
            "recommendations": []
        }, "OK"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_approve_seat)

    # conduct_round must succeed without Integrity Violation
    res = round_table_engine.conduct_round("RT-CRLF-001")
    assert res["status"] == "APPROVED"
    assert res["tally"]["approve"] == 3


def test_path_traversal_and_invalid_case_id_rejected(mock_round_table_env):
    """Adversarial check for RT-005 (Path Traversal & Injection)."""
    with pytest.raises(ValueError, match="Invalid case_id"):
        round_table_engine.init_round_table(
            case_id="../escaped_dir",
            title="Exploit",
            proposal_text="bad"
        )

    with pytest.raises(ValueError, match="Invalid case_id"):
        round_table_engine.init_round_table(
            case_id="bad/path/injection",
            title="Exploit",
            proposal_text="bad"
        )


def test_create_only_blocks_all_existing_cases(mock_round_table_env):
    """Adversarial check for RT-N2 & RT-005 (Create-Only invariant across all states)."""
    round_table_engine.init_round_table(
        case_id="RT-LOCK-001",
        title="Valid Case",
        proposal_text="Original Text"
    )

    # Overwrite attempt on SEALED
    with pytest.raises(ValueError, match="already exists"):
        round_table_engine.init_round_table(
            case_id="RT-LOCK-001",
            title="Overwritten Case",
            proposal_text="Hacked Text"
        )

    # Transition to REJECTED
    case = round_table_engine.RoundTableCase("RT-LOCK-001")
    state = case.load_state()
    state["status"] = "REJECTED"
    case.save_state(state)
    round_table_engine.finalize_decision("RT-LOCK-001", "REJECTED", "Rejected by vote.")

    # Overwrite attempt on REJECTED
    with pytest.raises(ValueError, match="already exists"):
        round_table_engine.init_round_table(
            case_id="RT-LOCK-001",
            title="Overwritten Case",
            proposal_text="Hacked Text"
        )


def test_terminal_case_cannot_run_deliberation(mock_round_table_env):
    """Adversarial check for RT-N5 (Cannot deliberate on terminal cases)."""
    round_table_engine.init_round_table(
        case_id="RT-TERM-001",
        title="Terminal Test",
        proposal_text="Original Text"
    )
    case = round_table_engine.RoundTableCase("RT-TERM-001")
    state = case.load_state()
    state["status"] = "APPROVED"
    case.save_state(state)

    with pytest.raises(ValueError, match="terminal status"):
        round_table_engine.conduct_round("RT-TERM-001")


def test_concurrent_lock_contention_no_deadlock(mock_round_table_env):
    """Adversarial check for RT-G1 (Lock inversion & contention between multiple threads)."""
    round_table_engine.init_round_table(
        case_id="RT-THREAD-CONTEND",
        title="Thread Contention Test",
        proposal_text="Thread Text"
    )
    case = round_table_engine.RoundTableCase("RT-THREAD-CONTEND")

    results = []

    def thread_worker(t_id, hold_time):
        try:
            with case.lock(timeout_s=5.0):
                time.sleep(hold_time)
                results.append((t_id, "SUCCESS"))
        except Exception as e:
            results.append((t_id, f"ERROR: {e}"))

    t1 = threading.Thread(target=thread_worker, args=(1, 0.2))
    t2 = threading.Thread(target=thread_worker, args=(2, 0.1))

    t1.start()
    time.sleep(0.05) # ensure T1 gets lock first
    t2.start()

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert len(results) == 2
    assert ("1", "SUCCESS") in [ (str(r[0]), r[1]) for r in results ]
    assert ("2", "SUCCESS") in [ (str(r[0]), r[1]) for r in results ]


def test_falsifier_all_seats_failing_produces_held_unavailable(mock_round_table_env, monkeypatch):
    """Adversarial check for RT-001 (All Seats Fail -> Must NOT Approve)."""
    round_table_engine.init_round_table(
        case_id="RT-FAIL-ALL",
        title="Failing Seats Test",
        proposal_text="# ADR Failure Test"
    )

    def mock_broken_seat(seat_id, prompt, timeout=300):
        return False, {"seat": seat_id, "verdict": "UNAVAILABLE", "reason": "CLI timeout"}, ""

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_broken_seat)

    res = round_table_engine.conduct_round("RT-FAIL-ALL", allow_fallbacks=False)
    assert res["status"] == "HELD_UNAVAILABLE"
    assert res["tally"]["approve"] == 0
    assert len(res["tally"]["failed_seats"]) == 3


def test_falsifier_single_reject_blocks_approval(mock_round_table_env, monkeypatch):
    """Adversarial check for RT-001 (1x REJECT + 2x APPROVE -> Must NOT Approve)."""
    round_table_engine.init_round_table(
        case_id="RT-ONE-REJECT",
        title="Single Reject Test",
        proposal_text="# ADR Single Reject Test"
    )

    def mock_one_reject(seat_id, prompt, timeout=300):
        if seat_id == "google":
            return True, {
                "seat": "google",
                "verdict": "REJECT",
                "confidence": 0.99,
                "summary": "Unacceptable security breach.",
                "strengths": ["Clean layout"],
                "issues": [{"id": "SEC-01", "severity": "blocking", "claim": "RCE exploit", "falsifier": "code execution", "resolution": "sandbox"}],
                "recommendations": []
            }, ""
        else:
            return True, {
                "seat": seat_id,
                "verdict": "APPROVE",
                "confidence": 0.95,
                "summary": "Looks good.",
                "strengths": ["Solid"],
                "issues": [],
                "recommendations": []
            }, ""

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_one_reject)

    res = round_table_engine.conduct_round("RT-ONE-REJECT")
    assert res["status"] == "REVISION_REQUIRED"
    assert res["status"] != "APPROVED"
    assert res["tally"]["reject"] == 1
    assert res["tally"]["approve"] == 2


def test_falsifier_strengths_non_list_handled_strictly(mock_round_table_env):
    """Adversarial check for RT-N4 (strengths/recommendations string instead of list)."""
    bad_vote = {
        "verdict": "APPROVE",
        "confidence": 0.9,
        "summary": "Approved",
        "strengths": "RCE exploit", # string instead of list!
        "issues": []
    }
    valid, vote, msg = round_table_engine.validate_seat_vote("google", bad_vote)
    assert not valid
    assert "Strengths must be a list" in msg


def test_immutable_decision_file(mock_round_table_env):
    """Adversarial check for RT-007 (Decision File Immutability)."""
    round_table_engine.init_round_table(
        case_id="RT-IMMUTABLE-01",
        title="Immutable Test",
        proposal_text="# Canonical Proposal Text"
    )

    dec1 = round_table_engine.finalize_decision("RT-IMMUTABLE-01", "APPROVED", "Approved originally.")
    original_content = dec1.read_text(encoding="utf-8")

    # Attempt to finalize again with different verdict
    dec2 = round_table_engine.finalize_decision("RT-IMMUTABLE-01", "REJECTED", "Tampered verdict.")
    new_content = dec2.read_text(encoding="utf-8")

    assert original_content == new_content
    assert "APPROVED" in new_content
    assert "REJECTED" not in new_content


def test_prompt_archetypes_and_custom_instructions(mock_round_table_env, monkeypatch):
    """Verifies that all prompt archetypes and custom instructions render correctly into seat prompts."""
    for arch_key in round_table_engine.PROMPT_ARCHETYPES.keys():
        case_id = f"RT-ARCH-{arch_key.upper()[:10]}"
        case_state = round_table_engine.init_round_table(
            case_id=case_id,
            title=f"Test {arch_key}",
            proposal_text="# Proposal content",
            archetype=arch_key,
            custom_instructions="MANDATORY: Verify zero token waste."
        )
        assert case_state["archetype"] == arch_key
        assert case_state["custom_instructions"] == "MANDATORY: Verify zero token waste."

        # Verify prompt building
        prompt = round_table_engine._build_seat_prompt(
            case_id=case_id,
            version="v001",
            proposal_text="# Proposal content",
            seat_id="google",
            round_num=1,
            archetype=arch_key,
            custom_instructions="MANDATORY: Verify zero token waste."
        )
        assert round_table_engine.PROMPT_ARCHETYPES[arch_key]["title"] in prompt
        assert "MANDATORY: Verify zero token waste." in prompt


def test_cli_list_profiles(capsys):
    """Verifies CLI list-profiles command runs cleanly."""
    sys_argv = ["round_table_engine.py", "list-profiles"]
    import sys
    orig_argv = sys.argv
    try:
        sys.argv = sys_argv
        code = round_table_engine.main()
        assert code == 0
        captured = capsys.readouterr()
        assert "DELIBERATION PROFILES" in captured.out
        assert "value_gate" in captured.out
        assert "security" in captured.out
        assert "experiment" in captured.out
    finally:
        sys.argv = orig_argv


def test_parallel_seat_execution_and_isolation(mock_round_table_env, monkeypatch):
    """Verifies that parallel dispatch executes all 3 seats concurrently with atomic per-seat files."""
    round_table_engine.init_round_table(
        case_id="RT-PARALLEL-001",
        title="Parallel Execution Test",
        proposal_text="# Parallel Proposal"
    )

    entered = threading.Barrier(3, timeout=10)

    def mock_delayed_seat(seat_id, prompt, timeout=300):
        # A sequential dispatcher cannot release this barrier. Scheduler and
        # filesystem latency do not determine the concurrency assertion.
        entered.wait()
        return True, {
            "seat": seat_id,
            "verdict": "APPROVE",
            "confidence": 0.95,
            "summary": f"Seat {seat_id} approved.",
            "strengths": ["Fast execution"],
            "issues": [],
            "recommendations": []
        }, "RAW"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_delayed_seat)

    res_parallel = round_table_engine.conduct_round("RT-PARALLEL-001", parallel=True)

    assert res_parallel["status"] == "APPROVED"
    assert res_parallel["tally"]["approve"] == 3

    # Verify that all 3 seat JSON files were properly written
    case = round_table_engine.RoundTableCase("RT-PARALLEL-001")
    r_dir = case.rounds_dir / "r001"
    for s_id in ("google", "anthropic", "openai"):
        s_file = r_dir / f"seat_{s_id}.json"
        assert s_file.exists()
        s_data = json.loads(s_file.read_text(encoding="utf-8"))
        assert s_data["seat"] == s_id
        assert s_data["vote"]["verdict"] == "APPROVE"


def test_parallel_partial_thread_failure_resilience(mock_round_table_env, monkeypatch):
    """Verifies that an unhandled exception or network disconnect in one seat thread does not take down the deliberation."""
    round_table_engine.init_round_table(
        case_id="RT-FAIL-RESILIENCE",
        title="Partial Thread Failure Test",
        proposal_text="# Partial Failure Proposal"
    )

    def mock_flaky_seat(seat_id, prompt, timeout=300):
        if seat_id == "anthropic":
            raise ConnectionResetError("Simulated LLM API network disconnect")
        return True, {
            "seat": seat_id,
            "verdict": "APPROVE",
            "confidence": 0.95,
            "summary": "OK",
            "strengths": ["Good"],
            "issues": [],
            "recommendations": []
        }, "RAW"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_flaky_seat)

    res = round_table_engine.conduct_round("RT-FAIL-RESILIENCE", parallel=True, allow_fallbacks=False)
    assert res["status"] == "HELD_UNAVAILABLE"
    assert "anthropic" in res["tally"]["failed_seats"]
    assert res["seat_votes"]["anthropic"]["verdict"] == "UNAVAILABLE"
    assert "google" not in res["tally"]["failed_seats"]
    assert "openai" not in res["tally"]["failed_seats"]


def test_full_two_round_deliberation_mediation_to_approval(mock_round_table_env, monkeypatch):
    """Verifies complete multi-round deliberation loop: Round 1 REVISION_REQUIRED -> Mediation v002 -> Round 2 APPROVED."""
    round_table_engine.init_round_table(
        case_id="RT-MULTI-ROUND",
        title="Multi-round Deliberation Test",
        proposal_text="# Initial Architecture Draft"
    )

    round_tracker = {"count": 0}

    def mock_evolving_seats(seat_id, prompt, timeout=300):
        # Determine round from prompt
        if "ROUND 1" in prompt:
            if seat_id == "google":
                return True, {
                    "seat": "google",
                    "verdict": "REVISE",
                    "confidence": 0.85,
                    "summary": "Missing bounded RAM streaming invariant.",
                    "strengths": ["Clear design"],
                    "issues": [{
                        "id": "MEM-01",
                        "severity": "blocking",
                        "claim": "Memory is unbounded on 10k files",
                        "falsifier": "Heap overflow",
                        "resolution": "Enforce 64 KiB chunked stream"
                    }],
                    "recommendations": ["Add bounded chunk streaming"]
                }, "RAW"
            else:
                return True, {
                    "seat": seat_id,
                    "verdict": "APPROVE",
                    "confidence": 0.95,
                    "summary": "Looks good.",
                    "strengths": ["Solid"],
                    "issues": [],
                    "recommendations": []
                }, "RAW"
        else:
            # Round 2: All seats approve the refined v002
            return True, {
                "seat": seat_id,
                "verdict": "APPROVE",
                "confidence": 0.98,
                "summary": "Blocker resolved cleanly.",
                "strengths": ["Bounded streaming added"],
                "issues": [],
                "recommendations": []
            }, "RAW"

    def mock_mediator(deployment, prompt, timeout=300, **kwargs):
        text = "# Objetivos\n\n- Invariant: 64 KiB bounded chunked stream added.\n\n# Não-objetivos\n\n- No scope expansion. " * 2
        return True, text, {"deployment_id": deployment["id"]}

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_evolving_seats)
    monkeypatch.setattr(round_table_engine, "_execute_text_role", mock_mediator)

    final_res = round_table_engine.run_full_deliberation("RT-MULTI-ROUND", max_rounds=3, parallel=True, llm_mediator=True)
    assert final_res["status"] == "APPROVED"
    assert final_res["decision_file"] is not None

    # Check case state
    case = round_table_engine.RoundTableCase("RT-MULTI-ROUND")
    state = case.load_state()
    assert state["status"] == "APPROVED"
    assert state["current_version"] == "v002"
    assert "v001" in state["versions"]
    assert "v002" in state["versions"]
    assert (case.decision_dir / "DECISION.md").exists()
