"""Lossless JSON value diffs for review; application always retains the original Git patch."""
import difflib
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from compute_plane.native_journal import regular

MAX_SOURCE_BYTES=8*1024*1024
MAX_REVIEW_DIFF_BYTES=256*1024


def _object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('Duplicate JSON key')
        value[key]=item
    return value


def _invalid(value):
    raise ValueError('Non-finite JSON number: '+value)


def _float(text):
    value=float(text)
    if not math.isfinite(value) or Decimal(text)!=Decimal(str(value)):
        raise ValueError('JSON numeric precision would be lost')
    return value


def _integer(text):
    if text=='-0':raise ValueError('JSON negative zero would be lost')
    return int(text)


def _json(raw):
    value=json.loads(raw.decode('utf8'),object_pairs_hook=_object,parse_constant=_invalid,
        parse_float=_float,parse_int=_integer)
    parts=[];size=0
    for part in json.JSONEncoder(ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False).iterencode(value):
        size+=len(part.encode('utf8'))
        if size+1>MAX_SOURCE_BYTES:raise ValueError('Canonical JSON exceeds its read bound')
        parts.append(part)
    return value,''.join(parts)+'\n'


def _large_changes(before,after,path='$'):
    """Size diagnostics only; the full diff below still carries every changed value."""
    if before==after:return []
    if isinstance(before,dict) and isinstance(after,dict):
        return [row for key in sorted(before.keys()|after.keys()) for row in
            _large_changes(before.get(key),after.get(key),path+'/'+key.replace('~','~0').replace('/','~1'))]
    if isinstance(before,list) and isinstance(after,list) and len(before)==len(after):
        return [row for i,(a,b) in enumerate(zip(before,after)) for row in _large_changes(a,b,path+'/'+str(i))]
    return [{'path':path,'before_bytes':len(json.dumps(before,ensure_ascii=False).encode()),
             'after_bytes':len(json.dumps(after,ensure_ascii=False).encode())}]


def compact_json_diff(tree,changed):
    from .challenger import git
    from graph_backlog.validation import validate_work_graph
    chunks=[];projections=[];views={}
    for name in sorted(changed):
        raw=git(tree,'diff','--binary','HEAD','--',name)
        if not name.lower().endswith('.json'):
            chunks.append(raw);continue
        try:
            try:before=git(tree,'show','HEAD:'+name)
            except ValueError:
                if git(tree,'ls-tree','-z','HEAD','--',name):raise
                before=None
            path=regular(Path(tree)/name)
            if not path.resolve().is_relative_to(Path(tree).resolve()):raise ValueError('Source redirects outside candidate')
            if path.is_file() and path.stat().st_size>MAX_SOURCE_BYTES:raise ValueError('Source too large')
            after=path.read_bytes() if path.is_file() else None
            if before is not None and len(before)>MAX_SOURCE_BYTES:raise ValueError('Source too large')
            a,left=_json(before) if before is not None else (None,'')
            b,right=_json(after) if after is not None else (None,'')
            for value in (a,b):
                if value is not None and (not isinstance(value,dict)
                        or not {'nodes','edges','sources','meta'}<=value.keys()
                        or validate_work_graph(value)['status']!='PASS'):
                    raise ValueError('Canonical key ordering is scoped to validated Backlog Graphs')
            projection={'path':name,'representation':'backlog-canonical-json-values/1',
                'base_blob_sha256':hashlib.sha256(before).hexdigest() if before is not None else None,
                'candidate_file_sha256':hashlib.sha256(after).hexdigest() if after is not None else None}
            text='diff --git a/'+name+' b/'+name+'\n'
            text+='# Review projection: JSON formatting only is omitted; every changed value is retained.\n'
            text+='# Hunk lines refer to canonical JSON, not the raw source. This is not an application patch.\n'
            text+=''.join(difflib.unified_diff(left.splitlines(keepends=True),right.splitlines(keepends=True),
                fromfile='a/'+name,tofile='b/'+name))
            if left==right:text+='# JSON values are unchanged; raw encoding or formatting differs.\n'
            body=text.encode('utf8');projection['diff_bytes']=len(body)
            projection['largest_changed_values']=sorted(_large_changes(a,b),
                key=lambda r:r['before_bytes']+r['after_bytes'],reverse=True)[:5]
            if after is not None:
                location='canonical-json/'+hashlib.sha256(name.encode('utf8')).hexdigest()[:24]+'.json'
                view=right.encode('utf8');views[location]=view
                projection['canonical_snapshot']={'repository':'review','path':location,
                    'sha256':hashlib.sha256(view).hexdigest()}
            chunks.append(body);projections.append(projection)
        except (ValueError,UnicodeError,RecursionError):
            chunks.append(raw)
    return b''.join(chunks),projections,views


def prepare(tree,changed):
    from .challenger import git
    raw=git(tree,'diff','--binary','HEAD','--')
    raw_hash=hashlib.sha256(raw).hexdigest()
    diff=raw;projections=[];views={}
    if len(raw)>131072:diff,projections,views=compact_json_diff(tree,changed)
    failure=None
    if len(diff)>MAX_REVIEW_DIFF_BYTES:
        failure={'verdict':'INCONCLUSIVE','failure_code':'REVIEW_PACKET_OVERSIZED','requires_candidate_revision':True,
            'reason':f'The candidate exceeds the bounded review file ({MAX_REVIEW_DIFF_BYTES//1024} KiB) after lossless JSON formatting: {len(diff)} bytes. No auditor started. Reduce unnecessary copied data without dropping required evidence.',
            'diff_projection':[{k:v for k,v in p.items() if k!='canonical_snapshot'} for p in projections]}
    return diff,projections,views,raw_hash,failure
