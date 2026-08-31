"""Strict loader for owner-owned execution recipe contracts."""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from pathlib import PurePosixPath
import tomllib
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.flow.model import (
    ActionBinding,
    ArtifactBinding,
    ExecutionRecipe,
    FlowContractError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    InputReference,
    RECIPE_INPUT_KINDS,
    PolicyCheck,
    PolicySpec,
    RecipeInput,
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


def _recipe_value(value: object, label: str) -> Any:
    """Capture portable recipe data and only whole-value input references."""

    if isinstance(value, str):
        if "${" in value:
            raise FlowContractError(
                f"{label} cannot use string interpolation; use a whole-value input reference"
            )
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FlowContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if "input" in value:
            if set(value) != {"input"}:
                raise FlowContractError(
                    f"{label} input reference must be a whole value"
                )
            name = value.get("input")
            if not isinstance(name, str) or not name:
                raise FlowContractError(f"{label}.input must be a non-empty name")
            return InputReference(name)
        if any(not isinstance(key, str) or not key for key in value):
            raise FlowContractError(f"{label} contains a non-string key")
        return {
            key: _recipe_value(item, f"{label}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _recipe_value(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    raise FlowContractError(f"{label} is not a portable recipe value")


def _string_mapping(value: object, label: str) -> dict[str, Any]:
    table = _recipe_value(_table(value, label), label)
    if not isinstance(table, dict) or any(
        not isinstance(item, (str, InputReference)) or not item
        for item in table.values()
    ):
        raise FlowContractError(
            f"{label} must map strings to strings or input references"
        )
    return table


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FlowContractError(f"{label} must be a table")
    return value


def _json_config(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    table = _recipe_value(_table(value, label), label)
    if not isinstance(table, dict):
        raise FlowContractError(f"{label} must be a table")
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


def _parse_inputs(raw: Mapping[str, Any]) -> dict[str, RecipeInput]:
    values = raw.get("inputs", {})
    table = _table(values, "Execution Recipe inputs")
    result: dict[str, RecipeInput] = {}
    for name, value in table.items():
        input_name = _text(name, "Execution Recipe input name")
        declaration = _table(value, f"inputs.{input_name}")
        _reject_unknown(
            declaration,
            {"kind"},
            f"inputs.{input_name}",
        )
        kind = _text(declaration.get("kind"), f"inputs.{input_name}.kind")
        if kind not in RECIPE_INPUT_KINDS:
            raise FlowContractError(
                f"inputs.{input_name}.kind must be one of "
                f"{sorted(RECIPE_INPUT_KINDS)}"
            )
        result[input_name] = RecipeInput(input_name, kind)
    return result


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
        _HEADER_FIELDS | {"name", "inputs", "actions", "nodes", "policies"},
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
        inputs=_parse_inputs(raw),
        owner_root=resolved_owner_root,
    )


def compile_flow_spec(
    recipe: ExecutionRecipe,
    *,
    flow_id: str,
    targets: tuple[FlowTarget, ...],
    inputs: Mapping[str, Any] | None = None,
    source_members: tuple[SourceMember, ...] = (),
) -> FlowSpec:
    """Compile one recipe with exact owner-target inputs for the Engine."""

    if inputs is not None and not isinstance(inputs, Mapping):
        raise FlowContractError("Flow inputs must be a mapping")
    supplied = {} if inputs is None else dict(inputs)
    if any(not isinstance(name, str) for name in supplied):
        raise FlowContractError("Flow inputs must use string names")
    expected = set(recipe.inputs)
    actual = set(supplied)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise FlowContractError(
            "Flow inputs do not match the Execution Recipe declaration: "
            + ", ".join(details)
        )

    resolved_inputs = {
        name: _validate_input_value(
            declaration.kind,
            supplied[name],
            f"inputs.{name}",
            owner_root=recipe.owner_root,
        )
        for name, declaration in recipe.inputs.items()
    }

    def resolve(value: Any, label: str) -> Any:
        if isinstance(value, InputReference):
            try:
                return resolved_inputs[value.name]
            except KeyError as exc:
                raise FlowContractError(
                    f"{label} references undeclared input {value.name!r}"
                ) from exc
        if isinstance(value, str):
            if "${" in value:
                raise FlowContractError(
                    f"{label} cannot use string interpolation; use a whole-value input reference"
                )
            return value
        if isinstance(value, Mapping):
            if "input" in value:
                if set(value) != {"input"}:
                    raise FlowContractError(
                        f"{label} input reference must be a whole value"
                    )
                reference = value.get("input")
                if not isinstance(reference, str) or not reference:
                    raise FlowContractError(f"{label}.input must be a non-empty name")
                return resolve(InputReference(reference), label)
            return {
                key: resolve(item, f"{label}.{key}")
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                resolve(item, f"{label}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    nodes = tuple(
        replace(
            node,
            config=resolve(node.config, f"nodes.{node.node_id}.config"),
            extensions=resolve(node.extensions, f"nodes.{node.node_id}.extensions"),
        )
        for node in recipe.nodes
    )
    action_bindings = tuple(
        replace(
            binding,
            config=resolve(
                binding.config,
                f"actions.{binding.action_kind}.config",
            ),
            platform_assets=resolve(
                binding.platform_assets,
                f"actions.{binding.action_kind}.platform_assets",
            ),
        )
        for binding in recipe.action_bindings
    )

    return FlowSpec(
        owner=recipe.owner,
        flow_id=flow_id,
        recipe_id=recipe.recipe_id,
        nodes=nodes,
        targets=targets,
        policies=recipe.policies,
        action_bindings=action_bindings,
        inputs=MappingProxyType(resolved_inputs),
        source_members=source_members,
        owner_root=recipe.owner_root,
    )


def _validate_input_value(
    kind: str,
    value: Any,
    label: str,
    *,
    owner_root: Path | None,
) -> Any:
    if kind == "text":
        if type(value) is not str:
            raise FlowContractError(f"{label} must be text")
        return value
    if kind == "boolean":
        if type(value) is not bool:
            raise FlowContractError(f"{label} must be boolean")
        return value
    if kind == "integer":
        if type(value) is not int:
            raise FlowContractError(f"{label} must be integer")
        return value
    if kind == "real":
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise FlowContractError(f"{label} must be a finite real")
        return value
    if kind == "owner-path":
        if type(value) is not str:
            raise FlowContractError(f"{label} must be an owner-relative path")
        if owner_root is None:
            raise FlowContractError(
                f"{label} requires an explicit Execution Recipe owner root"
            )
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or "\\" in value
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowContractError(f"{label} must stay within the explicit owner root")
        configured = owner_root.joinpath(*relative.parts)
        resolved = configured.resolve(strict=False)
        if configured != resolved:
            raise FlowContractError(f"{label} must not name a symlink")
        if not resolved.is_relative_to(owner_root):
            raise FlowContractError(f"{label} escaped the explicit owner root")
        if not resolved.is_file():
            raise FlowContractError(
                f"{label} must name an existing regular file within the owner root"
            )
        return relative.as_posix()
    if kind == "semantic-identity":
        if (
            type(value) is not str
            or not value
            or "\n" in value
            or "\r" in value
            or Path(value).is_absolute()
        ):
            raise FlowContractError(f"{label} must be a semantic identity")
        return value
    if kind == "scalar-map":
        if not isinstance(value, Mapping):
            raise FlowContractError(f"{label} must be a scalar map")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise FlowContractError(f"{label} must use non-empty string keys")
            if item is None or isinstance(item, (Mapping, list, tuple)):
                raise FlowContractError(f"{label}.{key} must be a scalar")
            if not isinstance(item, (str, bool, int, float)):
                raise FlowContractError(f"{label}.{key} must be a scalar")
            if isinstance(item, float) and not math.isfinite(item):
                raise FlowContractError(f"{label}.{key} must be finite")
            result[key] = item
        return MappingProxyType(result)
    raise FlowContractError(
        f"{label} uses unsupported recipe input kind {kind!r}; "
        f"expected one of {sorted(RECIPE_INPUT_KINDS)}"
    )


__all__ = [
    "compile_flow_spec",
    "load_execution_recipe",
    "parse_execution_recipe",
]
