"""Frozen exploration settings and receipts shared by native flows."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from compute_plane.native_journal import regular, save_json

DEFAULT = dict(enabled=False, profile='local-qwen', overflow=None, calls=4, requests=2,
               seconds=90, max_usd='0', challenger=None, sample_percent=0)


def policy_path():
    from .native_installation import storage_root
    return regular(storage_root()/'delegation'/'policy.json')


def policy():
    from .native_agent import read_json
    if os.environ.get('TARE_DELEGATION_POLICY', '').lower() == 'off':
        return deepcopy(DEFAULT)
    path = regular(Path(os.environ['TARE_DELEGATION_POLICY'])) if os.environ.get('TARE_DELEGATION_POLICY') else policy_path()
    # A policy saved before a field existed gets its default (overflow arrived on 2026-10-03).
    return {**deepcopy(DEFAULT), **read_json(path, 65536)} if path.exists() else deepcopy(DEFAULT)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or type(value['enabled']) is not bool:
        raise ValueError('Use the exploration settings fields.')
    for key, low, high in [('calls', 1, 32), ('requests', 1, 8), ('seconds', 1, 900), ('sample_percent', 0, 100)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('Invalid exploration limit: '+key)
    if not isinstance(value['max_usd'], str):
        raise ValueError('Exploration USD allowance must be a decimal string.')
    try:
        amount = Decimal(value['max_usd'])
        if not amount.is_finite() or amount < 0:
            raise ValueError()
    except Exception:
        raise ValueError('Exploration USD allowance must be nonnegative.') from None
    if not value['enabled']:
        return value
    from .challenger_variants import realize
    for key in ('profile', 'challenger', 'overflow'):
        if value[key] is None and key != 'profile':
            continue
        variant = realize(value[key])
        if isinstance(value[key], dict) and 'task_types' in value[key] and 'exploration' not in variant['task_types']:
            raise ValueError('Exploration variants must include the exploration task type.')
        if variant['accounting'] == 'usd' and amount <= 0:
            raise ValueError('Metered exploration needs a positive USD allowance.')
        if key == 'overflow' and variant['accounting'] == 'local':
            raise ValueError('The overflow explorer takes over from the local GPU: choose a hosted one.')
    if value['sample_percent'] and value['challenger'] is None:
        raise ValueError('Select a challenger before sampling exploration tasks.')
    return value


# Experiments over exploration, kept apart from the policy so code that predates them (a development checkout)
# still accepts the policy. TARE_EXPLORE_DECIDER overrides the decider for one process. The decider defaults to Jev,
# the rule taking over when Jev does not answer: on 2026-10-04 Jev put an answer first in 10 of 11 tasks (the rule
# in 3) and was never worse at 10 or more; judging 60 candidates 32 at once took about 1 s and US$0.001. Off
# restores the refusal.
EXPERIMENTS = {
    'decider': 'jev',               # off | shadow | rule | jev: who chooses the candidates explorers judge
    'ordered_candidates': True,     # explorers see their candidates best first, the likely ones marked
    'adaptive_k': False,            # with Jev, as many candidates as it finds likely, 10 to 40 per explorer
    'instruction_flags': True,      # comments and documents that instruct agents are marked as data
    'candidate_windows': False,     # each candidate comes with the lines around it, and the map is a short one
    # On since 2026-10-05: on 12 tasks it kept 150 of 157 answers (the explorer 149 of 155) with 23 explorers, not 40.
    'jev_first': True,              # with Jev and a small set, no explorer: the session judges the lines shown
    'markdown_candidates': False,   # search candidates in Markdown too, not only code
    'contradiction_flags': False,   # candidates that contradict a premise of the question are marked
    'location_status': False,       # explorers mark each location supported or hypothesis, and conclude
    'previous_locations': False,    # candidates may be the locations of an earlier investigation
    'specgraph_candidates': False,  # candidates may be a symbol's references in a published SpecGraph index
    # Ways of working, phase 1: the session may write a plan of units into its ledger (work_forms.PlanTools).
    # Off until its bench (12 planning requests, direct against plan) says otherwise, per class.
    'plan_tool': False,
    'planner_child': False,        # a bounded read-only planner on a configured planner route
    # Ways of working, phase 2: the session may hand a bounded unit to a child implementer (work_units).
    # Off until its bench (12 bounded tasks, direct against unit) says otherwise, per class.
    'implement_child': False,
    # Phase 3 (D8): units the session hands at once run in parallel, 2 at a time, without it asking each time.
    # Off: the session asks for it per call. Wall time against conflicts, rework and quota is the bench's to say.
    'implement_parallel': False,
    # A child implementer runs on the session's own bill (class and vendor). On: any hosted route of the ladder, as
    # before 2026-10-07, when a Codex quota session climbed into Bedrock dollars and the Claude CLI's quota.
    'child_routes_any': False,
    # Role guidance, phase 2: an explorer child reads the repository's root AGENTS.md (on: as before). Off when ~10
    # measured tasks say the answers hold without it (an explorer answers a bounded question; the file is the
    # repository's rules for changing it).
    'leaf_project_instructions': True,
}
DECIDERS = ('off', 'shadow', 'rule', 'jev')
# A result can change with the session's class of route (challenger_variants.accounting): a value per class takes
# precedence over the one above for sessions of that class. Jev first was measured with hosted sessions judging the
# lines; a local session (Qwen 3.8 27B) is not measured yet, so it keeps its explorer (2026-10-05).
CLASSES = ('local', 'quota', 'usd')
CLASS_DEFAULTS = {'local': {'jev_first': False}}


def experiments_path():
    return policy_path().parent/'experiments.json'


def typed(saved):
    return {k: v for k, v in saved.items() if k in EXPERIMENTS and type(v) is type(EXPERIMENTS[k])} \
        if isinstance(saved, dict) else {}


def class_values():
    """{class: {experiment: value}} where a class departs from the values above, defaults and saved."""
    from .native_agent import read_json
    try:
        path = experiments_path()
        saved = read_json(path, 65536).get('classes', {}) if path.is_file() else {}
    except (ValueError, OSError, AttributeError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    return {c: v for c in CLASSES if (v := {**CLASS_DEFAULTS.get(c, {}), **typed(saved.get(c))})}


def experiments(route_class=None):
    """The experiments in effect, for a session of `route_class` when given (local, quota or usd)."""
    from .native_agent import read_json
    value = dict(EXPERIMENTS)
    try:
        path = experiments_path()
        if path.is_file():
            value.update(typed(read_json(path, 65536)))
    except (ValueError, OSError):
        pass  # an unreadable file leaves every experiment off
    value.update(class_values().get(route_class, {}))
    if os.environ.get('TARE_EXPLORE_DECIDER') in DECIDERS:
        value['decider'] = os.environ['TARE_EXPLORE_DECIDER']
    if value['decider'] == 'local':
        value['decider'] = 'jev'  # the local judge left exploration on 2026-10-04; Jev took its place
    if value['decider'] not in DECIDERS:
        value['decider'] = 'off'
    return value


def save_experiments(route_class=None, **changes):
    """Save experiment values for every session, or for sessions of `route_class` only."""
    unknown = set(changes) - set(EXPERIMENTS)
    if unknown or any(type(v) is not type(EXPERIMENTS[k]) for k, v in changes.items()) or \
            changes.get('decider', 'off') not in DECIDERS:
        raise ValueError('Experiments: decider is one of ' + ', '.join(DECIDERS) + '; the others are true or false.')
    if route_class is not None and route_class not in CLASSES:
        raise ValueError('Experiment classes: ' + ', '.join(CLASSES) + '.')
    path = experiments_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from .native_agent import read_json
    saved = read_json(path, 65536) if path.is_file() else {}
    value = {**dict(EXPERIMENTS), **{k: v for k, v in saved.items() if k in EXPERIMENTS}}
    classes = saved.get('classes') if isinstance(saved.get('classes'), dict) else {}
    if route_class is None:
        value.update(changes)
    else:
        classes = {**classes, route_class: {**typed(classes.get(route_class)), **changes}}
    save_json(path, {**value, **({'classes': classes} if classes else {})})
    return value if route_class is None else experiments(route_class)


def configure(value):
    validate(value)
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, value)
    return value


def swaps(variant, parent):
    """A local explorer other than a local session's own model would swap models on the single GPU."""
    from .challenger_variants import accounting
    profile, own = variant['deployment']['native_profile'], parent['native_profile']
    return accounting(parent) == 'local' and (profile['endpoint'], profile['model']) != (own['endpoint'], own['model'])


