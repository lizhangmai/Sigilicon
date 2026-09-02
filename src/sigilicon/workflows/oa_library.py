"""Plan, rebuild, and attest a complete source-owned OA library."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re
import tomllib
from types import MappingProxyType
from typing import Any, TypeVar

from sigilicon.domain.netlist import (
    NetlistSnapshot,
    NetlistSubcircuit,
    load_netlist_snapshot,
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
)
from sigilicon.domain.oa_simulation import OASimulationSpec, load_oa_simulation_spec
from sigilicon.domain.oa_library import (
    OACellViewSource,
    OALibrarySource,
    OAViewReference,
    load_oa_library_source,
    resolve_oa_library_source,
)
from sigilicon.domain.platform import (
    PdkConfig,
    PlatformInventory,
    PlatformSnapshot,
    load_platform,
    resolve_platform_snapshot,
)
from sigilicon.project import Project
from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot
from sigilicon.execution.model import Source, json_value
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec, load_layout_spec
from sigilicon.virtuoso.attestation import attest_native_setup
from sigilicon.virtuoso.discovery import list_cells
from sigilicon.virtuoso.layout_generation import validate_layout_plan
from sigilicon.virtuoso.oa import cell_view_exists, delete_cell, delete_cell_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.design_lifecycle import (
    DesignInspection,
    attest_oa_design,
    inspect_design,
    synchronize_design,
)
from sigilicon.workflows.layout_generation import (
    LayoutPlanningResult,
    build_managed_layout_ir,
    generate_layout,
    plan_layout_snapshot,
)
from sigilicon.workflows.oa_testbench import (
    sync_oa_testbench,
)
from sigilicon.workflows.oa_text_view import (
    sync_oa_text_view,
)


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_T = TypeVar("_T", bound=Hashable)


@dataclass(frozen=True)
class DesignRebuildStep:
    inspection: DesignInspection
    imported_cells: tuple[str, ...]
    dependencies: tuple[str, ...]
    instance_parameters: tuple["InstanceParameterExpectation", ...] = ()


@dataclass(frozen=True)
class InstanceParameterExpectation:
    """Source value expected on one generated project-subcircuit instance."""

    instance: str
    master: str
    parameters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LayoutRebuildStep:
    planning: LayoutPlanningResult
    dependencies: tuple[tuple[str, str], ...]

    @property
    def spec(self) -> LayoutSpec:
        return self.planning.spec

    @property
    def plan(self) -> LayoutPlan:
        if self.planning.plan is None:
            raise ValueError("OA layout execution requires managed LayoutIR")
        return self.planning.plan


@dataclass(frozen=True)
class ViewRebuildStep:
    cell: str
    owner: str
    view: OACellViewSource
    source_snapshot: TextSourceSnapshot | None = None


@dataclass(frozen=True)
class TestbenchRebuildStep:
    cell: str
    source_snapshot: NetlistSnapshot
    dependencies: tuple[str, ...]
    simulation: OASimulationSpec

    @property
    def canonical_source(self) -> Path:
        return self.source_snapshot.source_path


@dataclass(frozen=True)
class OALibraryRebuildPlan:
    source: OALibrarySource
    library: str
    cells: tuple[str, ...]
    designs: tuple[DesignRebuildStep, ...]
    layouts: tuple[LayoutRebuildStep, ...]
    testbenches: tuple[TestbenchRebuildStep, ...]
    views: tuple[ViewRebuildStep, ...]
    expected_views: Mapping[str, tuple[str, ...]]
    netlist_snapshots: Mapping[Path, NetlistSnapshot] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )

    def require_layout_ir(self, operation: str) -> None:
        missing = tuple(
            f"{step.spec.cell}/{step.spec.view}"
            for step in self.layouts
            if step.planning.plan is None
        )
        if missing:
            raise ValueError(
                f"{operation} requires managed LayoutIR for: " + ", ".join(missing)
            )

    def as_dict(self) -> dict[str, object]:
        root = self.source.project_root

        def project_path(path: Path) -> str:
            return (
                path.relative_to(root).as_posix()
                if path.is_relative_to(root)
                else str(path)
            )

        return {
            "passed": True,
            "manifest": self.source.manifest_path.relative_to(root).as_posix(),
            "source_library": self.source.name,
            "target_library": self.library,
            "pdk": self.source.pdk,
            "workspace_template": project_path(self.source.workspace_template),
            "oa_library": project_path(self.source.oa_library),
            "primitive_masters": list(self.source.primitive_masters),
            "physical_verification": (
                None
                if self.source.physical_verification is None
                else self.source.physical_verification.path.relative_to(root).as_posix()
            ),
            "source_roots": [
                {
                    "owner": source.owner,
                    "manifest": source.manifest_path.relative_to(root).as_posix(),
                    "cell_roots": [
                        path.relative_to(root).as_posix()
                        for path in source.cell_roots
                    ],
                    "cells": [cell.cell for cell in source.cells],
                }
                for source in self.source.source_roots
            ],
            "cells": list(self.cells),
            "designs": [
                {
                    "cell": step.inspection.spec.cell,
                    "spec": step.inspection.spec.path.relative_to(root).as_posix(),
                    "mode": step.inspection.spec.sync_mode,
                    "imports": list(step.imported_cells),
                    "dependencies": list(step.dependencies),
                    "instance_parameter_count": sum(
                        len(item.parameters) for item in step.instance_parameters
                    ),
                }
                for step in self.designs
            ],
            "layouts": [
                {
                    "cell": step.spec.cell,
                    "view": step.spec.view,
                    "spec": step.spec.path.relative_to(root).as_posix(),
                    "dependencies": [
                        {"cell": cell, "view": view}
                        for cell, view in step.dependencies
                    ],
                }
                for step in self.layouts
            ],
            "testbenches": [
                {
                    "cell": step.cell,
                    "source": step.canonical_source.relative_to(root).as_posix(),
                    "setup": step.simulation.path.relative_to(root).as_posix(),
                    "simulator": step.simulation.simulator,
                    "dependencies": list(step.dependencies),
                    "native_rdb": (
                        None
                        if step.simulation.native_setup.rdb_contract is None
                        else {
                            "path": step.simulation.native_setup.rdb_contract.path.relative_to(
                                root
                            ).as_posix(),
                            "point_count": step.simulation.native_setup.rdb_contract.point_count,
                            "corners": list(
                                step.simulation.native_setup.rdb_contract.corners
                            ),
                            "tests": list(step.simulation.native_setup.rdb_contract.tests),
                            "waveforms": [
                                name
                                for name, _signal in step.simulation.native_setup.rdb_contract.waveform_outputs
                            ],
                            "scalars": list(
                                step.simulation.native_setup.rdb_contract.scalar_names
                            ),
                        }
                    ),
                }
                for step in self.testbenches
            ],
            "views": [
                {
                    "cell": step.cell,
                    "owner": step.owner,
                    "view": step.view.name,
                    "kind": step.view.kind,
                    "source": step.view.source.relative_to(root).as_posix(),
                    "dependencies": [
                        f"{dependency.cell}/{dependency.view}"
                        for dependency in step.view.dependencies
                    ],
                }
                for step in self.views
            ],
            "expected_views": {
                cell: list(views) for cell, views in self.expected_views.items()
            },
        }


def oa_plan_source_paths(plan: OALibraryRebuildPlan) -> frozenset[Path]:
    """Return the complete source closure consumed by one resolved OA plan."""

    paths = set(plan.source.source_documents)
    paths.update(plan.netlist_snapshots)
    for cell in plan.source.cells:
        paths.add(cell.canonical_source)
        paths.update(view.source for view in cell.views)
    for step in plan.designs:
        spec = step.inspection.spec
        paths.update(spec.source_documents)
        paths.add(spec.netlist_snapshot.source_path)
        paths.update(spec.pdk.source_paths)
    for step in plan.layouts:
        paths.update(step.planning.source_records)
    for step in plan.testbenches:
        paths.add(step.source_snapshot.source_path)
        paths.update(step.simulation.source_documents)
        native_setup = step.simulation.native_setup
        if native_setup is not None:
            paths.update(native_setup.pdk.source_paths)
            for model_set in native_setup.pdk.simulation.model_sets.values():
                paths.update(model_set.files)
            paths.add(native_setup.source_snapshot.source_path)
            rdb_contract = native_setup.rdb_contract
            if rdb_contract is not None:
                paths.add(rdb_contract.source_snapshot.source_path)
                paths.update(
                    snapshot.source_path
                    for snapshot in rdb_contract.support_source_snapshots
                )
    for step in plan.views:
        paths.add(step.view.source)
        if step.source_snapshot is not None:
            paths.add(step.source_snapshot.source_path)
    return frozenset(Path(path).resolve() for path in paths)


def _oa_plan_source_expectations(
    plan: OALibraryRebuildPlan,
) -> tuple[dict[Path, str], dict[Path, Mapping[str, Any]]]:
    exact = {
        path.resolve(): snapshot.text
        for path, snapshot in plan.netlist_snapshots.items()
    }
    documents: dict[Path, Mapping[str, Any]] = {
        path.resolve(): document
        for path, document in plan.source.source_documents.items()
    }
    for source_root in plan.source.source_roots:
        documents.update(
            {
                path.resolve(): document
                for path, document in source_root.source_documents.items()
            }
        )
    for step in plan.designs:
        spec = step.inspection.spec
        documents.update(
            {path.resolve(): document for path, document in spec.source_documents.items()}
        )
        documents.update(
            {path.resolve(): document for path, document in spec.pdk.source_documents.items()}
        )
        exact[spec.netlist_snapshot.source_path.resolve()] = spec.netlist_snapshot.text
    for step in plan.layouts:
        exact.update(
            {
                path.resolve(): record
                for path, record in step.planning.source_records.items()
            }
        )
    for step in plan.testbenches:
        exact[step.source_snapshot.source_path.resolve()] = step.source_snapshot.text
        simulation = step.simulation
        documents.update(
            {
                path.resolve(): document
                for path, document in simulation.source_documents.items()
            }
        )
        if simulation.source_snapshot is not None:
            exact[simulation.source_snapshot.source_path.resolve()] = (
                simulation.source_snapshot.text
            )
        native_setup = simulation.native_setup
        if native_setup is not None:
            exact[native_setup.source_snapshot.source_path.resolve()] = (
                native_setup.source_snapshot.text
            )
            documents.update(
                {
                    path.resolve(): document
                    for path, document in native_setup.pdk.source_documents.items()
                }
            )
            rdb_contract = native_setup.rdb_contract
            if rdb_contract is not None:
                exact[rdb_contract.source_snapshot.source_path.resolve()] = (
                    rdb_contract.source_snapshot.text
                )
                exact.update(
                    {
                        snapshot.source_path.resolve(): snapshot.text
                        for snapshot in rdb_contract.support_source_snapshots
                    }
                )
    for step in plan.views:
        if step.source_snapshot is not None:
            exact[step.source_snapshot.source_path.resolve()] = step.source_snapshot.text
    return exact, documents


def validate_oa_plan_source_members(
    plan: OALibraryRebuildPlan,
    members: Sequence[Source],
) -> None:
    """Prove that Action sources are complete and match the typed OA snapshots."""

    records = {member.location: member.text for member in members}
    required = oa_plan_source_paths(plan)
    if not required.issubset(records):
        missing = sorted(path.as_posix() for path in required - records.keys())
        raise ValueError(f"typed OA plan source closure is incomplete: {missing}")
    exact, documents = _oa_plan_source_expectations(plan)
    for path, record in exact.items():
        if records.get(path) != record:
            raise ValueError(f"typed OA plan source snapshot drift: {path}")
    for path, document in documents.items():
        try:
            parsed = tomllib.loads(records[path])
        except (KeyError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"typed OA plan document snapshot drift: {path}") from exc
        if json_value(parsed) != json_value(document):
            raise ValueError(f"typed OA plan document snapshot drift: {path}")


def _topological_order(
    items: Sequence[_T],
    dependencies: Mapping[_T, Iterable[_T]],
    *,
    label: str,
) -> tuple[_T, ...]:
    position = {item: index for index, item in enumerate(items)}
    if len(position) != len(items):
        raise ValueError(f"{label} contains duplicate build entries")
    remaining = {
        item: set(dependencies.get(item, ()))
        for item in items
    }
    unknown = {
        dependency
        for values in remaining.values()
        for dependency in values
        if dependency not in position
    }
    if unknown:
        raise ValueError(f"{label} names unknown dependencies: {sorted(unknown)!r}")
    ordered: list[_T] = []
    while remaining:
        ready = sorted(
            (item for item, values in remaining.items() if not values),
            key=position.__getitem__,
        )
        if not ready:
            cycle = sorted(remaining, key=position.__getitem__)
            raise ValueError(f"{label} dependency cycle: {cycle!r}")
        for item in ready:
            ordered.append(item)
            remaining.pop(item)
        for values in remaining.values():
            values.difference_update(ready)
    return tuple(ordered)


def _override_inspection_library(
    inspection: DesignInspection,
    library: str,
) -> DesignInspection:
    if inspection.spec.library == library:
        return inspection
    spec = replace(inspection.spec, library=library)
    return replace(inspection, spec=spec)


def _load_definitions(
    source: OALibrarySource,
) -> tuple[
    Mapping[str, NetlistSubcircuit],
    Mapping[Path, NetlistSnapshot],
]:
    netlist_cells = tuple(
        cell
        for cell in source.cells
        if any(view.kind == "spectre_netlist" for view in cell.views)
    )
    snapshots = {
        path: load_netlist_snapshot(path)
        for path in dict.fromkeys(
            cell.canonical_source for cell in netlist_cells
        )
    }
    definitions = parse_subcircuit_definitions(tuple(snapshots.values()))
    declared = {cell.cell for cell in netlist_cells}
    discovered = set(definitions)
    if declared != discovered:
        raise ValueError(
            "cell manifests and canonical subckt definitions differ: "
            f"missing_manifests={sorted(discovered - declared)}, "
            f"missing_subckts={sorted(declared - discovered)}"
        )
    for cell in netlist_cells:
        if definitions[cell.cell].source_path != cell.canonical_source:
            raise ValueError(
                f"{cell.cell} canonical_source does not own its subckt definition"
            )
    return definitions, snapshots


def _plan_designs(
    source: OALibrarySource,
    library: str,
    definitions: Mapping[str, NetlistSubcircuit],
    platform: PlatformSnapshot,
    netlist_snapshots: Mapping[Path, NetlistSnapshot] | None = None,
) -> tuple[DesignRebuildStep, ...]:
    inspections: list[DesignInspection] = []
    for cell in source.cells:
        if cell.design_spec is None:
            continue
        inspection = inspect_design(
            cell.design_spec,
            project=source.project,
            platform=platform,
            netlist_snapshot=(
                None
                if netlist_snapshots is None
                else netlist_snapshots.get(cell.canonical_source)
            ),
        )
        if inspection.spec.library != source.name:
            raise ValueError(
                f"design spec library differs from {source.name}: {cell.design_spec}"
            )
        if inspection.spec.cell != cell.cell:
            raise ValueError(f"design spec is not owned by its cell: {cell.design_spec}")
        if inspection.spec.pdk.key != source.pdk:
            raise ValueError(f"design spec uses the wrong PDK: {cell.design_spec}")
        inspections.append(_override_inspection_library(inspection, library))
    if not inspections:
        raise ValueError("OA library has no rebuild design entry points")

    owner: dict[str, str] = {}
    imports_by_top: dict[str, tuple[str, ...]] = {}
    inspection_by_top: dict[str, DesignInspection] = {}
    for inspection in inspections:
        top = inspection.spec.cell
        if top in inspection_by_top:
            raise ValueError(f"duplicate design rebuild entry point: {top}")
        inspection_by_top[top] = inspection
        imported = inspection.hierarchy
        imports_by_top[top] = imported
        for cell in imported:
            if cell not in definitions:
                raise ValueError(f"design rebuild imports undeclared cell {cell}")
            previous = owner.setdefault(cell, top)
            if previous != top:
                raise ValueError(
                    f"cell {cell} is imported by both {previous} and {top}"
                )
    declared = {cell.cell for cell in source.cells if cell.design_spec is not None}
    if set(owner) != declared:
        raise ValueError(
            "design rebuild entry points do not cover the complete library: "
            f"missing={sorted(declared - set(owner))}"
        )

    dependencies: dict[str, set[str]] = {
        inspection.spec.cell: set() for inspection in inspections
    }
    primitive_masters: set[str] = set()
    for cell, definition in definitions.items():
        if cell not in declared:
            continue
        top = owner[cell]
        for instance in parse_subcircuit_instances(definition):
            dependency_top = owner.get(instance.master)
            if dependency_top is None:
                primitive_masters.add(instance.master)
            elif dependency_top != top:
                dependencies[top].add(dependency_top)
    order = _topological_order(
        tuple(item.spec.cell for item in inspections),
        dependencies,
        label="design rebuild",
    )
    return tuple(
        DesignRebuildStep(
            inspection=inspection_by_top[top],
            imported_cells=imports_by_top[top],
            dependencies=tuple(sorted(dependencies[top])),
            instance_parameters=_instance_parameter_expectations(
                definitions[top], definitions, declared
            ),
        )
        for top in order
    )


_PARAMETER_ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)=(?P<value>[^\s]+)\Z"
)
_SPICEIN_MASTER_MAP = {"resistor": "res", "capacitor": "cap"}


def _parameter_assignments(
    tokens: Iterable[str],
    *,
    context: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in tokens:
        match = _PARAMETER_ASSIGNMENT.fullmatch(token)
        if match is None:
            raise ValueError(f"unsupported parameter assignment in {context}: {token}")
        name = match.group("name")
        if name in result:
            raise ValueError(f"duplicate parameter assignment in {context}: {name}")
        result[name] = match.group("value")
    return result


def _subcircuit_default_parameters(
    definition: NetlistSubcircuit,
) -> dict[str, str]:
    tokens = list(definition.parameters)
    for statement in definition.statements:
        if statement.lower().startswith("parameters "):
            tokens.extend(statement.split()[1:])
    return _parameter_assignments(tokens, context=f"subckt {definition.name}")


def _instance_parameter_expectations(
    definition: NetlistSubcircuit,
    definitions: Mapping[str, NetlistSubcircuit],
    declared_cells: set[str],
) -> tuple[InstanceParameterExpectation, ...]:
    result: list[InstanceParameterExpectation] = []
    for instance in parse_subcircuit_instances(definition):
        expected: dict[str, str] = {}
        if instance.master in declared_cells:
            expected.update(_subcircuit_default_parameters(definitions[instance.master]))
            expected.update(
                _parameter_assignments(
                    instance.parameters,
                    context=f"instance {definition.name}/{instance.name}",
                )
            )
        result.append(
            InstanceParameterExpectation(
                instance=instance.name,
                master=_SPICEIN_MASTER_MAP.get(instance.master, instance.master),
                parameters=tuple(sorted(expected.items())),
            )
        )
    return tuple(result)


def _plan_testbenches(
    source: OALibrarySource,
    definitions: Mapping[str, NetlistSubcircuit],
    netlist_snapshots: Mapping[Path, NetlistSnapshot],
    platform: PlatformSnapshot,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]] | None,
) -> tuple[TestbenchRebuildStep, ...]:
    declared_cells = {cell.cell for cell in source.cells}
    result: list[TestbenchRebuildStep] = []
    for cell in source.cells:
        if cell.role != "testbench":
            continue
        definition = definitions.get(cell.cell)
        if definition is None or definition.source_path != cell.canonical_source:
            raise ValueError(f"testbench source does not own {cell.cell}")
        setup_sources = {
            view.source for view in cell.views if view.kind in {"config", "maestro"}
        }
        if len(setup_sources) != 1:
            raise ValueError(
                f"testbench {cell.cell} config and Maestro views must share one setup source"
            )
        setup_source = next(iter(setup_sources))
        simulation = load_oa_simulation_spec(
            setup_source,
            project=source.project,
            platform=platform,
            architecture_source_documents=architecture_source_documents,
        )
        if simulation.library != source.name or simulation.cell != cell.cell:
            raise ValueError(f"testbench setup identity differs from {cell.cell}")
        if simulation.dut not in declared_cells:
            raise ValueError(f"testbench {cell.cell} names unknown DUT {simulation.dut}")
        measurement_views = tuple(
            view for view in cell.views if view.name == "measurement"
        )
        if len(measurement_views) != 1:
            raise ValueError(
                f"testbench {cell.cell} must declare exactly one measurement view"
            )
        if simulation.native_setup is None:
            raise ValueError(f"testbench {cell.cell} is not native schema-3")
        measurement_source = measurement_views[0].source
        if measurement_source != simulation.native_setup.source:
            raise ValueError(
                f"native testbench {cell.cell} measurement view must share "
                "the canonical setup source"
            )
        dependencies = tuple(
            sorted(
                {
                    instance.master
                    for instance in parse_subcircuit_instances(definition)
                    if instance.master in declared_cells
                }
            )
        )
        result.append(
            TestbenchRebuildStep(
                cell=cell.cell,
                source_snapshot=netlist_snapshots[cell.canonical_source],
                dependencies=dependencies,
                simulation=simulation,
            )
        )
    return tuple(result)


def _plan_layouts(
    source: OALibrarySource,
    library: str,
    definitions: Mapping[str, NetlistSubcircuit],
    platform: PlatformSnapshot,
    netlist_snapshots: Mapping[Path, NetlistSnapshot] | None = None,
) -> tuple[LayoutRebuildStep, ...]:
    specs: list[LayoutSpec] = []
    planning_by_key: dict[tuple[str, str], LayoutPlanningResult] = {}
    netlist_inventory = dict(netlist_snapshots or {})
    for cell in source.cells:
        for spec_path in cell.layout_specs:
            spec = load_layout_spec(
                spec_path,
                project=source.project,
                oa_source=source,
                platform=platform,
                netlist_inventory=netlist_inventory,
            )
            for snapshot in spec.source_snapshots:
                previous = netlist_inventory.setdefault(
                    snapshot.source_path,
                    snapshot,
                )
                if previous != snapshot:
                    raise ValueError(
                        "layout specs disagree on a shared netlist snapshot"
                    )
            if spec.library != source.name:
                raise ValueError(
                    f"layout spec library differs from {source.name}: {spec_path}"
                )
            if spec.cell != cell.cell:
                raise ValueError(f"layout spec is not owned by its cell: {spec_path}")
            if spec.pdk.key != source.pdk:
                raise ValueError(f"layout spec uses the wrong PDK: {spec_path}")
            if library != spec.library:
                spec = replace(spec, library=library)
            key = (spec.cell, spec.view)
            if key in planning_by_key:
                raise ValueError(f"duplicate canonical layout rebuild view: {key}")
            planning = plan_layout_snapshot(spec)
            specs.append(spec)
            planning_by_key[key] = planning
    keys = tuple((spec.cell, spec.view) for spec in specs)
    key_set = set(keys)
    dependencies: dict[tuple[str, str], set[tuple[str, str]]] = {
        key: set() for key in keys
    }
    declared_layouts = {
        (cell.cell, view.name): view
        for cell in source.cells
        for view in cell.views
        if view.kind == "layout"
    }
    if set(declared_layouts) != key_set:
        raise ValueError("OA layout views and layout specs disagree")
    for key, view in declared_layouts.items():
        for dependency in view.dependencies:
            master = (dependency.cell, dependency.view)
            if master not in key_set:
                raise ValueError(
                    f"canonical layout {key} requires undeclared generated master {master}"
                )
            dependencies[key].add(master)

    primitive_masters = source.primitive_masters
    layout_relevant_cells = {cell.cell for cell in source.cells if cell.role == "design"}
    for cell, definition in definitions.items():
        if cell not in layout_relevant_cells:
            continue
        for instance in parse_subcircuit_instances(definition):
            if instance.master not in definitions and instance.master not in primitive_masters:
                raise ValueError(
                    f"canonical cell {cell} has unresolved master {instance.master}"
                )

    order = _topological_order(keys, dependencies, label="layout rebuild")
    return tuple(
        LayoutRebuildStep(
            planning=planning_by_key[key],
            dependencies=tuple(sorted(dependencies[key])),
        )
        for key in order
    )


def _plan_views(
    source: OALibrarySource,
    testbenches: tuple[TestbenchRebuildStep, ...] = (),
) -> tuple[ViewRebuildStep, ...]:
    """Validate and order the complete explicit cell/view dependency graph."""

    setup_snapshots = {
        step.simulation.native_setup.source: (
            step.simulation.native_setup.source_snapshot
        )
        for step in testbenches
    }
    text_kinds = {"spectre_model", "veriloga", "system_verilog", "skill"}
    text_snapshots = dict(setup_snapshots)
    steps: list[ViewRebuildStep] = []
    for cell in source.cells:
        for view in cell.views:
            source_snapshot = None
            if view.kind in text_kinds:
                source_snapshot = text_snapshots.get(view.source)
                if source_snapshot is None:
                    source_snapshot = load_text_source_snapshot(view.source)
                    text_snapshots[view.source] = source_snapshot
            steps.append(
                ViewRebuildStep(
                    cell=cell.cell,
                    owner=cell.owner,
                    view=view,
                    source_snapshot=source_snapshot,
                )
            )
    planned_steps = tuple(steps)
    keys = tuple(OAViewReference(step.cell, step.view.name) for step in planned_steps)
    step_by_key = {
        key: step for key, step in zip(keys, planned_steps, strict=True)
    }
    dependencies = {
        key: step_by_key[key].view.dependencies
        for key in keys
    }
    order = _topological_order(keys, dependencies, label="OA view rebuild")
    return tuple(step_by_key[key] for key in order)


def plan_oa_library_rebuild(
    manifest_path: Path,
    *,
    project: Project,
    library: str | None = None,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> OALibraryRebuildPlan:
    """Prove that source alone describes every cell and canonical OA view."""

    resolved_manifest = manifest_path.resolve()
    if oa_source_inventory is None:
        source = load_oa_library_source(
            resolved_manifest,
            project=project,
        )
    else:
        try:
            source_snapshot = oa_source_inventory[resolved_manifest]
        except KeyError as exc:
            raise ValueError(
                f"OA source inventory has no {resolved_manifest} entry"
            ) from exc
        source = resolve_oa_library_source(
            resolved_manifest,
            project=project,
            snapshot=source_snapshot,
        )
    target_library = library or source.name
    if _IDENTIFIER.fullmatch(target_library) is None:
        raise ValueError("target OA library must be an identifier")
    if target_library != source.name:
        raise ValueError(
            f"OA assembly may materialize only its unique library {source.name}"
        )
    definitions, netlist_snapshots = _load_definitions(source)
    if platform_inventory is None:
        from sigilicon.execution.model import Resources

        platform_snapshot: PlatformSnapshot = load_platform(
            source.project,
            source.pdk,
            resources=Resources(),
        )
    else:
        if isinstance(platform_inventory, PlatformInventory):
            platform_snapshot = platform_inventory
        else:
            try:
                platform_snapshot = platform_inventory[source.pdk]
            except KeyError as exc:
                raise ValueError(
                    f"platform inventory has no {source.pdk!r} entry"
                ) from exc
        resolve_platform_snapshot(
            source.project,
            source.pdk,
            snapshot=platform_snapshot,
        )
    designs = _plan_designs(
        source,
        target_library,
        definitions,
        platform_snapshot,
        netlist_snapshots=netlist_snapshots,
    )
    layouts = _plan_layouts(
        source,
        target_library,
        definitions,
        platform_snapshot,
        netlist_snapshots=netlist_snapshots,
    )
    testbenches = _plan_testbenches(
        source,
        definitions,
        netlist_snapshots,
        platform_snapshot,
        architecture_source_documents,
    )
    views = _plan_views(source, testbenches)
    expected_views = MappingProxyType(
        {
            cell.cell: tuple(view.name for view in cell.views)
            for cell in source.cells
        }
    )
    return OALibraryRebuildPlan(
        source=source,
        library=target_library,
        cells=tuple(cell.cell for cell in source.cells),
        designs=designs,
        layouts=layouts,
        testbenches=testbenches,
        views=views,
        expected_views=expected_views,
        netlist_snapshots=MappingProxyType(dict(netlist_snapshots)),
    )


def build_oa_layout_ir(
    plan: OALibraryRebuildPlan,
    *,
    source_paths: Mapping[Path, Path],
    managed_project_root: Path,
) -> OALibraryRebuildPlan:
    """Generate every owner layout from sealed sources during managed execution."""

    layouts = tuple(
        replace(
            step,
            planning=build_managed_layout_ir(
                step.planning,
                source_paths=source_paths,
                managed_project_root=(
                    Path(managed_project_root)
                    / f"{index:03d}-{step.spec.cell}-{step.spec.view}"
                ),
            ),
        )
        for index, step in enumerate(plan.layouts)
    )
    return replace(plan, layouts=layouts)


def attest_oa_testbench(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int = 300,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> dict[str, object]:
    """Check one current native setup through Cadence's read-only API."""

    spec = step.simulation
    if step.cell != spec.cell:
        raise ValueError(f"testbench plan identity mismatch: {step.cell} != {spec.cell}")
    if spec.native_setup is None:
        raise ValueError(f"native setup is not declared for {step.cell}")
    project = plan.source.project
    with workspace_operation(
        client,
        project.workspace_root,
        "attest-oa-native-setup",
        policy=OperationPolicy.READ_ONLY,
        acquire_flow_lock=False,
        record_incident=False,
        operation_id=operation_id,
    ) as operation, operation.view_lease(
        plan.library,
        cells=(step.cell,),
        views=((step.cell, "config"), (step.cell, "maestro")),
    ):
        if callable(bind_operation):
            bind_operation(operation)
        if not (
            cell_view_exists(client, plan.library, step.cell, "config")
            and cell_view_exists(client, plan.library, step.cell, "maestro")
        ):
            raise RuntimeError(
                f"cannot attest missing OA config/maestro views for "
                f"{plan.library}/{step.cell}"
            )
        setup_attestation = attest_native_setup(
            spec,
            client,
            operation=operation,
            timeout=min(timeout, 300),
        )
    return {
        "passed": True,
        "library": plan.library,
        "testbench": step.cell,
        "setup_attestation": setup_attestation,
        "simulation_run": False,
        "product_qualification_conclusion": False,
    }


