"""Strict loader for owner-owned execution recipe contracts."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.flow.model import (
    ActionBinding,
    ArtifactBinding,
    ExecutionRecipe,
    FlowContractError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    PolicyCheck,
    PolicySpec,
    SourceMember,
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


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FlowContractError(f"{label} must be a table")
    return value


def _json_config(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    table = dict(_table(value, label))
    try:
        json.dumps(table, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FlowContractError(f"{label} is not JSON-compatible: {exc}") from exc
    return table


def _parse_nodes(raw: Mapping[str, Any]) -> tuple[FlowNode, ...]:
    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list):
        raise FlowContractError("Execution Recipe nodes must be an array of tables")
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
    return tuple(nodes)


def _parse_policies(raw: Mapping[str, Any]) -> tuple[PolicySpec, ...]:
    policies_raw = raw.get("policies", [])
    if not isinstance(policies_raw, list):
        raise FlowContractError("Execution Recipe policies must be an array of tables")
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
    return tuple(policies)


def _parse_action_bindings(raw: Mapping[str, Any]) -> tuple[ActionBinding, ...]:
    actions = _table(raw.get("actions"), "Execution Recipe actions")
    bindings: list[ActionBinding] = []
    for action_kind, value in actions.items():
        action = _table(value, f"actions.{action_kind}")
        _reject_unknown(
            action,
            {"adapter", "requires", "platform_assets", "config"},
            f"actions.{action_kind}",
        )
        bindings.append(
            ActionBinding(
                action_kind=_text(action_kind, "Action binding kind"),
                adapter=_text(
                    action.get("adapter"),
                    f"actions.{action_kind}.adapter",
                ),
                config=_json_config(
                    action.get("config"),
                    f"actions.{action_kind}.config",
                ),
                requires=_string_list(
                    action.get("requires", []),
                    f"actions.{action_kind}.requires",
                ),
                platform_assets=_string_mapping(
                    action.get("platform_assets", {}),
                    f"actions.{action_kind}.platform_assets",
                ),
            )
        )
    return tuple(bindings)


def load_execution_recipe(
    path: Path,
    *,
    owner_root: Path | None = None,
) -> ExecutionRecipe:
    """Read and validate one owner-owned execution recipe.

    Targets are owned by the repository catalog and therefore cannot appear in
    this operation recipe.  The explicit rejection keeps the two source
    contracts at a clear seam instead of silently discarding target data.
    """

    contract = Path(path).resolve()
    resolved_owner_root = None if owner_root is None else Path(owner_root).resolve()
    if resolved_owner_root is not None and not contract.is_relative_to(
        resolved_owner_root
    ):
        raise FlowContractError(
            "Execution Recipe must be inside the explicit owner root"
        )
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(
            f"cannot read Execution Recipe {contract}: {exc}"
        ) from exc
    return parse_execution_recipe(
        raw,
        contract,
        owner_root=resolved_owner_root,
    )


def parse_execution_recipe(
    raw: Mapping[str, Any],
    path: Path,
    *,
    owner_root: Path | None = None,
) -> ExecutionRecipe:
    """Validate an already captured execution-recipe source document."""

    contract = Path(path).resolve()
    resolved_owner_root = None if owner_root is None else Path(owner_root).resolve()
    if resolved_owner_root is not None and not contract.is_relative_to(
        resolved_owner_root
    ):
        raise FlowContractError(
            "Execution Recipe must be inside the explicit owner root"
        )
    if "targets" in raw:
        raise FlowContractError(
            "Execution Recipe cannot declare targets; targets belong to the owner catalog"
        )
    if "expand" in raw:
        raise FlowContractError(
            "Execution Recipe cannot declare expand; catalog expansion was removed"
        )
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "actions", "nodes", "policies"},
        str(contract),
    )
    if raw.get("schema") != 1:
        raise FlowContractError("Execution Recipe must use the current schema 1")
    if raw.get("contract_kind") != "execution-recipe":
        raise FlowContractError(
            "Execution Recipe contract_kind must be 'execution-recipe'"
        )
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Execution Recipe path_scope must be 'owner'")
    return ExecutionRecipe(
        owner=_text(raw.get("owner"), "Execution Recipe owner"),
        recipe_id=_text(raw.get("name"), "Execution Recipe name"),
        nodes=_parse_nodes(raw),
        policies=_parse_policies(raw),
        action_bindings=_parse_action_bindings(raw),
        owner_root=resolved_owner_root,
    )


def compile_flow_spec(
    recipe: ExecutionRecipe,
    *,
    flow_id: str,
    targets: tuple[FlowTarget, ...],
    source_members: tuple[SourceMember, ...] = (),
) -> FlowSpec:
    """Compile an operation recipe with owner-catalog targets for the Engine."""

    return FlowSpec(
        owner=recipe.owner,
        flow_id=flow_id,
        recipe_id=recipe.recipe_id,
        nodes=recipe.nodes,
        targets=targets,
        policies=recipe.policies,
        action_bindings=recipe.action_bindings,
        source_members=source_members,
        owner_root=recipe.owner_root,
    )


__all__ = [
    "compile_flow_spec",
    "load_execution_recipe",
    "parse_execution_recipe",
]
