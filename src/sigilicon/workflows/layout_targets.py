"""Project-owned target catalog for stable layout workflow entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TYPE_CHECKING, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.repository import OwnerCatalogSnapshot, Project
from sigilicon.flow.model import SourceMember
from sigilicon.flow.source_assets import snapshot_source_member

if TYPE_CHECKING:
    from sigilicon.workflows.layout_generation import LayoutPlanningResult

_TARGET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_ACTIONS = frozenset({"check", "generate", "verify"})
_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset({"description", "spec", "actions", "routes"})
_ROUTE_NAMES = frozenset({"generate", "verify-drc", "verify-lvs", "verify-all"})


@dataclass(frozen=True)
class LayoutRoute:
    operation: str
    flow: str
    target: str


@dataclass(frozen=True)
class LayoutTarget:
    name: str
    owner: str
    description: str
    spec: Path
    spec_relative: Path
    actions: tuple[str, ...]
    routes: tuple[LayoutRoute, ...]

    def supports(self, action: str) -> bool:
        return action in self.actions

    def get_route(self, operation: str) -> LayoutRoute:
        try:
            return next(route for route in self.routes if route.operation == operation)
        except StopIteration as exc:
            available = ", ".join(route.operation for route in self.routes)
            raise ValueError(
                f"layout target {self.name!r} has no route {operation!r}; "
                f"available routes: {available}"
            ) from exc


@dataclass(frozen=True)
class LayoutTargetCatalog:
    paths: tuple[Path, ...]
    project: Project
    targets: tuple[LayoutTarget, ...]
    catalog_members: tuple[SourceMember, ...]
    inventory: tuple[OwnerCatalogSnapshot, ...]

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

    def for_owner(self, owner: str) -> LayoutTargetCatalog:
        targets = tuple(target for target in self.targets if target.owner == owner)
        inventory = tuple(item for item in self.inventory if item.owner == owner)
        catalog_members = tuple(
            member
            for member in self.catalog_members
            if any(member.location == item.path for item in inventory)
        )
        return LayoutTargetCatalog(
            tuple(member.location for member in catalog_members),
            self.project,
            targets,
            catalog_members,
            inventory,
        )

    def source_members_for(
        self,
        target: LayoutTarget,
        planning: LayoutPlanningResult,
    ) -> tuple[SourceMember, ...]:
        """Snapshot the exact route, intent and Git-owned implementation inputs."""

        if target not in self.targets or planning.spec.path != target.spec:
            raise ValueError("layout planning result does not belong to this target")
        catalog_member = next(
            member
            for member in self.catalog_members
            if member.location in self.paths
            and self.project.owner_for(member.location).name == target.owner
        )
        spec = planning.spec
        paths = {
            *spec.source_documents,
            *spec.pdk.source_documents,
            spec.generator_source,
            *spec.generator_dependencies,
            *spec.generator_module_sources,
            *(snapshot.source_path for snapshot in spec.source_snapshots),
        }
        if spec.oa_assembly_manifest is not None:
            paths.add(spec.oa_assembly_manifest)
        if spec.physical_verification is not None:
            paths.add(spec.physical_verification.path)
        members = [catalog_member]
        for path in sorted(paths):
            source_root = _source_root(path, self.project.project_root)
            members.append(
                snapshot_source_member(
                    path,
                    source_root=source_root,
                    source_label="layout",
                )
            )
        unique: dict[tuple[Path, str], SourceMember] = {}
        for member in members:
            unique[(member.source_root, member.path)] = member
        return tuple(unique.values())


def _actions(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty string array")
    result = tuple(value)
    if any(not isinstance(item, str) or item not in _ACTIONS for item in result):
        raise ValueError(f"{field} must contain only {sorted(_ACTIONS)}")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _routes(
    value: object,
    field: str,
    actions: tuple[str, ...],
) -> tuple[LayoutRoute, ...]:
    required = set()
    if "generate" in actions:
        required.add("generate")
    if "verify" in actions:
        required.update({"verify-drc", "verify-lvs", "verify-all"})
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError(f"{field} must map exactly {sorted(required)}")
    routes: list[LayoutRoute] = []
    for operation, route in value.items():
        if operation not in _ROUTE_NAMES:
            raise ValueError(f"{field} contains unknown operation {operation!r}")
        if (
            not isinstance(route, (list, tuple))
            or len(route) != 2
            or any(
                not isinstance(item, str) or _TARGET_NAME_RE.fullmatch(item) is None
                for item in route
            )
        ):
            raise ValueError(f"{field}.{operation} must be [flow, target] identifiers")
        routes.append(LayoutRoute(operation, route[0], route[1]))
    return tuple(routes)


def _source_root(path: Path, project_root: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(project_root):
        return project_root
    package_root = Path(__file__).resolve().parents[2]
    if resolved.is_relative_to(package_root):
        return package_root
    raise ValueError(f"layout source is outside project and Sigilicon roots: {resolved}")


def load_layout_target_catalog(
    project: Project,
    *,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...] | None = None,
) -> LayoutTargetCatalog:
    """Load every selected layout target catalog, or an empty optional domain."""

    repository = project
    root = repository.project_root
    inventory = (
        repository.flow_catalog_inventory()
        if catalog_inventory is None
        else catalog_inventory
    )
    catalogs = repository.flow_catalog_snapshots(
        "layout_targets",
        inventory=inventory,
    )
    targets: list[LayoutTarget] = []
    catalog_members: list[SourceMember] = []
    names: set[str] = set()
    for catalog in catalogs:
        owner = catalog.owner
        catalog_path = catalog.path
        catalog_member = snapshot_source_member(
            catalog_path,
            source_root=root,
            record_text=catalog.record_text,
            source_label="layout",
        )
        catalog_members.append(catalog_member)
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
            actions = _actions(row.get("actions"), f"{field}.actions")
            targets.append(
                LayoutTarget(
                    name=name,
                    owner=owner,
                    description=description.strip(),
                    spec=spec,
                    spec_relative=spec_relative,
                    actions=actions,
                    routes=_routes(row.get("routes"), f"{field}.routes", actions),
                )
            )
    return LayoutTargetCatalog(
        tuple(catalog.path for catalog in catalogs),
        repository,
        tuple(targets),
        tuple(catalog_members),
        inventory,
    )
