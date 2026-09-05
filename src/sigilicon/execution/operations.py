"""Compile one explicitly named owner operation from component filesets."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.contracts import contract_schema
from sigilicon.execution._values import ContractError, adapter_identity
from sigilicon.execution._plan import Evidence, ExecutionPlan, RuntimeEnvironment, Step
from sigilicon.execution._source import Source
from sigilicon.paths import validate_artifact_component
from sigilicon.source import ComponentFilesetReference

if TYPE_CHECKING:
    from sigilicon.project import Project


_HEADER = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_DIRECT_FIELDS = frozenset(
    {"uses", "filesets", "runtime", "config_profile", "config", "evidence"}
)
_GRAPH_FIELDS = frozenset({"steps"})
_STEP_FIELDS = frozenset(
    {
        "id",
        "uses",
        "needs",
        "filesets",
        "runtime",
        "config_profile",
        "config",
        "evidence",
    }
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


def _fileset_references(
    value: object,
    field: str,
) -> tuple[ComponentFilesetReference, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} must be a non-empty array of component filesets")
    references: list[ComponentFilesetReference] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ContractError(
                f"{field}[{index}] must be a component/fileset table"
            )
        if set(raw) != {"component", "fileset"}:
            raise ContractError(
                f"{field}[{index}] must contain exactly component and fileset"
            )
        try:
            references.append(
                ComponentFilesetReference(
                    _name(raw.get("component"), f"{field}[{index}].component"),
                    _name(raw.get("fileset"), f"{field}[{index}].fileset"),
                )
            )
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    result = tuple(references)
    if len(result) != len(set(result)):
        raise ContractError(f"{field} contains duplicate component filesets")
    return result


def _config_profiles(value: object) -> dict[str, Mapping[str, Any]]:
    raw = _table({} if value is None else value, "config_profiles")
    return {
        _name(name, "config profile"): dict(
            _table(profile, f"config_profiles.{name}")
        )
        for name, profile in raw.items()
    }


def _config(
    value: object,
    profile: object,
    *,
    profiles: Mapping[str, Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    override = dict(_table({} if value is None else value, field))
    if profile is None:
        return override
    name = _name(profile, f"{field}_profile")
    try:
        base = profiles[name]
    except KeyError as exc:
        raise ContractError(
            f"{field}_profile references unknown config profile {name!r}; "
            f"available: {sorted(profiles)}"
        ) from exc
    return {**base, **override}


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
    project: Project,
    owner: str,
    fileset_references: tuple[ComponentFilesetReference, ...],
    *,
    field: str,
) -> tuple[Source, ...]:
    try:
        return project.resolve_source_filesets(owner, fileset_references)
    except (ContractError, ValueError) as exc:
        raise ContractError(f"{field} contains an invalid component fileset: {exc}") from exc


def _step(
    project: Project,
    owner: str,
    raw: Mapping[str, Any],
    *,
    field: str,
    runtime_profiles: Mapping[str, RuntimeEnvironment],
    runtime_defaults: Mapping[str, str],
    config_profiles: Mapping[str, Mapping[str, Any]],
    default_id: str | None = None,
) -> tuple[Step, tuple[Source, ...]]:
    unknown = set(raw) - _STEP_FIELDS
    if unknown:
        raise ContractError(f"{field} contains unknown fields: {sorted(unknown)}")
    step_id = default_id if default_id is not None else _name(raw.get("id"), f"{field}.id")
    uses = raw.get("uses")
    if not isinstance(uses, str):
        raise ContractError(f"{field}.uses must be an adapter identity")
    filesets = _fileset_references(raw.get("filesets"), f"{field}.filesets")
    sources = _fileset_sources(
        project,
        owner,
        filesets,
        field=f"{field}.filesets",
    )
    step = Step(
        id=step_id,
        uses=uses,
        config=_config(
            raw.get("config"),
            raw.get("config_profile"),
            profiles=config_profiles,
            field=f"{field}.config",
        ),
        needs=_strings(raw.get("needs"), f"{field}.needs"),
        evidence=_evidence(raw.get("evidence"), f"{field}.evidence"),
        runtime=_runtime(
            raw.get("runtime"),
            uses=uses,
            profiles=runtime_profiles,
            defaults=runtime_defaults,
            field=f"{field}.runtime",
        ),
        source_closure=sources,
    )
    return step, sources


def _compile_operation(
    project: Project,
    *,
    owner: str,
    operation: str,
    variant: str | None = None,
) -> ExecutionPlan:
    """Compile ``owner:operation[@variant]`` from one owner catalog."""

    selected_owner = project.owner(owner)
    relative = selected_owner.component.operation_catalog
    if relative is None:
        raise ValueError(f"owner {selected_owner.name!r} has no operation catalog")
    path = project.project_root.joinpath(*relative.parts).absolute()
    root = selected_owner.root.resolve()
    if path.resolve() != path or not path.is_relative_to(root):
        raise ContractError("operation catalog must be a non-symlink owner source")
    try:
        record_text = read_nofollow_text(path)
        raw = tomllib.loads(record_text)
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"cannot read operation catalog {path}: {exc}") from exc
    schema = contract_schema("owner-operations")
    expected_header = {
        "schema": schema,
        "contract_kind": "owner-operations",
        "path_scope": "owner",
        "owner": owner,
    }
    if any(raw.get(name) != value for name, value in expected_header.items()):
        raise ContractError(
            f"{path}: expected schema={schema}, contract_kind='owner-operations', "
            f"path_scope='owner', owner={owner!r}"
        )
    unknown = set(raw) - _HEADER - {
        "runtime",
        "runtime_defaults",
        "config_profiles",
        "operations",
    }
    if unknown:
        raise ContractError(f"{path}: unknown owner operation fields: {sorted(unknown)}")
    runtime_profiles = _runtime_profiles(raw.get("runtime"))
    config_profiles = _config_profiles(raw.get("config_profiles"))
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
                project,
                owner,
                {
                    "uses": uses,
                    "filesets": definition.get("filesets"),
                    "runtime": definition.get("runtime"),
                    "config_profile": definition.get("config_profile"),
                    "config": definition.get("config"),
                    "evidence": definition.get("evidence"),
                },
                field=f"operations.{identity}",
                runtime_profiles=runtime_profiles,
                runtime_defaults=runtime_defaults,
                config_profiles=config_profiles,
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
                project,
                owner,
                _table(value, f"operations.{identity}.steps[{index}]"),
                field=f"operations.{identity}.steps[{index}]",
                runtime_profiles=runtime_profiles,
                runtime_defaults=runtime_defaults,
                config_profiles=config_profiles,
            )
            for index, value in enumerate(steps_raw)
        )

    catalog_source = Source.capture(path, root=root, scope="owner")
    if catalog_source.read_text() != record_text:
        raise ContractError("operation catalog changed while it was being parsed")
    unique_sources: dict[tuple[Path, str], Source] = {
        (catalog_source.root, catalog_source.path): catalog_source
    }
    for _step_value, sources in compiled:
        for source in sources:
            unique_sources.setdefault((source.root, source.path), source)
    return ExecutionPlan(
        project_identity=project.operation_identity(selected_owner.name),
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


__all__ = ["parse_selector"]
