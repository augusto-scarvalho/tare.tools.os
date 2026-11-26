"""Read-only discovery, deterministic classification, SHA-256 deduplication, role suggestion, and
source-drift checking for local vendor skills across Claude Code, Codex, and AGY (SKL-01c / PA-04).

All discovery is strictly read-only and inert: no vendor code runs, no native CLI skill loading is
enabled, and no skill is activated in any session until explicitly imported and selected by the owner.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from . import managed_skills as _managed
from . import skill_bundles as _bundles

CATALOG_SCHEMA = 'tare.tools/vendor-skill-catalog/1'
MAX_INSPECT_SKILL_MD_BYTES = 256 * 1024  # Bounded read for classification (skills > 64 KiB -> needs_sanitization)
MAX_PLUGIN_INDEX_BYTES = 1024 * 1024

SKILL_CLASSES = (
    'portable_ready',
    'local_tool_hybrid',
    'needs_sanitization',
    'blocked_capability',
    'cli_internal',
)
ACTIONABLE_CLASSES = frozenset({'portable_ready', 'local_tool_hybrid', 'needs_sanitization'})
HIDDEN_CLASSES = frozenset({'blocked_capability', 'cli_internal'})
CLASS_ORDER = {name: i for i, name in enumerate(SKILL_CLASSES)}
CLASS_LABELS = {
    'portable_ready': 'portable',
    'local_tool_hybrid': 'local-tool',
    'needs_sanitization': 'sanitize',
    'blocked_capability': 'blocked-cap',
    'cli_internal': 'cli-only',
}

VENDOR_ORDER = {'claude': 0, 'codex': 1, 'agy': 2}
VENDOR_LABELS = {'claude': 'Claude Code', 'codex': 'Codex', 'agy': 'AGY'}
TIER_ORDER = {'user': 0, 'plugin': 1, 'system': 2, 'builtin': 3}

# Strong deterministic signals of vendor-specific CLI/harness internals (never portable to tare).
_CLI_INTERNAL_RE = re.compile(
    r'\b(?:invoke_subagent|define_subagent|manage_subagents|run_workflow|swarm_monitor|swarm_pane|'
    r'swarm_narrator|native_swarm|generative_ui|agy-customizations|antigravity_guide|antigravity ide|'
    r'agy cli|mcp-app-development|ui-plugin-navigation|migrate-agent-settings|migrate-workflows|'
    r'skill-creator|skill-installer|openai-docs|codex_home|codex\.toml|enabledplugins)\b|'
    r'\$codex_home|~/\.claude/plugins|~/\.codex/skills|~/\.gemini/antigravity',
    re.IGNORECASE,
)
_CLI_INTERNAL_SLUGS = frozenset({
    'agy-customizations', 'antigravity_guide', 'antigravity-guide', 'automation', 'conversations',
    'generative_ui', 'generative-ui', 'mcp-app-development', 'migrate-agent-settings',
    'migrate-workflows', 'native_swarm', 'native-swarm', 'plugin', 'run-workflow',
    'swarm_monitor', 'swarm_narrator', 'swarm_pane', 'ui-plugin-navigation',
    'skill-creator', 'skill-installer', 'openai-docs', 'ponytail-help',
})

# External paid APIs / networked vendor services blocked by tare's deny-network sandbox.
_BLOCKED_CAPABILITY_RE = re.compile(
    r'\b(?:openai_api_key|anthropic_api_key|gemini_api_key|api\.openai\.com|images\.generate|'
    r'gpt-image-1|dall-e|imagen-3|firecrawl_api_key|tavily_api_key)\b',
    re.IGNORECASE,
)

# Foreign harness orchestration / tool cues inside otherwise user-authored skills -> needs_sanitization (SKL-05).
_FOREIGN_HARNESS_RE = re.compile(
    r'\b(?:mcp__[a-z0-9_]+|browser_subagent|subagent dispatch|slash command|/plugin|'
    r'claude code|codex cli)\b|\{\{kiro_dir\}\}',
    re.IGNORECASE,
)

# Local CLI / script runtimes that may work under explicit host command permissions or SKL-04 workflows.
_LOCAL_RUNTIME_PATTERNS = (
    ('python', re.compile(r'\bpython3?\b|\.py\b', re.IGNORECASE)),
    ('node', re.compile(r'\b(?:node|npx|npm|pnpm|playwright)\b', re.IGNORECASE)),
    ('cargo', re.compile(r'\bcargo\b', re.IGNORECASE)),
    ('office', re.compile(r'\b(?:python-docx|openpyxl|reportlab|libreoffice|soffice|pandoc)\b', re.IGNORECASE)),
)
_OFFICE_DOC_RE = re.compile(r'\b(?:docx|pptx|xlsx|python-docx|openpyxl|reportlab|libreoffice|soffice)\b', re.IGNORECASE)

# Role-suggestion cues (always restricted to DEFAULT_ENABLED_ROLES; never explorer/researcher/vision/heads_up).
_REVIEW_ROLE_RE = re.compile(r'\b(?:review|audit|verify|validate|debt|gain|defect|critique|rubric)\b', re.IGNORECASE)
_PLAN_ROLE_RE = re.compile(r'\b(?:plan|spec|blueprint|strategize|intent|specify|requirements|tasks|discovery|steering)\b', re.IGNORECASE)
_IMPL_ROLE_RE = re.compile(r'\b(?:impl|debug|pixel|audio|tui|playwright|typesafe|ponytail|refactor|frontend|build|code)\b', re.IGNORECASE)


def resolve_home(home=None) -> Path | None:
    """Resolve the home directory for vendor skill discovery (honoring TARE_VENDOR_SKILLS_HOME for isolation)."""
    if home is not None:
        return Path(home)
    override = os.environ.get('TARE_VENDOR_SKILLS_HOME')
    if override is not None:
        stripped = override.strip()
        return Path(stripped) if stripped else None
    return Path.home()


def _is_unc_or_network(path: Path) -> bool:
    anchor = path.absolute().anchor
    return anchor.startswith('\\\\') or anchor.startswith('//')


def _read_claude_plugin_skill_dirs(home: Path) -> list[tuple[str, Path]]:
    """Read Claude Code installed_plugins.json safely and return active plugin `skills/` directories."""
    index_path = home / '.claude' / 'plugins' / 'installed_plugins.json'
    if not index_path.is_file() or _bundles._is_link_or_reparse(index_path):
        return []
    try:
        raw, _ = _bundles._read_pinned_bytes(index_path, MAX_PLUGIN_INDEX_BYTES)
        if raw is None:
            return []
        data = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    plugins = data.get('plugins') if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for plugin_key, entries in sorted(plugins.items()):
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            install_raw = entry.get('installPath')
            if not isinstance(install_raw, str) or not install_raw.strip():
                continue
            install_path = Path(install_raw)
            if '..' in install_path.parts or _is_unc_or_network(install_path):
                continue
            skills_dir = install_path / 'skills'
            if not skills_dir.is_dir() or _bundles._is_link_or_reparse(skills_dir):
                continue
            norm = os.path.normcase(str(skills_dir.resolve()))
            if norm not in seen:
                seen.add(norm)
                out.append((f'claude-plugin:{plugin_key}', skills_dir))
    return out


def default_vendor_sources(home=None) -> list[dict]:
    """Return the local vendor skill root directories to inspect read-only."""
    base = resolve_home(home)
    if base is None or not base.is_dir():
        return []
    sources: list[dict] = [
        {'vendor': 'claude', 'tier': 'user', 'source_id': 'claude-user', 'path': base / '.claude' / 'skills'},
        {'vendor': 'codex', 'tier': 'user', 'source_id': 'codex-user', 'path': base / '.codex' / 'skills'},
        {'vendor': 'codex', 'tier': 'system', 'source_id': 'codex-system', 'path': base / '.codex' / 'skills' / '.system'},
        {'vendor': 'codex', 'tier': 'user', 'source_id': 'agents-user', 'path': base / '.agents' / 'skills'},
        {'vendor': 'agy', 'tier': 'user', 'source_id': 'agy-user', 'path': base / '.gemini' / 'antigravity-cli' / 'skills'},
        {'vendor': 'agy', 'tier': 'user', 'source_id': 'gemini-user', 'path': base / '.gemini' / 'skills'},
        {'vendor': 'agy', 'tier': 'builtin', 'source_id': 'agy-builtin', 'path': base / '.gemini' / 'antigravity-cli' / 'builtin' / 'skills'},
    ]
    for source_id, pdir in _read_claude_plugin_skill_dirs(base):
        sources.append({'vendor': 'claude', 'tier': 'plugin', 'source_id': source_id, 'path': pdir})
    return sources


def _strip_win_verbatim_prefix(raw: str) -> str:
    """Strip Windows verbatim prefixes (`\\?\\`, `\\??\\`) to inspect UNC/local paths without touching the network."""
    for prefix in ('\\\\?\\UNC\\', '//?/UNC/', '\\??\\UNC\\', '\\\\?\\unc\\', '\\??\\unc\\'):
        if raw.startswith(prefix):
            return '\\\\' + raw[len(prefix):]
    for prefix in ('\\\\?\\', '//?/', '\\??\\'):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _is_raw_unc(raw: str) -> bool:
    cleaned = _strip_win_verbatim_prefix(raw)
    return cleaned.startswith('\\\\') or cleaned.startswith('//')


def _resolve_vendor_skill_root(entry: Path) -> tuple[Path, bool] | None:
    """Resolve a direct vendor skill entry safely: allows one explicit top-level local link/junction hop."""
    try:
        is_link = _bundles._is_link_or_reparse(entry)
    except OSError:
        return None
    if not is_link:
        if not entry.is_dir():
            return None
        abs_entry = entry.absolute()
        if _is_unc_or_network(abs_entry):
            return None
        return abs_entry, False
    # Inspect the raw link target BEFORE resolving/stating so UNC network targets are never contacted,
    # and chained/cyclic symlinks are rejected cleanly without RuntimeError.
    try:
        raw_target = os.readlink(entry)
    except OSError:
        return None
    if not raw_target or _is_raw_unc(raw_target):
        return None
    cleaned = _strip_win_verbatim_prefix(raw_target)
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = entry.parent / candidate
    candidate_abs = Path(os.path.normpath(str(candidate.absolute())))
    if _is_unc_or_network(candidate_abs):
        return None
    try:
        # Exactly one hop: refuse chained links/junctions (also blocks symlink cycles deterministically).
        if not os.path.lexists(candidate_abs) or _bundles._is_link_or_reparse(candidate_abs):
            return None
        if not candidate_abs.is_dir():
            return None
        resolved = candidate_abs.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if _is_unc_or_network(resolved) or not resolved.is_dir():
        return None
    try:
        if _bundles._is_link_or_reparse(resolved):
            return None
    except OSError:
        return None
    return resolved, True


def _detect_local_runtimes(text: str, has_scripts_dir: bool) -> list[dict]:
    found: list[dict] = []
    if has_scripts_dir:
        found.append({'runtime': 'scripts/', 'available': True})
    for name, pat in _LOCAL_RUNTIME_PATTERNS:
        if pat.search(text):
            if name == 'python':
                avail = bool(shutil.which('python') or shutil.which('python3'))
            elif name == 'node':
                avail = bool(shutil.which('node'))
            elif name == 'cargo':
                avail = bool(shutil.which('cargo'))
            else:
                avail = bool(shutil.which('soffice') or shutil.which('libreoffice') or shutil.which('pandoc'))
            found.append({'runtime': name, 'available': avail})
    return found


def classify_vendor_skill(
    *,
    slug: str,
    name: str,
    description: str,
    text: str,
    size_bytes: int,
    tier: str,
    has_scripts_dir: bool,
) -> dict:
    """Deterministic zero-token static classifier assigning one of SKILL_CLASSES with explicit reasons."""
    reasons: list[str] = []
    norm_slug = slug.strip().lower()
    local_runtimes = _detect_local_runtimes(text, has_scripts_dir)

    cli_match = _CLI_INTERNAL_RE.search(text)
    blocked_match = _BLOCKED_CAPABILITY_RE.search(text)
    foreign_match = _FOREIGN_HARNESS_RE.search(text)

    if norm_slug in _CLI_INTERNAL_SLUGS:
        reasons.append(f'known_cli_internal_skill:{norm_slug}')
        return {
            'classification': 'cli_internal',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': False,
        }

    if blocked_match:
        reasons.append(f'requires_blocked_external_api:{blocked_match.group(0)}')
        return {
            'classification': 'blocked_capability',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': False,
        }

    if tier in ('builtin', 'system') and cli_match:
        reasons.append(f'{tier}_skill_references_cli_internals:{cli_match.group(0).lower()}')
        return {
            'classification': 'cli_internal',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': False,
        }

    if cli_match:
        reasons.append(f'references_vendor_cli_internals:{cli_match.group(0).lower()}')
        return {
            'classification': 'cli_internal',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': False,
        }

    if size_bytes > _managed.MAX_SKILL_TEXT_BYTES:
        reasons.append(f'exceeds_64kib_limit:{size_bytes}_bytes')
        if foreign_match:
            reasons.append(f'foreign_harness_cue:{foreign_match.group(0).lower()}')
        return {
            'classification': 'needs_sanitization',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': True,
        }

    if foreign_match:
        reasons.append(f'foreign_harness_cue:{foreign_match.group(0).lower()}')
        return {
            'classification': 'needs_sanitization',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': True,
        }

    if has_scripts_dir or local_runtimes or _OFFICE_DOC_RE.search(text):
        if has_scripts_dir:
            reasons.append('bundle_has_scripts_dir')
        for rt in local_runtimes:
            if rt['runtime'] != 'scripts/':
                reasons.append(f"references_local_runtime:{rt['runtime']}")
        if not reasons:
            reasons.append('references_local_document_tooling')
        return {
            'classification': 'local_tool_hybrid',
            'reasons': reasons,
            'local_runtimes': local_runtimes,
            'visible_by_default': True,
        }

    reasons.append('portable_markdown_guidance_only')
    return {
        'classification': 'portable_ready',
        'reasons': reasons,
        'local_runtimes': [],
        'visible_by_default': True,
    }


def suggest_roles(*, slug: str, name: str, description: str, classification: str) -> list[str]:
    """Suggest sensible default roles strictly within DEFAULT_ENABLED_ROLES in canonical SKILL_ROLES order."""
    if classification in HIDDEN_CLASSES:
        return []
    haystack = f'{slug} {name} {description}'
    if _REVIEW_ROLE_RE.search(slug) or (_REVIEW_ROLE_RE.search(haystack) and not _IMPL_ROLE_RE.search(slug)):
        return ['plan-auditor', 'auditor']
    if _PLAN_ROLE_RE.search(slug):
        return ['coordinator', 'planner', 'plan-auditor']
    if classification == 'local_tool_hybrid' or _IMPL_ROLE_RE.search(slug):
        return ['implementer', 'planner', 'auditor']
    return list(_managed.DEFAULT_ENABLED_ROLES)


def _parse_frontmatter(text: str, fallback_name: str) -> tuple[str, str]:
    """Parse YAML frontmatter name and description (including folded > / | blocks) without executing anything."""
    name, description = fallback_name, ''
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return name, description
    end_idx = None
    for i in range(1, min(len(lines), 80)):
        if lines[i].strip() == '---':
            end_idx = i
            break
    if end_idx is None:
        return name, description
    i = 1
    while i < end_idx:
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith('name:'):
            val = stripped.split(':', 1)[1].strip().strip('"\'')
            if val:
                name = val[:120]
        elif stripped.startswith('description:'):
            val = stripped.split(':', 1)[1].strip()
            if val in ('>', '>-', '|', '|-'):
                parts = []
                i += 1
                while i < end_idx and (lines[i].startswith(' ') or lines[i].startswith('\t') or not lines[i].strip()):
                    if lines[i].strip():
                        parts.append(lines[i].strip())
                    i += 1
                description = ' '.join(parts)[:300]
                continue
            description = val.strip('"\'')[:300]
        i += 1
    return name, description


def _unified_diff(before: str, after: str, from_label: str, to_label: str) -> str:
    lines = list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=from_label,
        tofile=to_label,
        lineterm='',
    ))
    return '\n'.join(lines[:400])


def check_origin_drift(
    origin_path: str | None,
    base_sha256: str,
    base_text: str,
    *,
    base_git_head: str | None = None,
) -> dict:
    """Safely check whether a managed skill's local origin SKILL.md or Git HEAD changed on disk (zero execution, no link traversal)."""
    if not origin_path:
        return {'available': False, 'status': 'no_origin'}
    root = Path(origin_path)
    if '..' in root.parts or _is_unc_or_network(root):
        return {'available': False, 'status': 'origin_refused'}
    try:
        if not root.exists() or _bundles._is_link_or_reparse(root) or not root.is_dir():
            return {'available': False, 'status': 'origin_unavailable'}
        skill_md = root / 'SKILL.md'
        if not skill_md.exists() or _bundles._is_link_or_reparse(skill_md):
            return {'available': False, 'status': 'origin_unavailable'}
        raw, _ = _bundles._read_pinned_bytes(skill_md, _managed.MAX_SKILL_TEXT_BYTES)
        if raw is None:
            return {'available': False, 'status': 'origin_exceeds_limit'}
        live_text = raw.decode('utf-8', errors='strict')
    except (OSError, ValueError, UnicodeError):
        return {'available': False, 'status': 'origin_unavailable'}

    current_sha = hashlib.sha256(live_text.encode('utf-8')).hexdigest()
    git_info = _bundles._git_origin_info(root) if (base_git_head or (root / '.git').exists()) else {}
    current_head = git_info.get('origin_git_head')
    current_remote = git_info.get('origin_git_remote')
    git_meta = {
        **({'base_git_head': base_git_head} if base_git_head else {}),
        **({'current_git_head': current_head} if current_head else {}),
        **({'origin_git_remote': current_remote} if current_remote else {}),
    }
    if current_sha != base_sha256:
        return {
            'available': True,
            'status': 'changed',
            'current_sha256': current_sha,
            'skill_md_diff': _unified_diff(base_text, live_text, 'base (frozen)', 'origin (live)'),
            **git_meta,
        }
    if base_git_head and current_head and current_head != base_git_head:
        return {
            'available': True,
            'status': 'git_head_changed',
            'current_sha256': current_sha,
            **git_meta,
        }
    return {'available': False, 'status': 'in_sync', 'current_sha256': current_sha, **git_meta}


