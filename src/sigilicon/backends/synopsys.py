"""Direct Synopsys process backends with one owner-script boundary.

The backend owns process safety, runtime resources, and artifact collection.
The selected owner owns the small VCS/DC/FC/HSPICE launcher scripts and their
tool-specific inputs.  No registration module or generic command executor is
involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from typing import Any

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
)
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    PreflightCheck,
    Resources,
    Step,
    StepContext,
    StepResult,
)
from sigilicon.external_tools import (
    owned_directory,
    owned_executable,
    owned_input_file,
    owned_output_file,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
    run_process_group_capture,
)


_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ENVIRONMENT_PREFIX = re.compile(r"[A-Z][A-Z0-9_]*_\Z")
_TARGET = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


def _text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise ContractError(f"Synopsys step requires non-empty {name!r}")
    return value


def _positive_integer(config: Mapping[str, Any], name: str) -> int:
    value = config.get(name)
    if type(value) is not int or value <= 0:
        raise ContractError(f"Synopsys step requires positive integer {name!r}")
    return value


def _boolean(config: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise ContractError(f"Synopsys step {name!r} must be boolean")
    return value


def _target(config: Mapping[str, Any]) -> str:
    value = config.get("target")
    if isinstance(value, str) and _TARGET.fullmatch(value) is not None:
        return value
    source = _text(config, "target_from")
    target = _text(config, source)
    if _TARGET.fullmatch(target) is None:
        raise ContractError(f"invalid Synopsys runner target {target!r}")
    return target


def _strings(config: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = config.get(name, ())
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ContractError(f"Synopsys step {name!r} must be a text array")
    return value


def _mapping(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, Mapping):
        raise ContractError(f"Synopsys step {name!r} must be a table")
    return value


def _safe_relative(value: str, label: str) -> str:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError(f"{label} must be a canonical relative path")
    return value


def _source_members(
    step: Step,
    root_name: str,
    *,
    suffix: str,
) -> tuple[str, ...]:
    root = _safe_relative(_text(step.config, root_name), root_name)
    prefix = f"{root}/"
    return tuple(
        source
        for source in step.sources
        if source.startswith(prefix) and source.endswith(suffix)
    )


def _runner(step: Step) -> str:
    runner = _safe_relative(_text(step.config, "runner"), "runner")
    if runner not in step.sources:
        raise ContractError("Synopsys runner must be inside the step source closure")
    return runner


def _environment_path_check(
    resources: Resources,
    name: str,
    *,
    directory: bool = False,
    executable: bool = False,
) -> PreflightCheck:
    value = resources.environment.get(name)
    path = None if not value else Path(value)
    ready = bool(
        path is not None
        and ((path.is_dir() if directory else path.is_file()))
        and (not executable or os.access(path, os.X_OK))
    )
    kind = "directory" if directory else "executable" if executable else "file"
    return PreflightCheck(
        "runtime-resource",
        name,
        "ready" if ready else "blocked",
        f"{kind} supplied by the invoking environment" if ready else f"missing {kind}",
    )


def _base_checks(step: Step) -> list[PreflightCheck]:
    runner = _runner(step)
    _positive_integer(step.config, "timeout_seconds")
    return [PreflightCheck("owner-runner", runner, "ready", "sealed plan source")]


def _write_filelist(context: StepContext, name: str, sources: tuple[str, ...]) -> Path:
    if not sources:
        raise ExecutionError(f"managed {name} source set is empty")
    path = context.work_root / f"{name}.f"
    path.write_text(
        "".join(f"{context.source_path(source)}\n" for source in sources),
        encoding="utf-8",
    )
    return path


def _logs(context: StepContext, stdout: str, stderr: str) -> tuple[Artifact, ...]:
    values = (("stdout.log", stdout), ("stderr.log", stderr))
    return tuple(
        Artifact("log", "log.synopsys", context.write_text("log", name, value))
        for name, value in values
    )


def _copied(
    context: StepContext,
    *,
    role: str,
    kind: str,
    source: Path,
    filename: str,
) -> Artifact:
    if not source.is_file() or source.is_symlink():
        raise ExecutionError(f"Synopsys tool omitted required {role!r} output")
    destination = context.output_path(role, _safe_relative(filename, f"{role} output"))
    copy_immutable_file(source, destination)
    return Artifact(role, kind, destination)


def _tree_artifacts(root: Path, role: str, kind: str) -> tuple[Artifact, ...]:
    if not root.is_dir() or root.is_symlink():
        raise ExecutionError(f"Synopsys tool omitted required {role!r} directory")
    paths = tuple(
        path.absolute()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )
    if not paths:
        raise ExecutionError(f"Synopsys tool produced an empty {role!r} directory")
    return tuple(Artifact(role, kind, path) for path in paths)


def _artifact(context: StepContext, dependency: str, role: str) -> Artifact:
    artifacts = context.artifacts(dependency, role)
    if len(artifacts) != 1:
        raise ExecutionError(
            f"dependency {dependency!r} must publish exactly one {role!r} artifact"
        )
    return artifacts[0]


def _run_script(
    context: StepContext,
    environment: dict[str, str],
    *,
    argument: str,
    held_executables: tuple[str, ...],
    held_files: tuple[str, ...],
    held_directories: tuple[str, ...] = (),
):
    """Run one source-pinned script while holding every external input path."""

    runner = _runner(context.step)
    with ExitStack() as stack:
        source_root = stack.enter_context(owned_directory(context.source_root))
        work_root = stack.enter_context(owned_directory(context.work_root))
        for source in context.step.sources:
            stack.enter_context(
                owned_input_file(
                    context.source_path(source),
                    require_single_link=False,
                )
            )
        for name, value in tuple(environment.items()):
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(context.source_root):
                relative = path.relative_to(context.source_root)
                environment[name] = (
                    source_root.child_path
                    if relative == Path(".")
                    else f"{source_root.child_path}/{relative.as_posix()}"
                )
        executables = []
        launchers = []
        for name in held_executables:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            owned = stack.enter_context(owned_executable(Path(value)))
            if owned.directory is None:
                environment[name] = owned.target.child_named_path
            else:
                launcher_name = f".{name.lower()}.launcher"
                launcher_path = context.work_root / launcher_name
                payload = (
                    "#!/bin/sh\nexec "
                    + shlex.join(owned.command)
                    + ' "$@"\n'
                ).encode("utf-8")
                with owned_output_file(
                    work_root,
                    launcher_name,
                    mode=0o700,
                ) as generated:
                    generated.write_bytes(payload)
                    expected = os.fstat(generated.fd)
                launcher = stack.enter_context(
                    owned_input_file(launcher_path, require_single_link=True)
                )
                held = os.fstat(launcher.fd)
                if (
                    held.st_dev,
                    held.st_ino,
                    held.st_mode,
                    held.st_size,
                    held.st_mtime_ns,
                    os.pread(launcher.fd, len(payload) + 1, 0),
                ) != (
                    expected.st_dev,
                    expected.st_ino,
                    expected.st_mode,
                    expected.st_size,
                    expected.st_mtime_ns,
                    payload,
                ):
                    raise ExecutionError("generated Synopsys launcher identity changed")
                environment[name] = launcher.child_named_path
                launchers.append(launcher)
            executables.append(owned)
        for name in held_files:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            owned = stack.enter_context(
                owned_input_file(Path(value), require_single_link=False)
            )
            if name == "SIGILICON_PYTHON":
                running = os.stat("/proc/self/exe")
                held = os.fstat(owned.fd)
                if (running.st_dev, running.st_ino) != (held.st_dev, held.st_ino):
                    raise ExecutionError(
                        "configured Python interpreter is not the running trusted runtime"
                    )
            environment[name] = owned.child_named_path
        for name in held_directories:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            owned = stack.enter_context(owned_directory(Path(value)))
            environment[name] = owned.child_path
        command = [f"{source_root.child_path}/{runner}"]
        if argument:
            command.append(argument)

        def visible() -> None:
            source_root.require_visible()
            work_root.require_visible()
            for executable in executables:
                executable.require_visible()
            for launcher in launchers:
                launcher.require_visible()
        return run_process_group_capture(
            command,
            cwd=context.work_root,
            env=environment,
            timeout=_positive_integer(context.step.config, "timeout_seconds"),
            before_spawn=visible,
        )


class VcsBackend:
    name = "synopsys.vcs"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        target = _target(step.config)
        _source_members(step, "rtl_root", suffix=".sv")
        _source_members(step, "testbench_root", suffix=".sv")
        checks.append(
            _environment_path_check(
                resources, "SIGILICON_SYNOPSYS_VCS", executable=True
            )
        )
        if target in {"structural", "gate"}:
            checks.extend(
                _environment_path_check(resources, name)
                for name in (
                    "SIGILICON_STDCELL_RVT_VERILOG",
                    "SIGILICON_STDCELL_HVT_VERILOG",
                    "SIGILICON_STDCELL_LVT_VERILOG",
                )
            )
        return tuple(checks)

    def run(self, context: StepContext) -> StepResult:
        config = context.step.config
        target = _target(config)
        environment = dict(context.resources.environment)
        environment["SIGILICON_DESIGN_VARIANT"] = _text(config, "variant")
        rtl = _source_members(context.step, "rtl_root", suffix=".sv")
        testbench = _source_members(context.step, "testbench_root", suffix=".sv")
        if target != "gate":
            environment["SIGILICON_VCS_RTL_FILELIST"] = str(
                _write_filelist(context, "rtl", rtl)
            )
        if target != "structural":
            environment["SIGILICON_VCS_TESTBENCH_FILELIST"] = str(
                _write_filelist(context, "testbench", testbench)
            )
        held: list[str] = []
        if target in {"structural", "gate"}:
            held.extend(
                (
                    "SIGILICON_STDCELL_RVT_VERILOG",
                    "SIGILICON_STDCELL_HVT_VERILOG",
                    "SIGILICON_STDCELL_LVT_VERILOG",
                )
            )
        if target == "gate":
            artifact = _artifact(context, _text(config, "synthesis_step"), "mapped-netlist")
            environment["SIGILICON_VCS_MAPPED_NETLIST"] = str(artifact.path)
            held.append("SIGILICON_VCS_MAPPED_NETLIST")
        with owned_scratch_directory(
            prefix=f"sigilicon-vcs-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_VCS_OUTPUT_ROOT"] = scratch.child_path
            completed = _run_script(
                context,
                environment,
                argument=target,
                held_executables=("SIGILICON_SYNOPSYS_VCS",),
                held_files=tuple(held),
            )
        logs = _logs(context, completed.stdout, completed.stderr or "")
        if completed.returncode:
            return StepResult(
                "failed", logs, message=f"VCS runner exited {completed.returncode}"
            )
        return StepResult.succeeded(artifacts=logs)


class DcBackend:
    name = "synopsys.dc"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        constraints = _safe_relative(_text(step.config, "constraints"), "constraints")
        if constraints not in step.sources:
            raise ContractError("DC constraints must be inside the step source closure")
        _source_members(step, "rtl_root", suffix=".sv")
        checks.extend(
            _environment_path_check(resources, name, executable=name.endswith("DC_SHELL"))
            for name in (
                "SIGILICON_SYNOPSYS_DC_SHELL",
                "SIGILICON_STDCELL_RVT_DB",
                "SIGILICON_STDCELL_HVT_DB",
                "SIGILICON_STDCELL_LVT_DB",
            )
        )
        return tuple(checks)

    def run(self, context: StepContext) -> StepResult:
        config = context.step.config
        environment = dict(context.resources.environment)
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DC_RTL_FILELIST": str(
                    _write_filelist(
                        context,
                        "rtl",
                        _source_members(context.step, "rtl_root", suffix=".sv"),
                    )
                ),
                "SIGILICON_DC_CONSTRAINTS": str(
                    context.source_path(_text(config, "constraints"))
                ),
            }
        )
        held = (
            "SIGILICON_STDCELL_RVT_DB",
            "SIGILICON_STDCELL_HVT_DB",
            "SIGILICON_STDCELL_LVT_DB",
        )
        with owned_scratch_directory(
            prefix=f"sigilicon-dc-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_DC_OUTPUT_ROOT"] = scratch.child_path
            completed = _run_script(
                context,
                environment,
                argument="",
                held_executables=("SIGILICON_SYNOPSYS_DC_SHELL",),
                held_files=held,
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            if completed.returncode:
                return StepResult(
                    "failed", logs, message=f"DC runner exited {completed.returncode}"
                )
            outputs = (
                _copied(
                    context,
                    role="mapped-netlist",
                    kind="netlist.verilog",
                    source=scratch.path / "mapped.v",
                    filename="mapped.v",
                ),
                _copied(
                    context,
                    role="mapped-constraints",
                    kind="constraints.sdc",
                    source=scratch.path / "mapped.sdc",
                    filename="mapped.sdc",
                ),
                _copied(
                    context,
                    role="checkpoint",
                    kind="checkpoint.synopsys-ddc",
                    source=scratch.path / "mapped.ddc",
                    filename="mapped.ddc",
                ),
            )
            reports = tuple(
                _copied(
                    context,
                    role="report",
                    kind="report.synopsys",
                    source=scratch.path / relative,
                    filename=relative,
                )
                for relative in _strings(config, "reports")
            )
            return StepResult.succeeded(artifacts=(*logs, *outputs, *reports))


class FcBackend:
    name = "synopsys.fc"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        target = _target(step.config)
        if target not in {"library", "pnr"}:
            raise ContractError(f"unsupported FC target {target!r}")
        names = (
            (
                "SIGILICON_SYNOPSYS_LM_SHELL",
                "SIGILICON_FC_TECH_FILE",
                "SIGILICON_FC_TECH_LEF",
                "SIGILICON_STDCELL_RVT_LEF",
                "SIGILICON_STDCELL_HVT_LEF",
                "SIGILICON_STDCELL_LVT_LEF",
                "SIGILICON_STDCELL_RVT_DB",
                "SIGILICON_STDCELL_HVT_DB",
                "SIGILICON_STDCELL_LVT_DB",
            )
            if target == "library"
            else (
                "SIGILICON_SYNOPSYS_FC_SHELL",
                "SIGILICON_FC_GDS_MAP",
                "SIGILICON_FC_TLUPLUS",
                "SIGILICON_FC_ANTENNA_RULES",
            )
        )
        checks.extend(
            _environment_path_check(
                resources,
                name,
                executable=name.endswith(("LM_SHELL", "FC_SHELL")),
            )
            for name in names
        )
        return tuple(checks)

    def run(self, context: StepContext) -> StepResult:
        config = context.step.config
        target = _target(config)
        environment = dict(context.resources.environment)
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DESIGN_CORNER": _text(config, "corner"),
                "SIGILICON_DESIGN_TOP": _text(config, "top"),
            }
        )
        held_files: list[str] = []
        held_directories: list[str] = []
        if target == "library":
            held_executables = ("SIGILICON_SYNOPSYS_LM_SHELL",)
            name = _safe_relative(
                _text(config, "reference_library_output"),
                "reference library output",
            )
            reference_root = ensure_nofollow_directory(
                context.output_root / "reference-library"
            ) / name
            report = context.output_path("library-check-report", "check_workspace.rpt")
            environment.update(
                {
                    "SIGILICON_FC_REFERENCE_NDM": str(reference_root),
                    "SIGILICON_FC_LIBRARY_CHECK_REPORT": str(report),
                }
            )
            held_files.extend(
                (
                    "SIGILICON_FC_TECH_FILE",
                    "SIGILICON_FC_TECH_LEF",
                    "SIGILICON_STDCELL_RVT_LEF",
                    "SIGILICON_STDCELL_HVT_LEF",
                    "SIGILICON_STDCELL_LVT_LEF",
                    "SIGILICON_STDCELL_RVT_DB",
                    "SIGILICON_STDCELL_HVT_DB",
                    "SIGILICON_STDCELL_LVT_DB",
                )
            )
        else:
            held_executables = ("SIGILICON_SYNOPSYS_FC_SHELL",)
            synthesis = _text(config, "synthesis_step")
            reference = _text(config, "reference_step")
            mapped_netlist = _artifact(context, synthesis, "mapped-netlist")
            mapped_constraints = _artifact(context, synthesis, "mapped-constraints")
            reference_files = context.artifacts(reference, "reference-library")
            if not reference_files:
                raise ExecutionError("reference-library step published no files")
            reference_name = _text(config, "reference_library_output")
            candidates = {
                parent
                for artifact in reference_files
                for parent in artifact.path.parents
                if parent.name == reference_name
            }
            if len(candidates) != 1:
                raise ExecutionError("cannot reconstruct the reference-library directory")
            output_names = {
                key: _safe_relative(str(value), f"FC output {key}")
                for key, value in _mapping(config, "outputs").items()
                if isinstance(key, str) and isinstance(value, str)
            }
            required = {
                "routed-netlist",
                "routed-constraints",
                "layout-stream",
                "checkpoint",
                "design-check-report",
                "structural-report",
                "qor-report",
                "timing-report",
                "area-report",
                "power-report",
                "drc-report",
                "physical-completion-report",
                "tie-off-check-report",
            }
            if set(output_names) != required:
                raise ContractError("FC outputs do not match the physical result contract")
            role_environment = {
                "routed-netlist": "SIGILICON_FC_ROUTED_NETLIST",
                "routed-constraints": "SIGILICON_FC_ROUTED_CONSTRAINTS",
                "layout-stream": "SIGILICON_FC_GDS",
                "checkpoint": "SIGILICON_FC_CHECKPOINT",
                "design-check-report": "SIGILICON_FC_DESIGN_CHECK_REPORT",
                "structural-report": "SIGILICON_FC_STRUCTURAL_REPORT",
                "qor-report": "SIGILICON_FC_QOR_REPORT",
                "timing-report": "SIGILICON_FC_TIMING_REPORT",
                "area-report": "SIGILICON_FC_AREA_REPORT",
                "power-report": "SIGILICON_FC_POWER_REPORT",
                "drc-report": "SIGILICON_FC_DRC_REPORT",
                "physical-completion-report": "SIGILICON_FC_PHYSICAL_COMPLETION_REPORT",
                "tie-off-check-report": "SIGILICON_FC_TIE_OFF_CHECK_REPORT",
            }
            for role, environment_name in role_environment.items():
                path = context.output_root / role / output_names[role]
                ensure_nofollow_directory(path.parent)
                environment[environment_name] = str(path)
            environment.update(
                {
                    "SIGILICON_FC_MAPPED_NETLIST": str(mapped_netlist.path),
                    "SIGILICON_FC_MAPPED_SDC": str(mapped_constraints.path),
                    "SIGILICON_FC_REFERENCE_NDM": str(next(iter(candidates))),
                }
            )
            held_files.extend(
                (
                    "SIGILICON_FC_GDS_MAP",
                    "SIGILICON_FC_TLUPLUS",
                    "SIGILICON_FC_ANTENNA_RULES",
                    "SIGILICON_FC_MAPPED_NETLIST",
                    "SIGILICON_FC_MAPPED_SDC",
                )
            )
            held_directories.append("SIGILICON_FC_REFERENCE_NDM")
        with owned_scratch_directory(
            prefix=f"sigilicon-fc-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_FC_WORK_ROOT"] = scratch.child_path
            completed = _run_script(
                context,
                environment,
                argument=target,
                held_executables=held_executables,
                held_files=tuple(held_files),
                held_directories=tuple(held_directories),
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            if completed.returncode:
                return StepResult(
                    "failed", logs, message=f"FC runner exited {completed.returncode}"
                )
            if target == "library":
                artifacts = (
                    *_tree_artifacts(
                        reference_root,
                        "reference-library",
                        "library.synopsys-ndm",
                    ),
                    Artifact("library-check-report", "report.synopsys", report),
                )
            else:
                artifacts = tuple(
                    Artifact(
                        role,
                        "checkpoint.synopsys-dlib"
                        if role == "checkpoint"
                        else "result.synopsys-fc",
                        path.absolute(),
                    )
                    for role in sorted(required)
                    for path in (
                        sorted((context.output_root / role).rglob("*"))
                        if role == "checkpoint"
                        else [context.output_root / role / output_names[role]]
                    )
                    if path.is_file() and not path.is_symlink()
                )
                if {artifact.role for artifact in artifacts} != required:
                    raise ExecutionError("FC omitted one or more physical result roles")
            return StepResult.succeeded(artifacts=(*logs, *artifacts))


class HspiceBackend:
    name = "synopsys.hspice"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        target = _target(step.config)
        names = [
            "SIGILICON_SYNOPSYS_HSPICE",
            "SIGILICON_HSPICE_NOMINAL_MODEL",
            "SIGILICON_STDCELL_RVT_SPICE",
            "SIGILICON_STDCELL_HVT_SPICE",
            "SIGILICON_STDCELL_LVT_SPICE",
        ]
        if _boolean(step.config, "requires_mismatch"):
            names.append("SIGILICON_HSPICE_MISMATCH_MODEL")
        if _boolean(step.config, "requires_12t"):
            names.append("SIGILICON_STDCELL_12T_RVT_SPICE")
        checks.extend(
            _environment_path_check(
                resources,
                name,
                executable=name == "SIGILICON_SYNOPSYS_HSPICE",
            )
            for name in names
        )
        environment = _mapping(step.config, "environment")
        prefix = _text(step.config, "environment_prefix")
        if any(
            not isinstance(name, str)
            or _ENVIRONMENT.fullmatch(name) is None
            or not name.startswith(prefix)
            or not isinstance(value, (str, int, float, bool))
            for name, value in environment.items()
        ) or _ENVIRONMENT_PREFIX.fullmatch(prefix) is None:
            raise ContractError(
                "HSPICE owner environment must use its declared uppercase prefix"
            )
        collect = _mapping(step.config, "collect")
        for role, relative in collect.items():
            if not isinstance(role, str) or not isinstance(relative, str):
                raise ContractError("HSPICE collect must map roles to relative paths")
            _safe_relative(relative, f"HSPICE collect {role}")
        for name, value in _mapping(step.config, "source_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
                or value not in step.sources
            ):
                raise ContractError(
                    "HSPICE source_environment must map environment names to step sources"
                )
        for name, value in _mapping(step.config, "output_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
            ):
                raise ContractError(
                    "HSPICE output_environment must map environment names to relative paths"
                )
            _safe_relative(value, f"HSPICE output_environment {name}")
        _boolean(step.config, "requires_python")
        return tuple(checks)

    def run(self, context: StepContext) -> StepResult:
        config = context.step.config
        target = _target(config)
        environment = dict(context.resources.environment)
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_HSPICE_SOURCE_ROOT": str(context.source_root),
                "SIGILICON_HSPICE_DECK_ROOT": str(context.source_root),
                "SIGILICON_HSPICE_MODEL_SECTION": _text(config, "model_section"),
            }
        )
        environment.update(
            {name: str(value) for name, value in _mapping(config, "environment").items()}
        )
        environment.update(
            {
                str(name): str(context.source_path(str(value)))
                for name, value in _mapping(config, "source_environment").items()
            }
        )
        if _boolean(config, "requires_python"):
            environment["SIGILICON_PYTHON"] = str(
                Path(sys.executable).resolve(strict=True)
            )
        if "corner" in config:
            environment["SIGILICON_DESIGN_CORNER"] = _text(config, "corner")
        held = [
            "SIGILICON_HSPICE_NOMINAL_MODEL",
            "SIGILICON_STDCELL_RVT_SPICE",
            "SIGILICON_STDCELL_HVT_SPICE",
            "SIGILICON_STDCELL_LVT_SPICE",
        ]
        if _boolean(config, "requires_mismatch"):
            held.append("SIGILICON_HSPICE_MISMATCH_MODEL")
        if _boolean(config, "requires_12t"):
            held.append("SIGILICON_STDCELL_12T_RVT_SPICE")
        if _boolean(config, "requires_python"):
            held.append("SIGILICON_PYTHON")
        with owned_scratch_directory(
            prefix=f"sigilicon-hspice-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_HSPICE_OUTPUT_ROOT"] = scratch.child_path
            environment.update(
                {
                    str(name): f"{scratch.child_path}/{value}"
                    for name, value in _mapping(
                        config, "output_environment"
                    ).items()
                }
            )
            completed = _run_script(
                context,
                environment,
                argument=target,
                held_executables=("SIGILICON_SYNOPSYS_HSPICE",),
                held_files=tuple(held),
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            artifacts: list[Artifact] = list(logs)
            for role, relative in _mapping(config, "collect").items():
                source = scratch.path / str(relative)
                if not source.is_file() and completed.returncode:
                    continue
                if not source.is_file():
                    raise ExecutionError(
                        f"HSPICE omitted collected output {relative!r}"
                    )
                artifacts.append(
                    _copied(
                        context,
                        role=str(role),
                        kind="evidence.hspice",
                        source=source,
                        filename=Path(str(relative)).name,
                    )
                )
            if completed.returncode:
                return StepResult(
                    "failed",
                    tuple(artifacts),
                    message=f"HSPICE runner exited {completed.returncode}",
                )
            return StepResult.succeeded(artifacts=tuple(artifacts))


class StructuralLinkBackend:
    """Link owner RTL against one locked, uncharacterized macro release."""

    name = "synopsys.structural-link"
    _fields = frozenset(
        {
            "owner",
            "dependency",
            "dependency_lock",
            "variant",
            "variant_contract",
            "compile_script",
            "link_script",
            "library_name",
            "macro_cell",
            "parameter_overrides",
            "expected_macro_instances",
            "expected_unresolved_references",
            "release_export",
            "liberty_role",
            "release_manifest",
            "release_liberty",
            "rtl_sources",
            "timeout_seconds",
        }
    )
    _capabilities = frozenset(
        {"tool.synopsys-library-compiler", "tool.synopsys-dc"}
    )

    def _config(self, step: Step) -> Mapping[str, Any]:
        unknown = set(step.config) - self._fields
        missing = self._fields - set(step.config)
        if unknown or missing:
            raise ContractError(
                "structural-link config fields disagree with its contract; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        return step.config

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = self._config(step)
        _text(config, "owner")
        for name in (
            "dependency_lock",
            "variant_contract",
            "compile_script",
            "link_script",
            "release_manifest",
            "release_liberty",
        ):
            path = _safe_relative(_text(config, name), name)
            if path not in step.sources:
                raise ContractError(
                    f"structural-link {name} must be inside the source closure"
                )
        for source in _strings(config, "rtl_sources"):
            _safe_relative(source, "structural-link RTL source")
            if source not in step.sources:
                raise ContractError(
                    "structural-link RTL source must be inside the source closure"
                )
        for name in (
            "dependency",
            "variant",
            "library_name",
            "macro_cell",
            "release_export",
            "liberty_role",
        ):
            _text(config, name)
        _mapping(config, "parameter_overrides")
        _positive_integer(config, "expected_macro_instances")
        unresolved = config.get("expected_unresolved_references")
        if type(unresolved) is not int or unresolved < 0:
            raise ContractError(
                "structural-link expected_unresolved_references must be non-negative"
            )
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("structural-link requires an evidence envelope")
        checks = [
            _environment_path_check(
                resources,
                "SIGILICON_SYNOPSYS_LIBRARY_COMPILER",
                executable=True,
            ),
            _environment_path_check(
                resources,
                "SIGILICON_SYNOPSYS_DC_SHELL",
                executable=True,
            ),
        ]
        checks.extend(
            PreflightCheck(
                "runtime-capability",
                capability,
                "ready" if capability in resources.capabilities else "blocked",
                "supplied by the invoking runtime"
                if capability in resources.capabilities
                else "missing capability",
            )
            for capability in sorted(self._capabilities)
        )
        return tuple(checks)

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.workflows.run_artifacts import RunArtifacts
        from sigilicon.workflows.structural_link import (
            execute_structural_link,
            plan_structural_link,
        )

        config = self._config(context.step)
        lock_name = _safe_relative(
            _text(config, "dependency_lock"), "dependency lock"
        )
        compile_name = _safe_relative(
            _text(config, "compile_script"), "Liberty compile script"
        )
        link_name = _safe_relative(
            _text(config, "link_script"), "structural link script"
        )
        variant = _text(config, "variant")
        variant_name = _safe_relative(
            _text(config, "variant_contract"), "structural-link variant contract"
        )
        rtl_names = tuple(
            _safe_relative(name, "structural-link RTL source")
            for name in _strings(config, "rtl_sources")
        )
        planning = plan_structural_link(
            owner=_text(config, "owner"),
            dependency=_text(config, "dependency"),
            dependency_lock_path=context.owner_source_path(lock_name),
            variant_path=context.owner_source_path(variant_name),
            variant=variant,
            rtl_sources=tuple(context.owner_source_path(name) for name in rtl_names),
            compile_script=context.owner_source_path(compile_name),
            link_script=context.owner_source_path(link_name),
            library_name=_text(config, "library_name"),
            macro_cell=_text(config, "macro_cell"),
            parameter_overrides=_mapping(config, "parameter_overrides"),
            expected_macro_instances=_positive_integer(
                config, "expected_macro_instances"
            ),
            expected_unresolved_references=int(
                config["expected_unresolved_references"]
            ),
            release_export=_text(config, "release_export"),
            liberty_role=_text(config, "liberty_role"),
            release_manifest=context.project_source_path(
                _safe_relative(_text(config, "release_manifest"), "release manifest")
            ),
            release_liberty=context.project_source_path(
                _safe_relative(_text(config, "release_liberty"), "release Liberty")
            ),
        )
        library_compiler = Path(
            _text(
                context.resources.environment,
                "SIGILICON_SYNOPSYS_LIBRARY_COMPILER",
            )
        )
        design_compiler = Path(
            _text(context.resources.environment, "SIGILICON_SYNOPSYS_DC_SHELL")
        )
        with owned_scratch_directory(
            prefix=f"sigilicon-structural-link-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = RunArtifacts.from_step_context(
                context,
                "structural-link",
                {"owner": planning.owner, "variant": planning.variant},
                tool_work_root=scratch.path,
            )
            result = execute_structural_link(
                planning,
                artifacts=artifacts,
                library_compiler=library_compiler,
                design_compiler=design_compiler,
                environment=context.resources.environment,
                timeout=_positive_integer(config, "timeout_seconds"),
            )
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("structural-link lost its evidence envelope")
        context.write_text(
            "structural-link",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "structural-link-flow-evidence",
                    "plan_identity": context.plan_identity,
                    "variant": planning.variant,
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "status": result.status,
                    **result.facts,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = _tree_artifacts(
            context.output_root / "structural-link",
            "structural-link",
            "evidence.structural-link",
        )
        facts = {
            **result.facts,
            "evidence_role": envelope.role,
            "evidence_level": envelope.level,
            "evidence_scope": envelope.scope,
        }
        return (
            StepResult.succeeded(artifacts=published, facts=facts)
            if result.passed
            else StepResult(
                "failed",
                published,
                facts,
                "Synopsys structural link did not prove the declared macro seam",
            )
        )


def synopsys_backends() -> tuple[
    VcsBackend,
    DcBackend,
    FcBackend,
    HspiceBackend,
    StructuralLinkBackend,
]:
    """Return the fixed trusted standard-ASIC backend pack."""

    return (
        VcsBackend(),
        DcBackend(),
        FcBackend(),
        HspiceBackend(),
        StructuralLinkBackend(),
    )


__all__ = [
    "DcBackend",
    "FcBackend",
    "HspiceBackend",
    "StructuralLinkBackend",
    "VcsBackend",
    "synopsys_backends",
]
