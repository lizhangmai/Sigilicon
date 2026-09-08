"""Typed owner composition contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.paths import validate_artifact_component
from sigilicon.source import ComponentFilesetReference, SourceReference

from sigilicon.contracts import (
    DocumentStore,
    ContractReader,
    contract_schema,
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
    require_relative_path,
    require_strings,
    require_table,
    require_text,
)

if TYPE_CHECKING:
    from sigilicon.project import Project


COMPONENT_KINDS = {
    "composite-ip",
    "hard-macro",
    "rtl-shell",
    "rtl-ip",
    "source-library",
}
COMPONENT_LIFECYCLES = {"active", "legacy"}
_COMPONENT_FIELDS = {
    "schema",
    "contract_kind",
    "path_scope",
    "owner",
    "name",
    "root",
    "kind",
    "lifecycle",
    "public_interface",
    "operation_catalog",
    "platform_catalog",
    "release_contract",
    "dependency_lock",
    "sources",
    "filesets",
    "variants",
    "implementation",
    "component",
}
_DEPENDENCY_FIELDS = {"name", "contract", "release"}
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


@dataclass(frozen=True)
class ComponentRelease:
    export: str
    required_maturity: str
    views: tuple[str, ...]


@dataclass(frozen=True)
class SourceDependency:
    name: str
    contract: PurePosixPath


@dataclass(frozen=True)
class PackageDependency:
    name: str
    release: ComponentRelease


ComponentDependency = SourceDependency | PackageDependency


@dataclass(frozen=True)
class ComponentContract:
    path: Path
    project_root: Path
    root: Path
    owner: str
    name: str
    kind: str
    lifecycle: str
    public_interface: PurePosixPath | None
    sources: Mapping[str, PurePosixPath]
    filesets: Mapping[str, tuple[str, ...]]
    components: tuple[ComponentDependency, ...]
    variants: Mapping[str, PurePosixPath]
    implementations: Mapping[str, PurePosixPath]
    document: Mapping[str, Any] = field(repr=False, compare=False)
    operation_catalog: PurePosixPath | None = None
    platform_catalog: PurePosixPath | None = None
    release_contract: PurePosixPath | None = None
    dependency_lock: PurePosixPath | None = None

    def fileset_paths(self, name: str) -> tuple[PurePosixPath, ...]:
        """Resolve one fileset's source identities to its declared paths."""

        return tuple(self.sources[source] for source in self.filesets.get(name, ()))


def _source_role(
    value: object,
    label: str,
    sources: Mapping[str, PurePosixPath],
) -> PurePosixPath | None:
    if value is None:
        return None
    identity = require_text(value, label)
    try:
        return sources[identity]
    except KeyError as exc:
        raise ValueError(f"{label} references unknown source {identity!r}") from exc


def _source_roles(
    value: object,
    label: str,
    sources: Mapping[str, PurePosixPath],
) -> Mapping[str, PurePosixPath]:
    value = require_table(value, label)
    selected: dict[str, PurePosixPath] = {}
    for name, identity in value.items():
        role = require_text(name, f"{label} name")
        resolved = _source_role(identity, f"{label}.{role}", sources)
        assert resolved is not None
        selected[role] = resolved
    return MappingProxyType(selected)


def _component_release(value: object, label: str) -> ComponentRelease | None:
    if value is None:
        return None
    value = require_table(value, label)
    required = {"export", "required_maturity", "views"}
    if set(value) != required:
        raise ValueError(f"{label} fields must be exactly {sorted(required)}")
    views_raw = require_strings(
        value.get("views"), f"{label}.views", nonempty=True
    )
    return ComponentRelease(
        export=require_text(value.get("export"), f"{label}.export"),
        required_maturity=require_text(
            value.get("required_maturity"),
            f"{label}.required_maturity",
        ),
        views=views_raw,
    )