def scoped(frozen,parent=None):
    """Project a saved explorer onto this launch's allowed routes, without changing custody."""
    from .execution_scope import current,for_role
    if not frozen or not current().changes('explorer'):return frozen
    if current().pin('explorer'):
        from .execution_scope import pinned
        from .challenger_variants import realize, accounting, digest
        deployment=pinned('explorer')
        choice=deployment['native_model_choice']
        variant=realize({'profile':choice['connection'],'reasoning':choice['reasoning'],
                         'task_types':['exploration']})
        variant['deployment']=deployment
        variant['accounting']=accounting(deployment)
        variant['key']=digest([deployment,variant['task_types'],variant['identity']])
        return {**frozen,'variant':variant,'overflow':None,'experimental':False,
                'selection':'EXACT_TEMPORARY_PIN'}
    from .environment import allows
    from .turn_engine import avoid_local_gpu,available
    from .challenger_variants import realize
    from .native_routes import ranked
    def eligible(variant):
        return (allows(variant['deployment'])[0] and not (variant['accounting']=='local' and (
            avoid_local_gpu() or parent and parent.get('native_profile') and swaps(variant,parent))))
    with for_role('explorer'):
        candidates=[v for v in (frozen['variant'],frozen.get('overflow')) if v and eligible(v)]
        if not candidates:
            deployments,selection=ranked('explorer')
            for deployment in deployments:
                row=next(r for r in selection['candidates'] if r.get('eligible')
                         and r.get('selected_deployment')==deployment['id'])
                name=row.get('catalog',deployment['id'])
                live,_=available(name)
                if live is None:continue
                try:variant=realize({'profile':name,'task_types':['exploration']})
                except (ValueError,OSError,KeyError,TypeError):continue
                if eligible(variant):candidates.append(variant);break
        if not candidates:return None
        return {**frozen,'variant':candidates[0],'overflow':next((v for v in candidates[1:]
            if v['accounting']!='local'),None),'experimental':False,'selection':'TEMPORARY_TUI_EXECUTION_POLICY'}


