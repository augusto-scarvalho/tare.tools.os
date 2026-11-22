"""Offline tests for deterministic lexical skill selection, bounded shortlist, 5-state abstention,
triggers, opt-in --auto-skills frozen receipts, CLI, and /skills TUI toggle (SKL-06 Corte A)."""

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Button, OptionList

from relay import console_backend
from relay import managed_skills as managed
from relay import native_agent
from relay import skill_bundles as bundles
from relay import skill_selector as selector
from relay.console_skills import SkillsPanel
from relay.native_console import WorkConsole
import tare_ops
from test_native_agent import Client, deployment, setup
from test_native_console import FakeBackend


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
    return path


def _adopt(tmp_path: Path, storage: Path, slug: str, name: str, desc: str, body: str, *, roles=None, triggers=None):
    src = tmp_path / f'src-{slug}'
    _write(src, 'SKILL.md', f'---\nname: {name}\ndescription: {desc}\n---\n{body}\n')
    cap = bundles.capture_bundle(src, name=slug, storage_root=storage)
    managed.adopt_snapshot(slug, cap['snapshot_id'], allowed_roles=roles, storage_root=storage)
    if triggers is not None:
        managed.set_triggers(slug, triggers, storage_root=storage)
    return cap


def test_selector_five_states_hard_eligibility_shortlist_cap_and_fingerprint(tmp_path):
    storage = tmp_path / 'storage'
    pxl_cap = _adopt(
        tmp_path, storage, 'pxl-art', 'PXL Pixel Art',
        'Creates pixel art sprites, tilesets, and game icons.',
        '# Pixel Sprite Workflow\nDraw crisp 2D sprites.',
        roles=['implementer', 'planner', 'auditor'],
        triggers=['icone 16x16', 'pixel art'],
    )
    _adopt(
        tmp_path, storage, 'tune-audio', 'Tune Game Audio',
        'Synthesizes retro sound effects, footsteps, and chiptune jingles.',
        '# Procedural SFX\nGenerate WAV audio.',
        roles=['implementer', 'planner'],
        triggers=['sound effect', 'efeito sonoro'],
    )
    _adopt(
        tmp_path, storage, 'review-strict', 'Code Review Strict',
        'Defect-first code diff review checklist.',
        '# Diff Review\nCheck invariants.',
        roles=['plan-auditor', 'auditor'],
    )
    _adopt(
        tmp_path, storage, 'review-quick', 'Code Review Quick',
        'Fast code diff review checklist.',
        '# Diff Review\nSpot regressions.',
        roles=['plan-auditor', 'auditor'],
    )

    # 1. Explicit abstention (no_skill) on ordinary task.
    abstain = selector.select_skills('fix off by one error in date parser', storage_root=storage)
    assert abstain['status'] == 'no_skill'
    assert abstain['selected_ids'] == []
    assert abstain['authority'] == 'NONE' and abstain['effects_authorized'] is False

    # 2. Selected via explicit trigger with Portuguese diacritic folding (`ícone 16x16`).
    sel_pt = selector.select_skills('cria um ícone 16x16 pro botão de menu', storage_root=storage)
    assert sel_pt['status'] == 'selected'
    assert sel_pt['selected_ids'] == ['pxl-art']

    # 3. Complementary multi-skill query selects both non-overlapping skills cleanly.
    sel_multi = selector.select_skills('draw a pixel art sprite and synthesize a jump sound effect', storage_root=storage)
    assert sel_multi['status'] == 'selected'
    assert set(sel_multi['selected_ids']) == {'pxl-art', 'tune-audio'}

    # 4. Ambiguous competing skills matching the exact same query intent within score margin -> abstains.
    amb = selector.select_skills('run a code diff review checklist', role='auditor', storage_root=storage)
    assert amb['status'] == 'ambiguous'
    assert amb['selected_ids'] == []
    assert {c['skill_id'] for c in amb['shortlist']} >= {'review-strict', 'review-quick'}

    # Ambiguity must still be detected even when max_shortlist=1 truncates the displayed shortlist.
    amb_capped = selector.select_skills(
        'run a code diff review checklist', role='auditor', max_shortlist=1, storage_root=storage,
    )
    assert amb_capped['status'] == 'ambiguous'
    assert amb_capped['selected_ids'] == []
    assert len(amb_capped['shortlist']) == 1

    # Non-overlapping skills both matching strongly with max_shortlist=1 must also respect the shortlist cap
    # (abstain on tight tie at cutoff, never return a selected_id outside the shortlist).
    tight_cap = selector.select_skills(
        'draw a pixel art sprite and synthesize a jump sound effect', max_shortlist=1, storage_root=storage,
    )
    assert len(tight_cap['shortlist']) == 1
    assert set(tight_cap['selected_ids']).issubset({c['skill_id'] for c in tight_cap['shortlist']})

    # 5. Hard eligibility: explorer disabled by policy -> NEVER promoted by score -> unavailable.
    unavail_expl = selector.select_skills('use pxl-art for pixel art sprites', role='explorer', storage_root=storage)
    assert unavail_expl['status'] == 'unavailable'
    assert unavail_expl['selected_ids'] == []
    assert unavail_expl['shortlist'] == []
    assert unavail_expl['unavailable_matches'][0]['reason'] == 'role_disabled_by_policy'

    # Role not allowed on skill (coordinator cannot use pxl-art) -> unavailable.
    unavail_coord = selector.select_skills('use pxl-art for pixel art sprites', role='coordinator', storage_root=storage)
    assert unavail_coord['status'] == 'unavailable'
    assert unavail_coord['selected_ids'] == []
    assert unavail_coord['unavailable_matches'][0]['reason'] == 'role_not_allowed_on_skill'

    # Tampered backing snapshot -> moved to unavailable (snapshot_invalid), never selected.
    (Path(pxl_cap['snapshot_dir']) / 'files' / 'SKILL.md').write_text('TAMPERED', encoding='utf-8')
    unavail_tamper = selector.select_skills('use pxl-art for pixel art sprites', role='implementer', storage_root=storage)
    assert unavail_tamper['status'] == 'unavailable'
    assert unavail_tamper['selected_ids'] == []
    assert any(u['skill_id'] == 'pxl-art' and u['reason'] == 'snapshot_invalid' for u in unavail_tamper['unavailable_matches'])

    # 6. Catalog fingerprint invalidates when triggers change.
    fp1 = managed.catalog_index_fingerprint(storage_root=storage)
    managed.set_triggers('tune-audio', ['sfx', 'wav'], storage_root=storage)
    fp2 = managed.catalog_index_fingerprint(storage_root=storage)
    assert fp1 != fp2

    # 7. Bounded shortlist cap (<= 5) even with > 5 matching skills.
    for i in range(6):
        _adopt(tmp_path, storage, f'shader-{i}', f'Shader Helper {i}', 'GLSL shader graphics pipeline', '# Shaders')
    capped = selector.select_skills('write a GLSL shader graphics pipeline', storage_root=storage)
    assert len(capped['shortlist']) <= selector.MAX_SHORTLIST

    # 8. Technical errors are NEVER masked as no_skill.
    empty_err = selector.select_skills('   ', storage_root=storage, safe=True)
    assert empty_err['status'] == 'error' and empty_err['selected_ids'] == []
    managed.store_path(storage).write_text('{corrupted json', encoding='utf-8')
    corrupt_err = selector.select_skills('draw pixel art', storage_root=storage, safe=True)
    assert corrupt_err['status'] == 'error' and corrupt_err['selected_ids'] == []
    assert corrupt_err['error']