def _scan_source(source: dict) -> list[dict]:
    root = source['path']
    if not root.is_dir():
        return []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    found: list[dict] = []
    for child in children:
        if child.name.startswith('.'):
            continue
        resolved = _resolve_vendor_skill_root(child)
        if resolved is None:
            continue
        capture_root, linked_root = resolved
        skill_md = capture_root / 'SKILL.md'
        try:
            if not skill_md.exists() or _bundles._is_link_or_reparse(skill_md):
                continue
            st = skill_md.stat()
            size_bytes = int(st.st_size)
            raw, _ = _bundles._read_pinned_bytes(skill_md, MAX_INSPECT_SKILL_MD_BYTES)
        except (OSError, ValueError):
            continue
        if raw is None:
            # Larger than 256 KiB: still report honestly as needs_sanitization without reading unbounded bytes.
            text = ''
            sha256 = hashlib.sha256(f'oversized:{capture_root}:{size_bytes}'.encode('utf-8')).hexdigest()
            skill_name, description = child.name, f'Oversized SKILL.md ({size_bytes} bytes)'
        else:
            try:
                text = raw.decode('utf-8', errors='strict')
            except UnicodeError:
                continue
            sha256 = hashlib.sha256(raw).hexdigest()
            skill_name, description = _parse_frontmatter(text, child.name)
        has_scripts = (capture_root / 'scripts').is_dir() and not _bundles._is_link_or_reparse(capture_root / 'scripts')
        slug = None
        for candidate_slug in (child.name, skill_name):
            try:
                slug = _bundles.normalize_bundle_id(candidate_slug)
                break
            except ValueError:
                continue
        if slug is None:
            slug = f'skill-{sha256[:8]}'
        cls = classify_vendor_skill(
            slug=slug,
            name=skill_name,
            description=description,
            text=text,
            size_bytes=size_bytes,
            tier=source['tier'],
            has_scripts_dir=has_scripts,
        )
        found.append({
            'slug': slug,
            'skill_name': skill_name,
            'description': description,
            'skill_md_sha256': sha256,
            'size_bytes': size_bytes,
            'capture_root': str(capture_root),
            'vendor': source['vendor'],
            'tier': source['tier'],
            'source_id': source['source_id'],
            'declared_path': str(child),
            'linked_root': linked_root,
            'has_scripts_dir': has_scripts,
            **cls,
        })
    return found


