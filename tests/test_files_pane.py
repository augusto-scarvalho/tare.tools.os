import asyncio
import json

import pytest
from textual.widgets import OptionList, Select, TextArea

from relay.files_pane import FilesPane, read_open, save_open, touches
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def row(operation, target, state='succeeded', identity=None, detail='', **extra):
    return dict(kind='tool-activity', id=identity or f's/effect/{operation}/{target}/{state}', operation=operation,
                target=target, state=state, detail=detail, receipt='1 · '+'a'*64, duration_ms=None, **extra)


def source(tmp_path, stage='Implement'):
    from pathlib import Path
    from relay import relay_mesh
    return {'session': str(tmp_path/'c'/'units'/'u'/'session-1'), 'kind': 'session', 'mode': 'conversation',
            'owner': str(tmp_path/'c'), 'project': str(tmp_path.resolve()),
            'control_root': str(Path(relay_mesh.RELAY_ROOT).resolve()), 'stage': stage, 'work': ''}


def test_touches_group_by_turn_and_keep_the_last_state(tmp_path):
    history = ['You: first', row('read', 'a.py'), row('edit', 'a.py'), row('read', 'a.py'), row('search', 'value'),
               'reply', 'You: second', row('read', 'b.py', 'running'),
               row('edit', 'c.py', source=source(tmp_path)), row('edit', 'd.py', detail='Deleted file.'),
               row('folder_read', 'docs: guide.md'), row('read', 'e.py', 'refused', reason='COORDINATOR_ROUTING_REQUIRED')]
    session = {touch.path: touch for touch in touches(history, turn_only=False)}
    assert list(session) == ['a.py', 'b.py', 'c.py', 'd.py', 'docs: guide.md', 'e.py']  # a search is no file
    assert session['a.py'].edited and not session['a.py'].proposed  # an edit outranks the later read
    assert session['b.py'].running and session['c.py'].proposed and session['c.py'].role == 'impl'
    assert session['d.py'].deleted and session['docs: guide.md'].outside
    assert session['e.py'].failed and session['e.py'].reason == 'COORDINATOR_ROUTING_REQUIRED'
    assert [touch.path for touch in touches(history, turn_only=True)] == ['b.py', 'c.py', 'd.py', 'docs: guide.md', 'e.py']


def test_open_state_is_saved_per_machine_and_a_bad_file_is_refused(tmp_path):
    assert read_open(tmp_path) is False
    save_open(tmp_path, True)
    assert read_open(tmp_path) is True
    (tmp_path/'files-pane.json').write_text(json.dumps({'schema': 'other', 'open': True}))
    with pytest.raises(ValueError):
        read_open(tmp_path)


@pytest.mark.parametrize('size', [(150, 52), (80, 24)])
def test_f8_and_slash_files_open_the_pane_and_compact_hides_the_chat(tmp_path, size):
    project = tmp_path/'project'; project.mkdir()
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            pane, detail = app.query_one(FilesPane), app.query_one('#detail')
            assert not pane.display  # closed on a new machine
            await pilot.press('f8'); await pilot.pause()
            assert pane.display and app.focused is app.query_one('#files-touched', OptionList)
            assert detail.display is (size[0] >= 100)
            await pilot.press('f8'); await pilot.pause()
            assert not pane.display and detail.display and app.focused is app.query_one('#prompt', TextArea)
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('/files'); app.execute(False); await pilot.pause()
            assert pane.display and not prompt.text
            app.controls(True)  # a running turn leaves the pane usable
            assert not any(widget.disabled for widget in pane.query('*'))
            app.controls(False)
            await pilot.press('f1'); await pilot.pause()
            assert pane.display is (size[0] >= 140)  # F1 covers it unless there is room for both
            await pilot.press('f1'); await pilot.pause()
            assert pane.display
            assert not backend.launches
        reopened = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant',
                               storage_preferred=tmp_path/'state')
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete(); await pilot.pause()
            assert reopened.query_one(FilesPane).display  # the last state is remembered
    asyncio.run(scenario())


