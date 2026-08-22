"""Generic immutable custom-IP packaging and publication workflow."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tomllib
import uuid
from typing import Any, Mapping

from sigilicon import __version__
from sigilicon.artifacts import atomic_write_json, file_sha256, read_json_object, utc_now
from sigilicon.domain.component import load_component_graph
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
    IpContract,
    IpExport,
    IpPromotionContract,
    PromotionEvidence,
    ReleaseFingerprintSource,
    load_ip_contract,
    load_ip_promotion_contract,
    release_source_fingerprint,
    semantic_source_sha256,
)
from sigilicon.domain.provenance import digest
from sigilicon.domain.netlist import (
    load_netlist_snapshot,
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
    subckt_ports,
)
from sigilicon.domain.systemverilog import (
    ModulePort,
    module_ports,
    module_port_signatures,
    named_port_connections,
)
from sigilicon.external_tools import run_process_group
from sigilicon.flow import FlowEngine, FlowRegistry
from sigilicon.flow.model import (
    InputArtifact,
    PolicyCheck,
    PolicySpec,
    RunArtifactReference,
)
from sigilicon.flow.policy import evaluate_policy
from sigilicon.flow.run_artifacts import (
    load_run_artifact_reference,
    run_artifact_reference_payload,
    validate_durable_artifact,
)


class IpReleaseError(RuntimeError):
    """The requested release is not backed by accepted immutable evidence."""


_IMPLEMENTATION_ROLE_FORMATS = {
    "raw_macro_lef": {"lef"},
    "raw_macro_liberty_or_db": {"liberty", "db"},
    "raw_macro_gds_or_oasis": {"gds", "oasis"},
    "raw_macro_cdl_or_lvs_netlist": {"cdl", "spice", "spectre"},
}
_SIGNOFF_RECEIPT_BINDINGS = {
    "schematic_layout_parity_receipt": {
        "circuit_netlist",
        "raw_macro_gds_or_oasis",
        "raw_macro_cdl_or_lvs_netlist",
    },
    "drc_receipt": {"raw_macro_gds_or_oasis"},
    "lvs_receipt": {
        "circuit_netlist",
        "raw_macro_gds_or_oasis",
        "raw_macro_cdl_or_lvs_netlist",
    },
    "characterization_receipt": {
        "raw_macro_liberty_or_db",
        "pex_netlist",
    },
}
_QUALIFICATION_OUTPUT_ROLES = set(_IMPLEMENTATION_ROLE_FORMATS) | {
    *_SIGNOFF_RECEIPT_BINDINGS,
    "pex_netlist",
}


def _project_path(root: Path, relative: Path, label: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{label} escapes the project root: {relative}")
    return path


def _python_module_paths(root: Path, module: str) -> set[Path]:
    """Resolve one repository-owned absolute Python import and package initializers."""

    parts = module.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        return set()
    project_root = root.resolve()
    for source_root in (project_root, project_root / "src"):
        stem = source_root.joinpath(*parts)
        module_file = stem.with_suffix(".py")
        package_file = stem / "__init__.py"
        target = module_file if module_file.is_file() else package_file
        if not target.is_file() or not target.resolve().is_relative_to(project_root):
            continue
        paths = {target.resolve()}
        for length in range(1, len(parts)):
            initializer = source_root.joinpath(*parts[:length], "__init__.py")
            if initializer.is_file():
                paths.add(initializer.resolve())
        return paths
    return set()


def _sigilicon_tool_identity() -> dict[str, str]:
    """Return path-independent identity for the installed workflow implementation."""

    package_root = Path(__file__).resolve().parents[1]
    sources = sorted(package_root.rglob("*.py"))
    return {
        "version": __version__,
        "source_sha256": digest(
            {
                path.relative_to(package_root).as_posix(): file_sha256(path)
                for path in sources
            }
        ),
    }


def _python_import_closure(root: Path, paths: set[Path]) -> None:
    """Add the transitive repository-owned imports of declared Python inputs."""

    pending = [path for path in paths if path.suffix == ".py"]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ValueError(f"cannot inspect Python release input {path}: {exc}") from exc
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        for module in imports:
            for imported in _python_module_paths(root, module):
                if imported not in paths:
                    paths.add(imported)
                    if imported.suffix == ".py":
                        pending.append(imported)


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
    if not isinstance(value, list) or not value:
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


def _oa_port_contract(raw: Mapping[str, Any]) -> dict[str, ModulePort]:
    ports = _table(raw.get("ports"), "OA port contract ports")
    order = ports.get("order")
    directions = _table(ports.get("directions"), "OA port directions")
    if not isinstance(order, list) or any(
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


def _development_interface_check(
    contract: IpContract, exported: IpExport
) -> dict[str, Any]:
    """Validate the machine-readable boundary against shipped SV collateral."""

    root = contract.project_root
    producer = _project_path(root, Path(contract.producer), "IP producer")
    interface_path = _project_path(
        producer, Path(exported.interface_contract), "interface contract"
    )
    with interface_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
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
        exported.physical_interface, "interface.physical"
    )
    expected_logical = _identity_module(
        exported.logical_interface, "interface.logical"
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
    with oa_source.open("rb") as stream:
        oa_ports = _oa_port_contract(tomllib.load(stream))
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
    return {
        "name": f"development_interface_consistency:{exported.name}",
        "export": exported.name,
        "passed": True,
        "physical_module": physical_module,
        "physical_port_count": len(physical_ports),
        "transaction_module": logical_module,
        "transaction_port_count": len(logical_ports),
        "transaction_signature_checked": True,
        "physical_shell_module": shell_module,
        "physical_named_bindings_checked": len(bindings),
    }


@dataclass(frozen=True)
class _ReleaseSourceInputs:
    files: Mapping[str, str]
    fingerprint_sources: tuple[ReleaseFingerprintSource, ...]
    fingerprint_attributes: Mapping[str, object]


def _source_inputs(contract: IpContract) -> _ReleaseSourceInputs:
    root = contract.project_root
    component_path = _project_path(
        root, Path(contract.producer) / contract.component_contract, "component contract"
    )
    graph = load_component_graph(component_path, project_root=root)
    paths: set[Path] = {contract.path}
    locator_paths: set[Path] = {contract.path.resolve()}
    fingerprint_sources: list[ReleaseFingerprintSource] = []

    def bind(logical_role: str, source: Path) -> None:
        resolved = source.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(f"release fingerprint source is missing: {source}")
        fingerprint_sources.append(
            ReleaseFingerprintSource(logical_role=logical_role, source=resolved)
        )
        paths.add(resolved)

    component_attributes: list[dict[str, object]] = []
    for component in sorted(graph.values(), key=lambda item: item.name):
        paths.add(component.path)
        locator_paths.add(component.path.resolve())
        referenced = [path for values in component.filesets.values() for path in values]
        if component.public_interface is not None:
            referenced.append(component.public_interface)
        for relative in referenced:
            paths.add(_project_path(root, Path(relative), "component input"))
        if component.public_interface is not None:
            bind(
                f"component:{component.name}:public-interface",
                _project_path(root, Path(component.public_interface), "public interface"),
            )
        for fileset, values in sorted(component.filesets.items()):
            for index, relative in enumerate(values):
                bind(
                    f"component:{component.name}:fileset:{fileset}:{index}",
                    _project_path(root, Path(relative), "component fileset input"),
                )
        component_attributes.append(
            {
                "name": component.name,
                "kind": component.kind,
                "dependencies": sorted(item.name for item in component.components),
                "filesets": sorted(component.filesets),
            }
        )
    for relative in contract.source_files:
        path = _project_path(root, Path(relative), "source file")
        if not path.is_file():
            raise FileNotFoundError(f"IP source file is missing: {relative}")
        bind(f"release-support:{len(fingerprint_sources)}", path)

    oa_manifest = _project_path(root, Path(contract.oa_assembly), "OA assembly")
    from sigilicon.domain.oa_library import load_oa_library_source

    library = load_oa_library_source(oa_manifest, project_root=root)
    cell_by_name = {cell.cell: cell for cell in library.cells}
    netlist_cells = [
        cell
        for cell in library.cells
        if any(view.kind == "spectre_netlist" for view in cell.views)
    ]
    snapshots = [
        load_netlist_snapshot(cell.canonical_source) for cell in netlist_cells
    ]
    definitions = parse_subcircuit_definitions(snapshots)
    top_cells = {exported.oa_cell for exported in contract.exports}
    for exported in contract.exports:
        if exported.oa_library != library.name:
            raise ValueError(
                f"release export {exported.name} names OA library "
                f"{exported.oa_library}, expected {library.name}"
            )
        if exported.oa_cell not in cell_by_name or exported.oa_cell not in definitions:
            raise ValueError(
                "release OA top is absent from the canonical library: "
                f"{exported.name}/{exported.oa_cell}"
            )
    reachable = set(top_cells)
    pending = list(top_cells)
    while pending:
        cell_name = pending.pop()
        for instance in parse_subcircuit_instances(definitions[cell_name]):
            if instance.master in definitions and instance.master not in reachable:
                reachable.add(instance.master)
                pending.append(instance.master)
    top_owners = {cell_by_name[cell].owner for cell in top_cells}
    validation_cells = {
        cell.cell
        for cell in library.cells
        if cell.owner in top_owners
        and cell.role in {"testbench", "model"}
        and any(
            dependency.cell in reachable
            for view in cell.views
            for dependency in view.dependencies
        )
    }
    selected_cells = reachable | validation_cells
    paths.add(library.manifest_path)
    locator_paths.add(library.manifest_path.resolve())
    selected_cell_attributes: list[dict[str, object]] = []
    for cell in library.cells:
        if cell.cell not in selected_cells:
            continue
        paths.update(
            {
                cell.source_manifest_path,
                cell.manifest_path,
                cell.canonical_source,
                *(view.source for view in cell.views),
            }
        )
        locator_paths.update(
            {cell.source_manifest_path.resolve(), cell.manifest_path.resolve()}
        )
        bind(
            f"oa:{cell.owner}:{cell.role}:{cell.cell}:canonical",
            cell.canonical_source,
        )
        for view in cell.views:
            bind(
                f"oa:{cell.owner}:{cell.role}:{cell.cell}:view:{view.name}:{view.kind}",
                view.source,
            )
        selected_cell_attributes.append(
            {
                "owner": cell.owner,
                "role": cell.role,
                "cell": cell.cell,
                "views": [
                    {
                        "name": view.name,
                        "kind": view.kind,
                        "dependencies": sorted(
                            f"{dependency.cell}/{dependency.view}"
                            for dependency in view.dependencies
                        ),
                    }
                    for view in sorted(cell.views, key=lambda item: item.name)
                ],
            }
        )
        if cell.design_spec is not None:
            paths.add(cell.design_spec)
        if cell.role == "testbench":
            from sigilicon.domain.oa_simulation import load_oa_simulation_spec

            setup_sources = {
                view.source for view in cell.views if view.kind in {"config", "maestro"}
            }
            if len(setup_sources) != 1:
                raise ValueError(
                    f"release testbench config and Maestro sources disagree: {cell.cell}"
                )
            simulation = load_oa_simulation_spec(
                next(iter(setup_sources)), project_root=root
            )
            rdb_contract = simulation.native_setup.rdb_contract
            if rdb_contract is not None:
                paths.add(rdb_contract.path)
                bind(
                    f"oa:{cell.owner}:{cell.cell}:native-rdb",
                    rdb_contract.path,
                )
        for layout_spec in cell.layout_specs:
            paths.add(layout_spec)
            with layout_spec.open("rb") as stream:
                layout_raw = tomllib.load(stream).get("layout", {})
            if not isinstance(layout_raw, Mapping):
                raise ValueError(f"layout must be a TOML table: {layout_spec}")
            for field in ("generator_source", "source_netlist"):
                value = layout_raw.get(field)
                if value is None:
                    continue
                if not isinstance(value, str) or not value:
                    raise ValueError(f"layout.{field} must be a path: {layout_spec}")
                declared = (layout_spec.parent / value).resolve()
                if not declared.is_relative_to(root) or not declared.is_file():
                    raise FileNotFoundError(
                        f"layout.{field} input is missing: {layout_spec}: {value}"
                    )
                paths.add(declared)
            for field in ("generator_dependencies", "dependency_netlists"):
                values = layout_raw.get(field, [])
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value for value in values
                ):
                    raise ValueError(f"layout.{field} must be paths: {layout_spec}")
                for value in values:
                    declared = (layout_spec.parent / value).resolve()
                    if not declared.is_relative_to(root) or not declared.is_file():
                        raise FileNotFoundError(
                            f"layout.{field} input is missing: {layout_spec}: {value}"
                        )
                    paths.add(declared)
            modules = layout_raw.get("generator_modules", [])
            if not isinstance(modules, list) or any(
                not isinstance(module, str) or not module for module in modules
            ):
                raise ValueError(f"layout.generator_modules must be module names: {layout_spec}")
            for module in modules:
                paths.update(_python_module_paths(root, module))
    _python_import_closure(root, paths)

    covered = {item.source.resolve() for item in fingerprint_sources}
    transitive: dict[str, list[Path]] = {}
    for path in sorted(paths):
        resolved = path.resolve()
        if resolved in locator_paths or resolved in covered:
            continue
        semantic = semantic_source_sha256(resolved, project_root=root)
        transitive.setdefault(semantic, []).append(resolved)
    for semantic, values in sorted(transitive.items()):
        for index, path in enumerate(values):
            bind(f"transitive-content:{semantic}:{index}", path)

    attributes: dict[str, object] = {
        "ip_name": contract.name,
        "sigilicon_tool": _sigilicon_tool_identity(),
        # The generic release fingerprint intentionally ignores source
        # locators, but an IP package also records a source snapshot.  Include
        # the resolved closure layout in the package identity so a directory
        # reorganization cannot alias an older immutable artifact whose
        # manifest still points at the previous source paths.
        "source_layout": sorted(
            path.resolve().relative_to(root).as_posix() for path in paths
        ),
        "components": component_attributes,
        "oa_assembly": {
            "library": library.name,
            "assembly": library.name,
            "pdk": library.pdk,
            "selected_cells": sorted(
                selected_cell_attributes, key=lambda item: str(item["cell"])
            ),
        },
        "exports": [
            {
                "name": exported.name,
                "oa": {
                    "library": exported.oa_library,
                    "cell": exported.oa_cell,
                    "schematic_view": exported.schematic_view,
                    "layout_view": exported.layout_view,
                },
                "interface": {
                    "physical": exported.physical_interface,
                    "logical": exported.logical_interface,
                },
                "required_roles": {
                    level: sorted(roles)
                    for level, roles in exported.required_roles.items()
                },
            }
            for exported in sorted(contract.exports, key=lambda item: item.name)
        ],
        "collateral": [
            {
                "export": item.export,
                "role": item.role,
                "component": item.component,
                "package_path": item.package_path.as_posix(),
                "format": item.format,
                "module": item.module,
                "library": item.library,
                "cell": item.cell,
                "view": item.view,
                "corner": item.corner,
                "capabilities": sorted(item.capabilities),
            }
            for item in sorted(
                contract.collateral, key=lambda value: (value.export, value.role)
            )
        ],
    }
    return _ReleaseSourceInputs(
        files={
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(paths)
        },
        fingerprint_sources=tuple(fingerprint_sources),
        fingerprint_attributes=attributes,
    )


def _source_control(root: Path) -> tuple[str, bool]:
    revision = run_process_group(
        ["git", "rev-parse", "HEAD"], cwd=root, env=os.environ.copy(), timeout=30
    )
    if revision.returncode != 0 or not revision.stdout.strip():
        raise RuntimeError(f"cannot resolve source commit:\n{revision.stdout}")
    status_result = run_process_group(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        env=os.environ.copy(),
        timeout=30,
    )
    if status_result.returncode != 0:
        raise RuntimeError(f"cannot inspect source checkout:\n{status_result.stdout}")
    return revision.stdout.strip(), bool(status_result.stdout.strip())


def _missing_roles(contract: IpContract, level: str) -> list[str]:
    missing: list[str] = []
    for exported in contract.exports:
        present = {item.role for item in exported.collateral}
        missing.extend(
            f"{exported.name}:{role}"
            for role in sorted(set(exported.required_roles[level]) - present)
        )
    return missing


def _qualification_subject_fingerprint(
    contract: IpContract, inputs: _ReleaseSourceInputs
) -> str:
    excluded = {
        (contract.project_root / Path(item.source)).resolve()
        for item in contract.collateral
        if item.role in _QUALIFICATION_OUTPUT_ROLES
    }
    return release_source_fingerprint(
        attributes=inputs.fingerprint_attributes,
        sources=(
            item for item in inputs.fingerprint_sources if item.source not in excluded
        ),
        project_root=contract.project_root,
    )


def _receipt_problems(
    *,
    contract: IpContract,
    exported: IpExport,
    role: str,
    subject_fingerprint: str,
    source_commit: str,
    by_role: Mapping[str, Any],
) -> list[str]:
    item = by_role[role]
    source = _project_path(
        contract.project_root, Path(item.source), f"{role} source"
    )
    try:
        receipt = read_json_object(source, f"{role} receipt")
    except (OSError, ValueError) as exc:
        return [f"{exported.name}:{role}:invalid-json:{exc}"]
    problems: list[str] = []
    prefix = f"{exported.name}:{role}"
    if receipt.get("status") != "passed":
        problems.append(f"{prefix}:status")
    if receipt.get("qualification_subject_fingerprint") != subject_fingerprint:
        problems.append(f"{prefix}:source-fingerprint")
    if receipt.get("source_commit") != source_commit:
        problems.append(f"{prefix}:source-commit")
    expected_oa = {
        "library": exported.oa_library,
        "cell": exported.oa_cell,
        "schematic_view": exported.schematic_view,
        "layout_view": exported.layout_view,
    }
    if receipt.get("oa") != expected_oa:
        problems.append(f"{prefix}:oa-identity")
    tool = receipt.get("tool")
    if not isinstance(tool, Mapping) or any(
        not isinstance(tool.get(field), str) or not tool.get(field)
        for field in ("name", "version")
    ):
        problems.append(f"{prefix}:tool-version")
    rows: dict[str, str] = {}
    for field in ("inputs", "outputs"):
        values = receipt.get(field)
        if not isinstance(values, list):
            problems.append(f"{prefix}:{field}")
            continue
        for row in values:
            if (
                not isinstance(row, Mapping)
                or not isinstance(row.get("role"), str)
                or not isinstance(row.get("sha256"), str)
            ):
                problems.append(f"{prefix}:{field}-entry")
                continue
            rows[str(row["role"])] = str(row["sha256"])
    for bound_role in _SIGNOFF_RECEIPT_BINDINGS[role]:
        bound = by_role.get(bound_role)
        if bound is None:
            problems.append(f"{prefix}:missing-bound-role:{bound_role}")
            continue
        bound_source = _project_path(
            contract.project_root, Path(bound.source), f"{bound_role} source"
        )
        if rows.get(bound_role) != file_sha256(bound_source):
            problems.append(f"{prefix}:digest-binding:{bound_role}")
    return problems


def _qualification_semantics(
    contract: IpContract,
    level: str,
    *,
    subject_fingerprint: str,
    source_commit: str,
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    for exported in contract.exports:
        by_role = {item.role: item for item in exported.collateral}
        if level in {"implementation", "signoff"}:
            for role, formats in _IMPLEMENTATION_ROLE_FORMATS.items():
                item = by_role.get(role)
                if item is None:
                    continue
                prefix = f"{exported.name}:{role}"
                if item.format not in formats:
                    problems.append(f"{prefix}:format")
                if (
                    item.library != exported.oa_library
                    or item.cell != exported.oa_cell
                ):
                    problems.append(f"{prefix}:oa-identity")
                if not item.view:
                    problems.append(f"{prefix}:view")
                if role == "raw_macro_liberty_or_db" and not item.corner:
                    problems.append(f"{prefix}:corner")
        if level == "signoff":
            pex = by_role.get("pex_netlist")
            if pex is not None:
                if pex.format not in {"dspf", "spice", "spectre"}:
                    problems.append(f"{exported.name}:pex_netlist:format")
                if (
                    pex.library != exported.oa_library
                    or pex.cell != exported.oa_cell
                    or not pex.view
                    or not pex.corner
                ):
                    problems.append(
                        f"{exported.name}:pex_netlist:identity-or-corner"
                    )
            for role in _SIGNOFF_RECEIPT_BINDINGS:
                if role in by_role:
                    problems.extend(
                        _receipt_problems(
                            contract=contract,
                            exported=exported,
                            role=role,
                            subject_fingerprint=subject_fingerprint,
                            source_commit=source_commit,
                            by_role=by_role,
                        )
                    )
    return (
        {
            "name": "qualified_view_semantics",
            "passed": not problems,
            "qualification_subject_fingerprint": subject_fingerprint,
            "problems": sorted(problems),
        },
        sorted(problems),
    )


def _availability(
    level: str, roles: set[str], *, collateral_passed: bool
) -> dict[str, bool]:
    return {
        "simulation": "transaction_model" in roles,
        "synthesis": collateral_passed
        and level in {"implementation", "signoff"}
        and "integration_adapter" in roles
        and "physical_blackbox" in roles
        and "raw_macro_liberty_or_db" in roles,
        "physical_implementation": collateral_passed
        and level in {"implementation", "signoff"}
        and "integration_adapter" in roles
        and "physical_blackbox" in roles
        and "raw_macro_lef" in roles
        and "raw_macro_liberty_or_db" in roles
        and "raw_macro_gds_or_oasis" in roles
        and "raw_macro_cdl_or_lvs_netlist" in roles,
    }


def plan_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    contract = load_ip_contract(contract_path, project_root=project_root)
    level = contract.require_level(maturity or contract.default_maturity)
    inputs = _source_inputs(contract)
    source_fingerprint = release_source_fingerprint(
        attributes=inputs.fingerprint_attributes,
        sources=inputs.fingerprint_sources,
        project_root=contract.project_root,
    )
    commit, dirty = _source_control(contract.project_root)
    subject_fingerprint = _qualification_subject_fingerprint(contract, inputs)
    # The content fingerprint identifies the packaged source bytes, while the
    # suffix identifies the provenance of the immutable package.  A dirty
    # development package must never alias the clean package for the same
    # source bytes; otherwise a later clean build could silently reuse a
    # manifest whose provenance still says ``working_tree_dirty=true``.
    provenance_suffix = "dirty" if dirty else commit[:12]
    release_id = f"{level}-{source_fingerprint[:24]}-{provenance_suffix}"
    role_missing = _missing_roles(contract, level)
    component_path = _project_path(
        contract.project_root,
        Path(contract.producer) / contract.component_contract,
        "component contract",
    )
    component_graph = load_component_graph(
        component_path, project_root=contract.project_root
    )
    component = next(
        item for item in component_graph.values() if item.path == component_path
    )
    interface_checks = [
        _development_interface_check(contract, exported)
        for exported in contract.exports
    ]
    semantic_check, semantic_missing = _qualification_semantics(
        contract,
        level,
        subject_fingerprint=subject_fingerprint,
        source_commit=commit,
    )
    missing = [*role_missing, *semantic_missing]
    export_rows: list[dict[str, Any]] = []
    role_checks: list[dict[str, Any]] = []
    for exported in contract.exports:
        roles = {item.role for item in exported.collateral}
        export_missing = [
            item for item in missing if item.startswith(f"{exported.name}:")
        ]
        availability = _availability(
            level, roles, collateral_passed=not export_missing
        )
        role_checks.append(
            {
                "name": f"required_release_roles:{exported.name}",
                "export": exported.name,
                "passed": not any(
                    item.startswith(f"{exported.name}:") for item in role_missing
                ),
                "required": list(exported.required_roles[level]),
                "present": sorted(roles),
            }
        )
        export_rows.append(
            {
                "name": exported.name,
                "oa": {
                    "library": exported.oa_library,
                    "cell": exported.oa_cell,
                    "schematic_view": exported.schematic_view,
                    "layout_view": exported.layout_view,
                },
                "interface": {
                    "contract": exported.interface_contract.as_posix(),
                    "physical": exported.physical_interface,
                    "logical": exported.logical_interface,
                    "interfaces_are_distinct": (
                        exported.physical_interface != exported.logical_interface
                    ),
                },
                "maturity": {
                    "required_roles": list(exported.required_roles[level]),
                    "missing_items": export_missing,
                },
                "availability": availability,
            }
        )
    availability = {
        capability: all(
            bool(exported["availability"][capability]) for exported in export_rows
        )
        for capability in (
            "simulation",
            "synthesis",
            "physical_implementation",
        )
    }
    return {
        "ip_name": contract.name,
        "contract": contract.path.relative_to(contract.project_root).as_posix(),
        "producer": contract.producer.as_posix(),
        "component": {
            "name": component.name,
            "kind": component.kind,
            "contract": component.path.relative_to(contract.project_root).as_posix(),
        },
        "release_id": release_id,
        "release_root": (Path("ip") / contract.name / release_id).as_posix(),
        "source_commit": commit,
        "working_tree_dirty": dirty,
        "source_fingerprint": source_fingerprint,
        "qualification_subject_fingerprint": subject_fingerprint,
        "source_files": inputs.files,
        "maturity_level": level,
        "maturity_checks": [
            *role_checks,
            {
                "name": "source_fingerprint",
                "passed": True,
                "file_count": len(inputs.files),
            },
            *interface_checks,
            semantic_check,
        ],
        "missing_items": missing,
        "availability": availability,
        "exports": export_rows,
        "collateral": [
            {
                "export": item.export,
                "role": item.role,
                "component": item.component,
                "fileset": item.fileset,
                "source": item.source.as_posix(),
                "package_path": item.package_path.as_posix(),
                "format": item.format,
                "module": item.module,
                "library": item.library,
                "cell": item.cell,
                "view": item.view,
                "corner": item.corner,
                "capabilities": list(item.capabilities),
            }
            for item in contract.collateral
        ],
    }


def _readonly_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def build_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    plan = plan_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
    if plan["missing_items"]:
        missing = ", ".join(plan["missing_items"])
        raise IpReleaseError(
            f"cannot build {plan['maturity_level']} IP release; missing: {missing}"
        )
    if plan["working_tree_dirty"] and plan["maturity_level"] != "development":
        raise IpReleaseError(
            "implementation and signoff releases require a clean source checkout"
        )
    contract = load_ip_contract(contract_path, project_root=project_root)
    release_root = artifact_root.resolve() / Path(plan["release_root"])
    if release_root.exists():
        return audit_ip_release(
            contract_path,
            project_root=project_root,
            artifact_root=artifact_root,
            maturity=maturity,
        )
    namespace = release_root.parent
    namespace.mkdir(parents=True, exist_ok=True)
    temporary = namespace / f".{plan['release_id']}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        views: list[dict[str, Any]] = []
        for item in contract.collateral:
            source = _project_path(
                contract.project_root,
                Path(item.source),
                "collateral source",
            )
            destination = temporary / item.package_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            views.append(
                {
                    "export": item.export,
                    "role": item.role,
                    "path": item.package_path.as_posix(),
                    "sha256": file_sha256(destination),
                    "size": destination.stat().st_size,
                    "format": item.format,
                    "module": item.module,
                    "library": item.library,
                    "cell": item.cell,
                    "view": item.view,
                    "corner": item.corner,
                    "capabilities": list(item.capabilities),
                }
            )
        manifest: dict[str, Any] = {
            "schema": 1,
            "contract_kind": "ip-release-manifest",
            "release_kind": "source-package",
            "ip_name": plan["ip_name"],
            "release_id": plan["release_id"],
            "source_commit": plan["source_commit"],
            "source_fingerprint": plan["source_fingerprint"],
            "qualification_subject_fingerprint": plan[
                "qualification_subject_fingerprint"
            ],
            "source_files": plan["source_files"],
            "component": plan["component"],
            "exports": plan["exports"],
            "views": views,
            "maturity": {
                "level": plan["maturity_level"],
                "checks": plan["maturity_checks"],
                "missing_items": plan["missing_items"],
            },
            "provenance": {
                "contract": plan["contract"],
                "producer": plan["producer"],
                "generated_at": utc_now(),
                "generator": "flow-ip-packaging",
                "working_tree_dirty": plan["working_tree_dirty"],
            },
            "availability": plan["availability"],
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        _readonly_tree(temporary)
        try:
            os.replace(temporary, release_root)
        except FileExistsError:
            pass
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return audit_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )


def _load_release(release_root: Path) -> dict[str, Any]:
    return read_json_object(release_root / "manifest.json", "IP release manifest")


def load_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load one exact immutable release manifest without consulting a channel pointer."""

    if manifest_path.name != "manifest.json" or not manifest_path.is_file():
        raise FileNotFoundError(f"IP release manifest is missing: {manifest_path}")
    return _load_release(manifest_path.parent)