def snapshot(session_id, role, parent=None):
    """Freeze the explorer for a new session: always the configured one, with its own model, effort and
    parameters, never the session's. A local explorer has one slot; the investigations beyond it go to the
    configured hosted `overflow` explorer at once. When the local explorer cannot run (this conversation leaves
    the local GPU, or it would swap the local session's model), the overflow explorer takes all of them;
    without one the session does not delegate.
    """
    if role == 'explorer':
        return None  # Leaf sessions never recursively delegate.
    value = policy()
    from .execution_scope import current
    if not value['enabled'] and not current().pin('explorer'):
        return None
    validate(value)
    if not value['enabled']:  # an explorer pinned for this launch delegates while the configured one is off
        return scoped(dict(settings=value, qualification='UNQUALIFIED', task_type='exploration'), parent)
    from .challenger_variants import realize
    from .turn_engine import avoid_local_gpu
    sampled = int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 100 < value['sample_percent']
    variant = realize(value['challenger'] if sampled else value['profile'])
    overflow = realize(value['overflow']) if value['overflow'] is not None else None
    selection = 'CONFIGURED_EXPLORER'
    if current().changes('explorer'):
        return scoped(dict(settings=value,variant=variant,overflow=overflow,experimental=sampled,
            selection=selection,qualification='UNQUALIFIED',task_type='exploration'),parent)
    from .environment import allows
    if overflow is not None and not allows(overflow['deployment'])[0]:
        overflow = None  # this machine's setup declines it
    if not allows(variant['deployment'])[0] or variant['accounting'] == 'local' and (
            avoid_local_gpu() or parent is not None and 'native_profile' in parent and swaps(variant, parent)):
        if overflow is None:
            return None
        variant, overflow, sampled, selection = overflow, None, False, 'OVERFLOW_EXPLORER'
    frozen = dict(settings=value, variant=variant, experimental=sampled, selection=selection,
                  qualification='UNQUALIFIED', task_type='exploration')
    if variant['accounting'] == 'local' and overflow is not None:
        frozen['overflow'] = overflow
    return frozen


