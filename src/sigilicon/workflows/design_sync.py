"""Synchronize canonical design sources into generated OA state."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.artifacts import ArtifactRecord, new_identity
from sigilicon.domain.design import DesignSpec
from sigilicon.domain.netlist import (
    lower_subckt_default_parameters,
    materialize_netlist_snapshot,
)
from sigilicon.domain.provenance import design_identity_fingerprint
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.library import LibrarySyncResult, ensure_project_library
from sigilicon.virtuoso.importer import generate_symbol, import_schematic
from sigilicon.virtuoso.oa import (
    set_cell_port_directions,
    validate_cell_port_directions,
)
from sigilicon.virtuoso.provenance import oa_view_digest
from sigilicon.virtuoso.workspace import (
    OperationPolicy,
    workspace_operation,
)
from sigilicon.workflows.hierarchy_import import (
    HierarchyImportError,
    import_hierarchy,
    plan_hierarchy,
)
from sigilicon.workflows.source_control import inspect_source_state


@dataclass(frozen=True)
class DesignSyncResult:
    attempt_dir: Path | None
    manifest_path: Path | None
    library: LibrarySyncResult
    imported_cells: tuple[str, ...]


@dataclass(frozen=True)
class TargetOnlyDesignSyncResult:
    """Completed sync of exactly one existing OA-library cell.

    This deliberately does not reconcile the library itself or ``cds.lib``.
    It is for a design that is already registered in the active Virtuoso
    workspace and for which automation is authorized only for one cell.
    """

    attempt_dir: Path | None
    manifest_path: Path | None
    library_path: Path
    technology_library: str
    imported_cells: tuple[str, ...]


_STANDARD_SPICEIN_DEVICE_MAP = """\
devselect := resistor res
devselect := capacitor cap
"""


def _design_oa_view_digests(
    paths: ProjectContext,
    library: str,
    cells: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    return {
        cell: {
            view: oa_view_digest(
                paths.workspace_root / library / cell / view,
                allowed_symlink_root=paths.project_root,
            )
            for view in ("netlist", "schematic", "symbol")
        }
        for cell in cells
    }


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
        label="project-wide Spectre-to-analogLib device map",
    )


def sync_design(
    spec: DesignSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    disposable: bool = False,
) -> DesignSyncResult:
    """Make the OA library/schematic/symbol match the canonical design source."""

    if not disposable:
        return _sync_design_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            disposable=False,
        )
    with DisposableWork.create(prefix="sigilicon-oa-design-") as work:
        return _sync_design_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            disposable=True,
            _disposable_work=work,
        )


def _sync_design_impl(
    spec: DesignSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
) -> DesignSyncResult:
    """Run the shared design synchronizer under its caller-owned work scope."""

    paths = ProjectContext.from_project_root(spec.project_root)
    if artifact_root is not None:
        paths = ProjectContext.from_project_root(
            spec.project_root,
            artifact_root=artifact_root,
        )
    if disposable:
        if _disposable_work is None:
            raise RuntimeError("disposable design sync requires a work scope")
        attempt: Any = _disposable_work
    else:
        source_state = inspect_source_state(spec.project_root)
        attempt = ArtifactRecord.begin(
            paths.artifacts.execution(
                owner=spec.library,
                target=spec.cell,
                flow="design-sync",
                variant=spec.sync_mode,
                identity=new_identity(),
                artifact_kind="design_sync",
                identity_kind="attempt_id",
            ),
            entities={"library": spec.library, "cell": spec.cell},
            operation="sync-design",
            backend="virtuoso-oa",
            source_fingerprint=design_identity_fingerprint(spec),
        )
        attempt.write_json(
            "inputs",
            ("source-state.json",),
            source_state.as_dict(),
            label="source-state snapshot at OA design synchronization start",
        )
    hierarchy_plan = plan_hierarchy(spec.netlist_snapshot, top=spec.cell)
    device_map = _write_standard_spicein_device_map(attempt)
    ordered_cells = hierarchy_plan.ordered_cells
    imported: tuple[str, ...] = ()
    captured_partial_failure: dict[str, Any] | None = None
    current_stage = "workspace-enter"
    operation = None
    failure_context = (
        attempt.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=lambda: captured_partial_failure,
        )
        if not disposable
        else nullcontext()
    )
    with (
        failure_context,
        workspace_operation(
            client,
            paths.workspace_root,
            "sync-design",
            policy=OperationPolicy.RECURSIVE_OA,
        ) as operation,
    ):
        if not disposable:
            operation.register_artifact(attempt)

        def record_failure(error: BaseException) -> None:
            if attempt.status != "running":
                return
            partial = captured_partial_failure
            if (
                partial is None
                and isinstance(error, HierarchyImportError)
                and (error.completed or error.schematic_completed)
            ):
                partial = {
                    "completed_cells": list(error.completed),
                    "failed_cell": error.cell,
                    "failed_stage": error.stage,
                    "schematic_completed": list(error.schematic_completed),
                }
            if partial is None and imported:
                partial = {
                    "completed_cells": list(imported),
                    "failed_cell": spec.cell,
                    "failed_stage": current_stage,
                }
            attempt.fail(
                error,
                partial_failure=partial,
                uncertain_reason=operation.uncertain_reason,
            )

        def commit_sync() -> Path:
            nonlocal current_stage
            current_stage = "artifact-commit"
            oa_view_sha256 = _design_oa_view_digests(
                paths, spec.library, imported
            )
            completion = attempt.write_json(
                "outputs",
                ("completion.json",),
                {
                    "library": spec.library,
                    "cell": spec.cell,
                    "imported_cells": list(imported),
                    "source_fingerprint": design_identity_fingerprint(spec),
                    "oa_view_sha256": oa_view_sha256,
                    "oa_completion_confirmed": True,
                },
                label="OA synchronization completion proof",
            )
            return attempt.succeed(
                completion_evidence=(completion,),
                details={
                    "imported_cells": list(imported),
                    "oa_view_sha256": oa_view_sha256,
                },
            )

        deferred = (
            operation.defer_commit(commit_sync, on_failure=record_failure)
            if not disposable
            else None
        )
        current_stage = "ensure-library"
        with operation.mutation_scope(
            spec.library,
            cells=None,
            phase="ensure design library",
            expected_library_path=paths.workspace_root / spec.library,
            quarantine_root=attempt.directory("outputs", "stale-locks")
            if quarantine_stale_locks
            else None,
            require_view_lease=False,
        ):
            library = ensure_project_library(
                client,
                library=spec.library,
                path=paths.workspace_root / spec.library,
                technology_library=spec.pdk.oa.technology_library,
                cds_lib=paths.workspace_root / "cds.lib",
                operation=operation,
                timeout=timeout,
            )
        current_stage = "hierarchy-import"
        with operation.view_lease(
            spec.library,
            cells=ordered_cells,
            views=tuple(
                (cell, view)
                for cell in ordered_cells
                for view in ("netlist", "schematic", "symbol")
            ),
        ):
            try:
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
                )
            except HierarchyImportError as error:
                if error.completed or error.schematic_completed:
                    captured_partial_failure = {
                        "completed_cells": list(error.completed),
                        "failed_cell": error.cell,
                        "failed_stage": error.stage,
                        "schematic_completed": list(error.schematic_completed),
                    }
                raise
            current_stage = "port-directions"
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
                    fingerprint=design_identity_fingerprint(spec),
                    operation=operation,
                    timeout=timeout,
                )
            current_stage = "view-reconciliation"
        current_stage = "workspace-final-audit"
    if not disposable and (deferred is None or not deferred.completed):
        raise RuntimeError("design sync completed without committing its artifact")
    return DesignSyncResult(
        attempt_dir=attempt.paths.root if not disposable else None,
        manifest_path=attempt.paths.manifest if not disposable else None,
        library=library,
        imported_cells=imported,
    )


def sync_existing_design_target_only(
    spec: DesignSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    disposable: bool = False,
) -> TargetOnlyDesignSyncResult:
    """Synchronize one canonical single-subckt design into an existing library.

    Unlike :func:`sync_design`, this workflow never creates or reconciles a
    library, never writes ``cds.lib``, and never obtains a library-wide view
    lease.  It is intentionally generic: the same guarded bridge import,
    symbol generation, port-direction update, artifact lifecycle, and final
    Current OA parity checks are used for any single-cell design whose library already exists.
    """

    if not disposable:
        return _sync_existing_design_target_only_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            disposable=False,
        )
    with DisposableWork.create(prefix="sigilicon-oa-design-") as work:
        return _sync_existing_design_target_only_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            disposable=True,
            _disposable_work=work,
        )


def _sync_existing_design_target_only_impl(
    spec: DesignSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
) -> TargetOnlyDesignSyncResult:
    """Run target-only synchronization under its caller-owned work scope."""

    paths = ProjectContext.from_project_root(spec.project_root, artifact_root=artifact_root)
    plan = plan_hierarchy(spec.netlist_snapshot, top=spec.cell)
    if plan.ordered_cells != (spec.cell,):
        raise ValueError(
            "target-only design sync requires exactly one canonical subckt; "
            "use recursive sync for a multi-cell hierarchy"
        )
    if disposable:
        if _disposable_work is None:
            raise RuntimeError("disposable target-only sync requires a work scope")
        attempt: Any = _disposable_work
    else:
        source_state = inspect_source_state(spec.project_root)
        attempt = ArtifactRecord.begin(
            paths.artifacts.execution(
                owner=spec.library,
                target=spec.cell,
                flow="design-sync",
                variant=spec.sync_mode,
                identity=new_identity(),
                artifact_kind="design_sync",
                identity_kind="attempt_id",
            ),
            entities={"library": spec.library, "cell": spec.cell},
            operation="sync-existing-design-target-only",
            backend="virtuoso-oa",
            source_fingerprint=design_identity_fingerprint(spec),
        )
        attempt.write_json(
            "inputs",
            ("source-state.json",),
            source_state.as_dict(),
            label="source-state snapshot at target-only OA synchronization start",
        )
    operation = None
    device_map = _write_standard_spicein_device_map(attempt)
    imported: tuple[str, ...] = ()
    completed_stages: list[str] = []
    current_stage = "workspace-enter"
    library_path: Path | None = None
    technology_library = ""

    def partial_failure() -> dict[str, Any] | None:
        if not completed_stages:
            return None
        return {
            "completed_stages": list(completed_stages),
            "failed_cell": spec.cell,
            "failed_stage": current_stage,
        }

    failure_context = (
        attempt.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=partial_failure,
        )
        if not disposable
        else nullcontext()
    )
    with (
        failure_context,
        workspace_operation(
            client,
            paths.workspace_root,
            "sync-existing-design-target-only",
            policy=OperationPolicy.DIRECT_MUTATION,
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
        if not disposable:
            operation.register_artifact(attempt)
        current_stage = "verify-existing-library"
        visible_libraries = client.library.list(timeout=30)
        if spec.library not in visible_libraries:
            raise RuntimeError(
                f"target-only sync requires an existing library: {spec.library}"
            )
        info = client.library.get(spec.library, timeout=30)
        expected_library_path = paths.workspace_root / spec.library
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
            spec.path,
            label="canonical design specification",
        )
        canonical = materialize_netlist_snapshot(
            spec.netlist_snapshot,
            attempt.path("inputs", "canonical-netlist.scs"),
        )
        if not disposable:
            attempt.add_file(
                "inputs",
                canonical.path,
                label="immutable canonical transistor source",
            )
        spicein_snapshot = lower_subckt_default_parameters(
            spec.netlist_snapshot,
            spec.cell,
        )
        immutable = materialize_netlist_snapshot(
            spicein_snapshot,
            attempt.path("inputs", "spicein-netlist.scs"),
        )
        if not disposable:
            attempt.add_file(
                "inputs",
                immutable.path,
                label="mechanically default-elaborated spiceIn source",
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

        current_stage = "import-schematic"
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
            )
        completed_stages.append("schematic")
        imported = (spec.cell,)

        current_stage = "generate-symbol"
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
        completed_stages.append("symbol")

        current_stage = "set-port-directions"
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
                fingerprint=design_identity_fingerprint(spec),
                operation=operation,
                timeout=timeout,
            )
        completed_stages.append("port-directions")

        current_stage = "validate-generated-interface"
        validate_cell_port_directions(
            client,
            spec.library,
            spec.cell,
            spec.directions,
            fingerprint=design_identity_fingerprint(spec),
            operation=operation,
            timeout=timeout,
        )
        completed_stages.append("interface-validation")
        oa_view_sha256 = _design_oa_view_digests(
            paths, spec.library, (spec.cell,)
        )
        completion = attempt.write_json(
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
                "source_fingerprint": design_identity_fingerprint(spec),
                "spicein_source_sha256": spicein_snapshot.sha256,
                "oa_view_sha256": oa_view_sha256,
                "oa_completion_confirmed": True,
            },
            label="target-only OA synchronization completion proof",
        )

        def record_failure(error: BaseException) -> None:
            if attempt.status == "running":
                attempt.fail(
                    error,
                    partial_failure=partial_failure(),
                    uncertain_reason=operation.uncertain_reason,
                )

        if not disposable:
            operation.defer_commit(
                lambda: attempt.succeed(
                    completion_evidence=(completion,),
                    details={
                        "target_only": True,
                        "imported_cells": [spec.cell],
                        "library_path": str(library_path),
                        "technology_library": technology_library,
                        "oa_view_sha256": oa_view_sha256,
                    },
                ),
                on_failure=record_failure,
            )
        current_stage = "workspace-final-audit"

    if library_path is None:
        raise RuntimeError("target-only sync completed without a registered library path")
    return TargetOnlyDesignSyncResult(
        attempt_dir=attempt.paths.root if not disposable else None,
        manifest_path=attempt.paths.manifest if not disposable else None,
        library_path=library_path,
        technology_library=technology_library,
        imported_cells=imported or (spec.cell,),
    )