def _parse_component_contract(
    path: Path,
    *,
    project_root: Path,
    document: Mapping[str, Any],
) -> ComponentContract:
    """Validate one already-read component source document."""

    root = project_root.resolve()
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root) or not contract_path.is_file():
        raise FileNotFoundError("component contract is missing or outside the project root")
    header = require_config_header(
        document,
        contract_path,
        contract_kind="ip-component",
        path_scope="owner",
        schema=contract_schema("ip-component"),
    )
    reader = ContractReader(document, "component")
    reader.consume(*_COMPONENT_FIELDS)
    reader.finish()
    owner_root = root.joinpath(*require_relative_path(document.get("root"), "root").parts)
    if not owner_root.is_dir() or owner_root != owner_root.resolve() or not contract_path.is_relative_to(owner_root):
        raise ValueError("component contract must stay inside its declared owner root")
    kind = require_text(document.get("kind"), "kind")
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unsupported component kind: {kind}")
    lifecycle = require_text(document.get("lifecycle", "active"), "lifecycle")
    if lifecycle not in COMPONENT_LIFECYCLES:
        raise ValueError(f"unsupported component lifecycle: {lifecycle}")

    sources_raw = document.get("sources", {})
    if not isinstance(sources_raw, Mapping):
        raise ValueError("sources must be a TOML table")
    sources: dict[str, PurePosixPath] = {}
    source_paths: set[PurePosixPath] = set()
    for name, value in sources_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("source names must be non-empty strings")
        source = require_relative_path(value, f"sources.{name}")
        if source in source_paths:
            raise ValueError(f"source path has multiple identities: {source}")
        sources[name] = source
        source_paths.add(source)

    public_interface = _source_role(
        document.get("public_interface"),
        "public_interface",
        sources,
    )
    operation_catalog = _source_role(
        document.get("operation_catalog"),
        "operation_catalog",
        sources,
    )
    platform_catalog = _source_role(
        document.get("platform_catalog"),
        "platform_catalog",
        sources,
    )
    release_contract = _source_role(
        document.get("release_contract"),
        "release_contract",
        sources,
    )
    dependency_lock = _source_role(
        document.get("dependency_lock"),
        "dependency_lock",
        sources,
    )
    variants = _source_roles(document.get("variants", {}), "variants", sources)
    implementations = _source_roles(
        document.get("implementation", {}),
        "implementation",
        sources,
    )

    filesets_raw = document.get("filesets", {})
    if not isinstance(filesets_raw, Mapping):
        raise ValueError("filesets must be a TOML table")
    filesets: dict[str, tuple[str, ...]] = {}
    for name, values in filesets_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fileset names must be non-empty strings")
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"filesets.{name} must be a non-empty array")
        identities = tuple(
            require_text(value, f"filesets.{name} entry") for value in values
        )
        if len(set(identities)) != len(identities):
            raise ValueError(f"filesets.{name} contains duplicate sources")
        unknown_sources = set(identities) - sources.keys()
        if unknown_sources:
            raise ValueError(
                f"filesets.{name} references unknown sources: "
                f"{sorted(unknown_sources)}"
            )
        filesets[name] = identities

    dependencies_raw = document.get("component", [])
    if not isinstance(dependencies_raw, (list, tuple)):
        raise ValueError("component must be an array of tables")
    dependencies: list[ComponentDependency] = []
    names: set[str] = set()
    for index, value in enumerate(dependencies_raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"component[{index}] must be a TOML table")
        unknown = set(value) - _DEPENDENCY_FIELDS
        if unknown:
            raise ValueError(
                f"component[{index}] contains unknown fields: {sorted(unknown)}"
            )
        name = validate_artifact_component(value.get("name"), f"component[{index}].name")
        if name in names:
            raise ValueError(f"duplicate component dependency: {name}")
        names.add(name)
        if "release" in value:
            if "contract" in value:
                raise ValueError("package dependencies cannot require a producer source contract")
            release = _component_release(value["release"], f"component[{index}].release")
            if release is None or release.required_maturity not in {"development", "implementation", "signoff"}:
                raise ValueError("package dependency maturity is unsupported")
            dependencies.append(PackageDependency(name, release))
        else:
            dependencies.append(SourceDependency(name, require_relative_path(value.get("contract"), f"component[{index}].contract")))


    result = ComponentContract(
        path=contract_path,
        project_root=root,
        root=owner_root,
        owner=validate_artifact_component(header.owner, "component owner"),
        name=validate_artifact_component(document.get("name"), "component name"),
        kind=kind,
        lifecycle=lifecycle,
        public_interface=public_interface,
        sources=MappingProxyType(sources),
        filesets=MappingProxyType(filesets),
        components=tuple(dependencies),
        variants=variants,
        implementations=implementations,
        operation_catalog=operation_catalog,
        platform_catalog=platform_catalog,
        release_contract=release_contract,
        dependency_lock=dependency_lock,
        document=freeze_toml_document(document),
    )
    referenced = list(result.sources.values())
    for relative in referenced:
        if not (root / relative).resolve().is_relative_to(owner_root):
            raise ValueError(f"component source escapes its declared owner root: {relative}")
    referenced.extend(item.contract for item in result.components if isinstance(item, SourceDependency))
    for relative in referenced:
        resolved = (root / Path(relative)).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(f"component input is missing: {relative}")
    if result.operation_catalog is not None:
        if result.operation_catalog.suffix != ".toml":
            raise ValueError("operation_catalog must name a TOML file")
        operation_catalog = root.joinpath(*result.operation_catalog.parts)
        resolved = operation_catalog.resolve()
        if operation_catalog != resolved:
            raise ValueError("operation_catalog must not be a symlink")
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(
                f"component operation catalog is missing: {result.operation_catalog}"
            )
    if result.platform_catalog is not None:
        if result.platform_catalog.suffix != ".toml":
            raise ValueError("platform_catalog must name a TOML file")
        platform_catalog = root.joinpath(*result.platform_catalog.parts)
        resolved = platform_catalog.resolve()
        if platform_catalog != resolved:
            raise ValueError("platform_catalog must not be a symlink")
        if not resolved.is_relative_to(owner_root) or not resolved.is_file():
            raise FileNotFoundError(
                f"component platform catalog is missing: {result.platform_catalog}"
            )
    if result.release_contract is not None:
        release = root / result.release_contract
        if release != release.resolve() or release.suffix != ".toml":
            raise ValueError("release_contract must name a direct TOML source")
    return result


