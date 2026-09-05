"""Execute and attest source-derived OA library plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.virtuoso.attestation import attest_native_setup
from sigilicon.virtuoso.discovery import list_cells
from sigilicon.virtuoso.layout_generation import validate_layout_plan
from sigilicon.virtuoso.oa import cell_view_exists, delete_cell, delete_cell_view
from sigilicon.virtuoso.text_view import check_oa_text_view_source
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence.design_lifecycle import attest_oa_design, synchronize_design
from sigilicon.adapters.cadence.layout_generation import (
    build_managed_layout_ir,
    generate_layout,
)
from sigilicon.adapters.cadence.oa_library import (
    LayoutRebuildStep,
    OALibraryRebuildPlan,
    TestbenchRebuildStep,
)
from sigilicon.adapters.cadence.oa_testbench import materialize_oa_models, sync_oa_testbench
from sigilicon.adapters.cadence.oa_text_view import sync_oa_text_view


def build_oa_layout_ir(
    plan: OALibraryRebuildPlan,
    *,
    source_paths: Mapping[Path, Path],
    workspace: ExecutionWorkspace,
    python_executable: Path,
) -> OALibraryRebuildPlan:
    """Generate every owner layout from sealed sources during managed execution."""

    layouts = tuple(
        replace(
            step,
            planning=build_managed_layout_ir(
                step.planning,
                source_paths=source_paths,
                workspace=workspace.scoped(
                    f"{index:03d}-{step.spec.cell}-{step.spec.view}"
                ),
                python_executable=python_executable,
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
    with workspace_operation(
        client,
        plan.source.workspace_root,
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

    if plan.selected_testbench is not None and testbench != plan.selected_testbench:
        raise ValueError("selected OA plan requires its own testbench parity scope")
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
        else (tuple(plan.cells) if plan.selected_testbench is not None else _testbench_dependency_cells(plan, testbench))
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
    if plan.selected_testbench is not None:
        # Views outside the simulation plan belong to assembly-level verification.
        scoped_actual = {
            cell: tuple(view for view in views if view in expected[cell])
            for cell, views in scoped_actual.items()
        }
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
        try:
            if step.source_snapshot is None:
                raise ValueError("native text view requires its canonical source snapshot")
            check_oa_text_view_source(
                plan.source.oa_library / step.cell / step.view.name, step.source_snapshot,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            stale_or_modified[f"{step.cell}/{step.view.name}"] = str(exc)
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
    with workspace_operation(
        client,
        plan.source.workspace_root,
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
    with workspace_operation(
        client,
        plan.source.workspace_root,
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
    artifacts: ExecutionWorkspace,
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

    plan.require_assembly("OA rebuild")
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
                repository=plan.source.repository,
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
                artifacts=artifacts.scoped(f"{index:03d}-{step.spec.cell}-{step.spec.view}"),
                operation_id=operation_id,
                bind_operation=bind_operation,
            )
    for index, step in enumerate(selected_testbenches, start=1):
        emit(
            f"testbench {index}/{len(selected_testbenches)}: rebuild "
            f"{plan.library}/{step.cell}"
        )
        model_file = materialize_oa_models(
            step.simulation.native_setup.pdk.simulation.default,
            {**source_paths, **resource_paths},
            artifacts.scoped(f"testbench-{step.cell}"),
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