def test_touched_rows_follow_activity_for_every_role_and_survive_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    project = tmp_path/'project'; project.mkdir()
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('f8'); await pilot.pause()
            app.assistant_say('You: change the parser')
            app.assistant_activity({'channel': 'activity', 'activity': row('read', 'parser.py')})
            app.assistant_activity({'channel': 'activity', 'activity': row(
                'edit', 'parser.py', identity='child/effect/1', source=source(project))})
            for operation, target in (('implement', 'W-1 unit'), ('folder_read', 'docs: guide.md')):
                app.assistant_activity({'channel': 'activity', 'activity': row(operation, target)})
            await pilot.pause(.6)
            listing = app.query_one('#files-touched', OptionList)
            labels = [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]
            assert labels == ['p parser.py · impl', 'r docs: guide.md']  # a delegation is no file
            assert 'This turn · read 1 · edited 1' in str(app.query_one('#files-summary').render())
            app.checkpoint('draft_saved'); saved_id = app.demand_id
            app.new_demand(); await pilot.pause(.6)
            assert listing.option_count == 0
            await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved_id
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause(.6)
            # The reopened history is saved_history(): implement saved as an edit, folder_read as a read.
            assert [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)] == labels
            assert app.query_one(FilesPane).found[-1].outside
            assert not app.backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('width', [80, 120, 150])
def test_the_chat_comes_back_when_the_screen_widens_and_f8_brings_a_covered_pane_forward(tmp_path, width):
    project = tmp_path/'project'; project.mkdir()
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            pane, detail = app.query_one(FilesPane), app.query_one('#detail')
            await pilot.press('f8'); await pilot.pause()
            assert pane.display and not detail.display
            await pilot.resize_terminal(width, 40); await pilot.pause()
            assert pane.display and detail.display is (width >= 100)
            await pilot.press('f1'); await pilot.pause()
            assert pane.display is (width >= 140) and detail.display is (width >= 100)
            if not pane.display:  # covered by F1: F8 brings it forward instead of closing it
                await pilot.press('f8'); await pilot.pause()
                assert pane.display and app.panel is None and app.files_open
            assert not app.backend.launches
    asyncio.run(scenario())


def git(root, *args):
    import subprocess
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Pane', '-c', 'user.email=pane@example.invalid',
                           '-c', 'core.hooksPath=/dev/null', *args], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    root = tmp_path/'repo'; (root/'src').mkdir(parents=True)
    git(root, 'init', '-b', 'main')
    (root/'.gitignore').write_text('build/\n'); (root/'src'/'a.py').write_text('a = 1\n'); (root/'b.py').write_text('b = 1\n')
    git(root, 'add', '.'); git(root, 'commit', '-m', 'fixture')
    return root


async def until(pilot, check, seconds=6):
    for _ in range(int(seconds*10)):
        if check():
            return
        await pilot.pause(.1)
    assert check()


def test_list_paths_uses_git_and_falls_back_to_a_bounded_walk(repo, tmp_path):
    from relay.files_pane import list_paths
    (repo/'build').mkdir(); (repo/'build'/'out.bin').write_text('x'); (repo/'new.py').write_text('')
    assert list_paths(repo) == (['.gitignore', 'b.py', 'new.py', 'src/a.py'], False)  # ignored files are absent
    assert list_paths(repo, limit=2) == (['.gitignore', 'b.py'], True)
    plain = tmp_path/'plain'; (plain/'node_modules'/'x').mkdir(parents=True)
    (plain/'node_modules'/'x'/'i.js').write_text(''); (plain/'.env').write_text(''); (plain/'m.py').write_text('')
    assert list_paths(plain) == (['.env', 'm.py'], False)


def test_git_status_marks_numstat_and_untracked_folders(repo, tmp_path):
    from relay.files_pane import changed_dirs, git_status, mark_of
    (repo/'b.py').write_text('b = 2\nc = 3\n'); (repo/'src'/'a.py').unlink()
    (repo/'added.py').write_text('x\n'); git(repo, 'add', 'added.py')
    (repo/'fresh').mkdir(); (repo/'fresh'/'one.py').write_text('')
    status = git_status(repo)
    assert status['marks'] == {'b.py': 'M', 'src/a.py': 'D', 'added.py': 'A'} and status['dirs'] == ('fresh/',)
    assert status['count'] == 4 and status['numstat']['b.py'] == (2, 1)
    status['changed_dirs'] = changed_dirs(status)
    assert mark_of(status, 'fresh/one.py') == 'U' and mark_of(status, 'fresh', True) == 'U'
    assert mark_of(status, 'src', True) == '•' and mark_of(status, '.gitignore') == ''
    (repo/'fresh'/'sub').mkdir(); (repo/'fresh'/'sub'/'two.py').write_text('')
    assert mark_of(status, 'fresh/sub', True) == 'U'  # a folder inside an untracked folder
    (tmp_path/'plain').mkdir()
    assert git_status(tmp_path/'plain') is None


