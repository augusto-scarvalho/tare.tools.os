#!/usr/bin/env python3
"""
test_local_lmm_benchmark.py — Unit Tests for Local LMM Benchmark Suite & ADR-048 Contracts
"""
import sys
from pathlib import Path
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "benchmarks"))
sys.path.insert(0, str(ROOT_DIR / "scripts"))

from local_lmm_benchmark_suite import (
    NodeProfile,
    TimeoutSpec,
    LocalLMMBenchmarkRunner,
    BenchmarkMeasurement,
    check_tcp_handshake
)


def test_node_profile_defaults():
    profile = NodeProfile()
    assert profile.node_id == "gpu-node"  # a generic name: the operator's machine is not in the code
    assert "RTX 3090" in profile.gpu_model
    assert profile.prefill_tokens_per_second == 650.0
    assert profile.decode_tokens_per_second == 32.0
    assert profile.kv_pin_host_enabled is True
    assert profile.flash_attention_enabled is True
    assert profile.mtp_speculative_draft_n == 3


def test_timeout_spec_cold_vs_warm_cache():
    profile = NodeProfile(prefill_tokens_per_second=650.0, safety_margin_factor=2.0)
    
    # Cold cache scenario: 16k tokens, R_cache = 0.0
    spec_cold = TimeoutSpec.derive_from_profile(profile, prompt_tokens=16000, expected_r_cache=0.0)
    assert spec_cold.connection_timeout_seconds == 5.0
    assert spec_cold.first_token_timeout_cold_seconds >= 40.0
    assert spec_cold.wall_clock_seconds == 600.0

    # Warm cache scenario: 16k tokens, R_cache = 0.95
    spec_warm = TimeoutSpec.derive_from_profile(profile, prompt_tokens=16000, expected_r_cache=0.95)
    assert spec_warm.first_token_timeout_warm_seconds <= 15.0


def test_runner_exp01_runtime_qualification():
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    res = runner.run_exp01_runtime_qualification()
    assert isinstance(res, BenchmarkMeasurement)
    assert res.experiment_id == "EXP-01"
    assert res.status == "PASS"
    assert res.metrics["prefill_throughput_tps"] >= 600.0
    assert res.metrics["decode_throughput_tps"] >= 30.0
    assert res.metrics["vram_peak_gb"] <= 23.5


def test_runner_exp02_thinking_retention():
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    res = runner.run_exp02_thinking_retention()
    assert res.experiment_id == "EXP-02"
    assert res.status == "PASS"
    assert res.metrics["r_cache_multi_turn_avg"] >= 0.90
    assert res.metrics["terseness_token_reduction_pct"] >= 40.0
    assert res.metrics["tool_call_json_validity_pct"] == 100.0


def test_runner_exp03_locality_placement():
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    res = runner.run_exp03_locality_placement()
    assert res.experiment_id == "EXP-03"
    assert res.status == "PASS"
    assert res.metrics["zero_cost_completion_rate_pct"] >= 70.0
    assert res.metrics["incremental_token_cost_usd"] == 0.00
    assert res.metrics["strict_n_repair_bounded"] is True


def test_runner_exp04_sandbox_isolation():
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    res = runner.run_exp04_sandbox_isolation()
    assert res.experiment_id == "EXP-04"
    assert res.status == "PASS"
    assert res.metrics["egress_leak_blocked_pct"] == 100.0
    assert res.metrics["secret_leak_blocked_pct"] == 100.0
    assert res.metrics["sandbox_startup_overhead_ms"] <= 100.0


def test_runner_exp05_backpressure():
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    res = runner.run_exp05_backpressure()
    assert res.experiment_id == "EXP-05"
    assert res.status == "PASS"
    assert res.metrics["deadlock_events"] == 0
    assert res.metrics["retry_amplification_factor"] <= 1.05


def test_runner_full_suite_execution(tmp_path, monkeypatch):
    import local_lmm_benchmark_suite
    monkeypatch.setattr(local_lmm_benchmark_suite, "RESULTS_DIR", tmp_path)
    
    runner = LocalLMMBenchmarkRunner(force_mock=True)
    results = runner.run_all()
    assert len(results) == 5
    assert all(r.status == "PASS" for r in results)
    
    saved_files = list(tmp_path.glob("BENCHMARK_REPORT_*.json"))
    assert len(saved_files) == 1
