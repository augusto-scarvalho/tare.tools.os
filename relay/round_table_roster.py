#!/usr/bin/env python3
"""Validate the Round Table roster SSOT and project it as Markdown matrices."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import relay_io
except ImportError:  # Direct script execution.
    import relay_io

relay_io.utf8_console()
DEFAULT_CONFIG = Path(__file__).with_name("round_table_roster.json")
ROLE_DEPLOYMENTS = Path(__file__).with_name("role_deployments.json")
AGENT_ROLES = ("planner", "implementer", "plan-auditor", "auditor", "explorer", "researcher")
EFFORTS_BY_RUNNER = {
    "codex": {"low", "medium", "high", "xhigh"},
    "claude": {"low", "medium", "high"},
    "google-cli": {"low", "medium", "high"},
    "aider": {"low", "medium", "high"},
    "aider-bedrock": {"low", "medium", "high"},
    "aider-openrouter": {"low", "medium", "high"},
    "kimi-cli": set(),
    "native-http": set(),
}


def local(text: str) -> str:
    """Shipped local entries name the gateway placeholder; each machine resolves it to its own model gateway
    (tare setup --local-gateway), as bundled profiles do."""
    try:
        from .native_catalog import LOCAL_GATEWAY_PLACEHOLDER, local_gateway
    except ImportError:  # direct script execution only validates and renders, which never reads an endpoint
        return text
    return text.replace(LOCAL_GATEWAY_PLACEHOLDER, local_gateway()) if LOCAL_GATEWAY_PLACEHOLDER in text else text


def load_roster(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    data = json.loads(local(path.read_text(encoding="utf-8")))
    validate_roster(data)
    return data


def _all_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    chairs = data["chairs"]
    return chairs["fixed"] + chairs["fallbacks"] + data["mediators"] + data["scribes"]


def bias_domain(data: dict[str, Any], entry: dict[str, Any]) -> str:
    return data["models"][entry["model"]]["bias_domain"]


def runtime_model(data: dict[str, Any], entry: dict[str, Any]) -> str:
    try:
        return data["models"][entry["model"]]["runtime"][entry["runner"]]
    except KeyError as exc:
        raise ValueError(f"model {entry['model']} has no runtime id for {entry['runner']}") from exc


def attestation_capability(entry: dict[str, Any]) -> str:
    if entry["runner"] == "native-http":
        return ("request_pinned_unverified" if entry.get("native_profile", {}).get("protocol") == "bedrock-converse"
                else "runtime_reported")
    if entry["runner"] == "codex":
        return "runtime_reported"
    if entry["runner"] in ("aider-bedrock", "aider-openrouter"):
        return "request_pinned_unverified"
    return "UNATTESTED"


def validate_native_deployment(entry, requested_model):
    try:
        from .native_generation import deployment_profile
    except ImportError:
        from native_generation import deployment_profile
    profile = deployment_profile(entry, requested_model)
    if entry.get("provider") == "Amazon Bedrock" or profile.protocol == "bedrock-converse":
        raise ValueError(f"deployment {entry['id']}: Bedrock is not a Round Table transport; use role_deployments.json")


def validate_roster(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("unsupported round-table roster schema_version")

    models = data.get("models")
    policy = data.get("policy")
    chairs = data.get("chairs")
    if not isinstance(models, dict) or not isinstance(policy, dict) or not isinstance(chairs, dict):
        raise ValueError("models, policy and chairs must be objects")

    fixed = chairs.get("fixed", [])
    if len(fixed) != policy.get("chair_count"):
        raise ValueError("fixed chair count does not match policy.chair_count")

    entries = _all_entries(data)
    ids = [entry.get("id") for entry in entries]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("every role entry must have a unique non-empty id")
    if any(entry.get("model") not in models for entry in entries):
        raise ValueError("every role entry must reference a registered model")
    for entry in entries:
        runtime_model(data, entry)
        if not entry.get("quota_domain") or not entry.get("endpoint_domain"):
            raise ValueError(f"deployment {entry['id']} must define quota_domain and endpoint_domain")
        runner = entry.get("runner")
        if runner not in EFFORTS_BY_RUNNER:
            raise ValueError(f"deployment {entry['id']} has unsupported runner {runner}")
        effort = entry.get("effort")
        if effort is not None and effort not in EFFORTS_BY_RUNNER[runner]:
            raise ValueError(f"deployment {entry['id']} has unsupported effort {effort!r} for runner {runner}")
        if runner == "aider-bedrock":
            raise ValueError(f"deployment {entry['id']}: Bedrock is not a Round Table transport; use role_deployments.json")
        if runner == "native-http":
            validate_native_deployment(entry, runtime_model(data, entry))
        if runner == "aider-openrouter" and not runtime_model(data, entry).startswith("openrouter/"):
            raise ValueError(f"deployment {entry['id']} must pin an OpenRouter model")

    for section in (fixed, chairs.get("fallbacks", []), data.get("mediators", []), data.get("scribes", [])):
        priorities = [entry.get("priority") for entry in section]
        if priorities != sorted(priorities) or len(priorities) != len(set(priorities)):
            raise ValueError("priorities must be unique and ascending inside each role section")

    fixed_domains = [models[entry["model"]]["bias_domain"] for entry in fixed]
    if policy.get("unique_bias_domains_across_chairs") and len(fixed_domains) != len(set(fixed_domains)):
        raise ValueError("fixed chairs contain duplicate bias domains")

    expected_external_attestation = {
        "chair_vote": "allow_with_explicit_receipt_status",
        "llm_mediator": "allow_only_with_operator_opt_in",
        "full_attestation_claim": "forbidden_without_runtime_evidence",
        "runtime_mismatch": "fail_closed",
    }
    if policy.get("external_model_attestation") != expected_external_attestation:
        raise ValueError("external_model_attestation policy must remain explicit and fail closed")
    if policy.get("work_author_conflict") != "explicit_case_bias_domain_exclusion":
        raise ValueError("work_author_conflict must require explicit case bias-domain exclusion")

    fixed_by_id = {entry["id"]: entry for entry in fixed}
    for fallback in chairs.get("fallbacks", []):
        if fallback.get("kind") != "transport":
            continue
        replaced = fixed_by_id.get(fallback.get("replaces"))
        if not replaced or replaced["model"] != fallback["model"]:
            raise ValueError("transport fallback must replace the same model")

    trigger = policy.get("red_team_trigger", {})
    if trigger.get("classifier") != "deterministic_casefold_substring" or not trigger.get("signals"):
        raise ValueError("red_team_trigger must define deterministic non-empty signals")

    for model_id, model in models.items():
        record = model.get("bedrock")
        if record is not None:
            if (record.get("access") not in BEDROCK_ACCESS or not isinstance(record.get("model_id"), str)
                    or not record.get("region") or not record.get("checked") or not record.get("source")):
                raise ValueError(f"model {model_id} has an invalid bedrock catalog record")
            runtime_id = model.get("runtime", {}).get("aider-bedrock")
            if record["access"] == "verified" and runtime_id != "bedrock/" + record["model_id"]:
                raise ValueError(f"model {model_id} verified on Bedrock must expose runtime aider-bedrock = bedrock/{record['model_id']}")
        provenance = model.get("provenance", {})
        for independent_id in provenance.get("independent_from", []):
            if independent_id not in models:
                raise ValueError(f"model {model_id} has unknown independent_from reference {independent_id}")
            if model["bias_domain"] == models[independent_id]["bias_domain"]:
                raise ValueError(f"model {model_id} attests independence but shares bias domain with {independent_id}")


BEDROCK_ACCESS = {"verified", "denied", "provisioned_only"}


def load_role_deployments(path: Path = ROLE_DEPLOYMENTS, roster: dict[str, Any] | None = None) -> dict[str, Any]:
    """Native HTTP and vendor CLI roles, separate from Round Table chairs."""
    roster = roster or load_roster()
    data = json.loads(local(path.read_text(encoding="utf-8")))
    validate_role_deployments(data, roster)
    return data


def validate_role_deployments(data: dict[str, Any], roster: dict[str, Any]) -> None:
    if data.get("schema_version") != 1 or not isinstance(data.get("deployments"), list):
        raise ValueError("unsupported role deployments schema")
    ids = [entry.get("id") for entry in data["deployments"]]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("every role deployment must have a unique non-empty id")
    for entry in data["deployments"]:
        if entry.get("model") not in roster["models"]:
            raise ValueError(f"role deployment {entry['id']} references an unregistered model")
        runtime_model(roster, entry)
        runner = entry.get("runner")
        if runner not in EFFORTS_BY_RUNNER:
            raise ValueError(f"role deployment {entry['id']} has unsupported runner {runner}")
        if entry.get("effort") is not None and entry["effort"] not in EFFORTS_BY_RUNNER[runner]:
            raise ValueError(f"role deployment {entry['id']} has unsupported effort for {runner}")
        if not entry.get("quota_domain") or not entry.get("endpoint_domain"):
            raise ValueError(f"role deployment {entry['id']} must define quota_domain and endpoint_domain")
        roles = entry.get("roles")
        if not roles or not set(roles) <= set(AGENT_ROLES):
            raise ValueError(f"role deployment {entry['id']} must list roles from {AGENT_ROLES}")
        if entry.get("kind") not in {"primary", "reserve"}:
            raise ValueError(f"role deployment {entry['id']} kind must be primary or reserve")
        if entry.get("cost_tier") not in {"free", "flat", "free-credit", "metered"}:
            raise ValueError(f"role deployment {entry['id']} cost_tier must be free, flat, free-credit or metered")
        if type(entry.get('qualified', True)) is not bool:
            raise ValueError(f"role deployment {entry['id']} qualified must be true or false")
        priority = entry.get('review_priority', 1000)
        if type(priority) is not int or not 0 <= priority <= 1000:
            raise ValueError('REVIEW_PRIORITY_INVALID')
        if runner == 'native-http':
            from relay.native_generation import deployment_profile
            if entry.get('native_catalog') and 'native_profile' not in entry:
                # Portable role policy references the same host profile as the TUI.
                # Resolve and validate its contents at selection, not config load.
                import re
                if not isinstance(entry['native_catalog'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', entry['native_catalog']):
                    raise ValueError('ROLE_CATALOG_NAME_INVALID')
            else:
                deployment_profile(entry, runtime_model(roster, entry))
        if entry.get('provider') == 'Amazon Bedrock':
            if not entry.get("region"):
                raise ValueError(f"role deployment {entry['id']} must pin an AWS region")
            if (roster["models"][entry["model"]].get("bedrock") or {}).get("access") != "verified":
                raise ValueError(f"role deployment {entry['id']} uses a Bedrock model not verified for this account")


def render_role_deployments(roster: dict[str, Any], data: dict[str, Any]) -> str:
    lines = [
        "# Native deployments and CLIs by role",
        "",
        "> Generated from `relay/role_deployments.json`; separate from the Round Table. Models come from the roster catalog.",
        "",
        "| ID | Model | Vendor | Provider | Runner | Effort | Roles | Kind | Cost | Quota | Endpoint |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in data["deployments"]:
        model = roster["models"][entry["model"]]
        lines.append(
            f"| `{_cell(entry['id'])}` | {_cell(model['display_name'])} | {_cell(model['vendor'])} | "
            f"{_cell(entry['provider'])} | {_cell(entry['runner'])} | {_cell(entry.get('effort'))} | "
            f"{_cell(', '.join(entry['roles']))} | {_cell(entry['kind'])} | {_cell(entry['cost_tier'])} | {_cell(entry['quota_domain'])} | "
            f"{_cell(entry['endpoint_domain'])} |"
        )
    return "\n".join(lines) + "\n"


def bedrock_catalog(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Internal Bedrock catalog projected from roster models; the roster is the SSOT."""
    return [{"id": model_id, "display_name": model["display_name"], "vendor": model["vendor"],
             "bias_domain": model["bias_domain"], **model["bedrock"],
             "runtime": model.get("runtime", {}).get("aider-bedrock")}
            for model_id, model in data["models"].items() if "bedrock" in model]


