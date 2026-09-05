"""HSPICE circuit-simulation adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    Artifact, ContractError, ExecutionError, ExecutionIO, Path, PreflightCheck,
    Resources, Step, StepResult, _ENVIRONMENT, _ENVIRONMENT_PREFIX, _PYTHON,
    _base_checks, _boolean, _declared_inputs, _logs, _mapping, _run_script,
    _runtime_environment, _safe_relative, _strict_config, _target, _text,
    owned_scratch_directory, preflight_environment,
    process_group_cleanup_uncertainty,
)

class HspiceAdapter:
    name = "synopsys.hspice"
    prepare = _declared_inputs
    _fields = frozenset(
        {
            "collect",
            "diagnostics",
            "corner",
            "environment",
            "environment_prefix",
            "model_section",
            "output_environment",
            "requires_python",
            "runner",
            "source_environment",
            "target",
            "timeout_seconds",
            "variant",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        checks = _base_checks(step)
        _target(config)
        checks.extend(preflight_environment(step.runtime, resources))
        environment = _mapping(config, "environment")
        prefix = _text(config, "environment_prefix")
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
        for field in ("collect", "diagnostics"):
            for role, relative in _mapping(config, field).items():
                if not isinstance(role, str) or not isinstance(relative, str):
                    raise ContractError(f"HSPICE {field} must map roles to relative paths")
                _safe_relative(relative, f"HSPICE {field} {role}")
        for name, value in _mapping(config, "source_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
                or value not in step.sources
            ):
                raise ContractError(
                    "HSPICE source_environment must map environment names to step sources"
                )
        for name, value in _mapping(config, "output_environment").items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT.fullmatch(name) is None
                or not isinstance(value, str)
            ):
                raise ContractError(
                    "HSPICE output_environment must map environment names to relative paths"
                )
            _safe_relative(value, f"HSPICE output_environment {name}")
        if _boolean(config, "requires_python") and _PYTHON not in (
            step.runtime.tools
        ):
            raise ContractError(
                f"Python-backed HSPICE steps must bind {_PYTHON} as a tool"
            )
        return tuple(checks)

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        config = _strict_config(context.step, self._fields)
        target = _target(config)
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_HSPICE_SOURCE_ROOT": str(context.source_directory),
                "SIGILICON_HSPICE_DECK_ROOT": str(context.source_directory),
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
            for role, pattern in _mapping(config, "diagnostics").items():
                for source in sorted(scratch.path.glob(str(pattern))):
                    artifacts.append(
                        context.copy_output(
                            role=str(role),
                            kind="diagnostic.hspice",
                            source=source,
                            filename=source.relative_to(scratch.path).as_posix(),
                        )
                    )
            for role, relative in _mapping(config, "collect").items():
                source = scratch.path / str(relative)
                if not source.is_file() and completed.returncode:
                    continue
                if not source.is_file():
                    raise ExecutionError(
                        f"HSPICE omitted collected output {relative!r}"
                    )
                artifacts.append(
                    context.copy_output(
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
