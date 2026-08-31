"""Canonical owner target declarations.

This module owns the small configuration seam between a repository owner and
the execution planner.  A target is deliberately only an identity,
description, and set of named operations.  Domain-specific execution remains
behind the operation's recipe and is not expanded into design/layout/Flow
registries here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml_record,
    require_config_header,
)
from sigilicon.domain.ip_release import safe_relative
from sigilicon.domain.repository import OwnerTargetSnapshot, Project, RepositoryOwner
from sigilicon.paths import validate_artifact_component


_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_CATALOG_FIELDS = _HEADER_FIELDS | {"targets"}
_TARGET_FIELDS = frozenset({"description", "operations"})
_OPERATION_FIELDS = frozenset({"recipe", "goals"})


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a TOML table")
    return value


def _name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty identifier")
    try:
        return validate_artifact_component(value, field)
    except ValueError as exc:
        raise ValueError(f"{field} must be a non-empty identifier: {value!r}") from exc


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _goals(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty string array")
    goals: list[str] = []
    for index, goal in enumerate(value):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError(f"{field}[{index}] must be a non-empty string")
        if goal in goals:
            raise ValueError(f"{field} contains duplicate goal: {goal!r}")
        goals.append(goal)
    return tuple(goals)


@dataclass(frozen=True)
class TargetOperation:
    """One named operation exposed by an owner target."""

    name: str
    recipe: PurePosixPath
    goals: tuple[str, ...]

    def __post_init__(self) -> None:
        _name(self.name, "target operation name")
        if (
            not isinstance(self.recipe, PurePosixPath)
            or self.recipe.is_absolute()
            or self.recipe.as_posix() != str(self.recipe)
            or any(part in {"", ".", ".."} for part in self.recipe.parts)
        ):
            raise ValueError(
                "target operation recipe must be a canonical relative path"
            )
        if not isinstance(self.goals, tuple) or not self.goals:
            raise ValueError("target operation goals must be a non-empty tuple")
        if any(not isinstance(goal, str) or not goal.strip() for goal in self.goals):
            raise ValueError("target operation goals must be non-empty strings")
        if len(set(self.goals)) != len(self.goals):
            raise ValueError("target operation goals must be unique")


@dataclass(frozen=True)
class ProjectTarget:
    """One owner-local target and its explicitly declared operations."""

    name: str
    description: str
    operations: Mapping[str, TargetOperation]

    def __post_init__(self) -> None:
        _name(self.name, "target name")
        _string(self.description, "target description")
        if not isinstance(self.operations, Mapping) or not self.operations:
            raise ValueError("target operations must be a non-empty mapping")
        if any(
            not isinstance(operation, TargetOperation)
            or not isinstance(name, str)
            or operation.name != name
            for name, operation in self.operations.items()
        ):
            raise ValueError("target operations must be named TargetOperation values")
        object.__setattr__(
            self,
            "operations",
            MappingProxyType(dict(self.operations)),
        )

    def operation(self, name: str) -> TargetOperation:
        """Return one operation or explain the available operations."""

        operation_name = _name(name, "target operation name")
        try:
            return self.operations[operation_name]
        except KeyError as exc:
            available = ", ".join(self.operations)
            raise ValueError(
                f"target {self.name!r} has no operation {operation_name!r}; "
                f"available operations: {available}"
            ) from exc


@dataclass(frozen=True)
class OwnerTargetCatalog:
    """The validated target declarations selected by one repository owner."""

    path: Path
    owner: str
    targets: Mapping[str, ProjectTarget]
    document: Mapping[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.path != self.path.resolve() or not self.path.is_file():
            raise ValueError("owner target catalog path must be a regular resolved file")
        _name(self.owner, "target catalog owner")
        if not isinstance(self.targets, Mapping):
            raise ValueError("target catalog targets must be a mapping")
        if any(
            not isinstance(target, ProjectTarget)
            or not isinstance(name, str)
            or target.name != name
            for name, target in self.targets.items()
        ):
            raise ValueError("target catalog targets must be named ProjectTarget values")
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        if not is_frozen_toml_document(self.document):
            raise ValueError("owner target catalog document must be frozen")

    def get(self, name: str) -> ProjectTarget:
        """Return one target or explain the available target identities."""

        target_name = _name(name, "target name")
        try:
            return self.targets[target_name]
        except KeyError as exc:
            available = ", ".join(self.targets)
            raise ValueError(
                f"unknown target {target_name!r} for owner {self.owner!r}; "
                f"available targets: {available}"
            ) from exc


def _catalog_path(project: Project, owner: RepositoryOwner) -> Path:
    relative = owner.component.target_catalog
    if relative is None:
        raise ValueError(f"cataloged owner {owner.name!r} has no target_catalog")
    if relative.suffix != ".toml":
        raise ValueError(
            f"target_catalog for owner {owner.name!r} must name a TOML file"
        )
    configured = project.project_root.joinpath(*relative.parts)
    resolved = configured.resolve()
    if configured != resolved:
        raise ValueError(
            f"target_catalog for owner {owner.name!r} must not be a symlink"
        )
    if not resolved.is_relative_to(project.project_root):
        raise ValueError(
            f"target_catalog for owner {owner.name!r} must stay inside the project root"
        )
    if not resolved.is_relative_to(owner.root):
        raise ValueError(
            f"target_catalog for owner {owner.name!r} must stay inside its owner root"
        )
    if not resolved.is_file():
        raise FileNotFoundError(
            f"target_catalog for owner {owner.name!r} does not exist: {relative}"
        )
    return resolved


def _snapshot(
    project: Project,
    owner: RepositoryOwner,
    snapshot: OwnerTargetSnapshot,
) -> OwnerTargetSnapshot:
    path = _catalog_path(project, owner)
    if (
        not isinstance(snapshot, OwnerTargetSnapshot)
        or snapshot.owner != owner.name
        or snapshot.path != path
        or snapshot.contract_kind != "owner-targets"
        or not isinstance(snapshot.record_text, str)
        or not is_frozen_toml_document(snapshot.document)
    ):
        raise ValueError("owner target catalog snapshot identity drift")
    require_config_header(
        snapshot.document,
        snapshot.path,
        contract_kind="owner-targets",
        path_scope="owner",
        owner=owner.name,
    )
    return snapshot


def _flow_files(project: Project, owner: RepositoryOwner) -> tuple[Path, ...]:
    """Resolve the declared flow fileset without following symlinks."""

    values = owner.component.filesets.get("flow")
    if values is None:
        raise ValueError(
            f"owner {owner.name!r} must declare a flow fileset for target recipes"
        )
    result: list[Path] = []
    for relative in values:
        configured = project.project_root.joinpath(*relative.parts)
        resolved = configured.resolve()
        if configured != resolved:
            raise ValueError(
                f"owner {owner.name!r} flow fileset contains a symlink: {relative}"
            )
        if not resolved.is_relative_to(owner.root):
            raise ValueError(
                f"owner {owner.name!r} flow fileset escapes its owner root: "
                f"{relative}"
            )
        if not resolved.is_file():
            raise FileNotFoundError(
                f"owner {owner.name!r} flow fileset source is missing: {relative}"
            )
        result.append(resolved)
    return tuple(sorted(result))


def _recipe(
    project: Project,
    owner: RepositoryOwner,
    value: object,
    field: str,
    flow_files: tuple[Path, ...],
    documents: dict[Path, Mapping[str, Any]],
) -> PurePosixPath:
    relative = safe_relative(value, field)
    if relative.suffix != ".toml":
        raise ValueError(f"{field} must name a TOML execution recipe")
    path = owner.root.joinpath(*relative.parts)
    resolved = path.resolve()
    if path != resolved:
        raise ValueError(f"{field} must not name a symlink")
    if not resolved.is_relative_to(owner.root):
        raise ValueError(f"{field} must stay inside owner {owner.name!r} root")
    if not resolved.is_file():
        raise FileNotFoundError(f"{field} does not exist: {relative}")
    if resolved not in flow_files:
        raise ValueError(
            f"{field} must be declared in owner {owner.name!r} flow fileset"
        )
    document = documents.get(resolved)
    if document is None:
        document, _record_text = read_toml_record(resolved)
        document = freeze_toml_document(document)
        documents[resolved] = document
    require_config_header(
        document,
        resolved,
        contract_kind="execution-recipe",
        path_scope="owner",
        owner=owner.name,
    )
    return relative


def parse_owner_target_catalog(
    project: Project,
    owner: RepositoryOwner | str,
    snapshot: OwnerTargetSnapshot,
) -> OwnerTargetCatalog:
    """Parse one already selected and source-bound target catalog."""

    selected = project.owner(owner) if isinstance(owner, str) else owner
    if selected not in project.owners:
        raise ValueError(f"repository does not contain owner {selected.name!r}")
    selected_snapshot = _snapshot(project, selected, snapshot)
    raw = selected_snapshot.document
    require_config_header(
        raw,
        selected_snapshot.path,
        contract_kind="owner-targets",
        path_scope="owner",
        owner=selected.name,
    )
    unknown = set(raw) - _CATALOG_FIELDS
    if unknown:
        raise ValueError(
            f"owner target catalog contains unknown fields: {sorted(unknown)}"
        )
    targets_raw = _table(raw.get("targets"), "target catalog targets")
    flow_files: tuple[Path, ...] | None = None
    recipe_documents: dict[Path, Mapping[str, Any]] = {}
    targets: dict[str, ProjectTarget] = {}
    for target_name_raw, target_value in targets_raw.items():
        target_name = _name(target_name_raw, "target name")
        if target_name in targets:
            raise ValueError(f"duplicate target name: {target_name}")
        field_name = f"targets.{target_name}"
        target = _table(target_value, field_name)
        unknown = set(target) - _TARGET_FIELDS
        if unknown:
            raise ValueError(
                f"{field_name} contains unknown fields: {sorted(unknown)}"
            )
        description = _string(target.get("description"), f"{field_name}.description")
        operations_raw = _table(target.get("operations"), f"{field_name}.operations")
        if not operations_raw:
            raise ValueError(f"{field_name}.operations must not be empty")
        operations: dict[str, TargetOperation] = {}
        for operation_name_raw, operation_value in operations_raw.items():
            operation_name = _name(
                operation_name_raw,
                f"{field_name}.operations name",
            )
            if operation_name in operations:
                raise ValueError(
                    f"{field_name} contains duplicate operation: {operation_name}"
                )
            operation_field = f"{field_name}.operations.{operation_name}"
            operation = _table(operation_value, operation_field)
            unknown = set(operation) - _OPERATION_FIELDS
            if unknown:
                raise ValueError(
                    f"{operation_field} contains unknown fields: {sorted(unknown)}"
                )
            if flow_files is None:
                flow_files = _flow_files(project, selected)
            operations[operation_name] = TargetOperation(
                name=operation_name,
                recipe=_recipe(
                    project,
                    selected,
                    operation.get("recipe"),
                    f"{operation_field}.recipe",
                    flow_files,
                    recipe_documents,
                ),
                goals=_goals(operation.get("goals"), f"{operation_field}.goals"),
            )
        targets[target_name] = ProjectTarget(
            name=target_name,
            description=description,
            operations=MappingProxyType(dict(operations)),
        )
    return OwnerTargetCatalog(
        path=selected_snapshot.path,
        owner=selected.name,
        targets=MappingProxyType(dict(targets)),
        document=freeze_toml_document(raw),
    )


def load_owner_target_catalog(
    project: Project,
    owner: RepositoryOwner | str,
    *,
    catalog_snapshot: OwnerTargetSnapshot | None = None,
) -> OwnerTargetCatalog:
    """Load one owner's explicit target catalog and validate its recipes."""

    selected = project.owner(owner) if isinstance(owner, str) else owner
    if selected not in project.owners:
        raise ValueError(f"repository does not contain owner {selected.name!r}")
    snapshot = (
        project.owner_target_catalog(selected)
        if catalog_snapshot is None
        else catalog_snapshot
    )
    return parse_owner_target_catalog(project, selected, snapshot)


__all__ = [
    "OwnerTargetCatalog",
    "ProjectTarget",
    "TargetOperation",
    "load_owner_target_catalog",
    "parse_owner_target_catalog",
]