def discover_vendor_catalog(
    *,
    home=None,
    vendor: str | None = None,
    include_hidden: bool = False,
    classification: str | None = None,
    storage_root=None,
) -> dict:
    """Discover, classify, and SHA-256-deduplicate local Claude/Codex/AGY skills (read-only, zero execution)."""
    if vendor is not None and vendor not in VENDOR_ORDER:
        raise ValueError(f'Unknown vendor {vendor!r}; use one of: {", ".join(VENDOR_ORDER)}')
    if classification is not None and classification not in SKILL_CLASSES:
        raise ValueError(f'Unknown classification {classification!r}; use one of: {", ".join(SKILL_CLASSES)}')

    sources = default_vendor_sources(home=home)
    if vendor is not None:
        sources = [s for s in sources if s['vendor'] == vendor]

    raw_items: list[dict] = []
    for src in sources:
        raw_items.extend(_scan_source(src))

    # Deduplicate identical skills across vendors/locations by SKILL.md SHA-256.
    by_hash: dict[str, list[dict]] = {}
    for item in raw_items:
        by_hash.setdefault(item['skill_md_sha256'], []).append(item)

    # Cross-reference with existing managed skills by canonical origin_path only (never conflate
    # different-content skills that happen to share a folder name across vendors).
    store = _managed.load_store(storage_root=storage_root)
    managed_by_origin: dict[str, dict] = {}
    for sid, entry in store['skills'].items():
        if entry.get('origin_path'):
            managed_by_origin[os.path.normcase(str(Path(entry['origin_path']).absolute()))] = entry

    all_deduped: list[dict] = []
    for sha, group in by_hash.items():
        group.sort(key=lambda r: (
            TIER_ORDER.get(r['tier'], 9),
            VENDOR_ORDER.get(r['vendor'], 9),
            1 if r['linked_root'] else 0,
            r['declared_path'].lower(),
        ))
        primary = group[0]
        # Prefer the strictest/most informative classification if a skill appears in both user & system dirs,
        # except when a user copy exists: primary (lowest tier rank) decides user vs system context.
        vendors = sorted({g['vendor'] for g in group}, key=lambda v: VENDOR_ORDER.get(v, 9))
        source_records = [
            {
                'vendor': g['vendor'],
                'tier': g['tier'],
                'source_id': g['source_id'],
                'declared_path': g['declared_path'],
                'capture_root': g['capture_root'],
                'linked_root': g['linked_root'],
            }
            for g in group
        ]
        suggested = suggest_roles(
            slug=primary['slug'],
            name=primary['skill_name'],
            description=primary['description'],
            classification=primary['classification'],
        )
        matched_entry = None
        for g in group:
            norm_root = os.path.normcase(str(Path(g['capture_root']).absolute()))
            if norm_root in managed_by_origin:
                matched_entry = managed_by_origin[norm_root]
                break

        if matched_entry is None:
            sync_status = 'not_imported'
            managed_skill_id = None
        else:
            managed_skill_id = matched_entry['skill_id']
            sync_status = 'in_sync' if matched_entry.get('base_sha256') == sha else 'vendor_changed'

        all_deduped.append({
            'catalog_id': f"{primary['slug']}@{sha[:8]}",
            'slug': primary['slug'],
            'skill_name': primary['skill_name'],
            'description': primary['description'],
            'skill_md_sha256': sha,
            'size_bytes': primary['size_bytes'],
            'capture_root': primary['capture_root'],
            'linked_root': primary['linked_root'],
            'primary_vendor': primary['vendor'],
            'vendors': vendors,
            'tier': primary['tier'],
            'sources': source_records,
            'has_scripts_dir': primary['has_scripts_dir'],
            'classification': primary['classification'],
            'reasons': list(primary['reasons']),
            'local_runtimes': list(primary['local_runtimes']),
            'visible_by_default': primary['visible_by_default'],
            'suggested_roles': suggested,
            'managed_skill_id': managed_skill_id,
            'sync_status': sync_status,
            'authority': 'NONE',
            'effects_authorized': False,
        })

    all_deduped.sort(key=lambda r: (
        CLASS_ORDER.get(r['classification'], 9),
        TIER_ORDER.get(r['tier'], 9),
        r['slug'],
        r['catalog_id'],
    ))

    counts_by_class = {
        cls: sum(1 for r in all_deduped if r['classification'] == cls)
        for cls in SKILL_CLASSES
    }
    hidden_count = sum(1 for r in all_deduped if not r['visible_by_default'])

    filtered = []
    for item in all_deduped:
        if classification is not None and item['classification'] != classification:
            continue
        if not include_hidden and classification is None and not item['visible_by_default']:
            continue
        filtered.append(item)

    return {
        'schema': CATALOG_SCHEMA,
        'authority': 'NONE',
        'effects_authorized': False,
        'total_raw_discovered': len(raw_items),
        'total_deduped': len(all_deduped),
        'hidden_count': hidden_count,
        'counts_by_class': counts_by_class,
        'items': filtered,
    }


