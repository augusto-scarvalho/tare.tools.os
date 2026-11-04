"""Dated model references, separate from exact-profile execution evidence."""
import copy
import json
from pathlib import Path

from compute_plane.choice_assessment import digest

CATALOG = Path(__file__).with_name('model_evidence.json')


def configured_effort(profile, described):
    """Read the actual request parameters without inventing supported efforts."""
    if described:
        return described
    params = json.loads(profile.get('parameters_json', '{}'))
    if profile['protocol'] == 'bedrock-converse':
        extra = params.get('additionalModelRequestFields', {})
        return extra.get('output_config', {}).get('effort') or extra.get('reasoning_config') or 'profile-defined'
    return params.get('chat_template_kwargs', {}).get('reasoning_effort') or 'profile-defined'


def references(deployment, effort):
    """Exact wire aliases only; model references never become profile qualification.

    Bundled observations need no environment configuration or per-dispatch web
    calls. Refresh the dated catalog explicitly; never rewrite old receipts.
    """
    from .native_agent import read_json
    catalog = read_json(CATALOG, 262144)
    if catalog.get('schema') != 'tare.tools/model-evidence/1':
        raise ValueError('MODEL_EVIDENCE_CATALOG_INVALID')
    profile = deployment['native_profile']
    record = next((row for row in catalog['models'] if deployment['model'] in [row['model'], *row.get('model_aliases', [])]
                   and profile['model'] in row['wire_models']), None)
    if record is None:
        return {'records': [], 'price_references': []}
    identity = {'catalog_sha256': digest(catalog), 'catalog_record': record['model']}
    result = {'records': [], 'price_references': [], **identity}
    for source in record.get('quality', []):
        item = copy.deepcopy(source)
        tested = item.get('tested_effort')
        item['applicability'] = {
            'level': 'MODEL_REFERENCE' if item['kind'] == 'public-benchmark' else 'HISTORICAL_RUNTIME',
            'effort': 'MATCH' if tested == effort else 'UNKNOWN' if not tested else 'DIFFERS',
            'runtime': 'MATCH' if item.get('tested_protocol') == profile['protocol'] else 'NOT_ESTABLISHED',
            'current_profile_qualified': False}
        result['records'].append(item)
    # API reference prices must never be mistaken for a CLI plan charge.
    if profile['protocol'] != 'vendor-cli':
        result['price_references'] = copy.deepcopy(record.get('price_references', []))
    return result
