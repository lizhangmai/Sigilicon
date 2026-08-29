"""Generic immutable custom-IP packaging and publication workflow."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil
import stat
import tomllib
import uuid
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json, read_json_object, utc_now
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
    IpContract,
    IpExport,
    load_ip_contract,
)
from sigilicon.domain.netlist import (
    load_netlist_snapshot,
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
    subckt_ports,
)
from sigilicon.domain.oa_library import OALibrarySource
from sigilicon.domain.platform import PdkConfig
from sigilicon.domain.repository import Project
from sigilicon.domain.systemverilog import (
    ModulePort,
    module_ports,
    module_port_signatures,
    named_port_connections,
)
from sigilicon.external_tools import owned_directory, run_process_group
from sigilicon.flow.model import (
    PolicyCheck,
    PolicySpec,
)
from sigilicon.paths import ArtifactLayout


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


def _source_inputs(
    contract: IpContract,
    *,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
) -> tuple[str, ...]:
    root = contract.project_root
    graph = contract.component_graph
    paths: set[Path] = {contract.path}

    def add_source(source: Path) -> None:
        resolved = source.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(f"release source is missing: {source}")
        paths.add(resolved)

    for component in sorted(graph.values(), key=lambda item: item.name):
        paths.add(component.path)
        referenced = [path for values in component.filesets.values() for path in values]
        if component.public_interface is not None:
            referenced.append(component.public_interface)
        for relative in referenced:
            paths.add(_project_path(root, Path(relative), "component input"))
        if component.public_interface is not None:
            add_source(
                _project_path(root, Path(component.public_interface), "public interface"),
            )
        for _, values in sorted(component.filesets.items()):
            for relative in values:
                add_source(
                    _project_path(root, Path(relative), "component fileset input"),
                )
    for relative in contract.source_files:
        path = _project_path(root, Path(relative), "source file")
        if not path.is_file():
            raise FileNotFoundError(f"IP source file is missing: {relative}")
        add_source(path)

    oa_manifest = _project_path(root, Path(contract.oa_assembly), "OA assembly")
    from sigilicon.domain.oa_library import (
        load_oa_library_source,
        resolve_oa_library_source,
    )

    if oa_source_inventory is None:
        library = load_oa_library_source(oa_manifest, project=contract.project)
    else:
        try:
            source_snapshot = oa_source_inventory[oa_manifest]
        except KeyError as exc:
            raise ValueError(
                f"OA source inventory has no {oa_manifest} entry"
            ) from exc
        library = resolve_oa_library_source(
            oa_manifest,
            project=contract.project,
            snapshot=source_snapshot,
        )
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
    release_platform = None
    paths.add(library.manifest_path)
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
        add_source(cell.canonical_source)
        for view in cell.views:
            add_source(view.source)
        if cell.design_spec is not None:
            paths.add(cell.design_spec)
        if cell.role == "testbench":
            from sigilicon.domain.oa_simulation import load_oa_simulation_spec
            from sigilicon.domain.platform import load_platform, resolve_platform

            setup_sources = {
                view.source for view in cell.views if view.kind in {"config", "maestro"}
            }
            if len(setup_sources) != 1:
                raise ValueError(
                    f"release testbench config and Maestro sources disagree: {cell.cell}"
                )
            if release_platform is None:
                if platform_inventory is None:
                    release_platform = load_platform(library.project, library.pdk)
                else:
                    try:
                        platform_snapshot = platform_inventory[library.pdk]
                    except KeyError as exc:
                        raise ValueError(
                            f"platform inventory has no {library.pdk!r} entry"
                        ) from exc
                    release_platform = resolve_platform(
                        library.project,
                        library.pdk,
                        snapshot=platform_snapshot,
                    )
            simulation = load_oa_simulation_spec(
                next(iter(setup_sources)),
                project=library.project,
                platform=release_platform,
            )
            rdb_contract = simulation.native_setup.rdb_contract
            if rdb_contract is not None:
                paths.add(rdb_contract.path)
                add_source(rdb_contract.path)
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
    return tuple(
        path.relative_to(root).as_posix() for path in sorted(paths)
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


def _receipt_problems(
    *,
    contract: IpContract,
    exported: IpExport,
    role: str,
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
    receipt_roles: set[str] = set()
    for field in ("inputs", "outputs"):
        values = receipt.get(field)
        if not isinstance(values, list):
            problems.append(f"{prefix}:{field}")
            continue
        for row in values:
            if (
                not isinstance(row, Mapping)
                or not isinstance(row.get("role"), str)
                or not row.get("role")
            ):
                problems.append(f"{prefix}:{field}-entry")
                continue
            receipt_roles.add(str(row["role"]))
    for bound_role in _SIGNOFF_RECEIPT_BINDINGS[role]:
        if by_role.get(bound_role) is None:
            problems.append(f"{prefix}:missing-bound-role:{bound_role}")
            continue
        if bound_role not in receipt_roles:
            problems.append(f"{prefix}:missing-receipt-role:{bound_role}")
    return problems


def _qualification_semantics(
    contract: IpContract,
    level: str,
    *,
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
                            source_commit=source_commit,
                            by_role=by_role,
                        )
                    )
    return (
        {
            "name": "qualified_view_semantics",
            "passed": not problems,
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


def _release_project(
    *,
    project: Project | None,
    project_root: Path | None,
    artifact_root: Path | None,
) -> Project:
    repository = Project.bind(project=project, project_root=project_root)
    return (
        repository
        if artifact_root is None
        else repository.with_artifact_root(artifact_root)
    )


def _plan_loaded_ip_release(
    contract: IpContract,
    *,
    maturity: str | None = None,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
) -> dict[str, Any]:
    repository = contract.project
    level = contract.require_level(maturity or contract.default_maturity)
    source_paths = _source_inputs(
        contract,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
    )
    commit, dirty = _source_control(contract.project_root)
    release_id = f"{level}-{commit[:12]}"
    if dirty:
        release_id += "-dirty"
    role_missing = _missing_roles(contract, level)
    component_path = _project_path(
        contract.project_root,
        Path(contract.producer) / contract.component_contract,
        "component contract",
    )
    component_graph = contract.component_graph
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
        "owner": contract.owner,
        "contract": contract.path.relative_to(contract.project_root).as_posix(),
        "producer": contract.producer.as_posix(),
        "component": {
            "name": component.name,
            "kind": component.kind,
            "contract": component.path.relative_to(contract.project_root).as_posix(),
        },
        "release_id": release_id,
        "release_root": ArtifactLayout(repository.artifact_root)
        .export(contract.name, "package", release_id)
        .relative_to(repository.artifact_root)
        .as_posix(),
        "source_commit": commit,
        "working_tree_dirty": dirty,
        "source_files": list(source_paths),
        "maturity_level": level,
        "maturity_checks": [
            *role_checks,
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


def plan_ip_release(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    maturity: str | None = None,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
) -> dict[str, Any]:
    repository = _release_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_contract(contract_path, project=repository)
    return plan_ip_release_contract(
        contract,
        maturity=maturity,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
    )


def plan_ip_release_contract(
    contract: IpContract,
    *,
    maturity: str | None = None,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
) -> dict[str, Any]:
    """Plan one already validated IP release contract."""

    return _plan_loaded_ip_release(
        contract,
        maturity=maturity,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
    )


def _readonly_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one exact staging tree without following filesystem links."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        os.fchmod(descriptor, stat.S_IRWXU)
        for child in os.listdir(descriptor):
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                _remove_tree_at(descriptor, child)
            else:
                os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def build_ip_release(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    maturity: str | None = None,
) -> dict[str, Any]:
    repository = _release_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_contract(contract_path, project=repository)
    plan = _plan_loaded_ip_release(contract, maturity=maturity)
    if plan["missing_items"]:
        missing = ", ".join(plan["missing_items"])
        raise IpReleaseError(
            f"cannot build {plan['maturity_level']} IP release; missing: {missing}"
        )
    if plan["working_tree_dirty"]:
        raise IpReleaseError(
            "IP releases require a clean source checkout"
        )
    release_root = repository.artifact_root / Path(plan["release_root"])
    namespace = release_root.parent
    with owned_directory(namespace, create_missing=True) as release_namespace:
        try:
            existing = os.stat(
                release_root.name,
                dir_fd=release_namespace.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(existing.st_mode):
                raise RuntimeError(f"IP release path is unsafe: {release_root}")
            return _audit_loaded_ip_release(contract, plan)

        temporary_name = f".{plan['release_id']}.{uuid.uuid4().hex}.tmp"
        os.mkdir(temporary_name, dir_fd=release_namespace.fd)
        temporary = namespace / temporary_name
        installed = False
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
            os.rename(
                temporary_name,
                release_root.name,
                src_dir_fd=release_namespace.fd,
                dst_dir_fd=release_namespace.fd,
            )
            installed = True
        finally:
            if not installed:
                try:
                    _remove_tree_at(release_namespace.fd, temporary_name)
                except FileNotFoundError:
                    pass
    return _audit_loaded_ip_release(contract, plan)


def _load_release(release_root: Path) -> dict[str, Any]:
    return read_json_object(release_root / "manifest.json", "IP release manifest")


def load_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and audit one immutable release without consulting a channel pointer."""

    if manifest_path.name != "manifest.json" or not manifest_path.is_file():
        raise FileNotFoundError(f"IP release manifest is missing: {manifest_path}")
    return audit_ip_release_manifest(manifest_path)


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
            receipt_roles: set[str] = set()
            for field in ("inputs", "outputs"):
                values = receipt.get(field)
                if not isinstance(values, list):
                    problems.append(f"{export_name}:{role}:{field}")
                    continue
                for row in values:
                    if (
                        isinstance(row, Mapping)
                        and isinstance(row.get("role"), str)
                        and row.get("role")
                    ):
                        receipt_roles.add(str(row["role"]))
                    else:
                        problems.append(f"{export_name}:{role}:{field}-entry")
            for bound_role in bindings:
                bound = by_role.get(bound_role)
                if bound is None:
                    problems.append(
                        f"{export_name}:{role}:missing-bound-role:{bound_role}"
                    )
                elif bound_role not in receipt_roles:
                    problems.append(
                        f"{export_name}:{role}:missing-receipt-role:{bound_role}"
                    )
    if problems:
        raise RuntimeError(
            "IP release qualified view semantics are invalid: "
            + ", ".join(sorted(problems))
        )


