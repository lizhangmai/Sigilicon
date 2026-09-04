"""The deep Project module for composition and managed execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import hashlib
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, cast

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest, canonical_json
from sigilicon.contracts import (
    ContractReader,
    DocumentStore,
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
    require_strings,
    require_text,
    thaw_toml_document,
)
from sigilicon.domain.component import ComponentContract, load_component_contract
from sigilicon.paths import (
    ProjectContext,
    validate_artifact_component,
)
from sigilicon.execution._model import (
    ContractError,
    Resources,
    Source,
    resource_identity,
)

_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
if TYPE_CHECKING:
    from sigilicon.execution.adapter import AdapterRegistry
    from sigilicon.execution._model import (
        ExecutionPlan,
        PreflightResult,
        RunResult,
    )


def _runtime_resources(raw: Mapping[str, Any], contract: Path) -> Resources:
    runtime = raw.get("runtime", {})
    if not isinstance(runtime, Mapping):
        raise ValueError(f"{contract}: runtime must be a table")
    reader = ContractReader(runtime, f"{contract}: runtime")
    capabilities = require_strings(
        reader.take("capabilities", ()),
        f"{contract}: runtime.capabilities",
    )
    inherit_environment = require_strings(
        reader.take("inherit_environment", ()),
        f"{contract}: runtime.inherit_environment",
    )
    configured_environment = reader.table("environment", {})
    overlap = set(configured_environment) & set(inherit_environment)
    if overlap:
        raise ValueError(
            f"{contract}: runtime environment names cannot be both fixed and "
            f"inherited: {sorted(overlap)}"
        )
    environment: dict[str, str] = {}
    for name, value in configured_environment.items():
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"{contract}: runtime.environment.{name} must be non-empty text"
            )
        environment[name] = value
    for name in inherit_environment:
        try:
            environment[name] = os.environ[name]
        except KeyError:
            pass

    def table(name: str) -> dict[str, str]:
        value = reader.table(name, {})
        result: dict[str, str] = {}
        for key, item in value.items():
            try:
                identity = resource_identity(key)
            except ContractError as exc:
                raise ValueError(
                    f"{contract}: runtime.{name} has an invalid identity: {key!r}"
                ) from exc
            if not isinstance(item, str) or not item:
                raise ValueError(
                    f"{contract}: runtime.{name}.{identity} must be non-empty text"
                )
            result[identity] = item
        return result

    try:
        result = Resources(
            capabilities=frozenset(capabilities),
            tools=table("tools"),
            files=table("files"),
            directories=table("directories"),
            values=table("values"),
            inherit_environment=tuple(inherit_environment),
            environment=environment,
        )
    except ContractError as exc:
        raise ValueError(f"{contract}: invalid runtime configuration: {exc}") from exc
    reader.finish()
    return result


def _source_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Project the full deployment manifest onto the path contract schema."""

    return {key: value for key, value in raw.items() if key != "runtime"}