def eligible_mediators(data: dict[str, Any], chairs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    chairs = chairs or data["chairs"]["fixed"]
    models = data["models"]
    occupied = {models[chair["model"]]["bias_domain"] for chair in chairs}
    return [
        mediator
        for mediator in data["mediators"]
        if models[mediator["model"]]["bias_domain"] not in occupied
    ]


def chair_fallbacks(data: dict[str, Any], failed: dict[str, Any]) -> list[dict[str, Any]]:
    fallbacks = data["chairs"]["fallbacks"]
    transport = [entry for entry in fallbacks if entry.get("replaces") == failed["id"]]
    return transport + [entry for entry in fallbacks if entry.get("kind") != "transport"]


def eligible_scribes(data: dict[str, Any], mediator: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not mediator:
        return list(data["scribes"])
    occupied = bias_domain(data, mediator)
    return [entry for entry in data["scribes"] if bias_domain(data, entry) != occupied]


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(value: Any) -> str:
    return _value(value).replace("|", "\\|").replace("\n", " ")


def _role_table(title: str, entries: list[dict[str, Any]], models: dict[str, Any]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Priority | ID | Model | Vendor | Provider | Transport | Runner | Effort | Quota | Endpoint | Attestation | Kind |",
        "|---:|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in entries:
        model = models[entry["model"]]
        lines.append(
            f"| {entry['priority']} | `{_cell(entry['id'])}` | {_cell(model['display_name'])} | "
            f"{_cell(model['vendor'])} | {_cell(entry['provider'])} | {_cell(entry['transport'])} | "
            f"{_cell(entry['runner'])} | {_cell(entry.get('effort'))} | {_cell(entry['quota_domain'])} | "
            f"{_cell(entry['endpoint_domain'])} | {_cell(attestation_capability(entry))} | "
            f"{_cell(entry.get('kind', 'fixed'))} |"
        )
    return lines


def render_markdown(data: dict[str, Any]) -> str:
    models = data["models"]
    fixed = data["chairs"]["fixed"]
    occupied = {models[chair["model"]]["bias_domain"]: chair["id"] for chair in fixed}
    lines = [
        "# Round Table matrix projection",
        "",
        "> Generated from `relay/round_table_roster.json`; do not edit this projection by hand.",
        "",
        "## Policy",
        "",
        "| Rule | Value |",
        "|---|---|",
    ]
    lines.extend(f"| `{key}` | `{_value(value)}` |" for key, value in data["policy"].items())
    lines.extend([""] + _role_table("Fixed chairs", fixed, models))
    lines.extend([""] + _role_table("Chair fallbacks", data["chairs"]["fallbacks"], models))

    lines.extend([
        "",
        "## Mediators against the fixed chairs",
        "",
        "| Priority | ID | Model | Provider | Effort | Quota | Endpoint | Attestation | Compatibility |",
        "|---:|---|---|---|---|---|---|---|---|",
    ])
    for mediator in data["mediators"]:
        model = models[mediator["model"]]
        conflict = occupied.get(model["bias_domain"])
        status = f"BLOCKED by `{conflict}`" if conflict else "ELIGIBLE"
        lines.append(
            f"| {mediator['priority']} | `{_cell(mediator['id'])}` | {_cell(model['display_name'])} | "
            f"{_cell(mediator['provider'])} | {_cell(mediator.get('effort'))} | {_cell(mediator['quota_domain'])} | "
            f"{_cell(mediator['endpoint_domain'])} | {_cell(attestation_capability(mediator))} | {_cell(status)} |"
        )

    eligible = eligible_mediators(data, fixed)
    lines.extend([
        "",
        "**Effective order with the fixed chairs:** " + " -> ".join(
            models[entry["model"]]["display_name"] for entry in eligible
        ),
        "",
    ] + _role_table("Scribes", data["scribes"], models))
    return "\n".join(lines) + "\n"


def render_bedrock_catalog(data: dict[str, Any]) -> str:
    lines = [
        "## Bedrock catalog for this account",
        "",
        "> Projected from the `bedrock` records of `relay/round_table_roster.json`. `access` reflects the last",
        "> check recorded in `source`, not a current guarantee; `denied` blocks the deployment.",
        "",
        "| ID | Model | Vendor | Bias domain | `modelId` | Region | Access | Checked | Fit |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in bedrock_catalog(data):
        lines.append(
            f"| `{_cell(item['id'])}` | {_cell(item['display_name'])} | {_cell(item['vendor'])} | "
            f"{_cell(item['bias_domain'])} | `{_cell(item['model_id'])}` | {_cell(item['region'])} | "
            f"{_cell(item['access'])} | {_cell(item['checked'])} | {_cell(item.get('fit'))}"
            f"{' — ' + _cell(item['reason']) if item.get('reason') else ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="validate without rendering")
    parser.add_argument("--bedrock", action="store_true", help="render only the Bedrock catalog")
    parser.add_argument("--roles", action="store_true", help="render the active role deployments")
    parser.add_argument("--json", action="store_true", help="with --bedrock/--roles: print JSON")
    args = parser.parse_args()
    data = load_roster(args.config)
    if args.roles:
        roles = load_role_deployments(roster=data)
        print(json.dumps(roles, ensure_ascii=False, indent=2) if args.json else render_role_deployments(data, roles), end="")
        return 0
    if args.bedrock:
        print(json.dumps(bedrock_catalog(data), ensure_ascii=False, indent=2) if args.json else render_bedrock_catalog(data), end="")
        return 0
    print("ROUND_TABLE_ROSTER = VALID" if args.check else render_markdown(data), end="\n" if args.check else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
