"""Read-only runner diagnostics from local configuration and selected receipts.

No provider calls, keyring unlock, journal replay, repair or task admission.
"""
from importlib import import_module, metadata, util
import hashlib
import json
from pathlib import Path
import re
from compute_plane.managed_process import host_executable
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version

# How to install ripgrep where no managed release brings it (a release carries its own in bundle/tools).
RIPGREP_INSTALL = {'win32': 'winget install BurntSushi.ripgrep.MSVC', 'darwin': 'brew install ripgrep',
                   'linux': 'sudo apt install ripgrep'}


MODULES = {'tare-tools-os': 'relay.native_agent', 'tare-kernel': 'compute_plane.native_models',
           'tare-tools-agent-runtime': 'tare_tools_agent_runtime',
           'tare-tools-specgraph': 'specgraph', 'tare-tools-backlog-graph': 'graph_backlog'}


def read_object(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('diagnostic_input_over_budget')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('diagnostic_object_required')
    return value, hashlib.sha256(raw).hexdigest()


def requirements():
    # Source execution must not inherit requirements from an older installed OS.
    source = Path(__file__).resolve().parents[1]/'pyproject.toml'
    if source.is_file():
        with source.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('project_metadata_over_budget')
        project = tomllib.loads(raw.decode())['project']
        if project['name'] != 'tare-tools-os':
            raise ValueError('unexpected_project_metadata')
        version, deps, basis = project['version'], project['dependencies'], 'source_pyproject'
    else:
        distribution = metadata.distribution('tare-tools-os')
        version, deps, basis = distribution.version, distribution.requires or [], 'installed_metadata'
    selected = {r.name: r for text in deps if (r := Requirement(text)).name in MODULES
                and (r.marker is None or r.marker.evaluate())}
    selected['tare-tools-os'] = Requirement('tare-tools-os==' + version)
    if set(selected) != set(MODULES):
        raise ValueError('native_dependency_requirements_incomplete')
    return selected, basis


def package(name, module, requirement):
    result = {'name': name, 'requirement': str(requirement)}
    try:
        distribution = metadata.distribution(name)
        result['version'] = distribution.version
        result['distribution_root'] = str(Path(distribution.locate_file('')).resolve())
        compatible = Version(distribution.version) in requirement.specifier
        loaded = import_module(module)
        location = Path(loaded.__file__).resolve()
        result['module'] = str(location)
        # File ownership is not an attestation of its bytes or a Git revision.
        relative = module.replace('.', '/')
        candidates = {relative+'.py', relative+'/__init__.py'}
        files = distribution.files
        matched = files is not None and any(
            str(f).replace('\\', '/') in candidates
            and Path(distribution.locate_file(f)).resolve() == location for f in files)
        result['import_origin'] = 'DISTRIBUTION_FILE_MATCH' if matched else 'NOT_CONFIRMED'
        result['status'] = ('VERSION_OUTSIDE_REQUIREMENT' if not compatible else
                            'AVAILABLE' if matched else 'IMPORT_ORIGIN_UNCONFIRMED')
    except Exception as error:
        result.update(status='UNAVAILABLE', error_type=type(error).__name__)
    return result


def profile(deployment):
    from .native_generation import deployment_profile
    from .native_spend import settings
    selected = deployment_profile(deployment, deployment['native_profile']['model'])
    policy, tariff = settings(deployment)
    counter = deployment.get('native_counter_protocol', 'server-template-tokenize')
    estimated = counter == 'utf8-envelope-estimate-v1'
    issues = []
    if estimated and policy and not policy.allow_estimated_counts:
        issues.append('SPEND_REQUIRES_MEASURED_COUNT_BUT_PROFILE_ESTIMATES')
    if policy:
        from compute_plane.native_usage import BILLABLE
        rates = json.loads(tariff.rates_json)
        fields = BILLABLE if selected.protocol == 'bedrock-converse' else (BILLABLE[0], BILLABLE[1], BILLABLE[3])
        if any(rates[field] is None for field in fields):
            issues.append('SPEND_TARIFF_INCOMPLETE')
    return {'status': 'ACTION_REQUIRED' if issues else 'CONFIGURATION_VALID',
        'model': selected.model, 'protocol': selected.protocol,
        'context_window': selected.context_window, 'output_tokens': selected.output_tokens,
        'streaming': deployment.get('native_streaming',False),
        'counter_protocol': counter, 'counting': 'ESTIMATED' if estimated else 'MEASURED_REQUIRED',
        'credentials': 'NOT_CHECKED' if deployment.get('native_credential_env') else 'NOT_CONFIGURED',
        'cache_system': selected.cache_system, 'tariff': 'CONFIGURED' if tariff else 'NOT_CONFIGURED',
        'spend_policy': 'CONFIGURED' if policy else 'NOT_CONFIGURED', 'issues': issues,
        'provider_readiness': 'NOT_PROBED', 'verification': 'static_profile_contract_only'}


def receipt(path):
    value, digest = read_object(path, 4*1024*1024)
    if value.get('execution') == 'native' and isinstance(value.get('outcome', value), dict):
        outcome = value.get('outcome', value)
        status, reason = outcome.get('status', '').upper(), outcome.get('reason', '')
        kind = 'native_session_or_attempt'
    elif isinstance(value.get('effective_profile'), dict) and value.get('attempt_id'):
        status = 'COMPLETED' if value.get('returncode') == 0 else 'FAILED'
        reason, kind = value.get('failure_class', ''), 'native_text'
    else:
        raise ValueError('unsupported_native_receipt')
    # Never echo raw provider errors, prompts, model answers, or tool output.
    code = reason if isinstance(reason, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', reason) else None
    category, action = 'UNDETERMINED', 'Inspect the original receipt; available fields do not establish a cause.'
    if code and code.startswith('SPEND_'):
        category, action = 'SPENDING_ADMISSION', 'Inspect reservations, tariff and count policy before explicitly starting another attempt.'
    elif code == 'HTTP_429':
        category, action = 'PROVIDER_THROTTLING', 'Review provider capacity for this route; this receipt does not establish the scope or duration of the limit.'
    elif code in {'AUTH_FAILED', 'HTTP_401', 'HTTP_403'}:
        category, action = 'PROVIDER_ACCESS', 'Check the selected credential and route permissions.'
    elif code in {'INVALID_PROVIDER_RESPONSE', 'INVALID_REPLY', 'SERVED_MODEL_MISMATCH', 'RESPONSE_INCOMPLETE', 'EMPTY_RESPONSE'}:
        category, action = 'RESPONSE_CONTRACT', 'Inspect the wire response and selected profile; model versus provider-parser causation is not established.'
    elif code and (code.startswith('CONTEXT_') or code.startswith('SUMMARY_')):
        category, action = 'CONTEXT_PREPARATION', 'Inspect the count or summary evidence and this model context policy.'
    elif status == 'RECONCILIATION_REQUIRED' or outcome_pending(value):
        category, action = 'EXECUTION_UNCERTAIN', 'Reconcile the pending invocation before another execution; remote cancellation is not established.'
    elif status in {'CANCELLED', 'INTERRUPTED'} or code in {'CONTROL_STOPPED', 'HTTP_NOT_DISPATCHED'}:
        category, action = 'EXECUTION_STOPPED', 'Inspect local cleanup and dispatch evidence; do not infer remote stop from local cancellation.'
    elif code and code.startswith('HTTP_'):
        category, action = 'PROVIDER_TRANSPORT', 'Inspect transport evidence and route configuration.'
    elif status == 'EXHAUSTED':
        category, action = 'BUDGET_EXHAUSTED_WITHOUT_DELIVERY', 'Inspect recorded calls and replies; exhaustion alone does not prove a model or code bug.'
    elif (status in {'PROPOSED', 'COMPLETED'} and not code
          or status == 'PROPOSED' and code == 'DELIVERY_PROPOSED'):
        category, action = 'RESPONSE_OBSERVED', 'Evaluate task criteria independently; a response or proposal is not acceptance or settlement.'
    elif code == 'MODEL_FAILURE':
        category, action = 'MODEL_INVOCATION_FAILED', 'Inspect the model-failed journal event; this outcome alone lacks the provider or adapter cause.'
    return {'status': 'CLASSIFIED', 'kind': kind, 'sha256': digest, 'reason_code': code,
        'category': category, 'next_action': action, 'task_correctness': 'NOT_EVALUATED',
        'evidence_scope': 'selected_receipt_only; no related files followed; no model-text interpretation',
        'dispatch': 'NOT_STARTED' if value.get('generation_not_started') is True else 'NOT_ESTABLISHED_BY_CLASSIFIER',
        'cost_coverage': 'NOT_EVALUATED; inspect the original cost/spend fields'}


def outcome_pending(value):
    return bool(value.get('outcome', value).get('pending_id'))


def doctor(*, deployment=None, deployment_name=None, receipt_path=None, root=None):
    from .native_installation import storage_root
    try:
        selected, basis = requirements()
        packages = [package(name, module, selected[name]) for name, module in MODULES.items()]
        dependency_status = 'CHECKED'
    except Exception as error:
        packages, basis, dependency_status = [], type(error).__name__, 'UNAVAILABLE'
    try:
        root = storage_root()
        storage = {'root': str(root), 'status': 'CONFIGURED' if root.is_dir() else 'DIRECTORY_MISSING',
                   'write_probe': 'NOT_PERFORMED'}
    except Exception as error:
        storage = {'status': 'NOT_CONFIGURED', 'error_type': type(error).__name__}
    result = {'schema': 'tare.tools/native-diagnostics/1', 'authority': 'NONE',
        'packages': packages, 'dependency_check': dependency_status, 'requirements_basis': basis,
        'version_evidence': 'discovered distribution metadata; may come from a checkout on sys.path',
        'storage': storage, 'active_realization': 'native', 'remote_providers': 'NOT_PROBED',
        'verification': 'local configuration and selected receipts; no source-byte attestation or task correctness',
        'profile': {'status': 'NOT_SELECTED'}, 'receipt': {'status': 'NOT_SELECTED'}}
    # The search tool runs ripgrep from PATH; without it every search fails and agents work blind.
    result['ripgrep'] = host_executable('rg') or ('NOT_ON_PATH: install ripgrep ('
        + RIPGREP_INSTALL.get(sys.platform, RIPGREP_INSTALL['linux']) + ')')
    try:
        result['aider_package_present'] = util.find_spec('aider') is not None
    except (ImportError, ValueError):
        result['aider_package_present'] = None
    for name, configured, inspect in (
        ('profile', deployment is not None or deployment_name is not None,
         lambda: profile(import_module('relay.native_catalog').select(deployment_name, deployment))),
        ('receipt', receipt_path is not None, lambda: receipt(receipt_path))):
        if configured:
            try:
                result[name] = inspect()
            except Exception as error:
                result[name] = {'status': 'UNAVAILABLE', 'error_type': type(error).__name__}
    if root is not None:  # what each way of working needs in this project; a report, not a health check
        try:
            from .work_forms import availability
            result['work_forms'] = availability(root)
        except Exception as error:
            result['work_forms'] = {'status': 'UNAVAILABLE', 'error_type': type(error).__name__}
    healthy = (dependency_status == 'CHECKED' and all(p['status'] == 'AVAILABLE' for p in packages)
               and storage['status'] == 'CONFIGURED' and not result['ripgrep'].startswith('NOT_ON_PATH')
               and result['profile']['status'] in {'NOT_SELECTED', 'CONFIGURATION_VALID'}
               and result['receipt']['status'] in {'NOT_SELECTED', 'CLASSIFIED'})
    result['operational_health'] = 'CHECKS_PASSED' if healthy else 'ACTION_REQUIRED'
    return result