def preview_vendor_item(item: dict) -> str:
    """Inert preview text for one discovered vendor catalog item."""
    vendors_str = ' + '.join(VENDOR_LABELS.get(v, v) for v in item['vendors'])
    roles_str = ', '.join(item['suggested_roles']) if item['suggested_roles'] else '(none — hidden by default)'
    lines = [
        f"Vendor skill: {item['slug']} ({item['skill_name']})",
        f"Vendors: {vendors_str}  Tier: {item['tier']}  Status: {item['sync_status']}",
        f"Classification: {item['classification']} ({', '.join(item['reasons'])})",
        f"Suggested roles: {roles_str}",
        f"Capture root: {item['capture_root']}" + (' (resolved from linked vendor root)' if item['linked_root'] else ''),
        f"SKILL.md: {item['size_bytes']} B  SHA-256: {item['skill_md_sha256']}",
        'Authority: NONE  Effects authorized: False  (native vendor CLI skill loading stays off)',
    ]
    if item.get('local_runtimes'):
        rt_str = ', '.join(
            f"{rt['runtime']} ({'found' if rt['available'] else 'missing'})"
            for rt in item['local_runtimes']
        )
        lines.append(f'Local runtimes referenced: {rt_str}')
    if len(item['sources']) > 1:
        lines.append('Deduplicated across:')
        for src in item['sources']:
            lines.append(f"  - [{src['source_id']}] {src['declared_path']}")

    # Read inert SKILL.md body up to 64 KiB for preview.
    skill_md = Path(item['capture_root']) / 'SKILL.md'
    try:
        raw, _ = _bundles._read_pinned_bytes(skill_md, _managed.MAX_SKILL_TEXT_BYTES)
        if raw is None:
            lines.append('\n[SKILL.md exceeds 64 KiB managed limit; sanitize or trim before adopting.]')
        else:
            lines.append('\n--- SKILL.md (inert preview) ---\n' + raw.decode('utf-8', errors='replace'))
    except (OSError, ValueError) as exc:
        lines.append(f'\n[Preview unavailable: {exc}]')
    return '\n'.join(lines)


