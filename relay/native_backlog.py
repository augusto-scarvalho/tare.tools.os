"""Conversation tools for the bound Backlog, using owner transactions and edit scope."""
from dataclasses import asdict
import copy
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from compute_plane.mediated_tools import MediatedTools
from compute_plane.native_models import encode, object_json
from compute_plane.native_journal import regular
from graph_backlog.jsonutil import compute_revision_hash, UsageError
from graph_backlog.validation import validate_work_graph
from tare_tools_agent_runtime import EffectResult
from tare_ops import read_graph

from . import backlog_delivery


TEXT = {'type': 'string', 'minLength': 1}
RECORD = {'type': 'object', 'properties': {
    'work_id': TEXT, 'summary': TEXT,
    'evidence': {'type': 'array', 'items': TEXT, 'minItems': 1},
    'remaining': {'type': 'array', 'items': TEXT},
    'evidence_grade': {'type': 'string', 'enum': ['A', 'B', 'C', 'D']},
    'evidence_grade_reason': TEXT,
    'source_refs': {'type': 'array', 'items': TEXT},
    'criteria': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'criterion': TEXT, 'evidence': TEXT}, 'required': ['criterion', 'evidence'],
        'additionalProperties': False}}},
    'required': ['work_id', 'summary', 'evidence', 'remaining', 'criteria'], 'additionalProperties': False}
SPECS = {
    'backlog_read': {'description': 'Read one existing node in the saved project backlog, including scope summary, criteria, '
        'progress, pending obligations and the owner revision for recording progress. '
        'For an omitted source catalog, read up to eight IDs from node.source_refs with source_ids, '
        'including when the graph exceeds the generic file reader limit. Source records are metadata, '
        'not proof that their referenced contents were inspected. No full graph dump.',
        'parameters': {'type': 'object', 'properties': {'work_id': TEXT,
            'source_ids': {'type':'array','items':TEXT,'minItems':1,'maxItems':8}},
                       'required': ['work_id'], 'additionalProperties': False}},
    'backlog_record': {'description': 'Persist reviewed progress on an existing node when the user requested '
        'that task. Use expected_revision from backlog_read; reread after conflicts. Supply evidence and '
        'all real remaining obligations. Empty remaining means DONE: explicitly declare evidence_grade A/B/C/D '
        'and a nonempty evidence_grade_reason, plus evidence for every existing exit criterion. No automatic grade '
        'or grade inherited from a previous completion. source_refs are existing registry IDs only; A/B require '
        'a registered source with locator and recoverability. References and grades are claims, not content attestation. '
        'Use during meaningful progress and before delivery. Managed Work uses its settlement owner. '
        'Complete delivery is DONE/done; remaining obligations keep it PARTIAL/IN_PROGRESS. '
        'A historical admission label does not establish a new pending approval. '
        'When the operator authorizes registering inspected local reports, source_files binds each '
        'repository/path/sha256 to a successful read in this conversation. The host registers their '
        'observed hashes and appends source_refs atomically with the record; no automatic grade or attestation. '
        'Does not grant admission or create nodes.',
        'parameters': {'type': 'object', 'properties': {'record': RECORD, 'expected_revision': TEXT,
            'source_files': {'type':'array','minItems':1,'maxItems':8,'items':{
                'type':'object','properties':{'repository':TEXT,'path':TEXT,
                    'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$',
                        'description':'The SHA-256 returned by the successful read of this local file.'}},
                'required':['repository','path','sha256'],'additionalProperties':False}}},
                       'required': ['record', 'expected_revision'], 'additionalProperties': False}},
}