def records(root):
    from .native_agent import read_json
    directory = regular(Path(root)/'delegations')
    return [read_json(regular(p), 1024*1024) for p in sorted(directory.glob('*/receipt.json'))]


def children(root):
    """Derive paths from custody, never follow a receipt-supplied arbitrary path."""
    directory = regular(Path(root)/'delegations')
    return [regular(p.parent/'session') for p in sorted(directory.glob('*/receipt.json'))
            if regular(p.parent/'session'/'native-agent.json').is_file()]


def child_calls(root):
    """All admitted children consume the same coordinator ceiling, once each."""
    from .planning_tools import committed as planning_calls
    from .conversation_review import committed as review_calls
    from .work_units import unit_calls
    from .web_research import committed as research_calls
    from .execution_resources import calls_used
    from .native_agent import read_json
    observed = sum(calls_used(read_json(regular(p),1024*1024),p.parent/'session')
                   for p in regular(Path(root)/'delegations').glob('*/receipt.json'))
    counts = (planning_calls(root), review_calls(root), unit_calls(root), research_calls(root), observed)
    return sum(counts) if all(value is not None for value in counts) else None


def summary(root):
    rows = records(root)
    return {'requests': len(rows), 'completed': sum(r['status'] == 'COMPLETED' for r in rows),
            'children': [str(p) for p in children(root)],
            'records': [{k: r.get(k) for k in ('id', 'status', 'profile', 'experimental', 'reason', 'report')}
                        for r in rows], 'qualification': 'UNQUALIFIED', 'authority': 'NONE'}


def register(root, row):
    directory = policy_path().parent/'sessions'
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    save_json(regular(directory/(key+'.json')), {'session': str(root.resolve()),
        'variant_key': row['variant_key'], 'experimental': row['experimental'], 'profile': row['profile']})


def report(limit=30):
    """Recent measured journeys; acceptance remains a separate observed outcome."""
    from .native_agent import read_json
    from .execution_usage_sources import execution_usage
    directory = policy_path().parent/'sessions'
    rows = []
    for path in sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        row = read_json(regular(path))
        root = regular(Path(row['session']))
        try:
            row['exploration'] = summary(root)
            measured = execution_usage(sessions=[root])
            row['measured'] = {k: measured[k] for k in ('generation_intents', 'known_cost_by_currency', 'unpriced_or_partial_calls')}
            outcome = read_json(root/'outcome.json', 4*1024*1024) if (root/'outcome.json').exists() else {}
            if not outcome and (root/'attempt.json').exists():
                attempt = read_json(root/'attempt.json', 4*1024*1024)
                outcome = attempt.get('attempts', [{}])[-1].get('outcome', {})
            row['parent_status'] = outcome.get('status', 'UNKNOWN')
            artifact = read_json(root/'artifact.json') if (root/'artifact.json').exists() else {}
            row['parent_verdict'] = artifact.get('payload', {}).get('verdict', 'UNKNOWN') if outcome.get('status') == 'PROPOSED' else 'UNKNOWN'
            row['qualification'] = 'UNQUALIFIED'
        except (ValueError, OSError) as exc:
            row['unavailable'] = str(exc)[:300]
        rows.append(row)
    return {'sessions': rows, 'preference_changed': False,
            'measurement': 'Whole parent session plus explorers; verdict is observed, not independent qualification of the explorer.'}