def import_vendor_skill(
    target: str | dict,
    *,
    vendor: str | None = None,
    skill_id: str | None = None,
    allowed_roles=None,
    min_model_class: str | None = None,
    skip_plan_review: bool | None = None,
    allow_hidden: bool = False,
    home=None,
    storage_root=None,
) -> dict:
    """Capture a discovered vendor skill inertly and adopt it as a tare managed skill in one step."""
    if isinstance(target, dict):
        item = target
    else:
        catalog = discover_vendor_catalog(
            home=home, vendor=vendor, include_hidden=allow_hidden, storage_root=storage_root,
        )
        matches = [
            it for it in catalog['items']
            if it['catalog_id'] == target or it['slug'] == target
        ]
        if not matches:
            hint = '' if allow_hidden else ' (pass --all to inspect hidden cli_internal/blocked_capability skills)'
            raise ValueError(f'Vendor skill {target!r} not found in discovered catalog{hint}')
        if len(matches) > 1:
            ids = ', '.join(m['catalog_id'] for m in matches)
            raise ValueError(f'Ambiguous vendor skill {target!r}; specify one of: {ids}')
        item = matches[0]

    if item['size_bytes'] > _managed.MAX_SKILL_TEXT_BYTES:
        raise ValueError(
            f"Vendor skill {item['slug']!r} SKILL.md is {item['size_bytes']} bytes "
            f"(exceeds {_managed.MAX_SKILL_TEXT_BYTES} byte managed limit); trim or sanitize it first"
        )

    target_sid = skill_id
    if target_sid is None:
        store = _managed.load_store(storage_root=storage_root)
        existing = store['skills'].get(item['slug'])
        if existing is not None and os.path.normcase(str(Path(existing.get('origin_path') or '').absolute())) != os.path.normcase(str(Path(item['capture_root']).absolute())):
            target_sid = f"{item['slug']}-{item['primary_vendor']}"
        else:
            target_sid = item['slug']
    captured = _bundles.capture_bundle(
        Path(item['capture_root']),
        name=target_sid,
        storage_root=storage_root,
    )
    roles = allowed_roles if allowed_roles is not None else (
        item['suggested_roles'] or list(_managed.DEFAULT_ENABLED_ROLES)
    )
    return _managed.adopt_snapshot(
        captured['bundle_id'],
        captured['snapshot_id'],
        skill_id=target_sid,
        allowed_roles=roles,
        min_model_class=min_model_class,
        skip_plan_review=skip_plan_review,
        origin_vendor=item['primary_vendor'],
        origin_vendors=list(item['vendors']),
        storage_root=storage_root,
    )


