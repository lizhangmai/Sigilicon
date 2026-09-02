"""Declarative custom-IP release contracts and maturity rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping

from sigilicon.contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    require_config_header,
)

if TYPE_CHECKING:
    from sigilicon.project._component import ComponentContract
    from sigilicon.project import Project

RELEASE_MATURITY_LEVELS = ("development", "implementation", "signoff")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


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
class OaMixedSignalIpInterface:
    kind: Literal["oa-mixed-signal"]
    contract: PurePosixPath
    library: str
    cell: str
    schematic_view: str
    layout_view: str
    physical: str
    logical: str


@dataclass(frozen=True)
class OaNativeIpInterface:
    """A native OA circuit boundary without a synthesized transaction shell."""

    kind: Literal["oa-native"]
    contract: PurePosixPath
    library: str
    cell: str
    schematic_view: str
    layout_view: str


@dataclass(frozen=True)
class RtlIpInterface:
    kind: Literal["rtl"]
    contract: PurePosixPath
    module: str
    source_role: str
    variant: str | None = None


OaIpInterface = OaMixedSignalIpInterface | OaNativeIpInterface
IpInterface = OaIpInterface | RtlIpInterface


@dataclass(frozen=True)
class IpExport:
    name: str
    interface: IpInterface
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
    oa_assembly: PurePosixPath | None
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
    cataloged_owner = repository.require_owner(contract_path)
    header = require_config_header(
        raw,
        contract_path,
        contract_kind="ip-release",
        path_scope="owner",
        owner=cataloged_owner.name,
    )
    unknown = set(raw) - _HEADER_FIELDS - {
        "name",
        "default_maturity",
        "exports",
        "collateral",
    }
    if unknown:
        raise ValueError(f"IP release contains unknown fields: {sorted(unknown)}")
    if cataloged_owner.release_contract != contract_path:
        raise ValueError("IP release is not declared by its owner component")
    producer_path = cataloged_owner.root
    producer = PurePosixPath(producer_path.relative_to(root).as_posix())
    component_path = cataloged_owner.component.path
    component_contract = PurePosixPath(
        component_path.relative_to(producer_path).as_posix()
    )
    from sigilicon.project._component import (
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
        interface = _table(
            entry.get("interface"), f"exports[{index}].interface"
        )
        interface_kind = _string(
            interface.get("kind"), f"exports[{index}].interface.kind"
        )
        interface_contract = safe_relative(
            interface.get("contract"),
            f"exports[{index}].interface.contract",
        )
        if interface_kind in {"oa-mixed-signal", "oa-native"}:
            oa = _table(entry.get("oa"), f"exports[{index}].oa")
            oa_identity = {
                "contract": interface_contract,
                "library": _string(
                    oa.get("library"), f"exports[{index}].oa.library"
                ),
                "cell": _string(oa.get("cell"), f"exports[{index}].oa.cell"),
                "schematic_view": _string(
                    oa.get("schematic_view"),
                    f"exports[{index}].oa.schematic_view",
                ),
                "layout_view": _string(
                    oa.get("layout_view"),
                    f"exports[{index}].oa.layout_view",
                ),
            }
            if interface_kind == "oa-native":
                if "physical" in interface or "logical" in interface:
                    raise ValueError(
                        f"exports[{index}] native OA interface cannot declare "
                        "mixed-signal interface identities"
                    )
                parsed_interface = OaNativeIpInterface(
                    kind="oa-native",
                    **oa_identity,
                )
            else:
                parsed_interface = OaMixedSignalIpInterface(
                    kind="oa-mixed-signal",
                    physical=_string(
                        interface.get("physical"),
                        f"exports[{index}].interface.physical",
                    ),
                    logical=_string(
                        interface.get("logical"),
                        f"exports[{index}].interface.logical",
                    ),
                    **oa_identity,
                )
        elif interface_kind == "rtl":
            if "oa" in entry:
                raise ValueError(
                    f"exports[{index}] RTL interface cannot declare an OA identity"
                )
            parsed_interface = RtlIpInterface(
                kind="rtl",
                contract=interface_contract,
                module=_string(
                    interface.get("module"),
                    f"exports[{index}].interface.module",
                ),
                source_role=_string(
                    interface.get("source_role"),
                    f"exports[{index}].interface.source_role",
                ),
                variant=(
                    None
                    if interface.get("variant") is None
                    else _string(
                        interface.get("variant"),
                        f"exports[{index}].interface.variant",
                    )
                ),
            )
        else:
            raise ValueError(
                f"exports[{index}].interface.kind is unsupported: "
                f"{interface_kind!r}"
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
            "interface": parsed_interface,
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
            interface=values["interface"],
            collateral=tuple(collateral_by_export[name]),
            required_roles=MappingProxyType(dict(values["required_roles"])),
        )
        if isinstance(
            exported.interface, (OaMixedSignalIpInterface, OaNativeIpInterface)
        ):
            oa_identity = (exported.interface.library, exported.interface.cell)
            if oa_identity in oa_identities:
                raise ValueError(
                    "duplicate IP export OA identity: "
                    f"{exported.interface.library}/{exported.interface.cell}"
                )
            oa_identities.add(oa_identity)
        present = {item.role for item in exported.collateral}
        development = set(exported.required_roles["development"])
        if not development.issubset(present):
            missing = ", ".join(sorted(development - present))
            raise ValueError(
                f"IP export {name} is missing development collateral: {missing}"
            )
        interface_path = (producer_path / exported.interface.contract).resolve()
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

    if set(interface_documents) != expected_interface_paths:
        raise ValueError("IP release snapshot interface document identity drift")

    oa_exports = [
        exported
        for exported in exports
        if isinstance(
            exported.interface, (OaMixedSignalIpInterface, OaNativeIpInterface)
        )
    ]
    if oa_exports:
        oa_assembly_path = repository.oa_assembly_for(contract_path)
        if oa_assembly_path is None:
            raise ValueError("OA release owner must declare one OA assembly")
        oa_assembly = PurePosixPath(
            oa_assembly_path.relative_to(root).as_posix()
        )
    else:
        oa_assembly = None

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
        oa_assembly=oa_assembly,
        component_graph=MappingProxyType(dict(component_graph)),
        document=freeze_toml_document(raw),
        interface_documents=MappingProxyType(interface_documents),
    )
    return result


def load_ip_contract(
    path: Path,
    *,
    project: Project,
) -> IpContract:
    from sigilicon.project import Project

    repository = project
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
