"""Private owner composition contracts used by the Project module."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.contracts import (
    contract_schema,
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)


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
    "kind",
    "lifecycle",
    "public_interface",
    "operation_catalog",
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


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _safe_relative(value: object, label: str) -> PurePosixPath:
    text = _string(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a safe project-relative path: {text!r}")
    return path


@dataclass(frozen=True)
class ComponentDependency:
    name: str
    contract: PurePosixPath


@dataclass(frozen=True)
class ComponentContract:
    path: Path
    project_root: Path
    owner: str
    name: str
    kind: str
    lifecycle: str
    public_interface: PurePosixPath | None
    sources: Mapping[str, PurePosixPath]
    filesets: Mapping[str, tuple[PurePosixPath, ...]]
    components: tuple[ComponentDependency, ...]
    document: Mapping[str, Any] = field(repr=False, compare=False)
    operation_catalog: PurePosixPath | None = None
    release_contract: PurePosixPath | None = None


def parse_component_contract(
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
    unknown = set(document) - _COMPONENT_FIELDS
    if unknown:
        raise ValueError(f"component contains unknown fields: {sorted(unknown)}")
    kind = _string(document.get("kind"), "kind")
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unsupported component kind: {kind}")
    lifecycle = _string(document.get("lifecycle", "active"), "lifecycle")
    if lifecycle not in COMPONENT_LIFECYCLES:
        raise ValueError(f"unsupported component lifecycle: {lifecycle}")

    interface_value = document.get("public_interface")
    public_interface = (
        None
        if interface_value is None
        else _safe_relative(interface_value, "public_interface")
    )

    sources_raw = document.get("sources", {})
    if not isinstance(sources_raw, Mapping):
        raise ValueError("sources must be a TOML table")
    sources: dict[str, PurePosixPath] = {}
    source_paths: set[PurePosixPath] = set()
    for name, value in sources_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("source names must be non-empty strings")
        source = _safe_relative(value, f"sources.{name}")
        if source in source_paths:
            raise ValueError(f"source path has multiple identities: {source}")
        sources[name] = source
        source_paths.add(source)

    filesets_raw = document.get("filesets", {})
    if not isinstance(filesets_raw, Mapping):
        raise ValueError("filesets must be a TOML table")
    filesets: dict[str, tuple[PurePosixPath, ...]] = {}
    for name, values in filesets_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fileset names must be non-empty strings")
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"filesets.{name} must be a non-empty array")
        identities = tuple(
            _string(value, f"filesets.{name} entry") for value in values
        )
        if len(set(identities)) != len(identities):
            raise ValueError(f"filesets.{name} contains duplicate sources")
        unknown_sources = set(identities) - sources.keys()
        if unknown_sources:
            raise ValueError(
                f"filesets.{name} references unknown sources: "
                f"{sorted(unknown_sources)}"
            )
        filesets[name] = tuple(sources[identity] for identity in identities)

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
        name = _string(value.get("name"), f"component[{index}].name")
        if name in names:
            raise ValueError(f"duplicate component dependency: {name}")
        names.add(name)
        dependencies.append(
            ComponentDependency(
                name=name,
                contract=_safe_relative(
                    value.get("contract"), f"component[{index}].contract"
                ),
            )
        )

    operation_catalog_value = document.get("operation_catalog")
    operation_catalog = (
        None
        if operation_catalog_value is None
        else _safe_relative(operation_catalog_value, "operation_catalog")
    )
    release_value = document.get("release_contract")
    release_contract = (
        None
        if release_value is None
        else _safe_relative(release_value, "release_contract")
    )

    result = ComponentContract(
        path=contract_path,
        project_root=root,
        owner=header.owner,
        name=_string(document.get("name"), "name"),
        kind=kind,
        lifecycle=lifecycle,
        public_interface=public_interface,
        sources=MappingProxyType(sources),
        filesets=MappingProxyType(filesets),
        components=tuple(dependencies),
        operation_catalog=operation_catalog,
        release_contract=release_contract,
        document=freeze_toml_document(document),
    )
    referenced = list(result.sources.values())
    if result.public_interface is not None:
        referenced.append(result.public_interface)
    if result.release_contract is not None:
        referenced.append(result.release_contract)
    referenced.extend(item.contract for item in result.components)
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
    return result


def load_component_contract(path: Path, *, project_root: Path) -> ComponentContract:
    root = project_root.resolve()
    contract_path = path.absolute()
    if (
        contract_path.resolve() != contract_path
        or not contract_path.is_relative_to(root)
        or not contract_path.is_file()
    ):
        raise FileNotFoundError("component contract is missing or outside the project root")
    return parse_component_contract(
        contract_path,
        project_root=root,
        document=read_toml(contract_path),
    )


def resolve_component_contract(
    path: Path,
    *,
    project_root: Path,
    snapshot: ComponentContract | None = None,
) -> ComponentContract:
    """Load a component or validate one caller-owned source snapshot."""

    if snapshot is None:
        return load_component_contract(path, project_root=project_root)
    contract_path = path.resolve()
    root = project_root.resolve()
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
    validated = parse_component_contract(
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
    project_root: Path,
    root_contract: ComponentContract | None = None,
    contract_inventory: Mapping[Path, ComponentContract] | None = None,
) -> Mapping[str, ComponentContract]:
    """Load and validate the complete component ownership graph rooted at *path*."""

    root = project_root.resolve()
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
                project_root=root,
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
    project_root: Path,
    snapshot: Mapping[str, ComponentContract] | None = None,
) -> Mapping[str, ComponentContract]:
    """Load a component graph or validate one caller-owned graph snapshot."""

    if snapshot is None:
        return load_component_graph(path, project_root=project_root)
    root = project_root.resolve()
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
            project_root=root,
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