def _project_file(root: Path, value: object, field: str) -> Path:
    value = require_text(value, field)
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

    @property
    def release_contract(self) -> Path | None:
        relative = self.component.release_contract
        return (
            None
            if relative is None
            else (self.component.project_root / Path(relative)).resolve()
        )

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
    """Canonical project paths, catalogs, owners, and execution seam."""

    _paths: ProjectContext
    manifest_owner: str
    catalog_paths: tuple[tuple[str, Path], ...]
    owners: tuple[RepositoryOwner, ...]
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
    _runtime: Resources = field(
        default_factory=Resources,
        repr=False,
        compare=False,
    )
    _documents: DocumentStore | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _composition_documents: DocumentStore | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _adapter_registry: AdapterRegistry | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _plan_authority: object = field(default_factory=object, repr=False, compare=False)

    def _adapters(self) -> AdapterRegistry:
        """Return this Project's package-owned tool adapters."""

        from sigilicon.adapters import trusted_adapters
        from sigilicon.execution.adapter import AdapterRegistry

        registry = self._adapter_registry
        if registry is None:
            registry = AdapterRegistry(trusted_adapters())
            object.__setattr__(self, "_adapter_registry", registry)
        return registry

    @classmethod
    def open(
        cls,
        root: Path | str,
    ) -> "Project":
        """Open the canonical project rooted at *root* without loading tools."""

        location = Path(root).resolve()
        if not location.is_dir():
            raise ValueError("Project.open requires a project root directory")
        project = cls._from_file(location / "sigilicon.toml")
        if project.project_root != location:
            raise ValueError("sigilicon.toml declares a different project root")
        return project

    def plan(self, selector: str) -> ExecutionPlan:
        """Compile a selector to its complete source and runtime closure."""

        from sigilicon.execution.adapter import plan_execution
        from sigilicon.execution.operations import compile_operation, parse_selector

        if not isinstance(selector, str):
            raise TypeError("Project.plan requires an owner:operation selector")
        owner_name, operation, variant = parse_selector(selector)
        owner = self.owner(owner_name)
        relative = owner.component.operation_catalog
        if relative is None:
            raise ValueError(f"owner {owner.name!r} has no operation catalog")
        catalog = self.project_root.joinpath(*relative.parts).absolute()
        draft = compile_operation(
            catalog,
            owner=owner.name,
            owner_root=owner.root,
            project_root=self.project_root,
            component_filesets=owner.component.filesets,
            operation=operation,
            variant=variant,
            project_identity=self.operation_identity(owner.name),
        )
        composition_sources = tuple(
            Source.capture(path, root=self.project_root, scope="project")
            for path in self._operation_composition_paths(owner)
        )
        return plan_execution(
            draft,
            project=self,
            adapters=self._adapters(),
            resources=self._execution_resources(),
            authority=self._plan_authority,
            composition_sources=composition_sources,
        )

    def preflight(
        self,
        plan: ExecutionPlan,
    ) -> PreflightResult:
        """Check a plan without creating a run or starting an adapter."""

        from sigilicon.execution.engine import _preflight
        from sigilicon.execution._model import ExecutionPlan

        if not isinstance(plan, ExecutionPlan):
            raise TypeError("Project.preflight requires an ExecutionPlan")
        resources = self._execution_resources()
        self._require_project_plan(plan)
        return _preflight(plan, resources, self._adapters())

    def run(
        self,
        plan: ExecutionPlan,
        *,
        run_id: str | None = None,
        progress: Callable[[str, str], None] | None = None,
    ) -> RunResult:
        """Execute one source-current plan through its selected adapters."""

        from sigilicon.execution.engine import _run
        from sigilicon.execution._model import ExecutionPlan

        if not isinstance(plan, ExecutionPlan):
            raise TypeError("Project.run requires an ExecutionPlan")
        resources = self._execution_resources()
        self._require_project_plan(plan)
        return _run(
            plan,
            resources,
            self._adapters(),
            artifact_root=self.artifact_root,
            run_id=run_id,
            progress=progress,
        )

    def _require_project_plan(self, plan: ExecutionPlan) -> None:
        """Require a plan produced from this exact project composition."""

        if plan.project_identity != self.operation_identity(plan.owner):
            raise ContractError("execution plan belongs to another project composition")
        if plan._authority is not self._plan_authority:
            raise ContractError("execution plan was not produced by this Project")
        owner_root = self.owner(plan.owner).root.resolve()
        project_root = self.project_root.resolve()
        expected_composition = set(
            self._operation_composition_paths(self.owner(plan.owner))
        )
        actual_composition = {
            source.location for source in plan._composition_sources
        }
        if actual_composition != expected_composition or any(
            source.root != project_root or source.scope != "project"
            for source in plan._composition_sources
        ):
            raise ContractError(
                "execution plan composition monitor disagrees with this Project"
            )
        for source in plan.sources:
            expected_root = owner_root if source.scope == "owner" else project_root
            if source.root != expected_root or not source.location.is_relative_to(
                expected_root
            ):
                raise ContractError("execution plan source escaped its project scope")

    def _execution_resources(self) -> Resources:
        """Return the runtime deployment frozen when this Project was opened."""

        return self._runtime

    def resources(self) -> Resources:
        """Snapshot the project-declared runtime deployment and allowed host state."""

        self.manifest_source_document()
        return self._execution_resources()

    def configuration_documents(self) -> DocumentStore:
        """Return the immutable TOML closure captured when this Project opened."""

        expected: dict[Path, Mapping[str, Any]] = {
            self.manifest_path: self.manifest_document,
            **{
                owner.component.path: owner.component.document
                for owner in self.owners
            },
        }
        if self._ip_catalog is not None:
            expected[self._ip_catalog.path] = self._ip_catalog.document
        documents = self._documents
        if documents is None:
            documents = self._capture_configuration_documents()
            object.__setattr__(self, "_documents", documents)
        documents.verify("Project source snapshot", expected)
        return documents

    def _capture_configuration_documents(self) -> DocumentStore:
        return DocumentStore.capture_trees(
            self.project_root,
            self.configuration_roots,
            paths={
                self.manifest_path,
                *(path for _, path in self.catalog_paths),
            },
        )

    def _composition_snapshot(self) -> DocumentStore:
        snapshot = self._composition_documents
        if snapshot is None:
            expected: dict[Path, Mapping[str, Any]] = {
                self.manifest_path: self.manifest_document,
                **{
                    owner.component.path: owner.component.document
                    for owner in self.owners
                },
            }
            if self._ip_catalog is not None:
                expected[self._ip_catalog.path] = self._ip_catalog.document
            snapshot = DocumentStore(self.project_root, expected)
            object.__setattr__(self, "_composition_documents", snapshot)
        return snapshot

    @property
    def configuration_roots(self) -> tuple[Path, ...]:
        """Return the exact owner roots selected for configuration checking."""

        return tuple(
            sorted(
                {
                    *(owner.root for owner in self.owners),
                    *(
                        path.parent
                        for role, path in self.catalog_paths
                        if role == "platform"
                    ),
                }
            )
        )

    @property
    def component_inventory(self) -> Mapping[Path, ComponentContract]:
        """Return canonical owner component snapshots keyed by source path."""

        return MappingProxyType(
            {owner.component.path: owner.component for owner in self.owners}
        )

    @classmethod
    def _from_file(cls, path: Path | str) -> "Project":
        contract = Path(path).resolve()
        raw = read_toml(contract)
        runtime = _runtime_resources(raw, contract)
        source_raw = _source_manifest(raw)
        project = ProjectContext.from_contract(contract, source_raw)
        manifest_owner = cast(str, source_raw["owner"])
        catalogs = source_raw.get("catalogs")
        if not isinstance(catalogs, Mapping):
            raise ValueError(f"{contract}: catalogs must be a table")
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
        ip_catalog = dict(catalog_paths).get("ip")
        ip_raw: Mapping[str, Any] | None = None
        components: Mapping[str, Any] = {}
        if ip_catalog is not None:
            ip_raw = read_toml(ip_catalog)
            require_config_header(
                ip_raw,
                ip_catalog,
                contract_kind="ip-catalog",
                path_scope="repository",
            )
            unknown = set(ip_raw) - _HEADER_FIELDS - {"components"}
            if unknown:
                raise ValueError(
                    f"{ip_catalog}: IP catalog contains unknown fields: {sorted(unknown)}"
                )
            raw_components = ip_raw.get("components")
            if not isinstance(raw_components, Mapping):
                raise ValueError(f"{ip_catalog}: components must be a table")
            components = raw_components
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
            if component.name != owner or component.owner != owner:
                raise ValueError(
                    f"{ip_catalog}: component {name!r} identity disagrees with its contract"
                )
            if component.operation_catalog is not None:
                operation_catalog = project.project_root.joinpath(
                    *component.operation_catalog.parts
                )
                resolved_operation_catalog = operation_catalog.resolve()
                if operation_catalog != resolved_operation_catalog:
                    raise ValueError(
                        f"{component.path}: operation_catalog must not be a symlink"
                    )
                if not resolved_operation_catalog.is_relative_to(owner_root):
                    raise ValueError(
                        f"{component.path}: operation_catalog must stay inside its "
                        f"owner root: {component.operation_catalog}"
                    )
                if not resolved_operation_catalog.is_file():
                    raise FileNotFoundError(
                        f"{component.path}: operation_catalog is missing: "
                        f"{component.operation_catalog}"
                    )
            if component.release_contract is not None:
                configured_release = project.project_root.joinpath(
                    *component.release_contract.parts
                )
                release_contract = configured_release.resolve()
                if (
                    configured_release != release_contract
                    or release_contract.suffix != ".toml"
                ):
                    raise ValueError(
                        f"{component.path}: release_contract must name a direct "
                        "TOML source"
                    )
                if not release_contract.is_relative_to(owner_root):
                    raise ValueError(
                        f"{component.path}: release_contract must stay inside its "
                        f"owner root: {component.release_contract}"
                    )
            owned_sources = list(component.sources.values())
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
        result = cls(
            _paths=project,
            manifest_owner=manifest_owner,
            catalog_paths=catalog_paths,
            owners=tuple(sorted(owners, key=lambda item: item.name)),
            manifest_document=freeze_toml_document(raw),
            _manifest_path=contract,
            _manifest_context=project,
            _ip_catalog=(
                None
                if ip_catalog is None or ip_raw is None
                else RepositoryCatalogSnapshot(
                    role="ip",
                    path=ip_catalog,
                    contract_kind="ip-catalog",
                    owner=cast(str, ip_raw["owner"]),
                    document=freeze_toml_document(ip_raw),
                )
            ),
            _runtime=runtime,
        )
        object.__setattr__(result, "_composition_documents", result._composition_snapshot())
        return result

    def manifest_source_document(self) -> Mapping[str, Any]:
        """Validate and return the manifest source captured with this Project."""

        raw = self.manifest_document
        if not raw:
            return raw
        if not is_frozen_toml_document(raw):
            raise ValueError("project manifest snapshot source document drift")
        contract = self.manifest_path
        documents = self._composition_snapshot()
        try:
            documents.verify("project manifest snapshot", {contract: raw})
        except ValueError as exc:
            raise ValueError(
                "project manifest snapshot source document drift"
            ) from exc
        documents.verify_current("project manifest snapshot", (contract,))
        source_paths = ProjectContext.from_contract(contract, _source_manifest(raw))
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
        if not isinstance(catalogs, Mapping):
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
        if catalog_paths != self.catalog_paths:
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
    def identity(self) -> str:
        """Return the deterministic identity of this Project composition."""

        paths = {
            self.manifest_path,
            *(path for _, path in self.catalog_paths),
            *(owner.component.path for owner in self.owners),
            *(
                self.project_root.joinpath(*owner.component.operation_catalog.parts)
                for owner in self.owners
                if owner.component.operation_catalog is not None
            ),
        }
        return canonical_digest(
            {
                "sources": [
                    {
                        "path": path.relative_to(self.project_root).as_posix(),
                        "sha256": hashlib.sha256(
                            (
                                canonical_json(
                                    _source_manifest(
                                        thaw_toml_document(
                                            self.manifest_source_document()
                                        )
                                    )
                                )
                                if path == self.manifest_path
                                else read_nofollow_text(path)
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                    for path in sorted(paths)
                ],
            }
        )

    def operation_identity(self, owner: str) -> str:
        """Identify only the static project closure that selects one owner."""

        selected = self.owner(owner)
        self._require_owner_snapshot(selected)
        paths = [
            path
            for path in self._operation_composition_paths(selected)
            if path != self.manifest_path
        ]
        return canonical_digest(
            {
                "project": _source_manifest(
                    thaw_toml_document(self.manifest_source_document())
                ),
                "owner": {
                    "name": selected.name,
                    "root": selected.root.relative_to(self.project_root).as_posix(),
                    "sources": [
                        {
                            "path": path.relative_to(self.project_root).as_posix(),
                            "sha256": hashlib.sha256(
                                read_nofollow_text(path).encode("utf-8")
                            ).hexdigest(),
                        }
                        for path in sorted(paths)
                    ],
                },
            }
        )

    def _require_owner_snapshot(self, owner: RepositoryOwner) -> None:
        """Fail when cached catalog/component facts no longer match source."""

        catalog = self.ip_catalog_snapshot()
        documents = self._composition_snapshot()
        documents.verify(
            "component snapshot", {owner.component.path: owner.component.document}
        )
        documents.verify_current("component snapshot", (owner.component.path,))
        if owner.name not in catalog.document.get("components", {}):
            raise ValueError("IP catalog snapshot owner mapping drift")

    def _operation_composition_paths(
        self,
        owner: RepositoryOwner,
    ) -> tuple[Path, ...]:
        paths = [self.manifest_path, self.catalog("ip"), owner.component.path]
        if owner.component.operation_catalog is not None:
            paths.append(
                self.project_root.joinpath(*owner.component.operation_catalog.parts)
            )
        return tuple(dict.fromkeys(path.absolute() for path in paths))

    @property
    def project_root(self) -> Path:
        return self._paths.project_root

    @property
    def workspace_root(self) -> Path:
        return self._paths.workspace_root

    @property
    def artifact_root(self) -> Path:
        return self._paths.artifact_root

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
        documents = self._composition_snapshot()
        documents.verify("IP catalog snapshot", {snapshot.path: snapshot.document})
        documents.verify_current("IP catalog snapshot", (snapshot.path,))
        if (
            snapshot.path != self.catalog("ip")
            or not snapshot.path.is_relative_to(self.project_root)
            or not snapshot.path.is_file()
            or snapshot.role != "ip"
            or snapshot.contract_kind != header.contract_kind
            or snapshot.owner != header.owner
        ):
            raise ValueError("IP catalog snapshot source document drift")
        unknown = set(snapshot.document) - _HEADER_FIELDS - {"components"}
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
