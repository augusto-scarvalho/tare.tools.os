"""BenchLM's ranks of open-weight models as the local scale's boards (owner, 2026-11-18): read once a day with the
user's own key, one GET for every surface, never a hosted model's class. Offline: every row and rank here is invented."""
from io import StringIO

from relay import environment
from relay import model_signals as signals
from test_reviewer_rank import local
from test_task_types import DAY, KIMI, NOW, ONE, board

ATTRIBUTION = 'Data from BenchLM.ai; retain supplied publisher attribution and license notices'
POLICY = 'https://benchlm.ai/terms#data-access'
# The twelve consumer models in BenchLM's spellings, best first on coding. Fifths of twelve, best first:
# 4 4 3 3 2 2 2 1 1 0 0 0.
CODING = ['Qwen3.8-Flash-Next', 'Qwen3.8-27B', 'GPT-OSS 120B', 'Qwen3.6-27B', 'Gemma 4 31B', 'Gemma 4 26B A4B',
          'Qwen3.6-35B-A3B', 'Llama 4 Scout', 'GPT-OSS 20B', 'Gemma 3 27B', 'Qwen3 8B', 'Qwen3.5 0.8B']
SURFACES = {'coding': CODING, 'reasoning': CODING[::-1], 'overall': CODING[6:] + CODING[:6]}
LABS = {'Qwen': 'Alibaba', 'GPT': 'OpenAI', 'Gemma': 'Google', 'Llama': 'Meta', 'Kimi': 'Moonshot AI'}


def item(name, ranks, evidence='supported'):
    creator = next((lab for prefix, lab in LABS.items() if name.startswith(prefix)), 'Example Lab')
    return {'modelKey': name.lower().replace(' ', '-'), 'name': name, 'creator': creator, 'openWeight': True,
            'overall': {'rank': ranks.get('overall'), 'score': 50.0, 'evidenceStatus': evidence}, 'surfaceRanks': ranks}


FEED = {'dataAsOf': '2026-11-17T06:00:00Z', 'attribution': ATTRIBUTION, 'policyUrl': POLICY, 'methodologyVersion': 'fixture',
        'items': [item('Kimi K3.1 Pro', {'overall': 1, 'coding': 1, 'reasoning': 1}, 'estimated'),  # not a consumer model
                  item('Open Model X', {'overall': 40}),  # no position on coding or reasoning
                  *(item(n, {s: 10 + 2*order.index(n) for s, order in SURFACES.items()}) for n in CODING)],
        'pagination': {'total': 14, 'nextOffset': None}}
KEYS = {s['board']: signals.source_key(s) for t in signals.TYPES for s in [*t['sources'], *signals.LOCAL_GENERAL]
        if s['reader'] == 'benchlm'}


def refresh(monkeypatch, at, key='fixture-benchlm-key', setup=None, answer=FEED):
    """One refresh pass at `at` with `key` stored; the (source, key) of each GET of BenchLM. Only BenchLM answers."""
    calls = []
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: key if name == 'BENCHLM_API_KEY' else None)

    def fetch(source, token):
        calls.append((source, token))
        if source != 'benchlm' or answer is None:
            raise OSError('offline')  # the other boards are not this test's
        return answer
    signals.refresh_boards(at, fetch, {} if setup is None else setup)
    return [c for c in calls if c[0] == 'benchlm']


def saved(monkeypatch):
    refresh(monkeypatch, NOW)
    monkeypatch.setattr(signals, 'reading', lambda: None)
    return signals.boards()


def test_the_feed_gives_each_surface_its_published_ranks_with_their_evidence_and_no_line():
    rows, as_of, notice = signals.parse_benchlm(FEED, {'board': 'coding'})
    by = {r['name']: r for r in rows}
    assert as_of == '2026-11-17' and notice == {'attribution': ATTRIBUTION, 'policy_url': POLICY}
    assert by['Qwen3.8-Flash-Next']['intelligence'] == 10 and by['Kimi K3.1 Pro']['evidence'] == 'estimated'
    assert len(rows) == 13 and 'Open Model X' not in by  # a model with no position on the surface is no row
    assert all(r['line'] is None for r in rows)  # GPT-OSS's too: no row is ever an anchor of a hosted class
    assert signals.parse_benchlm({**FEED, 'items': []}, {'board': 'coding'}) is None


