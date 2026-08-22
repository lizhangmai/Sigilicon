"""Caller-owned repository inventory resolved from canonical catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sigilicon.domain.component import ComponentContract, load_component_contract
from sigilicon.domain.config_contracts import read_toml, require_config_header
from sigilicon.paths import ProjectContext, validate_artifact_component


_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})


def _project_file(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty project-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay below the project root")
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or not result.is_file():
        raise ValueError(f"{field} must name an existing project-owned file")
    return result


@dataclass(frozen=True)
class RepositoryOwner:
    """One cataloged owner root and its canonical component contract."""

    name: str
    root: Path
    component: ComponentContract

    def files(self, fileset: str) -> tuple[Path, ...]:
        return tuple(
            (self.component.project_root / Path(path)).resolve()
            for path in self.component.filesets.get(fileset, ())
        )


@dataclass(frozen=True)
class RepositoryContext:
    """Repository inventory kept separate from runtime/artifact paths."""

    project: ProjectContext
    catalog_paths: tuple[tuple[str, Path], ...]
    owners: tuple[RepositoryOwner, ...]

    @classmethod
    def from_file(cls, path: Path | str) -> "RepositoryContext":
        contract = Path(path).resolve()
        project = ProjectContext.from_file(contract)
        raw = read_toml(contract)
        catalogs = raw.get("catalogs")
        if not isinstance(catalogs, Mapping):
            raise ValueError(f"{contract}: catalogs must be a table")
        catalog_names = set(catalogs)
        missing = {"ip", "platform"} - catalog_names
        if missing:
            raise ValueError(
                f"{contract}: catalogs must contain ip and platform; "
                f"missing {sorted(missing)}"
            )
        unknown = catalog_names - {"ip", "platform"}
        if unknown:
            raise ValueError(
                f"{contract}: unknown catalog roles: {sorted(unknown)}"
            )
        catalog_paths = tuple(
            sorted(
                (
                    validate_artifact_component(name, "catalog name"),
                    _project_file(
                        project.project_root,
                        value,
                        f"{contract}: catalogs.{name}",
                    ),
                )
                for name, value in catalogs.items()
            )
        )
        ip_catalog = dict(catalog_paths)["ip"]
        ip_raw = read_toml(ip_catalog)
        require_config_header(
            ip_raw,
            ip_catalog,
            contract_kind="ip-catalog",
            path_scope="repository",
        )
        unknown = set(ip_raw) - _HEADER_FIELDS - {"targets", "components"}
        if unknown:
            raise ValueError(
                f"{ip_catalog}: IP catalog contains unknown fields: {sorted(unknown)}"
            )
        components = ip_raw.get("components")
        if not isinstance(components, Mapping):
            raise ValueError(f"{ip_catalog}: components must be a table")
        owners: list[RepositoryOwner] = []
        roots: set[Path] = set()
        for name, value in components.items():
            owner = validate_artifact_component(name, "component owner")
            if not isinstance(value, Mapping) or set(value) != {"contract", "root"}:
                raise ValueError(
                    f"{ip_catalog}: components.{name} must contain contract and root"
                )
            component_path = _project_file(
                project.project_root,
                value.get("contract"),
                f"{ip_catalog}: components.{name}.contract",
            )
            root_value = value.get("root")
            if not isinstance(root_value, str) or not root_value:
                raise ValueError(f"{ip_catalog}: components.{name}.root must be a path")
            relative_root = Path(root_value)
            owner_root = (project.project_root / relative_root).resolve()
            if (
                relative_root.is_absolute()
                or ".." in relative_root.parts
                or not owner_root.is_relative_to(project.project_root)
                or not owner_root.is_dir()
            ):
                raise ValueError(
                    f"{ip_catalog}: components.{name}.root must be a project-owned directory"
                )
            if owner_root in roots:
                raise ValueError(f"repository owner roots must be unique: {owner_root}")
            roots.add(owner_root)
            if not component_path.is_relative_to(owner_root):
                raise ValueError(
                    f"{ip_catalog}: components.{name}.contract must stay inside its root"
                )
            component = load_component_contract(
                component_path,
                project_root=project.project_root,
            )
            if component.name != owner:
                raise ValueError(
                    f"{ip_catalog}: component {name!r} identity disagrees with its contract"
                )
            owned_sources = [
                path
                for files in component.filesets.values()
                for path in files
            ]
            if component.public_interface is not None:
                owned_sources.append(component.public_interface)
            for relative in owned_sources:
                source = (project.project_root / Path(relative)).resolve()
                if not source.is_relative_to(owner_root):
                    raise ValueError(
                        f"{component.path}: component source escapes its cataloged root: "
                        f"{relative}"
                    )
            owners.append(RepositoryOwner(component.owner, owner_root, component))
        owner_names = [item.name for item in owners]
        if len(set(owner_names)) != len(owner_names):
            raise ValueError("repository component owners must be unique")
        for left in owners:
            for right in owners:
                if left is not right and left.root.is_relative_to(right.root):
                    raise ValueError("repository owner roots must not overlap")
        return cls(
            project=project,
            catalog_paths=catalog_paths,
            owners=tuple(sorted(owners, key=lambda item: item.name)),
        )

    @classmethod
    def from_project_root(cls, project_root: Path | str) -> "RepositoryContext":
        root = Path(project_root).resolve()
        repository = cls.from_file(root / "sigilicon.toml")
        if repository.project.project_root != root:
            raise ValueError("sigilicon.toml declares a different project root")
        return repository

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    def find_catalog(self, name: str) -> Path | None:
        """Return a registered catalog, or ``None`` for an optional domain."""

        key = validate_artifact_component(name, "catalog name")
        return dict(self.catalog_paths).get(key)

    def catalog(self, name: str) -> Path:
        path = self.find_catalog(name)
        if path is None:
            key = validate_artifact_component(name, "catalog name")
            raise ValueError(f"repository has no {key!r} catalog")
        return path

    def owner_for(self, path: Path | str) -> RepositoryOwner | None:
        resolved = Path(path).resolve()
        matches = tuple(owner for owner in self.owners if resolved.is_relative_to(owner.root))
        if len(matches) > 1:
            raise ValueError(f"repository path belongs to multiple owners: {resolved}")
        return matches[0] if matches else None

    def require_owner(self, path: Path | str) -> RepositoryOwner:
        owner = self.owner_for(path)
        if owner is None:
            raise ValueError(f"repository path has no cataloged owner: {Path(path).resolve()}")
        return owner

    def flow_catalogs(self, kind: str) -> tuple[tuple[str, Path], ...]:
        expected = {
            "design_targets": "flow-design-registry",
            "layout_targets": "flow-layout-registry",
        }.get(kind)
        if expected is None:
            raise ValueError(f"unsupported flow catalog kind: {kind!r}")
        result: list[tuple[str, Path]] = []
        for owner in self.owners:
            for path in owner.files("flow"):
                if path.suffix != ".toml":
                    continue
                raw = read_toml(path)
                contract_kind = raw.get("contract_kind")
                if contract_kind not in {
                    "flow-design-registry",
                    "flow-layout-registry",
                }:
                    continue
                if contract_kind == expected:
                    require_config_header(
                        raw,
                        path,
                        contract_kind=expected,
                        path_scope="owner",
                        owner=owner.name,
                    )
                    result.append((owner.name, path))
        return tuple(sorted(result))

    def owner_file(self, path: Path | str, fileset: str) -> Path | None:
        owner = self.require_owner(path)
        files = owner.files(fileset)
        if len(files) > 1:
            raise ValueError(
                f"owner {owner.name!r} fileset {fileset!r} must select at most one file"
            )
        return files[0] if files else None

    def oa_assembly_for(self, path: Path | str) -> Path | None:
        owner = self.require_owner(path)
        matches = tuple(
            source
            for source in owner.files("oa_source")
            if source.suffix == ".toml"
            and read_toml(source).get("contract_kind") == "oa-assembly"
        )
        if len(matches) > 1:
            raise ValueError(f"owner {owner.name!r} has multiple OA assemblies")
        return matches[0] if matches else None