def digest(days=7):
    """Exploration at a glance for the last `days`: per day, explorer, effort and breadth, the investigations,
    how many completed, and the explorers' calls and tokens (what a subscription's quota counts); then the waste
    and sufficiency measurements. Reads the receipts and logs already kept; nothing new is recorded."""
    from datetime import datetime, timedelta, timezone
    from .native_agent import read_json
    from .native_installation import storage_root
    from .work_forms import digest as form_digest
    since = datetime.now(timezone.utc) - timedelta(days=days)
    groups, evidence, citations = {}, {}, {}
    for pointer in sorted((policy_path().parent/'sessions').glob('*.json')):
        try:
            root = regular(Path(read_json(regular(pointer))['session']))
            receipts = sorted((root/'delegations').glob('*/receipt.json'))
        except (ValueError, OSError, KeyError):
            continue
        for receipt in receipts:
            try:
                stamp = datetime.fromtimestamp(receipt.stat().st_mtime, timezone.utc)
                if stamp < since:
                    continue
                row = read_json(regular(receipt), 1024*1024)
                usage = receipt.parent/'session'/'usage.json'
                tokens = read_json(regular(usage), 1024*1024)['tokens'] if usage.is_file() else {}
            except (ValueError, OSError, KeyError):
                continue
            key = (stamp.date().isoformat(), row.get('profile', '?'), row.get('effort') or '-', row.get('breadth') or '-')
            group = groups.setdefault(key, dict.fromkeys(('investigations', 'completed', 'calls', 'input_tokens',
                                                          'output_tokens'), 0))
            group['investigations'] += 1
            group['completed'] += row['status'] == 'COMPLETED'
            from .execution_resources import calls_used
            group['calls'] += calls_used(row, receipt.parent/'session')
            for kind in ('input_tokens', 'output_tokens'):
                group[kind] += (tokens.get(kind) or {}).get('known_sum') or 0
            report = row.get('report') or {}  # the location_status experiment's statuses and conclusions
            for mark in [l.get('status') for l in report.get('locations', [])] + [report.get('conclusion')]:
                if mark:
                    evidence[mark] = evidence.get(mark, 0) + 1
    logs = {}
    for name in ('explore-overlap', 'explore-judge', 'explore-tier', 'explore-decider', 'work-form'):
        try:
            path = storage_root()/'serve'/(name+'.jsonl')
            lines = path.read_text(encoding='utf-8').splitlines() if path.is_file() else []
        except (ValueError, OSError):
            lines = []
        logs[name] = [r for r in (json.loads(line) for line in lines if line.strip())
                      if datetime.fromisoformat(r['ts']) >= since]
    batches = [r for r in logs['explore-overlap'] if r['kind'] == 'batch']
    rereads = [r for r in logs['explore-overlap'] if r['kind'] == 'parent_reread']
    verdicts = {}
    for row in logs['explore-judge']:
        for kind, count in (row.get('citations') or {}).items():
            citations[kind] = citations.get(kind, 0) + count
        for judge in ('local', 'jev'):
            if 'choice' in row.get(judge, {}):
                verdicts[row[judge]['choice']] = verdicts.get(row[judge]['choice'], 0) + 1
                break
    tiers = {}  # the explorer the judges would have chosen, against how the investigation went
    for row in logs['explore-tier']:
        answer = next((row[j] for j in ('local', 'jev') if 'choice' in row.get(j, {})), None)
        if answer:
            tier = tiers.setdefault(answer['choice'], {'investigations': 0, 'completed': 0})
            tier['investigations'] += 1
            tier['completed'] += row.get('status') == 'COMPLETED'
    return {'days': days, 'rows': [dict(zip(('day', 'explorer', 'effort', 'breadth'), key), **value)
                                   for key, value in sorted(groups.items())],
            'waste': {'calls': len(batches), 'dispatched': sum(r.get('dispatched', 0) for r in batches),
                      'reused': sum(r.get('reused', 0) for r in batches),
                      'duplicates': sum(r.get('duplicates', 0) for r in batches),
                      'files_read_twice_in_a_call': sum(r.get('shared_files', 0) for r in batches),
                      'split_parts': sum(r.get('split', 0) for r in batches),
                      'unread_pieces': sum(r.get('unread_pieces', 0) for r in batches),
                      'reads_outside_part': sum(r.get('outside_reads', 0) for r in batches),
                      'session_rereads_cited': sum(bool(r.get('cited')) for r in rereads),
                      'session_rereads_other': sum(not r.get('cited') for r in rereads)},
            'sufficiency': verdicts, 'tiers': tiers, 'citations': citations, 'evidence': evidence, 'decider': decisions(logs['explore-decider']),
            'experiments': experiments(), 'experiment_classes': class_values(),
            'work_forms': form_digest(logs['work-form'])}


