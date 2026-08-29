"""Strict loader for the current Git-owned Flow contract schema."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.flow.model import (
    AdapterSelection,
    ArtifactBinding,
    CatalogSelection,
    ExecutionProfile,
    FlowCatalog,
    FlowCatalogEntry,
    FlowContractError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    PolicyCheck,
    PolicySpec,
)


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise FlowContractError(f"{label} contains unknown fields: {sorted(unknown)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FlowContractError(f"{label} must be a non-empty string")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise FlowContractError(f"{label} must be a string array")
    return tuple(value)


def _string_mapping(value: object, label: str) -> dict[str, str]:
    table = _table(value, label)
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or not item
        for key, item in table.items()
    ):
        raise FlowContractError(f"{label} must map strings to strings")
    return dict(table)


def _table(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FlowContractError(f"{label} must be a table")
    return value


def _json_config(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    table = _table(value, label)
    try:
        json.dumps(table, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FlowContractError(f"{label} is not JSON-compatible: {exc}") from exc
    return table


def load_flow_contract(path: Path, *, owner_root: Path | None = None) -> FlowSpec:
    contract = Path(path).resolve()
    resolved_owner_root = None if owner_root is None else Path(owner_root).resolve()
    if resolved_owner_root is not None and not contract.is_relative_to(
        resolved_owner_root
    ):
        raise FlowContractError("Flow contract must be inside the explicit owner root")
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(f"cannot read Flow contract {contract}: {exc}") from exc
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "nodes", "targets", "policies"},
        str(contract),
    )
    if raw.get("schema") != 1:
        raise FlowContractError("Flow contract must use the current schema 1")
    if raw.get("contract_kind") != "flow":
        raise FlowContractError("Flow contract_kind must be 'flow'")
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Flow path_scope must be 'owner'")

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list):
        raise FlowContractError("Flow nodes must be an array of tables")
    nodes: list[FlowNode] = []
    for index, value in enumerate(nodes_raw):
        node = _table(value, f"nodes[{index}]")
        _reject_unknown(
            node,
            {"id", "action", "config", "bindings", "order_after", "policy"},
            f"nodes[{index}]",
        )
        bindings_raw = node.get("bindings", [])
        if not isinstance(bindings_raw, list):
            raise FlowContractError(f"nodes[{index}].bindings must be an array")
        bindings: list[ArtifactBinding] = []
        for binding_index, binding_value in enumerate(bindings_raw):
            binding = _table(
                binding_value,
                f"nodes[{index}].bindings[{binding_index}]",
            )
            _reject_unknown(
                binding,
                {"input", "producer", "output", "requires"},
                f"nodes[{index}].bindings[{binding_index}]",
            )
            bindings.append(
                ArtifactBinding(
                    input=_text(binding.get("input"), "binding input"),
                    producer=_text(binding.get("producer"), "binding producer"),
                    output=_text(binding.get("output"), "binding output"),
                    requires=_text(
                        binding.get("requires", "accepted"),
                        "binding requirement",
                    ),
                )
            )
        policy = node.get("policy")
        nodes.append(
            FlowNode(
                node_id=_text(node.get("id"), f"nodes[{index}].id"),
                action_kind=_text(node.get("action"), f"nodes[{index}].action"),
                config=_json_config(node.get("config"), f"nodes[{index}].config"),
                bindings=tuple(bindings),
                order_after=_string_list(
                    node.get("order_after", []),
                    f"nodes[{index}].order_after",
                ),
                policy=(
                    None
                    if policy is None
                    else _text(policy, f"nodes[{index}].policy")
                ),
            )
        )

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list):
        raise FlowContractError("Flow targets must be an array of tables")
    targets: list[FlowTarget] = []
    for index, value in enumerate(targets_raw):
        target = _table(value, f"targets[{index}]")
        _reject_unknown(target, {"name", "goals"}, f"targets[{index}]")
        targets.append(
            FlowTarget(
                target_id=_text(target.get("name"), f"targets[{index}].name"),
                goals=_string_list(target.get("goals"), f"targets[{index}].goals"),
            )
        )

    policies_raw = raw.get("policies", [])
    if not isinstance(policies_raw, list):
        raise FlowContractError("Flow policies must be an array of tables")
    policies: list[PolicySpec] = []
    for index, value in enumerate(policies_raw):
        policy = _table(value, f"policies[{index}]")
        _reject_unknown(policy, {"id", "checks"}, f"policies[{index}]")
        checks_raw = policy.get("checks")
        if not isinstance(checks_raw, list):
            raise FlowContractError(f"policies[{index}].checks must be an array")
        checks: list[PolicyCheck] = []
        for check_index, check_value in enumerate(checks_raw):
            check = _table(
                check_value,
                f"policies[{index}].checks[{check_index}]",
            )
            _reject_unknown(
                check,
                {"id", "fact", "operator", "expected"},
                f"policies[{index}].checks[{check_index}]",
            )
            checks.append(
                PolicyCheck(
                    check_id=_text(check.get("id"), "policy check id"),
                    fact=_text(check.get("fact"), "policy check fact"),
                    operator=_text(check.get("operator"), "policy check operator"),
                    expected=check.get("expected"),
                )
            )
        policies.append(
            PolicySpec(
                policy_id=_text(policy.get("id"), f"policies[{index}].id"),
                checks=tuple(checks),
            )
        )
    return FlowSpec(
        owner=_text(raw.get("owner"), "Flow owner"),
        flow_id=_text(raw.get("name"), "Flow name"),
        nodes=tuple(nodes),
        targets=tuple(targets),
        policies=tuple(policies),
        owner_root=resolved_owner_root,
    )


def load_execution_profile(path: Path) -> ExecutionProfile:
    contract = Path(path).resolve()
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(
            f"cannot read Execution Profile {contract}: {exc}"
        ) from exc
    _reject_unknown(raw, _HEADER_FIELDS | {"name", "actions"}, str(contract))
    if raw.get("schema") != 1:
        raise FlowContractError("Execution Profile must use the current schema 1")
    if raw.get("contract_kind") != "execution-profile":
        raise FlowContractError(
            "Execution Profile contract_kind must be 'execution-profile'"
        )
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Execution Profile path_scope must be 'owner'")
    actions = _table(raw.get("actions"), "Execution Profile actions")
    selections: list[AdapterSelection] = []
    for action_kind, value in actions.items():
        action = _table(value, f"actions.{action_kind}")
        _reject_unknown(
            action,
            {"adapter", "requires", "platform_assets", "config"},
            f"actions.{action_kind}",
        )
        selections.append(
            AdapterSelection(
                action_kind=action_kind,
                adapter=_text(
                    action.get("adapter"),
                    f"actions.{action_kind}.adapter",
                ),
                config=_json_config(
                    action.get("config"),
                    f"actions.{action_kind}.config",
                ),
                required_capabilities=_string_list(
                    action.get("requires", []),
                    f"actions.{action_kind}.requires",
                ),
                platform_asset_identities=_string_mapping(
                    action.get("platform_assets", {}),
                    f"actions.{action_kind}.platform_assets",
                ),
            )
        )
    return ExecutionProfile(
        owner=_text(raw.get("owner"), "Execution Profile owner"),
        profile_id=_text(raw.get("name"), "Execution Profile name"),
        selections=tuple(selections),
    )


def _owner_path(owner_root: Path, value: object, label: str) -> Path:
    root = Path(owner_root).resolve()
    relative_text = _text(value, label)
    relative = Path(relative_text)
    if (
        relative.is_absolute()
        or "\\" in relative_text
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FlowContractError(f"{label} must stay within the explicit owner root")
    resolved = (root / relative).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise FlowContractError(f"{label} escaped the explicit owner root")
    return resolved


def parse_flow_catalog(
    document: Mapping[str, Any],
    path: Path,
    *,
    owner_root: Path,
) -> FlowCatalog:
    """Validate an already read Flow Catalog document."""

    root = Path(owner_root).resolve()
    contract = Path(path).resolve()
    if not contract.is_relative_to(root):
        raise FlowContractError("Flow Catalog must be inside the explicit owner root")
    raw = document
    _reject_unknown(raw, _HEADER_FIELDS | {"flows"}, str(contract))
    if raw.get("schema") != 1:
        raise FlowContractError("Flow Catalog must use the current schema 1")
    if raw.get("contract_kind") != "flow-catalog":
        raise FlowContractError("Flow Catalog contract_kind must be 'flow-catalog'")
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Flow Catalog path_scope must be 'owner'")
    flows = _table(raw.get("flows"), "Flow Catalog flows")
    entries: list[FlowCatalogEntry] = []
    for flow_id, value in flows.items():
        flow = _table(value, f"flows.{flow_id}")
        _reject_unknown(
            flow,
            {"contract", "default_profile", "profiles"},
            f"flows.{flow_id}",
        )
        profiles_raw = _table(flow.get("profiles"), f"flows.{flow_id}.profiles")
        profiles = {
            profile_id: _owner_path(
                root,
                profile_path,
                f"flows.{flow_id}.profiles.{profile_id}",
            )
            for profile_id, profile_path in profiles_raw.items()
        }
        entries.append(
            FlowCatalogEntry(
                flow_id=flow_id,
                contract=_owner_path(
                    root,
                    flow.get("contract"),
                    f"flows.{flow_id}.contract",
                ),
                default_profile=_text(
                    flow.get("default_profile"),
                    f"flows.{flow_id}.default_profile",
                ),
                profiles=profiles,
            )
        )
    return FlowCatalog(
        owner=_text(raw.get("owner"), "Flow Catalog owner"),
        owner_root=root,
        entries=tuple(entries),
    )


def load_flow_catalog(path: Path, *, owner_root: Path) -> FlowCatalog:
    """Read and validate one Flow Catalog file."""

    root = Path(owner_root).resolve()
    contract = Path(path).resolve()
    if not contract.is_relative_to(root):
        raise FlowContractError("Flow Catalog must be inside the explicit owner root")
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(f"cannot read Flow Catalog {contract}: {exc}") from exc
    return parse_flow_catalog(raw, contract, owner_root=root)


def resolve_catalog_selection(
    catalog: FlowCatalog,
    *,
    flow_id: str,
    profile_id: str | None = None,
) -> CatalogSelection:
    """Resolve one Flow and profile from an already validated catalog snapshot."""

    entry = catalog.entry(flow_id)
    selected_profile = profile_id or entry.default_profile
    try:
        profile_path = entry.profiles[selected_profile].resolve()
    except KeyError as exc:
        raise FlowContractError(
            f"Flow {flow_id!r} has no cataloged profile {selected_profile!r}"
        ) from exc
    if not profile_path.is_relative_to(catalog.owner_root):
        raise FlowContractError(
            "Execution Profile must be inside the catalog owner root"
        )
    spec = load_flow_contract(entry.contract, owner_root=catalog.owner_root)
    profile = load_execution_profile(profile_path)
    if spec.owner != catalog.owner or profile.owner != catalog.owner:
        raise FlowContractError(
            f"cataloged Flow and Execution Profile must be owned by {catalog.owner!r}"
        )
    if spec.flow_id != flow_id:
        raise FlowContractError(
            f"catalog entry {flow_id!r} resolved Flow {spec.flow_id!r}"
        )
    if profile.profile_id != selected_profile:
        raise FlowContractError(
            f"catalog profile {selected_profile!r} resolved {profile.profile_id!r}"
        )
    return CatalogSelection(spec=spec, profile=profile)


def load_catalog_selection(
    path: Path,
    *,
    owner_root: Path,
    flow_id: str,
    profile_id: str | None = None,
) -> CatalogSelection:
    """Load one Flow catalog and resolve its selected Flow and profile."""

    return resolve_catalog_selection(
        load_flow_catalog(path, owner_root=owner_root),
        flow_id=flow_id,
        profile_id=profile_id,
    )
