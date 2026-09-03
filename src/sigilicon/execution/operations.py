"""Compile one explicitly named owner operation from component filesets."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.execution._model import (
    ContractError,
    Evidence,
    ExecutionPlan,
    RuntimeEnvironment,
    Source,
    Step,
    _prepare_step,
    adapter_identity,
)
from sigilicon.paths import validate_artifact_component


_HEADER = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_DIRECT_FIELDS = frozenset({"uses", "filesets", "runtime", "config", "evidence"})
_GRAPH_FIELDS = frozenset({"steps"})
_STEP_FIELDS = frozenset(
    {"id", "uses", "needs", "filesets", "runtime", "config", "evidence"}
)
_RUNTIME_FIELDS = frozenset({"tools", "files", "directories", "values"})


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


def _strings(value: object, field: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if (
        not isinstance(value, list)
        or (required and not value)
        or any(not isinstance(item, str) for item in value)
    ):
        qualifier = "a non-empty " if required else "a "
        raise ContractError(f"{field} must be {qualifier}string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ContractError(f"{field} contains duplicates")
    return result


def _config(value: object, field: str) -> dict[str, Any]:
    return dict(_table({} if value is None else value, field))


def _runtime_profiles(value: object) -> dict[str, RuntimeEnvironment]:
    profiles = _table({} if value is None else value, "runtime")
    result: dict[str, RuntimeEnvironment] = {}
    for raw_name, raw_profile in profiles.items():
        name = _name(raw_name, "runtime profile")
        profile = _table(raw_profile, f"runtime.{name}")
        unknown = set(profile) - _RUNTIME_FIELDS
        if unknown:
            raise ContractError(
                f"runtime.{name} contains unknown fields: {sorted(unknown)}"
            )
        tables: dict[str, dict[str, str]] = {}
        for kind in _RUNTIME_FIELDS:
            raw_bindings = _table(profile.get(kind, {}), f"runtime.{name}.{kind}")
            tables[kind] = dict(raw_bindings)
        result[name] = RuntimeEnvironment(**tables)
    return result


def _runtime(
    value: object,
    *,
    uses: str,
    profiles: Mapping[str, RuntimeEnvironment],
    defaults: Mapping[str, str],
    field: str,
) -> RuntimeEnvironment:
    selected = defaults.get(uses) if value is None else value
    if selected is None:
        return RuntimeEnvironment()
    name = _name(selected, field)
    try:
        return profiles[name]
    except KeyError as exc:
        raise ContractError(
            f"{field} references unknown runtime profile {name!r}; "
            f"available: {sorted(profiles)}"
        ) from exc


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


def _operation_identity(value: object, field: str) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value or value.count("@") > 1:
        raise ContractError(f"{field} must be operation or operation@variant")
    operation, separator, variant = value.partition("@")
    if separator and not variant:
        raise ContractError(f"{field} must include a variant after '@'")
    return _name(operation, f"{field} operation"), (
        _name(variant, f"{field} variant") if separator else None
    )


def _fileset_sources(
    fileset_names: tuple[str, ...],
    *,
    component_filesets: Mapping[str, tuple[PurePosixPath, ...]],
    owner_root: Path,
    project_root: Path,
    field: str,
) -> tuple[Source, ...]:
    selected: list[Source] = []
    seen: set[str] = set()
    for index, raw_name in enumerate(fileset_names):
        name = _name(raw_name, f"{field}[{index}]")
        try:
            paths = component_filesets[name]
        except KeyError as exc:
            raise ContractError(
                f"{field} references unknown component fileset {name!r}; "
                f"available: {sorted(component_filesets)}"
            ) from exc
        if not isinstance(paths, tuple) or not paths:
            raise ContractError(f"component fileset {name!r} is empty or malformed")
        for relative in paths:
            if not isinstance(relative, PurePosixPath):
                raise ContractError(f"component fileset {name!r} is malformed")
            path = project_root.joinpath(*relative.parts)
            resolved = path.resolve()
            if (
                path.absolute() != resolved
                or not resolved.is_relative_to(owner_root)
                or not resolved.is_file()
            ):
                raise ContractError(
                    f"component fileset {name!r} contains unsafe owner source: {relative}"
                )
            source = Source.capture(resolved, root=owner_root, scope="owner")
            if source.path not in seen:
                selected.append(source)
                seen.add(source.path)
    return tuple(selected)


def _step(
    raw: Mapping[str, Any],
    *,
    field: str,
    component_filesets: Mapping[str, tuple[PurePosixPath, ...]],
    owner_root: Path,
    project_root: Path,
    runtime_profiles: Mapping[str, RuntimeEnvironment],
    runtime_defaults: Mapping[str, str],
    default_id: str | None = None,
) -> tuple[Step, tuple[Source, ...]]:
    unknown = set(raw) - _STEP_FIELDS
    if unknown:
        raise ContractError(f"{field} contains unknown fields: {sorted(unknown)}")
    step_id = default_id if default_id is not None else _name(raw.get("id"), f"{field}.id")
    uses = raw.get("uses")
    if not isinstance(uses, str):
        raise ContractError(f"{field}.uses must be an adapter identity")
    filesets = _strings(raw.get("filesets"), f"{field}.filesets", required=True)
    sources = _fileset_sources(
        filesets,
        component_filesets=component_filesets,
        owner_root=owner_root,
        project_root=project_root,
        field=f"{field}.filesets",
    )
    step = Step(
        id=step_id,
        uses=uses,
        config=_config(raw.get("config"), f"{field}.config"),
        needs=_strings(raw.get("needs"), f"{field}.needs"),
        sources=tuple(source.path for source in sources),
        evidence=_evidence(raw.get("evidence"), f"{field}.evidence"),
        runtime=_runtime(
            raw.get("runtime"),
            uses=uses,
            profiles=runtime_profiles,
            defaults=runtime_defaults,
            field=f"{field}.runtime",
        ),
    )
    return _prepare_step(step, source_snapshots=sources), sources


def compile_operation(
    catalog_path: Path,
    *,
    project_identity: str,
    owner: str,
    owner_root: Path,
    project_root: Path,
    component_filesets: Mapping[str, tuple[PurePosixPath, ...]],
    operation: str,
    variant: str | None = None,
) -> ExecutionPlan:
    """Compile ``owner:operation[@variant]`` from one owner catalog."""

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
        "schema": 3,
        "contract_kind": "owner-operations",
        "path_scope": "owner",
        "owner": owner,
    }
    if any(raw.get(name) != value for name, value in expected_header.items()):
        raise ContractError(
            f"{path}: expected schema=3, contract_kind='owner-operations', "
            f"path_scope='owner', owner={owner!r}"
        )
    unknown = set(raw) - _HEADER - {"runtime", "runtime_defaults", "operations"}
    if unknown:
        raise ContractError(f"{path}: unknown owner operation fields: {sorted(unknown)}")
    runtime_profiles = _runtime_profiles(raw.get("runtime"))
    runtime_defaults_raw = _table(
        raw.get("runtime_defaults", {}), "runtime_defaults"
    )
    runtime_defaults: dict[str, str] = {}
    for adapter, profile in runtime_defaults_raw.items():
        identity = adapter_identity(adapter)
        profile_name = _name(
            profile, f"runtime_defaults.{adapter}"
        )
        if profile_name not in runtime_profiles:
            raise ContractError(
                f"runtime_defaults.{identity} references unknown profile "
                f"{profile_name!r}"
            )
        runtime_defaults[identity] = profile_name
    definitions = _table(raw.get("operations"), "operations")
    identities: dict[str, tuple[str, str | None, Mapping[str, Any]]] = {}
    for raw_identity, value in definitions.items():
        parsed_operation, parsed_variant = _operation_identity(
            raw_identity, "operation identity"
        )
        canonical = parsed_operation + (
            "" if parsed_variant is None else f"@{parsed_variant}"
        )
        if canonical != raw_identity:
            raise ContractError(f"operation identity is not canonical: {raw_identity!r}")
        definition = _table(value, f"operations.{raw_identity}")
        if canonical in identities:
            raise ContractError(f"duplicate operation identity: {canonical!r}")
        identities[canonical] = (parsed_operation, parsed_variant, definition)

    operation_name = _name(operation, "operation")
    variant_name = None if variant is None else _name(variant, "variant")
    identity = operation_name + ("" if variant_name is None else f"@{variant_name}")
    try:
        _, _, definition = identities[identity]
    except KeyError as exc:
        raise ContractError(
            f"unknown operation {identity!r}; available: {sorted(identities)}"
        ) from exc

    uses = definition.get("uses")
    steps_raw = definition.get("steps")
    if (uses is None) == (steps_raw is None):
        raise ContractError(
            f"operation {identity!r} must declare exactly one of uses or steps"
        )
    compiled: list[tuple[Step, tuple[Source, ...]]] = []
    if uses is not None:
        unknown_direct = set(definition) - _DIRECT_FIELDS
        if unknown_direct:
            raise ContractError(
                f"operation {identity!r} contains unknown fields: {sorted(unknown_direct)}"
            )
        compiled.append(
            _step(
                {
                    "uses": uses,
                    "filesets": definition.get("filesets"),
                    "runtime": definition.get("runtime"),
                    "config": definition.get("config"),
                    "evidence": definition.get("evidence"),
                },
                field=f"operations.{identity}",
                component_filesets=component_filesets,
                owner_root=root,
                project_root=repository_root,
                runtime_profiles=runtime_profiles,
                runtime_defaults=runtime_defaults,
                default_id="run",
            )
        )
    else:
        unknown_graph = set(definition) - _GRAPH_FIELDS
        if unknown_graph:
            raise ContractError(
                f"operation {identity!r} graph contains unsupported shared fields: "
                f"{sorted(unknown_graph)}"
            )
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ContractError(f"operations.{identity}.steps must be a non-empty array")
        compiled.extend(
            _step(
                _table(value, f"operations.{identity}.steps[{index}]"),
                field=f"operations.{identity}.steps[{index}]",
                component_filesets=component_filesets,
                owner_root=root,
                project_root=repository_root,
                runtime_profiles=runtime_profiles,
                runtime_defaults=runtime_defaults,
            )
            for index, value in enumerate(steps_raw)
        )

    catalog_source = Source.capture(path, root=root, scope="owner")
    if catalog_source.read_text() != record_text:
        raise ContractError("operation catalog changed while it was being parsed")
    unique_sources: dict[str, Source] = {catalog_source.path: catalog_source}
    for _step_value, sources in compiled:
        for source in sources:
            unique_sources.setdefault(source.path, source)
    return ExecutionPlan(
        project_identity=project_identity,
        owner=owner,
        operation=operation_name,
        variant=variant_name,
        steps=tuple(step for step, _sources in compiled),
        sources=tuple(unique_sources.values()),
        resources=(),
    )


def parse_selector(value: str) -> tuple[str, str, str | None]:
    """Parse the canonical ``owner:operation[@variant]`` selector."""

    if not isinstance(value, str) or value.count(":") != 1 or "/" in value:
        raise ContractError("operation selector must be owner:operation[@variant]")
    owner, identity = value.split(":", 1)
    operation, variant = _operation_identity(identity, "operation selector")
    return _name(owner, "owner"), operation, variant


__all__ = ["compile_operation", "parse_selector"]
