"""A deterministic map of the code an explorer works in, and a split of it between explorers.

Outlines come from Python's own ast (classes, functions, signatures, line ranges and the names each calls);
other files appear with their size. Calls are matched by name, an approximation that is enough to rank code
near a question and to keep code that calls itself with one explorer. SpecGraph's index binds references
exactly, but builds in tens of seconds per repository; this map must be ready before the explorers start.
"""
import ast
import hashlib
import json
import re
import subprocess
from compute_plane.managed_process import host_executable
from pathlib import Path

SKIP = {'node_modules', '__pycache__', 'venv', '.venv'}
# A parser is optional: unrecognised source languages must remain visible next to Python tooling.
# Keep ordinary documentation, data and assets out of code coverage, rather than admitting only languages we know.
NON_SOURCE = {'.md', '.rst', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.csv', '.tsv',
              '.log', '.lock', '.uid', '.tscn', '.tres', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico',
              '.wav', '.mp3', '.ogg', '.mp4', '.ttf', '.woff', '.woff2', '.zip', '.pdf'}


def files(root, scope):
    """Repository-relative files under scope: git's tracked and untracked unignored files, else a plain walk."""
    root = Path(root)
    try:
        listed = subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard', '--', scope], capture_output=True, timeout=30, check=True)
        paths = [p for p in listed.stdout.decode('utf-8', 'replace').split('\0') if p]
    except (OSError, subprocess.SubprocessError):
        base = root/scope
        paths = [p.relative_to(root).as_posix() for p in ([base] if base.is_file() else base.rglob('*'))
                 if p.is_file() and not any(part.startswith('.') or part in SKIP for part in p.relative_to(root).parts)]
    return sorted(p for p in paths if not any(part.startswith('.') or part in SKIP for part in p.split('/')))


def outline(path):
    """{'lines': n, 'defs': [[kind, name, start, end, signature, [called names]]]} for one file."""
    raw = path.read_bytes()
    lines = raw.count(b'\n') + (1 if raw and not raw.endswith(b'\n') else 0)
    if b'\0' in raw[:4096]:
        return {'lines': 0, 'defs': []}  # binary
    defs = []
    if path.suffix == '.py':
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):
            tree = None

        def visit(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = prefix + child.name
                    if isinstance(child, ast.ClassDef):
                        signature = 'class ' + child.name + (
                            '(' + ', '.join(ast.unparse(b) for b in child.bases) + ')' if child.bases else '')
                    else:
                        signature = 'def ' + child.name + '(' + ast.unparse(child.args) + ')'
                    calls = sorted({c.func.id if isinstance(c.func, ast.Name) else c.func.attr
                                    for c in ast.walk(child) if isinstance(c, ast.Call)
                                    and isinstance(c.func, (ast.Name, ast.Attribute))})
                    defs.append(['class' if isinstance(child, ast.ClassDef) else 'def', name, child.lineno,
                                 child.end_lineno, signature[:160], calls[:40]])
                    visit(child, name + '.')
        if tree is not None:
            visit(tree, '')
    return {'lines': lines, 'defs': defs}


def outlines(root, scope, cache=None):
    """{path: outline} under scope. `cache` is a JSON file of earlier outlines, reused while size and mtime match."""
    root = Path(root)
    saved = {}
    if cache is not None and cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
    result, fresh = {}, {}
    listed = files(root, scope)
    # Code is what explorers split and navigate; logs, prompts and data only when the scope has no code.
    code = [p for p in listed if Path(p).suffix.lower() not in NON_SOURCE]
    for relative in code or listed:
        path = root/relative
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
                continue
            stamp = [path.stat().st_size, path.stat().st_mtime_ns]
            entry = saved.get(relative)
            if not entry or entry['stamp'] != stamp:
                entry = {'stamp': stamp, **outline(path)}
        except OSError:
            continue
        fresh[relative] = entry
        if entry['lines']:
            result[relative] = entry
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({**saved, **fresh}), encoding='utf-8')
        except OSError:
            pass  # ponytail: without a writable cache every map parses again
    return result


def terms(question):
    return {t.strip('-') for t in re.findall(r'[a-z_][a-z0-9_-]{2,}', question.casefold()) if t.strip('-') not in STOP}


STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'what', 'which', 'where', 'how', 'does', 'from', 'into',
        'each', 'every', 'are', 'its', 'their', 'when', 'who', 'not', 'any', 'all', 'find', 'list', 'file', 'files'}


def matches(root, scope, pattern, limit=400, markdown=False):
    """(path, line, text) of each code line under scope matching a ripgrep regex, in path order; with
    `markdown`, Markdown lines too. More than `limit` rows means the search was cut there, in path order."""
    excluded = sorted(NON_SOURCE - ({'.md'} if markdown else set()))
    found = subprocess.run(['rg', '-n', '--with-filename', '--no-heading', '--max-columns', '200', '--sort', 'path',
                            *[a for ext in excluded for a in ('-g', '!*'+ext)],
                            '-e', pattern, '--', scope],
                           cwd=root, capture_output=True, timeout=60)
    if found.returncode not in (0, 1):
        raise ValueError('candidate search failed: ' + found.stderr.decode('utf-8', 'replace')[:300])
    rows = []
    for line in found.stdout.decode('utf-8', 'replace').splitlines():
        if len(rows) > limit:
            break
        path, _, rest = line.partition(':')
        number, _, text = rest.partition(':')
        if number.isdigit():
            path = path.replace(chr(92), '/')
            while path.startswith('./'):
                path = path[2:]
            rows.append((path, int(number), text.strip()[:160]))
    return rows


def enclosing(code, path, line):
    """The innermost definition around a line, as (name, signature), or None."""
    best = None
    for _, name, start, end, signature, _ in (code.get(path) or {}).get('defs', []):
        if start <= line <= end and (best is None or start >= best[0]):
            best = (start, name, signature)
    return best and best[1:]


def rank(candidates, question, code):
    """A score in [0, 1] per candidate (path, line, text): the question's words in its path (3 each), in the name of
    the definition around it (2) and in its text (1); a deterministic baseline for choosing what explorers judge."""
    words = terms(question)
    raw = []
    for path, line, text in candidates:
        around = enclosing(code, path, line)
        raw.append(3*sum(w in path.casefold() for w in words) + 2*sum(w in (around[0] if around else '').casefold()
                   for w in words) + sum(w in text.casefold() for w in words))
    top = max(raw, default=0) or 1
    return [value/top for value in raw]


def share(items, parts):
    """Items (path, line, text) in path order, split into `parts` contiguous runs balanced by count; one file's
    items stay together."""
    files = {}
    for item in items:
        files.setdefault(item[0], []).append(item)
    result, current, done = [[] for _ in range(parts)], 0, 0
    for group in files.values():
        target = len(items)*(current+1)/parts
        if current < parts - 1 and result[current] and done + len(group) - target > target - done:
            current += 1
        result[current] += group
        done += len(group)
    return [part for part in result if part]


