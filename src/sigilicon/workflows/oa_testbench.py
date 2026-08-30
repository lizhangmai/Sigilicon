"""Materialize one canonical analog testbench/config/Maestro OA cell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sigilicon.domain.netlist import (
    NetlistSnapshot,
    load_netlist_snapshot,
    materialize_netlist_snapshot,
    parse_spectre_pwl_sources,
)
from sigilicon.domain.oa_simulation import OASimulationSpec
from sigilicon.domain.source import TextSourceSnapshot
from sigilicon.virtuoso.ade import (
    create_oa_native_config_view,
    create_oa_native_maestro_view,
)
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.importer import check_and_save_schematic, import_schematic
from sigilicon.virtuoso.oa import cell_exists, cell_view_exists, delete_cell
from sigilicon.virtuoso.schematic import set_instance_parameters
from sigilicon.virtuoso.text_view import import_oa_text_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.hierarchy_import import plan_hierarchy


_TESTBENCH_VIEWS = ("netlist", "schematic", "config", "measurement", "maestro")


def _testbench_pdk(spec: OASimulationSpec) -> Any:
    return spec.native_setup.pdk


def _native_setup_source(spec: OASimulationSpec) -> TextSourceSnapshot:
    return spec.native_setup.source_snapshot


def _materialize_inline_pwl_tables(
    spec: OASimulationSpec,
    snapshot: NetlistSnapshot,
    client: Any,
    *,
    operation: Any,
) -> None:
    """Map canonical inline Spectre PWL data to native analogLib CDF fields."""

    for source in parse_spectre_pwl_sources(snapshot, spec.cell):
        parameters = {
            "srcType": "pwl",
            "pwlEntryMethod": "Voltage/Time points",
            "tvpairs": str(len(source.points)),
        }
        for index, (time, value) in enumerate(source.points, start=1):
            parameters[f"t{index}"] = time
            parameters[f"v{index}"] = value
        set_instance_parameters(
            client,
            spec.library,
            spec.cell,
            source.instance,
            parameters,
            operation=operation,
            invoke_callbacks=False,
        )


def sync_oa_testbench(
    spec: OASimulationSpec,
    canonical_source: Path | NetlistSnapshot,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 300,
) -> None:
    """Build all generated testbench views directly from Git-owned source.

    A rebuild of a selected testbench is intentionally atomic at the cell
    level: an existing disposable cell is removed after the live workspace
    checks and then all five generated views are recreated in one operation.
    No previous cache metadata is consulted.
    """

    with DisposableWork.create(prefix="sigilicon-oa-testbench-") as work:
        _sync_oa_testbench_impl(
            spec,
            canonical_source,
            client,
            overwrite=overwrite,
            timeout=timeout,
            _work=work,
        )


def _sync_oa_testbench_impl(
    spec: OASimulationSpec,
    canonical_source: Path | NetlistSnapshot,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 300,
    _work: DisposableWork | None = None,
) -> None:
    """Materialize one testbench under its caller-owned temporary scope."""

    snapshot = (
        canonical_source
        if isinstance(canonical_source, NetlistSnapshot)
        else load_netlist_snapshot(canonical_source)
    )
    hierarchy = plan_hierarchy(snapshot, top=spec.cell)
    if hierarchy.ordered_cells != (spec.cell,):
        raise ValueError(
            "canonical OA testbench source must define exactly its own cell"
        )
    if _work is None:
        raise RuntimeError("OA testbench rebuild requires a work scope")
    work = _work
    canonical = materialize_netlist_snapshot(
        snapshot,
        work.path("inputs", "testbench.scs"),
    )
    device_map = work.write_text(
        "inputs",
        ("spiceIn.devmap",),
        "devselect := resistor res\ndevselect := capacitor cap\n",
    )
    setup_source = _native_setup_source(spec)
    project = spec.project

    with workspace_operation(
        client,
        project.workspace_root,
        "rebuild-oa-testbench",
        policy=OperationPolicy.DIRECT_MUTATION,
    ) as operation, operation.view_lease(
        spec.library,
        cells=(spec.cell,),
    ):
        visible = client.library.list(timeout=30)
        if spec.library not in visible:
            raise RuntimeError(
                f"testbench materialization requires existing library {spec.library}"
            )
        expected_library = project.workspace_root / spec.library
        if operation.require_project_library_target(client, spec.library) != expected_library:
            raise RuntimeError(
                "testbench library does not resolve inside the project workspace"
            )
        if cell_exists(client, spec.library, spec.cell):
            if not overwrite:
                raise RuntimeError(
                    "refusing to overwrite existing OA testbench "
                    f"{spec.library}/{spec.cell}"
                )
            with operation.mutation_scope(
                spec.library,
                cells=(spec.cell,),
                expected_deleted_cells=(spec.cell,),
                phase=f"discard disposable testbench cache {spec.library}/{spec.cell}",
            ):
                delete_cell(
                    client,
                    spec.library,
                    spec.cell,
                    operation=operation,
                    timeout=timeout,
                )

        references = tuple(
            dict.fromkeys(
                (
                    spec.library,
                    *_testbench_pdk(spec).oa.reference_libraries,
                    "analogLib",
                    "basic",
                )
            )
        )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "netlist"), (spec.cell, "schematic")),
            phase=f"canonical testbench schematic import {spec.library}/{spec.cell}",
        ):
            import_schematic(
                client,
                spec.library,
                spec.cell,
                canonical.path,
                own_netlist=canonical.open_fd,
                reference_libraries=references,
                dev_map_file=device_map,
                overwrite=False,
                run_dir=work.directory("work", "spicein"),
                timeout=timeout,
                operation=operation,
            )
            _materialize_inline_pwl_tables(
                spec,
                snapshot,
                client,
                operation=operation,
            )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "config"),),
            phase="canonical testbench config creation",
            allow_current_config_lock=True,
        ):
            create_oa_native_config_view(
                client,
                spec,
                reference_libraries=_testbench_pdk(spec).oa.reference_libraries,
                operation=operation,
                timeout=timeout,
            )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "measurement"),),
            phase="canonical testbench native measurement view creation",
            allow_current_config_lock=True,
        ):
            import_oa_text_view(
                client,
                library=spec.library,
                cell=spec.cell,
                kind="skill",
                view="measurement",
                source=setup_source,
                log_dir=work.directory("logs", "measurement"),
                work_dir=work.directory("work", "measurement"),
                operation=operation,
                timeout=timeout,
            )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "maestro"),),
            phase="canonical testbench Maestro creation",
            allow_current_config_lock=True,
        ):
            create_oa_native_maestro_view(
                client,
                spec,
                operation=operation,
                timeout=timeout,
            )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "schematic"),),
            phase="finalize native testbench schematic extraction",
            allow_current_config_lock=True,
        ):
            check_and_save_schematic(
                client,
                spec.library,
                spec.cell,
                timeout=timeout,
                operation=operation,
            )
        missing = [
            view
            for view in _TESTBENCH_VIEWS
            if not cell_view_exists(client, spec.library, spec.cell, view)
        ]
        if missing:
            raise RuntimeError(
                f"testbench materialization left missing views: {missing}"
            )
