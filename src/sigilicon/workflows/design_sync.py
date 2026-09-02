"""Synchronize canonical design sources into generated OA state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.domain.design import DesignSpec
from sigilicon.domain.netlist import (
    lower_subckt_default_parameters,
    materialize_netlist_snapshot,
)
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.library import LibrarySyncResult, ensure_project_library
from sigilicon.virtuoso.importer import generate_symbol, import_schematic
from sigilicon.virtuoso.oa import (
    set_cell_port_directions,
    validate_cell_port_directions,
)
from sigilicon.virtuoso.workspace import (
    OperationPolicy,
    workspace_operation,
)
from sigilicon.workflows.hierarchy_import import (
    import_hierarchy,
    plan_hierarchy,
)


@dataclass(frozen=True)
class DesignSyncResult:
    library: LibrarySyncResult
    imported_cells: tuple[str, ...]


@dataclass(frozen=True)
class TargetOnlyDesignSyncResult:
    """Completed sync of exactly one existing OA-library cell.

    This deliberately does not reconcile the library itself or ``cds.lib``.
    It is for a design that is already registered in the active Virtuoso
    workspace and for which automation is authorized only for one cell.
    """

    library_path: Path
    technology_library: str
    imported_cells: tuple[str, ...]


_STANDARD_SPICEIN_DEVICE_MAP = """\
devselect := resistor res
devselect := capacitor cap
"""


def _write_standard_spicein_device_map(attempt: Any) -> Path:
    """Materialize the project-wide mapping for Spectre ideal passives.

    Canonical sources use Spectre primitive names while the generated OA
    schematics use the corresponding analogLib cells.  Keeping the mapping in
    the shared synchronization workflow prevents individual designs from
    growing private SpiceIn exceptions.
    """

    return attempt.write_text(
        "inputs",
        ("spiceIn.devmap",),
        _STANDARD_SPICEIN_DEVICE_MAP,
    )


def sync_design(
    spec: DesignSpec,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    resources: Any,
) -> DesignSyncResult:
    """Make the OA library/schematic/symbol match the canonical design source."""

    with DisposableWork.create(prefix="sigilicon-oa-design-") as work:
        return _sync_design_impl(
            spec,
            client,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            work=work,
            operation_id=operation_id,
            bind_operation=bind_operation,
            resources=resources,
        )


def _sync_design_impl(
    spec: DesignSpec,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    work: DisposableWork,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    resources: Any,
) -> DesignSyncResult:
    """Run the shared design synchronizer under its caller-owned work scope."""

    project = spec.project
    attempt: Any = work
    hierarchy_plan = plan_hierarchy(spec.netlist_snapshot, top=spec.cell)
    device_map = _write_standard_spicein_device_map(attempt)
    ordered_cells = hierarchy_plan.ordered_cells
    imported: tuple[str, ...] = ()
    with workspace_operation(
        client,
        project.workspace_root,
        "sync-design",
        policy=OperationPolicy.RECURSIVE_OA,
        operation_id=operation_id,
    ) as operation:
        if callable(bind_operation):
            bind_operation(operation)
        with operation.mutation_scope(
            spec.library,
            cells=None,
            phase="ensure design library",
            expected_library_path=project.workspace_root / spec.library,
            quarantine_root=attempt.directory("outputs", "stale-locks")
            if quarantine_stale_locks
            else None,
            require_view_lease=False,
        ):
            library = ensure_project_library(
                client,
                library=spec.library,
                path=project.workspace_root / spec.library,
                technology_library=spec.pdk.oa.technology_library,
                cds_lib=project.workspace_root / "cds.lib",
                operation=operation,
                timeout=timeout,
            )
        with operation.view_lease(
            spec.library,
            cells=ordered_cells,
            views=tuple(
                (cell, view)
                for cell in ordered_cells
                for view in ("netlist", "schematic", "symbol")
            ),
        ):
            imported = import_hierarchy(
                client,
                plan=hierarchy_plan,
                library=spec.library,
                reference_libraries=spec.pdk.oa.reference_libraries,
                dev_map_file=device_map,
                overwrite=overwrite,
                artifact=attempt,
                source_role="inputs",
                work_role="work",
                timeout=timeout,
                operation=operation,
                resources=resources,
            )
            with operation.mutation_scope(
                spec.library,
                cells=(spec.cell,),
                views=((spec.cell, "schematic"), (spec.cell, "symbol")),
                phase="set design port directions",
            ):
                set_cell_port_directions(
                    client,
                    spec.library,
                    spec.cell,
                    spec.directions,
                    operation=operation,
                    timeout=timeout,
                )
    return DesignSyncResult(
        library=library,
        imported_cells=imported,
    )


def sync_existing_design_target_only(
    spec: DesignSpec,
    client: Any,
    *,
    design_source: Path,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    resources: Any,
) -> TargetOnlyDesignSyncResult:
    """Synchronize one canonical single-subckt design into an existing library.

    Unlike :func:`sync_design`, this workflow never creates or reconciles a
    library, never writes ``cds.lib``, and never obtains a library-wide view
    lease.  It is intentionally generic: the same guarded bridge import,
    symbol generation, port-direction update, artifact lifecycle, and final
    Current OA parity checks are used for any single-cell design whose library already exists.
    """

    with DisposableWork.create(prefix="sigilicon-oa-design-") as work:
        return _sync_existing_design_target_only_impl(
            spec,
            client,
            design_source=design_source,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            work=work,
            operation_id=operation_id,
            bind_operation=bind_operation,
            resources=resources,
        )


def _sync_existing_design_target_only_impl(
    spec: DesignSpec,
    client: Any,
    *,
    design_source: Path,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    work: DisposableWork,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    resources: Any,
) -> TargetOnlyDesignSyncResult:
    """Run target-only synchronization under its caller-owned work scope."""

    project = spec.project
    plan = plan_hierarchy(spec.netlist_snapshot, top=spec.cell)
    if plan.ordered_cells != (spec.cell,):
        raise ValueError(
            "target-only design sync requires exactly one canonical subckt; "
            "use recursive sync for a multi-cell hierarchy"
        )
    attempt: Any = work
    device_map = _write_standard_spicein_device_map(attempt)
    imported: tuple[str, ...] = ()
    library_path: Path | None = None
    technology_library = ""

    with (
        workspace_operation(
            client,
            project.workspace_root,
            "sync-existing-design-target-only",
            policy=OperationPolicy.DIRECT_MUTATION,
            operation_id=operation_id,
        ) as operation,
        operation.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=tuple(
                (spec.cell, view)
                for view in ("netlist", "schematic", "symbol")
            ),
        ),
    ):
        if callable(bind_operation):
            bind_operation(operation)
        visible_libraries = client.library.list(timeout=30)
        if spec.library not in visible_libraries:
            raise RuntimeError(
                f"target-only sync requires an existing library: {spec.library}"
            )
        info = client.library.get(spec.library, timeout=30)
        expected_library_path = project.workspace_root / spec.library
        library_path = operation.require_project_library_target(client, spec.library)
        if library_path != expected_library_path:
            raise RuntimeError(
                f"library {spec.library} resolves to {library_path}, "
                f"expected {expected_library_path}"
            )
        technology_library = str(info.technology_library or "")
        if technology_library != spec.pdk.oa.technology_library:
            raise RuntimeError(
                f"library {spec.library} uses technology {technology_library or None}, "
                f"expected {spec.pdk.oa.technology_library}"
            )

        attempt.copy_file(
            "inputs",
            ("design.toml",),
            design_source,
        )
        materialize_netlist_snapshot(
            spec.netlist_snapshot,
            attempt.path("inputs", "canonical-netlist.scs"),
        )
        spicein_snapshot = lower_subckt_default_parameters(
            spec.netlist_snapshot,
            spec.cell,
        )
        immutable = materialize_netlist_snapshot(
            spicein_snapshot,
            attempt.path("inputs", "spicein-netlist.scs"),
        )
        references = tuple(
            dict.fromkeys(
                (
                    spec.library,
                    *spec.pdk.oa.reference_libraries,
                    "analogLib",
                    "basic",
                )
            )
        )
        work_dir = attempt.directory("work", spec.cell)

        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "netlist"), (spec.cell, "schematic")),
            phase=f"target-only schematic import {spec.library}/{spec.cell}",
            quarantine_root=attempt.directory("outputs", "stale-locks")
            if quarantine_stale_locks
            else None,
        ):
            import_schematic(
                client,
                spec.library,
                spec.cell,
                immutable.path,
                own_netlist=immutable.open_fd,
                reference_libraries=references,
                dev_map_file=device_map,
                overwrite=overwrite,
                run_dir=work_dir,
                timeout=timeout,
                operation=operation,
                resources=resources,
            )
        imported = (spec.cell,)

        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "symbol"),),
            phase=f"target-only symbol generation {spec.library}/{spec.cell}",
        ):
            generate_symbol(
                client,
                spec.library,
                spec.cell,
                sort_pins="geometric",
                overwrite=overwrite,
                timeout=timeout,
                operation=operation,
            )
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "schematic"), (spec.cell, "symbol")),
            phase=f"target-only port update {spec.library}/{spec.cell}",
        ):
            set_cell_port_directions(
                client,
                spec.library,
                spec.cell,
                spec.directions,
                operation=operation,
                timeout=timeout,
            )
        validate_cell_port_directions(
            client,
            spec.library,
            spec.cell,
            spec.directions,
            operation=operation,
            timeout=timeout,
        )
        attempt.write_json(
            "outputs",
            ("completion.json",),
            {
                "library": spec.library,
                "cell": spec.cell,
                "library_path": str(library_path),
                "technology_library": technology_library,
                "imported_cells": [spec.cell],
                "target_only": True,
                "cds_lib_modified": False,
                "oa_completion_confirmed": True,
            },
        )

    if library_path is None:
        raise RuntimeError("target-only sync completed without a registered library path")
    return TargetOnlyDesignSyncResult(
        library_path=library_path,
        technology_library=technology_library,
        imported_cells=imported or (spec.cell,),
    )