def scores(code, question):
    """A score per file: question words in its path, definitions and the names they call, plus a share of the
    score of files whose definitions it calls or that call it."""
    words = terms(question)
    own = {}
    for path, entry in code.items():
        # The best definition counts, not their sum: a large file is not more relevant for being large.
        best = max((2*sum(w in name.casefold() for w in words) + min(2, sum(w in c.casefold() for c in calls
                    for w in words)) for kind, name, *_, calls in entry['defs']), default=0)
        own[path] = 3*sum(w in path.casefold() for w in words) + best
    defined = {}
    for path, entry in code.items():
        for _, name, *_ in entry['defs']:
            defined.setdefault(name.rsplit('.', 1)[-1], set()).add(path)
    near = {path: 0 for path in code}
    for path, entry in code.items():
        for called in {c for *_, calls in entry['defs'] for c in calls}:
            for other in defined.get(called, ()):
                if other != path:  # the best neighbour counts: a file calling everything is not near everything
                    near[path] = max(near[path], own[other])
                    near[other] = max(near[other], own[path])
    return {path: own[path] + near[path] // 3 for path in code}


def render(code, question, limit, owners=None, mine=None):
    """The map as text within `limit` characters: files by relevance (then path), each with its definitions;
    with `owners`, each file says which explorer covers it, and `mine` (one explorer's paths) come first."""
    ranked = scores(code, question)
    order = sorted(code, key=lambda p: (mine is not None and p not in mine, -ranked[p], p))
    out, used, left = [], 0, []
    for path in order:
        entry = code[path]
        owner = f' [explorer {owners[path]}]' if owners and path in owners else ''
        head = f"{path} ({entry['lines']} lines){owner}\n"
        body = ''.join(f"{'  '*(name.count('.')+1)}{start}-{end} {signature}\n"
                       for kind, name, start, end, signature, _ in entry['defs'])
        block = head + body
        if used + len(block) > limit:
            block = head if used + len(head) <= limit and (ranked[path] or mine and path in mine) else ''
        if not block:
            left.append(path)
            continue
        out.append(block)
        used += len(block)
    if left:
        folders = {}
        for path in left:
            folder = path.rsplit('/', 1)[0] if '/' in path else '.'
            folders[folder] = folders.get(folder, 0) + 1
        top = sorted(folders.items(), key=lambda item: (-item[1], item[0]))
        out.append(f'Not shown: {len(left)} files, most in ' + ', '.join(f'{f}/ ({n})' for f, n in top[:6]) + '\n')
    return ''.join(out)


def units(code, parts):
    """The pieces to share out: whole files, and files over half an explorer's share split at top-level
    definitions into contiguous ranges."""
    total = sum(entry['lines'] for entry in code.values()) or 1
    share = max(1, total // parts)
    result = []
    for path in sorted(code):
        entry = code[path]
        if entry['lines'] <= max(share // 2, 300) or not entry['defs']:  # a small file is read whole
            result.append((path, 1, max(1, entry['lines'])))
            continue
        cuts, start = [], 1
        for kind, name, first, last, *_ in entry['defs']:
            # Top-level definitions and the methods of top-level classes: a file that is one class still splits.
            if name.count('.') <= 1 and first - start >= share // 2:
                cuts.append((start, first - 1))
                start = first
        cuts.append((start, max(start, entry['lines'])))
        result += [(path, a, b) for a, b in cuts]
    return result


def partition(code, parts):
    """Deterministic split of the code into `parts` contiguous runs of units in path order, balanced by lines,
    so a folder and its neighbours stay with one explorer. Returns one list of (path, start, end) per part."""
    pieces = units(code, parts)
    total = sum(b - a + 1 for _, a, b in pieces)
    result, current, done = [[] for _ in range(parts)], 0, 0
    for piece in pieces:
        size, target = piece[2] - piece[1] + 1, total*(current+1)/parts
        # A piece that would overshoot this part's target by more than it now falls short starts the next part.
        if current < parts - 1 and result[current] and done + size - target > target - done:
            current += 1
        result[current].append(piece)
        done += size
    return [part for part in result if part]


def neighbours(code, part, others, limit=3):
    """The pieces of other parts that this part's code calls or is called by most: read if needed."""
    names = lambda pieces: {n.rsplit('.', 1)[-1] for path, a, b in pieces for _, n, s, e, *_ in code[path]['defs']
                            if s <= b and e >= a}
    calls = lambda pieces: {c for path, a, b in pieces for _, n, s, e, _, cs in code[path]['defs']
                            if s <= b and e >= a for c in cs}
    mine_names, mine_calls = names(part), calls(part)
    weighted = []
    for piece in others:
        theirs = names([piece])
        weight = len(theirs & mine_calls) + len(calls([piece]) & mine_names)
        if weight:
            weighted.append((-weight, piece))
    return [piece for _, piece in sorted(weighted)[:limit]]


def covered(pieces, reads):
    """The pieces of which an explorer read less than half. `reads` holds (path, start, end); end None is the
    whole file."""
    missing = []
    for path, a, b in pieces:
        seen = set()
        for read_path, start, end in reads:
            if read_path == path:
                seen.update(range(max(a, start), min(b, end if end is not None else b) + 1))
        if len(seen)*2 < b - a + 1:
            missing.append((path, a, b))
    return missing


def cache_file(root):
    from .native_installation import storage_root
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    return storage_root()/'explore'/'outlines'/(key+'.json')