def load_component_contract(path: Path, *, project: Project) -> ComponentContract:
    """Load a component through the canonical project composition seam."""

    root = project.project_root.resolve()
    contract_path = path.absolute()
    if (
        contract_path.resolve() != contract_path
        or not contract_path.is_relative_to(root)
        or not contract_path.is_file()
    ):
        raise FileNotFoundError("component contract is missing or outside the project root")
    return _parse_component_contract(
        contract_path,
        project_root=root,
        document=read_toml(contract_path),
    )


def resolve_component_contract(
    path: Path,
    *,
    project: Project,
    snapshot: ComponentContract | None = None,
) -> ComponentContract:
    """Load a component or validate one caller-owned source snapshot."""

    if snapshot is None:
        return load_component_contract(path, project=project)
    contract_path = path.resolve()
    root = project.project_root.resolve()
    if (
        snapshot.path != contract_path
        or snapshot.project_root != root
        or not isinstance(snapshot.sources, _MAPPING_PROXY_TYPE)
        or not isinstance(snapshot.filesets, _MAPPING_PROXY_TYPE)
        or not isinstance(snapshot.document, Mapping)
        or not is_frozen_toml_document(snapshot.document)
        or not snapshot.document
    ):
        raise ValueError("component snapshot identity drift")
    DocumentStore(root, {contract_path: snapshot.document}).verify_current(
        "component snapshot"
    )
    validated = _parse_component_contract(
        contract_path,
        project_root=root,
        document=snapshot.document,
    )
    if validated != snapshot or validated.document != snapshot.document:
        raise ValueError("component snapshot source document drift")
    return snapshot


def load_component_graph(
    path: Path,
    *,
    project: Project,
    root_contract: ComponentContract | None = None,
    contract_inventory: Mapping[Path, ComponentContract] | None = None,
) -> Mapping[str, ComponentContract]:
    """Load and validate the complete component ownership graph rooted at *path*."""

    root = project.project_root.resolve()
    graph_root = path.resolve()
    if root_contract is not None and (
        root_contract.path != graph_root
        or root_contract.project_root != root
    ):
        raise ValueError("component graph root snapshot disagrees with its path or project")
    if not graph_root.is_relative_to(root):
        raise FileNotFoundError(
            "component contract is missing or outside the project root"
        )
    contracts: dict[str, ComponentContract] = {}
    contracts_by_path: dict[Path, ComponentContract] = {}
    owners: dict[str, Path] = {}
    visiting: set[Path] = set()

    def visit(contract_path: Path) -> ComponentContract:
        resolved = contract_path.resolve()
        previous_contract = contracts_by_path.get(resolved)
        if previous_contract is not None:
            return previous_contract
        if resolved in visiting:
            raise ValueError(f"component dependency cycle includes {resolved}")
        visiting.add(resolved)
        try:
            inventory_snapshot = (
                None
                if contract_inventory is None
                else contract_inventory.get(resolved)
            )
            if root_contract is not None and resolved == graph_root:
                if (
                    inventory_snapshot is not None
                    and inventory_snapshot is not root_contract
                ):
                    raise ValueError(
                        "component graph inventory disagrees with its root snapshot"
                    )
                inventory_snapshot = root_contract
            contract = resolve_component_contract(
                resolved,
                project=project,
                snapshot=inventory_snapshot,
            )
            previous = owners.get(contract.name)
            if previous is not None and previous != contract.path:
                raise ValueError(
                    f"component identity {contract.name!r} is owned by both "
                    f"{previous} and {contract.path}"
                )
            owners[contract.name] = contract.path
            contracts[contract.name] = contract
            for dependency in contract.components:
                if isinstance(dependency, PackageDependency):
                    continue
                child = visit(root / Path(dependency.contract))
                if child.name != dependency.name:
                    raise ValueError(
                        f"component dependency {dependency.name!r} resolves to "
                        f"{child.name!r}"
                    )
            contracts_by_path[resolved] = contract
            return contract
        finally:
            visiting.remove(resolved)

    visit(graph_root)
    return contracts


