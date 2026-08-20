"""Legacy OA config/Maestro workflow for the old declarative AMS interface."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any

from sigilicon.artifacts import new_identity
from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.ams.load_contract import validate_load_attestation
from sigilicon.ams.render import render_load_wrapper_netlist, render_testbench
from sigilicon.ams.artifacts import AdeSetupAttempt, file_sha256
from sigilicon.ams.spec import AmsSpec
from sigilicon.external_tools import owned_directory
from sigilicon.paths import ProjectContext
from sigilicon.domain.provenance import design_fingerprint
from sigilicon.virtuoso.ade import create_config_view, create_maestro_view
from sigilicon.virtuoso.environment import sanitize_virtuoso_license_env
from sigilicon.virtuoso.oa import (
    cell_exists,
    set_cell_port_directions,
    validate_cell_port_directions,
)
from sigilicon.virtuoso.legacy_ade import (
    capture_ade_component,
    read_oa_load_instances,
    validate_ade_component_views,
)
from sigilicon.virtuoso.systemverilog import import_systemverilog_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.hierarchy_import import (
    HierarchyImportError,
    import_hierarchy,
    plan_hierarchy,
)


@dataclass(frozen=True)
class AdeSetupResult:
    namespace_dir: Path
    setup_dir: Path
    manifest_path: Path
    systemverilog_source: Path
    updated_testbench: bool


def _create_systemverilog_view(
    client: Any,
    spec: AmsSpec,
    attempt: AdeSetupAttempt,
    *,
    operation: Any,
    overwrite: bool,
) -> Path:
    design = spec.design
    if operation.client is not client:
        raise RuntimeError("SystemVerilog view creation uses a different bridge client")
    source_dir = attempt.directory("inputs", "systemverilog")
    source = attempt.path("inputs", "systemverilog", f"{spec.testbench}.sv")
    payload = render_testbench(
        spec,
        dut_cell=spec.wrapper_cell,
        include_supplies=False,
        dump_vcd=False,
    ).encode("utf-8")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    with owned_directory(source_dir, create_missing=True) as owned_source_dir:
        descriptor = os.open(
            source.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o444,
            dir_fd=owned_source_dir.fd,
        )
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise RuntimeError("could not materialize SystemVerilog source")
                remaining = remaining[written:]
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            os.fsync(owned_source_dir.fd)
        finally:
            os.close(descriptor)
    import_systemverilog_view(
        client,
        library=design.library,
        cell=spec.testbench,
        source=source,
        source_sha256=source_sha256,
        log_dir=attempt.directory("logs", "systemverilog"),
        work_dir=attempt.directory("work", "systemverilog"),
        operation=operation,
        overwrite=overwrite,
    )
    attempt.record.add_file("inputs", source, label="generated ADE SystemVerilog")
    attempt.record.add_file(
        "logs",
        attempt.record.paths.role("logs"),
        label="ADE setup diagnostic logs",
    )
    return source


def setup_ade(
    spec: AmsSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    quarantine_stale_locks: bool = False,
) -> AdeSetupResult:
    """Create generated SystemVerilog, config, and Maestro views in OA."""

    design = spec.design
    model = design.pdk.simulation.default
    if not model.file.is_file():
        raise FileNotFoundError(
            f"PDK model file does not exist: {model.file}"
        )
    setup_fingerprint = ams_fingerprint(spec)
    model_file_sha256 = file_sha256(model.file)
    paths = ProjectContext.from_project_root(
        design.project_root,
        artifact_root=artifact_root,
    )
    namespace = paths.artifacts.ade(design.library, spec.testbench)
    attempt = AdeSetupAttempt.begin(
        namespace,
        setup_fingerprint,
        attempt_id=new_identity(),
        entities={
            "library": design.library,
            "cell": design.cell,
            "testbench": spec.testbench,
        },
        operation="setup-ams-ade",
        backend="virtuoso-ade",
        source_fingerprint=design_fingerprint(design),
    )
    deferred_commit = None
    prepared_load_attestation: dict[str, Any] | None = None
    setup_partial_failure: dict[str, Any] | None = None
    current_stage = "workspace-enter"
    operation = None

    def partial_provenance() -> dict[str, Any] | None:
        if setup_partial_failure is not None:
            return setup_partial_failure
        registered = {
            reference["path"]
            for reference in attempt.record.manifest["files"]["evidence"]
        }
        completed = [
            name
            for name in ("load", "systemverilog", "config", "maestro")
            if f"evidence/components/{name}.json" in registered
        ]
        if not completed:
            return None
        return {
            "completed_components": completed,
            "failed_stage": current_stage,
        }

    with (
        attempt.record.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=partial_provenance,
        ),
        workspace_operation(
            client,
            paths.workspace_root,
            "setup-ams-ade",
            policy=OperationPolicy.RECURSIVE_OA,
        ) as operation,
        operation.view_lease(design.library) as view_lease,
    ):
        operation.register_artifact(attempt.record)

        def commit_validated_setup() -> Path:
            def validate_components(components: Any) -> None:
                maestro = components["maestro"]
                if (
                    maestro.get("model_file") != str(model.file)
                    or maestro.get("model_section") != model.single_section
                    or maestro.get("model_file_sha256") != model_file_sha256
                    or file_sha256(model.file) != model_file_sha256
                ):
                    raise RuntimeError(
                        "ADE PDK model no longer matches the setup fingerprint"
                    )
                validate_ade_component_views(
                    client,
                    paths.workspace_root,
                    design.library,
                    spec.testbench,
                    spec.wrapper_cell,
                    components,
                )
                if prepared_load_attestation is None:
                    raise RuntimeError(
                        "ADE load semantics were not prepared before setup commit"
                    )
                if components["load"].get("oa_load_attestation") != prepared_load_attestation:
                    raise RuntimeError(
                        "ADE OA load semantics changed before setup commit"
                    )

            return attempt.commit(validate_components=validate_components)

        deferred_commit = operation.defer_commit(
            commit_validated_setup,
            on_failure=lambda error: attempt.record_failure(
                error,
                uncertain_reason=getattr(operation, "uncertain_reason", None),
                partial_failure=partial_provenance(),
            ),
        )
        current_stage = "setup-preflight"
        sanitize_virtuoso_license_env(
            client,
            library=design.library,
            operation=operation,
        )
        if not cell_exists(client, design.library, design.cell):
            raise RuntimeError(
                f"missing required DUT {design.library}/{design.cell}; run sync-design first"
            )
        validate_cell_port_directions(
            client,
            design.library,
            design.cell,
            design.directions,
            fingerprint=design_fingerprint(design),
            operation=operation,
        )
        view_lease.checkpoint("DUT interface validation")
        replaced = cell_exists(client, design.library, spec.testbench)
        wrapper_replaced = cell_exists(client, design.library, spec.wrapper_cell)
        if replaced or wrapper_replaced:
            if not overwrite:
                raise RuntimeError(
                    "generated ADE cells already exist; "
                    "pass --overwrite to recreate it"
                )
        for cell in (spec.testbench, spec.wrapper_cell):
            with operation.mutation_scope(
                design.library,
                cells=(cell,),
                phase=f"ADE target preflight {design.library}/{cell}",
                quarantine_root=attempt.directory("evidence", "stale-locks")
                if quarantine_stale_locks
                else None,
                allow_current_config_lock=True,
            ):
                pass
        try:
            current_stage = "setup-components"
            wrapper_source = attempt.record.write_text(
                "inputs",
                (f"{spec.wrapper_cell}.scs",),
                render_load_wrapper_netlist(spec),
                label="ADE load wrapper source",
            )
            device_map = attempt.record.write_text(
                "inputs",
                ("spiceIn.devmap",),
                "devselect := capacitor cap\n",
                label="spiceIn device map",
            )
            wrapper_plan = plan_hierarchy(
                wrapper_source,
                top=spec.wrapper_cell,
            )
            import_hierarchy(
                client,
                plan=wrapper_plan,
                library=design.library,
                reference_libraries=design.pdk.oa.reference_libraries,
                dev_map_file=device_map,
                overwrite=overwrite,
                artifact=attempt.record,
                source_role="inputs",
                work_role="work",
                timeout=300,
                operation=operation,
            )
            view_lease.checkpoint("load-wrapper import")
            wrapper_directions = {
                name: design.directions[name]
                for name in design.inputs + design.outputs
            }
            with operation.mutation_scope(
                design.library,
                cells=(spec.wrapper_cell,),
                phase="load-wrapper port update",
            ):
                set_cell_port_directions(
                    client,
                    design.library,
                    spec.wrapper_cell,
                    wrapper_directions,
                    fingerprint=setup_fingerprint,
                    operation=operation,
                )
            view_lease.checkpoint("load-wrapper port update")
            load_attestation = validate_load_attestation(
                design.outputs,
                spec.simulation.interface.load_cap,
                read_oa_load_instances(
                    client,
                    design.library,
                    spec.wrapper_cell,
                    operation=operation,
                ),
            )
            view_lease.checkpoint("load-wrapper attestation")
            attempt.record_component(
                "load",
                **capture_ade_component(
                    paths.workspace_root,
                    design.library,
                    spec.wrapper_cell,
                    "load",
                ),
                source=wrapper_source.relative_to(attempt.setup_dir).as_posix(),
                source_sha256=file_sha256(wrapper_source),
                device_map=device_map.relative_to(attempt.setup_dir).as_posix(),
                device_map_sha256=file_sha256(device_map),
                load_cap=spec.simulation.interface.load_cap,
                oa_load_attestation=load_attestation,
            )
            with operation.mutation_scope(
                design.library,
                cells=(spec.testbench,),
                phase="SystemVerilog view creation",
            ):
                source = _create_systemverilog_view(
                    client,
                    spec,
                    attempt,
                    operation=operation,
                    overwrite=overwrite,
                )
            view_lease.checkpoint("SystemVerilog view creation")
            attempt.record_component(
                "systemverilog",
                **capture_ade_component(
                    paths.workspace_root,
                    design.library,
                    spec.testbench,
                    "systemverilog",
                ),
                source=source.relative_to(attempt.setup_dir).as_posix(),
                source_sha256=file_sha256(source),
            )
            with operation.mutation_scope(
                design.library,
                cells=(spec.testbench,),
                phase="config view creation",
                allow_current_config_lock=True,
            ):
                create_config_view(
                    client,
                    library=design.library,
                    testbench=spec.testbench,
                    dut=spec.wrapper_cell,
                    reference_libraries=design.pdk.oa.reference_libraries,
                    operation=operation,
                )
            view_lease.checkpoint("config view creation")
            attempt.record_component(
                "config",
                **capture_ade_component(
                    paths.workspace_root,
                    design.library,
                    spec.testbench,
                    "config",
                ),
            )
            with operation.mutation_scope(
                design.library,
                cells=(spec.testbench,),
                phase="Maestro view creation",
                allow_current_config_lock=True,
            ):
                create_maestro_view(
                    client,
                    library=design.library,
                    testbench=spec.testbench,
                    signals=design.inputs + design.outputs,
                    stop=spec.simulation.timing.stop,
                    maxstep=spec.simulation.timing.maxstep,
                    errpreset=spec.simulation.backends.ade.errpreset,
                    model_file=model.file,
                    model_section=model.single_section,
                    vdd=spec.simulation.interface.vdd,
                    connect_rules=spec.simulation.interface.connect_rules,
                    rise_time=spec.simulation.interface.rise_time,
                    vthi=spec.simulation.interface.vthi,
                    vtlo=spec.simulation.interface.vtlo,
                    operation=operation,
                )
            view_lease.checkpoint("Maestro view creation")
            attempt.record_component(
                "maestro",
                **capture_ade_component(
                    paths.workspace_root,
                    design.library,
                    spec.testbench,
                    "maestro",
                ),
                model_file=str(model.file),
                model_file_sha256=model_file_sha256,
                model_section=model.single_section,
            )
            prepared_load_attestation = validate_load_attestation(
                design.outputs,
                spec.simulation.interface.load_cap,
                read_oa_load_instances(
                    client,
                    design.library,
                    spec.wrapper_cell,
                    operation=operation,
                ),
            )
            if prepared_load_attestation != load_attestation:
                raise RuntimeError(
                    "ADE OA load semantics changed during setup preparation"
                )
            view_lease.checkpoint("final setup preparation")
            current_stage = "workspace-exit-audit"
        except BaseException as exc:
            completed_components = [
                name
                for name in ("load", "systemverilog", "config", "maestro")
                if attempt.path("evidence", "components", f"{name}.json").is_file()
            ]
            if completed_components:
                setup_partial_failure = {
                    "completed_components": completed_components,
                    "failed_stage": getattr(exc, "stage", type(exc).__name__),
                }
            elif isinstance(exc, HierarchyImportError) and exc.schematic_completed:
                setup_partial_failure = {
                    "completed_components": [],
                    "completed_cells": list(exc.completed),
                    "schematic_completed": list(exc.schematic_completed),
                    "failed_cell": exc.cell,
                    "failed_stage": exc.stage,
                }
            raise
    assert deferred_commit is not None and deferred_commit.completed
    manifest = deferred_commit.result
    return AdeSetupResult(
        namespace_dir=namespace.root,
        setup_dir=attempt.setup_dir,
        manifest_path=manifest,
        systemverilog_source=source,
        updated_testbench=replaced,
    )