def test_selector_isolates_compound_slugs_and_shared_prefix_skill_families(tmp_path):
    storage = tmp_path / 'storage'
    _adopt(
        tmp_path, storage, 'coord-canary', 'Coord Canary',
        'Coordinator reply tagging convention.', '# Tag\nStart with tag.',
    )
    _adopt(
        tmp_path, storage, 'impl-canary', 'Impl Canary',
        'Adds a canary header marker to edited Python files.', '# Header\nPlace marker.',
    )
    _adopt(
        tmp_path, storage, 'ponytail-audit', 'Ponytail Audit',
        'Runs deep audit checks on code changes.', '# Audit\nVerify invariants.',
    )
    _adopt(
        tmp_path, storage, 'ponytail-debt', 'Ponytail Debt',
        'Tracks technical debt items in repositories.', '# Debt\nList debt.',
    )
    _adopt(
        tmp_path, storage, 'ponytail-gain', 'Ponytail Gain',
        'Measures refactor performance gains.', '# Gain\nBenchmark.',
    )

    # Explicit compound slug mention must select ONLY coord-canary and never spill 'canary' into impl-canary.
    r_exact = selector.select_skills(
        'Using coord-canary, tell me what add(2, 3) returns in one short sentence. Do not edit files.',
        storage_root=storage,
    )
    assert r_exact['status'] == 'selected'
    assert r_exact['selected_ids'] == ['coord-canary']

    # Shared-prefix family queried with spaces ('ponytail audit') must select ONLY ponytail-audit.
    r_family = selector.select_skills('Run ponytail audit on auth.py', storage_root=storage)
    assert r_family['status'] == 'selected'
    assert r_family['selected_ids'] == ['ponytail-audit']

    # Common stopwords like 'any' must not trigger shortlist noise on unrelated prompts.
    r_plain = selector.select_skills('Do not edit any files; list what is here.', storage_root=storage)
    assert r_plain['status'] == 'no_skill'
    assert r_plain['selected_ids'] == []