def resolve_component_graph(
    path: Path,
    *,
    project: Project,
    snapshot: Mapping[str, ComponentContract] | None = None,
) -> Mapping[str, ComponentContract]:
    """Load a component graph or validate one caller-owned graph snapshot."""

    if snapshot is None:
        return load_component_graph(path, project=project)
    root = project.project_root.resolve()
    graph_root = path.resolve()
    if (
        not isinstance(snapshot, _MAPPING_PROXY_TYPE)
        or not snapshot
        or not graph_root.is_relative_to(root)
    ):
        raise ValueError("component graph snapshot identity drift")
    by_path: dict[Path, ComponentContract] = {}
    for name, contract in snapshot.items():
        if not isinstance(name, str) or name != contract.name:
            raise ValueError("component graph snapshot identity drift")
        resolved = resolve_component_contract(
            contract.path,
            project=project,
            snapshot=contract,
        )
        if resolved.path in by_path:
            raise ValueError("component graph snapshot has duplicate source paths")
        by_path[resolved.path] = resolved
    graph_root_contract = by_path.get(graph_root)
    if graph_root_contract is None:
        raise ValueError("component graph snapshot lacks its root contract")

    visited: set[Path] = set()
    visiting: set[Path] = set()

    def visit(contract: ComponentContract) -> None:
        if contract.path in visited:
            return
        if contract.path in visiting:
            raise ValueError("component graph snapshot contains a dependency cycle")
        visiting.add(contract.path)
        try:
            for dependency in contract.components:
                if isinstance(dependency, PackageDependency):
                    continue
                child_path = (root / Path(dependency.contract)).resolve()
                child = by_path.get(child_path)
                if child is None or child.name != dependency.name:
                    raise ValueError("component graph snapshot dependency drift")
                visit(child)
            visited.add(contract.path)
        finally:
            visiting.remove(contract.path)

    visit(graph_root_contract)
    if visited != set(by_path):
        raise ValueError("component graph snapshot contains unreachable contracts")
    return snapshot


def resolve_component_source(
    graph: Mapping[str, ComponentContract], component: str, source: str
) -> PurePosixPath:
    """Resolve one source identity through an already validated component graph."""

    if component not in graph:
        raise ValueError(f"unknown component in release contract: {component}")
    try:
        return graph[component].sources[source]
    except KeyError as exc:
        raise ValueError(
            f"component {component!r} has no source {source!r}"
        ) from exc


def resolve_component_fileset(
    graph: Mapping[str, ComponentContract],
    root_component: str,
    reference: ComponentFilesetReference,
) -> tuple[tuple[SourceReference, PurePosixPath], ...]:
    """Resolve one source-level graph fileset to qualified source identities."""

    if root_component not in graph:
        raise ValueError(f"unknown root component: {root_component!r}")
    reachable: set[str] = set()
    pending = [root_component]
    while pending:
        component_name = pending.pop()
        if component_name in reachable:
            continue
        try:
            component = graph[component_name]
        except KeyError as exc:
            raise ValueError(
                f"component graph is missing dependency {component_name!r}"
            ) from exc
        reachable.add(component_name)
        pending.extend(
            dependency.name
            for dependency in component.components
            if isinstance(dependency, SourceDependency)
        )
    if reference.component not in reachable:
        raise ValueError(
            f"component {reference.component!r} is not a source-level dependency "
            f"of {root_component!r}"
        )
    try:
        component = graph[reference.component]
        source_ids = component.filesets[reference.fileset]
    except KeyError as exc:
        raise ValueError(
            f"component {reference.component!r} has no fileset "
            f"{reference.fileset!r}"
        ) from exc
    if not source_ids:
        raise ValueError(
            f"component {reference.component!r} fileset "
            f"{reference.fileset!r} is empty"
        )
    return tuple(
        (
            SourceReference(reference.component, source_id),
            component.sources[source_id],
        )
        for source_id in source_ids
    )
