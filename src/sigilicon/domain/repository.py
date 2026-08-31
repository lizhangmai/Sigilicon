"""Caller-owned repository inventory resolved from canonical catalogs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, cast

from sigilicon.domain.component import ComponentContract, load_component_contract
from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    read_toml_record,
    require_config_header,
)
from sigilicon.paths import (
    ArtifactLayout,
    ProjectContext,
    ProjectScope,
    validate_artifact_component,
)

if TYPE_CHECKING:
    from sigilicon.execution import RunStore


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

    def flow_implementation_files(self) -> tuple[Path, ...]:
        """Return manifest-declared owner code that may implement a Flow."""

        implementation: set[Path] = {
            path
            for fileset in self.component.filesets
            for path in self.files(fileset)
            if path.suffix == ".py"
        }
        implementation.update(
            path
            for path in self.files("flow")
            if path.suffix not in {".toml", ".json", ".yaml", ".yml"}
        )
        return tuple(sorted(implementation))


@dataclass(frozen=True)
class RepositoryFlowExtension:
    """One project-selected, owner-owned Flow registry extension source."""

    owner: str
    source: Path


@dataclass(frozen=True)
class OwnerTargetSnapshot:
    """One owner-selected configuration source read for an operation."""

    owner: str
    path: Path
    contract_kind: str
    record_text: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class RepositoryCatalogSnapshot:
    """One validated repository-level catalog source snapshot."""

    role: str
    path: Path
    contract_kind: str
    owner: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class Project:
    """Canonical project paths, catalogs, owners and Flow extensions."""

    _paths: ProjectContext
    manifest_owner: str
    catalog_paths: tuple[tuple[str, Path], ...]
    owners: tuple[RepositoryOwner, ...]
    flow_registry_extensions: tuple[RepositoryFlowExtension, ...]
    manifest_document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )
    _manifest_path: Path | None = field(default=None, repr=False, compare=False)
    _manifest_context: ProjectContext | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _ip_catalog: RepositoryCatalogSnapshot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def component_inventory(self) -> Mapping[Path, ComponentContract]:
        """Return canonical owner component snapshots keyed by source path."""

        return MappingProxyType(
            {owner.component.path: owner.component for owner in self.owners}
        )

    @classmethod
    def from_file(cls, path: Path | str) -> "Project":
        contract = Path(path).resolve()
        raw = read_toml(contract)
        project = ProjectContext.from_contract(contract, raw)
        manifest_owner = cast(str, raw["owner"])
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
            if component.target_catalog is not None:
                target_catalog = project.project_root.joinpath(
                    *component.target_catalog.parts
                )
                resolved_target_catalog = target_catalog.resolve()
                if target_catalog != resolved_target_catalog:
                    raise ValueError(
                        f"{component.path}: target_catalog must not be a symlink"
                    )
                if not resolved_target_catalog.is_relative_to(owner_root):
                    raise ValueError(
                        f"{component.path}: target_catalog must stay inside its "
                        f"owner root: {component.target_catalog}"
                    )
                if not resolved_target_catalog.is_file():
                    raise FileNotFoundError(
                        f"{component.path}: target_catalog is missing: "
                        f"{component.target_catalog}"
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
        flow = raw.get("flow", {})
        if not isinstance(flow, Mapping):
            raise ValueError(f"{contract}: flow must be a table")
        unknown_flow_fields = set(flow) - {"registry_extensions"}
        if unknown_flow_fields:
            raise ValueError(
                f"{contract}: flow contains unknown fields: "
                f"{sorted(unknown_flow_fields)}"
            )
        extensions = flow.get("registry_extensions", {})
        if not isinstance(extensions, Mapping):
            raise ValueError(
                f"{contract}: flow.registry_extensions must be a table"
            )
        owners_by_name = {owner.name: owner for owner in owners}
        flow_registry_extensions: list[RepositoryFlowExtension] = []
        for name, value in extensions.items():
            if not isinstance(name, str) or name not in owners_by_name:
                raise ValueError(
                    f"{contract}: Flow registry extension names unknown owner {name!r}"
                )
            owner = owners_by_name[name]
            source = _project_file(
                project.project_root,
                value,
                f"{contract}: flow.registry_extensions.{name}",
            )
            if not source.is_relative_to(owner.root):
                raise ValueError(
                    f"{contract}: Flow registry extension for {name!r} must stay "
                    "inside its owner root"
                )
            if source.suffix != ".py":
                raise ValueError(
                    f"{contract}: Flow registry extension for {name!r} must be "
                    "a Python source"
                )
            if source not in owner.files("flow"):
                raise ValueError(
                    f"{contract}: Flow registry extension for {name!r} must be "
                    "declared in its owner flow fileset"
                )
            flow_registry_extensions.append(RepositoryFlowExtension(name, source))
        return cls(
            _paths=project,
            manifest_owner=manifest_owner,
            catalog_paths=catalog_paths,
            owners=tuple(sorted(owners, key=lambda item: item.name)),
            flow_registry_extensions=tuple(
                sorted(flow_registry_extensions, key=lambda item: item.owner)
            ),
            manifest_document=freeze_toml_document(raw),
            _manifest_path=contract,
            _manifest_context=project,
            _ip_catalog=RepositoryCatalogSnapshot(
                role="ip",
                path=ip_catalog,
                contract_kind="ip-catalog",
                owner=cast(str, ip_raw["owner"]),
                document=freeze_toml_document(ip_raw),
            ),
        )

    @classmethod
    def from_project_root(cls, project_root: Path | str) -> "Project":
        root = Path(project_root).resolve()
        project = cls.from_file(root / "sigilicon.toml")
        if project.project_root != root:
            raise ValueError("sigilicon.toml declares a different project root")
        return project

    def manifest_source_document(self) -> Mapping[str, Any]:
        """Validate and return the manifest source captured with this Project."""

        raw = self.manifest_document
        if not raw:
            return raw
        if not is_frozen_toml_document(raw):
            raise ValueError("project manifest snapshot source document drift")
        contract = self.manifest_path
        source_paths = ProjectContext.from_contract(contract, raw)
        if (
            (
                self._manifest_context is not None
                and source_paths != self._manifest_context
            )
            or source_paths.project_root != self.project_root
            or source_paths.workspace_root != self.workspace_root
            or raw.get("owner") != self.manifest_owner
        ):
            raise ValueError("project manifest snapshot identity drift")
        catalogs = raw.get("catalogs")
        if not isinstance(catalogs, Mapping) or set(catalogs) != {"ip", "platform"}:
            raise ValueError("project manifest snapshot catalog drift")
        catalog_paths = tuple(
            sorted(
                (
                    validate_artifact_component(name, "catalog name"),
                    _project_file(
                        self.project_root,
                        value,
                        f"{contract}: catalogs.{name}",
                    ),
                )
                for name, value in catalogs.items()
            )
        )
        flow = raw.get("flow", {})
        if not isinstance(flow, Mapping) or set(flow) - {"registry_extensions"}:
            raise ValueError("project manifest snapshot Flow selection drift")
        extensions = flow.get("registry_extensions", {})
        if not isinstance(extensions, Mapping):
            raise ValueError("project manifest snapshot Flow selection drift")
        selected_extensions = tuple(
            sorted(
                (
                    RepositoryFlowExtension(
                        owner,
                        _project_file(
                            self.project_root,
                            value,
                            f"{contract}: flow.registry_extensions.{owner}",
                        ),
                    )
                    for owner, value in extensions.items()
                    if isinstance(owner, str)
                ),
                key=lambda item: item.owner,
            )
        )
        if (
            catalog_paths != self.catalog_paths
            or selected_extensions != self.flow_registry_extensions
            or len(selected_extensions) != len(extensions)
        ):
            raise ValueError("project manifest snapshot source document drift")
        return raw

    @property
    def manifest_path(self) -> Path:
        """Return the explicit source path used to construct this Project."""

        return (
            self.project_root / "sigilicon.toml"
            if self._manifest_path is None
            else self._manifest_path
        )

    @property
    def project_root(self) -> Path:
        return self._paths.project_root

    @property
    def workspace_root(self) -> Path:
        return self._paths.workspace_root

    @property
    def artifact_root(self) -> Path:
        return self._paths.artifact_root

    @property
    def artifacts(self) -> ArtifactLayout:
        return self._paths.artifacts

    @property
    def context(self) -> ProjectContext:
        """Return the explicit paths bound to this project inventory."""

        return self._paths

    @property
    def runs(self) -> RunStore:
        """Bind immutable run lookup to this project's artifact root."""

        from sigilicon.execution import RunStore

        return RunStore(self.context)

    def with_artifact_root(self, artifact_root: Path | str) -> "Project":
        """Return this exact project inventory with a run-scoped artifact root."""

        return replace(
            self,
            _paths=self._paths.with_artifact_root(artifact_root),
        )

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

    def ip_catalog_snapshot(self) -> RepositoryCatalogSnapshot:
        """Return the canonical IP catalog parsed with this Project."""

        snapshot = self._ip_catalog
        if snapshot is None:
            path = self.catalog("ip")
            raw = read_toml(path)
            snapshot = RepositoryCatalogSnapshot(
                role="ip",
                path=path,
                contract_kind="ip-catalog",
                owner=cast(str, raw.get("owner")),
                document=freeze_toml_document(raw),
            )
        header = require_config_header(
            snapshot.document,
            snapshot.path,
            contract_kind="ip-catalog",
            path_scope="repository",
            owner=self.manifest_owner,
        )
        if (
            snapshot.path != self.catalog("ip")
            or not snapshot.path.is_relative_to(self.project_root)
            or not snapshot.path.is_file()
            or snapshot.role != "ip"
            or snapshot.contract_kind != header.contract_kind
            or snapshot.owner != header.owner
        ):
            raise ValueError("IP catalog snapshot belongs to a different Project")
        unknown = set(snapshot.document) - _HEADER_FIELDS - {
            "targets",
            "components",
        }
        if unknown:
            raise ValueError(
                f"{snapshot.path}: IP catalog contains unknown fields: "
                f"{sorted(unknown)}"
            )
        return snapshot

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

    def owner(self, name: str) -> RepositoryOwner:
        """Select one cataloged owner by its canonical identity."""

        identity = validate_artifact_component(name, "project owner")
        try:
            return next(owner for owner in self.owners if owner.name == identity)
        except StopIteration as exc:
            raise ValueError(
                f"unknown cataloged project owner: {identity!r}"
            ) from exc

    def resolve_owner_file(
        self,
        owner: RepositoryOwner | str,
        value: object,
        field: str,
    ) -> tuple[Path, PurePosixPath]:
        """Resolve one canonical project-relative file inside an owner's root."""

        selected = self.owner(owner) if isinstance(owner, str) else owner
        if selected not in self.owners:
            raise ValueError(
                f"repository does not contain owner {selected.name!r}"
            )
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty relative path")
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or "\\" in value
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(f"{field} must be a canonical project-relative path")
        resolved = self.project_root.joinpath(*relative.parts).resolve()
        if not resolved.is_relative_to(selected.root):
            raise ValueError(
                f"{field} must stay inside owner {selected.name!r} root"
            )
        if not resolved.is_file():
            raise ValueError(f"{field} does not exist inside its owner root")
        return resolved, relative

    def owner_target_catalog(
        self,
        owner: RepositoryOwner | str,
    ) -> OwnerTargetSnapshot:
        """Read the one explicitly selected target catalog for an owner.

        Target catalogs are selected by the owner component contract rather than
        discovered by scanning a fileset.  The configured path is project
        relative, while the target catalog's own entries use owner-relative
        paths.  Both the catalog path and every parent directory must be
        canonical regular filesystem objects so a symlink cannot alter the
        selected source after project assembly.
        """

        selected = self.owner(owner) if isinstance(owner, str) else owner
        if selected not in self.owners:
            raise ValueError(
                f"repository does not contain owner {selected.name!r}"
            )
        relative = selected.component.target_catalog
        if relative is None:
            raise ValueError(
                f"cataloged owner {selected.name!r} has no target_catalog"
            )
        configured = self.project_root.joinpath(*relative.parts)
        resolved = configured.resolve()
        if configured != resolved:
            raise ValueError(
                f"target_catalog for owner {selected.name!r} must not be a symlink"
            )
        if not resolved.is_relative_to(selected.root):
            raise ValueError(
                f"target_catalog for owner {selected.name!r} must stay inside "
                "its owner root"
            )
        if not resolved.is_file():
            raise FileNotFoundError(
                f"target_catalog for owner {selected.name!r} does not exist: "
                f"{relative}"
            )
        raw, record_text = read_toml_record(resolved)
        require_config_header(
            raw,
            resolved,
            contract_kind="owner-targets",
            path_scope="owner",
            owner=selected.name,
        )
        return OwnerTargetSnapshot(
            owner=selected.name,
            path=resolved,
            contract_kind="owner-targets",
            record_text=record_text,
            document=freeze_toml_document(raw),
        )

    def scope(self, owner: RepositoryOwner | str) -> ProjectScope:
        """Bind one cataloged owner to this project's explicit runtime paths."""

        selected = self.owner(owner) if isinstance(owner, str) else owner
        if selected not in self.owners:
            raise ValueError(
                f"repository does not contain owner {selected.name!r}"
            )
        return ProjectScope._from_cataloged_owner(
            self._paths,
            selected.name,
            selected.root,
        )

    def flow_registry_extension(self, owner: RepositoryOwner) -> Path | None:
        """Return the explicitly assembled registry source for one owner."""

        if owner not in self.owners:
            raise ValueError(f"repository does not contain owner {owner.name!r}")
        return next(
            (
                extension.source
                for extension in self.flow_registry_extensions
                if extension.owner == owner.name
            ),
            None,
        )

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
