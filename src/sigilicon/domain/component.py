"""Small, technology-neutral component ownership contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.ip_release import safe_relative
from sigilicon.paths import ProjectContext


COMPONENT_KINDS = {"composite-ip", "hard-macro", "rtl-shell", "rtl-ip"}


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
    name: str
    kind: str
    public_interface: PurePosixPath | None
    filesets: Mapping[str, tuple[PurePosixPath, ...]]
    components: tuple[ComponentDependency, ...]


def load_component_contract(path: Path, *, project_root: Path) -> ComponentContract:
    context = ProjectContext.from_project_root(project_root)
    root = context.project_root
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root) or not contract_path.is_file():
        raise FileNotFoundError("component contract is missing or outside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    owner = contract_path.relative_to(context.ip_root).parts[0].replace("_", "-")
    require_config_header(
        raw,
        contract_path,
        contract_kind="ip-component",
        path_scope="owner",
        owner=owner,
    )
    kind = _string(raw.get("kind"), "kind")
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unsupported component kind: {kind}")

    interface_value = raw.get("public_interface")
    public_interface = (
        None
        if interface_value is None
        else safe_relative(interface_value, "public_interface")
    )

    filesets_raw = raw.get("filesets", {})
    if not isinstance(filesets_raw, Mapping):
        raise ValueError("filesets must be a TOML table")
    filesets: dict[str, tuple[PurePosixPath, ...]] = {}
    for name, values in filesets_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fileset names must be non-empty strings")
        if not isinstance(values, list) or not values:
            raise ValueError(f"filesets.{name} must be a non-empty array")
        filesets[name] = tuple(
            safe_relative(value, f"filesets.{name} entry") for value in values
        )

    dependencies_raw = raw.get("component", [])
    if not isinstance(dependencies_raw, list):
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
        name=_string(raw.get("name"), "name"),
        kind=kind,
        public_interface=public_interface,
        filesets=filesets,
        components=tuple(dependencies),
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


def load_component_graph(
    path: Path, *, project_root: Path
) -> Mapping[str, ComponentContract]:
    """Load and validate the complete component ownership graph rooted at *path*."""

    root = project_root.resolve()
    contracts: dict[str, ComponentContract] = {}
    owners: dict[str, Path] = {}
    visiting: set[Path] = set()

    def visit(contract_path: Path) -> ComponentContract:
        resolved = contract_path.resolve()
        if resolved in visiting:
            raise ValueError(f"component dependency cycle includes {resolved}")
        visiting.add(resolved)
        try:
            contract = load_component_contract(resolved, project_root=root)
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
            return contract
        finally:
            visiting.remove(resolved)

    visit(path)
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