def test_one_get_a_day_reads_every_benchlm_board(monkeypatch):
    assert refresh(monkeypatch, NOW) == [('benchlm', 'fixture-benchlm-key')]
    kept = signals.boards()
    assert sorted(KEYS) == ['coding', 'overall', 'reasoning']  # mathematics and science share the reasoning board
    for found in map(kept.get, KEYS.values()):
        assert (found['read_at'], found['as_of'], found['attribution'], found['policy_url']) == (NOW, '2026-11-17', ATTRIBUTION, POLICY)
        # lower is better: the rank is negated, so Kimi, first everywhere, holds the highest number
        assert max(found['rows'], key=lambda r: r['intelligence'])['name'] == 'Kimi K3.1 Pro'
    assert refresh(monkeypatch, NOW + 3600) == []  # once a day
    assert refresh(monkeypatch, NOW + 2*DAY, answer=None) == [('benchlm', 'fixture-benchlm-key')]  # a failed GET
    assert signals.boards()[KEYS['coding']]['rows'] == kept[KEYS['coding']]['rows']
    assert refresh(monkeypatch, NOW + 2*DAY + 3700) == []  # a failed try waits a day too: 1,000 reads a month


def test_without_a_key_nothing_is_read_and_nothing_changes(monkeypatch):
    assert refresh(monkeypatch, NOW, key=None) == [] and not set(KEYS.values()) & set(signals.boards())
    before = saved(monkeypatch)
    later = NOW + 2*DAY
    assert refresh(monkeypatch, later, key=None) == []
    declined = {'api_providers': {'items': {'BENCHLM_API_KEY': False}}}  # tare setup --no-use api:BENCHLM_API_KEY
    assert refresh(monkeypatch, later, setup=declined) == []
    assert {k: signals.boards()[k] for k in KEYS.values()} == {k: before[k] for k in KEYS.values()}  # no tried_at either


def test_a_hosted_route_keeps_its_class_beside_a_benchlm_board(monkeypatch):
    kept = saved(monkeypatch)
    coding = signals.task_type('coding')
    # The worst case too: a board under BenchLM's key ranking eight known lines below Kimi would place it frontier.
    worst = {KEYS['coding']: board(ONE, ('kimi-k3.1-pro_max', 'Moonshot', .80))}
    for with_board in ({KEYS['coding']: kept[KEYS['coding']]}, worst):
        assert signals.judged(KIMI, 'coding', with_board) == signals.judged(KIMI, 'coding', {}) == {'class': 'cheap', 'source': 'general'}
        assert signals.best_first(KIMI, 'coding', with_board) == signals.best_first(KIMI, 'coding', {})
        assert signals.type_class(KIMI, coding, with_board)['general']


FLASH, BIG, MOE = local('qwen3.8-flash-next'), local('gpt-oss-120b'), local('qwen3.6-35b-a3b')


def test_local_models_take_their_fifths_on_benchlms_coding_board(monkeypatch):
    kept = saved(monkeypatch)
    rows = kept[KEYS['coding']]['rows']
    assert [signals._band(rows, d) for d in (FLASH, BIG, MOE)] == [4, 3, 2]
    # the three spellings BenchLM needs (consumer_models.json aliases)
    assert [signals._band(rows, local(m)) for m in ('gemma-4-31b-it', 'llama-4-scout-17b-16e-instruct', 'gemma-3-27b-it')] == [2, 1, 0]
    assert signals.may_review(BIG, FLASH, 'coding', kept)  # one band below the author
    assert not signals.may_review(MOE, FLASH, 'coding', kept)  # two below


def test_a_math_or_science_unit_compares_on_the_reasoning_surface(monkeypatch):
    kept = saved(monkeypatch)  # reasoning reverses coding: Flash-Next is band 0 there, Qwen3.6 MoE band 2
    for kind in ('math', 'science'):
        assert not signals.may_review(FLASH, MOE, kind, kept) and signals.may_review(MOE, FLASH, kind, kept)
    assert signals.may_review(FLASH, MOE, 'coding', kept)


def test_a_type_with_no_board_compares_two_locals_on_overall(monkeypatch):
    kept = saved(monkeypatch)
    q27, scout = local('qwen3.6-27b'), local('llama-4-scout-17b-16e-instruct')  # overall: band 0 and band 4
    assert not signals.may_review(q27, scout, 'games', kept)
    assert not signals.may_review(q27, scout)  # no type: the saved boards, read here
    assert signals.may_review(q27, scout, 'coding', kept) and signals.may_review(q27, scout, 'math', kept)


def test_storing_the_benchlm_key_marks_no_api_provider(monkeypatch):
    import keyring
    stored = {}
    monkeypatch.setattr(keyring, 'set_password', lambda service, name, secret: stored.update({name: secret}))
    assert environment.store_credential({}, 'BENCHLM_API_KEY', StringIO('fixture-benchlm-key\n')) == {}
    assert stored == {'BENCHLM_API_KEY': 'fixture-benchlm-key'}
