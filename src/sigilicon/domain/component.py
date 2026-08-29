"""Small, technology-neutral component ownership contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    require_config_header,
)
from sigilicon.domain.ip_release import safe_relative


COMPONENT_KINDS = {
    "composite-ip",
    "hard-macro",
    "rtl-shell",
    "rtl-ip",
    "source-library",
}


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


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
    public_interface: PurePosixPath | None
    filesets: Mapping[str, tuple[PurePosixPath, ...]]
    components: tuple[ComponentDependency, ...]
    document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )


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
    )
    kind = _string(document.get("kind"), "kind")
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unsupported component kind: {kind}")

    interface_value = document.get("public_interface")
    public_interface = (
        None
        if interface_value is None
        else safe_relative(interface_value, "public_interface")
    )

    filesets_raw = document.get("filesets", {})
    if not isinstance(filesets_raw, Mapping):
        raise ValueError("filesets must be a TOML table")
    filesets: dict[str, tuple[PurePosixPath, ...]] = {}
    for name, values in filesets_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fileset names must be non-empty strings")
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"filesets.{name} must be a non-empty array")
        filesets[name] = tuple(
            safe_relative(value, f"filesets.{name} entry") for value in values
        )

    dependencies_raw = document.get("component", [])
    if not isinstance(dependencies_raw, (list, tuple)):
        raise ValueError("component must be an array of tables")
    dependencies: list[ComponentDependency] = []
    names: set[str] = set()
    for index, value in enumerate(dependencies_raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"component[{index}] must be a TOML table")
        name = _string(value.get("name"), f"component[{index}].name")
        if name in names:
            raise ValueError(f"duplicate component dependency: {name}")
        names.add(name)
        dependencies.append(
            ComponentDependency(
                name=name,
                contract=safe_relative(
                    value.get("contract"), f"component[{index}].contract"
                ),
            )
        )

    result = ComponentContract(
        path=contract_path,
        project_root=root,
        owner=header.owner,
        name=_string(document.get("name"), "name"),
        kind=kind,
        public_interface=public_interface,
        filesets=filesets,
        components=tuple(dependencies),
        document=freeze_toml_document(document),
    )
    referenced = [path for values in result.filesets.values() for path in values]
    if result.public_interface is not None:
        referenced.append(result.public_interface)
    referenced.extend(item.contract for item in result.components)
    for relative in referenced:
        resolved = (root / Path(relative)).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(f"component input is missing: {relative}")
    return result


def load_component_contract(path: Path, *, project_root: Path) -> ComponentContract:
    root = project_root.resolve()
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root) or not contract_path.is_file():
        raise FileNotFoundError("component contract is missing or outside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    return parse_component_contract(
        contract_path,
        project_root=root,
        document=raw,
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
        or not isinstance(snapshot.document, Mapping)
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
                    and inventory_snapshot.document
                ):
                    raise ValueError(
                        "component graph inventory disagrees with its root snapshot"
                    )
                inventory_snapshot = root_contract
            if inventory_snapshot is not None and not inventory_snapshot.document:
                inventory_snapshot = None
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


def resolve_component_fileset(
    graph: Mapping[str, ComponentContract], component: str, fileset: str
) -> tuple[PurePosixPath, ...]:
    """Resolve one named fileset through an already validated component graph."""

    if component not in graph:
        raise ValueError(f"unknown component in release contract: {component}")
    values = graph[component].filesets.get(fileset)
    if values is None:
        raise ValueError(f"component {component!r} has no fileset {fileset!r}")
    return values
