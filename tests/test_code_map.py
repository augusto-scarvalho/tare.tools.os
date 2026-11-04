"""The explorers' code map: outlines, ranking within a size, a balanced split and what a part left unread."""
from relay import code_map


def repo(tmp_path):
    (tmp_path/'src').mkdir()
    for name, size in (('a', 40), ('b', 30), ('c', 20), ('d', 10)):
        body = f'def {name}_main(value):\n    return helper_{name}(value)\n\n\ndef helper_{name}(value):\n'
        body += ''.join(f'    value += {i}\n' for i in range(size)) + '    return value\n'
        (tmp_path/'src'/f'{name}.py').write_text(body, encoding='utf-8')
    (tmp_path/'notes.txt').write_text('not code\n', encoding='utf-8')
    return tmp_path


def test_outlines_list_code_with_signatures_and_reuse_the_cache(tmp_path):
    root = repo(tmp_path)
    cache = tmp_path/'cache'/'outlines.json'
    code = code_map.outlines(root, '.', cache)
    assert sorted(code) == ['src/a.py', 'src/b.py', 'src/c.py', 'src/d.py']  # code only: notes.txt is not
    assert code['src/a.py']['defs'][0][:5] == ['def', 'a_main', 1, 2, 'def a_main(value)']
    assert code['src/a.py']['defs'][0][5] == ['helper_a']
    (root/'src'/'a.py').write_text('def changed():\n    pass\n', encoding='utf-8')
    assert [d[1] for d in code_map.outlines(root, '.', cache)['src/a.py']['defs']] == ['changed']


def test_render_puts_relevant_files_first_within_the_limit(tmp_path):
    code = code_map.outlines(repo(tmp_path), '.', None)
    text = code_map.render(code, 'What does helper_c do?', 10_000)
    assert text.startswith('src/c.py')
    short = code_map.render(code, 'What does helper_c do?', 120)
    assert len(short) < 220 and short.startswith('src/c.py') and 'Not shown:' in short


def test_unparsed_project_languages_remain_visible_next_to_python_tooling(tmp_path):
    (tmp_path/'tools').mkdir()
    (tmp_path/'tools'/'art.py').write_text('def draw():\n    pass\n', encoding='utf-8')
    for name in ('main.gd', 'other.unfamiliar'):
        (tmp_path/name).write_text('func pay_neon():\n    return score / 25.0\n', encoding='utf-8')
    (tmp_path/'main.gd.uid').write_text('uid://example\n', encoding='utf-8')
    (tmp_path/'notes.txt').write_text('score is a design question\n', encoding='utf-8')
    (tmp_path/'asset.unknown').write_bytes(b'\x00binary\n')
    code = code_map.outlines(tmp_path, '.')
    assert set(code) == {'main.gd', 'other.unfamiliar', 'tools/art.py'}
    assert code['main.gd']['lines'] == 2 and code['main.gd']['defs'] == []
    assert 'main.gd (2 lines)' in code_map.render(code, 'pay_neon', 6000)


def test_candidates_search_the_same_unparsed_sources_and_respect_word_boundaries(tmp_path):
    (tmp_path/'tools').mkdir()
    (tmp_path/'tools'/'art.py').write_text('# desenho das costas\n', encoding='utf-8')
    (tmp_path/'main.gd').write_text('var upgrade_cost = 25\n', encoding='utf-8')
    (tmp_path/'other.unfamiliar').write_text('cost = 50\n', encoding='utf-8')
    (tmp_path/'notes.md').write_text('cost = 100\n', encoding='utf-8')
    (tmp_path/'data.json').write_text('{"cost": 200}\n', encoding='utf-8')
    hits = code_map.matches(tmp_path, '.', r'\bcost\b|upgrade_cost')
    assert [(p, n) for p, n, _ in hits] == [('main.gd', 1), ('other.unfamiliar', 1)]
    assert [(p, n) for p, n, _ in code_map.matches(tmp_path, 'main.gd', 'upgrade_cost')] == [('main.gd', 1)]
    assert set(code_map.outlines(tmp_path, 'main.gd')) == {'main.gd'}
    assert [p for p, *_ in code_map.matches(tmp_path, '.', 'cost', markdown=True)] == [
        'main.gd', 'notes.md', 'other.unfamiliar', 'tools/art.py']


def test_partition_is_deterministic_balanced_and_covers_everything(tmp_path):
    code = code_map.outlines(repo(tmp_path), '.', None)
    parts = code_map.partition(code, 2)
    assert parts == code_map.partition(code, 2)
    assert sorted(p for part in parts for p, *_ in part) == sorted(code)
    sizes = [sum(b - a + 1 for _, a, b in part) for part in parts]
    assert max(sizes) <= 2*min(sizes)


def test_covered_reports_pieces_read_less_than_half():
    pieces = [('a.py', 1, 10), ('b.py', 1, 10), ('c.py', 1, 10)]
    assert code_map.covered(pieces, [('a.py', 1, 8), ('b.py', 1, 3), ('c.py', 1, None)]) == [('b.py', 1, 10)]
