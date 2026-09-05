"""Design Compiler synthesis adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    ExecutionError, ExecutionIO, StepResult, _ToolVerdict, _logs,
    _run_script, _runtime_environment, _write_filelist, owned_scratch_directory,
    process_group_cleanup_uncertainty,
)

from sigilicon.adapters.synopsys.planning import DcAction, RunnerAdapter, require_action

class DcAdapter(RunnerAdapter):
    name = "synopsys.dc"
    action_type = DcAction

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        action = require_action(step, DcAction)
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": action.invocation.variant,
                "SIGILICON_DESIGN_CORNER": action.corner,
                "SIGILICON_DC_RTL_FILELIST": str(
                    _write_filelist(
                        context,
                        "rtl",
                        action.rtl,
                    )
                ),
                "SIGILICON_DC_CONSTRAINTS": str(
                    context.source_path(action.constraints)
                ),
                "SIGILICON_IMPLEMENTATION_EVALUATOR": str(
                    context.source_path(action.evaluator)
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
                invocation=action.invocation,
                argument="",
                held_executables=runtime.tools,
                held_files=runtime.files,
                held_directories=runtime.directories,
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            declared = (
                ("mapped-netlist", "netlist.verilog", "mapped.v"),
                ("mapped-constraints", "constraints.sdc", "mapped.sdc"),
                ("checkpoint", "checkpoint.synopsys-ddc", "mapped.ddc"),
                *(("report", "report.synopsys", name) for name in action.reports),
                ("execution-verdict", "evidence.tool-verdict", action.verdict),
            )
            artifacts = list(logs)
            missing = []
            for role, kind, name in declared:
                source = scratch.path / name
                if not source.is_file():
                    missing.append(name)
                    continue
                artifacts.append(context.copy_output(role=role, kind=kind, source=source, filename=name))
            if completed.returncode:
                return StepResult("failed", tuple(artifacts), message=f"DC runner exited {completed.returncode}")
            if missing:
                return StepResult("failed", tuple(artifacts), message=f"DC omitted outputs: {missing}")
            try:
                verdict = _ToolVerdict.load(
                    scratch.path / action.verdict, context=context, stage="synthesis",
                    variant=action.invocation.variant, corner=action.corner,
                )
            except ExecutionError as exc:
                return StepResult("failed", tuple(artifacts), message=str(exc))
            if not verdict.passed:
                return StepResult(
                    "failed", tuple(artifacts),
                    message="DC execution completed but owner evidence failed",
                )
            return StepResult.succeeded(artifacts=tuple(artifacts))
