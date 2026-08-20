"""Standalone Xcelium AMS workflow used by active characterization and legacy runners."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sigilicon.ams.render import render_analog_netlist, render_reference_module, render_testbench
from sigilicon.ams.provenance import ams_fingerprint, ams_run_fingerprint
from sigilicon.ams.results import parse_results, render_truth_table, validate_truth_contract
from sigilicon.artifacts import ArtifactRecord, new_identity, read_nofollow_text
from sigilicon.ams.spec import AmsSpec
from sigilicon.domain.provenance import design_fingerprint
from sigilicon.external_tools import (
    ProcessGroupCleanupUncertainError,
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    process_group_cleanup_uncertainty,
    run_process_group,
)
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.operation_journal import (
    rollback_unreferenced_operation_incident,
    write_operation_incident,
)


@dataclass(frozen=True)
class StandaloneResult:
    namespace_dir: Path
    run_dir: Path
    xrun: Path
    source_netlist: Path
    truth_table: Path
    waveform: Path | None
    vectors: int
    run_fingerprint: str


def find_xrun(explicit: Path | None = None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise FileNotFoundError(f"xrun does not exist: {explicit}")
    discovered = shutil.which("xrun")
    if discovered:
        return Path(discovered).resolve()
    for variable in ("XCELIUM_HOME", "IUS_HOME"):
        value = os.environ.get(variable)
        if not value:
            continue
        home = Path(value)
        for candidate in (home / "tools" / "bin" / "xrun", home / "bin" / "xrun"):
            if candidate.is_file():
                return candidate.resolve()
    raise FileNotFoundError(
        "xrun was not found; load Xcelium, set XCELIUM_HOME, or pass --xrun"
    )


def _xcelium_home(xrun: Path) -> Path:
    resolved = xrun.resolve()
    configured = os.environ.get("XCELIUM_HOME") or os.environ.get("IUS_HOME")
    candidates = ([Path(configured)] if configured else []) + list(resolved.parents)
    for home in candidates:
        for launcher in (home / "tools" / "bin" / "xrun", home / "bin" / "xrun"):
            if launcher.is_file() and launcher.resolve() == resolved:
                return home
    raise RuntimeError(f"cannot determine Xcelium installation root from {xrun}")


def xrun_env(xrun: Path) -> dict[str, str]:
    env = cadence_subprocess_env()
    xcelium_home = _xcelium_home(xrun)
    path_entries = (xcelium_home / "tools" / "bin", xcelium_home / "bin")
    lib_entries = (
        xcelium_home / "tools" / "inca" / "lib",
        xcelium_home / "tools" / "tbsc" / "lib",
        xcelium_home / "tools" / "vic" / "lib" / "gnu",
        xcelium_home / "tools" / "systemc" / "lib",
        xcelium_home / "tools" / "lib",
        xcelium_home / "lib",
    )
    env["PATH"] = os.pathsep.join(map(str, path_entries)) + os.pathsep + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = (
        os.pathsep.join(map(str, lib_entries)) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    )
    env.setdefault("XCELIUM_HOME", str(xcelium_home))
    env.setdefault("IUS_HOME", str(xcelium_home))
    env.setdefault("CDS_INST_DIR", str(xcelium_home))
    return env


def _prepare_hdl_inputs(
    spec: AmsSpec,
    artifact: ArtifactRecord,
) -> tuple[Path, Path]:
    testbench = artifact.write_text(
        "inputs",
        (f"{spec.testbench}.sv",),
        render_testbench(
            spec,
            dut_cell=spec.wrapper_cell,
            include_supplies=False,
            dump_vcd=spec.simulation.backends.standalone.dump_vcd,
        ),
        label="rendered testbench",
    )
    reference = artifact.write_text(
        "inputs",
        (f"{spec.wrapper_cell}_ref.v",),
        render_reference_module(spec),
        label="rendered reference module",
    )
    return testbench, reference


def _simulation_log(archived_log: Path | None, stdout: str) -> str:
    if archived_log is not None:
        text = read_nofollow_text(archived_log, errors="replace")
        if "SUMMARY " in text:
            return text
    return stdout


def run_standalone(
    spec: AmsSpec,
    *,
    artifact_root: Path | None = None,
    xrun: Path | None = None,
    timeout: int = 600,
) -> StandaloneResult:
    """Generate, execute, and verify one standalone AMS simulation."""

    design = spec.design
    if not design.pdk.model_file.is_file():
        raise FileNotFoundError(
            f"PDK model file does not exist: {design.pdk.model_file}"
        )
    paths = ProjectContext.from_project_root(
        design.project_root,
        artifact_root=artifact_root,
    )
    xrun_bin = find_xrun(xrun)
    run_fingerprint = ams_run_fingerprint(spec, backend="standalone")
    run_id = new_identity()
    operation_id = new_identity()
    attempt = ArtifactRecord.begin(
        paths.artifacts.standalone_run(
            design.library,
            spec.testbench,
            run_id,
        ),
        entities={
            "library": design.library,
            "cell": design.cell,
            "testbench": spec.testbench,
        },
        operation="simulate",
        backend="standalone",
        source_fingerprint=design_fingerprint(design),
        setup_fingerprint=ams_fingerprint(spec),
        run_fingerprint=run_fingerprint,
    )
    attempt.bind_operation(operation_id)
    run_dir = attempt.paths.root
    namespace_dir = attempt.paths.namespace_root
    work_dir = attempt.paths.role("work")
    truth_table = attempt.path("results", "truth_table.csv")
    wave_path: Path | None = None
    try:
        testbench, reference = _prepare_hdl_inputs(spec, attempt)
        testbench.chmod(0o444)
        reference.chmod(0o444)
        with (
            owned_directory(work_dir) as owned_work,
            owned_input_file(testbench) as owned_testbench,
            owned_input_file(reference) as owned_reference,
            owned_input_file(
                design.pdk.model_file,
                require_single_link=False,
            ) as owned_model,
            owned_input_file(
                design.source_netlist,
                require_single_link=False,
            ) as owned_source,
        ):
            attempt.write_json(
                "inputs",
                ("external-input-references.json",),
                {
                    "pdk_model": {
                        "path": str(design.pdk.model_file),
                        "sha256": owned_model.sha256,
                    },
                    "source_netlist": {
                        "path": str(design.source_netlist),
                        "sha256": owned_source.sha256,
                    },
                },
                label="durable external input references",
            )
            canonical_analog = attempt.write_text(
                "inputs",
                ("analog.scs",),
                render_analog_netlist(
                    spec,
                    reference_file=str(reference),
                    model_file=str(design.pdk.model_file),
                    source_netlist=str(design.source_netlist),
                ),
                label="durable rendered analog input",
            )
            canonical_analog.chmod(0o444)
            analog = attempt.write_text(
                "work",
                ("analog.tool.scs",),
                render_analog_netlist(
                    spec,
                    reference_file=owned_reference.child_named_path,
                    model_file=owned_model.child_named_path,
                    source_netlist=owned_source.child_named_path,
                ),
                label="invocation-only exact-inode analog tool input",
            )
            analog.chmod(0o444)
            with owned_input_file(analog) as owned_analog:
                command = [
                    str(xrun_bin),
                    "-64bit",
                    "-sv",
                    "-adv_ms",
                    "-timescale",
                    "1ns/1ps",
                    "-analogsolver",
                    "spectre",
                    "-ieinfo",
                    "-ieinfo_log",
                    "ams_ieinfo.log",
                    owned_testbench.child_named_path,
                    owned_analog.child_named_path,
                ]

                def validate_spawn() -> None:
                    for owned_input in (
                        owned_testbench,
                        owned_reference,
                        owned_model,
                        owned_source,
                        owned_analog,
                    ):
                        owned_input.require_visible()

                completed = run_process_group(
                    command,
                    cwd=Path(owned_work.child_path),
                    env=xrun_env(xrun_bin),
                    timeout=timeout,
                    before_spawn=validate_spawn,
                    pass_fds=(
                        owned_work.fd,
                        owned_testbench.fd,
                        owned_testbench.directory_fd,
                        owned_reference.fd,
                        owned_reference.directory_fd,
                        owned_model.fd,
                        owned_model.directory_fd,
                        owned_source.fd,
                        owned_source.directory_fd,
                        owned_analog.fd,
                        owned_analog.directory_fd,
                    ),
                )
        stdout_log = attempt.write_text(
            "logs",
            ("xrun.stdout.log",),
            completed.stdout,
            label="Xcelium captured stdout",
        )
        native_log = attempt.path("work", "xrun.log")
        archived_log: Path | None = None
        if native_log.is_file():
            archived_log = attempt.copy_file(
                "logs",
                ("xrun.log",),
                native_log,
                label="Xcelium diagnostic log",
            )
        simulation_text = _simulation_log(archived_log, completed.stdout)
        rows, summary = parse_results(simulation_text)
        if rows:
            truth_table = attempt.write_text(
                "results",
                ("truth_table.csv",),
                render_truth_table(rows),
                label="verified truth table",
            )

        source_vcd = attempt.path("work", f"{spec.testbench}.vcd")
        if source_vcd.is_file():
            wave_path = attempt.copy_file(
                "results",
                ("waveforms", source_vcd.name),
                source_vcd,
                label="simulation waveform",
            )

        validate_truth_contract(spec, simulation_text, rows, summary)
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-60:])
            raise RuntimeError(
                f"xrun failed with exit code {completed.returncode}\n{tail}"
            )
        assert summary is not None
        attempt.add_file("work", work_dir, label="Xcelium native work directory")
        attempt.succeed(
            completion_evidence=(truth_table,),
            details={
                "variables": {},
                "xrun": str(xrun_bin),
                "vectors": summary["vectors"],
            },
        )
    except BaseException as exc:
        try:
            if attempt.status == "running":
                cleanup_reason = process_group_cleanup_uncertainty(exc)
                if cleanup_reason is not None:
                    reason = (
                        "standalone process-group cleanup could not be proven: "
                        + cleanup_reason
                    )
                    incident: Path | None = None
                    try:
                        incident = write_operation_incident(
                            workspace_root=paths.workspace_root,
                            artifact_root=paths.artifact_root,
                            operation_id=operation_id,
                            name="standalone-ams-simulation",
                            policy="isolated-process-group",
                            status="uncertain",
                            error=exc,
                            uncertain_reason=reason,
                            view_snapshots=(),
                            ownership_scopes=({"kind": "xrun-process-group"},),
                        )
                        attempt.attach_incident(incident)
                    except Exception as journal_error:
                        if incident is not None:
                            try:
                                rollback_unreferenced_operation_incident(
                                    artifact_root=paths.artifact_root,
                                    operation_id=operation_id,
                                    incident_path=incident,
                                )
                            except Exception as rollback_error:
                                exc.add_note(
                                    "could not safely roll back unreferenced "
                                    f"operation incident: {rollback_error}"
                                )
                        attempt.fail(
                            exc,
                            uncertain_reason=reason,
                            details={
                                "incident_recording_error": (
                                    f"{type(journal_error).__name__}: {journal_error}"
                                )[:4000]
                            },
                        )
                        exc.add_note(
                            f"could not record standalone safety incident: {journal_error}"
                        )
                    else:
                        attempt.fail(exc, uncertain_reason=reason)
                else:
                    attempt.fail(exc)
        except Exception as record_error:
            exc.add_note(f"could not record standalone run failure: {record_error}")
        raise
    return StandaloneResult(
        namespace_dir=namespace_dir,
        run_dir=run_dir,
        xrun=xrun_bin,
        source_netlist=design.source_netlist,
        truth_table=truth_table,
        waveform=wave_path,
        vectors=summary["vectors"],
        run_fingerprint=run_fingerprint,
    )
