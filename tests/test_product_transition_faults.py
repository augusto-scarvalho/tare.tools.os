"""Real product effect cuts; temporary homes, no scheduler/network/process effects."""
from pathlib import Path
import pytest
from relay import native_product as product
from test_native_product import bundle, fake_install
from test_native_product_update import server


@pytest.fixture
def installed(tmp_path, fake_install, monkeypatch):
    home = tmp_path/'home'
    first = product.install(bundle(tmp_path/'one', '1'), home)['current']
    saved = home/'state/history.json'
    saved.write_bytes(b'seeded current durable cache/history')
    candidate = bundle(tmp_path/'two', '2')
    # No task/network side effects: all external boundaries are adapters.
    monkeypatch.setattr(product.WindowsServeTask, 'available', lambda self: True)
    monkeypatch.setattr(product, '_port_open', lambda: False)
    monkeypatch.setattr(product.time, 'sleep', lambda seconds: None)
    return home, first, candidate, saved


@pytest.mark.parametrize('cut', ['prepare', 'ready', 'trial', 'pinned', 'bootstrap',
                                  'launcher', 'fsync', 'replace', 'ack'])
def test_one_shot_install_cut_replays_to_qualified_selected_healthy_target(installed, monkeypatch, cut):
    home, first, candidate, saved = installed
    target = product.digest(candidate/'bundle.json')
    before = (home/'active.json').read_bytes()
    fired = []
    def fail():
        fired.append(cut)
        raise OSError('one-shot '+cut)
    with monkeypatch.context() as patch:
        if cut == 'prepare':
            patch.setattr(product.venv.EnvBuilder, 'create', lambda *a: fail())
        elif cut in ('ready', 'pinned'):
            real = product.write_json
            def write(path, value):
                if Path(path).name == ('ready.json' if cut == 'ready' else 'catalog-trial.json'):
                    fail()
                return real(path, value)
            patch.setattr(product, 'write_json', write)
        elif cut == 'trial':
            patch.setattr(product, 'qualify_transition', lambda *a: fail())
        elif cut == 'launcher':
            real = Path.write_text
            def write_text(path, *a, **kw):
                if path.parent == home and path.name in ('tare', 'tare.cmd'):
                    fail()
                return real(path, *a, **kw)
            patch.setattr(Path, 'write_text', write_text)
        elif cut == 'fsync':
            # Arm only at activation: ready and pinned evidence must already exist.
            real = product.write_activation
            def activation(*a, **kw):
                patch.setattr(product.os, 'fsync', lambda fd: fail())
                return real(*a, **kw)
            patch.setattr(product, 'write_activation', activation)
        else:
            real = product.os.replace
            def replace(source, destination):
                destination = Path(destination)
                hit = destination == home/('product.py' if cut == 'bootstrap' else 'active.json')
                if hit and cut != 'ack':
                    fail()
                result = real(source, destination)
                if hit and cut == 'ack':
                    fail()  # committed seal, caller never received acknowledgement
                return result
            patch.setattr(product.os, 'replace', replace)
        with pytest.raises(OSError, match='one-shot'):
            product.install(candidate, home)
    assert fired == [cut]
    if cut == 'ack':
        sealed = (home/'active.json').read_bytes()
        assert product.state(home)['current'] == target
    else:
        assert (home/'active.json').read_bytes() == before
    product.install(candidate, home)
    if cut == 'ack':
        assert (home/'active.json').read_bytes() == sealed
    selected = product.state(home)
    assert (selected['current'], selected['previous']) == (target, first)
    assert selected['last_transition']['from'] == first
    assert selected['last_transition']['to'] == target
    trial = selected['last_transition']['catalog_trial']
    assert product.digest(home/trial['path']) == trial['sha256']
    product.check_release(home, target)
    monkeypatch.setattr(product, 'serve_health', lambda: server(home, target))
    monkeypatch.setattr(product.WindowsServeTask, 'start', lambda self: pytest.fail('already healthy'))
    assert product.restart_serve(home, wait_seconds=0) == 'current'
    assert saved.read_bytes() == b'seeded current durable cache/history'
    with product.exclusive(home):
        with pytest.raises(OSError):
            with product.exclusive(home):
                pytest.fail('second owner admitted')
    with product.exclusive(home):
        pass  # exception unwinding released the real OS lock


@pytest.mark.parametrize('cut', ['drain', 'stop', 'start', 'stop_ack', 'start_ack'])
def test_control_cut_replays_or_explicitly_rolls_back_without_oscillation(installed, monkeypatch, cut):
    home, first, candidate, saved = installed
    target = product.install(candidate, home)['current']
    running = [first]
    controls = []
    failed = []
    def health():
        return None if running[0] is None else server(home, running[0])
    def control(identity, owner, action):
        controls.append((identity, owner, action))
        if action == 'resume' and running[0] is None:
            raise OSError('server already stopped; release acknowledgement unavailable')
        if action == cut and not failed:
            failed.append(cut)
            raise OSError('lost control acknowledgement')
        reply = {**server(home, identity['release']), **identity, 'action': action,
                 'draining': action != 'resume', 'stopping': action == 'stop'}
        if action == 'stop':
            running[0] = None
        if action == 'stop':
            assert controls[-2][1] == owner  # same scoped drain lease
            if cut == 'stop_ack' and not failed:
                failed.append(cut)
                raise OSError('stop took effect before acknowledgement loss')
        return reply
    def start(self):
        if cut in ('start', 'start_ack') and not failed:
            failed.append(cut)
            if cut == 'start_ack':
                running[0] = product.state(home)['current']
            raise OSError('one-shot start')
        running[0] = product.state(home)['current']
    monkeypatch.setattr(product, 'serve_health', health)
    monkeypatch.setattr(product, 'serve_control', control)
    monkeypatch.setattr(product.WindowsServeTask, 'start', start)
    result = product.restart_serve(home, wait_seconds=0)
    if cut in ('start', 'start_ack'):
        assert result.startswith('rolled back:')
        assert product.state(home)['auto_update']['after'] == target
        assert product.state(home)['last_transition']['operation'] == 'auto_rollback'
    else:
        assert result.startswith('deferred:')
        assert product.state(home)['current'] == target
        assert controls[-1][2] == 'resume'
        assert product.restart_serve(home, wait_seconds=0) == 'restarted'
    before = (home/'active.json').read_bytes()
    for _ in range(2):
        assert product.restart_serve(home, wait_seconds=0) == 'current'
    assert (home/'active.json').read_bytes() == before
    assert running[0] == product.state(home)['current']
    assert saved.read_bytes() == b'seeded current durable cache/history'


