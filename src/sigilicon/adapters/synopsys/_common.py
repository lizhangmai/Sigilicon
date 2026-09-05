"""Shared supervised-process boundary for Synopsys adapters."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shlex
import tarfile
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sigilicon.adapters.synopsys.planning import Invocation

from sigilicon.artifacts import SafeTree, ensure_nofollow_directory
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import require_relative_path
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution._model import (
    Artifact,
    ContractError,
    ExecutionError,
    ResourceBinding,
    PreflightCheck,
    Resources,
    Source,
    Step,
    ExecutionIO,
    StepResult,
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
from sigilicon.project import Project
from sigilicon.adapters.synopsys.structural_link import (
    StructuralLinkPlan,
    execute_structural_link,
    plan_structural_link,
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


@dataclass(frozen=True)
class _ToolVerdict:
    owner: str
    stage: str
    variant: str
    passed: bool
    product_qualification_conclusion: bool
    checks: Mapping[str, bool]

    @classmethod
    def load(
        cls, path: Path, *, owner: str, stage: str, variant: str
    ) -> _ToolVerdict:
        try:
            with owned_input_file(path, require_single_link=True) as source:
                payload = json.loads(os.pread(source.fd, os.fstat(source.fd).st_size, 0))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"invalid tool verdict {path.name}: {exc}") from exc
        fields = {
            "schema",
            "contract_kind",
            "owner",
            "stage",
            "variant",
            "passed",
            "product_qualification_conclusion",
            "checks",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ExecutionError(f"invalid tool verdict envelope in {path.name}")
        if payload["schema"] != 1 or payload["contract_kind"] != "tool-verdict":
            raise ExecutionError(f"unsupported tool verdict contract in {path.name}")
        texts = {
            name: payload[name]
            for name in ("owner", "stage", "variant")
        }
        if any(not isinstance(value, str) or not value for value in texts.values()):
            raise ExecutionError(f"invalid tool verdict identity in {path.name}")
        expected = {"owner": owner, "stage": stage, "variant": variant}
        for name, value in expected.items():
            if texts[name] != value:
                raise ExecutionError(
                    f"tool verdict {name} mismatch: expected {value!r}, got {texts[name]!r}"
                )
        passed = payload["passed"]
        qualification = payload["product_qualification_conclusion"]
        checks = payload["checks"]
        if not isinstance(passed, bool) or not isinstance(qualification, bool):
            raise ExecutionError(f"invalid tool verdict conclusion in {path.name}")
        if (
            not isinstance(checks, dict)
            or not checks
            or any(
                not isinstance(name, str)
                or not name
                or not isinstance(value, bool)
                for name, value in checks.items()
            )
        ):
            raise ExecutionError(f"invalid tool verdict checks in {path.name}")
        if passed != all(checks.values()):
            raise ExecutionError(f"inconsistent tool verdict conclusion in {path.name}")
        return cls(
            owner=texts["owner"],
            stage=texts["stage"],
            variant=texts["variant"],
            passed=passed,
            product_qualification_conclusion=qualification,
            checks=checks,
        )

    def facts(self) -> dict[str, object]:
        return {
            "tool_verdict": {
                "owner": self.owner,
                "stage": self.stage,
                "variant": self.variant,
                "passed": self.passed,
                "product_qualification_conclusion": (
                    self.product_qualification_conclusion
                ),
                "checks": dict(self.checks),
            }
        }


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


def _source_members(
    step: Step,
    root_name: str,
    *,
    suffix: str | tuple[str, ...],
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
        Artifact("log", "log.synopsys", context.write_text("log", name, value))
        for name, value in values
    )


def _artifact(context: ExecutionIO, dependency: str, role: str) -> Artifact:
    artifacts = context.artifacts(dependency, role)
    if len(artifacts) != 1:
        raise ExecutionError(
            f"dependency {dependency!r} must publish exactly one {role!r} artifact"
        )
    return artifacts[0]


def _run_script(
    context: ExecutionIO,
    environment: dict[str, str],
    *,
    argument: str,
    invocation: Invocation,
    held_executables: tuple[str, ...],
    held_files: tuple[str, ...],
    held_directories: tuple[str, ...] = (),
) -> ProcessResult:
    """Run one source-pinned script while holding every external input path."""

    runner = invocation.runner
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
            timeout_seconds=invocation.timeout_seconds,
            before_spawn=visible,
        ))
