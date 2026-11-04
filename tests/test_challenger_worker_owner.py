import os
from pathlib import Path
from types import SimpleNamespace
from relay import challenger


def test_worker_uses_the_importing_owners_code_and_preserves_sdk_paths(tmp_path,monkeypatch):
    import psutil
    monkeypatch.setenv('PYTHONPATH','configured-sdk-root')
    seen=[]
    monkeypatch.setattr(challenger.subprocess,'Popen',lambda args,**kwargs:
        seen.append((args,kwargs)) or SimpleNamespace(pid=123))
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(create_time=lambda:42))
    assert challenger.spawn(tmp_path)==(123,42)
    args,options=seen[0]
    assert args[1:4]==['-P','-m','relay.challenger_worker']
    roots=options['env']['PYTHONPATH'].split(os.pathsep)
    assert Path(roots[0])==Path(challenger.__file__).resolve().parents[1]
    assert roots[1:]==['configured-sdk-root']
    assert os.environ['PYTHONPATH']=='configured-sdk-root'
