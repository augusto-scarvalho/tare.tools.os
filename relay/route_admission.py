"""Deterministic evidence admission for an explicitly enabled joint route policy."""
import math


def eligible_winners(candidates, *, cohort, acceptance_floor):
    """A model cannot establish missing cost or cohort comparability.

    Return the cheapest, then fastest adequate pairs, allowing exact ties. This
    is a historical cohort comparison, not a prediction for the current task.
    """
    if not isinstance(cohort, str) or not cohort.strip():
        raise ValueError('TASK_ROUTING_COHORT_MISSING')
    if (type(acceptance_floor) not in (int, float) or not math.isfinite(acceptance_floor)
            or not 0 < acceptance_floor <= 1):
        raise ValueError('ACCEPTANCE_FLOOR_INVALID')
    if len(candidates) < 2:
        raise ValueError('COMPARISON_REQUIRES_TWO_PAIRS')
    ranked = []
    for candidate in candidates:
        value = candidate['delivery']
        if value.get('status') != 'SUPPLIED_COHORT':
            raise ValueError('DELIVERY_EVIDENCE_MISSING')
        if value.get('cohort') != cohort:
            raise ValueError('DELIVERY_COHORT_MISMATCH')
        if type(value.get('accepted')) is int and value['accepted'] == 0 and type(value.get('attempted')) is int and value['attempted'] > 0:
            continue  # Failed deliveries remain in costs; a zero-success pair cannot win.
        for key in ('usd_per_accepted_delivery', 'seconds_per_accepted_delivery'):
            n = value.get(key)
            if type(n) not in (int, float) or not math.isfinite(n) or n < 0:
                raise ValueError('DELIVERY_EVIDENCE_INCOMPLETE')
        accepted, attempted = value.get('accepted'), value.get('attempted')
        if (type(accepted) is not int or type(attempted) is not int
                or not 0 < accepted <= attempted):
            raise ValueError('DELIVERY_EVIDENCE_INCOMPLETE')
        if accepted / attempted >= acceptance_floor:
            ranked.append(((value['usd_per_accepted_delivery'], value['seconds_per_accepted_delivery']), candidate['key']))
    if not ranked:
        raise ValueError('NO_PAIR_MEETS_QUALITY_FLOOR')
    best = min(score for score, _ in ranked)
    return [key for score, key in ranked if score == best]