JEV_USD_PER_MILLION = 0.042  # input tokens; output is free (TypeSafe's announcement, 2026-09-16)


def decisions(rows):
    """The decider experiment at a glance: decisions per mode, candidates left unjudged, evidence flags, how often
    two arms chose the same candidates (Jaccard of their top sets) and the time spent deciding."""
    if not rows:
        return {}
    pairs = {}
    for row in rows:
        for pair, value in row.get('agreement', {}).items():
            pairs.setdefault(pair, []).append(value)
    modes = {}
    for row in rows:
        modes[row['mode']] = modes.get(row['mode'], 0) + 1
    return {'decisions': len(rows), 'modes': modes, 'applied': sum(r['applied'] for r in rows),
            'candidates': sum(len(r['candidates']) for r in rows),
            'unjudged': sum(max(0, len(r['candidates'])-r['cap']) for r in rows if r['applied']),
            'judged': sum(r.get('judged', 0) for r in rows), 'flagged': sum(len(r.get('flagged', {})) for r in rows),
            'agreement': {pair: round(sum(v)/len(v), 3) for pair, v in sorted(pairs.items())},
            'ms': round(sum(r.get('ms', 0) for r in rows)/len(rows)),
            'jev_tokens': sum(r.get('jev_tokens', 0) for r in rows)}


def digest_text(value):
    rows = value['rows']
    lines = [f"Exploration, last {value['days']} days"]
    if not rows:
        lines.append('No investigations.')
    for row in rows:
        lines.append(f"{row['day']} {row['explorer']} effort {row['effort']} {row['breadth']}: "
                     f"{row['investigations']} investigations, {row['completed']} completed, {row['calls']} calls, "
                     f"{row['input_tokens']:,} input and {row['output_tokens']:,} output tokens")
    total = {k: sum(r[k] for r in rows) for k in ('investigations', 'calls', 'input_tokens', 'output_tokens')}
    lines.append(f"Total: {total['investigations']} investigations, {total['calls']} calls, "
                 f"{total['input_tokens']:,} input and {total['output_tokens']:,} output tokens")
    waste = value['waste']
    lines.append(f"Waste: {waste['reused']} reused and {waste['duplicates']} repeated questions not run again; "
                 f"{waste['files_read_twice_in_a_call']} files read by two explorers of one call; "
                 f"{waste['unread_pieces']} split pieces left unread, {waste['reads_outside_part']} reads outside a part; "
                 f"session re-reads: {waste['session_rereads_cited']} of cited lines, {waste['session_rereads_other']} other")
    if value.get('citations'):
        lines.append('Citations checked by Jev: ' + ', '.join(f'{k} {n}' for k, n in sorted(value['citations'].items())))
    if value['sufficiency']:
        lines.append('Judges on the reports (shadow): ' + ', '.join(f'{k} {n}' for k, n in sorted(value['sufficiency'].items())))
    if value['tiers']:
        lines.append('Explorer the judges would choose (shadow): ' + ', '.join(
            f"{k} {t['investigations']} ({t['completed']} completed)" for k, t in sorted(value['tiers'].items())))
    decided = value.get('decider')
    if decided:
        lines.append(f"Candidate decider: {decided['decisions']} decisions (" + ', '.join(
            f'{k} {n}' for k, n in sorted(decided['modes'].items())) + f"), {decided['applied']} cut, "
            f"{decided['unjudged']} of {decided['candidates']} candidates left unjudged, {decided['judged']} judged, "
            f"{decided['flagged']} flagged, {decided['ms']} ms each, {decided['jev_tokens']:,} Jev tokens "
            f"(US$ {decided['jev_tokens']*JEV_USD_PER_MILLION/1e6:.4f})" + ('; agreement ' + ', '.join(
                f'{k} {v}' for k, v in decided['agreement'].items()) if decided['agreement'] else ''))
    if value.get('evidence'):
        lines.append('Location status and conclusions: ' + ', '.join(f'{k} {n}' for k, n in sorted(value['evidence'].items())))
    on = {k: v for k, v in value.get('experiments', {}).items() if v not in (False, 'off')}
    lines.append('Experiments: ' + (', '.join(f'{k}={v}' if isinstance(v, str) else k for k, v in on.items()) or 'all off'))
    forms = value.get('work_forms')
    if forms:
        lines.append(f"Session turns (shadow of the ways of working): {forms['turns']}; by class " + ', '.join(
            f'{k} {n}' for k, n in forms['classes'].items()) + '; by form ' + ', '.join(
            f'{k} {n}' for k, n in forms['forms'].items()) + f"; {forms['with_verifier']} with a verifier, "
            f"{forms['verify_runs']} verifier runs, {forms['verify_failed']} failed, "
            f"{forms['turns_ending_on_two_failures']} turns ending on two or more failures in a row" + (
            '; Jev would choose ' + ', '.join(f'{k} {n}' for k, n in forms['jev'].items() if n) +
            f" (the session's form in {forms['jev_agreed']})" if any(forms['jev'].values()) else '') + (
            '; units ' + ', '.join(f'{k} {n}' for k, n in forms['units'].items()) if forms.get('units') else ''))
    for name, values in value.get('experiment_classes', {}).items():
        lines.append(f'Experiments for {name} sessions: ' + ', '.join(
            f"{k}={'on' if v is True else 'off' if v is False else v}" for k, v in values.items()))
    return '\n'.join(lines)


