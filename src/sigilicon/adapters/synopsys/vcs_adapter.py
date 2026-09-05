"""VCS simulation adapter."""

from __future__ import annotations

from sigilicon.execution._model import (
    ExecutionIO,
    StepResult,
)
from sigilicon.external_tools import (
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.adapters.synopsys._common import (
    _artifact,
    _logs,
    _run_script,
    _runtime_environment,
    _write_filelist,
)

from sigilicon.adapters.synopsys.planning import VcsAction, RunnerAdapter, require_action

class VcsAdapter(RunnerAdapter):
    name = "synopsys.vcs"
    action_type = VcsAction

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        action = require_action(step, VcsAction)
        target = action.target
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment["SIGILICON_DESIGN_VARIANT"] = action.invocation.variant
        rtl = action.rtl
        testbench = action.testbench
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
            artifact = _artifact(context, action.synthesis_step, "mapped-netlist")
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
                invocation=action.invocation,
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
        marker = action.success_marker
        terminal_lines = tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )
        marker_count = sum(line == marker for line in terminal_lines)
        if marker_count == 0:
            return StepResult(
                "failed",
                logs,
                message="VCS runner omitted its declared success marker",
            )
        if marker_count != 1 or terminal_lines[-1] != marker:
            return StepResult(
                "failed",
                logs,
                message="VCS runner success marker is not one unique terminal record",
            )
        return StepResult.succeeded(artifacts=logs)
