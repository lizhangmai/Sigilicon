"""Design Compiler synthesis adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    ContractError, ExecutionIO, PreflightCheck, Resources, Step, StepResult,
    _ToolVerdict, _base_checks, _declared_inputs, _logs, _run_script,
    _runtime_environment, _safe_relative, _source_members, _strict_config,
    _strings, _text, _write_filelist, owned_scratch_directory,
    preflight_environment, process_group_cleanup_uncertainty,
)

class DcAdapter:
    name = "synopsys.dc"
    prepare = _declared_inputs
    _fields = frozenset(
        {
            "constraints",
            "corner",
            "evaluator",
            "reports",
            "rtl_root",
            "runner",
            "timeout_seconds",
            "variant",
            "verdict_report",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        _strict_config(step, self._fields)
        checks = _base_checks(step)
        constraints = _safe_relative(_text(step.config, "constraints"), "constraints")
        evaluator = _safe_relative(_text(step.config, "evaluator"), "evaluator")
        _text(step.config, "corner")
        if constraints not in step.sources:
            raise ContractError("DC constraints must be inside the step source closure")
        if evaluator not in step.sources:
            raise ContractError("DC evaluator must be inside the step source closure")
        _source_members(step, "rtl_root", suffix=".sv")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        config = _strict_config(context.step, self._fields)
        runtime = _runtime_environment(context.runtime, context.step)
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
                "SIGILICON_IMPLEMENTATION_EVALUATOR": str(
                    context.source_path(_text(config, "evaluator"))
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
            verdict_name = _safe_relative(
                _text(config, "verdict_report"), "DC verdict report"
            )
            verdict = _ToolVerdict.load(
                scratch.path / verdict_name,
                owner=context.owner,
                stage="synthesis",
                variant=_text(config, "variant"),
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
                for relative in _strings(config, "reports")
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
