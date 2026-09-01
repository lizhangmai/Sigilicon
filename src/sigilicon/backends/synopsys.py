"""Direct Synopsys process backends with one owner-script boundary.

The backend owns process safety, runtime resources, and artifact collection.
The selected owner owns the small VCS/DC/FC/HSPICE launcher scripts and their
tool-specific inputs.  No registration module or generic command executor is
involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
import os
from pathlib import Path, PurePosixPath
import re
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
    owned_input_file,
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
    held_files: tuple[str, ...],
    held_directories: tuple[str, ...] = (),
):
    """Run one source-pinned script while holding every external input path."""

    runner = _runner(context.step)
    with ExitStack() as stack:
        source_root = stack.enter_context(owned_directory(context.source_root))
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
        return run_process_group_capture(
            command,
            cwd=context.work_root,
            env=environment,
            timeout=_positive_integer(context.step.config, "timeout_seconds"),
            before_spawn=source_root.require_visible,
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
        environment["SIGILICON_VCS_OUTPUT_ROOT"] = str(context.work_root / "tool")
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
        held = ["SIGILICON_SYNOPSYS_VCS"]
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
        completed = _run_script(
            context,
            environment,
            argument=target,
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
        tool_root = ensure_nofollow_directory(context.work_root / "tool")
        environment = dict(context.resources.environment)
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DC_OUTPUT_ROOT": str(tool_root),
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
            "SIGILICON_SYNOPSYS_DC_SHELL",
            "SIGILICON_STDCELL_RVT_DB",
            "SIGILICON_STDCELL_HVT_DB",
            "SIGILICON_STDCELL_LVT_DB",
        )
        completed = _run_script(
            context, environment, argument="", held_files=held
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
                source=tool_root / "mapped.v",
                filename="mapped.v",
            ),
            _copied(
                context,
                role="mapped-constraints",
                kind="constraints.sdc",
                source=tool_root / "mapped.sdc",
                filename="mapped.sdc",
            ),
            _copied(
                context,
                role="checkpoint",
                kind="checkpoint.synopsys-ddc",
                source=tool_root / "mapped.ddc",
                filename="mapped.ddc",
            ),
        )
        reports = tuple(
            _copied(
                context,
                role="report",
                kind="report.synopsys",
                source=tool_root / relative,
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
                "SIGILICON_FC_WORK_ROOT": str(context.work_root / "tool"),
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DESIGN_CORNER": _text(config, "corner"),
                "SIGILICON_DESIGN_TOP": _text(config, "top"),
            }
        )
        held_files: list[str] = []
        held_directories: list[str] = []
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
            held_files.extend(
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
                (
                    "SIGILICON_SYNOPSYS_FC_SHELL",
                    "SIGILICON_FC_GDS_MAP",
                    "SIGILICON_FC_TLUPLUS",
                    "SIGILICON_FC_ANTENNA_RULES",
                    "SIGILICON_FC_MAPPED_NETLIST",
                    "SIGILICON_FC_MAPPED_SDC",
                )
            )
            held_directories.append("SIGILICON_FC_REFERENCE_NDM")
        completed = _run_script(
            context,
            environment,
            argument=target,
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
                *_tree_artifacts(reference_root, "reference-library", "library.synopsys-ndm"),
                Artifact("library-check-report", "report.synopsys", report),
            )
        else:
            artifacts = tuple(
                Artifact(
                    role,
                    "checkpoint.synopsys-dlib" if role == "checkpoint" else "result.synopsys-fc",
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
        tool_root = ensure_nofollow_directory(context.work_root / "tool")
        environment = dict(context.resources.environment)
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_HSPICE_OUTPUT_ROOT": str(tool_root),
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
        environment.update(
            {
                str(name): str(tool_root / str(value))
                for name, value in _mapping(config, "output_environment").items()
            }
        )
        if _boolean(config, "requires_python"):
            environment["SIGILICON_PYTHON"] = str(
                Path(sys.executable).resolve(strict=True)
            )
        if "corner" in config:
            environment["SIGILICON_DESIGN_CORNER"] = _text(config, "corner")
        held = [
            "SIGILICON_SYNOPSYS_HSPICE",
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
        completed = _run_script(
            context,
            environment,
            argument=target,
            held_files=tuple(held),
        )
        logs = _logs(context, completed.stdout, completed.stderr or "")
        artifacts: list[Artifact] = list(logs)
        for role, relative in _mapping(config, "collect").items():
            source = tool_root / str(relative)
            if not source.is_file() and completed.returncode:
                continue
            if not source.is_file():
                raise ExecutionError(f"HSPICE omitted collected output {relative!r}")
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


def synopsys_backends() -> tuple[VcsBackend, DcBackend, FcBackend, HspiceBackend]:
    """Return the fixed trusted standard-ASIC backend pack."""

    return VcsBackend(), DcBackend(), FcBackend(), HspiceBackend()


__all__ = [
    "DcBackend",
    "FcBackend",
    "HspiceBackend",
    "VcsBackend",
    "synopsys_backends",
]