def test_a_project_inside_a_repository_and_unusual_names(repo, tmp_path):
    from relay.files_pane import changed_dirs, git_status, list_paths, mark_of
    package = repo/'pkg'/'sub'; package.mkdir(parents=True)
    (package/'x y.py').write_text('x = 1\n'); git(repo, 'add', '.'); git(repo, 'commit', '-m', 'package')
    (package/'x y.py').write_text('x = 2\n'); (package/'ação.py').write_text(''); (repo/'b.py').write_text('outside\n')
    status = git_status(repo/'pkg')  # paths from the opened folder, not from the repository's top
    assert status['marks'] == {'sub/x y.py': 'M', 'sub/ação.py': 'U'} and status['count'] == 2
    assert status['numstat'] == {'sub/x y.py': (1, 1)}
    assert list_paths(repo/'pkg') == (['sub/ação.py', 'sub/x y.py'], False)
    loose = repo/'loose'/'inner'; loose.mkdir(parents=True); (loose/'l.py').write_text('')
    status = git_status(loose)  # the opened folder is wholly untracked: git names it as one folder
    assert status['marks'] == {} and status['dirs'] == ('',)
    status['changed_dirs'] = changed_dirs(status)
    assert mark_of(status, 'l.py') == 'U' and '' not in status['changed_dirs']
    unborn = tmp_path/'unborn'; unborn.mkdir(); git(unborn, 'init'); (unborn/'n.py').write_text('n\n')
    assert git_status(unborn) == {'marks': {'n.py': 'U'}, 'dirs': (), 'count': 1, 'numstat': {}}


def test_a_merge_conflict_is_marked(repo):
    from relay.files_pane import git_status
    git(repo, 'checkout', '-b', 'other'); (repo/'b.py').write_text('b = 2\n'); git(repo, 'commit', '-am', 'other')
    git(repo, 'checkout', 'main'); (repo/'b.py').write_text('b = 3\n'); git(repo, 'commit', '-am', 'main')
    git(repo, 'merge', 'other')
    assert git_status(repo)['marks'] == {'b.py': '!'}


