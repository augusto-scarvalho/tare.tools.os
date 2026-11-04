"""TUI revision of a failed stage, preserving settled work and prior attempts."""
import asyncio
import copy
import json
import sys
import pytest

from textual.widgets import Input, Select, TextArea
from relay import relay_mesh, work_sequence, work_run, demand_outcome, demand_conduction
from relay.native_console import WorkConsole
from test_mutation_recovery import LocalBackend
from test_demand_outcome import saved
from test_work_sequence import sequence, create, save_graph
from test_work_resume import host
from test_work_run import os_root, _git


def source(case):
    repo=case['repo']; doc=repo/'RULE.md'
    doc.write_text('---\nid: RULE\nstatus: ACCEPTED\nacceptance_criteria:\n  AC-02: second file contains strict\n---\n',encoding='utf-8',newline='\n')
    _git('add','RULE.md',cwd=repo); _git('commit','-qm','corrected premise',cwd=repo)
    return [sys.executable,'-c',"from pathlib import Path; assert Path('src/second.txt').read_text() == 'strict\\n'"]


@pytest.mark.timeout(90)
def test_tui_revises_failed_work_resumes_and_closes_without_repeating_valid_work(sequence,monkeypatch):
    argv=source(sequence)
    from compute_plane import native_models
    client=native_models.NativeHttpClient()
    post=type(client).post
    def reply(self,path,body,control):
        response=post(self,path,body,control)
        if 'AC-02: second file contains strict' in json.dumps(body):
            for block in response.get('output',{}).get('message',{}).get('content',[]):
                if block.get('toolUse',{}).get('name')=='verify':
                    block['toolUse']['input']['name']='AC-02: second file contains strict'
        return response
    monkeypatch.setattr(type(client),'post',reply)
    graph=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    graph['nodes'][1]['metrics']['verification_checks'][0]['command'][-1]=(
        "from pathlib import Path; assert Path('src/second.txt').read_text() == 'legacy\\n'")
    save_graph(graph)
    value=saved(sequence,monkeypatch)
    create(sequence)
    observed={}

    class Process:
        returncode=None
        def __init__(self,args): self.args=args
        async def communicate(self):
            result=await asyncio.to_thread(work_sequence.execute,sequence['dir'],
                invocation_id=self.args[self.args.index('--invocation-id')+1])
            self.returncode=0
            return json.dumps(result).encode(),b''

    class Backend(LocalBackend):
        async def launch(self,args,root):
            # Real sequence/Work execution; the fixture replaces only providers/admission inputs.
            assert args[0]=='sequence' and '--resume' in args,args
            return Process(args)

    async def journey():
        app=WorkConsole(sequence['repo'],backend=Backend())
        async with app.run_test(size=(140,52)) as pilot:
            await app.workers.wait_for_complete(); await app.list_demands().wait()
            app.query_one('#saved-demand',Select).value=value['id']
            app.open_demand(); await app.workers.wait_for_complete()
            app.query_one('#mode',Select).value='sequence'
            app.query_one('#sequence',Input).value=str(sequence['dir'])
            app.execute(True); await app.workers.wait_for_complete()
            failed=app.last_result
            assert failed['status']=='PAUSED',failed
            first=copy.deepcopy(failed['items'][0]); calls=len(sequence['calls'])
            assert first['status']=='SETTLED'
            old_train=work_run.train_dir_for('work-two')
            observed.update(failed=failed,old_files={p:p.read_bytes() for p in old_train.rglob('*') if p.is_file()},
                original_first=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0])
            app.query_one('#mode',Select).value='work'
            app.query_one('#work-id',Input).value='work-two'
            await app.load_sources().wait()
            app.query_one('#source-ref',Select).value='repo-one:RULE.md'
            app.query_one('#criterion-ids',Input).value='AC-02'
            app.query_one('#work-verifier',Input).value=json.dumps(argv)
            app.query_one('#revision-reason',Input).value='The legacy format assumption was false.'
            await app.preview_revision().wait()
            assert app.reviewed_revision,str(app.query_one('#status').render())
            app.confirm_revision(); await app.workers.wait_for_complete()
            assert app.last_result['status']=='REVISED',app.last_result
            app.query_one('#mode',Select).value='sequence'
            app.query_one('#sequence',Input).value=str(sequence['dir'])
            app.query_one('#sequence-revision-reason',Input).value='Adopt corrected rule; keep first result.'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision,str(app.query_one('#status').render())
            view=app.reviewed_sequence_revision[-1]
            assert view['retained']==['work-one'] and [r['work_id'] for r in view['rerun']]==['work-two']
            app.confirm_sequence_revision(); await app.workers.wait_for_complete()
            assert app.last_result['status']=='READY',app.last_result
            assert app.last_result['items'][0]==first
            assert len(sequence['calls'])==calls
            app.execute(True); await app.workers.wait_for_complete()
            assert app.last_result['status']=='COMPLETED',app.last_result
            assert app.last_result['items'][0]==first and len(sequence['calls'])==calls+5
            await app.continue_demand().wait()
            app.query_one('#outcome-summary',TextArea).load_text('Corrected the false premise; preserved the first result and completed the second.')
            await app.review_demand_outcome().wait(); await app.confirm_demand_outcome().wait()
            assert demand_outcome.latest(app.demand_state)['disposition']=='FULFILLED'
            assert demand_conduction.next_step(app.demand_state)['action']=='DEMAND_COMPLETE'
    asyncio.run(journey())
    current=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    assert current['nodes'][0]==observed['original_first']
    assert all(n['completion']['status']=='DONE' for n in current['nodes'])
    assert all(p.read_bytes()==raw for p,raw in observed['old_files'].items() if p.name!='STATE.json')
    assert json.loads(next(p for p in observed['old_files'] if p.name=='STATE.json').read_bytes())['status']=='RETIRED'
    revision=json.loads((sequence['dir']/'sequence.json').read_bytes())['revisions'][-1]
    assert revision['previous']['items']==observed['failed']['items']
    assert revision['retained']==['work-one']
