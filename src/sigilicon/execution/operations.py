"""Load an owner operation exactly once and compile it into typed Steps."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.execution.model import (
    ContractError,
    Evidence,
    OperationPlan,
    OperationStep,
    Source,
)
from sigilicon.paths import validate_artifact_component


_HEADER = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset(
    {
        "description", "with", "sources", "source_groups", "source_globs",
        "project_sources", "project_source_globs", "operations",
    }
)
_OPERATION_FIELDS = frozenset(
    {
        "uses", "with", "sources", "source_groups", "source_globs",
        "project_sources", "project_source_globs", "evidence", "steps",
    }
)
_STEP_FIELDS = frozenset(
    {
        "id", "uses", "needs", "with", "sources", "source_groups",
        "source_globs", "project_sources", "project_source_globs", "evidence",
    }
)
_SOURCE_GROUP_FIELDS = frozenset(
    {"sources", "source_globs", "project_sources", "project_source_globs"}
)


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
    root: Path,
    values: tuple[str, ...],
    field: str,
    *,
    scope: str,
) -> tuple[Source, ...]:
    return tuple(
        Source.capture(
            _source_path(root, value, f"{field}[{index}]"),
            root=root,
            scope=scope,
        )
        for index, value in enumerate(values)
    )


def _glob_sources(
    root: Path,
    values: tuple[str, ...],
    field: str,
    *,
    scope: str,
) -> tuple[Source, ...]:
    selected: list[Source] = []
    for index, value in enumerate(values):
        pattern = PurePosixPath(value)
        if (
            not value
            or pattern.is_absolute()
            or "\\" in value
            or pattern.as_posix() != value
            or any(part in {"", ".", ".."} for part in pattern.parts)
        ):
            raise ContractError(f"{field}[{index}] must be a canonical source glob")
        matches = sorted(path for path in root.glob(value) if path.is_file())
        if not matches:
            raise ContractError(f"{field}[{index}] matched no source files")
        selected.extend(
            Source.capture(path, root=root, scope=scope) for path in matches
        )
    return tuple(selected)


def _selected_sources(
    owner_root: Path,
    project_root: Path,
    raw: Mapping[str, Any],
    *,
    field: str,
    source_groups: Mapping[str, Mapping[str, Any]],
) -> tuple[Source, ...]:
    names = list(_strings(raw.get("sources"), f"{field}.sources"))
    grouped: list[Source] = []
    for group in _strings(raw.get("source_groups"), f"{field}.source_groups"):
        try:
            group_selection = source_groups[group]
        except KeyError as exc:
            raise ContractError(
                f"{field}.source_groups references unknown group {group!r}"
            ) from exc
        grouped.extend(
            _selected_sources(
                owner_root,
                project_root,
                group_selection,
                field=f"source_groups.{group}",
                source_groups={},
            )
        )
    if len(names) != len(set(names)):
        raise ContractError(f"{field} selects duplicate sources")
    selected = (
        *grouped,
        *_sources(
            owner_root, tuple(names), f"{field}.sources", scope="owner"
        ),
        *_glob_sources(
            owner_root,
            _strings(raw.get("source_globs"), f"{field}.source_globs"),
            f"{field}.source_globs",
            scope="owner",
        ),
        *_sources(
            project_root,
            _strings(raw.get("project_sources"), f"{field}.project_sources"),
            f"{field}.project_sources",
            scope="project",
        ),
        *_glob_sources(
            project_root,
            _strings(
                raw.get("project_source_globs"),
                f"{field}.project_source_globs",
            ),
            f"{field}.project_source_globs",
            scope="project",
        ),
    )
    identities = {(source.root, source.path) for source in selected}
    if len(identities) != len(selected):
        raise ContractError(f"{field} selects overlapping source trees")
    if len({source.path for source in selected}) != len(selected):
        raise ContractError(f"{field} source paths collide across scopes")
    return selected


def _step(
    raw: Mapping[str, Any],
    *,
    field: str,
    owner_root: Path,
    project_root: Path,
    inherited_config: Mapping[str, Any],
    inherited_sources: tuple[Source, ...],
    source_groups: Mapping[str, Mapping[str, Any]],
    default_id: str | None = None,
) -> OperationStep:
    unknown = set(raw) - _STEP_FIELDS
    if unknown:
        raise ContractError(f"{field} contains unknown fields: {sorted(unknown)}")
    step_id = default_id if default_id is not None else _name(raw.get("id"), f"{field}.id")
    uses = raw.get("uses")
    if not isinstance(uses, str):
        raise ContractError(f"{field}.uses must be a backend identity")
    own_sources = _selected_sources(
        owner_root,
        project_root,
        raw,
        field=field,
        source_groups=source_groups,
    )
    return OperationStep(
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
    project_identity: str,
    owner: str,
    owner_root: Path,
    project_root: Path,
    target: str,
    operation: str,
) -> ExecutionPlan:
    """Compile ``owner/target:operation`` from one owner-operations document."""

    path = Path(catalog_path).absolute()
    root = Path(owner_root).resolve()
    repository_root = Path(project_root).resolve()
    if not root.is_relative_to(repository_root):
        raise ContractError("operation owner root must stay inside its project")
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
    unknown = set(raw) - _HEADER - {"source_groups", "operations", "targets"}
    if unknown:
        raise ContractError(f"{path}: unknown owner operation fields: {sorted(unknown)}")
    raw_groups = _table(raw.get("source_groups", {}), "source_groups")
    source_groups: dict[str, Mapping[str, Any]] = {}
    for name, value in raw_groups.items():
        group = _name(name, "source group")
        selection = (
            {"sources": value}
            if isinstance(value, list)
            else _table(value, f"source_groups.{group}")
        )
        unknown_group = set(selection) - _SOURCE_GROUP_FIELDS
        if unknown_group:
            raise ContractError(
                f"source_groups.{group} contains unknown fields: "
                f"{sorted(unknown_group)}"
            )
        for selection_field in _SOURCE_GROUP_FIELDS:
            _strings(
                selection.get(selection_field),
                f"source_groups.{group}.{selection_field}",
            )
        source_groups[group] = selection
    operation_definitions = _table(raw.get("operations"), "operations")
    targets = _table(raw.get("targets"), "targets")
    enabled_by_targets: set[str] = set()
    for configured_target, value in targets.items():
        configured_name = _name(configured_target, "target")
        configured = _table(value, f"targets.{configured_name}")
        unknown_target = set(configured) - _TARGET_FIELDS
        if unknown_target:
            raise ContractError(
                f"targets.{configured_name} contains unknown fields: "
                f"{sorted(unknown_target)}"
            )
        if not isinstance(configured.get("description"), str) or not str(
            configured["description"]
        ).strip():
            raise ContractError(
                f"targets.{configured_name}.description must be non-empty text"
            )
        enabled_by_targets.update(
            _name(name, f"targets.{configured_name}.operations entry")
            for name in _strings(
                configured.get("operations"),
                f"targets.{configured_name}.operations",
            )
        )
    defined_operations: set[str] = set()
    for configured_operation, value in operation_definitions.items():
        configured_name = _name(configured_operation, "operation")
        configured = _table(value, f"operations.{configured_name}")
        unknown_operation = set(configured) - _OPERATION_FIELDS
        if unknown_operation:
            raise ContractError(
                f"operation {configured_name!r} contains unknown fields: "
                f"{sorted(unknown_operation)}"
            )
        defined_operations.add(configured_name)
    if enabled_by_targets != defined_operations:
        raise ContractError(
            "defined and enabled owner operations must form the same closed set; "
            f"undefined={sorted(enabled_by_targets - defined_operations)}, "
            f"unused={sorted(defined_operations - enabled_by_targets)}"
        )
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
    enabled_operations = _strings(
        target_raw.get("operations"), f"targets.{target_name}.operations"
    )
    if operation_name not in enabled_operations:
        raise ContractError(
            f"target {target_name!r} has no operation {operation_name!r}; "
            f"available: {sorted(enabled_operations)}"
        )
    try:
        operation_raw = _table(
            operation_definitions[operation_name],
            f"operations.{operation_name}",
        )
    except KeyError as exc:
        raise ContractError(
            f"target {target_name!r} enables undefined operation {operation_name!r}"
        ) from exc
    unknown_operation = set(operation_raw) - _OPERATION_FIELDS
    if unknown_operation:
        raise ContractError(
            f"operation {operation_name!r} contains unknown fields: {sorted(unknown_operation)}"
        )
    target_config = _config(target_raw.get("with"), f"targets.{target_name}.with")
    operation_config = _config(
        operation_raw.get("with"),
        f"operations.{operation_name}.with",
    )
    inherited_sources = (
        *_selected_sources(
            root,
            repository_root,
            target_raw,
            field=f"targets.{target_name}",
            source_groups=source_groups,
        ),
        *_selected_sources(
            root,
            repository_root,
            operation_raw,
            field=f"operations.{operation_name}",
            source_groups=source_groups,
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
                project_root=repository_root,
                inherited_config=inherited_config,
                inherited_sources=inherited_sources,
                source_groups=source_groups,
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
                project_root=repository_root,
                inherited_config=inherited_config,
                inherited_sources=inherited_sources,
                source_groups=source_groups,
            )
            for index, value in enumerate(steps_raw)
        )
    selected_step_sources = (
        ()
        if not isinstance(steps_raw, list)
        else tuple(
            source
            for index, value in enumerate(steps_raw)
            for source in _selected_sources(
                root,
                repository_root,
                _table(value, f"operations.{operation_name}.steps[{index}]"),
                field=f"operations.{operation_name}.steps[{index}]",
                source_groups=source_groups,
            )
        )
    )
    catalog_source = Source.capture(path, root=root, scope="owner")
    if catalog_source.text != record_text:
        raise ContractError("operation catalog changed while it was being parsed")
    unique_sources: dict[str, Source] = {catalog_source.path: catalog_source}
    for source in (*inherited_sources, *selected_step_sources):
        if source.path in unique_sources and unique_sources[source.path].root != source.root:
            raise ContractError("operation source paths collide across scopes")
        unique_sources[source.path] = source
    for step in steps:
        for source_name in step.sources:
            if source_name not in unique_sources:
                raise ContractError(
                    f"step {step.id!r} source is absent from the compiled closure: "
                    f"{source_name}"
                )
    return OperationPlan(
        project_identity,
        owner,
        target_name,
        operation_name,
        steps,
        tuple(unique_sources.values()),
    )


def parse_selector(value: str) -> tuple[str, str, str]:
    """Parse the canonical ``owner/target:operation`` selector."""

    if not isinstance(value, str) or value.count("/") != 1 or value.count(":") != 1:
        raise ContractError("operation selector must be owner/target:operation")
    owner, remainder = value.split("/", 1)
    target, operation = remainder.split(":", 1)
    return _name(owner, "owner"), _name(target, "target"), _name(operation, "operation")


__all__ = ["compile_operation", "parse_selector"]
