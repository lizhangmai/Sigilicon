"""VCS simulation adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    ExecutionIO, PreflightCheck, Resources, Step, StepResult, _artifact,
    _base_checks, _declared_inputs, _logs, _run_script, _runtime_environment,
    _source_members, _strict_config, _target, _text, _write_filelist,
    owned_scratch_directory, preflight_environment,
    process_group_cleanup_uncertainty,
)

class VcsAdapter:
    name = "synopsys.vcs"
    prepare = _declared_inputs
    _fields = frozenset(
        {
            "rtl_root",
            "runner",
            "success_marker",
            "synthesis_step",
            "target",
            "testbench_root",
            "timeout_seconds",
            "variant",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        _strict_config(step, self._fields)
        checks = _base_checks(step)
        _target(step.config)
        _text(step.config, "success_marker")
        _source_members(step, "rtl_root", suffix=".sv")
        _source_members(step, "testbench_root", suffix=".sv")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        config = _strict_config(context.step, self._fields)
        target = _target(config)
        runtime = _runtime_environment(context.runtime, context.step)
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
        marker = _text(config, "success_marker")
        if marker not in completed.stdout:
            return StepResult(
                "failed",
                logs,
                message="VCS runner omitted its declared success marker",
            )
        return StepResult.succeeded(artifacts=logs)
