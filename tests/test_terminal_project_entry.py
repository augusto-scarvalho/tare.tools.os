"""Terminal shortcuts reach the existing TUI with caller-owned project scope."""
import os
from pathlib import Path
import sys

import pytest

from relay import native_console, native_installation, native_product


@pytest.fixture
def opened(monkeypatch, tmp_path):
    project = tmp_path/'current project'
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv('TARE_OS_ROOT', raising=False)
    monkeypatch.setenv('TARE_MANAGED_HOME', str(tmp_path/'managed'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    calls = []

    class Console:
        def __init__(self, root, **options):
            calls.append((root, options))

        def run(self):
            pass

    monkeypatch.setattr(native_console, 'WorkConsole', Console)
    return project, calls


@pytest.mark.parametrize('args', [[], ['.'], ['./'], ['console'], ['console', '--root', '.']])
def test_current_directory_opens_without_changing_it(opened, args):
    project, calls = opened
    assert native_installation.main(args) == 0
    assert calls[0][0] == project.resolve()
    assert calls[0][1]['initial_mode'] == 'assistant'
    assert Path.cwd() == project
    assert not list(project.iterdir())


@pytest.mark.parametrize('form', ['absolute', 'relative', 'parent', 'home', 'terminator'])
def test_explicit_folder_with_spaces_and_unicode(opened, monkeypatch, form):
    current, calls = opened
    target = current.parent/'projeto com espaço'
    target.mkdir()
    if form == 'absolute':
        args = [str(target)]
    elif form == 'relative':
        args = [os.path.relpath(target, current)]
    elif form == 'parent':
        target = current.parent
        args = ['..']
    elif form == 'home':
        monkeypatch.setenv('USERPROFILE' if os.name == 'nt' else 'HOME', str(target))
        args = ['~']
    else:
        args = ['--', str(target)]
    assert native_installation.main(args) == 0
    assert calls[0][0] == target.resolve()
    assert Path.cwd() == current
    if target != current.parent:
        assert not list(target.iterdir())


def test_cli_command_keeps_its_owner_even_when_folder_has_same_name(opened, monkeypatch):
    current, calls = opened
    (current/'profiles').mkdir()
    from relay import native_profiles
    seen = []
    monkeypatch.setattr(native_profiles, 'main', lambda args: seen.append(args) or 7)
    assert native_installation.main(['profiles', 'list']) == 7
    assert seen == [['list']] and not calls
    assert native_installation.main(['./profiles']) == 0
    assert calls[0][0] == (current/'profiles').resolve()


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_unavailable_path_fails_before_tui_and_does_not_create_it(opened, capsys, kind):
    current, calls = opened
    target = current/'absent'
    if kind == 'file':
        target.write_text('preserved')
    with pytest.raises(SystemExit) as error:
        native_installation.main([str(target)])
    assert error.value.code == 2 and not calls
    assert 'Folder not found:' in capsys.readouterr().err
    if kind == 'file':
        assert target.read_text() == 'preserved'
    else:
        assert not target.exists()


def test_automation_is_not_silently_started_without_a_terminal(opened, monkeypatch, capsys):
    _, calls = opened
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert native_installation.main([]) == 2
    assert 'interactive terminal' in capsys.readouterr().err and not calls


@pytest.mark.parametrize('arguments,expected', [([], ['console']), (['C:/my project'], ['C:/my project']),
    (['--subscription_only','C:/my project'],['console','--subscription_only','C:/my project']),
    (['C:/my project','--disable_judge'],['C:/my project','--disable_judge'])])
def test_managed_launcher_preserves_caller_folder_and_arguments(tmp_path, monkeypatch, arguments, expected):
    # Use a prepared minimal install without invoking pip or the operator's state.
    home = tmp_path/'home'
    home.mkdir()
    identity = 'a'*64
    release = native_product.release_at(home, identity)
    release.mkdir(parents=True)
    (release/'ready.json').write_text('{}')
    native_product.write_json(home/'active.json', {'current': identity, 'previous': None})
    seen = []
    monkeypatch.setattr(native_product.subprocess, 'call', lambda argv, **options: seen.append((argv, options)) or 0)
    caller = Path.cwd()
    assert native_product.main(['--home', str(home), *arguments]) == 0
    argv, options = seen[0]
    assert argv[-len(expected):] == expected
    assert 'cwd' not in options and Path.cwd() == caller
    assert options['env']['TARE_MANAGED_HOME'] == str(home)


def test_managed_status_remains_explicit_and_does_not_open_tui(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(native_product.subprocess, 'call', lambda *a, **k: pytest.fail('status opened a TUI'))
    assert native_product.main(['--home', str(tmp_path), 'status']) == 0
    assert '"current": null' in capsys.readouterr().out
