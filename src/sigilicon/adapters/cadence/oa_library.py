"""Plan a complete source-owned OA library."""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
)
from sigilicon.domain.platform import (
    PlatformSet,
    PlatformSnapshot,
    load_platform,
    resolve_platform_snapshot,
)
from sigilicon.project import Project
from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot
from sigilicon.execution._model import Source, json_value
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec, load_layout_spec
from sigilicon.adapters.cadence.design_lifecycle import (
    DesignInspection,
    inspect_design,
)
from sigilicon.adapters.cadence.layout_generation import (
    LayoutPlanningResult,
    plan_layout_snapshot,
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
            if native_setup.pdk.runtime_bound:
                for model_set in native_setup.pdk.simulation.model_sets.values():
                    paths.update(model_set.paths)
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

    records = {member.location: member for member in members}
    required = oa_plan_source_paths(plan)
    if not required.issubset(records):
        missing = sorted(path.as_posix() for path in required - records.keys())
        raise ValueError(f"typed OA plan source closure is incomplete: {missing}")
    exact, documents = _oa_plan_source_expectations(plan)
    for path, record in exact.items():
        member = records.get(path)
        if member is None or member.read_text() != record:
            raise ValueError(f"typed OA plan source snapshot drift: {path}")
    for path, document in documents.items():
        try:
            parsed = tomllib.loads(records[path].read_text())
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
    dependents: dict[_T, list[_T]] = {item: [] for item in items}
    for item in items:
        for dependency in remaining[item]:
            dependents[dependency].append(item)
    ready = deque(item for item in items if not remaining[item])
    ordered: list[_T] = []
    while ready:
        item = ready.popleft()
        ordered.append(item)
        for dependent in dependents[item]:
            remaining[dependent].remove(item)
            if not remaining[dependent]:
                ready.append(dependent)
    if len(ordered) != len(items):
        cycle = sorted(
            (item for item in items if remaining[item]),
            key=position.__getitem__,
        )
        raise ValueError(f"{label} dependency cycle: {cycle!r}")
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
    project: Project,
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
            project=project,
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
    project: Project,
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
            project=project,
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
    project: Project,
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
                project=project,
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
            planning = plan_layout_snapshot(spec, project=project)
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
    platform_inventory: PlatformSet | None = None,
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
        source = load_oa_library_source(
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
        from sigilicon.execution._model import Resources

        platform_snapshot: PlatformSnapshot = load_platform(
            project,
            source.pdk,
            resources=Resources(),
        )
    else:
        if isinstance(platform_inventory, PlatformSet):
            platform_snapshot = platform_inventory
        else:
            try:
                platform_snapshot = platform_inventory[source.pdk]
            except KeyError as exc:
                raise ValueError(
                    f"platform inventory has no {source.pdk!r} entry"
                ) from exc
        resolve_platform_snapshot(
            project,
            source.pdk,
            snapshot=platform_snapshot,
        )
    designs = _plan_designs(
        source,
        project,
        target_library,
        definitions,
        platform_snapshot,
        netlist_snapshots=netlist_snapshots,
    )
    layouts = _plan_layouts(
        source,
        project,
        target_library,
        definitions,
        platform_snapshot,
        netlist_snapshots=netlist_snapshots,
    )
    testbenches = _plan_testbenches(
        source,
        project,
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
