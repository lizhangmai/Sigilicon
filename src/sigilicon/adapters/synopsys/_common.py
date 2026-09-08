"""Shared supervised-process boundary for Synopsys adapters."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shlex
import tarfile
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sigilicon.adapters.synopsys.planning import Invocation

from sigilicon.artifacts import SafeTree
from sigilicon.contracts import require_relative_path
from sigilicon.execution._result import Artifact
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._resources import Resources
from sigilicon.execution._plan import Step
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution.runtime import BoundEnvironment, bind_environment
from sigilicon.external_tools import (
    ProcessRequest,
    ProcessResult,
    managed_process,
    owned_directory,
    owned_input_closure,
    owned_input_file,
    owned_output_file,
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
    target = _text(config, "target")
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


def _strict_config(step: Step, fields: frozenset[str]) -> Mapping[str, Any]:
    unknown = set(step.config) - fields
    if unknown:
        raise ContractError(
            f"{step.uses} step {step.id!r} has unknown config fields: "
            f"{', '.join(sorted(unknown))}"
        )
    return step.config


def _safe_relative(value: str, label: str) -> str:
    try:
        return require_relative_path(value, label).as_posix()
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _runner(step: Step) -> str:
    runner = _safe_relative(_text(step.config, "runner"), "runner")
    if runner not in step.sources:
        raise ContractError("Synopsys runner must be inside the step source closure")
    return runner


def _prepend_path(environment: dict[str, str], directory: Path) -> None:
    existing = environment.get("PATH")
    environment["PATH"] = str(directory) + (
        os.pathsep + existing if existing else ""
    )


def _archive_directory(source: Path, destination: Path, name: str) -> None:
    """Bundle one completed tool directory without exposing vendor path syntax."""

    SafeTree(source).inventory(verify_content=True)

    def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
        member.uid = 0
        member.gid = 0
        member.uname = ""
        member.gname = ""
        member.mtime = 0
        member.mode = 0o755 if member.isdir() else 0o644
        member.pax_headers.clear()
        return member

    with tarfile.open(destination, "w", format=tarfile.GNU_FORMAT) as archive:
        archive.add(source, arcname=name, recursive=True, filter=normalize)


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


def _write_filelist(context: ExecutionIO, name: str, sources: tuple[str, ...]) -> Path:
    if not sources:
        raise ExecutionError(f"managed {name} source set is empty")
    return context.workspace(
        "synopsys",
        {},
        tool_work_root=context.work_directory,
    ).write_text(
        "work",
        (f"{name}.f",),
        "".join(f"{context.source_path(source)}\n" for source in sources),
    )


def _logs(context: ExecutionIO, stdout: str, stderr: str) -> tuple[Artifact, ...]:
    values = (("stdout.log", stdout), ("stderr.log", stderr))
    return tuple(
        Artifact("log", "log.synopsys", context.write_text("log", f"logs/{name}", value))
        for name, value in values
    )


def _run_script(
    context: ExecutionIO,
    environment: dict[str, str],
    *,
    argument: str,
    invocation: Invocation,
    held_executables: tuple[str, ...],
    held_files: tuple[str, ...],
    held_directories: tuple[str, ...] = (),
    generated_runner: Path | None = None,
) -> ProcessResult:
    """Run one source-pinned script while holding every external input path."""

    runner = invocation.runner
    environment.update({
        "SIGILICON_PLAN_IDENTITY": context.plan_identity,
        "SIGILICON_RUN_ID": context.run_id,
        "SIGILICON_STEP_ID": context.step.id,
    })
    with ExitStack() as stack:
        source_root = stack.enter_context(owned_directory(context.source_directory))
        work_root = stack.enter_context(owned_directory(context.work_directory))
        source_closure = stack.enter_context(
            owned_input_closure(
                context.source_directory,
                files=tuple(
                    context.source_path(source)
                    for source in context.step.sources
                ),
            )
        )
        resource_root = (
            None
            if context.resource_directory is None
            else stack.enter_context(owned_directory(context.resource_directory))
        )

        def child_input(path: Path) -> str | None:
            if path.is_relative_to(context.source_directory):
                relative = path.relative_to(context.source_directory)
                return (
                    source_root.child_path
                    if relative == Path(".")
                    else f"{source_root.child_path}/{relative.as_posix()}"
                )
            if (
                resource_root is not None
                and context.resource_directory is not None
                and path.is_relative_to(context.resource_directory)
            ):
                relative = path.relative_to(context.resource_directory)
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
                and path.is_relative_to(context.source_directory)
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
            owned = stack.enter_context(context.runtime.owned_tool(identity))
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
                launcher_path = context.work_directory / launcher_name
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
        if generated_runner is None:
            runner_path = f"{source_root.child_path}/{runner}"
        else:
            if not generated_runner.is_relative_to(context.work_directory):
                raise ExecutionError("generated runner must be in managed tool work")
            generated = stack.enter_context(owned_input_file(
                generated_runner, require_single_link=True))
            launchers.append(generated)
            runner_path = generated.child_named_path
        command = [shell_path, runner_path]
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
            timeout_seconds=invocation.timeout_seconds,
            before_spawn=visible,
        ))


def _hdl_environment(context, compilation):
    """One sealed compile contract for synthesis and simulation owner runners."""
    payload = {**compilation.record,
               "sources": [str(context.source_path(path)) for path in compilation.sources],
               "headers": [str(context.source_path(path)) for path in compilation.headers],
               "include_dirs": [str(context.source_directory / path) for path in compilation.include_dirs]}
    return {"SIGILICON_DESIGN_TOP": compilation.top,
            "SIGILICON_HDL_FILELIST": str(_write_filelist(context, "hdl", compilation.sources)),
            "SIGILICON_HDL_CONTRACT": str(context.workspace("synopsys", {}, tool_work_root=context.work_directory).write_text("work", ("hdl.json",), json.dumps(payload) + "\n"))}
