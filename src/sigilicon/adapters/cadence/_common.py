"""Shared source, resource, and action binding for Cadence adapters."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import require_relative_path
from sigilicon.domain.oa_library import find_oa_assembly
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution._model import (
    Artifact,
    ContractError,
    ExecutionError,
    PlannedAction,
    ResourceBinding,
    PreflightCheck,
    Resources,
    Source,
    Step,
    ExecutionIO,
    StepResult,
)
from sigilicon.external_tools import (
    CADENCE_SPECTRE_TOOL,
    CADENCE_SPICEIN_TOOL,
    CADENCE_TEXT_IMPORT_TOOL,
    CADENCE_VIRTUOSO_TOOL,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.project import Project
from sigilicon.virtuoso.bridge import (
    VIRTUOSO_BRIDGE_HOST,
    VIRTUOSO_BRIDGE_PORT,
)


_XRUN = "cadence.xrun"
_XSTREAM = "cadence.xstream"
_CALIBRE = "mentor.calibre"
_PYTHON = "runtime.python"
_OA_CAPABILITIES = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})
_OA_TEXT_VIEW_KINDS = frozenset({"spectre_model", "veriloga", "system_verilog"})
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE_RESOURCES = (VIRTUOSO_BRIDGE_HOST, VIRTUOSO_BRIDGE_PORT)
_SPECTRE_TEMPLATE_TOKEN = re.compile(
    r"\{\{(?:source|file|value):[^{}]+\}\}"
)


def _runtime_bindings(
    resources: Resources,
    *identities: str,
) -> tuple[ResourceBinding, ...]:
    """Capture the exact configured runtime dependencies used by an adapter."""

    return tuple(resources.capture(identity) for identity in identities)


def _strict_config(step: Step, fields: frozenset[str]) -> Mapping[str, Any]:
    config = step.config
    unknown = set(config) - fields
    missing = fields - set(config)
    if unknown or missing:
        raise ContractError(
            f"{step.uses} config fields disagree with its contract; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return config


def _text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise ContractError(f"Cadence step requires non-empty {name!r}")
    return value


def _positive_integer(config: Mapping[str, Any], name: str) -> int:
    value = config.get(name)
    if type(value) is not int or value <= 0:
        raise ContractError(f"Cadence step requires positive integer {name!r}")
    return value


def _strings(config: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = config.get(name)
    if not isinstance(value, tuple) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ContractError(f"Cadence step requires non-empty text array {name!r}")
    if len(value) != len(set(value)):
        raise ContractError(f"Cadence step {name!r} contains duplicates")
    return value


def _relative(value: str, label: str) -> str:
    try:
        return require_relative_path(value, label).as_posix()
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _capability_checks(
    resources: Resources,
    required: frozenset[str],
) -> tuple[PreflightCheck, ...]:
    return tuple(
        PreflightCheck(
            "runtime-capability",
            capability,
            "ready" if capability in resources.capabilities else "blocked",
            "declared by the project runtime"
            if capability in resources.capabilities
            else "missing capability",
        )
        for capability in sorted(required)
    )


def _configured_executable(resources: Resources, name: str) -> Path | None:
    return resources.configured_tool(name)


def _executable_check(resources: Resources, name: str) -> PreflightCheck:
    path = _configured_executable(resources, name)
    ready = path is not None
    return PreflightCheck(
        "runtime-resource",
        name,
        "ready" if ready else "blocked",
        "executable declared by the project runtime" if ready else "missing executable",
    )


def _bridge_check(resources: Resources) -> PreflightCheck:
    from sigilicon.virtuoso.bridge import bridge_endpoint

    try:
        host, port = bridge_endpoint(resources)
    except (TypeError, ValueError, ContractError) as exc:
        return PreflightCheck(
            "runtime-resource",
            "virtuoso-bridge-endpoint",
            "blocked",
            str(exc),
        )
    return PreflightCheck(
        "runtime-resource",
        "virtuoso-bridge-endpoint",
        "ready",
        f"explicit endpoint {host}:{port}",
    )


def _oa_runtime_executables(planning: Any, operation: str) -> tuple[str, ...]:
    required: list[str] = []
    if operation in {"check", "rebuild"}:
        required.append(_PYTHON)
    if operation == "rebuild":
        if getattr(planning, "designs", ()) or getattr(planning, "testbenches", ()):
            required.append(CADENCE_SPICEIN_TOOL)
        if any(
            item.view.kind in _OA_TEXT_VIEW_KINDS
            for item in getattr(planning, "views", ())
        ):
            required.append(CADENCE_TEXT_IMPORT_TOOL)
        if any(
            item.view.kind in {"system_verilog", "veriloga"}
            for item in getattr(planning, "views", ())
        ):
            required.append(_XRUN)
    return tuple(required)


def _bind_source_paths(
    project: Project,
    owner_name: str,
    step: Step,
    paths: Mapping[Path, str | Source] | tuple[Path, ...] | frozenset[Path],
) -> Mapping[Path, tuple[str, str]]:
    """Bind Project-owned inputs; package code and PDK files stay runtime resources."""

    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    artifact_root = project.artifact_root.resolve()
    selected: dict[Path, tuple[str, str]] = {}
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"adapter source must not traverse a symlink: {path}")
        if path.is_relative_to(artifact_root):
            continue
        if path.is_relative_to(owner_root):
            name = path.relative_to(owner_root).as_posix()
        elif path.is_relative_to(project_root):
            name = path.relative_to(project_root).as_posix()
        else:
            continue
        snapshot = None
        if isinstance(paths, Mapping):
            expected = paths[source]
            if isinstance(expected, Source):
                if expected.location != path or not expected.current():
                    raise ContractError(f"typed adapter source snapshot drift: {path}")
                digest = expected.sha256
            else:
                snapshot = read_nofollow_text(path)
                if snapshot != expected:
                    raise ContractError(f"typed adapter source snapshot drift: {path}")
                digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        else:
            snapshot = read_nofollow_text(path)
            digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        selected[path] = (
            name,
            digest,
        )
    return MappingProxyType(selected)


def _validate_oa_plan_sources(
    project: Project,
    owner_name: str,
    planning: Any,
    paths: frozenset[Path],
) -> Mapping[Path, Source]:
    from sigilicon.adapters.cadence.oa_library import validate_oa_plan_source_members

    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    members: list[Source] = []
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"OA plan source must not traverse a symlink: {path}")
        if path.is_relative_to(owner_root):
            root, scope = owner_root, "owner"
        elif path.is_relative_to(project_root):
            root, scope = project_root, "project"
        else:
            root, scope = path.parent, "resource"
        members.append(Source.capture(path, root=root, scope=scope))
    try:
        validate_oa_plan_source_members(planning, members)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    return MappingProxyType(
        {member.location: member for member in members}
    )


def _require_bound_sources(
    context: ExecutionIO,
    sources: Mapping[Path, tuple[str, str]],
) -> None:
    for name, digest in sources.values():
        current = hashlib.sha256(
            context.source_text(name).encode("utf-8")
        ).hexdigest()
        if current != digest:
            raise ExecutionError(f"sealed adapter source identity drift: {name}")


def _external_file_records(
    project: Project,
    source_records: Mapping[Path, str | Source],
    extra_paths: tuple[Path, ...] = (),
    identities: Mapping[Path, str] = MappingProxyType({}),
) -> tuple[ResourceBinding, ...]:
    project_root = project.project_root.resolve()
    artifact_root = project.artifact_root.resolve()
    selected: dict[str, tuple[Path, str | Source | None]] = {}
    seen: set[Path] = set()
    entries = (
        *((source, False) for source in source_records),
        *((source, True) for source in extra_paths),
    )
    for source, include_project in entries:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"external resource must not traverse a symlink: {path}")
        if (
            not path.is_relative_to(artifact_root)
            and path.is_relative_to(project_root)
            and not include_project
        ):
            continue
        if path in seen:
            continue
        expected = source_records.get(source)
        if path.is_relative_to(artifact_root):
            identity = identities.get(
                path,
                "release:" + path.relative_to(artifact_root).as_posix(),
            )
        elif path.is_relative_to(project_root):
            identity = "project:" + path.relative_to(project_root).as_posix()
        elif path in identities:
            identity = identities[path]
        else:
            raise ContractError(
                f"external resource has no logical identity: {path.name}"
            )
        previous = selected.get(identity)
        if previous is not None and previous[0] != path:
            raise ContractError(f"external resource identity is ambiguous: {identity}")
        seen.add(path)
        selected[identity] = (path, expected)
    resources = tuple(
        ResourceBinding.capture(path, identity=identity)
        for identity, (path, _expected) in sorted(selected.items())
    )
    for resource in resources:
        expected = selected[resource.identity][1]
        if isinstance(expected, Source) and (
            resource.sha256 != expected.sha256
            or resource.record["size"] != expected.size
        ):
            raise ContractError(
                f"external resource changed during planning: {resource.identity}"
            )
        if (
            isinstance(expected, str)
            and expected.encode("utf-8") != resource.read_bytes()
        ):
            raise ContractError(
                f"external resource changed during planning: {resource.identity}"
            )
    return resources


def _oa_resource_identities(
    project: Project,
    planning: Any,
    paths: Mapping[Path, str | Source],
    resources: Resources,
) -> Mapping[Path, str]:
    root = project.project_root.resolve()
    if not any(not Path(path).absolute().is_relative_to(root) for path in paths):
        return MappingProxyType({})
    from sigilicon.domain.platform import load_platform, platform_resource_identities

    pdk = getattr(getattr(planning, "source", None), "pdk", None)
    if not isinstance(pdk, str) or not pdk:
        raise ContractError("OA plan external resources have no platform identity")
    platform = load_platform(project, pdk, resources=resources)
    selected = dict(platform_resource_identities(platform))
    asset_root = platform.asset_root
    if asset_root is not None:
        for source in paths:
            path = Path(source).absolute()
            if path in selected or not path.is_relative_to(asset_root):
                continue
            relative = path.relative_to(asset_root).as_posix()
            basename = re.sub(r"[^A-Za-z0-9._-]", "-", path.name)
            identity_hash = hashlib.sha256(relative.encode("utf-8")).hexdigest()
            selected[path] = (
                f"pdk:{platform.key}:asset/{identity_hash[:20]}-{basename}"
            )
    for source in paths:
        path = Path(source).absolute()
        if path in selected or not path.is_relative_to(_PACKAGE_ROOT):
            continue
        selected[path] = (
            "package:sigilicon/" + path.relative_to(_PACKAGE_ROOT).as_posix()
        )
    return MappingProxyType(selected)


def _captured_project_sources(
    project: Project,
    owner_name: str,
    sources: Mapping[Path, tuple[str, str]],
    records: Mapping[Path, str | Source],
) -> tuple[Source, ...]:
    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    captured: list[Source] = []
    for path in sorted(sources, key=str):
        if path.is_relative_to(owner_root):
            root, scope = owner_root, "owner"
        elif path.is_relative_to(project_root):
            root, scope = project_root, "project"
        else:
            raise ContractError(f"prepared source is outside the Project: {path}")
        existing = records.get(path)
        if isinstance(existing, Source):
            if existing.root != root or existing.scope != scope:
                raise ContractError(f"prepared source has the wrong scope: {path}")
            captured.append(existing)
        else:
            captured.append(Source.capture(path, root=root, scope=scope))
    return tuple(captured)


@dataclass(frozen=True)
class _CadenceInputs:
    """Sealed source, resource, and workspace bindings shared by Cadence actions."""

    plan_identity: str
    runtime_identities: tuple[str, ...]
    sources: Mapping[Path, tuple[str, str]]
    resources: tuple[tuple[Path, str, str], ...]
    workspace_root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan_identity, str)
            or not self.plan_identity.startswith("sha256-")
            or len(self.plan_identity) != 71
        ):
            raise ContractError("Cadence action plan identity is invalid")
        if not isinstance(self.runtime_identities, tuple) or any(
            not isinstance(name, str) or not name
            for name in self.runtime_identities
        ):
            raise ContractError("Cadence action runtime identities are invalid")
        if not isinstance(self.sources, Mapping):
            raise ContractError("prepared Cadence sources must be a mapping")
        if len({identity for _path, identity, _digest in self.resources}) != len(
            self.resources
        ):
            raise ContractError("prepared Cadence resources contain duplicate identities")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "workspace_root", self.workspace_root.resolve())

    @classmethod
    def create(
        cls,
        plan_identity: str,
        runtime_identities: tuple[str, ...],
        sources: Mapping[Path, tuple[str, str]],
        resources: tuple[ResourceBinding, ...],
        workspace_root: Path,
    ) -> "_CadenceInputs":
        return cls(
            plan_identity,
            runtime_identities,
            sources,
            tuple(
                (resource.location, resource.identity, resource.sha256)
                for resource in resources
            ),
            workspace_root,
        )

    @property
    def identity(self) -> str:
        """Return the deterministic identity recorded by the Step."""

        return canonical_digest(self.record)

    @property
    def record(self) -> Mapping[str, Any]:
        return {
            "plan_identity": self.plan_identity,
            "runtime_identities": self.runtime_identities,
            "sources": tuple(
                sorted((name, digest) for name, digest in self.sources.values())
            ),
            "resources": tuple(
                sorted(
                    (identity, digest)
                    for _path, identity, digest in self.resources
                )
            ),
        }

    def validate(self, context: ExecutionIO) -> None:
        _require_bound_sources(context, self.sources)
        for _path, identity, _digest in self.resources:
            context.resource_path(identity)

    def source_paths(self, context: ExecutionIO) -> Mapping[Path, Path]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.source_path(name)
                for original, (name, _digest) in self.sources.items()
            }
        )

    def resource_paths(self, context: ExecutionIO) -> Mapping[Path, Path]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.resource_path(identity)
                for original, identity, _digest in self.resources
            }
        )

    def resource_text(self, context: ExecutionIO) -> Mapping[Path, str]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.resource_text(identity)
                for original, identity, _digest in self.resources
            }
        )

@dataclass(frozen=True)
class _CadencePreparation:
    """Kernel preparation data awaiting one vertical adapter action."""

    inputs: _CadenceInputs
    sources: tuple[Source, ...]
    resources: tuple[ResourceBinding, ...]

    def bind(self, action: PlannedAction) -> AdapterPreparation:
        return AdapterPreparation(
            action=action,
            sources=self.sources,
            resources=self.resources,
        )


def _prepare_cadence_inputs(
    project: Project,
    step: Step,
    resources: Resources,
    *,
    owner: str,
    plan_identity: str,
    source_records: Mapping[Path, str | Source],
    resource_identities: Mapping[Path, str],
    extra_resources: tuple[Path, ...] = (),
    runtime_identities: tuple[str, ...] = (),
) -> _CadencePreparation:
    """Seal common inputs without erasing the vertical action's plan type."""

    sources = _bind_source_paths(project, owner, step, source_records)
    external = _external_file_records(
        project,
        source_records,
        extra_resources,
        identities=resource_identities,
    )
    runtime_bindings = _runtime_bindings(resources, *runtime_identities)
    combined_bindings = tuple(dict.fromkeys((*external, *runtime_bindings)))
    if len({binding.identity for binding in combined_bindings}) != len(
        combined_bindings
    ):
        raise ContractError("Cadence plan binds a runtime identity more than once")
    return _CadencePreparation(
        _CadenceInputs.create(
            plan_identity,
            tuple(binding.identity for binding in runtime_bindings),
            sources,
            external,
            project.workspace_root,
        ),
        _captured_project_sources(
            project,
            owner,
            sources,
            source_records,
        ),
        combined_bindings,
    )
