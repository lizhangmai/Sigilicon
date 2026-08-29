"""Declarative custom-IP release contracts and maturity rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    require_config_header,
)

if TYPE_CHECKING:
    from sigilicon.domain.component import ComponentContract
    from sigilicon.domain.repository import Project

RELEASE_MATURITY_LEVELS = ("development", "implementation", "signoff")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def safe_relative(value: object, label: str) -> PurePosixPath:
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
class IpCollateral:
    export: str
    role: str
    component: str
    fileset: str
    source: PurePosixPath
    package_path: PurePosixPath
    format: str
    module: str | None
    library: str | None
    cell: str | None
    view: str | None
    corner: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class IpExport:
    name: str
    oa_library: str
    oa_cell: str
    schematic_view: str
    layout_view: str
    interface_contract: PurePosixPath
    physical_interface: str
    logical_interface: str
    collateral: tuple[IpCollateral, ...]
    required_roles: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class IpContract:
    path: Path
    project: Project
    owner: str
    name: str
    producer: PurePosixPath
    component_contract: PurePosixPath
    default_maturity: str
    exports: tuple[IpExport, ...]
    source_files: tuple[PurePosixPath, ...]
    oa_assembly: PurePosixPath
    component_graph: Mapping[str, ComponentContract]
    document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    interface_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    @property
    def collateral(self) -> tuple[IpCollateral, ...]:
        return tuple(item for exported in self.exports for item in exported.collateral)

    def get_export(self, name: str) -> IpExport:
        matches = [item for item in self.exports if item.name == name]
        if len(matches) != 1:
            raise KeyError(f"unknown IP export: {name}")
        return matches[0]

    def require_level(self, level: str) -> str:
        if level not in RELEASE_MATURITY_LEVELS:
            raise ValueError(
                f"unsupported release maturity {level!r}; expected one of "
                f"{', '.join(RELEASE_MATURITY_LEVELS)}"
            )
        return level


def _parse_ip_contract(
    contract_path: Path,
    *,
    repository: Project,
    raw: Mapping[str, Any],
    source_component_graph: Mapping[str, ComponentContract] | None,
    source_interface_documents: Mapping[Path, Mapping[str, Any]] | None,
) -> IpContract:
    root = repository.project_root
    producer = safe_relative(raw.get("producer"), "producer")
    cataloged_owner = repository.require_owner(contract_path)
    header = require_config_header(
        raw,
        contract_path,
        contract_kind="ip-release",
        path_scope="owner",
        owner=cataloged_owner.name,
    )
    source = _table(raw.get("source"), "source")

    component_contract = safe_relative(raw.get("component"), "component")
    producer_path = (root / producer).resolve()
    if (
        not producer_path.is_dir()
        or not producer_path.is_relative_to(root)
        or not contract_path.is_relative_to(producer_path)
        or producer_path != cataloged_owner.root
    ):
        raise ValueError("IP release contract must stay inside its declared producer")
    component_path = (producer_path / component_contract).resolve()
    if not component_path.is_file() or not component_path.is_relative_to(producer_path):
        raise FileNotFoundError("IP component contract is missing or outside its owner")
    from sigilicon.domain.component import (
        load_component_graph,
        resolve_component_fileset,
        resolve_component_graph,
    )

    if source_component_graph is None:
        component_graph = load_component_graph(
            component_path,
            project_root=root,
            root_contract=(
                cataloged_owner.component
                if cataloged_owner.component.path == component_path
                else None
            ),
            contract_inventory=repository.component_inventory,
        )
    else:
        component_graph = resolve_component_graph(
            component_path,
            project_root=root,
            snapshot=source_component_graph,
        )
    ip_name = _string(raw.get("name"), "name")
    component = component_graph.get(ip_name)
    if component is None or component.path != component_path:
        raise ValueError(
            "IP release name must match its producer component identity"
        )

    exports_raw = raw.get("exports")
    if not isinstance(exports_raw, (list, tuple)) or not exports_raw:
        raise ValueError("IP contract must declare at least one exports entry")
    export_specs: dict[str, dict[str, object]] = {}
    for index, value in enumerate(exports_raw):
        entry = _table(value, f"exports[{index}]")
        name = _string(entry.get("name"), f"exports[{index}].name")
        if name in export_specs:
            raise ValueError(f"duplicate IP export: {name}")
        oa = _table(entry.get("oa"), f"exports[{index}].oa")
        interface = _table(
            entry.get("interface"), f"exports[{index}].interface"
        )
        maturity = _table(
            entry.get("maturity"), f"exports[{index}].maturity"
        )
        required_roles: dict[str, tuple[str, ...]] = {}
        previous: set[str] = set()
        for level in RELEASE_MATURITY_LEVELS:
            level_table = _table(
                maturity.get(level),
                f"exports[{index}].maturity.{level}",
            )
            values = level_table.get("required_roles")
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ValueError(
                    f"exports[{index}].maturity.{level}.required_roles "
                    "must be strings"
                )
            current = set(values)
            if len(current) != len(values):
                raise ValueError(
                    f"exports[{index}].maturity.{level}.required_roles "
                    "must be unique"
                )
            if not previous.issubset(current):
                raise ValueError(
                    f"exports[{index}].maturity.{level} must include "
                    "lower-level roles"
                )
            required_roles[level] = tuple(values)
            previous = current
        export_specs[name] = {
            "oa_library": _string(
                oa.get("library"), f"exports[{index}].oa.library"
            ),
            "oa_cell": _string(oa.get("cell"), f"exports[{index}].oa.cell"),
            "schematic_view": _string(
                oa.get("schematic_view"),
                f"exports[{index}].oa.schematic_view",
            ),
            "layout_view": _string(
                oa.get("layout_view"), f"exports[{index}].oa.layout_view"
            ),
            "interface_contract": safe_relative(
                interface.get("contract"),
                f"exports[{index}].interface.contract",
            ),
            "physical_interface": _string(
                interface.get("physical"),
                f"exports[{index}].interface.physical",
            ),
            "logical_interface": _string(
                interface.get("logical"),
                f"exports[{index}].interface.logical",
            ),
            "required_roles": required_roles,
        }

    collateral_raw = raw.get("collateral")
    if not isinstance(collateral_raw, (list, tuple)) or not collateral_raw:
        raise ValueError("IP contract must declare at least one collateral entry")
    roles: set[tuple[str, str]] = set()
    package_paths: set[PurePosixPath] = set()
    collateral_by_export: dict[str, list[IpCollateral]] = {
        name: [] for name in export_specs
    }
    for index, item in enumerate(collateral_raw):
        entry = _table(item, f"collateral[{index}]")
        export = _string(entry.get("export"), f"collateral[{index}].export")
        if export not in export_specs:
            raise ValueError(
                f"collateral[{index}] names unknown IP export: {export}"
            )
        role = _string(entry.get("role"), f"collateral[{index}].role")
        component = _string(
            entry.get("component"), f"collateral[{index}].component"
        )
        fileset = _string(entry.get("fileset"), f"collateral[{index}].fileset")
        sources = resolve_component_fileset(component_graph, component, fileset)
        if len(sources) != 1:
            raise ValueError(
                f"collateral[{index}] fileset must resolve to exactly one file"
            )
        package_path = safe_relative(
            entry.get("package_path"), f"collateral[{index}].package_path"
        )
        if package_path.parts[:2] != ("exports", export):
            raise ValueError(
                f"collateral[{index}].package_path must stay below "
                f"exports/{export}/"
            )
        capabilities = entry.get("capabilities", [])
        if not isinstance(capabilities, (list, tuple)) or any(
            not isinstance(value, str) or not value for value in capabilities
        ):
            raise ValueError(f"collateral[{index}].capabilities must be strings")
        role_key = (export, role)
        if role_key in roles:
            raise ValueError(f"duplicate IP collateral role: {export}/{role}")
        if package_path in package_paths:
            raise ValueError(f"duplicate IP package path: {package_path}")
        roles.add(role_key)
        package_paths.add(package_path)
        module = entry.get("module")
        library = entry.get("library")
        cell = entry.get("cell")
        view = entry.get("view")
        corner = entry.get("corner")
        if module is not None:
            module = _string(module, f"collateral[{index}].module")
        if corner is not None:
            corner = _string(corner, f"collateral[{index}].corner")
        if library is not None:
            library = _string(library, f"collateral[{index}].library")
        if cell is not None:
            cell = _string(cell, f"collateral[{index}].cell")
        if view is not None:
            view = _string(view, f"collateral[{index}].view")
        parsed = IpCollateral(
            export=export,
            role=role,
            component=component,
            fileset=fileset,
            source=sources[0],
            package_path=package_path,
            format=_string(entry.get("format"), f"collateral[{index}].format"),
            module=module,
            library=library,
            cell=cell,
            view=view,
            corner=corner,
            capabilities=tuple(capabilities),
        )
        collateral_by_export[export].append(parsed)

    exports: list[IpExport] = []
    interface_documents = dict(source_interface_documents or {})
    expected_interface_paths: set[Path] = set()
    oa_identities: set[tuple[str, str]] = set()
    for name, values in export_specs.items():
        exported = IpExport(
            name=name,
            oa_library=str(values["oa_library"]),
            oa_cell=str(values["oa_cell"]),
            schematic_view=str(values["schematic_view"]),
            layout_view=str(values["layout_view"]),
            interface_contract=values["interface_contract"],
            physical_interface=str(values["physical_interface"]),
            logical_interface=str(values["logical_interface"]),
            collateral=tuple(collateral_by_export[name]),
            required_roles=MappingProxyType(dict(values["required_roles"])),
        )
        oa_identity = (exported.oa_library, exported.oa_cell)
        if oa_identity in oa_identities:
            raise ValueError(
                f"duplicate IP export OA identity: {exported.oa_library}/"
                f"{exported.oa_cell}"
            )
        oa_identities.add(oa_identity)
        present = {item.role for item in exported.collateral}
        development = set(exported.required_roles["development"])
        if not development.issubset(present):
            missing = ", ".join(sorted(development - present))
            raise ValueError(
                f"IP export {name} is missing development collateral: {missing}"
            )
        interface_path = (producer_path / exported.interface_contract).resolve()
        expected_interface_paths.add(interface_path)
        if not interface_path.is_file() or not interface_path.is_relative_to(
            producer_path
        ):
            raise FileNotFoundError(
                f"IP export interface contract is missing or outside its owner: {name}"
            )
        if (
            source_interface_documents is None
            and interface_path not in interface_documents
        ):
            with interface_path.open("rb") as stream:
                interface_raw: dict[str, Any] = tomllib.load(stream)
            interface_documents[interface_path] = freeze_toml_document(interface_raw)
        exports.append(exported)

    if interface_documents and set(interface_documents) != expected_interface_paths:
        raise ValueError("IP release snapshot interface document identity drift")

    source_files = source.get("files", [])
    if not isinstance(source_files, (list, tuple)):
        raise ValueError("source.files must be an array")
    oa_assembly = safe_relative(source.get("oa_assembly"), "source.oa_assembly")
    oa_assembly_path = (root / oa_assembly).resolve()
    if (
        not oa_assembly_path.is_file()
        or not oa_assembly_path.is_relative_to(producer_path)
    ):
        raise FileNotFoundError(
            "source.oa_assembly must name a manifest inside the producer root"
        )

    default = _string(raw.get("default_maturity"), "default_maturity")
    if default not in RELEASE_MATURITY_LEVELS:
        raise ValueError("default_maturity is unsupported")
    result = IpContract(
        path=contract_path,
        project=repository,
        owner=header.owner,
        name=ip_name,
        producer=producer,
        component_contract=component_contract,
        default_maturity=default,
        exports=tuple(exports),
        source_files=tuple(
            safe_relative(value, "source.files entry") for value in source_files
        ),
        oa_assembly=oa_assembly,
        component_graph=MappingProxyType(dict(component_graph)),
        document=freeze_toml_document(raw),
        interface_documents=MappingProxyType(interface_documents),
    )
    return result


def load_ip_contract(
    path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
) -> IpContract:
    from sigilicon.domain.repository import Project

    repository = Project.bind(project=project, project_root=project_root)
    contract_path = path.resolve()
    if not contract_path.is_relative_to(repository.project_root):
        raise ValueError("IP contract must be inside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    return _parse_ip_contract(
        contract_path,
        repository=repository,
        raw=raw,
        source_component_graph=None,
        source_interface_documents=None,
    )


def resolve_ip_contract(
    path: Path,
    *,
    project: Project,
    snapshot: IpContract | None = None,
) -> IpContract:
    """Load an IP contract or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_ip_contract(path, project=project)
    contract_path = path.resolve()
    root = project.project_root
    if (
        snapshot.path != contract_path
        or snapshot.project is not project
        or not contract_path.is_relative_to(root)
        or not contract_path.is_file()
    ):
        raise ValueError("IP release snapshot identity drift")
    if not isinstance(snapshot.document, Mapping) or not snapshot.document:
        raise ValueError("IP release snapshot source document is missing")
    if not is_frozen_toml_document(snapshot.document):
        raise ValueError("IP release snapshot document is mutable")
    documents = snapshot.interface_documents
    if not isinstance(documents, _MAPPING_PROXY_TYPE):
        raise ValueError("IP release snapshot interface document identity drift")
    if any(
        not isinstance(exported.required_roles, _MAPPING_PROXY_TYPE)
        for exported in snapshot.exports
    ):
        raise ValueError("IP release snapshot typed contract is mutable")
    envelope_fields = frozenset({"contract_kind", "path_scope", "owner"})
    for interface_path, document in documents.items():
        if (
            not isinstance(interface_path, Path)
            or not isinstance(document, Mapping)
            or not is_frozen_toml_document(document)
        ):
            raise ValueError("IP release snapshot interface document is mutable")
        present = envelope_fields & document.keys()
        if present:
            if present != envelope_fields:
                raise ValueError("IP release snapshot interface header is incomplete")
            require_config_header(
                document,
                interface_path,
                contract_kind="ip-interface",
                path_scope="owner",
                owner=snapshot.owner,
            )
    parsed = _parse_ip_contract(
        contract_path,
        repository=project,
        raw=snapshot.document,
        source_component_graph=snapshot.component_graph,
        source_interface_documents=documents,
    )
    if parsed != snapshot:
        raise ValueError("IP release snapshot typed contract drift")
    return snapshot