def audit_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    """Audit an exact immutable package without consulting producer source."""

    release_root = manifest_path.parent
    if manifest_path.is_symlink() or release_root.is_symlink():
        raise RuntimeError("IP release root and manifest cannot be symlinks")
    with owned_directory(release_root, create_missing=False):
        return _audit_ip_release_manifest(manifest_path)


def _audit_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    release_root = manifest_path.parent
    for path in release_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"IP release cannot contain symlinks: {path}")
    manifest = _load_release(manifest_path.parent)
    if (
        manifest.get("schema") != 1
        or manifest.get("contract_kind") != "ip-release-manifest"
    ):
        raise RuntimeError("unsupported IP release manifest schema")
    if manifest.get("release_kind") != "source-package":
        raise RuntimeError("unsupported IP release kind")
    release_root = release_root.resolve()
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
        if path.stat().st_size != view.get("size"):
            raise RuntimeError(f"IP release view size drifted: {relative}")
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


def _audit_loaded_ip_release(
    contract: IpContract,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    repository = contract.project
    release_root = repository.artifact_root / Path(plan["release_root"])
    if not release_root.is_dir() or release_root.is_symlink():
        raise FileNotFoundError(f"IP release has not been built: {release_root}")
    manifest = audit_ip_release_manifest(release_root / "manifest.json")
    expected = {
        "ip_name": plan["ip_name"],
        "release_id": plan["release_id"],
        "source_commit": plan["source_commit"],
        "source_files": plan["source_files"],
        "component": plan["component"],
        "exports": plan["exports"],
        "availability": plan["availability"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"IP release manifest {key} does not match its source plan")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping) or any(
        provenance.get(key) != plan[key] for key in ("contract", "producer")
    ):
        raise RuntimeError("IP release provenance does not match its source plan")
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
        .relative_to(repository.artifact_root)
        .as_posix(),
        "audit": {"passed": True, "audited_at": utc_now()},
    }