def sync_managed_from_origin(
    skill_id: str,
    *,
    keep_overlay: bool = False,
    storage_root=None,
) -> dict:
    """Re-capture a managed skill's local origin_path into a verified inert snapshot and update the managed skill."""
    view = _managed.effective_view(skill_id, storage_root=storage_root)
    origin_path = view['origin'].get('origin_path')
    if not origin_path:
        raise ValueError(f'Managed skill {skill_id!r} has no recorded origin_path')
    root = Path(origin_path)
    if '..' in root.parts or _is_unc_or_network(root):
        raise ValueError(f'Origin path for {skill_id!r} is not a local directory')
    captured = _bundles.capture_bundle(
        root,
        name=view['origin']['bundle_id'],
        storage_root=storage_root,
    )
    return _managed.apply_upstream_update(
        skill_id,
        captured['snapshot_id'],
        keep_overlay=keep_overlay,
        storage_root=storage_root,
    )


def format_catalog(catalog: dict) -> str:
    """Human-readable summary of discovered local vendor skills."""
    items = catalog['items']
    lines = [
        f"Vendor skill catalog: {len(items)} shown / {catalog['total_deduped']} unique "
        f"({catalog['total_raw_discovered']} raw across Claude/Codex/AGY; {catalog['hidden_count']} auto-hidden)",
    ]
    if not items:
        lines.append('No actionable vendor skills found.')
    else:
        for it in items:
            vtag = '+'.join(it['vendors'])
            cls_tag = CLASS_LABELS.get(it['classification'], it['classification'])
            roles_tag = _managed.format_role_tags(it['suggested_roles']) if it['suggested_roles'] else 'none'
            sync = '' if it['sync_status'] == 'not_imported' else f" [{it['sync_status']}]"
            lines.append(
                f"  - {it['slug']:<26} [{cls_tag:<11} | {vtag:<12} | roles={roles_tag}]{sync}  {it['skill_name']}"
            )
    if catalog['hidden_count'] and all(it['visible_by_default'] for it in items):
        lines.append(
            f"Auto-hidden {catalog['hidden_count']} vendor-specific/blocked skill(s) "
            f"(cli_internal={catalog['counts_by_class']['cli_internal']}, "
            f"blocked_capability={catalog['counts_by_class']['blocked_capability']}). Pass --all to inspect."
        )
    return '\n'.join(lines)
