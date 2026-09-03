"""Direct Synopsys process adapters with one owner-script boundary.

The adapter owns process safety, runtime resources, and artifact collection.
The selected owner owns the small VCS/DC/FC/HSPICE launcher scripts and their
tool-specific inputs.  No registration module or generic command executor is
involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Any

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
)
from sigilicon.canonical import canonical_digest
from sigilicon.execution.adapter import DirectAdapter, PlanningProject
from sigilicon.execution.model import (
    _AdapterAction,
    Artifact,
    ContractError,
    ExecutionError,
    ResourceBinding,
    PreflightCheck,
    Resources,
    Source,
    Step,
    StepContext,
    StepResult,
    json_value,
)
from sigilicon.execution.runtime import (
    BoundEnvironment,
    bind_environment,
    preflight_environment,
)
from sigilicon.external_tools import (
    ProcessRequest,
    ProcessResult,
    managed_process,
    owned_directory,
    owned_input_closure,
    owned_input_file,
    owned_output_file,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)


_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ENVIRONMENT_PREFIX = re.compile(r"[A-Z][A-Z0-9_]*_\Z")
_TARGET = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_RUNNER_SHELL = "SIGILICON_RUNNER_SHELL"
_PYTHON = "SIGILICON_PYTHON"
_TOOL_LOCATION_ENVIRONMENT = frozenset(
    {
        "VCS_HOME",
        "VCS_ARCH_OVERRIDE",
        "SYN_HOME",
        "FUSIONCOMPILER_HOME",
        "HSPICE_HOME",
        "LC_HOME",
        "SYNOPSYS_LC_ROOT",
    }
)


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
    root = _safe_relative(_text(step.action.config, root_name), root_name)
    prefix = f"{root}/"
    return tuple(
        source
        for source in step.sources
        if source.startswith(prefix) and source.endswith(suffix)
    )


def _runner(step: Step) -> str:
    runner = _safe_relative(_text(step.action.config, "runner"), "runner")
    if runner not in step.sources:
        raise ContractError("Synopsys runner must be inside the step source closure")
    return runner


def _prepend_path(environment: dict[str, str], directory: Path) -> None:
    existing = environment.get("PATH")
    environment["PATH"] = str(directory) + (
        os.pathsep + existing if existing else ""
    )


def _runtime_environment(
    resources: Resources,
    step: Step,
) -> BoundEnvironment:
    """Bind an owner profile and derive only vendor installation variables."""

    bound = bind_environment(step.runtime, resources)
    environment = {
        name: value
        for name, value in bound.values.items()
        if name not in _TOOL_LOCATION_ENVIRONMENT
    }
    for name in bound.tools:
        executable = Path(environment[name])
        if (
            executable.name == "vcs"
            and executable.parent.name == "bin"
        ):
            environment["VCS_HOME"] = str(executable.parents[1])
            environment["VCS_ARCH_OVERRIDE"] = "linux"
        elif (
            executable.name == "dc_shell"
            and executable.parent.name == "bin"
        ):
            environment["SYN_HOME"] = str(executable.parents[1])
        elif (
            executable.name in {"lm_shell", "fc_shell"}
            and executable.parent.name == "bin"
            and executable.parent.parent.name == "fusioncompiler"
        ):
            environment["FUSIONCOMPILER_HOME"] = str(executable.parents[1])
            environment["SYN_HOME"] = str(executable.parents[2])
        elif (
            executable.name == "hspice"
            and executable.parent.name == "bin"
            and executable.parent.parent.name == "hspice"
        ):
            environment["HSPICE_HOME"] = str(executable.parents[2])
        elif (
            executable.name == "lc_shell"
            and executable.parent.name == "bin"
        ):
            home = executable.parents[1]
            environment["LC_HOME"] = str(home)
            environment["SYNOPSYS_LC_ROOT"] = str(home)
        _prepend_path(environment, executable.parent)
    return replace(bound, values=environment)


def _base_checks(step: Step) -> list[PreflightCheck]:
    runner = _runner(step)
    _positive_integer(step.action.config, "timeout_seconds")
    if not step.runtime.tools:
        raise ContractError("Synopsys step requires a runtime profile with a tool")
    if _RUNNER_SHELL not in step.runtime.tools:
        raise ContractError(
            f"Synopsys runtime profile must bind {_RUNNER_SHELL} as a tool"
        )
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
) -> ProcessResult:
    """Run one source-pinned script while holding every external input path."""

    runner = _runner(context.step)
    with ExitStack() as stack:
        source_root = stack.enter_context(owned_directory(context.source_root))
        work_root = stack.enter_context(owned_directory(context.work_root))
        source_closure = stack.enter_context(
            owned_input_closure(
                context.source_root,
                files=tuple(
                    context.source_path(source)
                    for source in context.step.sources
                ),
            )
        )
        resource_root = (
            None
            if context.resource_root is None
            else stack.enter_context(owned_directory(context.resource_root))
        )

        def child_input(path: Path) -> str | None:
            if path.is_relative_to(context.source_root):
                relative = path.relative_to(context.source_root)
                return (
                    source_root.child_path
                    if relative == Path(".")
                    else f"{source_root.child_path}/{relative.as_posix()}"
                )
            if (
                resource_root is not None
                and context.resource_root is not None
                and path.is_relative_to(context.resource_root)
            ):
                relative = path.relative_to(context.resource_root)
                return (
                    resource_root.child_path
                    if relative == Path(".")
                    else f"{resource_root.child_path}/{relative.as_posix()}"
                )
            return None

        for name, value in tuple(environment.items()):
            path = Path(value)
            if (
                path.is_absolute()
                and path.is_relative_to(context.source_root)
                and (selected := child_input(path)) is not None
            ):
                environment[name] = selected
        executable_by_name = {}
        executables = []
        launchers = []
        for name in held_executables:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            identity = context.step.runtime.tools.get(name)
            if identity is None:
                raise ExecutionError(
                    f"runtime executable {name} is outside the Step tool bindings"
                )
            owned = stack.enter_context(context.resources.owned_tool(identity))
            executable_by_name[name] = owned
            executables.append(owned)
        runner_shell = executable_by_name.get(_RUNNER_SHELL)
        if runner_shell is None or len(runner_shell.command) != 1:
            raise ExecutionError("runtime runner shell must be a native executable")
        shell_path = runner_shell.target.child_named_path
        for name, owned in executable_by_name.items():
            if len(owned.command) == 1:
                environment[name] = owned.target.child_named_path
            else:
                launcher_name = f".{name.lower()}.launcher"
                launcher_path = context.work_root / launcher_name
                payload = (
                    f"#!{shell_path}\nexec "
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
        for name in held_files:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            selected = child_input(Path(value))
            if selected is not None and Path(value).is_file():
                environment[name] = selected
            else:
                owned = stack.enter_context(
                    owned_input_file(Path(value), require_single_link=False)
                )
                environment[name] = owned.child_named_path
        for name in held_directories:
            value = environment.get(name)
            if not value:
                raise ExecutionError(f"runtime environment omitted {name}")
            selected = child_input(Path(value))
            if selected is not None and Path(value).is_dir():
                environment[name] = selected
            else:
                owned = stack.enter_context(owned_directory(Path(value)))
                environment[name] = owned.child_path
        command = [shell_path, f"{source_root.child_path}/{runner}"]
        if argument:
            command.append(argument)

        def visible() -> None:
            source_root.require_visible()
            source_closure.require_visible()
            work_root.require_visible()
            if resource_root is not None:
                resource_root.require_visible()
            for executable in executables:
                executable.require_visible()
            for launcher in launchers:
                launcher.require_visible()
        return managed_process.run(ProcessRequest(
            argv=tuple(command),
            cwd=Path(work_root.child_path),
            environment=environment,
            timeout_seconds=_positive_integer(
                context.step.action.config, "timeout_seconds"
            ),
            before_spawn=visible,
        ))


class VcsAdapter(DirectAdapter):
    name = "synopsys.vcs"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        _target(step.action.config)
        _source_members(step, "rtl_root", suffix=".sv")
        _source_members(step, "testbench_root", suffix=".sv")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        config = context.step.action.config
        target = _target(config)
        runtime = _runtime_environment(context.resources, context.step)
        environment = runtime.values
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
        held = list(runtime.files)
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
                held_executables=runtime.tools,
                held_files=tuple(held),
                held_directories=runtime.directories,
            )
        logs = _logs(context, completed.stdout, completed.stderr or "")
        if completed.returncode:
            return StepResult(
                "failed", logs, message=f"VCS runner exited {completed.returncode}"
            )
        return StepResult.succeeded(artifacts=logs)


class DcAdapter(DirectAdapter):
    name = "synopsys.dc"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        constraints = _safe_relative(_text(step.action.config, "constraints"), "constraints")
        _text(step.action.config, "corner")
        if constraints not in step.sources:
            raise ContractError("DC constraints must be inside the step source closure")
        _source_members(step, "rtl_root", suffix=".sv")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        config = context.step.action.config
        runtime = _runtime_environment(context.resources, context.step)
        environment = runtime.values
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
                held_executables=runtime.tools,
                held_files=runtime.files,
                held_directories=runtime.directories,
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


class FcAdapter(DirectAdapter):
    name = "synopsys.fc"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        target = _target(step.action.config)
        _text(step.action.config, "corner")
        if target not in {"library", "pnr"}:
            raise ContractError(f"unsupported FC target {target!r}")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        config = context.step.action.config
        target = _target(config)
        runtime = _runtime_environment(context.resources, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DESIGN_CORNER": _text(config, "corner"),
                "SIGILICON_DESIGN_TOP": _text(config, "top"),
            }
        )
        held_files = list(runtime.files)
        held_directories = list(runtime.directories)
        if target == "library":
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
        else:
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
                ("SIGILICON_FC_MAPPED_NETLIST", "SIGILICON_FC_MAPPED_SDC")
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
                held_executables=runtime.tools,
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


class HspiceAdapter(DirectAdapter):
    name = "synopsys.hspice"

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        checks = _base_checks(step)
        _target(step.action.config)
        checks.extend(preflight_environment(step.runtime, resources))
        environment = _mapping(step.action.config, "environment")
        prefix = _text(step.action.config, "environment_prefix")
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
        collect = _mapping(step.action.config, "collect")
        for role, relative in collect.items():
            if not isinstance(role, str) or not isinstance(relative, str):
                raise ContractError("HSPICE collect must map roles to relative paths")
            _safe_relative(relative, f"HSPICE collect {role}")
        for name, value in _mapping(step.action.config, "source_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
                or value not in step.sources
            ):
                raise ContractError(
                    "HSPICE source_environment must map environment names to step sources"
                )
        for name, value in _mapping(step.action.config, "output_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
            ):
                raise ContractError(
                    "HSPICE output_environment must map environment names to relative paths"
                )
            _safe_relative(value, f"HSPICE output_environment {name}")
        _boolean(step.action.config, "requires_python")
        if _boolean(step.action.config, "requires_python") and _PYTHON not in (
            step.runtime.tools
        ):
            raise ContractError(
                f"Python-backed HSPICE steps must bind {_PYTHON} as a tool"
            )
        return tuple(checks)

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        config = context.step.action.config
        target = _target(config)
        runtime = _runtime_environment(context.resources, context.step)
        environment = runtime.values
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
        if "corner" in config:
            environment["SIGILICON_DESIGN_CORNER"] = _text(config, "corner")
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
                held_executables=runtime.tools,
                held_files=runtime.files,
                held_directories=runtime.directories,
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


class StructuralLinkAdapter(DirectAdapter):
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
            "library_compiler_version",
            "release_export",
            "liberty_role",
            "timeout_seconds",
        }
    )
    def _config(self, step: Step) -> Mapping[str, Any]:
        config = step.action.config
        unknown = set(config) - self._fields
        missing = self._fields - set(config)
        if unknown or missing:
            raise ContractError(
                "structural-link config fields disagree with its contract; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        return config

    @staticmethod
    def _rtl_sources(step: Step) -> tuple[str, ...]:
        sources = tuple(
            source
            for source in step.sources
            if Path(source).suffix.lower() in {".sv", ".v"}
        )
        if not sources:
            raise ContractError("structural-link filesets select no RTL sources")
        return sources

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = self._config(step)
        _text(config, "owner")
        for name in (
            "dependency_lock",
            "variant_contract",
            "compile_script",
            "link_script",
        ):
            path = _safe_relative(_text(config, name), name)
            if path not in step.sources:
                raise ContractError(
                    f"structural-link {name} must be inside the source closure"
                )
        for source in self._rtl_sources(step):
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
        _text(config, "library_compiler_version")
        unresolved = config.get("expected_unresolved_references")
        if type(unresolved) is not int or unresolved < 0:
            raise ContractError(
                "structural-link expected_unresolved_references must be non-negative"
            )
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("structural-link requires an evidence envelope")
        if not step.runtime.tools:
            raise ContractError(
                "structural-link requires an owner-declared runtime profile"
            )
        return preflight_environment(step.runtime, resources)

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.workflows.structural_link import plan_structural_link

        initial = step
        config = self._config(initial)
        owner = _text(config, "owner")
        owner_root = project.owner(owner).root
        project_root = project.project_root
        lock_name = _safe_relative(
            _text(config, "dependency_lock"), "dependency lock"
        )
        variant_name = _safe_relative(
            _text(config, "variant_contract"), "structural-link variant contract"
        )
        compile_name = _safe_relative(
            _text(config, "compile_script"), "Liberty compile script"
        )
        link_name = _safe_relative(
            _text(config, "link_script"), "structural link script"
        )
        rtl_names = tuple(
            _safe_relative(name, "structural-link RTL source")
            for name in self._rtl_sources(initial)
        )
        owner_names = (
            lock_name,
            variant_name,
            compile_name,
            link_name,
            *rtl_names,
        )
        captured_sources = [
            ("owner", Source.capture(owner_root / name, root=owner_root, scope="owner"))
            for name in owner_names
        ]
        by_location = {source.location: source for _scope, source in captured_sources}
        planning = plan_structural_link(
            owner=owner,
            dependency=_text(config, "dependency"),
            dependency_lock_path=by_location[owner_root / lock_name].location,
            variant_path=by_location[owner_root / variant_name].location,
            variant=_text(config, "variant"),
            rtl_sources=tuple(by_location[owner_root / name].location for name in rtl_names),
            compile_script=by_location[owner_root / compile_name].location,
            link_script=by_location[owner_root / link_name].location,
            library_name=_text(config, "library_name"),
            macro_cell=_text(config, "macro_cell"),
            parameter_overrides=_mapping(config, "parameter_overrides"),
            expected_macro_instances=_positive_integer(
                config, "expected_macro_instances"
            ),
            expected_unresolved_references=int(
                config["expected_unresolved_references"]
            ),
            library_compiler_version=_text(config, "library_compiler_version"),
            release_export=_text(config, "release_export"),
            liberty_role=_text(config, "liberty_role"),
            artifact_root=project.artifact_root,
        )
        external = (
            ResourceBinding.capture(
                planning.release_sources[0],
                identity=(
                    f"release:{_text(config, 'dependency')}:"
                    f"{planning.release_id}/manifest"
                ),
            ),
            ResourceBinding.capture(
                planning.release_sources[1],
                identity=(
                    f"release:{_text(config, 'dependency')}:"
                    f"{planning.release_id}/role/{_text(config, 'liberty_role')}"
                ),
            ),
        )
        if any(not source.current() for _scope, source in captured_sources):
            raise ContractError(
                "structural-link input changed while its Step was being prepared"
            )
        if (
            external[0].sha256 != planning.release_manifest_sha256
            or external[1].sha256 != planning.release_liberty_sha256
        ):
            raise ContractError(
                "structural-link release changed while its Step was being bound"
            )
        prepared_record = self._planning_record(
            planning,
            rtl_sources=rtl_names,
            compile_script=compile_name,
            link_script=link_name,
            release_manifest_resource=external[0].identity,
            release_liberty_resource=external[1].identity,
        )
        source_names = tuple(
            dict.fromkeys(
                (*step.sources, *(source.path for _scope, source in captured_sources))
            )
        )
        prepared = replace(
            step,
            action=_AdapterAction(step.uses, config, prepared_record),
            sources=source_names,
            resources=tuple(resource.identity for resource in external),
            _source_snapshots=tuple(
                dict.fromkeys(
                    (
                        *step._source_snapshots,
                        *(source for _scope, source in captured_sources),
                    )
                )
            ),
            _resource_bindings=external,
        )
        return prepared

    @staticmethod
    def _planning_record(
        plan: Any,
        *,
        rtl_sources: tuple[str, ...],
        compile_script: str,
        link_script: str,
        release_manifest_resource: str,
        release_liberty_resource: str,
    ) -> Mapping[str, Any]:
        return {
            "owner": plan.owner,
            "variant": plan.variant,
            "top": plan.top,
            "rtl_sources": list(rtl_sources),
            "compile_script": compile_script,
            "link_script": link_script,
            "library_name": plan.library_name,
            "macro_cell": plan.macro_cell,
            "parameter_overrides": dict(plan.parameter_overrides),
            "expected_macro_instances": plan.expected_macro_instances,
            "expected_unresolved_references": plan.expected_unresolved_references,
            "library_compiler_version": plan.library_compiler_version,
            "release_id": plan.release_id,
            "release_source_commit": plan.release_source_commit,
            "release_store": plan.release_store,
            "release_manifest_resource": release_manifest_resource,
            "release_manifest_sha256": plan.release_manifest_sha256,
            "release_liberty_resource": release_liberty_resource,
            "release_liberty_sha256": plan.release_liberty_sha256,
        }

    def _execute(self, context: StepContext, planning: Any) -> StepResult:
        from sigilicon.workflows.structural_link import execute_structural_link

        config = self._config(context.step)
        runtime = _runtime_environment(context.resources, context.step)
        try:
            library_compiler = context.step.runtime.tools[
                "SIGILICON_SYNOPSYS_LIBRARY_COMPILER"
            ]
            design_compiler = context.step.runtime.tools[
                "SIGILICON_SYNOPSYS_DC_SHELL"
            ]
        except KeyError as exc:
            raise ContractError(
                "structural-link runtime profile must bind both compiler roles"
            ) from exc
        with owned_scratch_directory(
            prefix=f"sigilicon-structural-link-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = context.files(
                "structural-link",
                {"owner": planning.owner, "variant": planning.variant},
                tool_work_root=scratch.path,
            )
            result = execute_structural_link(
                planning,
                artifacts=artifacts,
                resources=context.resources,
                library_compiler=library_compiler,
                design_compiler=design_compiler,
                environment=runtime.values,
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


    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.workflows.structural_link import StructuralLinkPlan

        config = self._config(step)
        prepared = step.action.prepared
        if not isinstance(prepared, Mapping):
            raise ExecutionError("structural-link request was not prepared")
        rtl_names = tuple(
            _safe_relative(name, "structural-link RTL source")
            for name in self._rtl_sources(context.step)
        )
        compile_script = _safe_relative(
            _text(config, "compile_script"), "Liberty compile script"
        )
        link_script = _safe_relative(
            _text(config, "link_script"), "structural link script"
        )
        release_resource = _text(
            prepared,
            "release_liberty_resource",
        )
        manifest_resource = _text(
            prepared,
            "release_manifest_resource",
        )
        planning = StructuralLinkPlan(
            owner=_text(prepared, "owner"),
            variant=_text(prepared, "variant"),
            top=_text(prepared, "top"),
            rtl_sources=tuple(context.owner_source_path(name) for name in rtl_names),
            compile_script=context.owner_source_path(compile_script),
            link_script=context.owner_source_path(link_script),
            library_name=_text(prepared, "library_name"),
            macro_cell=_text(prepared, "macro_cell"),
            parameter_overrides=_mapping(prepared, "parameter_overrides"),
            expected_macro_instances=_positive_integer(
                prepared, "expected_macro_instances"
            ),
            expected_unresolved_references=int(
                prepared["expected_unresolved_references"]
            ),
            library_compiler_version=_text(
                prepared, "library_compiler_version"
            ),
            release_liberty=context.resource_path(release_resource),
            release_id=_text(prepared, "release_id"),
            release_source_commit=_text(prepared, "release_source_commit"),
            release_store=_text(prepared, "release_store"),
            release_manifest_sha256=_text(
                prepared, "release_manifest_sha256"
            ),
            release_liberty_sha256=_text(
                prepared, "release_liberty_sha256"
            ),
            release_sources=(
                context.resource_path(manifest_resource),
                context.resource_path(release_resource),
            ),
        )
        return self._execute(context, planning)


def synopsys_adapters() -> tuple[
    VcsAdapter,
    DcAdapter,
    FcAdapter,
    HspiceAdapter,
    StructuralLinkAdapter,
]:
    """Return the fixed trusted standard-ASIC adapter pack."""

    return (
        VcsAdapter(),
        DcAdapter(),
        FcAdapter(),
        HspiceAdapter(),
        StructuralLinkAdapter(),
    )


__all__ = [
    "DcAdapter",
    "FcAdapter",
    "HspiceAdapter",
    "StructuralLinkAdapter",
    "VcsAdapter",
    "synopsys_adapters",
]
