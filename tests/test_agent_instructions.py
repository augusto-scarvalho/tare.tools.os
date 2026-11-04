from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_shared_agent_instructions_stay_small_and_claude_reads_the_same_file():
    # AGENTS.md reaches every agent and every tare role's package: only what all of them need (role guidance is
    # in prompt capsules, the relay mesh in docs/guides/RELAY_MESH.md). It held 12.7 KB of swarm rules until
    # 2026-10-08.
    assert len((ROOT/'AGENTS.md').read_bytes()) <= 4096
    assert '@AGENTS.md' in (ROOT/'CLAUDE.md').read_text(encoding='utf-8').split()