def _manifest_exports(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = manifest.get("exports")
    if not isinstance(values, list) or not values:
        raise RuntimeError("IP release manifest has no exports")
    exports: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(values):
        if not isinstance(item, Mapping):
            raise RuntimeError(f"IP release export {index} is invalid")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in exports:
            raise RuntimeError("IP release export identities must be non-empty and unique")
        exports[name] = item
    return exports


def release_role_view(
    manifest: Mapping[str, Any], role: str, *, export: str
) -> Mapping[str, Any]:
    """Return one role from one exact export of an IP release."""

    _manifest_exports(manifest)[export]
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release manifest has no views")
    matches = [
        item
        for item in views
        if isinstance(item, Mapping)
        and item.get("export") == export
        and item.get("role") == role
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"IP release must contain exactly one {export}/{role} view"
        )
    return matches[0]


def _packaged_interface_check(
    manifest: Mapping[str, Any], manifest_path: Path
) -> None:
    for export_name, exported in _manifest_exports(manifest).items():
        interface = exported.get("interface")
        if not isinstance(interface, Mapping):
            raise RuntimeError(f"IP release export {export_name} has no interface")
        physical_identity = interface.get("physical")
        logical_identity = interface.get("logical")
        if not isinstance(physical_identity, str) or not isinstance(
            logical_identity, str
        ):
            raise RuntimeError(
                f"IP release export {export_name} interface identities are invalid"
            )
        try:
            physical_module = _identity_module(
                physical_identity, f"exports.{export_name}.interface.physical"
            )
            logical_module = _identity_module(
                logical_identity, f"exports.{export_name}.interface.logical"
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        contract_path = resolve_release_role(
            manifest, manifest_path, "interface_contract", export=export_name
        )
        try:
            with contract_path.open("rb") as stream:
                raw: dict[str, Any] = tomllib.load(stream)
            physical = _table(raw.get("physical_macro"), "physical_macro")
            transaction = _table(
                raw.get("transaction_boundary"), "transaction_boundary"
            )
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                f"packaged {export_name} interface contract is invalid: {exc}"
            ) from exc
        shell_module = transaction.get("ams_wrapper_module")
        expected_modules = {
            "physical_blackbox": physical_module,
            "transaction_model": logical_module,
            "integration_adapter": shell_module,
        }
        if (
            physical.get("module") != physical_module
            or transaction.get("module") != logical_module
        ):
            raise RuntimeError(
                f"packaged {export_name} interface identities disagree with its manifest"
            )
        for role, module in expected_modules.items():
            if not isinstance(module, str) or release_role_view(
                manifest, role, export=export_name
            ).get("module") != module:
                raise RuntimeError(
                    f"packaged {export_name}/{role} module disagrees with its interface"
                )

        try:
            expected_transaction_ports = _interface_ports(
                transaction.get("ports"), "transaction_boundary.ports"
            )
        except ValueError as exc:
            raise RuntimeError(
                f"packaged {export_name} transaction ports are invalid: {exc}"
            ) from exc
        sources = {
            role: resolve_release_role(
                manifest, manifest_path, role, export=export_name
            )
            for role in (
                "transaction_model",
                "integration_adapter",
                "physical_blackbox",
                "oa_port_contract",
                "circuit_netlist",
            )
        }
        try:
            actual_transaction_ports = module_port_signatures(
                sources["transaction_model"].read_text(encoding="utf-8"),
                logical_module,
            )
            shell_text = sources["integration_adapter"].read_text(encoding="utf-8")
            actual_shell_ports = module_port_signatures(
                shell_text, str(shell_module)
            )
            actual_physical_ports = module_port_signatures(
                sources["physical_blackbox"].read_text(encoding="utf-8"),
                physical_module,
            )
            with sources["oa_port_contract"].open("rb") as stream:
                expected_physical_ports = _oa_port_contract(tomllib.load(stream))
            circuit_ports = subckt_ports(
                sources["circuit_netlist"], physical_module
            )
            bindings = named_port_connections(
                shell_text, str(shell_module), physical_module
            )
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                f"packaged {export_name} SystemVerilog interface is invalid: {exc}"
            ) from exc
        if actual_transaction_ports != expected_transaction_ports:
            raise RuntimeError(
                f"packaged {export_name} transaction model signature disagrees"
            )
        if (
            dict(list(actual_shell_ports.items())[: len(expected_transaction_ports)])
            != expected_transaction_ports
        ):
            raise RuntimeError(
                f"packaged {export_name} physical shell signature disagrees"
            )
        if actual_physical_ports != expected_physical_ports:
            raise RuntimeError(
                f"packaged {export_name} physical blackbox disagrees with its OA ports"
            )
        if circuit_ports != tuple(expected_physical_ports):
            raise RuntimeError(
                f"packaged {export_name} circuit pin order disagrees with its OA ports"
            )
        if tuple(bindings) != tuple(expected_physical_ports):
            raise RuntimeError(
                f"packaged {export_name} physical named bindings disagree"
            )


