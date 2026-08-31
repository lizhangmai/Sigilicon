"""Synopsys VCS Adapter implementation."""

from __future__ import annotations

from ._common import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    Any,
    CollectedActionResult,
    FlowExecutionError,
    Mapping,
    Path,
    ProducedArtifact,
    _VCS_MODEL_ENVIRONMENT,
    _VCS_TARGETS,
    _fact_set,
    _manifest_members,
    _pinned_owner_runner,
    _stage_source_set,
    atomic_write_json,
    complete_staged_run,
    os,
    run_process_group_capture,
)

class SynopsysVCSAdapter:
    """Run owner VCS recipes behind typed simulation Action interfaces."""

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
        if context.action.kind not in _VCS_TARGETS:
            diagnostics.append("Synopsys VCS Adapter received an unsupported Action")
            return tuple(diagnostics)
        try:
            self._target(context)
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in self._source_set_roles(context):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"VCS input {role!r} is missing")
                continue
            try:
                _manifest_members(
                    artifact.path,
                    artifact.kind,
                    artifact.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        if context.action.kind == "asic.gate-simulation":
            mapped = context.inputs.get("mapped-netlist")
            if mapped is None or not mapped.path.is_file():
                diagnostics.append("VCS mapped-netlist input is unavailable")
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        target = self._target(context)
        runner = self._pinned_runner(context)
        configuration = self._configuration(context)
        executable, models = self._execution_resources(context)
        qualifiers = self._qualifiers(context)
        environment = os.environ.copy()
        environment["SIGILICON_SYNOPSYS_VCS"] = str(executable)
        environment["SIGILICON_VCS_OUTPUT_ROOT"] = str(
            context.output_root / "tool"
        )
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        for role in self._source_set_roles(context):
            environment_name = {
                "rtl-sources": "SIGILICON_VCS_RTL_FILELIST",
                "testbench": "SIGILICON_VCS_TESTBENCH_FILELIST",
            }.get(role)
            if environment_name is not None:
                environment[environment_name] = str(
                    _stage_source_set(
                        context,
                        role,
                        f"{role}.f",
                    )
                )
        if context.action.kind == "asic.gate-simulation":
            environment["SIGILICON_VCS_MAPPED_NETLIST"] = str(
                context.input("mapped-netlist").path
            )
        for role, environment_name in _VCS_MODEL_ENVIRONMENT.items():
            if role in models:
                environment[environment_name] = str(models[role])

        completed = run_process_group_capture(
            [str(runner), target],
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
        return AdapterExecution(
            "succeeded" if completed.returncode == 0 else "failed",
            completed.returncode,
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        qualifiers = self._qualifiers(context)
        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
        evidence = context.output_path("evidence", "simulation.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "simulation-evidence",
                "kind": "evidence.simulation",
                "action": context.action.kind,
                "target": self._target(context),
                "qualifiers": dict(qualifiers),
            },
        )
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    context.action.output("evidence").kind,
                    evidence,
                    qualifiers=qualifiers,
                ),
            ),
            facts=_fact_set(context, {}),
            evidence=(stdout, stderr),
        )

    def _target(self, context: ActionContext) -> str:
        value = context.action_config.get("target")
        if not isinstance(value, str) or value not in _VCS_TARGETS[context.action.kind]:
            raise FlowExecutionError(
                f"unsupported VCS target {value!r} for {context.action.kind}"
            )
        return value

    def _configuration(self, context: ActionContext) -> dict[str, int]:
        unknown = set(context.adapter_config) - {"timeout_seconds"}
        if unknown:
            raise FlowExecutionError(
                f"VCS Adapter contains unknown configuration: {sorted(unknown)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError("VCS Adapter requires a positive timeout_seconds")
        return {"timeout_seconds": timeout}

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            context,
            "simulation-recipe",
            "simulation",
        )

    def _source_set_roles(self, context: ActionContext) -> tuple[str, ...]:
        if context.action.kind == "asic.rtl-simulation":
            return ("rtl-sources", "testbench")
        if context.action.kind == "asic.structural-elaboration":
            return ("rtl-sources",)
        return ("testbench",)

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        semantic_inputs = [
            artifact
            for role, artifact in context.inputs.items()
            if role != "simulation-recipe"
        ]
        if not semantic_inputs:
            raise FlowExecutionError("VCS Action has no semantic design input")
        qualifiers = dict(semantic_inputs[0].qualifiers)
        if "variant" not in qualifiers:
            raise FlowExecutionError("VCS design input omitted the variant qualifier")
        if any(dict(artifact.qualifiers) != qualifiers for artifact in semantic_inputs[1:]):
            raise FlowExecutionError("VCS design input qualifiers do not match")
        recipe = context.input("simulation-recipe")
        if dict(recipe.qualifiers) != qualifiers:
            raise FlowExecutionError("VCS simulation recipe qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-vcs")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys VCS capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys VCS executable is unavailable")
        if context.action.kind == "asic.rtl-simulation":
            return executable, {}
        asset = context.platform_assets.get("standard-cell-models")
        if asset is None or asset.kind != "library.verilog-model-set":
            raise FlowExecutionError(
                "Synopsys VCS requires a resolved standard-cell Verilog model set"
            )
        models: dict[str, Path] = {}
        for role in _VCS_MODEL_ENVIRONMENT:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"standard-cell Verilog model set omitted {role!r}"
                )
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved standard-cell Verilog model {role!r} is unavailable"
                )
            models[role] = member.location
        return executable, models
