"""Offline tests for read-only vendor skill catalog discovery, static classification, SHA-256 dedup,
1-click import, live origin drift detection, CLI, and /skills TUI integration (SKL-01c)."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from textual.widgets import Button, OptionList, TextArea

from relay import managed_skills as managed
from relay import vendor_skills as vendor_cat
from relay.console_skills import SkillsPanel
from relay.native_console import WorkConsole
import tare_ops
from test_native_console import FakeBackend


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
    return path


def _link_dir(link_path: Path, target: Path) -> bool:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        try:
            from _winapi import CreateJunction
            CreateJunction(str(target), str(link_path))
            return True
        except OSError:
            return False
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return True
    except OSError:
        return False


def _link_file(link_path: Path, target: Path) -> bool:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link_path)
        return True
    except OSError:
        return False


def _build_fake_home(base: Path) -> Path:
    home = base / 'fake-home'
    # Claude user skills
    _write(home / '.claude' / 'skills' / 'pxl-art', 'SKILL.md',
           '---\nname: PXL Art\ndescription: Draw game sprites with node\n---\nRun npx pxl render sprite.js.\n')
    _write(home / '.claude' / 'skills' / 'pxl-art', 'scripts/render.js', 'console.log("inert");\n')
    _write(home / '.claude' / 'skills' / 'clean-review', 'SKILL.md',
           '---\nname: Clean Review\ndescription: Defect-first code review checklist\n---\nVerify invariants and cite file hashes.\n')
    shared_body = '---\nname: TypeSafe AI\ndescription: Structured output conventions\n---\nValidate JSON schemas strictly.\n'
    _write(home / '.claude' / 'skills' / 'typesafe', 'SKILL.md', shared_body)

    # Claude installed plugin (ponytail-review portable + ponytail-help cli_internal)
    plugin_root = home / '.claude' / 'plugins' / 'cache' / 'ponytail' / '1.0.0'
    _write(plugin_root / 'skills' / 'ponytail-review', 'SKILL.md',
           '---\nname: Ponytail Review\ndescription: Audit diff for over-engineering\n---\nFlag needless abstractions.\n')
    _write(plugin_root / 'skills' / 'ponytail-help', 'SKILL.md',
           '---\nname: Ponytail Help\ndescription: Claude Code plugin commands\n---\nUse /plugin commands in Claude Code.\n')
    _write(home / '.claude' / 'plugins', 'installed_plugins.json', json.dumps({
        'version': 2,
        'plugins': {'ponytail@test': [{'scope': 'user', 'installPath': str(plugin_root)}]},
    }))

    # Codex user skills (duplicate of typesafe + oversized + foreign harness cues)
    _write(home / '.codex' / 'skills' / 'typesafe', 'SKILL.md', shared_body)
    _write(home / '.codex' / 'skills' / 'oversized-design', 'SKILL.md',
           '---\nname: Huge Design\ndescription: Big frontend guide\n---\n' + ('x' * 70000) + '\n')
    _write(home / '.codex' / 'skills' / 'kiro-mixed', 'SKILL.md',
           '---\nname: Kiro Mixed\ndescription: TDD workflow\n---\nUse TDD with subagent dispatch and {{KIRO_DIR}}.\n')

    # Codex .system skills (blocked_capability imagegen, cli_internal skill-installer, portable review-agent)
    _write(home / '.codex' / 'skills' / '.system' / 'imagegen', 'SKILL.md',
           '---\nname: ImageGen\ndescription: Generate images with OpenAI\n---\nSet OPENAI_API_KEY and call images.generate.\n')
    _write(home / '.codex' / 'skills' / '.system' / 'skill-installer', 'SKILL.md',
           '---\nname: Skill Installer\ndescription: Install Codex skills\n---\nInstalls skills into $CODEX_HOME/skills.\n')
    _write(home / '.codex' / 'skills' / '.system' / 'review-agent', 'SKILL.md',
           '---\nname: Review Agent\ndescription: Read-only defect-first review\n---\nReport concrete defects with evidence.\n')

    # AGY builtin skills (cli_internal)
    _write(home / '.gemini' / 'antigravity-cli' / 'builtin' / 'skills' / 'native_swarm', 'SKILL.md',
           '---\nname: Native Swarm\ndescription: AGY swarm orchestration\n---\nUse swarm and invoke_subagent in Antigravity.\n')
    _write(home / '.gemini' / 'antigravity-cli' / 'builtin' / 'skills' / 'agy-customizations', 'SKILL.md',
           '---\nname: AGY Customizations\ndescription: Configure AGY CLI\n---\nEdit ~/.gemini/antigravity-cli rules.\n')
    return home


def test_vendor_catalog_discovery_classification_dedup_and_role_suggestions(tmp_path):
    home = _build_fake_home(tmp_path)
    storage = tmp_path / 'storage'

    default_cat = vendor_cat.discover_vendor_catalog(home=home, storage_root=storage)
    by_slug = {it['slug']: it for it in default_cat['items']}

    # Hidden cli_internal & blocked_capability items are filtered out by default.
    assert 'imagegen' not in by_slug
    assert 'skill-installer' not in by_slug
    assert 'native_swarm' not in by_slug
    assert 'agy-customizations' not in by_slug
    assert 'ponytail-help' not in by_slug
    assert default_cat['hidden_count'] == 5
    assert default_cat['counts_by_class']['blocked_capability'] == 1
    assert default_cat['counts_by_class']['cli_internal'] == 4

    # Actionable items are classified accurately and deduplicated by SHA-256 across vendors.
    assert by_slug['typesafe']['classification'] == 'portable_ready'
    assert by_slug['typesafe']['vendors'] == ['claude', 'codex']
    assert len(by_slug['typesafe']['sources']) == 2

    assert by_slug['clean-review']['classification'] == 'portable_ready'
    assert by_slug['clean-review']['suggested_roles'] == ['plan-auditor', 'auditor']

    assert by_slug['review-agent']['classification'] == 'portable_ready'
    assert by_slug['review-agent']['suggested_roles'] == ['plan-auditor', 'auditor']

    assert by_slug['ponytail-review']['classification'] == 'portable_ready'
    assert by_slug['ponytail-review']['suggested_roles'] == ['plan-auditor', 'auditor']

    assert by_slug['pxl-art']['classification'] == 'local_tool_hybrid'
    assert by_slug['pxl-art']['has_scripts_dir'] is True
    assert by_slug['pxl-art']['suggested_roles'] == ['implementer', 'planner', 'auditor']

    assert by_slug['oversized-design']['classification'] == 'needs_sanitization'
    assert any('exceeds_64kib_limit' in r for r in by_slug['oversized-design']['reasons'])
    assert by_slug['kiro-mixed']['classification'] == 'needs_sanitization'

    # Never suggest disabled roles (explorer/researcher/vision/heads_up).
    for it in default_cat['items']:
        assert not (set(it['suggested_roles']) & managed.DEFAULT_DISABLED_ROLES)

    # include_hidden=True exposes blocked_capability & cli_internal with explicit reasons.
    full_cat = vendor_cat.discover_vendor_catalog(home=home, include_hidden=True, storage_root=storage)
    full_by_slug = {it['slug']: it for it in full_cat['items']}
    assert full_by_slug['imagegen']['classification'] == 'blocked_capability'
    assert full_by_slug['imagegen']['suggested_roles'] == []
    assert full_by_slug['native_swarm']['classification'] == 'cli_internal'
    assert full_by_slug['ponytail-help']['classification'] == 'cli_internal'


def test_linked_top_level_vendor_root_resolves_safely_while_internal_links_stay_refused(tmp_path):
    home = tmp_path / 'home'
    storage = tmp_path / 'storage'
    real_pxl = tmp_path / 'side-projects' / 'claude-pxl'
    _write(real_pxl, 'SKILL.md', '---\nname: Linked PXL\ndescription: Draw pixel sprites\n---\nDraw sprites.\n')

    linked_root = home / '.claude' / 'skills' / 'pxl-link'
    if not _link_dir(linked_root, real_pxl):
        pytest.skip('Directory links/junctions unavailable on this host')

    cat = vendor_cat.discover_vendor_catalog(home=home, storage_root=storage)
    assert len(cat['items']) == 1
    item = cat['items'][0]
    assert item['slug'] == 'pxl-link'
    assert item['linked_root'] is True
    assert Path(item['capture_root']).resolve() == real_pxl.resolve()

    imported = vendor_cat.import_vendor_skill('pxl-link', home=home, storage_root=storage)
    assert imported['skill_id'] == 'pxl-link'
    assert imported['origin']['origin_vendor'] == 'claude'
    assert Path(imported['origin']['origin_path']).resolve() == real_pxl.resolve()

    # An internal symlink inside the bundle target MUST still be refused fail-closed by capture_bundle.
    real_bad = tmp_path / 'side-projects' / 'bad-skill'
    secret = tmp_path / 'outside-secret.txt'
    secret.write_text('TOP_SECRET', encoding='utf-8')
    _write(real_bad, 'SKILL.md', '---\nname: Bad Skill\n---\nReferences evil.md.\n')
    if _link_file(real_bad / 'evil.md', secret):
        assert _link_dir(home / '.claude' / 'skills' / 'bad-link', real_bad)
        with pytest.raises(ValueError, match='[Ll]inked|reparse'):
            vendor_cat.import_vendor_skill('bad-link', home=home, storage_root=storage)


def test_cli_catalog_import_and_live_origin_drift_update(tmp_path, monkeypatch, capsys):
    home = _build_fake_home(tmp_path)
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setenv('TARE_VENDOR_SKILLS_HOME', str(home))

    assert tare_ops.main(['skills', 'catalog', '--json']) == 0
    cat = json.loads(capsys.readouterr().out)
    assert cat['hidden_count'] == 5
    assert any(it['slug'] == 'clean-review' for it in cat['items'])

    # Refuses importing oversized (>64 KiB) skill fail-closed.
    assert tare_ops.main(['skills', 'import', 'oversized-design']) == 2
    assert 'exceeds' in capsys.readouterr().err

    # 1-click import uses classifier's suggested roles and records vendor provenance.
    assert tare_ops.main(['skills', 'import', 'clean-review', '--json']) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported['skill_id'] == 'clean-review'
    assert imported['effective_roles'] == ['plan-auditor', 'auditor']
    assert imported['origin']['origin_vendor'] == 'claude'
    assert imported['origin_drift']['available'] is False

    # Edit live vendor SKILL.md on disk -> drift detected with reviewable diff without auto-mutating managed skill.
    origin_md = home / '.claude' / 'skills' / 'clean-review' / 'SKILL.md'
    origin_md.write_text('---\nname: Clean Review\n---\nUpdated v2 review rubric.\n', encoding='utf-8', newline='\n')
    managed.set_overlay('clean-review', 'My custom review overlay.\n', storage_root=storage)

    assert tare_ops.main(['skills', 'effective', 'clean-review', '--json']) == 0
    eff = json.loads(capsys.readouterr().out)
    assert eff['origin_drift']['available'] is True
    assert '+Updated v2 review rubric.' in eff['origin_drift']['skill_md_diff']
    assert eff['effective_text'] == 'My custom review overlay.\n'

    # Explicit --from-origin --keep-overlay re-captures origin, updates base, clears drift, keeps overlay.
    assert tare_ops.main(['skills', 'update', 'clean-review', '--from-origin', '--keep-overlay', '--json']) == 0
    synced = json.loads(capsys.readouterr().out)
    assert synced['origin_drift']['available'] is False
    assert synced['has_overlay'] is True
    assert synced['effective_text'] == 'My custom review overlay.\n'
    assert 'Updated v2 review rubric.' in synced['base_text']


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_vendor_catalog_import_hidden_toggle_and_live_drift_update(tmp_path, monkeypatch, size):
    home = tmp_path / 'vendor-home'
    storage = tmp_path / 'state'
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setenv('TARE_VENDOR_SKILLS_HOME', str(home))

    shared_md = '---\nname: Clean Review\ndescription: Defect-first review\n---\nCheck invariants v1.\n'
    _write(home / '.claude' / 'skills' / 'clean-review', 'SKILL.md', shared_md)
    _write(home / '.codex' / 'skills' / 'clean-review', 'SKILL.md', shared_md)
    _write(home / '.codex' / 'skills' / '.system' / 'imagegen', 'SKILL.md',
           '---\nname: ImageGen\n---\nRequires OPENAI_API_KEY and images.generate.\n')
    _write(home / '.gemini' / 'antigravity-cli' / 'builtin' / 'skills' / 'native_swarm', 'SKILL.md',
           '---\nname: Native Swarm\n---\nUses invoke_subagent and swarm.\n')

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

            # Row 0 is deduplicated [claude+codex] clean-review; hidden skills collapsed into toggle row.
            assert panel.current['row_kind'] == 'vendor'
            assert '[claude+codex] clean-review' in panel.current['label']
            assert any(r['row_kind'] == 'vendor_hidden_toggle' for r in panel.rows)
            assert not any(r.get('slug') == 'imagegen' for r in panel.rows)

            # Toggle hidden vendor skills ON, verify imagegen appears, then toggle back OFF.
            menu = panel.query_one('#workbench-list', OptionList)
            toggle_idx = next(i for i, r in enumerate(panel.rows) if r['row_kind'] == 'vendor_hidden_toggle')
            menu.highlighted = toggle_idx
            await pilot.pause()
            assert str(panel.query_one('#skills-manage', Button).label) == 'Show'
            await pilot.click('#skills-manage')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            assert any(r.get('slug') == 'imagegen' for r in panel.rows)
            await pilot.click('#skills-manage')
            await pilot.pause(.4)
            assert not any(r.get('slug') == 'imagegen' for r in panel.rows)

            # 1-click Import clean-review into managed skills with suggested roles.
            clean_idx = next(i for i, r in enumerate(panel.rows) if r.get('slug') == 'clean-review')
            menu.highlighted = clean_idx
            await pilot.pause()
            assert str(panel.query_one('#skills-manage', Button).label) == 'Import'
            await pilot.click('#skills-manage')
            await pilot.pause()
            assert panel.current['row_kind'] == 'managed'
            assert panel.current['skill_id'] == 'clean-review'
            assert panel.current['effective_roles'] == ['plan-auditor', 'auditor']

            # Mutate origin file on disk -> reload panel, verify drift diff, click Update.
            (home / '.claude' / 'skills' / 'clean-review' / 'SKILL.md').write_text(
                '---\nname: Clean Review\ndescription: Defect-first review\n---\nCheck invariants v2.\n',
                encoding='utf-8', newline='\n',
            )
            panel._reload_and_select('managed:clean-review', 'Reloaded.')
            await pilot.pause()
            assert 'drift' in panel.current['label']
            assert panel.query_one('#skills-update', Button).disabled is False
            await pilot.click('#skills-view')
            await pilot.pause()
            assert '=== Base -> Origin (live) ===' in panel.query_one('#workbench-preview', TextArea).text
            assert '+Check invariants v2.' in panel.query_one('#workbench-preview', TextArea).text

            await pilot.click('#skills-update')
            await pilot.pause()
            assert 'Check invariants v2.' in panel.query_one('#workbench-preview', TextArea).text
            assert 'drift' not in panel.current['label']
            assert not app.backend.launches

    asyncio.run(scenario())