def _packaged_maturity_check(
    manifest: Mapping[str, Any], manifest_path: Path
) -> None:
    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping):
        raise RuntimeError("IP release maturity identity is missing")
    level = maturity.get("level")
    if level not in RELEASE_MATURITY_LEVELS:
        raise RuntimeError("IP release maturity level is invalid")
    problems: list[str] = []
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release views must be a list")
    for export_name, exported in _manifest_exports(manifest).items():
        oa = exported.get("oa")
        export_maturity = exported.get("maturity")
        if not isinstance(oa, Mapping) or not isinstance(
            export_maturity, Mapping
        ):
            problems.append(f"{export_name}:maturity-identity")
            continue
        by_role = {
            str(view.get("role")): view
            for view in views
            if isinstance(view, Mapping)
            and view.get("export") == export_name
            and isinstance(view.get("role"), str)
        }
        required = export_maturity.get("required_roles")
        if not isinstance(required, list) or any(
            not isinstance(role, str) or not role for role in required
        ):
            problems.append(f"{export_name}:required-roles")
            required = []
        for role in required:
            if role not in by_role:
                problems.append(f"{export_name}:{role}:missing")
        if level in {"implementation", "signoff"}:
            for role, formats in _IMPLEMENTATION_ROLE_FORMATS.items():
                view = by_role.get(role)
                if view is None:
                    problems.append(f"{export_name}:{role}:missing")
                    continue
                if view.get("format") not in formats:
                    problems.append(f"{export_name}:{role}:format")
                if view.get("library") != oa.get("library") or view.get(
                    "cell"
                ) != oa.get("cell"):
                    problems.append(f"{export_name}:{role}:oa-identity")
                if not isinstance(view.get("view"), str) or not view.get("view"):
                    problems.append(f"{export_name}:{role}:view")
                if role == "raw_macro_liberty_or_db" and not view.get("corner"):
                    problems.append(f"{export_name}:{role}:corner")
        if level != "signoff":
            continue
        pex = by_role.get("pex_netlist")
        if pex is None:
            problems.append(f"{export_name}:pex_netlist:missing")
        elif (
            pex.get("format") not in {"dspf", "spice", "spectre"}
            or pex.get("library") != oa.get("library")
            or pex.get("cell") != oa.get("cell")
            or not pex.get("view")
            or not pex.get("corner")
        ):
            problems.append(f"{export_name}:pex_netlist:identity-or-corner")
        subject = manifest.get("qualification_subject_fingerprint")
        source_commit = manifest.get("source_commit")
        for role, bindings in _SIGNOFF_RECEIPT_BINDINGS.items():
            if role not in by_role:
                problems.append(f"{export_name}:{role}:missing")
                continue
            receipt_path = resolve_release_role(
                manifest, manifest_path, role, export=export_name
            )
            try:
                receipt = read_json_object(
                    receipt_path, f"{export_name}/{role} receipt"
                )
            except (OSError, ValueError) as exc:
                problems.append(f"{export_name}:{role}:invalid-json:{exc}")
                continue
            if receipt.get("status") != "passed":
                problems.append(f"{export_name}:{role}:status")
            if receipt.get("qualification_subject_fingerprint") != subject:
                problems.append(f"{export_name}:{role}:source-fingerprint")
            if receipt.get("source_commit") != source_commit:
                problems.append(f"{export_name}:{role}:source-commit")
            expected_oa = {
                "library": oa.get("library"),
                "cell": oa.get("cell"),
                "schematic_view": oa.get("schematic_view"),
                "layout_view": oa.get("layout_view"),
            }
            if receipt.get("oa") != expected_oa:
                problems.append(f"{export_name}:{role}:oa-identity")
            tool = receipt.get("tool")
            if not isinstance(tool, Mapping) or any(
                not isinstance(tool.get(field), str) or not tool.get(field)
                for field in ("name", "version")
            ):
                problems.append(f"{export_name}:{role}:tool-version")
            rows: dict[str, str] = {}
            for field in ("inputs", "outputs"):
                values = receipt.get(field)
                if not isinstance(values, list):
                    problems.append(f"{export_name}:{role}:{field}")
                    continue
                for row in values:
                    if isinstance(row, Mapping) and isinstance(row.get("role"), str):
                        rows[str(row["role"])] = str(row.get("sha256", ""))
            for bound_role in bindings:
                bound = by_role.get(bound_role)
                if bound is None or rows.get(bound_role) != bound.get("sha256"):
                    problems.append(
                        f"{export_name}:{role}:digest-binding:{bound_role}"
                    )
    if problems:
        raise RuntimeError(
            "IP release qualified view semantics are invalid: "
            + ", ".join(sorted(problems))
        )


