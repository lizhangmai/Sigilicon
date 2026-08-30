"""Project-owned target catalog for stable layout workflow entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.repository import OwnerCatalogSnapshot, Project

_TARGET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_ACTIONS = frozenset({"check", "generate", "verify"})
_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset({"description", "spec", "actions"})


@dataclass(frozen=True)
class LayoutTarget:
    name: str
    owner: str
    description: str
    spec: Path
    spec_relative: Path
    actions: tuple[str, ...]

    def supports(self, action: str) -> bool:
        return action in self.actions


@dataclass(frozen=True)
class LayoutTargetCatalog:
    paths: tuple[Path, ...]
    project: Project
    targets: tuple[LayoutTarget, ...]

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    def get(self, name: str, *, action: str | None = None) -> LayoutTarget:
        try:
            target = next(item for item in self.targets if item.name == name)
        except StopIteration as exc:
            available = ", ".join(item.name for item in self.targets)
            raise ValueError(
                f"unknown layout target {name!r}; available targets: {available}"
            ) from exc
        if action is not None and not target.supports(action):
            supported = ", ".join(target.actions)
            raise ValueError(
                f"layout target {name!r} does not support {action!r}; "
                f"supported actions: {supported}"
            )
        return target


def _actions(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty string array")
    result = tuple(value)
    if any(not isinstance(item, str) or item not in _ACTIONS for item in result):
        raise ValueError(f"{field} must contain only {sorted(_ACTIONS)}")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def load_layout_target_catalog(
    project: Project,
    *,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...] | None = None,
) -> LayoutTargetCatalog:
    """Load every selected layout target catalog, or an empty optional domain."""

    repository = project
    root = repository.project_root
    catalogs = repository.flow_catalog_snapshots(
        "layout_targets",
        inventory=catalog_inventory,
    )
    targets: list[LayoutTarget] = []
    names: set[str] = set()
    for catalog in catalogs:
        owner = catalog.owner
        catalog_path = catalog.path
        raw = catalog.document
        require_config_header(
            raw,
            catalog_path,
            contract_kind="flow-layout-registry",
            path_scope="owner",
            owner=owner,
        )
        unknown = set(raw) - _HEADER_FIELDS - {"targets"}
        if unknown:
            raise ValueError(
                f"layout target catalog contains unknown fields: {sorted(unknown)}"
            )
        rows = raw.get("targets")
        if not isinstance(rows, Mapping):
            raise ValueError("layout target catalog targets must be a table")
        for name, row in rows.items():
            field = f"targets.{name}"
            if not isinstance(name, str) or _TARGET_NAME_RE.fullmatch(name) is None:
                raise ValueError(f"layout target name must match {_TARGET_NAME_RE.pattern!r}")
            if name in names:
                raise ValueError(f"duplicate layout target across owner catalogs: {name}")
            names.add(name)
            if not isinstance(row, Mapping):
                raise ValueError(f"{field} must be a table")
            unknown = set(row) - _TARGET_FIELDS
            if unknown:
                raise ValueError(f"{field} contains unknown fields: {sorted(unknown)}")
            description = row.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"{field}.description must be a non-empty string")
            spec, spec_relative = repository.resolve_owner_file(
                owner, row.get("spec"), f"{field}.spec"
            )
            targets.append(
                LayoutTarget(
                    name=name,
                    owner=owner,
                    description=description.strip(),
                    spec=spec,
                    spec_relative=spec_relative,
                    actions=_actions(row.get("actions"), f"{field}.actions"),
                )
            )
    return LayoutTargetCatalog(
        tuple(catalog.path for catalog in catalogs),
        repository,
        tuple(targets),
    )
