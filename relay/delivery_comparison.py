"""Compare equivalent delivery trials from existing receipts; no inference."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

from compute_plane.choice_assessment import digest
from compute_plane.native_journal import custody_lock, regular, save_json

from .native_agent import read_json

SCHEMA = 'tare.tools/delivery-study/1'


def strategy_profile(value):
    """Selection observations vary per dispatch; the selected execution profile does not."""
    return {k: v for k, v in value.items() if k != 'native_selection'}


def money(value):
    try:
        amount = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        raise ValueError('Costs must be nonnegative decimal strings; omit unknown costs.')
    return amount


def profiles(candidate):
    from .native_catalog import select
    from .adaptive_routes import proposed_route
    return {role: proposed_route(candidate[role], [select(candidate[role]['deployment'])])
            for role in ('implementer', 'auditor')}


def compare(path):
    """Case equivalence and final verdict are explicit operator inputs.

    Charges, invocations, failed attempts, profile identities and named checks
    come from native receipts. PROPOSED never becomes an acceptance verdict.
    Trials must be dictionaries in a list with nonempty string candidate/case
    IDs; references and completeness are validated before profile resolution.
    """
    from .execution_usage import aggregate_sessions
    from .execution_usage_sources import session_entry
    from .native_continuity import chain
    from .native_review import review
    path = regular(Path(path).resolve())
    study = read_json(path, 256*1024)
    from . import delivery_costs
    cost_snapshot = delivery_costs.load(study.get('costs'))
    if study.get('schema') != SCHEMA or not isinstance(study.get('cohort'), str) or not study['cohort'].strip():
        raise ValueError('A named delivery cohort and delivery-study/1 schema are required.')
    cases, candidates, trials = study['cases'], study['candidates'], study.get('trials')
    if (not isinstance(cases, dict) or not 2 <= len(cases) <= 100 or
            not isinstance(candidates, list) or not 2 <= len(candidates) <= 3):
        raise ValueError('Compare two or three candidates on the same two to one hundred cases.')
    if any(not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'].strip()
           for c in candidates):
        raise ValueError('Each candidate must be a dictionary with a nonempty string ID.')
    identities = [c['id'] for c in candidates]
    if len(set(identities)) != len(identities) or study['baseline'] not in identities:
        raise ValueError('Candidate IDs must be unique and include the baseline.')
    for case in cases.values():
        if (not isinstance(case, dict)
                or not all(isinstance(case.get(k), str) and case[k].strip() for k in ('instruction', 'baseline'))
                or not isinstance(case.get('checks'), dict) or not case['checks']
                or any(not isinstance(name, str) or not name or not isinstance(argv, list) or not argv
                       or any(not isinstance(arg, str) for arg in argv) for name, argv in case['checks'].items())):
            raise ValueError('Each case needs its instruction, baseline reference and named checks.')
    if (not isinstance(trials, list) or
            any(not isinstance(trial, dict) or
                not all(isinstance(trial.get(key), str) and trial[key].strip()
                        for key in ('candidate', 'case')) for trial in trials)):
        raise ValueError('Trials must be a list of dictionaries with nonempty string candidate and case IDs.')
    expected = {(c, key) for c in identities for key in cases}
    actual = [(t['candidate'], t['case']) for t in trials]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('Every candidate must have exactly one trial per case; keep failed and unfinished trials.')
    floor = study.get('acceptance_floor', 1.0)
    if type(floor) not in (int, float) or not 0 < floor <= 1:
        raise ValueError('Acceptance floor must be greater than zero and at most one.')
    results, seen_sessions, evidence = [], {}, []
    if cost_snapshot is not None:
        evidence.append({'cost_snapshot_sha256': digest(cost_snapshot)})
    for candidate in candidates:
        configured = profiles(candidate)
        hashes = {role: digest(strategy_profile(value)) for role, value in configured.items()}
        rows, total, seconds, accepted, issues = [], Decimal(0), 0.0, 0, []
        for trial in (t for t in trials if t['candidate'] == candidate['id']):
            case = cases[trial['case']]
            if 'contract_sha256' in trial and trial['contract_sha256'] != digest(case):
                raise ValueError('Trial does not match the case contract: '+trial['case'])
            stages = trial['sessions']
            if not stages or not any(s['stage'] == 'implement' for s in stages):
                raise ValueError('Each trial must retain its implementation sessions.')
            entries, starts, ends, signatures, first, last_audit, audit_state = [], [], [], [], None, None, None
            timing_complete = True
            local_seen = set()
            for source in stages:
                stage = source['stage']
                if stage not in {'plan', 'implement', 'rework', 'audit'}:
                    raise ValueError('Unknown delivery stage: '+stage)
                session = Path(source['path'])
                if not session.is_absolute():
                    raise ValueError('Session references must be absolute paths.')
                if not (session/'native-agent.json').exists() and not (session/'challenger-input.json').exists():
                    issues.append('INCOMPLETE_EXECUTION:'+trial['case'])
                    timing_complete = False
                    signatures.append({'missing_session': str(session)})
                    continue
                for session in chain(session) if (session/'native-agent.json').exists() else [session]:
                    session = regular(session)
                    if str(session) in local_seen:
                        continue
                    local_seen.add(str(session))
                    with custody_lock(session):
                        from .challenger import source_state
                        state = source_state(session)
                        entry = session_entry(session, stage, locked=True)
                        entry = delivery_costs.apply(entry, state['deployment'], cost_snapshot)
                        # Include journal bytes so new work invalidates an exported preference,
                        # even when an interrupted writer has not refreshed usage.json yet.
                        signatures.append({name: hashlib.sha256((session/name).read_bytes()).hexdigest()
                                           for name in ('native-agent.json', 'journal.jsonl', 'usage.json', 'outcome.json',
                                                        'host.json', 'attempt.json', 'challenger-input.json', 'observations.jsonl')
                                           if (session/name).exists()})
                    owner = (candidate['id'], trial['case'])
                    if seen_sessions.setdefault(state['session_id'], owner) != owner:
                        raise ValueError('A session cannot supply different comparison trials.')
                    role = 'auditor' if stage == 'audit' else 'implementer'
                    if stage != 'plan' and digest(strategy_profile(state['deployment'])) != hashes[role]:
                        issues.append('PROFILE_CHANGED_OR_MIXED:'+trial['case'])
                    if stage == 'implement' and first is None:
                        first = state
                    invocations = state.get('invocations', [])
                    if not invocations or any(type(i.get(k)) not in (int, float) or not math.isfinite(i[k])
                            for i in invocations for k in ('started_at', 'ended_at')):
                        issues.append('INCOMPLETE_EXECUTION:'+trial['case'])
                        timing_complete = False
                    elif any(i['ended_at'] < i['started_at'] for i in invocations):
                        # Wall clocks can move backwards. Keep costs and outcomes;
                        # do not manufacture a zero duration or abort the whole study.
                        issues.append('INVALID_EXECUTION_TIMESTAMPS:'+trial['case'])
                        timing_complete = False
                    else:
                        starts += [i['started_at'] for i in invocations]
                        ends += [i['ended_at'] for i in invocations]
                        if stage == 'audit' and (audit_state is None or
                                max(i['ended_at'] for i in invocations) > max(i['ended_at'] for i in audit_state['invocations'])):
                            last_audit, audit_state = session, state
                    entries.append(entry)
            if first is not None and first['instruction'] != case['instruction']:
                raise ValueError('Implementation instruction differs from the case contract.')
            usage = aggregate_sessions(entries)
            verdict = trial.get('verdict', 'unknown')
            if first is None:
                verdict = 'unknown'
                issues.append('IMPLEMENTATION_MISSING:'+trial['case'])
            if verdict not in {'accepted', 'rejected', 'unknown'}:
                raise ValueError('Verdict must be accepted, rejected or unknown.')
            if verdict != 'unknown' and not str(trial.get('verdict_source', '')).strip():
                raise ValueError('A verdict needs its external review reference.')
            if verdict == 'unknown':
                issues.append('VERDICT_UNKNOWN:'+trial['case'])
            if verdict == 'accepted':
                if last_audit is None:
                    issues.append('AUDIT_MISSING:'+trial['case'])
                else:
                    if any(audit_state['config'].get('verifiers', {}).get(name, {}).get('argv') != argv
                           for name, argv in case['checks'].items()):
                        issues.append('VERIFIER_CONTRACT_MISMATCH:'+trial['case'])
                    observed = review(last_audit, include_diffs=False)
                    latest = {c['name']: c for c in observed['checks']}
                    if (observed['unresolved_effects'] or any(name not in latest or
                            latest[name]['status'] != 'succeeded' or latest[name]['returncode'] != 0 or
                            latest[name]['freshness'] != 'OBSERVED_FILES_MATCH' for name in case['checks'])):
                        issues.append('ACCEPTANCE_CHECKS_UNCONFIRMED:'+trial['case'])
                accepted += 1
            amounts = usage['known_cost_by_currency']
            if (usage['unpriced_or_partial_calls'] or usage['unknown_session_ids'] or
                    set(amounts) - {'USD'} or 'USD' not in amounts):
                issues.append('COST_INCOMPLETE:'+trial['case'])
            from .provider_prices import REFERENCE, is_reference
            costs = [c.get('cost', {}) for entry in entries for c in (entry.get('usage') or {}).get('calls', [])]
            sources = sorted({cost.get('source', cost['tariff']['source'] if is_reference(cost.get('tariff'))
                                       else 'configured_tariff_or_unknown') for cost in costs})
            # A models.dev reference may cap spend, but never picks a winner: no route order changes on it.
            if any(str(s).startswith(REFERENCE) for s in sources):
                issues.append('COST_REFERENCE_PRICE:'+trial['case'])
            additional = trial.get('additional_cost_usd')
            if additional is None:
                issues.append('ADDITIONAL_COST_UNKNOWN:'+trial['case'])
            cost = money(amounts.get('USD', '0')) + (money(additional) if additional is not None else Decimal(0))
            elapsed = max(ends)-min(starts) if timing_complete and starts and ends else None
            total += cost
            if elapsed is not None:
                seconds += elapsed
            evidence.append({'trial': [candidate['id'], trial['case']], 'receipts': signatures})
            rows.append({'case': trial['case'], 'verdict': verdict,
                'verdict_source': trial.get('verdict_source'), 'known_cost_usd': str(cost),
                'seconds': elapsed, 'generation_intents': usage['generation_intents'],
                'cost_sources': sources,
                'retry_intents': usage['retry_intents'], 'summary_intents': usage['summary_intents'],
                'sessions': [{'stage': e['stage'], 'source': e['source'], 'profile': e['profile'],
                              'invocations': len(e['attempts'])} for e in entries]})
        results.append({'id': candidate['id'], 'pair_sha256': digest(hashes), 'profiles': hashes,
            'attempted': len(cases), 'accepted': accepted, 'rejected': sum(r['verdict']=='rejected' for r in rows),
            'known_cost_usd': str(total), 'total_seconds': seconds if all(r['seconds'] is not None for r in rows) else None,
            'usd_per_accepted_delivery': str(total/accepted) if accepted else None,
            'issues': sorted(set(issues)), 'trials': rows})
    baseline = next(r for r in results if r['id'] == study['baseline'])
    eligible = [r for r in results if not r['issues'] and r['accepted'] and
                r['accepted']/r['attempted'] >= floor and r['accepted'] >= baseline['accepted']]
    winner, reason = None, 'INCOMPLETE_EVIDENCE'
    if not any(r['issues'] for r in results):
        reason = 'NO_ADEQUATE_CANDIDATE'
        if eligible:
            best_cost = min(money(r['usd_per_accepted_delivery']) for r in eligible)
            best = [r for r in eligible if money(r['usd_per_accepted_delivery']) == best_cost]
            if len(best) == 1 and best[0]['id'] != baseline['id']:
                winner, reason = best[0]['id'], 'LOWEST_COST_ADEQUATE_DELIVERY'
            else:
                reason = 'BASELINE_BEST_OR_TIED'
    return {'schema': 'tare.tools/delivery-comparison/1', 'cohort': study['cohort'],
        'baseline': study['baseline'], 'winner': winner, 'reason': reason,
        'acceptance_floor': floor, 'candidates': results,
        'cost_basis': cost_snapshot['basis'] if cost_snapshot else 'recorded_charges_and_configured_tariffs',
        'study_sha256': digest(study), 'evidence_sha256': digest(evidence),
        'basis': 'Matched operator-declared case contracts and verdicts; costs and checks derived from receipts.',
        'cost_scope': 'All listed model sessions, continuations and corrections, plus declared additional costs. Unknown is not zero.',
        'generation_calls': 0}


def write_policy(study_path, output):
    report = compare(study_path)
    if report['winner'] is None:
        raise ValueError('Preference unchanged: '+report['reason'])
    study = read_json(Path(study_path), 256*1024)
    if digest(study) != report['study_sha256']:
        raise ValueError('Study changed during comparison; compare again before saving a preference.')
    strategies = []
    for candidate, measured in zip(study['candidates'], report['candidates']):
        strategies.append({k: candidate[k] for k in ('implementer', 'auditor')} | {'evidence': {
            'pair_sha256': measured['pair_sha256'], 'scope': 'implementation+audit+rework', 'currency': 'USD',
            'source': str(Path(study_path).resolve()), 'observed_at': datetime.now(timezone.utc).isoformat(),
            'cohort': study['cohort'], 'accepted': measured['accepted'], 'attempted': measured['attempted'],
            'cost_basis': report['cost_basis'],
            'total_cost_usd': float(measured['known_cost_usd']), 'total_seconds': measured['total_seconds']}})
    policy = {'schema': 'tare.tools/adaptive-route-policy/3', 'mode': 'guarded',
        'selection': 'measured_delivery', 'acceptance_floor': report['acceptance_floor'], 'strategies': strategies,
        'delivery_study': {'path': str(Path(study_path).resolve()), 'study_sha256': report['study_sha256'],
            'evidence_sha256': report['evidence_sha256'], 'winner': report['winner']}}
    output = regular(Path(output).resolve()); output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = output.with_suffix('.study.json')
    snapshot_source = str(snapshot.resolve())
    for strategy in strategies:
        strategy['evidence']['source'] = snapshot_source
    policy['delivery_study']['path'] = snapshot_source
    with custody_lock(output.parent):
        if output.exists():
            raise ValueError('Choose a new policy file; the existing preference was preserved.')
        if snapshot.exists():
            raise ValueError('Choose a new policy file; the existing study snapshot was preserved.')
        save_json(snapshot, study)
        save_json(output, policy)
    return {**report, 'policy': str(output), 'study_snapshot': snapshot_source, 'activation': 'Set TARE_ADAPTIVE_ROUTE_POLICY to this file; only matching routing_cohort tasks can use it.'}


def validate_evidence(policy):
    if policy.get('selection') != 'measured_delivery':
        return
    binding = policy['delivery_study']
    report = compare(binding['path'])
    if any(report[key] != binding[key] for key in ('study_sha256', 'evidence_sha256', 'winner')) or not report['winner']:
        raise ValueError('DELIVERY_COMPARISON_CHANGED')
    if policy['acceptance_floor'] != report['acceptance_floor']:
        raise ValueError('DELIVERY_COMPARISON_CHANGED')
    study = read_json(Path(binding['path']), 256*1024)
    if len(policy['strategies']) != len(study['candidates']):
        raise ValueError('DELIVERY_COMPARISON_CHANGED')
    for strategy, candidate, row in zip(policy['strategies'], study['candidates'], report['candidates']):
        if any(strategy[role] != candidate[role] for role in ('implementer', 'auditor')):
            raise ValueError('DELIVERY_COMPARISON_CHANGED')
        evidence = strategy['evidence']
        if evidence.get('cost_basis', 'recorded_charges_and_configured_tariffs') != report['cost_basis']:
            raise ValueError('DELIVERY_COMPARISON_CHANGED')
        if any(evidence[k] != v for k, v in {'cohort': report['cohort'], 'pair_sha256': row['pair_sha256'],
                'accepted': row['accepted'], 'attempted': row['attempted'],
                'total_cost_usd': float(row['known_cost_usd']), 'total_seconds': row['total_seconds']}.items()):
            raise ValueError('DELIVERY_COMPARISON_CHANGED')
