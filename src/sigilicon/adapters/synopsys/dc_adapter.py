"""Design Compiler synthesis adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    ExecutionIO, StepResult, _ToolVerdict, _logs,
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
            if completed.returncode:
                return StepResult(
                    "failed", logs, message=f"DC runner exited {completed.returncode}"
                )
            verdict_name = action.verdict
            verdict = _ToolVerdict.load(
                scratch.path / verdict_name,
                owner=context.owner,
                stage="synthesis",
                variant=action.invocation.variant,
            )
            outputs = (
                context.copy_output(
                    role="mapped-netlist",
                    kind="netlist.verilog",
                    source=scratch.path / "mapped.v",
                    filename="mapped.v",
                ),
                context.copy_output(
                    role="mapped-constraints",
                    kind="constraints.sdc",
                    source=scratch.path / "mapped.sdc",
                    filename="mapped.sdc",
                ),
                context.copy_output(
                    role="checkpoint",
                    kind="checkpoint.synopsys-ddc",
                    source=scratch.path / "mapped.ddc",
                    filename="mapped.ddc",
                ),
            )
            reports = tuple(
                context.copy_output(
                    role="report",
                    kind="report.synopsys",
                    source=scratch.path / relative,
                    filename=relative,
                )
                for relative in action.reports
            )
            verdict_artifact = context.copy_output(
                role="execution-verdict",
                kind="evidence.tool-verdict",
                source=scratch.path / verdict_name,
                filename=verdict_name,
            )
            artifacts = (*logs, *outputs, *reports, verdict_artifact)
            if not verdict.passed:
                return StepResult(
                    "failed",
                    artifacts,
                    message="DC execution completed but owner evidence failed",
                )
            return StepResult.succeeded(artifacts=artifacts)