def test_session_auto_skills_opt_in_freezes_receipt_isolates_roles_and_continues(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))

    _adopt(
        tmp_path, storage, 'pxl-craft', 'PXL Craft',
        'Creates pixel art sprites for 2D games.',
        'PXL_AUTO_BODY_V1.',
        roles=['implementer', 'planner', 'auditor'],
        triggers=['pixel sprite'],
    )
    _adopt(
        tmp_path, storage, 'coord-notes', 'Coord Notes',
        'Coordinator discipline notes.',
        'COORD_EXPLICIT_BODY.',
        roles=['coordinator'],
    )

    # Conversation with auto_skills=True + explicit coord-notes selects pxl-craft for child roles automatically.
    coord_cfg = {
        **config,
        'role': 'explorer',
        'writable': {},
        'verifiers': {},
        'capabilities': ['read', 'list', 'search'],
        'managed_skills': ['coord-notes'],
        'auto_skills': True,
    }
    c1 = Client(steps=[('tare_deliver', {'summary': 'Coordinated.'})])
    out1 = native_agent.run_session(
        coord_cfg, deployment(), custody, 'Please create a pixel sprite for the hero.',
        conversation=True, client=c1, local_counter=c1,
    )
    assert out1['status'] == 'PROPOSED'
    saved = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved['skill_selection']['status'] == 'selected'
    assert saved['skill_selection']['selected_ids'] == ['pxl-craft']
    # Coordinator keeps its explicit skill and NEVER loads pxl-craft body; implementer inherits frozen pxl-craft.
    assert [s['skill_id'] for s in saved['managed_skills']] == ['coord-notes']
    assert [s['skill_id'] for s in managed.frozen_for_role(saved, 'implementer')] == ['pxl-craft']
    assert managed.frozen_for_role(saved, 'explorer') == []
    sys1 = [m['content'] for path, body in c1.calls if path == '/chat/completions'
            for m in body['messages'] if m['role'] == 'system']
    assert any('COORD_EXPLICIT_BODY.' in s for s in sys1)
    assert not any('PXL_AUTO_BODY_V1.' in s for s in sys1)

    # Provider continuation preserves frozen skill_selection receipt and pool even after store removal.
    managed.remove_managed('pxl-craft', storage_root=storage)
    from relay import native_continuity
    cont_custody = tmp_path / 'cont-auto'
    prev = native_continuity.preview(custody, root)
    native_continuity.save_request(cont_custody, prev, prev['text'], deployment())
    c_cont = Client(steps=[('tare_deliver', {'summary': 'Continued.'})])
    native_agent.run_session(
        saved['config'], deployment(), cont_custody, prev['text'],
        conversation=True, client=c_cont, local_counter=c_cont,
    )
    cont_saved = json.loads((cont_custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert cont_saved['skill_selection'] == saved['skill_selection']
    assert [s['skill_id'] for s in managed.frozen_for_role(cont_saved, 'implementer')] == ['pxl-craft']

    # Ordinary prompt with auto_skills=True abstains cleanly (no_skill -> zero skills injected).
    custody2 = tmp_path / 'abstain-session'
    c2 = Client(steps=[('tare_deliver', {'summary': 'Fixed.'})])
    native_agent.run_session(
        {**config, 'capabilities': ['read', 'list'], 'verifiers': {}, 'auto_skills': True},
        deployment(), custody2, 'Rename variable foo to bar.',
        client=c2, local_counter=c2,
    )
    saved2 = json.loads((custody2 / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved2['skill_selection']['status'] == 'no_skill'
    assert 'managed_skills' not in saved2

    # TUI ASSISTANT_CONTEXT boilerplate (contains '/terminal', '/diff', etc.) must NEVER falsely trigger skills.
    from relay.console_entry import ASSISTANT_CONTEXT
    _adopt(
        tmp_path, storage, 'terminal-helper', 'Terminal Helper',
        'Terminal session diagnostics.',
        'TERMINAL_BODY.',
        roles=['implementer', 'planner'],
        triggers=['terminal'],
    )
    tui_abstain = tmp_path / 'tui-abstain'
    c3 = Client(steps=[('tare_deliver', {'summary': 'Renamed.'})])
    native_agent.run_session(
        {**config, 'capabilities': ['read', 'list'], 'verifiers': {}, 'auto_skills': True},
        deployment(), tui_abstain, ASSISTANT_CONTEXT + 'Rename variable foo to bar.',
        conversation=True, client=c3, local_counter=c3,
    )
    saved3 = json.loads((tui_abstain / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved3['skill_selection']['status'] == 'no_skill'
    assert 'managed_skills' not in saved3

    # Real operator mention of 'terminal' after ASSISTANT_CONTEXT still selects terminal-helper.
    tui_match = tmp_path / 'tui-match'
    c4 = Client(steps=[('tare_deliver', {'summary': 'Checked.'})])
    native_agent.run_session(
        {**config, 'capabilities': ['read', 'list'], 'verifiers': {}, 'auto_skills': True},
        deployment(), tui_match, ASSISTANT_CONTEXT + 'Run a terminal diagnostic check on the logs.',
        conversation=True, client=c4, local_counter=c4,
    )
    saved4 = json.loads((tui_match / 'native-agent.json').read_text(encoding='utf-8'))
    assert saved4['skill_selection']['status'] == 'selected'
    assert saved4['skill_selection']['selected_ids'] == ['terminal-helper']


def test_cli_select_triggers_and_agent_auto_skills_flag_wiring(tmp_path, monkeypatch, capsys):
    storage = tmp_path / 'storage'
    proj = tmp_path / 'proj'
    proj.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    _adopt(tmp_path, storage, 'pxl-cli', 'PXL CLI', 'Pixel art tool', 'Draw sprites.')

    assert tare_ops.main(['skills', 'triggers', 'pxl-cli', 'icone 16x16', 'sprite', '--json']) == 0
    trig_view = json.loads(capsys.readouterr().out)
    assert trig_view['triggers'] == ['icone 16x16', 'sprite']

    assert tare_ops.main(['skills', 'select', 'gera um ícone 16x16', '--json']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'selected' and receipt['selected_ids'] == ['pxl-cli']

    assert tare_ops.main(['skills', 'triggers', 'pxl-cli', '--clear', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['triggers'] == []

    # console_backend passes --auto-skills on new sessions when enabled.
    sel = console_backend.Selection(root=str(proj), mode='session', prompt='Do work', auto_skills=True)
    argv = console_backend.command(sel, resume=False)
    assert '--auto-skills' in argv

    # native_agent CLI refuses --auto-skills with --resume.
    rc = native_agent.main(['--resume', '--session', str(tmp_path / 's'), '--auto-skills', '--format', 'json'])
    assert rc == 2
    assert 'native_agent_startup_options_are_saved' in json.loads(capsys.readouterr().out)['reason']


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_skills_panel_auto_skills_toggle_fits_compact_and_wide_screens(tmp_path, monkeypatch, size):
    storage = tmp_path / 'state'
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    _adopt(tmp_path, storage, 'pxl-ui', 'PXL UI', 'Pixel art sprites', 'Draw sprites.')

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.action_skills()
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, SkillsPanel)
            assert panel.region.contains_region(panel.query_one('#skills-manage').region)
            assert panel.region.contains_region(panel.query_one('#skills-view').region)

            menu = panel.query_one('#workbench-list', OptionList)
            toggle_idx = next(i for i, r in enumerate(panel.rows) if r['row_kind'] == 'auto_skills_toggle')
            menu.highlighted = toggle_idx
            await pilot.pause()
            assert str(panel.query_one('#skills-manage', Button).label) == 'Enable'
            await pilot.click('#skills-manage')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            assert app.auto_skills is True
            assert app.selection().auto_skills is True
            assert 'ON (shortlist + abstain)' in panel.current['label']

            assert str(panel.query_one('#skills-manage', Button).label) == 'Disable'
            await pilot.click('#skills-manage')
            await pilot.pause()
            assert app.auto_skills is False
            assert app.selection().auto_skills is False
            assert not app.backend.launches

    asyncio.run(scenario())
