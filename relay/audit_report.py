"""Read-only report on the cheap judges (hosted Jev, the resident local judge).

Reads `<storage>/serve/audit-judge.jsonl` (the `materiality` and `fix` rows written by triage and
judge_fix) and `<storage>/serve/jev-shadow.jsonl` (written by jev_shadow) and prints how many
findings were judged and demoted, how often the two judges agree, the proportion verdicts and how
many fix-review items were judged new, how often each judge's tier pick matches final_tier, and
the mean call time per judge. Stdlib only; the logs are only read.
"""
import argparse
import json
import sys
from pathlib import Path

JUDGES = ('jev', 'local')
# The tier index of each tier name, the order tare routes in: local first, then cheap, then
# intermediate (turn_engine's TIER_OPTIONS order).
TIER_INDEX = {'local': 0, 'cheap': 1, 'intermediate': 2}


def _rows(path):
    """The JSONL rows of a log; a missing file, a blank line or a corrupt row yields nothing."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _answered(verdict):
    return isinstance(verdict, dict) and isinstance(verdict.get('choice'), str)


def _deciding(verdicts):
    """The answer that decides: the resident local judge when it answered, else hosted Jev
    (turn_engine.deciding)."""
    return next((verdicts[k] for k in ('local', 'jev') if _answered(verdicts.get(k))), None)


def _ms(buckets, verdict):
    value = verdict.get('ms') if isinstance(verdict, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        buckets['total'] += value
        buckets['count'] += 1


def summarize(audit_path, shadow_path):
    """Aggregate the two judge logs into plain counts the report renders."""
    summary = {
        'materiality': {'rows': 0, 'findings': 0, 'demoted': 0, 'both': 0, 'agree': 0},
        'fix': {'rows': 0, 'items': 0, 'new': 0, 'proportion': {j: {} for j in JUDGES}},
        'shadow': {'rows': 0, 'answered': {j: 0 for j in JUDGES}, 'match': {j: 0 for j in JUDGES}},
        'ms': {j: {'total': 0.0, 'count': 0} for j in JUDGES},
    }
    for row in _rows(audit_path):
        if row.get('kind') == 'materiality':
            m = summary['materiality']
            m['rows'] += 1
            for item in row.get('items') or []:
                if _answered(item.get('jev')) or _answered(item.get('local')):
                    m['findings'] += 1  # a finding only counts as judged when some judge answered
                if item.get('demoted'):
                    m['demoted'] += 1
                if _answered(item.get('jev')) and _answered(item.get('local')):
                    m['both'] += 1
                    if item['jev']['choice'] == item['local']['choice']:
                        m['agree'] += 1
                for judge in JUDGES:
                    _ms(summary['ms'][judge], item.get(judge))
        elif row.get('kind') == 'fix':
            f = summary['fix']
            f['rows'] += 1
            for item in row.get('novelty') or []:
                f['items'] += 1
                if _deciding(item) is not None and _deciding(item)['choice'] == 'new':
                    f['new'] += 1
                for judge in JUDGES:
                    _ms(summary['ms'][judge], item.get(judge))
            proportion = row.get('proportion')
            proportion = proportion if isinstance(proportion, dict) else {}
            for judge in JUDGES:
                verdict = proportion.get(judge)
                if _answered(verdict):
                    f['proportion'][judge][verdict['choice']] = \
                        f['proportion'][judge].get(verdict['choice'], 0) + 1
                _ms(summary['ms'][judge], verdict)
    for row in _rows(shadow_path):
        s = summary['shadow']
        s['rows'] += 1
        final_tier = row.get('final_tier')
        for judge in JUDGES:
            verdict = row.get(judge)
            _ms(summary['ms'][judge], verdict)
            if not _answered(verdict) or not isinstance(final_tier, int) or isinstance(final_tier, bool):
                continue
            s['answered'][judge] += 1
            if TIER_INDEX.get(verdict['choice']) == final_tier:
                s['match'][judge] += 1
    return summary


def _frac(num, denom):
    if not denom:
        return f'{num} of 0'
    return f'{num} of {denom} ({num / denom:.0%})'


def render(storage, s):
    """The report text for a summary."""
    lines = [f'storage: {storage}', '']
    m = s['materiality']
    lines.append(f"materiality: {m['rows']} rows, {m['findings']} findings judged, {m['demoted']} demoted")
    lines.append(f"  jev + local agree when both answered: {_frac(m['agree'], m['both'])}")
    f = s['fix']
    lines.append(f"fix: {f['rows']} rows, {f['items']} review items")
    for judge in JUDGES:
        counts = f['proportion'][judge]
        lines.append(f'  {judge} proportion verdicts: ' +
                     (', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'none answered'))
    lines.append(f'  review items judged new: {_frac(f["new"], f["items"])}')
    sh = s['shadow']
    lines.append(f"jev-shadow: {sh['rows']} rows")
    for judge in JUDGES:
        lines.append(f"  {judge} tier pick matches final_tier: {_frac(sh['match'][judge], sh['answered'][judge])}")
    lines.append('mean ms per judge:')
    for judge in JUDGES:
        ms = s['ms'][judge]
        if ms['count']:
            lines.append(f"  {judge}: {ms['total'] / ms['count']:.1f} ms (n={ms['count']})")
        else:
            lines.append(f'  {judge}: no timing')
    return '\n'.join(lines)


def main(argv=None):
    """`python -m relay.audit_report [--storage DIR]`."""
    parser = argparse.ArgumentParser(prog='relay.audit_report',
                                     description='Read-only report on the cheap judges\' shadow logs.')
    parser.add_argument('--storage', type=Path, default=None,
                        help='the storage root (default: relay.native_installation.storage_root())')
    args = parser.parse_args(argv)
    if args.storage is not None:
        storage = args.storage
    else:
        from .native_installation import storage_root
        try:
            storage = storage_root()
        except ValueError as exc:
            print(f'audit_report: {exc}', file=sys.stderr)
            return 2
    serve = storage / 'serve'
    print(render(storage, summarize(serve / 'audit-judge.jsonl', serve / 'jev-shadow.jsonl')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