def test_real_stale_pair_refuses_effect_and_preserves_lineage(installed):
    home, first, candidate, saved = installed
    stale = product.state(home)
    target = product.install(candidate, home)['current']
    before = (home/'active.json').read_bytes()
    with product.exclusive(home), pytest.raises(ValueError, match='stale transition refused'):
        product.write_activation(home, stale, first, 'rollback')
    assert (home/'active.json').read_bytes() == before
    assert product.state(home)['last_transition']['to'] == target


def test_permanent_activation_failure_stays_unknown_and_preserves_data(installed, monkeypatch):
    home, first, candidate, saved = installed
    before = (home/'active.json').read_bytes()
    real = product.os.replace
    failed = []
    def unavailable(source, destination):
        if Path(destination) == home/'active.json':
            failed.append(1)
            raise OSError('persistent activation storage failure')
        return real(source, destination)
    monkeypatch.setattr(product.os, 'replace', unavailable)
    monkeypatch.setattr(product, 'serve_health', lambda: server(home, first))
    for _ in range(2):
        with pytest.raises(OSError, match='persistent activation'):
            product.install(candidate, home)
        assert (home/'active.json').read_bytes() == before
        assert saved.read_bytes() == b'seeded current durable cache/history'
        assert product.transition_status(home)['completed'] is False
    assert len(failed) == 2  # bounded caller retries, never invented autonomous recovery


@pytest.mark.parametrize('evidence', ['foreign', 'legacy', 'unavailable'])
def test_unknown_evidence_never_stops_or_claims_completion(installed, monkeypatch, evidence):
    home, first, candidate, saved = installed
    product.install(candidate, home)
    value = server(home, first)
    if evidence == 'foreign':
        value['installation'] = 'foreign'
    elif evidence == 'legacy':
        value['control_protocol'] = 0
    else:
        monkeypatch.setattr(product.WindowsServeTask, 'available', lambda self: False)
    monkeypatch.setattr(product, 'serve_health', lambda: value)
    monkeypatch.setattr(product, 'serve_control', lambda *a: pytest.fail('unsafe control'))
    monkeypatch.setattr(product.WindowsServeTask, 'start', lambda self: pytest.fail('unsafe start'))
    before = (home/'active.json').read_bytes()
    assert product.restart_serve(home, wait_seconds=0).startswith('deferred:')
    assert (home/'active.json').read_bytes() == before


@pytest.mark.parametrize('broken', ['stale', 'atomic'])
def test_real_boundary_oracle_detects_deliberately_removed_guard(installed, monkeypatch, broken):
    """Falsifiers of the REAL sealing path, restored by monkeypatch after each test."""
    home, first, candidate, saved = installed
    product.install(candidate, home)
    stale = product.state(home)
    product.install(bundle(candidate.parent/'three', '3'), home)
    before = product.state(home)
    if broken == 'stale':
        monkeypatch.setattr(product, '_require_pair', lambda *a: None)
        with product.exclusive(home):
            product.write_activation(home, stale, first, 'rollback')
        # The destination is ready, but its causal predecessor was overwritten.
        assert product.state(home)['last_transition']['from'] != before['current']
    else:
        real = product.write_json
        def torn(path, value):
            if Path(path) == home/'active.json':
                real(path, {**value, 'last_transition': before['last_transition']})
                raise OSError('torn seal negative control')
            return real(path, value)
        monkeypatch.setattr(product, 'write_json', torn)
        with pytest.raises(OSError, match='torn seal'):
            product.rollback(home)
        observed = product.state(home)
        assert observed['last_transition']['to'] != observed['current']
    assert saved.read_bytes() == b'seeded current durable cache/history'


@pytest.mark.parametrize('resume', [False, True])
def test_failed_manual_target_preserves_operator_receipt_even_after_resume(installed, monkeypatch, resume):
    home, first, candidate, saved = installed
    bad = product.install(candidate, home)['current']
    product.install(bundle(candidate.parent/'three', '3'), home)
    product.rollback(home)
    receipt = product.state(home)['last_transition']
    assert product.state(home)['current'] == bad
    assert receipt['operation'] == 'rollback'
    if resume:
        product.auto_update(home, resume=True)
        assert product.state(home)['auto_update'] == {'paused': False, 'after': None}
    before = (home/'active.json').read_bytes()
    monkeypatch.setattr(product, 'serve_health', lambda: None)
    monkeypatch.setattr(product.WindowsServeTask, 'start',
                        lambda self: (_ for _ in ()).throw(OSError('bad manual target')))
    for _ in range(2):
        with pytest.raises(ValueError, match='rollback release remains selected'):
            product.restart_serve(home, wait_seconds=0)
        assert (home/'active.json').read_bytes() == before
        assert product.state(home)['last_transition'] == receipt
    assert saved.read_bytes() == b'seeded current durable cache/history'