def audit_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    """Audit an exact immutable package without consulting producer source."""

    manifest = load_ip_release_manifest(manifest_path)
    if (
        manifest.get("schema") != 1
        or manifest.get("contract_kind") != "ip-release-manifest"
    ):
        raise RuntimeError("unsupported IP release manifest schema")
    if manifest.get("release_kind") == "flow-promotion":
        return _audit_promoted_ip_release_manifest(manifest_path)
    if manifest.get("release_kind") != "source-package":
        raise RuntimeError("unsupported IP release kind")
    release_root = manifest_path.parent.resolve()
    views = manifest.get("views")
    if not isinstance(views, list) or not views:
        raise RuntimeError("IP release views must be a non-empty list")
    exports = _manifest_exports(manifest)
    roles: set[tuple[str, str]] = set()
    expected_files = {Path("manifest.json")}
    for view in views:
        if not isinstance(view, Mapping) or not isinstance(view.get("role"), str):
            raise RuntimeError("IP release view entry is invalid")
        role = str(view["role"])
        export = view.get("export")
        if not isinstance(export, str) or export not in exports:
            raise RuntimeError(f"IP release view names an unknown export: {export}")
        role_key = (export, role)
        if role_key in roles:
            raise RuntimeError(
                f"IP release contains duplicate role: {export}/{role}"
            )
        roles.add(role_key)
        relative = Path(str(view.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RuntimeError("IP release view path is unsafe")
        path = (release_root / relative).resolve()
        if not path.is_relative_to(release_root) or not path.is_file():
            raise RuntimeError(f"IP release view is missing: {relative}")
        if file_sha256(path) != view.get("sha256") or path.stat().st_size != view.get(
            "size"
        ):
            raise RuntimeError(f"IP release view digest drifted: {relative}")
        expected_files.add(relative)
    actual_files = {
        path.relative_to(release_root)
        for path in release_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("IP release file inventory does not match its manifest")
    _packaged_interface_check(manifest, manifest_path)
    _packaged_maturity_check(manifest, manifest_path)
    return manifest


def _audit_source_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    plan = plan_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
    release_root = artifact_root.resolve() / Path(plan["release_root"])
    if not release_root.is_dir() or release_root.is_symlink():
        raise FileNotFoundError(f"IP release has not been built: {release_root}")
    manifest = audit_ip_release_manifest(release_root / "manifest.json")
    expected = {
        "ip_name": plan["ip_name"],
        "release_id": plan["release_id"],
        "source_fingerprint": plan["source_fingerprint"],
        "source_files": plan["source_files"],
        "component": plan["component"],
        "exports": plan["exports"],
        "availability": plan["availability"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"IP release manifest {key} does not match its source plan")
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise RuntimeError("IP release source_commit is invalid")
    maturity_data = manifest.get("maturity")
    if not isinstance(maturity_data, Mapping):
        raise RuntimeError("IP release maturity record is missing")
    if (
        maturity_data.get("level") != plan["maturity_level"]
        or maturity_data.get("missing_items") != []
        or maturity_data.get("checks") != plan["maturity_checks"]
    ):
        raise RuntimeError("IP release maturity record is inconsistent")
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release views must be a list")
    expected_roles = {
        (item["export"], item["role"]) for item in plan["collateral"]
    }
    actual_roles: set[tuple[str, str]] = set()
    expected_views = {
        (item["export"], item["role"]): item for item in plan["collateral"]
    }
    for view in views:
        if not isinstance(view, Mapping):
            raise RuntimeError("IP release view entry is invalid")
        relative = Path(str(view.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RuntimeError("IP release view path is unsafe")
        path = (release_root / relative).resolve()
        if not path.is_relative_to(release_root.resolve()) or not path.is_file():
            raise RuntimeError(f"IP release view is missing: {relative}")
        if file_sha256(path) != view.get("sha256") or path.stat().st_size != view.get("size"):
            raise RuntimeError(f"IP release view digest drifted: {relative}")
        role = str(view.get("role"))
        export = str(view.get("export"))
        role_key = (export, role)
        expected_view = expected_views.get(role_key)
        if (
            expected_view is None
            or view.get("path") != expected_view["package_path"]
            or any(
                view.get(field) != expected_view[field]
                for field in ("format", "module", "corner", "capabilities")
            )
        ):
            raise RuntimeError(
                f"IP release view metadata drifted: {export}/{role}"
            )
        actual_roles.add(role_key)
    if actual_roles != expected_roles:
        raise RuntimeError("IP release view roles do not match the producer contract")
    expected_files = {
        Path("manifest.json"),
        *(Path(str(view["path"])) for view in views),
    }
    actual_files: set[Path] = set()
    for path in release_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"IP release cannot contain symlinks: {path}")
        if path.is_file():
            relative = path.relative_to(release_root)
            actual_files.add(relative)
            if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError(f"IP release file is writable: {relative}")
        elif path.is_dir() and path.stat().st_mode & (
            stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        ):
            raise RuntimeError(
                f"IP release directory is writable: {path.relative_to(release_root)}"
            )
    if actual_files != expected_files:
        raise RuntimeError("IP release file inventory does not match its manifest")
    return {
        **manifest,
        "manifest": (release_root / "manifest.json")
        .relative_to(artifact_root.resolve())
        .as_posix(),
        "audit": {"passed": True, "audited_at": utc_now()},
    }


def publish_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    plan = plan_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
    if plan["working_tree_dirty"]:
        raise IpReleaseError(
            "cannot publish an immutable IP release from a dirty source checkout"
        )
    audited = audit_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
    provenance = audited.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("working_tree_dirty"):
        raise IpReleaseError(
            "cannot publish a release candidate that was built from dirty source"
        )
    manifest = artifact_root.resolve() / Path(str(audited["manifest"]))
    namespace = artifact_root.resolve() / "ip" / str(audited["ip_name"])
    pointer = {
        "ip_name": audited["ip_name"],
        "release_id": audited["release_id"],
        "maturity": audited["maturity"]["level"],
        "source_fingerprint": audited["source_fingerprint"],
        "manifest": manifest.relative_to(artifact_root.resolve()).as_posix(),
        "published_at": utc_now(),
    }
    atomic_write_json(namespace / "current.json", pointer)
    return pointer


def load_published_ip(
    pointer_path: Path,
    *,
    artifact_root: Path,
) -> tuple[dict[str, Any], Path]:
    pointer = read_json_object(pointer_path, "IP current pointer")
    relative = Path(str(pointer.get("manifest", "")))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RuntimeError("IP current pointer manifest path is unsafe")
    manifest_path = (artifact_root.resolve() / relative).resolve()
    if not manifest_path.is_relative_to(artifact_root.resolve()):
        raise RuntimeError("IP current pointer escapes the artifact root")
    manifest = _load_release(manifest_path.parent)
    for key in ("ip_name", "release_id", "source_fingerprint"):
        if manifest.get(key) != pointer.get(key):
            raise RuntimeError(f"IP current pointer {key} is inconsistent")
    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping) or (
        maturity.get("level") != pointer.get("maturity")
    ):
        raise RuntimeError("IP current pointer maturity is inconsistent")
    return manifest, manifest_path


def resolve_release_role(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    role: str,
    *,
    export: str,
) -> Path:
    view = release_role_view(manifest, role, export=export)
    path = (manifest_path.parent / str(view.get("path"))).resolve()
    if not path.is_relative_to(manifest_path.parent.resolve()) or not path.is_file():
        raise RuntimeError(f"IP release role {export}/{role} is missing")
    if file_sha256(path) != view.get("sha256"):
        raise RuntimeError(f"IP release role {export}/{role} digest drifted")
    return path


def _promotion_run_root(
    contract: IpPromotionContract,
    artifact_root: Path,
    references: tuple[RunArtifactReference, ...],
) -> Path:
    reference = references[0]
    root = artifact_root.resolve()
    if root == Path(root.anchor):
        raise IpReleaseError("promotion artifact root cannot be a filesystem root")
    run_root = (
        root
        / "flows"
        / contract.owner
        / reference.flow_id
        / "runs"
        / reference.run_id
    )
    if (
        not run_root.resolve(strict=False).is_relative_to(root)
        or not run_root.is_dir()
        or run_root.is_symlink()
    ):
        raise IpReleaseError("promotion Flow Run does not exist or is unsafe")
    return run_root


def _promotion_source_identity(
    contract: IpPromotionContract,
    plan: Mapping[str, Any],
) -> dict[str, object]:
    nodes = plan.get("nodes")
    if not isinstance(nodes, list):
        raise IpReleaseError("promotion run has no resolved nodes")
    identities: set[tuple[str, bool]] = set()
    for node in nodes:
        source_assets = node.get("source_assets") if isinstance(node, Mapping) else None
        git = (
            source_assets.get("git")
            if isinstance(source_assets, Mapping)
            else None
        )
        if not isinstance(git, Mapping):
            continue
        commit = git.get("commit")
        dirty = git.get("dirty")
        if not isinstance(commit, str) or not isinstance(dirty, bool):
            raise IpReleaseError("promotion run source identity is invalid")
        identities.add((commit, dirty))
    if len(identities) != 1:
        raise IpReleaseError("promotion requires one canonical Git source identity")
    commit, dirty = identities.pop()
    if dirty:
        raise IpReleaseError("cannot promote a release from dirty source")
    if commit != contract.source_commit or dirty != contract.source_dirty:
        raise IpReleaseError("promotion source identity differs from its request")
    return {"commit": commit, "dirty": dirty}


def _promotion_policy(
    plan: Mapping[str, Any],
    policy_id: str,
) -> PolicySpec:
    policies = plan.get("policies")
    if not isinstance(policies, list):
        raise IpReleaseError("promotion run has no policies")
    matches = [
        value
        for value in policies
        if isinstance(value, Mapping) and value.get("policy_id") == policy_id
    ]
    if len(matches) != 1:
        raise IpReleaseError(f"promotion required policy is missing: {policy_id}")
    checks = matches[0].get("checks")
    if not isinstance(checks, list) or not checks:
        raise IpReleaseError(f"promotion required policy has no checks: {policy_id}")
    try:
        return PolicySpec(
            policy_id=policy_id,
            checks=tuple(
                PolicyCheck(
                    check_id=str(check["check_id"]),
                    fact=str(check["fact"]),
                    operator=str(check["operator"]),
                    expected=check.get("expected"),
                )
                for check in checks
                if isinstance(check, Mapping)
            ),
        )
    except (KeyError, ValueError) as exc:
        raise IpReleaseError(
            f"promotion required policy is malformed: {policy_id}"
        ) from exc


def _promotion_evidence_receipt(
    evidence: PromotionEvidence,
    *,
    run_root: Path,
    result: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    nodes = result.get("nodes")
    node = nodes.get(evidence.node_id) if isinstance(nodes, Mapping) else None
    if not isinstance(node, Mapping) or (
        node.get("execution_status") != "succeeded"
        or node.get("result_status") != "valid"
        or node.get("status") != "accepted"
    ):
        raise IpReleaseError(
            f"promotion evidence producer is not accepted: {evidence.node_id}"
        )
    try:
        action_result = read_json_object(
            run_root / "nodes" / evidence.node_id / "action_result.json",
            f"promotion {evidence.role} Action Result",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise IpReleaseError(
            f"promotion evidence producer is not accepted: {evidence.node_id}"
        ) from exc
    execution = action_result.get("execution")
    if (
        action_result.get("schema") != 1
        or action_result.get("contract_kind") != "action-result"
        or action_result.get("node") != evidence.node_id
        or action_result.get("result_status") != "valid"
        or not isinstance(execution, Mapping)
        or execution.get("status") != "succeeded"
        or action_result.get("facts") != node.get("facts")
    ):
        raise IpReleaseError(
            f"promotion evidence producer is not accepted: {evidence.node_id}"
        )
    if evidence.evaluation == "producer":
        try:
            receipt = read_json_object(
                run_root / "nodes" / evidence.node_id / "policy_receipt.json",
                f"promotion {evidence.role} Policy Receipt",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise IpReleaseError(
                f"promotion required policy is missing: {evidence.policy}"
            ) from exc
        if (
            receipt.get("schema") != 1
            or receipt.get("contract_kind") != "policy-receipt"
            or receipt.get("policy") != evidence.policy
            or receipt.get("status") != "accepted"
        ):
            raise IpReleaseError(
                f"promotion required policy was not accepted: {evidence.policy}"
            )
        return dict(receipt)

    facts = node.get("facts")
    if not isinstance(facts, Mapping):
        raise IpReleaseError(f"promotion evidence facts are missing: {evidence.node_id}")
    evaluated = evaluate_policy(_promotion_policy(plan, evidence.policy), facts)
    if evaluated.status != "accepted":
        raise IpReleaseError(
            f"promotion required policy was not accepted: {evidence.policy}"
        )
    return {
        "schema": 1,
        "contract_kind": "policy-receipt",
        "policy": evaluated.policy_id,
        "status": evaluated.status,
        "checks": [
            {
                "check_id": check.check_id,
                "status": check.status,
                "actual": check.actual,
                "expected": check.expected,
            }
            for check in evaluated.checks
        ],
    }


def _validate_conclusions(
    conclusions: Mapping[str, Any],
    evidence_roles: set[str],
) -> None:
    fields = {
        "implementation_regression",
        "physical_completion_readiness",
        "qualification",
        "signoff",
    }
    if set(conclusions) != fields or any(
        not isinstance(conclusions[field], bool) for field in fields
    ):
        raise IpReleaseError("promotion conclusions do not match the current schema")
    if (
        conclusions["implementation_regression"]
        and "regression" not in evidence_roles
    ):
        raise IpReleaseError(
            "implementation regression conclusion lacks regression evidence"
        )
    if (
        conclusions["physical_completion_readiness"]
        and "readiness" not in evidence_roles
    ):
        raise IpReleaseError(
            "physical completion readiness conclusion lacks readiness evidence"
        )
    if conclusions["qualification"] and "qualification" not in evidence_roles:
        raise IpReleaseError(
            "qualification cannot be claimed from diagnostic, regression, or readiness evidence"
        )
    if conclusions["signoff"] and "signoff" not in evidence_roles:
        raise IpReleaseError(
            "signoff cannot be claimed from diagnostic, regression, or readiness evidence"
        )
    if conclusions["signoff"] and not conclusions["qualification"]:
        raise IpReleaseError("signoff conclusion requires qualification")


def _validate_promotion_conclusions(
    contract: IpPromotionContract,
    evidence: tuple[PromotionEvidence, ...],
) -> None:
    _validate_conclusions(
        contract.conclusions,
        {item.evidence_role for item in evidence},
    )


def _git_interface_bytes(contract: IpPromotionContract) -> bytes:
    relative = (Path(contract.producer) / contract.interface_contract).as_posix()
    command = run_process_group(
        ["git", "show", f"{contract.source_commit}:{relative}"],
        cwd=contract.project_root,
        env=os.environ.copy(),
        timeout=30,
    )
    if command.returncode != 0:
        raise IpReleaseError(
            "required interface is absent from the promoted source commit"
        )
    return command.stdout.encode()


def _promotion_reference_payload(
    reference: RunArtifactReference,
) -> dict[str, Any]:
    payload = run_artifact_reference_payload(reference)
    payload.pop("schema")
    payload.pop("contract_kind")
    return payload


def _copy_promoted_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    try:
        manifest = read_json_object(source, "promoted durable artifact")
    except (OSError, RuntimeError, ValueError):
        return
    if manifest.get("contract_kind") != "artifact-directory-manifest":
        return
    root = manifest.get("root")
    relative = Path(root) if isinstance(root, str) else Path()
    if (
        not isinstance(root, str)
        or not root
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in root
    ):
        raise IpReleaseError("promoted directory artifact root is unsafe")
    source_root = source.parent / relative
    destination_root = destination.parent / relative
    if not source_root.is_dir() or source_root.is_symlink():
        raise IpReleaseError("promoted directory artifact root is missing")
    shutil.copytree(source_root, destination_root)


def _audit_promoted_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = load_ip_release_manifest(manifest_path)
    fields = {
        "schema",
        "contract_kind",
        "release_kind",
        "owner",
        "ip_name",
        "export",
        "release_id",
        "release_fingerprint",
        "maturity",
        "source",
        "interface",
        "artifacts",
        "evidence",
        "conclusions",
        "boundaries",
        "provenance",
    }
    if (
        set(manifest) != fields
        or manifest.get("schema") != 1
        or manifest.get("contract_kind") != "ip-release-manifest"
        or manifest.get("release_kind") != "flow-promotion"
    ):
        raise RuntimeError("unsupported promoted IP release manifest")
    release_root = manifest_path.parent.resolve()
    owner = manifest.get("owner")
    ip_name = manifest.get("ip_name")
    export = manifest.get("export")
    release_id = manifest.get("release_id")
    release_fingerprint = manifest.get("release_fingerprint")
    if any(
        not isinstance(value, str) or not value
        for value in (owner, ip_name, export, release_id, release_fingerprint)
    ):
        raise RuntimeError("promoted IP release identity is invalid")
    if release_root.name != release_id or release_root.parent.name != ip_name:
        raise RuntimeError("promoted IP release path does not match its identity")
    maturity = manifest.get("maturity")
    if (
        not isinstance(maturity, Mapping)
        or set(maturity) != {"level"}
        or maturity.get("level") not in RELEASE_MATURITY_LEVELS
    ):
        raise RuntimeError("promoted IP release maturity is invalid")
    source = manifest.get("source")
    if (
        not isinstance(source, Mapping)
        or set(source) != {"commit", "dirty"}
        or source.get("dirty") is not False
        or not isinstance(source.get("commit"), str)
        or len(source["commit"]) not in {40, 64}
        or any(
            character not in "0123456789abcdef"
            for character in source["commit"]
        )
    ):
        raise RuntimeError("promoted IP release source identity is invalid")
    expected_files = {Path("manifest.json")}
    interface = manifest.get("interface")
    if not isinstance(interface, Mapping) or set(interface) != {
        "path",
        "digest",
        "logical",
        "physical",
    }:
        raise RuntimeError("promoted IP release interface is missing")
    interface_relative = Path(str(interface.get("path", "")))
    interface_path = (release_root / interface_relative).resolve()
    if (
        interface_relative.is_absolute()
        or ".." in interface_relative.parts
        or not interface_path.is_relative_to(release_root)
        or not interface_path.is_file()
        or file_sha256(interface_path) != interface.get("digest")
    ):
        raise RuntimeError("promoted IP release interface drifted")
    expected_files.add(interface_relative)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("promoted IP release artifacts are missing")
    reference_rows: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, Mapping) or set(item) != {
            "reference",
            "path",
            "digest",
        }:
            raise RuntimeError("promoted IP release artifact is invalid")
        relative = Path(str(item.get("path", "")))
        path = (release_root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(release_root)
            or not path.is_file()
            or file_sha256(path) != item.get("digest")
        ):
            raise RuntimeError("promoted IP release artifact drifted")
        expected_files.add(relative)
        try:
            reference_raw = item.get("reference")
            if not isinstance(reference_raw, Mapping):
                raise RuntimeError("promoted IP release reference is missing")
            reference = load_run_artifact_reference(
                {
                    "schema": 1,
                    "contract_kind": "run-artifact-reference",
                    **reference_raw,
                }
            )
            members = validate_durable_artifact(
                path,
                kind=reference.kind,
                qualifiers=reference.qualifiers,
                digest=reference.digest,
            )
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError("promoted IP release artifact drifted") from exc
        reference_rows.append(_promotion_reference_payload(reference))
        expected_files.update(
            member.relative_to(release_root) for member in members
        )

    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("promoted IP release evidence is missing")
    evidence_roles: set[str] = set()
    evidence_receipts: list[dict[str, Any]] = []
    for item in evidence:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "node",
                "role",
                "policy",
                "evidence_role",
                "status",
                "path",
                "digest",
            }
            or item.get("status") != "accepted"
        ):
            raise RuntimeError("promoted IP release evidence is not accepted")
        evidence_role = item.get("evidence_role")
        if evidence_role not in {
            "diagnostic",
            "regression",
            "readiness",
            "qualification",
            "signoff",
        }:
            raise RuntimeError("promoted IP release evidence role is invalid")
        role = item.get("role")
        if not isinstance(role, str) or not role or role in evidence_roles:
            raise RuntimeError("promoted IP release evidence roles are not unique")
        evidence_roles.add(role)
        relative = Path(str(item.get("path", "")))
        path = (release_root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(release_root)
            or not path.is_file()
            or file_sha256(path) != item.get("digest")
        ):
            raise RuntimeError("promoted IP release evidence drifted")
        receipt = read_json_object(path, "promoted IP release Policy Receipt")
        if (
            receipt.get("schema") != 1
            or receipt.get("contract_kind") != "policy-receipt"
            or receipt.get("policy") != item.get("policy")
            or receipt.get("status") != "accepted"
        ):
            raise RuntimeError("promoted IP release evidence receipt is invalid")
        evidence_receipts.append(dict(receipt))
        expected_files.add(relative)

    conclusions = manifest.get("conclusions")
    if not isinstance(conclusions, Mapping):
        raise RuntimeError("promoted IP release conclusions are missing")
    _validate_conclusions(
        conclusions,
        {
            str(item["evidence_role"])
            for item in evidence
            if isinstance(item, Mapping)
        },
    )
    boundaries = manifest.get("boundaries")
    if not isinstance(boundaries, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"kind", "status", "summary", "subjects"}
        for item in boundaries
    ):
        raise RuntimeError("promoted IP release boundaries are invalid")
    provenance = manifest.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != {"contract", "generator"}
        or provenance.get("generator") != "sigilicon-flow-promotion"
    ):
        raise RuntimeError("promoted IP release provenance is invalid")

    expected_fingerprint = digest(
        {
            "schema": 1,
            "owner": owner,
            "ip_name": ip_name,
            "export": export,
            "maturity": dict(maturity),
            "source": dict(source),
            "interface": {
                "logical": interface.get("logical"),
                "physical": interface.get("physical"),
                "digest": interface.get("digest"),
            },
            "artifacts": reference_rows,
            "evidence": evidence_receipts,
            "conclusions": dict(conclusions),
            "boundaries": boundaries,
        }
    )
    expected_release_id = (
        f"{maturity['level']}-{expected_fingerprint[:24]}-"
        f"{source['commit'][:12]}"
    )
    if (
        release_fingerprint != expected_fingerprint
        or release_id != expected_release_id
    ):
        raise RuntimeError("promoted IP release identity digest is inconsistent")
    actual_files = {
        path.relative_to(release_root)
        for path in release_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("promoted IP release inventory drifted")
    return manifest


def audit_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    """Audit the exact release selected by the configured IP contract."""

    try:
        with contract_path.open("rb") as stream:
            contract_kind = tomllib.load(stream).get("contract_kind")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read IP release contract {contract_path}: {exc}") from exc
    if contract_kind == "ip-promotion":
        return audit_promoted_ip_release(
            contract_path,
            project_root=project_root,
            artifact_root=artifact_root,
            maturity=maturity,
        )
    return _audit_source_ip_release(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )


@dataclass(frozen=True)
class _ResolvedPromotion:
    contract: IpPromotionContract
    references: tuple[RunArtifactReference, ...]
    artifacts: tuple[InputArtifact, ...]
    source: dict[str, Any]
    receipts: tuple[dict[str, Any], ...]
    interface_bytes: bytes
    interface_digest: str
    boundaries: tuple[dict[str, Any], ...]
    release_fingerprint: str
    release_id: str
    release_root: Path


def _resolve_ip_promotion(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> _ResolvedPromotion:
    contract = load_ip_promotion_contract(
        contract_path,
        project_root=project_root,
    )
    if maturity is not None and maturity != contract.maturity:
        raise IpReleaseError(
            "promotion maturity differs from the configured release: "
            f"expected {contract.maturity}, got {maturity}"
        )
    _validate_promotion_conclusions(contract, contract.evidence)
    try:
        references = tuple(
            load_run_artifact_reference(value) for value in contract.artifacts
        )
    except (TypeError, ValueError) as exc:
        raise IpReleaseError(f"promotion Run Artifact reference is invalid: {exc}") from exc
    run_root = _promotion_run_root(contract, artifact_root, references)
    engine = FlowEngine(FlowRegistry())
    try:
        resolved = tuple(
            engine.resolve_run_artifact(
                artifact_root=artifact_root,
                consumer_owner=contract.owner,
                reference=reference,
            )
            for reference in references
        )
    except Exception as exc:
        raise IpReleaseError(
            f"promotion Run Artifact identity/digest validation failed: {exc}"
        ) from exc

    try:
        plan = read_json_object(run_root / "resolved_plan.json", "promotion Flow Plan")
        result = read_json_object(run_root / "flow_result.json", "promotion Flow Result")
    except (OSError, RuntimeError, ValueError) as exc:
        raise IpReleaseError(f"promotion run records are incomplete: {exc}") from exc
    source = _promotion_source_identity(contract, plan)
    receipts = tuple(
        _promotion_evidence_receipt(
            evidence,
            run_root=run_root,
            result=result,
            plan=plan,
        )
        for evidence in contract.evidence
    )
    interface_bytes = _git_interface_bytes(contract)
    interface_digest = hashlib.sha256(interface_bytes).hexdigest()
    reference_rows = tuple(
        _promotion_reference_payload(reference) for reference in references
    )
    boundaries = tuple(
        {
            "kind": boundary.kind,
            "status": boundary.status,
            "summary": boundary.summary,
            "subjects": list(boundary.subjects),
        }
        for boundary in contract.boundaries
    )
    release_fingerprint = digest(
        {
            "schema": 1,
            "owner": contract.owner,
            "ip_name": contract.name,
            "export": contract.export,
            "maturity": {"level": contract.maturity},
            "source": source,
            "interface": {
                "logical": contract.logical_interface,
                "physical": contract.physical_interface,
                "digest": interface_digest,
            },
            "artifacts": reference_rows,
            "evidence": list(receipts),
            "conclusions": dict(contract.conclusions),
            "boundaries": boundaries,
        }
    )
    release_id = (
        f"{contract.maturity}-{release_fingerprint[:24]}-"
        f"{contract.source_commit[:12]}"
    )
    release_root = artifact_root.resolve() / "ip" / contract.name / release_id
    return _ResolvedPromotion(
        contract=contract,
        references=references,
        artifacts=resolved,
        source=source,
        receipts=receipts,
        interface_bytes=interface_bytes,
        interface_digest=interface_digest,
        boundaries=boundaries,
        release_fingerprint=release_fingerprint,
        release_id=release_id,
        release_root=release_root,
    )


def audit_promoted_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    maturity: str | None = None,
) -> dict[str, Any]:
    """Read-only audit of the promoted release selected by an owner contract."""

    promotion = _resolve_ip_promotion(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
    if not promotion.release_root.is_dir() or promotion.release_root.is_symlink():
        raise FileNotFoundError(
            f"promoted IP release has not been built: {promotion.release_root}"
        )
    manifest = _audit_promoted_ip_release_manifest(
        promotion.release_root / "manifest.json"
    )
    if manifest.get("release_fingerprint") != promotion.release_fingerprint:
        raise IpReleaseError("promoted IP release differs from its owner contract")
    return manifest


def promote_ip_release(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
) -> dict[str, Any]:
    """Publish one immutable release from exact accepted Flow Run evidence."""

    promotion = _resolve_ip_promotion(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = promotion.contract
    references = promotion.references
    resolved = promotion.artifacts
    source = promotion.source
    receipts = promotion.receipts
    interface_bytes = promotion.interface_bytes
    interface_digest = promotion.interface_digest
    boundaries = promotion.boundaries
    release_fingerprint = promotion.release_fingerprint
    release_id = promotion.release_id
    release_root = promotion.release_root
    if release_root.exists():
        manifest = _audit_promoted_ip_release_manifest(release_root / "manifest.json")
        if manifest.get("release_fingerprint") != release_fingerprint:
            raise IpReleaseError("immutable release identity collision")
        return manifest

    namespace = release_root.parent
    namespace.mkdir(parents=True, exist_ok=True)
    temporary = namespace / f".{release_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        interface_path = Path("exports") / contract.export / "interface.toml"
        destination_interface = temporary / interface_path
        destination_interface.parent.mkdir(parents=True)
        destination_interface.write_bytes(interface_bytes)

        artifact_rows: list[dict[str, Any]] = []
        for reference, artifact in zip(references, resolved, strict=True):
            relative = Path("artifacts") / reference.role / artifact.path.name
            _copy_promoted_artifact(artifact.path, temporary / relative)
            artifact_rows.append(
                {
                    "reference": _promotion_reference_payload(reference),
                    "path": relative.as_posix(),
                    "digest": reference.digest,
                }
            )

        evidence_rows: list[dict[str, Any]] = []
        for selected, receipt in zip(contract.evidence, receipts, strict=True):
            relative = Path("evidence") / f"{selected.role}.json"
            atomic_write_json(temporary / relative, receipt)
            evidence_rows.append(
                {
                    "node": selected.node_id,
                    "role": selected.role,
                    "policy": selected.policy,
                    "evidence_role": selected.evidence_role,
                    "status": "accepted",
                    "path": relative.as_posix(),
                    "digest": file_sha256(temporary / relative),
                }
            )

        manifest = {
            "schema": 1,
            "contract_kind": "ip-release-manifest",
            "release_kind": "flow-promotion",
            "owner": contract.owner,
            "ip_name": contract.name,
            "export": contract.export,
            "release_id": release_id,
            "release_fingerprint": release_fingerprint,
            "maturity": {"level": contract.maturity},
            "source": source,
            "interface": {
                "path": interface_path.as_posix(),
                "digest": interface_digest,
                "logical": contract.logical_interface,
                "physical": contract.physical_interface,
            },
            "artifacts": artifact_rows,
            "evidence": evidence_rows,
            "conclusions": dict(contract.conclusions),
            "boundaries": boundaries,
            "provenance": {
                "contract": contract.path.relative_to(
                    contract.project_root
                ).as_posix(),
                "generator": "sigilicon-flow-promotion",
            },
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        _readonly_tree(temporary)
        try:
            os.replace(temporary, release_root)
        except FileExistsError:
            pass
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return _audit_promoted_ip_release_manifest(release_root / "manifest.json")
