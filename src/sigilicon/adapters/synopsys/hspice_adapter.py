"""HSPICE circuit-simulation adapter."""

from __future__ import annotations

from sigilicon.execution._model import (
    Artifact,
    ExecutionError,
    ExecutionIO,
    StepResult,
)
from sigilicon.external_tools import (
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.adapters.synopsys._common import (
    _logs,
    _run_script,
    _runtime_environment,
)

from sigilicon.adapters.synopsys.planning import HspiceAction, RunnerAdapter, require_action

class HspiceAdapter(RunnerAdapter):
    name = "synopsys.hspice"
    action_type = HspiceAction

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        action = require_action(step, HspiceAction)
        target = action.target
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": action.invocation.variant,
                "SIGILICON_HSPICE_SOURCE_ROOT": str(context.source_directory),
                "SIGILICON_HSPICE_DECK_ROOT": str(context.source_directory),
                "SIGILICON_HSPICE_MODEL_SECTION": action.model_section,
            }
        )
        environment.update(
            dict(action.environment)
        )
        environment.update(
            {
                str(name): str(context.source_path(str(value)))
                for name, value in action.source_environment
            }
        )
        environment["SIGILICON_DESIGN_CORNER"] = action.corner
        with owned_scratch_directory(
            prefix=f"sigilicon-hspice-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_HSPICE_OUTPUT_ROOT"] = scratch.child_path
            environment.update(
                {
                    str(name): f"{scratch.child_path}/{value}"
                    for name, value in action.output_environment
                }
            )
            completed = _run_script(
                context,
                environment,
                invocation=action.invocation,
                argument=target,
                held_executables=runtime.tools,
                held_files=runtime.files,
                held_directories=runtime.directories,
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            artifacts: list[Artifact] = list(logs)
            for output in action.outputs:
                paths = (scratch.path / output.path,) if output.required else sorted(scratch.path.glob(output.path))
                for source in paths:
                    if not source.is_file() and completed.returncode:
                        continue
                    if not source.is_file():
                        raise ExecutionError(f"HSPICE omitted collected output {output.path!r}")
                    artifacts.append(context.copy_output(
                        role=output.role,
                        kind="evidence.hspice" if output.required else "diagnostic.hspice",
                        source=source,
                        filename=source.relative_to(scratch.path).as_posix(),
                    ))
            if completed.returncode:
                return StepResult(
                    "failed",
                    tuple(artifacts),
                    message=f"HSPICE runner exited {completed.returncode}",
                )
            return StepResult.succeeded(artifacts=tuple(artifacts))
