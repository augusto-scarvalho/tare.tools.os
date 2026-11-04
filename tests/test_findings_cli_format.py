"""Frozen behavior checks against cwd's code."""
import contextlib,io,json,sys
from pathlib import Path
from unittest.mock import patch
from relay import planning_findings as target
assert Path(target.__file__).resolve()==Path(__file__).resolve().parents[1]/'relay/planning_findings.py'


def test_findings_cli_formats_and_compatible_refusals():
    view={'reports':[{'id':'2','question':'Which setting?','artifact':{'session_id':'author-2','profile_digest':'b'*64,
     'payload':{'summary':'Override may differ.','remaining':['Inspect current configuration.']}},
     'sources':[{'repository':'project','path':'settings.py','sha256':'c'*64}],'web_citations':[]}],
     'omitted':[{'id':'1','status':'PENDING','question':'Default?'}],'sha256':'a'*64,'documents':{'private':'raw source'}}
    saved={'path':'C:/custody/findings/manifest.json','sha256':'d'*64,'root':'project'}
    for form in [None,'json','text']:
     for saving in [False,True]:
      args=['--kind','parallel','--source','unused']+(['--format',form] if form else [])+(['--save',view['sha256']] if saving else [])
      out=io.StringIO()
      with patch.object(target,'preview',return_value=view),patch.object(target,'save',return_value=saved) as save,contextlib.redirect_stdout(out):
       assert target.main(args)==0
      text=out.getvalue()
      if form=='text':
       for value in ([saved['path'],saved['sha256']] if saving else ['2','Which setting?','Override may differ.','author-2','settings.py','Inspect current configuration.','1','PENDING',view['sha256']]):assert value in text,value
      else:
       data=json.loads(text)
       assert data==saved if saving else data['sha256']==view['sha256'] and data['documents']=='Snapshot text omitted from display.'
      assert save.call_count==int(saving)
      assert 'raw source' not in text
    for form in ['json','text']:
     out=io.StringIO()
     with patch.object(target,'preview',side_effect=ValueError('source changed')),contextlib.redirect_stdout(out):
      assert target.main(['--kind','parallel','--source','unused','--format',form])==2
     assert 'source changed' in out.getvalue()
    print('PASS: JSON, readable preview/save, no source dump, refusal status.')
