import json
import subprocess
import zipfile

import pytest

from relay import native_product as product


def write_state(home, current, previous=None):
    home.mkdir(parents=True, exist_ok=True)
    (home/'active.json').write_text(json.dumps({'current': current, 'previous': previous}), encoding='utf-8')


A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Protocol-1 server and scheduler adapter; identical fixture on Linux/Windows."""
    home = tmp_path/'product'
    calls, health, installed = [], [], []
    monkeypatch.setattr(product.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(product, 'status', lambda home: product.state(home))
    monkeypatch.setattr(product, '_port_open', lambda: False)
    monkeypatch.setattr(product.WindowsServeTask, 'available', lambda self: True)
    monkeypatch.setattr(product.WindowsServeTask, 'start', lambda self: calls.append('start'))
    monkeypatch.setattr(product, 'serve_health', lambda: health.pop(0) if len(health) > 1 else health[0])
    def control(identity, owner, action):
        calls.append((identity, owner, action))
        return {**server(home, identity['release']), **identity, 'action': action,
                'draining': action != 'resume', 'stopping': action == 'stop'}
    monkeypatch.setattr(product, 'serve_control', control)
    return home, calls, health, installed


def server(home, release, **extra):
    return {'status': 'ok', 'pid': 7, 'release': release, 'installation': product._installation(home),
            'control_protocol': 1, 'busy': 0, 'stopping': False, **extra}


def test_idle_serve_is_drained_and_stopped_before_start(machine):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health += [server(home, A), server(home, B)]
    assert product.restart_serve(home, wait_seconds=0) == 'restarted'
    assert [c[2] for c in calls if isinstance(c, tuple)] == ['drain', 'stop']
    assert calls[-1] == 'start'
    assert calls[0][1] == calls[1][1]
    assert calls[0][0] == product._identity(server(home, A))


@pytest.mark.parametrize('foreign', [{'status': 'ok'}, {'control_protocol': 0},
                                     {'installation': 'foreign'}, {'release': C}, {'pid': None}])
def test_legacy_or_foreign_server_is_never_stopped(machine, foreign):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append({**server(home, A), **foreign} if foreign != {'status': 'ok'} else foreign)
    assert product.restart_serve(home, wait_seconds=0).startswith('deferred:')
    assert calls == []


def test_busy_deadline_releases_the_same_owners_lease(machine, monkeypatch):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append(server(home, A, busy=2))
    def control(identity, owner, action):
        calls.append((identity, owner, action))
        return {**server(home, A, busy=2), 'action': action, 'draining': action == 'drain'}
    monkeypatch.setattr(product, 'serve_control', control)
    assert 'lease released' in product.restart_serve(home, wait_seconds=0)
    assert [c[2] for c in calls] == ['drain', 'resume']
    assert calls[0][1] == calls[1][1]


@pytest.mark.parametrize('malformed', [None, [], {}, {'busy':0, 'stopping':True, 'action':'stop'}])
def test_malformed_stop_reply_defers_without_starting_another_server(machine, monkeypatch, malformed):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health.append(server(home, A))
    actual = product.serve_control
    def control(identity, owner, action):
        if action == 'stop':
            return malformed
        return actual(identity, owner, action)
    monkeypatch.setattr(product, 'serve_control', control)
    assert product.restart_serve(home, wait_seconds=0).startswith('deferred:')
    assert 'start' not in calls


def test_selected_release_reconciles_before_candidate_lookup(machine, monkeypatch):
    home, calls, health, _ = machine
    write_state(home, B, A)
    health += [server(home, A), server(home, B)]
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: pytest.fail('replay selected another candidate'))
    assert product.auto_update(home, wait_seconds=0)['serve'] == 'restarted'


@pytest.mark.parametrize('with_receipt', [False, True])
def test_prune_keeps_current_and_previous_and_skips_a_release_in_use(tmp_path, monkeypatch, with_receipt):
    home = tmp_path/'product'
    write_state(home, A, B)
    if with_receipt:
        product.write_activation(home, product.state(home), B, 'rollback')
    before = (home/'active.json').read_bytes()
    for name in (A[:16], B[:16], C[:16], 'd' * 16):
        (home/'releases'/name).mkdir(parents=True)
    real_rename = product.os.rename
    monkeypatch.setattr(product.os, 'rename', lambda a, b: (_ for _ in ()).throw(PermissionError())
                        if str(a).endswith('d' * 16) else real_rename(a, b))  # a running TUI holds this one
    product.prune(home)
    assert sorted(p.name for p in (home/'releases').iterdir()) == sorted([A[:16], B[:16], 'd' * 16])
    assert (home/'active.json').read_bytes() == before
    if with_receipt:
        assert product.state(home)['last_transition']['to'] == B
        assert product.state(home)['last_transition']['from'] == A


def test_prune_retries_retired_leftovers_without_touching_selected_or_unknown_folders(tmp_path,monkeypatch):
    home=tmp_path/'product';write_state(home,A,B)
    before=(home/'active.json').read_bytes()
    for name in (A[:16],B[:16],A[:16]+'.delete',C[:16],'operator-notes.delete'):
        p=home/'releases'/name;p.mkdir(parents=True);(p/'keep').write_text('bytes')
    original=product.shutil.rmtree;held=True
    def remove(path,**options):
        if held and path.name==C[:16]+'.delete':return  # a locked file remains after retirement
        return original(path,**options)
    monkeypatch.setattr(product.shutil,'rmtree',remove)
    product.prune(home)
    assert (home/'releases'/(C[:16]+'.delete')/'keep').is_file()
    held=False;product.prune(home)
    assert not (home/'releases'/(C[:16]+'.delete')).exists()
    assert sorted(p.name for p in (home/'releases').iterdir())==sorted([A[:16],B[:16],A[:16]+'.delete','operator-notes.delete'])
    assert (home/'active.json').read_bytes()==before


def test_vs_code_lists_what_serve_publishes_without_touching_anything_else(tmp_path):
    user = tmp_path/'Code'/'User'
    user.mkdir(parents=True)
    models = user/'chatLanguageModels.json'
    other = {'name': 'mine', 'vendor': 'openai', 'models': [{'id': 'gpt'}]}
    mine = {'id': 'tare', 'name': 'tare (agent)', 'url': 'http://old/v1', 'toolCalling': True, 'extra': 1}
    models.write_text(json.dumps([other, {'name': 'tare', 'vendor': 'customendpoint', 'apiKey': 'tare-local',
                                          'apiType': 'chat-completions', 'models': [mine]}]), encoding='utf-8')
    published = [{'id': 'tare', 'context_window': 1000000}, {'id': 'tare-copilot', 'context_window': None},
                 {'id': 'tare-qwen38', 'context_window': 131072}]
    report = product.sync_vscode(published, appdata=tmp_path)
    data = json.loads(models.read_text(encoding='utf-8'))
    assert data[0] == other and report == ['Code: url of tare, added tare-copilot, added tare-qwen38']
    entries = {m['id']: m for m in data[1]['models']}
    assert entries['tare'] == {**mine, 'url': product.SERVE_CHAT}  # the user's values stay
    assert entries['tare-copilot']['vision'] and entries['tare-copilot']['maxInputTokens'] == 131072 - 16384
    assert not entries['tare-qwen38']['vision'] and entries['tare-qwen38']['maxInputTokens'] == 131072 - 16384
    assert len(list(user.glob('chatLanguageModels.json.bak-*'))) == 1
    assert product.sync_vscode(published, appdata=tmp_path) == []  # nothing new: no write, no backup
    assert len(list(user.glob('chatLanguageModels.json.bak-*'))) == 1


def test_files_tare_cannot_safely_edit_are_left_as_they_are(tmp_path):
    for flavour, text in (('Code', '// comments\n[]'),
                          ('Code - Insiders', '[{"name": "tare", "vendor": "someone-else", "models": []}]')):
        (tmp_path/flavour/'User').mkdir(parents=True)
        (tmp_path/flavour/'User'/'chatLanguageModels.json').write_text(text, encoding='utf-8')
    report = product.sync_vscode([{'id': 'tare'}], appdata=tmp_path)
    assert report == ['Code: not plain JSON, left as is', 'Code - Insiders: unexpected structure, left as is']
    assert (tmp_path/'Code'/'User'/'chatLanguageModels.json').read_text(encoding='utf-8') == '// comments\n[]'
    assert product.sync_vscode([{'id': 'tare'}], appdata=tmp_path/'no-vscode') == []  # VS Code not installed


def test_the_newest_publication_wins_when_releases_share_a_commit(monkeypatch):
    # gh lists newest first, and both releases of one OS commit carry that commit's date as createdAt
    listed = [{'tagName': 'product-new', 'createdAt': '2026-10-02T00:29:51Z', 'publishedAt': '2026-10-02T00:38:21Z'},
              {'tagName': 'product-old', 'createdAt': '2026-10-02T00:29:51Z', 'publishedAt': '2026-10-02T00:34:02Z'}]
    monkeypatch.setattr(product, 'ci_green', lambda repo, tag: True)
    monkeypatch.setattr(product.subprocess, 'run',
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listed)))
    assert product.latest_published('owner/repo') == 'product-new'


def test_only_a_release_whose_commit_passed_ci_on_main_is_installed(monkeypatch):
    # A push publishes its release in minutes; its CI takes half an hour and may end red.
    listed = [{'tagName': f'product-{n}', 'createdAt': f'2026-10-05T0{n}:00:00Z'} for n in (1, 2, 3)]
    runs = {'product-3': [{'conclusion': None, 'headBranch': 'main'}],          # still running
            'product-2': [{'conclusion': 'failure', 'headBranch': 'main'}],     # red
            'product-1': [{'conclusion': 'success', 'headBranch': 'main'}]}
    asked = []
    def run(argv, **kw):
        if argv[:2] == ['gh', 'api']:
            return subprocess.CompletedProcess(argv, 0, stdout=argv[2].rsplit('/', 1)[1] + '\n')
        if argv[:3] == ['gh', 'run', 'list']:
            asked.append(argv[argv.index('--commit') + 1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(runs[argv[argv.index('--commit') + 1]]))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listed))
    monkeypatch.setattr(product.subprocess, 'run', run)
    assert product.latest_published('owner/repo') == 'product-1' and asked == ['product-3', 'product-2', 'product-1']
    # Never back: with product-2 installed, the older green product-1 is not a candidate (it would come forward again).
    asked.clear()
    assert product.latest_published('owner/repo', current='2' + 'f' * 63) is None and asked == ['product-3']
    runs['product-3'] = [{'conclusion': 'success', 'headBranch': 'main'}]
    assert product.latest_published('owner/repo', current='2' + 'f' * 63) == 'product-3'
    runs['product-1'] = [{'conclusion': 'success', 'headBranch': 'dev'}]  # green elsewhere is not green on main
    runs['product-3'] = [{'conclusion': 'failure', 'headBranch': 'main'}]
    assert product.latest_published('owner/repo') is None