def _audit_source_ip_release(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    maturity: str | None = None,
) -> dict[str, Any]:
    repository = _release_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_contract(contract_path, project=repository)
    plan = _plan_loaded_ip_release(contract, maturity=maturity)
    return _audit_loaded_ip_release(contract, plan)


def publish_ip_release(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    maturity: str | None = None,
) -> dict[str, Any]:
    repository = _release_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_contract(contract_path, project=repository)
    plan = _plan_loaded_ip_release(contract, maturity=maturity)
    if plan["working_tree_dirty"]:
        raise IpReleaseError(
            "cannot publish an immutable IP release from a dirty source checkout"
        )
    audited = _audit_loaded_ip_release(contract, plan)
    provenance = audited.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("working_tree_dirty"):
        raise IpReleaseError(
            "cannot publish a release candidate that was built from dirty source"
        )
    manifest = repository.artifact_root / Path(str(audited["manifest"]))
    namespace = ArtifactLayout(repository.artifact_root).export(
        str(audited["ip_name"]), "package"
    )
    pointer = {
        "ip_name": audited["ip_name"],
        "release_id": audited["release_id"],
        "maturity": audited["maturity"]["level"],
        "source_commit": audited["source_commit"],
        "manifest": manifest.relative_to(repository.artifact_root).as_posix(),
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
    artifact_base = artifact_root.resolve()
    manifest_path = artifact_base / relative
    if not manifest_path.resolve().is_relative_to(artifact_base):
        raise RuntimeError("IP current pointer escapes the artifact root")
    manifest = load_ip_release_manifest(manifest_path)
    for key in ("ip_name", "release_id", "source_commit"):
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
    return path


def audit_ip_release(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    maturity: str | None = None,
) -> dict[str, Any]:
    """Audit the exact release selected by the configured IP contract."""
    return _audit_source_ip_release(
        contract_path,
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
        maturity=maturity,
    )
