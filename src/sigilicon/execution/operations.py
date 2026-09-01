"""Load an owner operation exactly once and compile it into typed Steps."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.execution.model import ContractError, Evidence, ExecutionPlan, Source, Step
from sigilicon.paths import validate_artifact_component


_HEADER = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset({"description", "with", "sources", "operations"})
_OPERATION_FIELDS = frozenset({"uses", "with", "sources", "evidence", "steps"})
_STEP_FIELDS = frozenset({"id", "uses", "needs", "with", "sources", "evidence"})


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be a TOML table")
    return value


def _name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be an identifier")
    try:
        return validate_artifact_component(value, field)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _strings(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError(f"{field} must be a string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ContractError(f"{field} contains duplicates")
    return result


def _config(value: object, field: str) -> dict[str, Any]:
    return dict(_table({} if value is None else value, field))


def _merge(*values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        result.update(value)
    return result


def _evidence(value: object, field: str) -> Evidence | None:
    if value is None:
        return None
    table = _table(value, field)
    if set(table) != {"role", "level", "scope"}:
        raise ContractError(f"{field} must contain role, level, and scope")
    try:
        return Evidence(table["role"], table["level"], table["scope"])
    except (KeyError, TypeError) as exc:
        raise ContractError(f"{field} contains invalid evidence values") from exc


def _source_path(owner_root: Path, value: str, field: str) -> Path:
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError(f"{field} must be a canonical owner-relative path")
    path = owner_root.joinpath(*relative.parts)
    resolved = path.resolve()
    if path.absolute() != resolved or not resolved.is_relative_to(owner_root):
        raise ContractError(f"{field} must not traverse a symlink or leave its owner")
    return resolved


def _sources(
    owner_root: Path,
    values: tuple[str, ...],
    field: str,
) -> tuple[Source, ...]:
    return tuple(
        Source.capture(
            _source_path(owner_root, value, f"{field}[{index}]"),
            root=owner_root,
            scope="owner",
        )
        for index, value in enumerate(values)
    )


def _step(
    raw: Mapping[str, Any],
    *,
    field: str,
    owner_root: Path,
    inherited_config: Mapping[str, Any],
    inherited_sources: tuple[Source, ...],
    default_id: str | None = None,
) -> Step:
    unknown = set(raw) - _STEP_FIELDS
    if unknown:
        raise ContractError(f"{field} contains unknown fields: {sorted(unknown)}")
    step_id = default_id if default_id is not None else _name(raw.get("id"), f"{field}.id")
    uses = raw.get("uses")
    if not isinstance(uses, str):
        raise ContractError(f"{field}.uses must be a backend identity")
    own_sources = _sources(
        owner_root,
        _strings(raw.get("sources"), f"{field}.sources"),
        f"{field}.sources",
    )
    return Step(
        step_id,
        uses,
        _merge(inherited_config, _config(raw.get("with"), f"{field}.with")),
        _strings(raw.get("needs"), f"{field}.needs"),
        tuple(source.path for source in (*inherited_sources, *own_sources)),
        _evidence(raw.get("evidence"), f"{field}.evidence"),
    )


def compile_operation(
    catalog_path: Path,
    *,
    owner: str,
    owner_root: Path,
    target: str,
    operation: str,
) -> ExecutionPlan:
    """Compile ``owner/target:operation`` from one owner-operations document."""

    path = Path(catalog_path).absolute()
    root = Path(owner_root).resolve()
    if path.resolve() != path or not path.is_relative_to(root):
        raise ContractError("operation catalog must be a non-symlink owner source")
    try:
        record_text = read_nofollow_text(path)
        raw = tomllib.loads(record_text)
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"cannot read operation catalog {path}: {exc}") from exc
    expected_header = {
        "schema": 1,
        "contract_kind": "owner-operations",
        "path_scope": "owner",
        "owner": owner,
    }
    if any(raw.get(name) != value for name, value in expected_header.items()):
        raise ContractError(
            f"{path}: expected schema=1, contract_kind='owner-operations', "
            f"path_scope='owner', owner={owner!r}"
        )
    unknown = set(raw) - _HEADER - {"targets"}
    if unknown:
        raise ContractError(f"{path}: unknown owner operation fields: {sorted(unknown)}")
    targets = _table(raw.get("targets"), "targets")
    target_name = _name(target, "target")
    operation_name = _name(operation, "operation")
    try:
        target_raw = _table(targets[target_name], f"targets.{target_name}")
    except KeyError as exc:
        raise ContractError(f"unknown target {target_name!r}; available: {sorted(targets)}") from exc
    unknown_target = set(target_raw) - _TARGET_FIELDS
    if unknown_target:
        raise ContractError(
            f"targets.{target_name} contains unknown fields: {sorted(unknown_target)}"
        )
    description = target_raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ContractError(f"targets.{target_name}.description must be non-empty text")
    operations = _table(target_raw.get("operations"), f"targets.{target_name}.operations")
    try:
        operation_raw = _table(
            operations[operation_name],
            f"targets.{target_name}.operations.{operation_name}",
        )
    except KeyError as exc:
        raise ContractError(
            f"target {target_name!r} has no operation {operation_name!r}; "
            f"available: {sorted(operations)}"
        ) from exc
    unknown_operation = set(operation_raw) - _OPERATION_FIELDS
    if unknown_operation:
        raise ContractError(
            f"operation {operation_name!r} contains unknown fields: {sorted(unknown_operation)}"
        )
    target_config = _config(target_raw.get("with"), f"targets.{target_name}.with")
    operation_config = _config(
        operation_raw.get("with"),
        f"targets.{target_name}.operations.{operation_name}.with",
    )
    inherited_sources = (
        *_sources(
            root,
            _strings(target_raw.get("sources"), f"targets.{target_name}.sources"),
            f"targets.{target_name}.sources",
        ),
        *_sources(
            root,
            _strings(operation_raw.get("sources"), f"operations.{operation_name}.sources"),
            f"operations.{operation_name}.sources",
        ),
    )
    steps_raw = operation_raw.get("steps")
    uses = operation_raw.get("uses")
    if (steps_raw is None) == (uses is None):
        raise ContractError(
            f"operation {operation_name!r} must declare exactly one of uses or steps"
        )
    inherited_config = _merge(target_config, operation_config)
    if uses is not None:
        steps = (
            _step(
                {
                    "uses": uses,
                    "with": {},
                    "sources": [],
                    "evidence": operation_raw.get("evidence"),
                },
                field=f"operations.{operation_name}",
                owner_root=root,
                inherited_config=inherited_config,
                inherited_sources=inherited_sources,
                default_id="run",
            ),
        )
    else:
        if operation_raw.get("evidence") is not None:
            raise ContractError("multi-step operation evidence belongs on each step")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ContractError(f"operation {operation_name!r}.steps must be a non-empty array")
        steps = tuple(
            _step(
                _table(value, f"operations.{operation_name}.steps[{index}]"),
                field=f"operations.{operation_name}.steps[{index}]",
                owner_root=root,
                inherited_config=inherited_config,
                inherited_sources=inherited_sources,
            )
            for index, value in enumerate(steps_raw)
        )
    catalog_source = Source.capture(path, root=root, scope="owner")
    if catalog_source.text != record_text:
        raise ContractError("operation catalog changed while it was being parsed")
    unique_sources: dict[str, Source] = {catalog_source.path: catalog_source}
    for source in inherited_sources:
        unique_sources[source.path] = source
    for step in steps:
        for source_name in step.sources:
            if source_name not in unique_sources:
                unique_sources[source_name] = Source.capture(
                    _source_path(root, source_name, f"steps.{step.id}.sources"),
                    root=root,
                    scope="owner",
                )
    return ExecutionPlan(owner, target_name, operation_name, steps, tuple(unique_sources.values()))


def parse_selector(value: str) -> tuple[str, str, str]:
    """Parse the canonical ``owner/target:operation`` selector."""

    if not isinstance(value, str) or value.count("/") != 1 or value.count(":") != 1:
        raise ContractError("operation selector must be owner/target:operation")
    owner, remainder = value.split("/", 1)
    target, operation = remainder.split(":", 1)
    return _name(owner, "owner"), _name(target, "target"), _name(operation, "operation")


__all__ = ["compile_operation", "parse_selector"]
