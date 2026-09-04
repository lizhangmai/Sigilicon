"""Direct Spectre and Xcelium adapters."""

from __future__ import annotations

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, Artifact, CADENCE_SPECTRE_TOOL, ContractError,
    ExecutionError, ExecutionIO, Mapping, Path, Project, PreflightCheck,
    PurePosixPath, Resources, Step, StepResult, _SPECTRE_TEMPLATE_TOKEN, _XRUN,
    _executable_check, _positive_integer, _relative, _runtime_bindings,
    _strict_config, _strings, _text, json, owned_scratch_directory,
)

class SpectreAdapter:
    """Run one source-owned Spectre deck template as managed raw evidence."""

    name = "cadence.spectre"
    _fields = frozenset({"deck", "outputs", "timeout_seconds"})

    @classmethod
    def _configuration(
        cls,
        step: Step,
    ) -> tuple[str, tuple[str, ...], int]:
        config = _strict_config(step, cls._fields)
        deck = _relative(_text(config, "deck"), "Spectre deck")
        if deck not in step.sources:
            raise ContractError("Spectre deck must be selected by the step filesets")
        outputs = tuple(
            _relative(name, "Spectre output")
            for name in _strings(config, "outputs")
        )
        if step.runtime.tools or step.runtime.directories:
            raise ContractError(
                "cadence.spectre runtime profiles support files and values only"
            )
        return deck, outputs, _positive_integer(config, "timeout_seconds")

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        del project
        self._configuration(step)
        return AdapterPreparation(
            resources=_runtime_bindings(resources, CADENCE_SPECTRE_TOOL),
        )

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        self._configuration(step)
        from sigilicon.execution.runtime import preflight_environment

        return (
            _executable_check(resources, CADENCE_SPECTRE_TOOL),
            *preflight_environment(step.runtime, resources),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        from sigilicon.adapters.cadence.spectre import run_spectre_deck

        step = context.step
        deck, outputs, timeout = self._configuration(step)
        workspace = context.workspace(
            "spectre",
            {
                "schema": 1,
                "contract_kind": "direct-spectre-plan",
                "deck": deck,
                "outputs": list(outputs),
            },
        )
        staged: dict[str, Path] = {}
        for name in step.sources:
            relative = PurePosixPath(name)
            destination = ("sources", *relative.parts)
            if len(destination) > 1:
                workspace.directory("inputs", *destination[:-1])
            staged[f"source:{name}"] = workspace.copy_file(
                "inputs",
                destination,
                context.owner_source_path(name),
            )
        if step.runtime.files:
            workspace.directory("inputs", "runtime")
        for alias, identity in step.runtime.files.items():
            staged[f"file:{alias}"] = workspace.copy_file(
                "inputs",
                ("runtime", alias),
                context.resource_path(identity),
            )
        values = {
            alias: context.runtime.require_value(identity)
            for alias, identity in step.runtime.values.items()
        }
        template = context.source_text(deck)

        def render(paths: Mapping[str, str]) -> str:
            rendered = template
            for key, path in paths.items():
                rendered = rendered.replace("{{" + key + "}}", path)
            for alias, value in values.items():
                rendered = rendered.replace("{{value:" + alias + "}}", value)
            unresolved = _SPECTRE_TEMPLATE_TOKEN.search(rendered)
            if unresolved is not None:
                raise ExecutionError(
                    f"Spectre deck has an unresolved input token: {unresolved.group()}"
                )
            return rendered

        execution = run_spectre_deck(
            workspace,
            render_deck=render,
            inputs=staged,
            output_names=outputs,
            timeout=timeout,
            resources=context.runtime,
            environment_values=context.runtime.environment,
        )
        artifacts: list[Artifact] = []
        for name, payload in execution.raw_outputs.items():
            relative = PurePosixPath(name)
            if len(relative.parts) > 1:
                workspace.directory("outputs", *relative.parts[:-1])
            artifacts.append(
                Artifact(
                    "spectre",
                    "raw.cadence-spectre",
                    workspace.write_bytes("outputs", relative.parts, payload),
                )
            )
        envelope = step.evidence
        evidence: dict[str, object] = {
            "schema": 1,
            "contract_kind": "cadence-spectre-evidence",
            "simulator_completed": True,
            "output_count": len(artifacts),
            "product_qualification_conclusion": False,
        }
        if envelope is not None:
            evidence.update(
                evidence_role=envelope.role,
                evidence_level=envelope.level,
                evidence_scope=envelope.scope,
            )
        artifacts.append(
            Artifact(
                "spectre",
                "evidence.cadence-spectre",
                context.write_text(
                    "spectre",
                    "flow-evidence.json",
                    json.dumps(evidence, sort_keys=True, separators=(",", ":"))
                    + "\n",
                ),
            )
        )
        return StepResult.succeeded(artifacts=tuple(artifacts))


class XceliumAdapter:
    """Execute one explicit, source-closed Verilog/SystemVerilog testbench."""

    name = "cadence.xcelium"
    _fields = frozenset({"success_marker", "timeout_seconds"})

    @staticmethod
    def _hdl_sources(step: Step) -> tuple[str, ...]:
        sources = tuple(
            source
            for source in step.sources
            if Path(source).suffix.lower() in {".sv", ".svh", ".v", ".vh"}
        )
        if not sources:
            raise ContractError("Xcelium filesets select no Verilog sources")
        return sources

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        for source in self._hdl_sources(step):
            _relative(source, "HDL fileset source")
        _text(config, "success_marker")
        _positive_integer(config, "timeout_seconds")
        return (_executable_check(resources, _XRUN),)

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        del project
        return AdapterPreparation(
            resources=_runtime_bindings(resources, _XRUN),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.xcelium import execute_xcelium_invocation

        config = _strict_config(context.step, self._fields)
        source_names = self._hdl_sources(context.step)
        sources = tuple(
            context.owner_source_path(_relative(source, "hdl source"))
            for source in source_names
        )
        timeout = _positive_integer(config, "timeout_seconds")
        marker = _text(config, "success_marker")
        with owned_scratch_directory(
            prefix=f"sigilicon-xcelium-{context.run_id}-"
        ) as scratch:
            workspace = context.workspace(
                "xcelium",
                {"step": step.id},
                tool_work_root=scratch.path,
            )
            completed = execute_xcelium_invocation(
                artifacts=workspace,
                plan_record={
                    "schema": 1,
                    "contract_kind": "direct-xcelium-plan",
                    "step": step.id,
                    "sources": list(source_names),
                    "success_marker": marker,
                },
                cell=step.id,
                dut=step.id,
                success_marker=marker,
                command_factory=lambda xrun, work, library: [
                    str(xrun),
                    "-64bit",
                    "-sv",
                    "-timescale",
                    "1ns/1ps",
                    "-xmlibdirname",
                    library,
                    "-log",
                    f"{work}/xrun.log",
                    *(str(source) for source in sources),
                ],
                resources=context.runtime,
                environment_values=context.runtime.environment,
                timeout=timeout,
            )
        artifacts = (
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "stdout.log", completed.stdout),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "stderr.log", completed.stderr or ""),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "xrun.log", completed.native_log),
            ),
            Artifact(
                "xcelium",
                "summary.cadence-xcelium",
                completed.run_summary,
            ),
        )
        return (
            StepResult.succeeded(artifacts=artifacts)
            if completed.passed
            else StepResult(
                "failed",
                artifacts,
                message="Xcelium did not prove a successful declared testbench",
            )
        )
