"""Validate source and package-facing IP release contracts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.artifacts import read_json_object
from sigilicon.contracts import (
    read_toml,
    require_config_header,
    require_relative_path,
)
from sigilicon.domain.ip_release import (
    IpContract,
    IpExport,
    OaMixedSignalIpInterface,
    OaNativeIpInterface,
    RtlIpInterface,
)
from sigilicon.domain.netlist import subckt_ports
from sigilicon.domain.systemverilog import (
    ModulePort,
    module_port_signatures,
    named_port_connections,
)
from sigilicon.project import Project
from sigilicon.adapters.release.release_plan_record import (
    InterfaceConsistencyCheck,
    ReleaseAvailability,
)

if TYPE_CHECKING:
    from sigilicon.domain.design import DesignSpec


def _project_path(root: Path, relative: Path, label: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{label} escapes the project root: {relative}")
    return path


def _identity_module(value: str, label: str) -> str:
    module, separator, boundary = value.partition(":")
    if not separator or not module or not boundary:
        raise ValueError(f"{label} must be module:boundary")
    return module


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a table")
    return value


def _interface_ports(value: object, label: str) -> dict[str, ModulePort]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{label} must be a non-empty array of port tables")
    ports: dict[str, ModulePort] = {}
    for index, raw in enumerate(value):
        item = _table(raw, f"{label}[{index}]")
        name = item.get("name")
        direction = item.get("direction")
        width = item.get("width")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError(f"{label}[{index}].name must be an identifier")
        if direction not in {"input", "output", "inout"}:
            raise ValueError(f"{label}[{index}].direction is unsupported")
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValueError(f"{label}[{index}].width must be a positive integer")
        if name in ports:
            raise ValueError(f"{label} contains duplicate port {name}")
        ports[name] = ModulePort(direction=direction, width=width)
    return ports


def _rtl_module_contract(
    raw: Mapping[str, Any],
    *,
    module_name: str,
    variant: str | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    public_module = _table(raw.get("module"), "RTL interface module")
    if variant is None:
        selected = public_module
    else:
        variants = _table(raw.get("variant_modules"), "RTL interface variants")
        selected = _table(
            variants.get(variant), f"RTL interface variant {variant}"
        )
    if selected.get("name") != module_name:
        raise ValueError("RTL module identity disagrees with the interface contract")
    return selected, public_module


def _oa_port_contract(raw: Mapping[str, Any]) -> dict[str, ModulePort]:
    ports = _table(raw.get("ports"), "OA port contract ports")
    order = ports.get("order")
    directions = _table(ports.get("directions"), "OA port directions")
    if not isinstance(order, (list, tuple)) or any(
        not isinstance(name, str) or not name for name in order
    ):
        raise ValueError("OA ports.order must be a string array")
    if len(order) != len(set(order)) or set(order) != set(directions):
        raise ValueError("OA port order and directions must name the same unique pins")
    normalized = {"input": "input", "output": "output", "inputOutput": "inout"}
    result: dict[str, ModulePort] = {}
    for name in order:
        direction = directions[name]
        if direction not in normalized:
            raise ValueError(f"unsupported OA direction for {name}: {direction}")
        result[name] = ModulePort(direction=normalized[str(direction)], width=1)
    return result


def _oa_port_contract_source(
    root: Path, physical: Mapping[str, Any]
) -> Path:
    netlist_value = physical.get("canonical_netlist")
    selector_value = physical.get("canonical_port_contract")
    if not isinstance(netlist_value, str) or not isinstance(selector_value, str):
        raise ValueError("physical macro canonical source contracts are missing")
    contract_name, separator, selector = selector_value.partition(":")
    if not separator or selector != "[ports].order":
        raise ValueError("canonical_port_contract must select [ports].order")
    contract_relative = Path(netlist_value).parent / contract_name
    return _project_path(root, contract_relative, "OA port contract")


def _oa_port_contract_document(
    contract: IpContract,
    path: Path,
    *,
    project: Project,
    design_inventory: Mapping[Path, DesignSpec] | None,
) -> Mapping[str, Any]:
    if design_inventory is None:
        return read_toml(path)
    try:
        design_snapshot = design_inventory[path]
    except KeyError as exc:
        raise ValueError(f"OA design inventory has no {path} entry") from exc
    from sigilicon.domain.design import resolve_design_spec

    design = resolve_design_spec(
        path,
        project=project,
        snapshot=design_snapshot,
    )
    producer_root = _project_path(
        contract.project_root,
        Path(contract.producer),
        "IP producer",
    )
    if not path.is_relative_to(producer_root):
        raise ValueError("OA port contract must stay inside the release producer")
    return design.source_documents[path]


def _development_interface_check(
    contract: IpContract,
    exported: IpExport,
    *,
    project: Project,
) -> ReleaseCheck:
    return _development_interface_check_with_design_inventory(
        contract,
        exported,
        project=project,
        design_inventory=None,
    )


def _development_interface_check_with_design_inventory(
    contract: IpContract,
    exported: IpExport,
    *,
    project: Project,
    design_inventory: Mapping[Path, DesignSpec] | None,
) -> ReleaseCheck:
    """Validate the machine-readable boundary against shipped SV collateral."""

    if isinstance(exported.interface, RtlIpInterface):
        return _rtl_development_interface_check(contract, exported)
    if isinstance(exported.interface, OaNativeIpInterface):
        return _native_oa_development_interface_check(
            contract,
            exported,
            project=project,
            design_inventory=design_inventory,
        )

    interface = exported.interface
    root = contract.project_root
    producer = _project_path(root, Path(contract.producer), "IP producer")
    interface_path = _project_path(
        producer, Path(interface.contract), "interface contract"
    )
    documents = contract.interface_documents
    raw = documents.get(interface_path)
    if raw is None and documents:
        raise ValueError("IP release interface snapshot is incomplete")
    if raw is None:
        raw = read_toml(interface_path)
    physical = _table(raw.get("physical_macro"), "physical_macro")
    transaction = _table(raw.get("transaction_boundary"), "transaction_boundary")
    by_role = {item.role: item for item in exported.collateral}
    required_roles = {
        "interface_contract",
        "oa_port_contract",
        "physical_blackbox",
        "integration_adapter",
        "transaction_model",
        "circuit_netlist",
    }
    if not required_roles.issubset(by_role):
        raise ValueError("development interface roles are incomplete")

    expected_physical = _identity_module(
        interface.physical, "interface.physical"
    )
    expected_logical = _identity_module(
        interface.logical, "interface.logical"
    )
    physical_module = physical.get("module")
    logical_module = transaction.get("module")
    adapter_module = transaction.get("adapter_module")
    shell_module = transaction.get("ams_wrapper_module")
    if physical_module != expected_physical or logical_module != expected_logical:
        raise ValueError("interface identities disagree with the interface contract")
    if any(not isinstance(value, str) or not value for value in (adapter_module, shell_module)):
        raise ValueError("interface contract must name adapter and physical shell modules")
    if by_role["physical_blackbox"].module != physical_module:
        raise ValueError("physical blackbox role module disagrees with the interface")
    if by_role["transaction_model"].module != logical_module:
        raise ValueError("transaction model role module disagrees with the interface")
    if by_role["integration_adapter"].module != shell_module:
        raise ValueError("integration adapter role must expose the physical shell module")

    expected_sources = {
        "interface_contract": interface_path.relative_to(root).as_posix(),
        "oa_port_contract": _oa_port_contract_source(root, physical)
        .relative_to(root)
        .as_posix(),
        "physical_blackbox": physical.get("systemverilog_blackbox"),
        "transaction_model": transaction.get("source"),
        "integration_adapter": transaction.get("adapter_source"),
        "circuit_netlist": physical.get("canonical_netlist"),
    }
    for role, expected in expected_sources.items():
        if not isinstance(expected, str) or by_role[role].source.as_posix() != expected:
            raise ValueError(f"{role} source disagrees with the interface contract")

    physical_source = _project_path(root, Path(str(expected_sources["physical_blackbox"])), "physical blackbox")
    logical_source = _project_path(root, Path(str(expected_sources["transaction_model"])), "transaction model")
    adapter_source = _project_path(root, Path(str(expected_sources["integration_adapter"])), "integration adapter")
    oa_source = _project_path(
        root, Path(str(expected_sources["oa_port_contract"])), "OA port contract"
    )
    circuit_source = _project_path(
        root, Path(str(expected_sources["circuit_netlist"])), "canonical circuit netlist"
    )
    oa_document = _oa_port_contract_document(
        contract,
        oa_source,
        project=project,
        design_inventory=design_inventory,
    )
    oa_ports = _oa_port_contract(oa_document)
    physical_ports = module_port_signatures(
        physical_source.read_text(encoding="utf-8"), str(physical_module)
    )
    logical_ports = module_port_signatures(
        logical_source.read_text(encoding="utf-8"), str(logical_module)
    )
    adapter_text = adapter_source.read_text(encoding="utf-8")
    adapter_ports = module_port_signatures(adapter_text, str(adapter_module))
    shell_ports = module_port_signatures(adapter_text, str(shell_module))
    transaction_ports = _interface_ports(
        transaction.get("ports"), "transaction_boundary.ports"
    )
    if logical_ports != transaction_ports:
        raise ValueError("transaction model signature disagrees with the interface contract")
    adapter_transaction = dict(list(adapter_ports.items())[: len(transaction_ports)])
    if adapter_transaction != transaction_ports:
        raise ValueError("adapter transaction signature disagrees with the interface contract")
    shell_transaction = dict(list(shell_ports.items())[: len(transaction_ports)])
    if shell_transaction != transaction_ports:
        raise ValueError("physical shell transaction signature disagrees with the interface contract")
    port_count = physical.get("port_count")
    if isinstance(port_count, bool) or not isinstance(port_count, int) or port_count <= 0:
        raise ValueError("physical_macro.port_count must be a positive integer")
    if len(physical_ports) != port_count:
        raise ValueError("physical blackbox port count disagrees with the interface contract")
    if physical_ports != oa_ports:
        raise ValueError("physical blackbox signature disagrees with the OA port contract")
    if subckt_ports(circuit_source, str(physical_module)) != tuple(oa_ports):
        raise ValueError("canonical circuit pin order disagrees with the OA port contract")
    bindings = named_port_connections(adapter_text, str(shell_module), str(physical_module))
    if tuple(bindings) != tuple(oa_ports):
        raise ValueError("physical shell named-pin order disagrees with the OA port contract")
    return InterfaceConsistencyCheck(
        export=exported.name,
        physical_module=str(physical_module),
        physical_port_count=len(physical_ports),
        transaction_module=str(logical_module),
        transaction_port_count=len(logical_ports),
        transaction_signature_checked=True,
        physical_shell_module=str(shell_module),
        physical_named_bindings_checked=len(bindings),
    )


def _native_oa_development_interface_check(
    contract: IpContract,
    exported: IpExport,
    *,
    project: Project,
    design_inventory: Mapping[Path, DesignSpec] | None,
) -> ReleaseCheck:
    """Validate a native OA boundary without imposing a digital adapter schema."""

    interface = exported.interface
    if not isinstance(interface, OaNativeIpInterface):
        raise TypeError("native OA validation requires a native OA export")
    root = contract.project_root
    producer = _project_path(root, Path(contract.producer), "IP producer")
    interface_path = _project_path(
        producer, Path(interface.contract), "interface contract"
    )
    raw = contract.interface_documents.get(interface_path)
    if raw is None:
        if contract.interface_documents:
            raise ValueError("IP release interface snapshot is incomplete")
        raw = read_toml(interface_path)
    port_count, port_contract_relative = _native_oa_interface_contract(
        raw,
        path=interface_path,
        owner=contract.owner,
        library=interface.library,
        cell=interface.cell,
    )

    by_role = {item.role: item for item in exported.collateral}
    required_roles = {
        "interface_contract",
        "oa_port_contract",
        "circuit_netlist",
    }
    if not required_roles.issubset(by_role):
        raise ValueError("native OA development interface roles are incomplete")
    expected_contract = interface_path.relative_to(root).as_posix()
    if by_role["interface_contract"].source.as_posix() != expected_contract:
        raise ValueError("interface_contract source disagrees with the native OA interface")

    port_contract_path = _project_path(
        root, Path(port_contract_relative), "OA port contract"
    )
    if not port_contract_path.is_relative_to(producer):
        raise ValueError("OA port contract must stay inside the release producer")
    if by_role["oa_port_contract"].source != port_contract_relative:
        raise ValueError("oa_port_contract source disagrees with the native OA interface")
    oa_document = _oa_port_contract_document(
        contract,
        port_contract_path,
        project=project,
        design_inventory=design_inventory,
    )
    oa_ports = _oa_port_contract(oa_document)
    if len(oa_ports) != port_count:
        raise ValueError("OA port count disagrees with the native OA interface")

    circuit_source = _project_path(
        root,
        Path(by_role["circuit_netlist"].source),
        "canonical circuit netlist",
    )
    if not circuit_source.is_relative_to(producer):
        raise ValueError("canonical circuit netlist must stay inside the release producer")
    if subckt_ports(circuit_source, interface.cell) != tuple(oa_ports):
        raise ValueError("canonical circuit pin order disagrees with the OA port contract")
    return InterfaceConsistencyCheck(
        export=exported.name,
        interface_kind=interface.kind,
        oa_library=interface.library,
        oa_cell=interface.cell,
        physical_port_count=len(oa_ports),
        native_oa_port_contract_checked=True,
    )


def _native_oa_interface_contract(
    raw: Mapping[str, Any],
    *,
    path: Path,
    owner: str,
    library: str,
    cell: str,
) -> tuple[int, PurePosixPath]:
    """Validate the source/package-stable native OA interface envelope."""

    require_config_header(
        raw,
        path,
        contract_kind="ip-interface",
        path_scope="owner",
        owner=owner,
    )
    digital_sections = {"physical_macro", "transaction_boundary"} & set(raw)
    if digital_sections:
        raise ValueError(
            "native OA interface cannot declare digital transaction sections: "
            f"{sorted(digital_sections)}"
        )
    physical = _table(raw.get("physical"), "physical")
    _table(raw.get("behavior"), "behavior")
    _table(raw.get("supplies"), "supplies")
    if physical.get("library") != library or physical.get("cell") != cell:
        raise ValueError("native OA identity disagrees with the interface contract")
    port_count = physical.get("port_count")
    if (
        isinstance(port_count, bool)
        or not isinstance(port_count, int)
        or port_count <= 0
    ):
        raise ValueError("physical.port_count must be a positive integer")

    port_contract_relative = require_relative_path(
        physical.get("canonical_port_contract"),
        "physical.canonical_port_contract",
    )
    return port_count, port_contract_relative


def _rtl_development_interface_check(
    contract: IpContract, exported: IpExport
) -> ReleaseCheck:
    """Validate one synthesizable top directly against its public RTL contract."""

    interface = exported.interface
    if not isinstance(interface, RtlIpInterface):
        raise TypeError("RTL interface validation requires an RTL export")
    root = contract.project_root
    producer = _project_path(root, Path(contract.producer), "IP producer")
    interface_path = _project_path(
        producer, Path(interface.contract), "interface contract"
    )
    raw = contract.interface_documents.get(interface_path)
    if raw is None:
        if contract.interface_documents:
            raise ValueError("IP release interface snapshot is incomplete")
        raw = read_toml(interface_path)
    module, public_module = _rtl_module_contract(
        raw,
        module_name=interface.module,
        variant=interface.variant,
    )
    source_value = module.get("source")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("RTL interface module.source must be a project-relative path")

    by_role = {item.role: item for item in exported.collateral}
    required_roles = {"interface_contract", interface.source_role}
    if not required_roles.issubset(by_role):
        raise ValueError("RTL development interface roles are incomplete")
    expected_contract = interface_path.relative_to(root).as_posix()
    if by_role["interface_contract"].source.as_posix() != expected_contract:
        raise ValueError("interface_contract source disagrees with the RTL interface")
    rtl_view = by_role[interface.source_role]
    if (
        rtl_view.module != interface.module
        or rtl_view.source.as_posix() != source_value
    ):
        raise ValueError("RTL source role disagrees with the interface contract")
    rtl_source = _project_path(root, Path(source_value), "RTL interface source")
    if not rtl_source.is_relative_to(producer):
        raise ValueError("RTL interface source must stay inside the release producer")
    expected_ports = _interface_ports(
        public_module.get("ports"), "module.ports"
    )
    actual_ports = module_port_signatures(
        rtl_source.read_text(encoding="utf-8"), interface.module
    )
    if actual_ports != expected_ports:
        raise ValueError("RTL module signature disagrees with the interface contract")
    return InterfaceConsistencyCheck(
        export=exported.name,
        interface_kind=interface.kind,
        module=interface.module,
        variant=interface.variant,
        port_count=len(actual_ports),
    )


def _missing_roles(contract: IpContract, level: str) -> list[str]:
    missing: list[str] = []
    for exported in contract.exports:
        present = {item.role for item in exported.collateral}
        missing.extend(
            f"{exported.name}:{role}"
            for role in sorted(set(exported.required_roles[level]) - present)
        )
    return missing


def _release_semantics(
    contract: IpContract, level: str, *, source_commit: str,
) -> dict[str, tuple[ReleaseAvailability, tuple[str, ...]]]:
    from sigilicon.adapters.release.release_semantics import ExportSemantics

    assessments = {}
    for exported in contract.exports:
        by_role = {item.role: item for item in exported.collateral}
        def read_receipt(role: str):
            source = _project_path(contract.project_root, Path(by_role[role].source), f"{role} source")
            return read_json_object(source, f"{role} receipt")
        assessments[exported.name] = ExportSemantics.from_source(exported, level).assess(
            level, source_commit, read_receipt,
        )
    return assessments
