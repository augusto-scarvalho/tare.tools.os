"""Focused user journeys for the read-only conversation usage panel."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import TextArea

from relay.console_workbench import WorkbenchPanel, usage_rows
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def _flow_receipts(tmp_path, monkeypatch):
    import json
    root = tmp_path/'project'; root.mkdir()
    state = tmp_path/'state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    entry = state/'console-entries'/('a'*32)
    (entry/'flow-assessment').mkdir(parents=True)
    proposal = {'root': str(root), 'directory': str(entry), 'generation_calls': 2,
                'flow_assessment': {'receipt': 'must-not-follow-this-path'}}
    decision = {'inference_attempts': 1,
                'assessment': {'status': 'FAILED', 'reason': 'ASSESSMENT_CAPACITY_EXCEEDED',
                               'worker_stage': 'CPU_INPUT_PREFLIGHT'},
                'fallback': {'status': 'SUGGESTED', 'generation_calls': 1, 'deployment': 'test-vendor',
                             'tokens': {'input': 517, 'output': 9},
                             'receipt': {'served_model': 'test-model',
                                         'cost': {'known_amount': '0.01', 'currency': 'USD', 'complete': False}}}}
    for path, value in [(entry/'request.json', {'root': str(root)}),
                        (entry/'proposal.json', proposal),
                        (entry/'flow-assessment/decision.json', decision)]:
        path.write_text(json.dumps(value), encoding='utf-8')
    return root, entry, proposal, decision


def test_flow_usage_separates_attempts_deduplicates_and_does_not_mutate(tmp_path, monkeypatch):
    from relay.flow_usage import flow_usage_rows
    root, entry, proposal, _ = _flow_receipts(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in entry.rglob('*') if p.is_file()}
    demand = {'root': str(root), 'events': [{'kind': 'interpretation_finished', 'detail': proposal}]*2}
    rows = flow_usage_rows(root, proposal=proposal, demand=demand)
    assert len(rows) == 2
    assert 'Worker attempts: 1 (not proven model calls)' in rows[0]['body']
    assert 'Model calls: Unknown' in rows[0]['body']
    assert 'Input tokens: Unknown' in rows[0]['body']
    assert 'Known model cost: Unknown' in rows[0]['body']
    assert 'Model calls: 1' in rows[1]['body']
    assert 'Input tokens: 517' in rows[1]['body']
    assert 'Output tokens: 9' in rows[1]['body']
    assert 'test-model' in rows[1]['body']
    assert 'USD 0.01' in rows[1]['body'] and 'Cost coverage: Partial' in rows[1]['body']
    assert {p: p.read_bytes() for p in entry.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('fault', ['foreign', 'outside', 'oversized', 'invalid', 'missing', 'traversal'])
def test_flow_usage_rejects_untrusted_or_incomplete_receipts(tmp_path, monkeypatch, fault):
    import json
    from relay.flow_usage import flow_usage_rows
    root, entry, proposal, _ = _flow_receipts(tmp_path, monkeypatch)
    decision = entry/'flow-assessment/decision.json'
    if fault == 'foreign':
        (entry/'request.json').write_text(json.dumps({'root': str(tmp_path/'foreign')}))
    elif fault == 'outside':
        proposal['directory'] = str(tmp_path/('b'*32))
    elif fault == 'oversized':
        decision.write_text(' ' * 131073)
    elif fault == 'invalid':
        decision.write_text('{"x":1,"x":2}')
    elif fault == 'missing':
        decision.unlink()
    else:
        proposal['directory'] = str(entry/'..'/entry.name)
    rows = flow_usage_rows(root, proposal=proposal)
    assert len(rows) == 1
    assert rows[0]['label'] == 'Flow selection · usage unavailable'
    assert '517' not in rows[0]['body']


def test_flow_usage_rejects_linked_custody(tmp_path, monkeypatch):
    from relay.flow_usage import entry_usage
    root, entry, _, _ = _flow_receipts(tmp_path, monkeypatch)
    original = entry/'flow-assessment'
    target = entry/'moved'; original.rename(target)
    try:
        original.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows directory junctions require no symlink privilege.
        import os, subprocess
        if os.name != 'nt':
            raise
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(original), str(target)], check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='link_forbidden'):
            entry_usage(entry, root)
    finally:
        if original.is_symlink():
            original.unlink()
        else:
            original.rmdir()


@pytest.mark.parametrize('size', [(80, 24), (120, 40)])
def test_flow_usage_available_before_execution_in_choose_flow(tmp_path, monkeypatch, size):
    root, entry, proposal, _ = _flow_receipts(tmp_path, monkeypatch)
    def forbidden(*a, **k):
        pytest.fail('Opening recorded usage must not call a model')
    monkeypatch.setattr('relay.flow_assessment.entry', forbidden)
    monkeypatch.setattr('relay.native_profiles.usage', forbidden)
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='conversation')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('keep my unsent draft')
            app.entry_proposal = (app.selection(), proposal)
            scope = app.workbench_scope()
            assert app.assistant_session is None
            assert app.open_workbench('usage')
            await pilot.pause()
            assert len(app.screen.rows) == 4 and app.screen.rows[0]['id'] == 'quota'  # the provider readings come first
            assert app.screen.rows[-1]['id'] == 'behavioral-profiles'
            assert 'Completed Work executions: 0/10' in app.screen.rows[-1]['body']
            assert app.screen.query_one('#workbench-preview', TextArea).text.startswith('No reading yet.')
            await pilot.click('#workbench-list', offset=(1, 0))
            await pilot.press('down'); await pilot.pause()
            assert 'CPU_INPUT_PREFLIGHT' in app.screen.query_one('#workbench-preview', TextArea).text
            await pilot.press('down'); await pilot.pause()
            assert 'Input tokens: 517' in app.screen.query_one('#workbench-preview', TextArea).text
            await pilot.press('escape'); await pilot.pause()
            assert prompt.text == 'keep my unsent draft'
            assert app.workbench_scope() == scope
            assert not app.backend.launches
    asyncio.run(scenario())


def _body(rows):
    return rows[0]["body"]


def test_usage_rows_distinguish_recorded_and_unknown_values():
    body = _body(usage_rows({
        "model_calls": 3,
        "tokens": {
            "total_tokens": {"known_sum": 125, "unknown_calls": 1},
            "input_tokens": {"known_sum": 80, "unknown_calls": 0},
            "output_tokens": {"known_sum": 45, "unknown_calls": 1},
            "cache_read_tokens": {"known_sum": 20, "unknown_calls": 2},
        },
        "known_cost_by_currency": {"EUR": "0.25", "USD": "1.50"},
        "unpriced_or_partial_calls": 1,
        "effective_limits": {"max_model_calls": 8, "max_total_tokens": None},
    }))
    for text in ("Model calls: 3", "Total tokens: 125; 1 call(s) missing counts",
                 "Cache reads: 20; 2 call(s) missing counts", "Cache writes: Unknown",
                 "EUR 0.25, USD 1.50", "Unpriced or partially priced calls: 1",
                 "max model calls: 8", "max total tokens: Unlimited",
                 'Subscription quota and API credit: see "Provider quota and credit"'):
        assert text in body
    unknown = _body(usage_rows({}))
    assert "Model calls: Unknown" in unknown
    assert "Known model cost: Unknown" in unknown
    assert "Unpriced or partially priced calls: Unknown" in unknown
    assert "Configured limits:\n  Unknown" in unknown


def test_no_model_calls_are_recorded_as_zero_not_missing():
    body = _body(usage_rows({"model_calls": 0, "tokens": {},
                            "known_cost_by_currency": {},
                            "unpriced_or_partial_calls": 0, "effective_limits": {}}))
    assert "Model calls: 0" in body
    assert "Unpriced or partially priced calls: 0" in body
    assert "Total tokens: Unknown" in body
    assert "Known model cost: Unknown" in body


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_usage_command_preserves_draft_and_scope_and_empty_state(tmp_path, monkeypatch, size):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("unsent draft")
            scope = app.workbench_scope()
            app.action_usage(); await pilot.pause()
            panel = app.screen
            assert isinstance(panel, WorkbenchPanel)
            # The panel opens on what each provider last reported as left, read from the saved readings only.
            assert panel.rows[0]["id"] == "quota" and panel.rows[0]["body"].startswith("No reading yet.")
            assert panel.query_one("#workbench-preview", TextArea).text.startswith("No reading yet.")
            panel.show_row(1)
            assert "No saved native session" in panel.query_one("#workbench-preview", TextArea).text
            assert panel.region.contains_region(panel.query_one("#workbench-preview").region)
            await pilot.press("escape"); await pilot.pause()
            assert prompt.text == "unsent draft"
            assert app.workbench_scope() == scope
            prompt.load_text("/usage"); app.execute(False); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            assert not app.backend.launches
    asyncio.run(scenario())


def test_usage_panel_shows_the_saved_provider_readings_without_asking_a_provider(tmp_path, monkeypatch):
    import json
    from relay import provider_quota
    from relay.console_workbench import quota_row
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    monkeypatch.setattr(provider_quota, "refresh", lambda *a, **k: pytest.fail("the panel asked a provider"))
    assert quota_row()["body"].startswith("No reading yet.")
    saved = {"schema": provider_quota.SCHEMA, "observed_at": 1794300000, "accounts": [
        {"account": "agy", "name": "AGY subscription", "kind": "subscription", "status": "OBSERVED", "buckets": [
            {"id": "gemini-argon-wtu-weekly", "label": "Gemini 4 Argon, week", "remaining": 0, "resets_at": 1794632256}]},
        {"account": "openrouter", "name": "OpenRouter", "kind": "credit", "status": "OBSERVED",
         "credit": {"currency": "USD", "total": 20, "remaining": 5.23}}]}
    provider_quota.cache_path().parent.mkdir(parents=True)
    provider_quota.cache_path().write_text(json.dumps(saved), encoding="utf-8")
    row = quota_row()
    assert row["label"] == "Provider quota and credit"
    for text in ("Gemini 4 Argon, week", "none left", "OpenRouter: US$ 5.23 left of US$ 20.00",
                 "Reopen /usage to read again."):
        assert text in row["body"]


def test_busy_usage_never_reads_session_and_preserves_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("active writer was read")
    monkeypatch.setattr("relay.native_profiles.usage", forbidden)
    monkeypatch.setattr("relay.native_review.review", forbidden)
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one("#prompt", TextArea)
            app.assistant_session = tmp_path/"session"
            app.controls(True)
            prompt.load_text("/usage"); app.execute(False); await pilot.pause()
            app.screen.show_row(1)  # the provider readings come first
            assert "does not read an active writer" in app.screen.query_one("#workbench-preview", TextArea).text
            await pilot.press("escape"); await pilot.pause()
            assert not prompt.text
            app.controls(False)
    asyncio.run(scenario())


def _saved_shell(path: Path):
    path.mkdir()
    for name in ("native-agent.json", "journal.jsonl", "writer.lock"):
        (path/name).write_text("{}" if name.endswith(".json") else "", encoding="utf-8")


def test_saved_usage_validates_custody_and_refuses_wrong_project(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    session = tmp_path/"session"; _saved_shell(session)
    called = []
    def refuse(value, **kwargs):
        called.append(kwargs)
        raise ValueError("This conversation belongs to another project.")
    monkeypatch.setattr("relay.native_review.review", refuse)
    monkeypatch.setattr("relay.native_profiles.usage", lambda value: pytest.fail("usage read before custody"))
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = session
            assert app.open_workbench("usage") is False
            assert called == [{"project": root, "include_diffs": False}]
            assert not isinstance(app.screen, WorkbenchPanel)
    asyncio.run(scenario())


def test_saved_usage_panel_and_explicit_profile_tasks_label(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    session = tmp_path/"session"; _saved_shell(session)
    monkeypatch.setattr("relay.native_review.review", lambda *a, **k: {})
    monkeypatch.setattr("relay.native_profiles.usage", lambda value: {
        "model_calls": 1, "tokens": {"total_tokens": {"known_sum": 9, "unknown_calls": 0}},
        "known_cost_by_currency": {"USD": "0.01"}, "unpriced_or_partial_calls": 0,
        "effective_limits": {"max_model_calls": 4}})
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = session
            app.action_usage(); await pilot.pause()
            app.screen.show_row(1)  # the provider readings come first
            assert "Model calls: 1" in app.screen.query_one("#workbench-preview", TextArea).text
            await pilot.press("escape"); await pilot.pause()
            app.active = SimpleNamespace(profile={"native_profile": {"model": "gpt-5.6-sol"}},
                                         deployment="local-qwen")
            app.action_tasks(); await pilot.pause()
            text = app.screen.query_one("#workbench-preview", TextArea).text
            assert "Profile: Custom profile" in text
            assert "local-qwen" not in text
    asyncio.run(scenario())


def test_usage_owner_accepts_strict_generated_report_over_two_mib(tmp_path):
    import json
    from relay.native_profiles import usage

    session = tmp_path/"large-session"; session.mkdir()
    (session/"native-agent.json").write_text(json.dumps({"session_id": "large-report"}), encoding="utf-8")
    report = {"calls": [], "tokens": {"total_tokens": {"known_sum": 42, "unknown_calls": 0}},
              "coverage": {"generated": "x" * (2 * 1024 * 1024 + 128)},
              "known_cost_by_currency": {"USD": "0.10"}, "unpriced_or_partial_calls": 0}
    (session/"usage.json").write_text(json.dumps(report), encoding="utf-8")
    assert (session/"usage.json").stat().st_size > 2 * 1024 * 1024

    result = usage(session)
    assert result["session_id"] == "large-report"
    assert result["tokens"]["total_tokens"]["known_sum"] == 42
    assert len(result["coverage"]["generated"]) > 2 * 1024 * 1024


def test_usage_rejection_is_contained_at_ui_boundary(tmp_path, monkeypatch):
    from tare_tools_agent_runtime import ConversationRejected, ConversationRejectReason

    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    session = tmp_path/"session"; _saved_shell(session)
    monkeypatch.setattr("relay.native_review.review", lambda *a, **k: {})
    def rejected(_session):
        raise ConversationRejected(ConversationRejectReason.INVALID_EVENT, "malformed report")
    monkeypatch.setattr("relay.native_profiles.usage", rejected)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            app.assistant_session = session
            assert app.open_workbench("usage") is False
            assert not isinstance(app.screen, WorkbenchPanel)
    asyncio.run(scenario())


def test_interactive_usage_panel_reads_the_providers_again_when_it_opens(tmp_path, monkeypatch):
    import json
    from relay import provider_quota
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    monkeypatch.delenv("TARE_CLI_DISCOVERY", raising=False)
    root = tmp_path/"project"; root.mkdir()

    def refresh():
        provider_quota.cache_path().parent.mkdir(parents=True, exist_ok=True)
        provider_quota.cache_path().write_text(json.dumps({
            "schema": provider_quota.SCHEMA, "observed_at": 1794300000, "accounts": [
                {"account": "openrouter", "name": "OpenRouter", "kind": "credit", "status": "OBSERVED",
                 "credit": {"currency": "USD", "total": 20, "remaining": 5.23}}]}), encoding="utf-8")
    monkeypatch.setattr(provider_quota, "refresh", refresh)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.asks_providers = True  # what the interactive entry sets, without its CLI discovery
            app.action_usage(); await pilot.pause()
            panel = app.screen
            assert panel.rows[0]["body"].startswith("No reading yet. Reading each provider again")
            await app.workers.wait_for_complete()
            panel.refresh_observations()  # the panel's own timer does this every second
            assert "OpenRouter: US$ 5.23 left of US$ 20.00" in panel.rows[0]["body"]
            assert panel.rows[0]["body"].endswith("Reopen /usage to read again.")
            assert "OpenRouter" in panel.query_one("#workbench-preview", TextArea).text
            assert [r["id"] for r in panel.rows[1:]] and panel.rows[1]["id"] != "quota"
    asyncio.run(scenario())


def test_usage_panel_shows_what_heads_up_used_only_when_it_called_a_writer(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    root = tmp_path/"project"; root.mkdir()
    session = tmp_path/"session"; _saved_shell(session)
    monkeypatch.setattr("relay.native_review.review", lambda *a, **k: {})
    monkeypatch.setattr("relay.native_profiles.usage", lambda value: {"model_calls": 1})
    log = tmp_path/"state"/"serve"/"heads-up.jsonl"
    call = {"route": "w", "ok": True, "ms": 2500, "tokens": {"input_tokens": 1200, "output_tokens": 80}, "usd": 0.0123}
    rows = [{"session": session.name, "writers": [call, {"route": "w", "ok": True, "ms": 500}]},
            {"session": "another", "writers": [call]}]

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = session
            app.action_usage(); await pilot.pause()
            assert "heads-up" not in [r["id"] for r in app.screen.rows]  # no writer ran in this conversation
            await pilot.press("escape"); await pilot.pause()
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("".join(json.dumps(r) + chr(10) for r in rows), encoding="utf-8")
            app.action_usage(); await pilot.pause(.3)
            ids = [r["id"] for r in app.screen.rows]
            assert ids.index("heads-up") == ids.index("usage") + 1
            row = app.screen.rows[ids.index("heads-up")]
            assert row["label"] == "Heads-up · 2 calls"
            for text in ("writer calls: 2", "1200 in, 80 out; 1 call(s) without token counts", "Time: 3.0 s",
                         "USD 0.0123; 1 call(s) without a known price"):
                assert text in row["body"]
    asyncio.run(scenario())