def _testbench_dependency_cells(
    plan: OALibraryRebuildPlan,
    testbench: str,
) -> tuple[str, ...]:
    """Return the source-ordered transitive cell closure of one testbench.

    A native run consumes the DUT hierarchy and protocol stimulus in addition
    to the Maestro cell.  Scoping parity to the testbench cell alone therefore
    cannot identify which OA design materialization was actually simulated.
    """

    matches = tuple(step for step in plan.testbenches if step.cell == testbench)
    if len(matches) != 1:
        raise ValueError(f"unknown OA testbench in assembly: {testbench}")
    design_dependencies = {
        step.inspection.spec.cell: step.dependencies for step in plan.designs
    }
    selected = {testbench}
    pending = list(matches[0].dependencies)
    while pending:
        cell = pending.pop()
        if cell in selected:
            continue
        if cell not in plan.expected_views:
            raise ValueError(
                f"OA testbench {testbench} depends on unknown cell {cell}"
            )
        selected.add(cell)
        pending.extend(design_dependencies.get(cell, ()))
    return tuple(cell for cell in plan.cells if cell in selected)


def check_oa_parity(
    plan: OALibraryRebuildPlan,
    client: Any,
    *,
    timeout: int = 120,
    acquire_flow_lock: bool = True,
    record_incident: bool = False,
    testbench: str | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    operation: Any | None = None,
) -> dict[str, object]:
    """Check current OA inventory/content against the Git assembly.

    ``testbench`` scopes the check to one complete generated testbench cell.
    The unscoped form checks the full assembly; neither form performs native
    setup semantic attestation.
    """

    plan.require_layout_ir("OA parity")

    if testbench is not None and testbench not in plan.expected_views:
        raise ValueError(f"unknown OA testbench in assembly: {testbench}")

    inventory = list_cells(client, plan.library)
    actual = {
        str(row["name"]): tuple(sorted(str(view) for view in row["views"]))
        for row in inventory["cells"]
    }
    scoped_cells = (
        tuple(plan.cells)
        if testbench is None
        else _testbench_dependency_cells(plan, testbench)
    )
    scoped_cell_set = set(scoped_cells)
    expected = {
        cell: tuple(sorted(views))
        for cell, views in plan.expected_views.items()
        if cell in scoped_cell_set
    }
    scoped_actual = (
        actual
        if testbench is None
        else {
            cell: actual.get(cell, ())
            for cell in scoped_cells
            if cell in actual
        }
    )
    missing_cells = sorted(set(expected) - set(scoped_actual))
    extra_cells = sorted(set(scoped_actual) - set(expected))
    missing_views = {
        cell: sorted(set(expected[cell]) - set(scoped_actual[cell]))
        for cell in sorted(set(expected) & set(scoped_actual))
        if set(expected[cell]) - set(scoped_actual[cell])
    }
    extra_views = {
        cell: sorted(set(scoped_actual[cell]) - set(expected[cell]))
        for cell in sorted(set(expected) & set(scoped_actual))
        if set(scoped_actual[cell]) - set(expected[cell])
    }
    stale_or_modified: dict[str, str] = {}
    design_reports: list[dict[str, object]] = []
    layout_reports: list[dict[str, object]] = []
    text_view_reports: list[dict[str, object]] = []
    for step in plan.designs:
        if step.inspection.spec.cell not in scoped_cell_set:
            continue
        cell = step.inspection.spec.cell
        if cell not in actual or not {"schematic", "symbol"}.issubset(actual[cell]):
            continue
        try:
            report = attest_oa_design(
                step.inspection,
                client,
                timeout=timeout,
                acquire_flow_lock=acquire_flow_lock,
                record_incident=record_incident,
                operation_id=operation_id,
                bind_operation=bind_operation,
                operation=operation,
                instance_parameters={
                    item.instance: (item.master, dict(item.parameters))
                    for item in step.instance_parameters
                },
            )
        except (OSError, RuntimeError, ValueError) as exc:
            stale_or_modified[f"{cell}/schematic+symbol"] = str(exc)
            continue
        design_reports.append(report)
    if testbench is None:
        layout_steps = tuple(
            step
            for step in plan.layouts
            if step.spec.cell in actual and step.spec.view in actual[step.spec.cell]
        )
    else:
        # Simulation identity needs the actual source-derived layout state only
        # for cells in its closure; it does not validate unrelated layouts.
        layout_steps = tuple(
            step
            for step in plan.layouts
            if step.spec.cell in scoped_cell_set
            and step.spec.cell in actual
            and step.spec.view in actual[step.spec.cell]
        )
    if layout_steps:
        try:
            _attest_layout_steps(
                plan,
                layout_steps,
                client,
                operation_name="check-oa-library-layouts",
                timeout=timeout,
                acquire_flow_lock=acquire_flow_lock,
                record_incident=record_incident,
                operation_id=operation_id,
                bind_operation=bind_operation,
                operation=operation,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            stale_or_modified["canonical-layouts"] = str(exc)
    for step in layout_steps:
        layout_reports.append(
            {
                "cell": step.spec.cell,
                "view": step.spec.view,
            }
        )
    testbench_reports: list[dict[str, object]] = []
    for step in plan.testbenches:
        if testbench is not None and step.cell != testbench:
            continue
        if step.cell not in scoped_actual:
            continue
        expected = tuple(plan.expected_views[step.cell])
        present = tuple(view for view in expected if view in scoped_actual[step.cell])
        if not present:
            continue
        complete = set(expected).issubset(actual[step.cell])
        testbench_reports.append(
            {
                "cell": step.cell,
                "complete": complete,
            }
        )
    for step in plan.views:
        if step.cell not in scoped_cell_set:
            continue
        if step.view.kind not in {"spectre_model", "veriloga", "system_verilog"}:
            continue
        if step.cell not in actual or step.view.name not in actual[step.cell]:
            continue
        text_view_reports.append(
            {
                "cell": step.cell,
                "view": step.view.name,
            }
        )
    passed = not any(
        (
            missing_cells,
            extra_cells,
            missing_views,
            extra_views,
            stale_or_modified,
        )
    )
    return {
        "passed": passed,
        "library": plan.library,
        "testbench": testbench,
        "cell_count": len(scoped_cells),
        "dependency_cells": list(scoped_cells),
        "layout_count": len(layout_steps),
        "missing_cells": missing_cells,
        "extra_cells": extra_cells,
        "missing_views": missing_views,
        "extra_views": extra_views,
        "stale_or_modified_views": stale_or_modified,
        "designs": design_reports,
        "layouts": layout_reports,
        "testbenches": testbench_reports,
        "text_views": text_view_reports,
    }


def _attest_layout_steps(
    plan: OALibraryRebuildPlan,
    steps: Sequence[LayoutRebuildStep],
    client: Any,
    *,
    operation_name: str,
    timeout: int,
    acquire_flow_lock: bool = True,
    record_incident: bool = True,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    operation: Any | None = None,
) -> None:
    """Validate exact generated views through the common read-only boundary."""

    if not steps:
        return
    layout_cells = tuple(dict.fromkeys(step.spec.cell for step in steps))
    layout_views = tuple(
        dict.fromkeys((step.spec.cell, step.spec.view) for step in steps)
    )

    def attest(current: Any) -> None:
        with current.view_lease(
            plan.library,
            cells=layout_cells,
            views=layout_views,
        ):
            for step in steps:
                validate_layout_plan(
                    client,
                    step.plan,
                    operation=current,
                    timeout=timeout,
                )

    if operation is not None:
        attest(operation)
        return
    project = plan.source.project
    with workspace_operation(
        client,
        project.workspace_root,
        operation_name,
        policy=OperationPolicy.READ_ONLY,
        acquire_flow_lock=acquire_flow_lock,
        record_incident=record_incident,
        operation_id=operation_id,
    ) as current:
        if callable(bind_operation):
            bind_operation(current)
        attest(current)


def _discard_undeclared_oa_cache(
    plan: OALibraryRebuildPlan,
    actual: Mapping[str, Sequence[str]],
    client: Any,
    *,
    timeout: int,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> None:
    """Remove only OA cells/views absent from the current Git assembly."""

    expected = {
        cell: set(views) for cell, views in plan.expected_views.items()
    }
    extra_cells = sorted(set(actual) - set(expected))
    extra_views = sorted(
        (cell, view)
        for cell, views in actual.items()
        if cell in expected
        for view in set(views) - expected[cell]
    )
    if not extra_cells and not extra_views:
        return
    target_cells = tuple(
        sorted(set(extra_cells) | {cell for cell, _view in extra_views})
    )
    project = plan.source.project
    with workspace_operation(
        client,
        project.workspace_root,
        "discard-undeclared-oa-cache",
        policy=OperationPolicy.DIRECT_MUTATION,
        operation_id=operation_id,
    ) as operation, operation.view_lease(
        plan.library,
        cells=target_cells,
    ):
        if callable(bind_operation):
            bind_operation(operation)
        for cell in extra_cells:
            with operation.mutation_scope(
                plan.library,
                cells=(cell,),
                expected_deleted_cells=(cell,),
                phase=f"discard undeclared OA cell {plan.library}/{cell}",
            ):
                delete_cell(
                    client,
                    plan.library,
                    cell,
                    operation=operation,
                    timeout=timeout,
                )
        for cell, view in extra_views:
            if cell in extra_cells:
                continue
            with operation.mutation_scope(
                plan.library,
                cells=(cell,),
                views=((cell, view),),
                phase=f"discard undeclared OA view {plan.library}/{cell}/{view}",
            ):
                delete_cell_view(
                    client,
                    plan.library,
                    cell,
                    view,
                    operation=operation,
                    timeout=timeout,
                )


def rebuild_oa_library(
    plan: OALibraryRebuildPlan,
    client: Any,
    *,
    source_paths: Mapping[Path, Path],
    resource_paths: Mapping[Path, Path],
    resources: Any,
    cell: str | None = None,
    testbench: str | None = None,
    timeout: int = 300,
    report: Callable[[str], None] | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> dict[str, object]:
    """Rebuild the selected source-defined OA objects from the current Git tree.

    Existing OA objects are disposable materialized cache state.  A selective
    ``cell`` or ``testbench`` rebuild limits the mutation scope to that cell;
    a full rebuild refreshes every source-defined object.
    """

    plan.require_layout_ir("OA rebuild")
    target_cell = cell
    if target_cell is not None and testbench is not None:
        raise ValueError("select at most one OA rebuild target")
    emit = report or (lambda _message: None)
    visible = client.library.list(timeout=30)
    all_testbenches = plan.testbenches
    selected_testbenches = all_testbenches
    if testbench is not None:
        selected_testbenches = tuple(
            step for step in all_testbenches if step.cell == testbench
        )
        if len(selected_testbenches) != 1:
            raise ValueError(f"unknown OA testbench in assembly: {testbench}")
    elif target_cell is not None:
        if target_cell not in plan.expected_views:
            raise ValueError(f"unknown OA cell in assembly: {target_cell}")
        if any(step.cell == target_cell for step in all_testbenches):
            raise ValueError(
                f"{target_cell} is a testbench; select it with --testbench"
            )
        selected_testbenches = ()
    if (
        target_cell is not None or testbench is not None
    ) and plan.library not in visible:
        raise RuntimeError(
            "selective OA rebuild requires the target library to already exist"
        )
    actual: dict[str, tuple[str, ...]] = {}
    if plan.library in visible and testbench is None:
        inventory = list_cells(client, plan.library)
        actual = {
            str(row["name"]): tuple(str(view) for view in row["views"])
            for row in inventory["cells"]
        }
        if target_cell is None:
            _discard_undeclared_oa_cache(
                plan,
                actual,
                client,
                timeout=timeout,
                operation_id=operation_id,
                bind_operation=bind_operation,
            )
            if set(actual) - set(plan.expected_views) or any(
                set(views) - set(plan.expected_views.get(actual_cell, ()))
                for actual_cell, views in actual.items()
            ):
                inventory = list_cells(client, plan.library)
                actual = {
                    str(row["name"]): tuple(str(view) for view in row["views"])
                    for row in inventory["cells"]
                }
    if testbench is None:
        design_steps = tuple(
            step
            for step in plan.designs
            if target_cell is None or step.inspection.spec.cell == target_cell
        )
        for index, step in enumerate(design_steps, start=1):
            design_cell = step.inspection.spec.cell
            emit(
                f"schematic {index}/{len(design_steps)}: "
                f"{'refresh' if design_cell in actual else 'create'} source "
                f"{plan.library}/{design_cell}"
            )
            synchronize_design(
                step.inspection,
                client,
                source_paths=source_paths,
                overwrite=True,
                timeout=timeout,
                operation_id=operation_id,
                bind_operation=bind_operation,
                resources=resources,
            )
            actual[design_cell] = ("netlist", "schematic", "symbol")
        text_steps = tuple(
            step
            for step in plan.views
            if step.view.kind in {"spectre_model", "veriloga", "system_verilog"}
            and (target_cell is None or step.cell == target_cell)
        )
        for index, step in enumerate(text_steps, start=1):
            if step.source_snapshot is None:
                raise RuntimeError("OA text-view plan has no immutable source")
            identity = f"{plan.library}/{step.cell}/{step.view.name}"
            action = "refresh" if cell_view_exists(
                client, plan.library, step.cell, step.view.name
            ) else "create"
            emit(f"text view {index}/{len(text_steps)}: {action} {identity}")
            sync_oa_text_view(
                client,
                project=plan.source.project,
                library=plan.library,
                cell=step.cell,
                view=step.view.name,
                kind=step.view.kind,
                source=step.source_snapshot,
                resources=resources,
                overwrite=True,
                timeout=timeout,
                operation_id=operation_id,
                bind_operation=bind_operation,
            )
        layout_steps = tuple(
            step
            for step in plan.layouts
            if target_cell is None or step.spec.cell == target_cell
        )
        for index, step in enumerate(layout_steps, start=1):
            identity = f"{plan.library}/{step.spec.cell}/{step.spec.view}"
            prefix = f"layout {index}/{len(layout_steps)}"
            action = "refresh" if cell_view_exists(
                client,
                plan.library,
                step.spec.cell,
                step.spec.view,
            ) else "generate"
            emit(f"{prefix}: {action} {identity}")
            generate_layout(
                step.planning,
                client,
                overwrite=True,
                timeout=timeout,
                operation_id=operation_id,
                bind_operation=bind_operation,
            )
    for index, step in enumerate(selected_testbenches, start=1):
        emit(
            f"testbench {index}/{len(selected_testbenches)}: rebuild "
            f"{plan.library}/{step.cell}"
        )
        model_path = step.simulation.native_setup.pdk.simulation.default.file.resolve()
        model_file = resource_paths.get(model_path, source_paths.get(model_path))
        if model_file is None:
            raise ValueError(
                "OA testbench PDK model is outside the sealed input closure"
            )
        sync_oa_testbench(
            step.simulation,
            step.source_snapshot,
            client,
            model_file=model_file,
            resources=resources,
            overwrite=True,
            timeout=timeout,
            operation_id=operation_id,
            bind_operation=bind_operation,
        )
        actual[step.cell] = tuple(plan.expected_views[step.cell])
    emit(f"check: {plan.library}")
    if testbench is not None:
        return check_oa_parity(
            plan,
            client,
            testbench=testbench,
            timeout=timeout,
            record_incident=False,
            operation_id=operation_id,
            bind_operation=bind_operation,
        )
    return check_oa_parity(
        plan,
        client,
        timeout=timeout,
        record_incident=False,
        operation_id=operation_id,
        bind_operation=bind_operation,
    )