def main(argv=None):
    import argparse
    import json
    from .native_agent import read_json
    parser = argparse.ArgumentParser(description='Configure assisted exploration; inspect without model calls.')
    parser.add_argument('--configure', type=Path, help='Save a complete exploration settings JSON file')
    parser.add_argument('--session', type=Path, help='Inspect a parent session and its exploration receipts')
    parser.add_argument('--report', action='store_true', help='Read recent exploration journeys, usage and parent verdicts')
    parser.add_argument('--digest', type=int, nargs='?', const=7, metavar='DAYS',
                        help='Summarize exploration per day: calls, tokens, completion, waste (default 7 days)')
    parser.add_argument('--experiment', action='append', default=[], metavar='[CLASS.]NAME=VALUE',
                        help='Turn an exploration experiment on or off, e.g. decider=shadow or adaptive_k=on; '
                             'local.jev_first=on for local sessions only (classes: local, quota, usd)')
    parser.add_argument('--experiments', action='store_true', help='Show the exploration experiments')
    args = parser.parse_args(argv)
    try:
        if args.experiment:
            changes = {}
            for item in args.experiment:
                name, _, raw = item.partition('=')
                route_class, _, name = name.rpartition('.')
                if name not in EXPERIMENTS:
                    raise ValueError('Unknown experiment: ' + name + '. Known: ' + ', '.join(EXPERIMENTS))
                changes.setdefault(route_class or None, {})[name] = \
                    raw if name == 'decider' else raw.lower() in ('on', 'true', '1', 'yes')
            for route_class, values in changes.items():
                saved = save_experiments(route_class, **values)
            print(json.dumps({**saved, 'classes': class_values()}, indent=2))
            return 0
        if args.experiments:
            print(json.dumps({**experiments(), 'classes': class_values()}, indent=2))
            return 0
        if args.digest is not None:
            print(digest_text(digest(args.digest)))
            return 0
        result = configure(read_json(args.configure)) if args.configure else summary(args.session) if args.session else report() if args.report else policy()
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    raise SystemExit(main())
