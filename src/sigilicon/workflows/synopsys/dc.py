"""Synopsys Design Compiler Adapter implementation."""

from __future__ import annotations

from ._common import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    Any,
    CollectedActionResult,
    FlowExecutionError,
    Path,
    ProducedArtifact,
    _DC_RESOURCE_ENVIRONMENT,
    _SYNTHESIS_OUTPUT_ROLES,
    _fact_set,
    _manifest_members,
    _pinned_owner_runner,
    _stage_source_set,
    _text_mapping,
    atomic_write_json,
    complete_staged_run,
    os,
    run_process_group_capture,
)

class SynopsysDCAdapter:
    """Run one owner recipe behind the typed ``asic.synthesis`` interface."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind != "asic.synthesis":
            diagnostics.append("Synopsys DC Adapter requires asic.synthesis")
        try:
            self._pinned_runner(context)
            self._configuration(context)
            self._execution_resources(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in ("rtl-sources", "constraints", "synthesis-recipe"):
            try:
                artifact = context.input(role)
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
                continue
            if not artifact.path.is_file():
                diagnostics.append(f"synthesis input {role!r} is not a regular file")
        if rtl := context.inputs.get("rtl-sources"):
            try:
                _manifest_members(
                    rtl.path,
                    rtl.kind,
                    rtl.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        rtl = context.inputs.get("rtl-sources")
        constraints = context.inputs.get("constraints")
        if rtl is not None and constraints is not None:
            for dimension in sorted(set(rtl.qualifiers) & set(constraints.qualifiers)):
                if rtl.qualifiers[dimension] != constraints.qualifiers[dimension]:
                    diagnostics.append(
                        f"synthesis input qualifier {dimension!r} does not match"
                    )
        if rtl is not None and "variant" not in rtl.qualifiers:
            diagnostics.append("DC design input omitted the variant qualifier")
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        runner = self._pinned_runner(context)
        configuration = self._configuration(context)
        environment = os.environ.copy()
        executable, timing_members = self._execution_resources(context)
        environment["SIGILICON_SYNOPSYS_DC_SHELL"] = str(executable)
        for role, environment_name in _DC_RESOURCE_ENVIRONMENT.items():
            environment[environment_name] = str(timing_members[role])
        tool_root = context.output_root / "tool"
        environment["SIGILICON_DC_OUTPUT_ROOT"] = str(tool_root)
        materialized_inputs = {
            "rtl-sources": _stage_source_set(
                context,
                "rtl-sources",
                "rtl-sources.f",
            ),
            "constraints": context.input("constraints").path,
        }
        environment["SIGILICON_DC_RTL_FILELIST"] = str(
            materialized_inputs["rtl-sources"]
        )
        environment["SIGILICON_DC_CONSTRAINTS"] = str(
            materialized_inputs["constraints"]
        )
        qualifiers = context.input("rtl-sources").qualifiers
        if "variant" not in qualifiers:
            raise FlowExecutionError("DC design input omitted the variant qualifier")
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])

        completed = run_process_group_capture(
            [str(runner)],
            cwd=context.work_root,
            env=environment,
            timeout=configuration["timeout_seconds"],
        )
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        status = "succeeded" if completed.returncode == 0 else "failed"
        return AdapterExecution(status, completed.returncode)

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        configuration = self._configuration(context)
        tool_root = context.output_root / "tool"
        outputs = configuration["outputs"]
        produced: list[ProducedArtifact] = []
        qualifiers = context.input("rtl-sources").qualifiers
        for role in sorted(_SYNTHESIS_OUTPUT_ROLES):
            output = self._managed_tool_output(tool_root, outputs[role])
            if not output.is_file():
                raise FlowExecutionError(
                    f"Synopsys DC omitted required output {role!r}: {outputs[role]}"
                )
            produced.append(
                ProducedArtifact(
                    role,
                    context.action.output(role).kind,
                    output,
                    qualifiers=qualifiers,
                )
            )

        report_members: list[dict[str, Any]] = []
        for relative in configuration["reports"]:
            report = self._managed_tool_output(tool_root, relative)
            if not report.is_file():
                raise FlowExecutionError(
                    f"Synopsys DC omitted required report: {relative}"
                )
            report_members.append(
                {
                    "path": report.relative_to(context.output_root).as_posix(),
                }
            )
        report_manifest = context.output_path("reports", "reports.json")
        atomic_write_json(
            report_manifest,
            {
                "schema": 1,
                "contract_kind": "artifact-collection",
                "kind": "report.collection",
                "qualifiers": dict(qualifiers),
                "members": report_members,
            },
        )
        produced.append(
            ProducedArtifact(
                "reports",
                context.action.output("reports").kind,
                report_manifest,
                qualifiers=qualifiers,
            )
        )
        return CollectedActionResult(
            artifacts=tuple(produced),
            facts=_fact_set(context, {}),
            evidence=(
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
            ),
        )

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-dc")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys DC capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys DC executable is unavailable")
        asset = context.platform_assets.get("standard-cell-timing")
        if asset is None or asset.kind != "library.synopsys-db-set":
            raise FlowExecutionError(
                "Synopsys DC requires a resolved standard-cell timing DB set"
            )
        members: dict[str, Path] = {}
        for role in _DC_RESOURCE_ENVIRONMENT:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"standard-cell timing DB set omitted {role!r}"
                )
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved standard-cell timing DB {role!r} is unavailable"
                )
            members[role] = member.location
        return executable, members

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            context,
            "synthesis-recipe",
            "synthesis",
        )

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        unknown = set(context.adapter_config) - {
            "timeout_seconds",
            "outputs",
            "reports",
        }
        if unknown:
            raise FlowExecutionError(
                f"DC Adapter contains unknown configuration: {sorted(unknown)}"
            )
        timeout_seconds = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise FlowExecutionError("DC Adapter requires a positive timeout_seconds")
        outputs = _text_mapping(
            context.adapter_config.get("outputs"),
            "DC outputs",
        )
        if set(outputs) != _SYNTHESIS_OUTPUT_ROLES:
            raise FlowExecutionError(
                "DC outputs must map mapped-netlist, mapped-constraints and checkpoint"
            )
        raw_reports = context.adapter_config.get("reports")
        if (
            not isinstance(raw_reports, tuple)
            or not raw_reports
            or any(not isinstance(item, str) or not item for item in raw_reports)
        ):
            raise FlowExecutionError("DC reports must be a non-empty list")
        reports = tuple(raw_reports)
        for relative in (*outputs.values(), *reports):
            self._validate_relative_output(relative)
        all_outputs = (*outputs.values(), *reports)
        if len(all_outputs) != len(set(all_outputs)):
            raise FlowExecutionError("DC output and report paths must be unique")
        return {
            "timeout_seconds": timeout_seconds,
            "outputs": outputs,
            "reports": reports,
        }

    @staticmethod
    def _validate_relative_output(value: str) -> None:
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(f"unsafe DC output path: {value!r}")

    @staticmethod
    def _managed_tool_output(tool_root: Path, relative: str) -> Path:
        output = (tool_root / relative).resolve()
        if not output.is_relative_to(tool_root.resolve()):
            raise FlowExecutionError(f"DC output escaped managed root: {relative!r}")
        return output
