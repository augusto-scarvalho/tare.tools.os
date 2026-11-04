"""Bounded catalog qualification, never model calls or the operator installation."""
import copy
from contextlib import contextmanager
import json
import zipfile

import pytest

from relay import native_product as product
from test_native_product import bundle, fake_install


def catalog(*names, window=1000):
    return {'object': 'list', 'data': [
        {'id': name, 'object': 'model', 'owned_by': 'tare', 'context_window': window}
        for name in names or ('tare', 'tare-copilot')]}


class Peer:
    def __init__(self, *responses):
        self.responses, self.calls = responses, 0

    def __call__(self):
        value = self.responses[min(self.calls, len(self.responses)-1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def test_fresh_shadow_then_exact_balanced_canary_without_model_calls():
    incumbent = Peer(catalog())
    candidate = Peer(catalog('tare', 'tare-copilot', 'new-model', window=2000))
    receipt = product.run_catalog_trial(incumbent, candidate)
    assert receipt['status'] == 'PASS' and receipt['selected'] == 'candidate'
    # One shadow pair, ten routed requests (8 N / 2 N+1), and fresh N for each N+1 comparison.
    assert (incumbent.calls, candidate.calls) == (11, 3)
    assert receipt['schema'] == 'tare.tools/catalog-transition/1'
    assert len(json.dumps(receipt).encode()) < 8192


def test_model_removal_is_refused_in_shadow_before_any_canary():
    incumbent, candidate = Peer(catalog()), Peer(catalog('tare'))
    result = product.run_catalog_trial(incumbent, candidate)
    assert result['status'] == 'REJECTED' and result['phase'] == 'shadow'
    assert result['selected'] == 'incumbent' and (incumbent.calls, candidate.calls) == (1, 1)


def test_first_canary_error_rolls_routing_back_and_stops_remaining_traffic():
    incumbent, candidate = Peer(catalog()), Peer(catalog(), OSError('unavailable'))
    result = product.run_catalog_trial(incumbent, candidate)
    assert result['status'] == 'REJECTED' and result['phase'] == 'canary'
    assert result['selected'] == 'incumbent' and candidate.calls == 2
    assert incumbent.calls < 11


def test_explicit_error_budget_is_bounded_and_does_not_hide_a_second_failure():
    incumbent = Peer(catalog())
    candidate = Peer(catalog(), TimeoutError(), catalog())
    assert product.run_catalog_trial(incumbent, candidate, max_errors=1)['status'] == 'PASS'
    candidate = Peer(catalog(), TimeoutError())
    result = product.run_catalog_trial(incumbent, candidate, max_errors=1)
    assert result['status'] == 'REJECTED' and result['selected'] == 'incumbent'
    assert candidate.calls == 3


@pytest.mark.parametrize('stage', ['shadow', 'canary'])
def test_broken_incumbent_is_inconclusive_not_evidence_against_successor(stage):
    incumbent = Peer(OSError()) if stage == 'shadow' else Peer(catalog(), OSError())
    candidate = Peer(catalog())
    result = product.run_catalog_trial(incumbent, candidate)
    assert result['status'] == 'INCONCLUSIVE' and result['selected'] == 'incumbent'
    assert candidate.calls <= 1


@pytest.mark.parametrize('override', [
    {'samples': 0}, {'samples': True}, {'samples': 51}, {'samples': 10.5},
    {'canary_percent': 0}, {'canary_percent': 51}, {'canary_percent': True},
    {'canary_percent': 1}, {'max_errors': True}, {'max_errors': -1}, {'max_errors': 2}])
def test_invalid_or_unobservable_allocation_fails_before_requests(override):
    peer = Peer(catalog())
    with pytest.raises(ValueError):
        product.run_catalog_trial(peer, peer, **override)
    assert peer.calls == 0


@pytest.mark.parametrize('value', [
    {}, [], {'object': 'list', 'data': []}, {'object': 'list', 'data': [None]},
    catalog('duplicate', 'duplicate'), catalog(window=True), catalog(window=0),
    catalog(window=-1), catalog(window='1000'), catalog('x'*129)])
def test_malformed_catalog_is_not_a_success(value):
    with pytest.raises(ValueError):
        product.catalog_contract(value)


def test_normalization_keeps_contract_but_not_unknown_payloads_or_order():
    value = catalog('b', 'a', window=None)
    expected = product.catalog_contract(value)
    value['data'].reverse(); value['data'][0]['irrelevant'] = 'private content'
    assert product.catalog_contract(value) == expected
    assert 'private content' not in json.dumps(expected)


def test_unexpected_programmer_failure_is_not_relabelled_as_pass():
    with pytest.raises(RuntimeError):
        product.run_catalog_trial(Peer(catalog()), Peer(RuntimeError('bug')))


@pytest.mark.parametrize('failed_peer,expected', [('incumbent','INCONCLUSIVE'),('candidate','REJECTED')])
def test_worker_startup_failure_has_no_candidate_success(tmp_path,monkeypatch,failed_peer,expected):
    a,b='a'*64,'b'*64
    closed=[]
    @contextmanager
    def worker(home,identifier,folder,deadline):
        peer='incumbent' if identifier==a else 'candidate'
        if peer==failed_peer:raise OSError('startup')
        try:yield Peer(catalog())
        finally:closed.append(peer)
    monkeypatch.setattr(product,'catalog_peer',worker)
    result=product.qualify_transition(tmp_path,a,b)
    assert result['status']==expected and result['selected']=='incumbent'
    assert result['incumbent']==a and result['candidate']==b
    assert closed==([] if failed_peer=='incumbent' else ['incumbent'])
    assert not list(tmp_path.glob('catalog-trial-*'))
    assert json.loads((tmp_path/'last-catalog-trial.json').read_bytes())==result


def test_unconfirmed_cleanup_cannot_report_candidate_selected_or_pass(tmp_path,monkeypatch):
    @contextmanager
    def worker(*args):
        yield Peer(catalog())
        raise OSError('cleanup unconfirmed')
    monkeypatch.setattr(product,'catalog_peer',worker)
    result=product.qualify_transition(tmp_path,'a'*64,'b'*64)
    assert result['status']=='INCONCLUSIVE' and result['selected']=='incumbent'


def test_rejected_shadow_preserves_pointer_receipt_bootstrap_and_user_data(tmp_path,fake_install,monkeypatch):
    home=tmp_path/'home'
    first=product.install(bundle(tmp_path/'one','1'),home)['current']
    sentinel=home/'state/history.json';sentinel.write_text('retained cache/history')
    before={p:p.read_bytes() for p in [home/'active.json',home/'product.py',sentinel]}
    candidate=bundle(tmp_path/'two','2')
    @contextmanager
    def worker(home,identifier,folder,deadline):
        yield Peer(catalog() if identifier==first else catalog('tare'))
    monkeypatch.setattr(product,'catalog_peer',worker)
    # Use the real orchestration, not fake_install's successful-qualification adapter.
    monkeypatch.setattr(product,'qualify_transition',fake_install)
    with pytest.raises(ValueError,match='Catalog transition REJECTED'):
        product.install(candidate,home)
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert product.state(home)['current']==first


def test_same_bundle_does_not_repeat_qualification_or_replace_receipt(tmp_path,fake_install,monkeypatch):
    home=tmp_path/'home';one=bundle(tmp_path/'one','1');product.install(one,home)
    two=bundle(tmp_path/'two','2');current=product.install(two,home)['current']
    receipt=product.release_at(home,current)/'catalog-trial.json'
    before=(home/'active.json').read_bytes(),receipt.read_bytes()
    monkeypatch.setattr(product,'qualify_transition',lambda *a:pytest.fail('idempotent update resampled'))
    product.install(two,home)
    assert ((home/'active.json').read_bytes(),receipt.read_bytes())==before


@pytest.mark.parametrize('outcome',['REJECTED','INCONCLUSIVE'])
def test_auto_qualification_refusal_keeps_selection_and_bounds_failed_storage(tmp_path,fake_install,monkeypatch,outcome):
    home=tmp_path/'home';first=product.install(bundle(tmp_path/'one','1'),home)['current']
    original=product.state(home);candidate=bundle(tmp_path/'two','2')
    identifier=product.digest(candidate/'bundle.json');looked=[];downloads=[]
    monkeypatch.setattr(product,'_restart_locked',lambda *a,**k:'current')
    monkeypatch.setattr(product,'sync_vscode',lambda *a:[])
    monkeypatch.setattr(product,'auto_storage',lambda *a:'off')
    def latest(repo,current,*,after):
        looked.append(after)
        return None if after==identifier else 'product-'+identifier[:16]
    def download(argv,**kw):
        if argv[:3]!=['gh','release','download']:return None
        downloads.append(argv)
        destination=argv[argv.index('--dir')+1]
        from pathlib import Path
        with zipfile.ZipFile(Path(destination)/'bundle.zip','w') as archive:
            for path in candidate.iterdir():archive.write(path,path.name)
    def refused(home,old,new):
        value={'status':outcome,'reason':'controlled failure','incumbent':old,'candidate':new}
        product.write_json(home/'last-catalog-trial.json',value)
        return value
    monkeypatch.setattr(product,'latest_published',latest)
    monkeypatch.setattr(product.subprocess,'run',download)
    monkeypatch.setattr(product,'qualify_transition',refused)
    result=product.auto_update(home)
    actual=product.state(home)
    assert result['installed'] is None and result['qualification']['status']==outcome
    assert all(actual[k]==original[k] for k in ('current','previous','last_transition'))
    assert actual['current']==first and not product.release_at(home,identifier).exists()
    assert (home/'last-catalog-trial.json').is_file() and len(downloads)==1
    if outcome=='REJECTED':
        assert actual['auto_update']=={'paused':False,'after':identifier}
        product.auto_update(home)
        assert looked==[None,identifier] and len(downloads)==1
    else:
        assert (home/'active.json').read_bytes()==json.dumps(original,indent=2,sort_keys=True).encode()+b'\n'


@pytest.mark.parametrize('refusal',[None,'foreign-pid','bytecode-enabled'])
def test_discovery_worker_keeps_private_environment_identity_and_owned_cleanup(tmp_path,monkeypatch,refusal):
    from http.server import ThreadingHTTPServer
    import threading,time
    from relay import native_serve
    seen={}
    class Worker:
        pid=12345
        returncode=None
        def __init__(self,argv,**kwargs):
            seen.update(argv=argv,env=kwargs['env'])
            env=kwargs['env']
            self.server=ThreadingHTTPServer(('127.0.0.1',0),native_serve.handler({},env['TARE_TRIAL_KEY']))
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
            product.write_json(tmp_path/'peer/ready.json',{'pid':self.pid+int(refusal=='foreign-pid'),
                'port':self.server.server_port,'bytecode_disabled':refusal!='bytecode-enabled'})
        def poll(self):return self.returncode
        def terminate(self):
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=3);self.returncode=0
            assert not self.thread.is_alive();seen['terminated']=True
        def wait(self,timeout):assert self.returncode==0;return 0
    monkeypatch.setenv('OPENAI_API_KEY','private-sentinel')
    monkeypatch.setenv('TARE_STORAGE_ROOT','foreign-state')
    monkeypatch.setattr(product.subprocess,'Popen',Worker)
    if refusal:
        with pytest.raises(ValueError,match='identity mismatch'):
            with product.catalog_peer(tmp_path,'a'*64,tmp_path/'peer',time.monotonic()+10):
                pytest.fail('Foreign identity admitted')
    else:
        with product.catalog_peer(tmp_path,'a'*64,tmp_path/'peer',time.monotonic()+10) as fetch:
            assert product.catalog_contract(fetch())
    assert seen['env']['TARE_STORAGE_ROOT']==str(tmp_path/'peer/state')
    assert 'OPENAI_API_KEY' not in seen['env'] and 'PYTHONPATH' not in seen['env']
    assert '-I' in seen['argv'] and '-B' in seen['argv']
    if product.os.name=='nt':
        assert '-S' in seen['argv'] and str(product.python_at(product.release_at(tmp_path,'a'*64)))!=seen['argv'][0]
    assert seen['terminated']