class BacklogTools:
    def __init__(self, tools, config):
        self.tools = tools
        self.graph = config['project_graph']
        self.resolver = MediatedTools(config)
        names = ['backlog_read'] if 'read' in tools.names else []
        try:
            self._write_path()
        except (ValueError, OSError):
            pass
        else:
            if names:
                names.append('backlog_record')
        self.names = (*tools.names, *names)
        specs=copy.deepcopy({n:SPECS[n] for n in names})
        if 'backlog_record' in specs:
            specs['backlog_record']['parameters']['properties']['source_files']['items']['properties']['repository']={
                'type':'string','enum':list(self.resolver.roots),
                'description':'Repository identifier used by the successful read, not a filesystem path.'}
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **specs})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def _write_path(self):
        if 'edit' not in self.tools.names:
            raise ValueError('Backlog editing requires project edit permission.')
        repository = self.graph['repository']
        path = regular(self.graph['path'])
        relative = path.relative_to(self.resolver.roots[repository]).as_posix()
        return self.resolver.path(repository, relative, write=True)

    def _instructions(self, *, write=False):
        instructions = self.tools.instructions
        if instructions is None:
            return []
        try:
            relative = Path(self.graph['path']).relative_to(self.resolver.roots[self.graph['repository']]).as_posix()
        except ValueError:
            return []  # An explicitly bound external graph is observable, never writable here.
        known = {(r['repository'], r['path']) for r in instructions.records()}
        rows = instructions.observe(self.graph['repository'], relative)
        if write and any(r['text'] is not None and (r['repository'], r['path']) not in known for r in rows):
            raise ValueError('Read the backlog node and its scoped project instructions before recording progress.')
        return rows

    def _operation(self, request):
        return {'session_id': self.session_id, 'effect_id': request.effect_id,
                'request_sha256': hashlib.sha256(encode({'request': asdict(request), 'graph': self.graph}).encode()).hexdigest()}

    def _source_records(self, files, control):
        """Register observed versions, never model-authored hashes or authority."""
        from compute_plane.native_capabilities import canonical
        if not isinstance(files,list) or not 1<=len(files)<=8:
            raise ValueError('source_files must contain 1 to 8 inspected local reports.')
        reads=self.tools.observations()['reads']
        sources={}
        for item in files:
            if (not isinstance(item,dict) or set(item)!={'repository','path','sha256'}
                    or any(not isinstance(v,str) or not v.strip() for v in item.values())):
                raise ValueError('Each source_files entry needs repository, path and read sha256.')
            item=canonical(item)
            if control.should_stop():
                raise ValueError('Execution stopped before registering sources.')
            key=item['repository']+':'+item['path']
            if reads.get(key)!=item['sha256']:
                raise ValueError('Read the declared source file in this conversation before registering it: '+key)
            target=self.resolver.path(item['repository'],item['path'])
            if target.resolve()==Path(self.graph['path']).resolve():
                raise ValueError('The bound graph cannot serve as its own source; attach the original report.')
            if self.resolver.observed(item['repository'],item['path'])!=item['sha256']:
                raise ValueError('Source file changed after its read; read it again: '+key)
            locator=target.resolve().as_posix()
            identity='FILE:'+hashlib.sha256((locator+'\0'+item['sha256']).encode()).hexdigest()
            sources[identity]={'kind':'repo_file','type':'LOCAL_FILE_OBSERVATION','title':target.name,
                'recoverability':'RECOVERABLE_REFERENCE','locator':locator,'sha256':item['sha256'],
                'repository':item['repository'],'source_path':item['path'],
                'observed_at':datetime.now(timezone.utc).isoformat(),
                'verification':'Local read hash matched; no producer attestation or new test execution.',
                'authority':'NONE'}
        if control.should_stop():
            raise ValueError('Execution stopped before registering sources.')
        return sources

    def execute(self, request, control):
        name = request.tool_call.name
        if name not in SPECS:
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        writing = False
        try:
            if name not in self.names:
                raise ValueError('Backlog capability is not available in this conversation.')
            args = object_json(request.tool_call.arguments_json)
            parameters=SPECS[name]['parameters']
            if (not set(parameters['required'])<=set(args)<=set(parameters['properties'])
                    or len(encode(args).encode()) > 16384):
                raise ValueError('Use the declared arguments, within 16 KiB.')
            if name == 'backlog_read':
                if not isinstance(args['work_id'], str) or not args['work_id'].strip():
                    raise ValueError('Select an existing Work ID.')
                scoped = self._instructions()
                graph = read_graph(regular(self.graph['path']))
                if validate_work_graph(graph)['status'] != 'PASS':
                    raise ValueError('Invalid Backlog graph. Run backlog validate for details.')
                node = next((n for n in graph['nodes'] if n['id'] == args['work_id']), None)
                if node is None:
                    raise ValueError('Unknown Work ID: '+args['work_id'])
                references=node.get('source_refs',[])
                selected=args.get('source_ids',references)
                if 'source_ids' in args and (not isinstance(selected,list) or not 1<=len(selected)<=8
                        or any(not isinstance(ref,str) or ref not in references for ref in selected)):
                    raise ValueError('source_ids must contain 1 to 8 IDs from this node.source_refs.')
                output = {'path': self.graph['path'], 'revision': compute_revision_hash(graph),
                    'can_record': 'backlog_record' in self.names,
                    'node': {k: node.get(k) for k in ('id', 'canonical_system', 'canonical_id', 'canonical_revision',
                        'title', 'kind', 'summary', 'readiness', 'work_status',
                        'exit_criteria', 'completion', 'admission_state', 'provenance', 'source_refs')},
                    'sources': {ref: graph.get('sources', {}).get(ref) for ref in selected},
                    'delivery': node.get('metrics', {}).get('delivery_latest'),
                    'scoped_project_instructions': scoped, 'authority': 'NONE'}
                if 'source_ids' in args:
                    output.update(source_ids_selected=list(dict.fromkeys(selected)),
                        sources_complete=set(selected)==set(references),
                        sources_omitted=len(set(references)-set(selected)))
                if len(encode(output).encode()) > 24576:
                    # Only registry records are optional; never trim node context or revision.
                    output['sources'] = {}
                    output['sources_complete'] = False
                    output['sources_omitted'] = len(references)
                    output['sources_location'] = {'path': self.graph['path'], 'json_pointer': '/sources'}
                    if selected:
                        if 'source_ids' in args and len(selected)==1:
                            raise ValueError('Selected source record exceeds the 24 KiB node response. '
                                             'Use its original locator through existing access; no contents were inspected.')
                        count=min(8,max(1,len(selected)//2)) if 'source_ids' in args else 8
                        output['sources_request']={'work_id':args['work_id'],'source_ids':selected[:count]}
                    output['sources_notice'] = (
                        'Source records omitted to keep the total response within 24 KiB; '
                        'omitted sources have not been inspected. Use backlog_read with sources_request '
                        'or select up to eight IDs from node.source_refs, reducing the selection if needed. '
                        'For graphs within the generic file reader limit, existing access also permits read at '
                        'sources_location, selecting IDs from node.source_refs (escape ~ as ~0 '
                        'and / as ~1 in JSON pointers). Tool paths remain repository-root-relative; '
                        'external paths require existing folder access. No new authority or permission.')
                    try:
                        relative = Path(self.graph['path']).relative_to(
                            self.resolver.roots[self.graph['repository']]).as_posix()
                    except ValueError:
                        pass  # An external graph binding grants no read-tool path authority.
                    else:
                        if Path(self.graph['path']).stat().st_size<=2*1024*1024:
                            output['sources_read'] = {'repository': self.graph['repository'],
                                                      'path': relative, 'json_pointer': '/sources'}
                    if len(encode(output).encode()) > 24576:
                        raise ValueError('Node response exceeds 24 KiB even without source records. '
                                         'Read selected JSON fields from '+self.graph['path'])
            else:
                path = self._write_path()
                self._instructions(write=True)
                sources=self._source_records(args['source_files'],control) if 'source_files' in args else None
                record=copy.deepcopy(args['record'])
                if sources:
                    if (not isinstance(record,dict) or not isinstance(record.get('source_refs',[]),list)
                            or any(not isinstance(ref,str) or not ref.strip() for ref in record.get('source_refs',[]))):
                        raise ValueError('Record source_refs must be a list of registry IDs.')
                    record['source_refs']=list(dict.fromkeys([*record.get('source_refs',[]),*sources]))
                plan = backlog_delivery.preview(path, [record], sources=sources)
                if control.should_stop():
                    raise ValueError('Execution stopped before recording progress.')
                writing = True
                verify=(lambda:self._source_records(args['source_files'],control)) if sources else None
                graph = backlog_delivery.apply(path, plan, args['expected_revision'],
                    operation=self._operation(request), verify_sources=verify)
                output = {'recorded': args['record']['work_id'], 'revision': compute_revision_hash(graph), 'authority': 'NONE',
                          **({'registered_sources':list(sources)} if sources else {})}
                recorded = next(node for node in graph['nodes'] if node['id'] == output['recorded'])
                output.update(completion_status=recorded['completion']['status'],
                              work_status=recorded['work_status'], remaining=recorded['completion']['residuals'])
            return EffectResult(request.effect_id, 'succeeded', encode(output), name == 'backlog_record')
        except OSError as exc:
            return EffectResult(request.effect_id, 'unknown' if writing else 'refused', encode({'reason': str(exc)}))
        except (ValueError, UsageError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)}))

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        request = pending(journal, self.session_id)
        if request is None or request.tool_call.name not in SPECS:
            return self.tools.recover_effect(journal)
        if request.tool_call.name not in self.names:
            raise ValueError('Recovery capability is not admitted.')
        if request.tool_call.name == 'backlog_record':
            self._write_path()
            args = object_json(request.tool_call.arguments_json)
            graph = read_graph(regular(self.graph['path']))
            operation = self._operation(request)
            for node in graph['nodes']:
                if node['id'] != args.get('record', {}).get('work_id'):
                    continue
                for entry in node.get('metrics', {}).get('delivery_history', []):
                    if entry.get('operation') == operation:
                        return request, EffectResult(request.effect_id, 'succeeded', encode({
                            'recorded': node['id'], 'authority': 'NONE',
                            'recovery': 'Confirmed from atomic graph history. No effect repeated. Read the node for current progress.'}), True)
        return request, EffectResult(request.effect_id, 'refused', encode({
            'reason': 'Interrupted backlog operation has no committed receipt. Read the current node before deciding what remains.'}))
