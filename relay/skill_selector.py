"""Deterministic lexical skill selection with bounded shortlist, hard eligibility filtering,
and explicit 5-state abstention (SKL-06 Corte A / PA-04).

Zero model calls and zero token spend: filters strictly by role policy, per-skill allowed_roles,
and verified backing snapshot integrity before scoring lightweight metadata (explicit triggers,
skill_id, skill_name, description, and top-level headings). Full SKILL.md bodies are never loaded
into the selector context; ineligible skills never become eligible through lexical scores.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from . import managed_skills as _managed
from . import skill_bundles as _bundles

SCHEMA = 'tare.tools/skill-selection/1'
METHOD = 'deterministic_lexical_v1'
STATES = ('selected', 'no_skill', 'ambiguous', 'unavailable', 'error')

MAX_SHORTLIST = 5
MAX_AUTO_SELECTED = 3
SHORTLIST_THRESHOLD = 2.0
SELECT_THRESHOLD = 4.0
AMBIGUITY_MARGIN = 1.5
AMBIGUITY_TERM_OVERLAP = 0.7

# Bilingual (EN + PT-BR) filler words ignored during lexical matching so ordinary phrasing abstains cleanly.
_STOPWORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can', 'do', 'for', 'from', 'has', 'have',
    'help', 'how', 'in', 'into', 'is', 'it', 'me', 'my', 'need', 'no', 'not', 'of', 'on', 'one',
    'or', 'our', 'please', 'run', 'see', 'should', 'some', 'that', 'the', 'their', 'them', 'then',
    'there', 'these', 'this', 'to', 'use', 'using', 'want', 'we', 'what', 'when', 'where', 'which',
    'with', 'without', 'work', 'you', 'your',
    'ao', 'aos', 'aqui', 'as', 'ate', 'com', 'como', 'criar', 'da', 'das', 'de', 'dele', 'dela',
    'depois', 'do', 'dos', 'ela', 'ele', 'em', 'entre', 'era', 'essa', 'esse', 'esta', 'este',
    'eu', 'fazer', 'faz', 'foi', 'isso', 'isto', 'ja', 'mais', 'mas', 'me', 'meu', 'minha', 'na',
    'nao', 'nas', 'nem', 'no', 'nos', 'nossa', 'nosso', 'num', 'numa', 'os', 'ou', 'para', 'pela',
    'pelo', 'pode', 'por', 'pra', 'preciso', 'qual', 'quando', 'que', 'quem', 'quero', 'se', 'sem',
    'ser', 'seu', 'sua', 'tambem', 'tem', 'ter', 'toda', 'todo', 'tudo', 'um', 'uma', 'usar', 'vai',
    'ver', 'voce',
})

_TOKEN_RE = re.compile(r'[a-z0-9]{2,}')
_HEADING_RE = re.compile(r'^#{1,3}\s+(.+)$', re.MULTILINE)


def _fold(text: str) -> str:
    """Lowercase ASCII-folded normalization so Portuguese diacritics match cleanly."""
    norm = unicodedata.normalize('NFKD', text or '')
    return norm.encode('ascii', 'ignore').decode('ascii').lower()


def _tokenize(text: str) -> list[str]:
    folded = _fold(text)
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(folded):
        if raw in _STOPWORDS:
            continue
        if raw not in seen:
            seen.add(raw)
            tokens.append(raw)
    return tokens


def _extract_headings(text: str, limit: int = 12) -> list[str]:
    headings: list[str] = []
    for match in _HEADING_RE.finditer(text or ''):
        h = match.group(1).strip()
        if h:
            headings.append(h[:120])
        if len(headings) >= limit:
            break
    return headings


def _build_lightweight_record(view: dict) -> dict:
    """Extract only lightweight metadata for lexical scoring (never passes full SKILL.md body downstream)."""
    sid = view['skill_id']
    slug_tokens = set(_tokenize(sid.replace('-', ' ').replace('_', ' ')))
    name_tokens = set(_tokenize(view.get('skill_name', '')))
    desc_tokens = set(_tokenize(view.get('description', '')))
    headings = _extract_headings(view.get('effective_text', ''))
    heading_tokens = set(_tokenize(' '.join(headings)))
    triggers = [_fold(t).strip() for t in (view.get('triggers') or ()) if isinstance(t, str) and t.strip()]
    trigger_tokens = set()
    for t in triggers:
        trigger_tokens.update(_tokenize(t))
    return {
        'skill_id': sid,
        'skill_name': view.get('skill_name', sid),
        'description': view.get('description', ''),
        'effective_sha256': view['effective_sha256'],
        'effective_roles': list(view['effective_roles']),
        'allowed_roles': list(view['allowed_roles']),
        'disabled_by_policy_roles': list(view.get('disabled_by_policy_roles') or ()),
        'bundle_id': view['origin']['bundle_id'],
        'snapshot_id': view['origin']['snapshot_id'],
        'triggers': triggers,
        'trigger_tokens': trigger_tokens,
        'slug_folded': _fold(sid),
        'slug_tokens': slug_tokens,
        'name_tokens': name_tokens,
        'desc_tokens': desc_tokens,
        'heading_tokens': heading_tokens,
    }


def _score_record(
    rec: dict,
    *,
    query_folded: str,
    query_tokens: list[str],
    idf_map: dict[str, float],
) -> tuple[float, list[str], list[str]]:
    """Deterministic lexical score across triggers, slug, name, description, and headings."""
    score = 0.0
    matched_terms: list[str] = []
    matched_fields: list[str] = []
    seen_terms: set[str] = set()
    seen_fields: set[str] = set()

    def add_match(term: str, field: str):
        if term not in seen_terms:
            seen_terms.add(term)
            matched_terms.append(term)
        if field not in seen_fields:
            seen_fields.add(field)
            matched_fields.append(field)

    # 1. Explicit triggers (phrase match or exact token match).
    for trig in rec['triggers']:
        if len(trig) >= 2 and re.search(rf'(?<![a-z0-9]){re.escape(trig)}(?![a-z0-9])', query_folded):
            score += 5.0
            add_match(trig, 'trigger')

    # 2. Exact full skill_id slug mention in prompt (e.g. "use pxl-pixel-art").
    slug = rec['slug_folded']
    if len(slug) >= 3 and re.search(rf'(?<![a-z0-9]){re.escape(slug)}(?![a-z0-9])', query_folded):
        score += 6.0
        add_match(slug, 'skill_id')

    # 3. Token-level weighted lexical matching with gentle IDF multiplier.
    for tok in query_tokens:
        idf = idf_map.get(tok, 1.0)
        if tok in rec['trigger_tokens'] and tok not in seen_terms:
            score += 3.5 * idf
            add_match(tok, 'trigger')
        elif tok in rec['slug_tokens']:
            score += 4.0 * idf
            add_match(tok, 'skill_id')
        elif tok in rec['name_tokens']:
            score += 3.0 * idf
            add_match(tok, 'skill_name')
        elif tok in rec['desc_tokens']:
            score += 1.5 * idf
            add_match(tok, 'description')
        elif tok in rec['heading_tokens']:
            score += 1.0 * idf
            add_match(tok, 'heading')

    return round(score, 3), matched_terms, matched_fields


def _term_overlap(terms_a: list[str], terms_b: list[str]) -> float:
    sa, sb = set(terms_a), set(terms_b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


def select_skills(
    query: str,
    *,
    project=None,
    role: str | None = None,
    max_shortlist: int = MAX_SHORTLIST,
    max_selected: int = MAX_AUTO_SELECTED,
    storage_root=None,
    safe: bool = False,
) -> dict:
    """Deterministically rank eligible managed skills into a bounded shortlist with explicit 5-state abstention.

    Hard eligibility gates (`role_policy` ∩ `allowed_roles`, plus verified backing snapshots for shortlisted
    candidates) are enforced before selection: an ineligible skill never becomes eligible by lexical score.
    """
    if role is not None:
        role = _managed._validate_role(role)
    if not isinstance(max_shortlist, int) or not 1 <= max_shortlist <= MAX_SHORTLIST:
        raise ValueError(f'max_shortlist must be an integer between 1 and {MAX_SHORTLIST}')
    if not isinstance(max_selected, int) or not 1 <= max_selected <= MAX_AUTO_SELECTED:
        raise ValueError(f'max_selected must be an integer between 1 and {MAX_AUTO_SELECTED}')

    query_text = query if isinstance(query, str) else ''
    query_sha = hashlib.sha256(query_text.encode('utf-8')).hexdigest()

    def _error_receipt(msg: str) -> dict:
        return {
            'schema': SCHEMA,
            'method': METHOD,
            'authority': 'NONE',
            'effects_authorized': False,
            'status': 'error',
            'reason': 'selector_technical_error',
            'role': role,
            'query_sha256': query_sha,
            'catalog_sha256': None,
            'eligible_count': 0,
            'selected_ids': [],
            'shortlist': [],
            'unavailable_matches': [],
            'error': msg,
        }

    if not query_text.strip():
        if safe:
            return _error_receipt('Selection query must be a non-empty string')
        raise ValueError('Selection query must be a non-empty string')

    try:
        store = _managed.load_store(storage_root=storage_root)
        catalog_sha = _managed.catalog_index_fingerprint(project=project, storage_root=storage_root)
    except (OSError, ValueError) as exc:
        if safe:
            return _error_receipt(str(exc))
        raise

    eligible_records: list[dict] = []
    ineligible_records: list[tuple[dict, str]] = []

    for sid in sorted(store['skills']):
        try:
            view = _managed.effective_view(sid, project=project, role=role, storage_root=storage_root)
        except (OSError, ValueError) as exc:
            if safe:
                return _error_receipt(f'Failed to resolve managed skill {sid!r}: {exc}')
            raise
        rec = _build_lightweight_record(view)
        if role is not None:
            ra = view['role_access']
            if ra['allowed']:
                eligible_records.append(rec)
            else:
                ineligible_records.append((rec, ra['reason']))
        else:
            if view['effective_roles']:
                eligible_records.append(rec)
            else:
                reason = 'role_disabled_by_policy' if view.get('disabled_by_policy_roles') else 'no_allowed_roles'
                ineligible_records.append((rec, reason))

    query_folded = _fold(query_text)
    query_tokens = _tokenize(query_text)
    all_records = eligible_records + [r for r, _ in ineligible_records]
    n_docs = max(1, len(all_records))
    doc_freq: dict[str, int] = {}
    for rec in all_records:
        vocab = rec['trigger_tokens'] | rec['slug_tokens'] | rec['name_tokens'] | rec['desc_tokens'] | rec['heading_tokens']
        for tok in vocab:
            doc_freq[tok] = doc_freq.get(tok, 0) + 1
    idf_map = {
        tok: round(1.0 + math.log((1.0 + n_docs) / (1.0 + doc_freq.get(tok, 0))) / 2.0, 3)
        for tok in query_tokens
    }

    scored_eligible: list[dict] = []
    for rec in eligible_records:
        score, terms, fields = _score_record(
            rec, query_folded=query_folded, query_tokens=query_tokens, idf_map=idf_map,
        )
        if score >= SHORTLIST_THRESHOLD:
            scored_eligible.append({
                'skill_id': rec['skill_id'],
                'skill_name': rec['skill_name'],
                'score': score,
                'matched_terms': terms,
                'matched_fields': fields,
                'effective_roles': rec['effective_roles'],
                'effective_sha256': rec['effective_sha256'],
                'meets_threshold': score >= SELECT_THRESHOLD,
                '_bundle_id': rec['bundle_id'],
                '_snapshot_id': rec['snapshot_id'],
            })

    scored_eligible.sort(key=lambda item: (-item['score'], item['skill_id']))

    unavailable_matches: list[dict] = []
    for rec, inelig_reason in ineligible_records:
        score, terms, fields = _score_record(
            rec, query_folded=query_folded, query_tokens=query_tokens, idf_map=idf_map,
        )
        if score >= SELECT_THRESHOLD:
            unavailable_matches.append({
                'skill_id': rec['skill_id'],
                'skill_name': rec['skill_name'],
                'score': score,
                'matched_terms': terms,
                'matched_fields': fields,
                'reason': inelig_reason,
            })

    # Verify backing inert snapshots for every threshold-passing candidate (so max_shortlist truncation
    # never hides a competing tie) and enough sub-threshold candidates to populate the bounded shortlist.
    shortlist: list[dict] = []
    passing: list[dict] = []
    for cand in scored_eligible:
        if not cand['meets_threshold'] and len(shortlist) >= max_shortlist:
            break
        try:
            _bundles.load_snapshot(
                cand['_bundle_id'], cand['_snapshot_id'], storage_root=storage_root, verify=True,
            )
        except (OSError, ValueError):
            if cand['meets_threshold']:
                unavailable_matches.append({
                    'skill_id': cand['skill_id'],
                    'skill_name': cand['skill_name'],
                    'score': cand['score'],
                    'matched_terms': cand['matched_terms'],
                    'matched_fields': cand['matched_fields'],
                    'reason': 'snapshot_invalid',
                })
            continue
        item = {k: v for k, v in cand.items() if not k.startswith('_')}
        if item['meets_threshold']:
            passing.append(item)
        if len(shortlist) < max_shortlist:
            shortlist.append(item)

    unavailable_matches.sort(key=lambda item: (-item['score'], item['skill_id']))
    effective_max_selected = min(max_selected, max_shortlist)

    status: str
    reason: str
    selected_ids: list[str] = []

    if not passing:
        if unavailable_matches:
            status = 'unavailable'
            reason = (
                'Matching skill(s) exist in the catalog but are ineligible or have invalid snapshots: '
                + ', '.join(f"{u['skill_id']} ({u['reason']})" for u in unavailable_matches[:3])
            )
        else:
            status = 'no_skill'
            reason = (
                'No managed skills are eligible for this scope.'
                if not eligible_records else
                'No eligible managed skill crossed the deterministic selection threshold; abstaining.'
            )
    else:
        # Check for genuine ambiguity across ALL verified passing candidates before any shortlist cutoff.
        competing_pair = None
        for i, first in enumerate(passing):
            for second in passing[i + 1:]:
                if abs(first['score'] - second['score']) <= AMBIGUITY_MARGIN:
                    overlap = _term_overlap(first['matched_terms'], second['matched_terms'])
                    if overlap >= AMBIGUITY_TERM_OVERLAP:
                        competing_pair = (first['skill_id'], second['skill_id'])
                        break
            if competing_pair:
                break

        if competing_pair is not None:
            status = 'ambiguous'
            reason = (
                f"Competing eligible skills matched overlapping terms within score margin: "
                f"{competing_pair[0]} vs {competing_pair[1]}; abstaining until explicitly chosen."
            )
        elif (
            len(passing) > effective_max_selected
            and (passing[effective_max_selected - 1]['score'] - passing[effective_max_selected]['score']) <= AMBIGUITY_MARGIN
        ):
            status = 'ambiguous'
            reason = (
                f'More than {effective_max_selected} eligible skills clustered at the selection cutoff; '
                'abstaining until explicitly narrowed.'
            )
        else:
            selected_ids = [item['skill_id'] for item in passing[:effective_max_selected]]
            status = 'selected'
            reason = f"Deterministically selected {len(selected_ids)} eligible skill(s) from {len(shortlist)} shortlisted."

    return {
        'schema': SCHEMA,
        'method': METHOD,
        'authority': 'NONE',
        'effects_authorized': False,
        'status': status,
        'reason': reason,
        'role': role,
        'query_sha256': query_sha,
        'catalog_sha256': catalog_sha,
        'eligible_count': len(eligible_records),
        'selected_ids': selected_ids,
        'shortlist': shortlist,
        'unavailable_matches': unavailable_matches,
        'error': None,
    }


def format_selection(receipt: dict) -> str:
    """Human-readable summary of a deterministic skill selection / abstention receipt."""
    lines = [
        f"Skill selection ({receipt['method']}): status={receipt['status']}  "
        f"role={receipt['role'] or 'conversation'}  eligible={receipt['eligible_count']}",
        f"Reason: {receipt['reason']}",
        f"Catalog SHA-256: {(receipt.get('catalog_sha256') or 'n/a')[:16]}  "
        f"Authority: {receipt['authority']}  Effects authorized: {receipt['effects_authorized']}",
    ]
    if receipt.get('selected_ids'):
        lines.append('Selected: ' + ', '.join(receipt['selected_ids']))
    if receipt.get('shortlist'):
        lines.append(f"Shortlist ({len(receipt['shortlist'])}/{MAX_SHORTLIST}):")
        for item in receipt['shortlist']:
            mark = '*' if item['skill_id'] in receipt['selected_ids'] else ' '
            terms = ', '.join(item['matched_terms']) or '-'
            roles = _managed.format_role_tags(item['effective_roles'])
            lines.append(
                f"  {mark} {item['skill_id']:<24} score={item['score']:5.2f}  "
                f"roles={roles:<16}  matched=[{terms}]"
            )
    if receipt.get('unavailable_matches'):
        lines.append('Ineligible / unavailable matches (never promoted by score):')
        for u in receipt['unavailable_matches']:
            lines.append(
                f"  ! {u['skill_id']:<24} reason={u['reason']}  matched=[{', '.join(u['matched_terms'])}]"
            )
    if receipt.get('error'):
        lines.append(f"Error: {receipt['error']}")
    return '\n'.join(lines)