def test_tree_reads_git_only_while_shown_follows_edits_and_filters_changes(repo, tmp_path, monkeypatch):
    from relay import files_pane
    from relay.files_pane import FileTree
    calls, real = [], files_pane.git_status
    monkeypatch.setattr(files_pane, 'git_status', lambda root, *args: calls.append(root) or real(root, *args))
    monkeypatch.setattr(files_pane, 'SLOW_SECONDS', 60)  # a loaded CI runner must not switch to the 10 s back-off
    async def scenario():
        app = WorkConsole(repo, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause(1)
            assert calls == []  # a hidden pane runs no git
            pane, tree = app.query_one(FilesPane), app.query_one(FileTree)
            await pilot.press('f8')
            await until(pilot, lambda: pane.paths)
            assert set(tree.nodes) == {'.gitignore', 'b.py', 'src'}  # src/ loads when it is expanded
            (repo/'b.py').write_text('b = 2\n')
            await until(pilot, lambda: tree.labels['b.py'] == 'b.py  M')
            (repo/'src'/'new.py').write_text('')
            app.assistant_activity({'channel': 'activity', 'activity': row('edit', 'src/new.py')})
            await until(pilot, lambda: tree.cursor_node is not None and tree.cursor_node.data == ('src/new.py', False))
            assert tree.nodes['src'].is_expanded and tree.labels['src'] == 'src  •'
            await pilot.press('c'); await pilot.pause()
            assert set(tree.nodes) == {'b.py', 'src', 'src/new.py'}  # changed only, expanded
            await pilot.press('f8'); await pilot.pause(1)  # a read started on the last tick may still finish
            seen = len(calls)
            await pilot.pause(4)
            assert len(calls) == seen and not app.backend.launches  # hidden again: no more git
    asyncio.run(scenario())


def test_read_and_diff_text_are_bounded_and_refuse_outside_paths(repo, tmp_path):
    from relay.files_pane import VIEW_BYTES, diff_text, read_text
    (repo/'b.py').write_bytes(b'b = 2\r\nc = 3\r\n'); (repo/'new.py').write_text('fresh\n')
    (repo/'big.txt').write_bytes(b'x'*(VIEW_BYTES+1)); (repo/'bin.dat').write_bytes(b'\0\1')
    assert read_text(repo, 'b.py') == 'b = 2\nc = 3\n'
    assert 'limit' in read_text(repo, 'big.txt') and 'Binary' in read_text(repo, 'bin.dat')
    assert '-b = 1' in diff_text(repo, 'b.py') and '+c = 3' in diff_text(repo, 'b.py')
    assert diff_text(repo, 'new.py') == 'New file, not in Git yet:\n\nfresh\n'
    assert diff_text(repo, '.gitignore') == 'No change since the last commit.'
    for outside in ('../elsewhere.py', str(tmp_path/'x.py')):
        with pytest.raises(ValueError):
            read_text(repo, outside)
    with pytest.raises(ValueError):
        read_text(repo, 'src')  # a folder
    plain = tmp_path/'plain'; plain.mkdir(); (plain/'p.py').write_text('p\n')
    assert diff_text(plain, 'p.py') == 'Not a Git repository: no diff.'


def test_open_diff_mention_and_copy_send_nothing_to_a_model(repo, tmp_path, monkeypatch):
    from relay import files_pane
    from relay.changes_panel import FileViewer
    monkeypatch.setattr(files_pane, 'SLOW_SECONDS', 60)
    (repo/'b.py').write_text('b = 2\n')
    async def scenario():
        app = WorkConsole(repo, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        copies = []; app.copy_to_clipboard = copies.append
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep this draft')
            app.assistant_activity({'channel': 'activity', 'activity': row('edit', 'b.py')})
            await pilot.press('f8')
            pane = app.query_one(FilesPane)
            await until(pilot, lambda: pane.paths and pane.found)
            await pilot.press('enter')
            await until(pilot, lambda: isinstance(app.screen, FileViewer))
            assert app.screen.content == 'b = 2\n'
            await until(pilot, lambda: app.screen.query('#file-viewer-heading'))
            assert str(app.screen.query_one('#file-viewer-heading').content) == 'File · b.py'
            await pilot.press('escape'); await pilot.pause()
            await pilot.press('d')
            await until(pilot, lambda: isinstance(app.screen, FileViewer))
            assert '+b = 2' in app.screen.content and 'What changed' in app.screen.notice
            await until(pilot, lambda: app.screen.query('#file-viewer-heading'))
            assert str(app.screen.query_one('#file-viewer-heading').content) == 'Diff · b.py'
            await pilot.press('escape'); await pilot.pause()
            await pilot.press('a'); await pilot.pause()
            assert app.context_paths == ['b.py'] and app.context_root == str(repo)
            await pilot.press('y'); await pilot.pause()
            assert copies == ['b.py']
            assert prompt.text == 'Keep this draft' and not app.backend.launches  # nothing was sent
    asyncio.run(scenario())


def test_diff_is_literal_runs_no_textconv_and_shows_deletions(repo, tmp_path):
    from relay.files_pane import diff_text
    (repo/'a1.py').write_text('one\n'); (repo/'gone.py').write_text('bye\n')
    (repo/'.gitattributes').write_text('*.py diff=conv\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-m', 'more')
    git(repo, 'config', 'diff.conv.textconv', 'echo CONVERTED')
    (repo/'a1.py').write_text('two\n'); (repo/'a[1].py').write_text('glob\n'); (repo/'gone.py').unlink()
    assert diff_text(repo, 'a[1].py') == 'New file, not in Git yet:\n\nglob\n'  # not a1.py's diff
    changed = diff_text(repo, 'a1.py')
    assert '+two' in changed and 'CONVERTED' not in changed
    assert '-bye' in diff_text(repo, 'gone.py')
    with pytest.raises(ValueError):
        diff_text(repo, '../gone.py')
    unborn = tmp_path/'unborn'; unborn.mkdir(); git(unborn, 'init'); (unborn/'n.py').write_text('n\n')
    git(unborn, 'add', 'n.py')
    assert diff_text(unborn, 'n.py') == 'No commit yet:\n\nn\n'


def test_mention_keeps_the_composer_rules_and_tree_enter_opens_files(repo, tmp_path, monkeypatch):
    from relay import files_pane
    from relay.changes_panel import FileViewer
    from relay.files_pane import FileTree
    monkeypatch.setattr(files_pane, 'SLOW_SECONDS', 60)
    async def scenario():
        app = WorkConsole(repo, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('f8')
            pane, tree = app.query_one(FilesPane), app.query_one(FileTree)
            await until(pilot, lambda: pane.paths)
            assert not app.add_context(str(repo), '../outside.py') and app.context_paths == []
            (tmp_path/'b.py').write_text('x\n')  # exists in the other project, so only the one-project rule refuses
            notices = []; app.notify = lambda message, **kwargs: notices.append(message)
            assert app.add_context(str(repo), 'b.py') and not app.add_context(str(tmp_path), 'b.py')
            assert notices == ['Remove the context from the previous project first.']
            app.context_paths = [f'f{n}.py' for n in range(16)]
            assert not app.add_context(str(repo), '.gitignore')  # the 16-path cap
            tree.focus(); tree.reveal('src'); await until(pilot, lambda: tree.cursor_node is tree.nodes['src'])
            await pilot.press('enter'); await pilot.pause()
            assert tree.nodes['src'].is_expanded and not isinstance(app.screen, FileViewer)  # a folder toggles
            tree.reveal('src/a.py'); await until(pilot, lambda: tree.cursor_node is tree.nodes['src/a.py'])
            await pilot.press('enter')
            await until(pilot, lambda: isinstance(app.screen, FileViewer))
            assert app.screen.content == 'a = 1\n' and not app.backend.launches
    asyncio.run(scenario())


def test_git_line_says_ahead_behind_an_operation_in_progress_and_conflicts(repo, tmp_path):
    from relay.files_pane import git_line, git_state, git_status
    assert git_state(repo) == {'ahead': None, 'behind': None, 'operation': ''}  # no upstream
    origin = tmp_path/'origin.git'
    git(tmp_path, 'init', '--bare', str(origin))
    git(repo, 'remote', 'add', 'origin', str(origin)); git(repo, 'push', '-u', 'origin', 'main')
    (repo/'b.py').write_text('b = 2\n'); git(repo, 'commit', '-am', 'local')
    other = tmp_path/'other'; git(tmp_path, 'clone', '-b', 'main', str(origin), str(other))
    (other/'b.py').write_text('b = 3\n'); git(other, 'commit', '-am', 'remote'); git(other, 'push')
    git(repo, 'fetch')
    assert git_state(repo) == {'ahead': 1, 'behind': 1, 'operation': ''}
    git(repo, 'merge', 'origin/main')  # conflicts on b.py
    status = git_status(repo); status['state'] = git_state(repo)
    assert status['state']['operation'] == 'merge'
    assert git_line(status) == '↑1 ↓1 · 1 changed · merge in progress · !1 conflict'
    assert git_line(None) == 'Not a Git repository'
    git(repo, 'merge', '--abort'); git(repo, 'reset', '--hard', 'origin/main')
    assert git_state(repo/'src') == {'ahead': 0, 'behind': 0, 'operation': ''}  # from a folder inside the repository
    status = git_status(repo); status['state'] = git_state(repo)
    assert git_line(status) == 'No changes'  # in sync: no arrows
    linked = tmp_path/'linked'; git(repo, 'worktree', 'add', '-b', 'side', str(linked), 'HEAD~1')
    (linked/'b.py').write_text('b = 4\n'); git(linked, 'commit', '-am', 'side')
    git(linked, 'rebase', 'main')  # stops on the conflict, in the linked worktree only
    assert git_state(linked)['operation'] == 'rebase' and git_state(repo)['operation'] == ''


def test_history_follows_renames_and_shows_what_a_commit_changed(repo, tmp_path):
    from relay.files_pane import commit_text, file_history
    (repo/'b.py').write_text('b = 2\n'); git(repo, 'commit', '-am', 'change b')
    git(repo, 'mv', 'b.py', 'c.py'); git(repo, 'commit', '-m', 'rename to c')
    commits = file_history(repo, 'c.py')
    assert [line.split(' ', 2)[2].split(' · ')[0] for _, line, _ in commits] == ['rename to c', 'change b', 'fixture']
    assert [name for _, _, name in commits] == ['c.py', 'b.py', 'b.py']
    first = commit_text(repo, commits[-1][0], commits[-1][2])
    assert '+b = 1' in first and 'fixture' in first
    with pytest.raises(ValueError):
        file_history(repo, '../outside.py')
    plain = tmp_path/'plain'; plain.mkdir(); (plain/'p.py').write_text('p\n')
    with pytest.raises(ValueError):
        file_history(plain, 'p.py')


def test_h_opens_the_history_and_enter_opens_a_commit(repo, tmp_path, monkeypatch):
    from relay import files_pane
    from relay.changes_panel import FileViewer
    from relay.files_pane import FileHistory, FileTree
    monkeypatch.setattr(files_pane, 'SLOW_SECONDS', 60)
    (repo/'b.py').write_text('b = 2\n'); git(repo, 'commit', '-am', 'change b')
    async def scenario():
        app = WorkConsole(repo, backend=FakeBackend(), initial_mode='assistant', storage_preferred=tmp_path/'state')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('f8')
            pane, tree = app.query_one(FilesPane), app.query_one(FileTree)
            await until(pilot, lambda: pane.paths)
            tree.focus(); tree.reveal('b.py'); await until(pilot, lambda: tree.cursor_node is tree.nodes['b.py'])
            await pilot.press('h')
            await until(pilot, lambda: isinstance(app.screen, FileHistory))
            assert len(app.screen.commits) == 2
            newest = app.screen.commits[0][1].split(' ', 1)[0]
            await pilot.press('enter')  # the newest commit
            await until(pilot, lambda: isinstance(app.screen, FileViewer))
            assert '+b = 2' in app.screen.content and 'change b' in app.screen.content
            await until(pilot, lambda: app.screen.query('#file-viewer-heading'))
            assert str(app.screen.query_one('#file-viewer-heading').content) == f'Commit {newest} · b.py'
            await pilot.press('escape'); await pilot.pause()
            await pilot.press('escape'); await pilot.pause()
            assert app.screen is app.screen_stack[0] and not app.backend.launches
    asyncio.run(scenario())


def test_history_resists_crafted_subjects_and_works_from_a_folder_inside_the_repository(repo):
    from relay.files_pane import VIEW_BYTES, commit_text, file_history
    (repo/'b.py').write_text('b = 2\n')
    git(repo, 'commit', '-am', 'evil\x1e--output=pwned.txt\tsubject')
    commits = file_history(repo, 'b.py')
    assert len(commits) == 2 and all(len(sha) == 40 and not sha.strip('0123456789abcdef') for sha, _, _ in commits)
    assert '+b = 2' in commit_text(repo, commits[0][0], commits[0][2]) and not (repo/'pwned.txt').exists()
    with pytest.raises(ValueError):
        commit_text(repo, '--output=pwned.txt', 'b.py')  # an option is no revision
    (repo/'src'/'a.py').write_text('a = 2\n'); git(repo, 'commit', '-am', 'change a')
    inside = file_history(repo/'src', 'a.py')  # names come back relative to the opened folder
    assert [name for _, _, name in inside] == ['a.py', 'a.py']
    assert '+a = 2' in commit_text(repo/'src', inside[0][0], inside[0][2])
    (repo/'big.txt').write_text('x'*(VIEW_BYTES+1)); git(repo, 'add', 'big.txt'); git(repo, 'commit', '-m', 'big')
    big = file_history(repo, 'big.txt')
    assert commit_text(repo, big[0][0], big[0][2]) == 'Commit exceeds the interactive preview limit.'
