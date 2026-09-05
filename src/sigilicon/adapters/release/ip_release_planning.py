"""Plan immutable custom-IP releases from typed owner contracts."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.artifacts import _inspect_nofollow_file
from sigilicon.contracts import read_toml
from sigilicon.domain.ip_release import (
    IpContract,
    IpExport,
    OaMixedSignalIpInterface,
    OaNativeIpInterface,
    load_ip_contract,
    resolve_ip_contract,
)
from sigilicon.domain.netlist import (
    load_netlist_snapshot,
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
    render_canonical_spectre,
    resolve_netlist_hierarchy,
)
from sigilicon.project import Project
from sigilicon.domain.systemverilog import module_ports
from sigilicon.adapters.release.source_control import inspect_checkout
from sigilicon.adapters.release.release_contract_checks import (
    _development_interface_check,
    _development_interface_check_with_design_inventory,
    _missing_roles,
    _project_path,
    _release_semantics,
)
from sigilicon.adapters.release.release_plan_record import (
    IpReleasePlan,
    IpReleaseRecord,
    MixedSignalReleaseInterface,
    NativeBundleMetadata,
    NativeOaReleaseInterface,
    ReleaseAvailability,
    ReleaseCheck,
    ReleaseCollateralRecord,
    ReleaseComponentRecord,
    ReleaseExportRecord,
    ReleaseInterface,
    ReleaseOaIdentity,
    RequiredRolesCheck,
    QualifiedViewSemanticsCheck,
    RtlReleaseInterface,
)

if TYPE_CHECKING:
    from sigilicon.domain.design import DesignSpec
    from sigilicon.domain.oa_library import OALibrarySource
    from sigilicon.domain.platform import PlatformSet
    from sigilicon.adapters.cadence.oa_library import OALibraryRebuildPlan



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
        source_root = root / "src" if path.is_relative_to(root / "src") else root
        package = path.parent.relative_to(source_root).parts
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package):
                        raise ValueError(f"relative Python import escapes its package: {path}")
                    base = package[:len(package) - node.level + 1]
                    module = ".".join((*base, *((node.module or "").split(".") if node.module else ())))
                else:
                    module = node.module or ""
                if module:
                    imports.add(module)
                    imports.update(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")
        for module in imports:
            for imported in _python_module_paths(root, module):
                if imported not in paths:
                    paths.add(imported)
                    if imported.suffix == ".py":
                        pending.append(imported)



def _resolve_release_oa_source(
    contract: IpContract,
    *,
    project: Project,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None,
    resolved_oa_source: OALibrarySource | None = None,
) -> tuple[Path, OALibrarySource]:
    """Resolve one operation-owned OA assembly snapshot for release work."""

    if contract.oa_assembly is None:
        raise ValueError("OA release owner has no OA assembly")
    oa_manifest = _project_path(
        contract.project_root,
        Path(contract.oa_assembly),
        "OA assembly",
    )
    from sigilicon.domain.oa_library import load_oa_library_source

    if resolved_oa_source is not None:
        if (
            resolved_oa_source.repository != contract.repository
            or resolved_oa_source.manifest_path != oa_manifest
        ):
            raise ValueError("resolved OA release source identity drift")
        library = resolved_oa_source
    elif oa_source_inventory is None:
        library = load_oa_library_source(oa_manifest, project=project)
    else:
        try:
            source_snapshot = oa_source_inventory[oa_manifest]
        except KeyError as exc:
            raise ValueError(
                f"OA source inventory has no {oa_manifest} entry"
            ) from exc
        library = load_oa_library_source(
            oa_manifest,
            project=project,
            snapshot=source_snapshot,
        )
    return oa_manifest, library


def _source_inputs(
    contract: IpContract,
    *,
    project: Project,
    platform_inventory: PlatformSet | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    oa_plan_inventory: Mapping[Path, OALibraryRebuildPlan] | None = None,
    resolved_oa_source: OALibrarySource | None = None,
) -> tuple[str, ...]:
    root = contract.project_root
    graph = contract.component_graph
    paths: set[Path] = {contract.path, *contract.interface_documents}

    def add_source(source: Path) -> None:
        resolved = source.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError(f"release source is missing: {source}")
        paths.add(resolved)

    for component in sorted(graph.values(), key=lambda item: item.name):
        paths.add(component.path)
        if component.public_interface is not None:
            add_source(
                _project_path(root, Path(component.public_interface), "public interface"),
            )
        for relative in component.sources.values():
            add_source(
                _project_path(root, Path(relative), "component source input"),
            )
    oa_exports = [
        exported
        for exported in contract.exports
        if isinstance(
            exported.interface, (OaMixedSignalIpInterface, OaNativeIpInterface)
        )
    ]
    if not oa_exports:
        _python_import_closure(root, paths)
        return tuple(
            path.relative_to(root).as_posix() for path in sorted(paths)
        )

    oa_manifest, library = _resolve_release_oa_source(
        contract,
        project=project,
        oa_source_inventory=oa_source_inventory,
        resolved_oa_source=resolved_oa_source,
    )
    paths.update(library.source_documents)
    from sigilicon.domain.platform import load_platform, resolve_platform_snapshot

    if platform_inventory is None:
        release_platform = load_platform(
            project,
            library.pdk,
            resources=project.resources(),
        )
    else:
        try:
            platform_snapshot = platform_inventory[library.pdk]
        except KeyError as exc:
            raise ValueError(
                f"platform inventory has no {library.pdk!r} entry"
            ) from exc
        release_platform = resolve_platform_snapshot(
            project,
            library.pdk,
            snapshot=platform_snapshot,
        )
    paths.update(release_platform.source_paths)
    oa_plan = None
    if oa_plan_inventory is not None:
        try:
            oa_plan = oa_plan_inventory[oa_manifest]
        except KeyError as exc:
            raise ValueError(
                f"OA plan inventory has no {oa_manifest} entry"
            ) from exc
        if oa_plan.source is not library or oa_plan.library != library.name:
            raise ValueError("OA rebuild plan source identity drift")
    planned_testbenches = (
        {} if oa_plan is None else {step.cell: step for step in oa_plan.testbenches}
    )
    planned_layouts = (
        {} if oa_plan is None else {step.spec.path: step for step in oa_plan.layouts}
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
    top_cells = {exported.interface.cell for exported in oa_exports}
    for exported in oa_exports:
        interface = exported.interface
        if interface.library != library.name:
            raise ValueError(
                f"release export {exported.name} names OA library "
                f"{interface.library}, expected {library.name}"
            )
        if interface.cell not in cell_by_name or interface.cell not in definitions:
            raise ValueError(
                "release OA top is absent from the canonical library: "
                f"{exported.name}/{interface.cell}"
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
            setup_sources = {
                view.source for view in cell.views if view.kind in {"config", "maestro"}
            }
            if len(setup_sources) != 1:
                raise ValueError(
                    f"release testbench config and Maestro sources disagree: {cell.cell}"
                )
            if oa_plan is None:
                from sigilicon.domain.oa_simulation import load_oa_simulation_spec
                simulation = load_oa_simulation_spec(
                    next(iter(setup_sources)),
                    project=project,
                    platform=release_platform,
                )
            else:
                try:
                    testbench_step = planned_testbenches[cell.cell]
                except KeyError as exc:
                    raise ValueError(
                        f"OA rebuild plan has no testbench {cell.cell}"
                    ) from exc
                simulation = testbench_step.simulation
                if (
                    testbench_step.canonical_source != cell.canonical_source
                    or simulation.path != next(iter(setup_sources))
                    or simulation.repository != library.repository
                    or simulation.library != library.name
                    or simulation.cell != cell.cell
                    or simulation.native_setup.pdk.source_paths
                    != release_platform.source_paths
                ):
                    raise ValueError("OA rebuild testbench plan identity drift")
            rdb_contract = simulation.native_setup.rdb_contract
            if rdb_contract is not None:
                paths.add(rdb_contract.path)
                add_source(rdb_contract.path)
        for layout_spec in cell.layout_specs:
            paths.add(layout_spec)
            if oa_plan is None:
                layout_raw = read_toml(layout_spec).get("layout", {})
                if not isinstance(layout_raw, Mapping):
                    raise ValueError(f"layout must be a TOML table: {layout_spec}")
                for field in ("generator_source", "source_netlist"):
                    value = layout_raw.get(field)
                    if value is None:
                        if field == "generator_source":
                            inferred = next(
                                (
                                    parent / "layout_generator.py"
                                    for parent in (
                                        layout_spec.parent,
                                        *layout_spec.parents,
                                    )
                                    if parent.is_relative_to(root)
                                    and (parent / "layout_generator.py").is_file()
                                ),
                                None,
                            )
                            if inferred is not None:
                                add_source(inferred)
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
                    raise ValueError(
                        "layout.generator_modules must be module names: "
                        f"{layout_spec}"
                    )
                for module in modules:
                    paths.update(_python_module_paths(root, module))
            else:
                try:
                    layout_step = planned_layouts[layout_spec]
                except KeyError as exc:
                    raise ValueError(
                        f"OA rebuild plan has no layout {layout_spec}"
                    ) from exc
                if (
                    layout_step.spec.path != layout_spec
                    or layout_step.spec.repository != library.repository
                    or layout_step.spec.library != library.name
                    or layout_step.spec.cell != cell.cell
                ):
                    raise ValueError("OA rebuild layout plan identity drift")
                for declared in (
                    layout_step.spec.generator_source,
                    *layout_step.spec.generator_dependencies,
                    layout_step.spec.source_netlist,
                    *layout_step.spec.dependency_netlists,
                ):
                    add_source(declared)
                for module, module_source in zip(
                    layout_step.spec.generator_modules,
                    layout_step.spec.generator_module_sources,
                    strict=True,
                ):
                    module_paths = _python_module_paths(root, module)
                    if module_source.is_relative_to(root):
                        add_source(module_source)
                        if module_source not in module_paths:
                            raise ValueError(
                                "OA rebuild layout module identity drift"
                            )
                    elif module_paths:
                        raise ValueError("OA rebuild layout module identity drift")
                    paths.update(module_paths)
    _python_import_closure(root, paths)
    return tuple(
        path.relative_to(root).as_posix() for path in sorted(paths)
    )



def _release_design_inventory(
    contract: IpContract,
    oa_plan_inventory: Mapping[Path, OALibraryRebuildPlan] | None,
) -> Mapping[Path, DesignSpec] | None:
    if oa_plan_inventory is None:
        return None
    if contract.oa_assembly is None:
        return None
    root = contract.project_root
    oa_manifest = _project_path(root, Path(contract.oa_assembly), "OA assembly")
    try:
        oa_plan = oa_plan_inventory[oa_manifest]
    except KeyError as exc:
        raise ValueError(f"OA plan inventory has no {oa_manifest} entry") from exc
    producer_root = _project_path(root, Path(contract.producer), "IP producer")
    declared: dict[Path, Any] = {}
    for cell in oa_plan.source.cells:
        if cell.design_spec is None:
            continue
        path = cell.design_spec.resolve()
        if path in declared:
            raise ValueError(f"OA source declares duplicate design spec: {path}")
        declared[path] = cell
    result: dict[Path, DesignSpec] = {}
    for step in oa_plan.designs:
        spec = step.inspection.spec
        path = spec.path.resolve()
        try:
            cell = declared[path]
        except KeyError as exc:
            raise ValueError(
                f"OA rebuild plan contains undeclared design spec: {path}"
            ) from exc
        if (
            path in result
            or spec.path != path
            or spec.repository != oa_plan.source.repository
            or spec.cell != cell.cell
            or spec.library != oa_plan.source.name
            or spec.pdk.key != oa_plan.source.pdk
        ):
            raise ValueError(f"OA rebuild design plan identity drift: {path}")
        result[path] = spec
    if set(result) != set(declared):
        missing = sorted(str(path) for path in set(declared) - set(result))
        raise ValueError(f"OA rebuild plan omits design specs: {missing}")
    return {
        path: spec
        for path, spec in result.items()
        if path.is_relative_to(producer_root)
    }


def _export_interface_manifest(
    contract: IpContract, exported: IpExport
) -> tuple[ReleaseOaIdentity | None, ReleaseInterface]:
    interface = exported.interface
    if isinstance(interface, OaMixedSignalIpInterface):
        return (
            ReleaseOaIdentity(
                interface.library,
                interface.cell,
                interface.schematic_view,
                interface.layout_view,
            ),
            MixedSignalReleaseInterface(
                contract=interface.contract.as_posix(),
                physical=interface.physical,
                logical=interface.logical,
                interfaces_are_distinct=interface.physical != interface.logical,
            ),
        )
    if isinstance(interface, OaNativeIpInterface):
        return (
            ReleaseOaIdentity(
                interface.library,
                interface.cell,
                interface.schematic_view,
                interface.layout_view,
            ),
            NativeOaReleaseInterface(
                contract=(contract.producer / interface.contract).as_posix(),
            ),
        )
    return (
        None,
        RtlReleaseInterface(
            contract=(contract.producer / interface.contract).as_posix(),
            module=interface.module,
            source_role=interface.source_role,
            variant=interface.variant,
        ),
    )


def _native_oa_spectre_bundle(
    contract: IpContract,
    exported: IpExport,
    *,
    library: OALibrarySource,
) -> tuple[str, NativeBundleMetadata]:
    """Close one native OA circuit role over its reachable Spectre hierarchy."""

    interface = exported.interface
    if not isinstance(interface, OaNativeIpInterface):
        raise TypeError("native OA Spectre bundling requires a native OA export")
    circuit = [
        item for item in exported.collateral if item.role == "circuit_netlist"
    ]
    if len(circuit) != 1:
        raise ValueError(
            f"native OA export {exported.name} must have one circuit_netlist role"
        )
    if circuit[0].format != "spectre-source":
        raise ValueError(
            f"native OA export {exported.name} circuit_netlist must use "
            "spectre-source format"
        )
    if library.name != interface.library:
        raise ValueError(
            f"native OA export {exported.name} library identity drifted"
        )
    snapshots = tuple(
        load_netlist_snapshot(cell.canonical_source)
        for cell in library.cells
        if any(view.kind == "spectre_netlist" for view in cell.views)
    )
    hierarchy = resolve_netlist_hierarchy(
        snapshots,
        top=interface.cell,
        primitive_masters=library.primitive_masters,
    )
    expected_source = _project_path(
        contract.project_root,
        Path(circuit[0].source),
        "native OA circuit source",
    )
    if hierarchy.definitions[interface.cell].source_path != expected_source:
        raise ValueError(
            f"native OA export {exported.name} circuit source disagrees with its "
            "OA assembly"
        )
    text = render_canonical_spectre(hierarchy)
    return text, NativeBundleMetadata(
        subcircuits=hierarchy.dependency_order,
        primitive_masters=tuple(sorted(hierarchy.primitive_counts)),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _plan_loaded_ip_release(
    contract: IpContract,
    *,
    project: Project,
    maturity: str | None = None,
    platform_inventory: PlatformSet | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    oa_plan_inventory: Mapping[Path, OALibraryRebuildPlan] | None = None,
) -> IpReleasePlan:
    contract = resolve_ip_contract(
        contract.path,
        project=project,
        snapshot=contract,
    )
    level = contract.require_level(maturity or contract.default_maturity)
    oa_exports = [
        exported
        for exported in contract.exports
        if isinstance(
            exported.interface, (OaMixedSignalIpInterface, OaNativeIpInterface)
        )
    ]
    oa_library = None
    if oa_exports:
        _, oa_library = _resolve_release_oa_source(
            contract,
            project=project,
            oa_source_inventory=oa_source_inventory,
        )
    source_paths = _source_inputs(
        contract,
        project=project,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
        oa_plan_inventory=oa_plan_inventory,
        resolved_oa_source=oa_library,
    )
    source_state = inspect_checkout(
        contract.project_root,
        project.resources(),
    )
    commit = source_state.commit
    dirty = source_state.working_tree_dirty
    release_id = f"{level}-{commit}"
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
    design_inventory = _release_design_inventory(contract, oa_plan_inventory)
    interface_checks = (
        [
            _development_interface_check(contract, exported, project=project)
            for exported in contract.exports
        ]
        if design_inventory is None
        else [
            _development_interface_check_with_design_inventory(
                contract,
                exported,
                project=project,
                design_inventory=design_inventory,
            )
            for exported in contract.exports
        ]
    )
    semantics = _release_semantics(
        contract,
        level,
        source_commit=commit,
    )
    semantic_missing = sorted({problem for _, problems in semantics.values() for problem in problems})
    semantic_check = QualifiedViewSemanticsCheck(passed=not semantic_missing, problems=tuple(semantic_missing))
    missing = sorted(set(role_missing) | set(semantic_missing))
    export_rows: list[ReleaseExportRecord] = []
    role_checks: list[ReleaseCheck] = []
    for exported in contract.exports:
        roles = {item.role for item in exported.collateral}
        export_missing = [
            item for item in missing if item.startswith(f"{exported.name}:")
        ]
        availability = semantics[exported.name][0]
        role_checks.append(
            RequiredRolesCheck(
                export=exported.name,
                passed=not any(
                    item.startswith(f"{exported.name}:") for item in role_missing
                ),
                required=exported.required_roles[level],
                present=tuple(sorted(roles)),
            )
        )
        oa_identity, interface = _export_interface_manifest(contract, exported)
        export_rows.append(
            ReleaseExportRecord(
                name=exported.name,
                interface=interface,
                maturity_required_roles=exported.required_roles[level],
                maturity_missing_items=tuple(export_missing),
                availability=availability,
                oa=oa_identity,
            )
        )
    availability = ReleaseAvailability(
        simulation=all(
            exported.availability.simulation for exported in export_rows
        ),
        synthesis=all(
            exported.availability.synthesis for exported in export_rows
        ),
        physical_implementation=all(
            exported.availability.physical_implementation
            for exported in export_rows
        ),
    )
    native_bundle_metadata: dict[tuple[str, str], NativeBundleMetadata] = {}
    native_bundles: dict[tuple[str, str], str] = {}
    if oa_library is not None:
        for exported in contract.exports:
            if not isinstance(exported.interface, OaNativeIpInterface):
                continue
            text, metadata = _native_oa_spectre_bundle(
                contract,
                exported,
                library=oa_library,
            )
            key = (exported.name, "circuit_netlist")
            native_bundle_metadata[key] = metadata
            native_bundles[key] = text
    collateral_source_identity: dict[tuple[str, str], tuple[int, str]] = {}
    for item in contract.collateral:
        source = _project_path(
            contract.project_root,
            Path(item.source),
            f"{item.export}/{item.role} source",
        )
        source_metadata, source_digest = _inspect_nofollow_file(source)
        collateral_source_identity[(item.export, item.role)] = (
            source_metadata.st_size,
            source_digest,
        )
    collateral = tuple(
        ReleaseCollateralRecord(
            export=item.export,
            role=item.role,
            component=item.component,
            source_id=item.source_id,
            source=item.source.as_posix(),
            package_path=item.package_path.as_posix(),
            format=item.format,
            module=item.module,
            library=item.library,
            cell=item.cell,
            view=item.view,
            corner=item.corner,
            capabilities=item.capabilities,
            source_size=collateral_source_identity[(item.export, item.role)][0],
            source_sha256=collateral_source_identity[(item.export, item.role)][1],
            native_bundle=native_bundle_metadata.get((item.export, item.role)),
        )
        for item in contract.collateral
    )
    payload = IpReleaseRecord(
        ip_name=contract.name,
        owner=contract.owner,
        contract=contract.path.relative_to(contract.project_root).as_posix(),
        producer=contract.producer.as_posix(),
        component=ReleaseComponentRecord(
            name=component.name,
            kind=component.kind,
            lifecycle=component.lifecycle,
            contract=component.path.relative_to(contract.project_root).as_posix(),
        ),
        release_id=release_id,
        release_store=contract.owner,
        source_commit=commit,
        working_tree_dirty=dirty,
        source_files=source_paths,
        maturity_level=level,
        maturity_checks=tuple((*role_checks, *interface_checks, semantic_check)),
        missing_items=tuple(missing),
        availability=availability,
        exports=tuple(export_rows),
        collateral=collateral,
    )
    return IpReleasePlan(contract, payload, native_bundles)


def plan_ip_release(
    contract_path: Path,
    *,
    project: Project,
    maturity: str | None = None,
    platform_inventory: PlatformSet | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    oa_plan_inventory: Mapping[Path, OALibraryRebuildPlan] | None = None,
) -> IpReleasePlan:
    contract = load_ip_contract(contract_path, project=project)
    return plan_ip_release_contract(
        contract,
        project=project,
        maturity=maturity,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
        oa_plan_inventory=oa_plan_inventory,
    )


def plan_ip_release_contract(
    contract: IpContract,
    *,
    project: Project,
    maturity: str | None = None,
    platform_inventory: PlatformSet | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    oa_plan_inventory: Mapping[Path, OALibraryRebuildPlan] | None = None,
) -> IpReleasePlan:
    """Plan one already validated IP release contract."""

    return _plan_loaded_ip_release(
        contract,
        project=project,
        maturity=maturity,
        platform_inventory=platform_inventory,
        oa_source_inventory=oa_source_inventory,
        oa_plan_inventory=oa_plan_inventory,
    )
