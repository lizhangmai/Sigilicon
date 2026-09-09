"""Direct Spectre and Xcelium adapters."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sigilicon.canonical import canonical_digest
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.external_tools import CADENCE_SPECTRE_TOOL, owned_scratch_directory
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from typing import Mapping
from pathlib import Path, PurePosixPath
from sigilicon.project import Project
from sigilicon.domain.hdl import HdlCompilation
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.adapters.cadence._common import (
    _SPECTRE_TEMPLATE_TOKEN,
    _XRUN,
    _executable_check,
    _positive_integer,
    _relative,
    _runtime_bindings,
    _strict_config,
    _strings,
    _text,
)
from sigilicon.adapters.cadence.spectre_measurement import (
    MeasurementAction, measurement_configuration, prepare_measurement, run_measurement,
)

@dataclass(frozen=True)
class _SpectreAction:
    deck: str
    outputs: tuple[str, ...]
    timeout_seconds: int

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "direct-spectre", **asdict(self)}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


@dataclass(frozen=True)
class _XceliumAction:
    hdl: HdlCompilation
    success_marker: str
    timeout_seconds: int
    read_access: bool
    outputs: tuple[str, ...]

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "direct-xcelium", **asdict(self)}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


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

    def contract(self, project: Project, step: Step) -> StepContract:
        if "program" in step.config:
            from sigilicon.domain.platform import load_platform
            config, _, _, _, artifacts = measurement_configuration(step)
            platform = load_platform(
                project, config["owner"], config["platform"]
            )
            if platform.simulation is None:
                raise ContractError("Spectre measurement requires a simulation platform")
            platform.simulation.model_set(config["model_set"])
            return StepContract(consumes=tuple(ref for _, ref in artifacts), produces=(ArtifactProduct("measurement", "evidence.measurement", path="measurements.json"),
                ArtifactProduct("measurement", "table.measurement", path="waveforms.csv"),
                ArtifactProduct("spectre", "raw.cadence-spectre", "many")))
        _, outputs, _ = self._configuration(step)
        return StepContract(produces=(*(ArtifactProduct("spectre", "raw.cadence-spectre", path=f"spectre/{name}") for name in outputs),
                                      ArtifactProduct("spectre", "evidence.cadence-spectre", path="flow-evidence.json")))

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        if "program" in step.config:
            return prepare_measurement(project, step, resources)
        del project
        action = _SpectreAction(*self._configuration(step))
        return AdapterPreparation(
            action=action,
            resources=_runtime_bindings(resources, CADENCE_SPECTRE_TOOL),
        )

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        if isinstance(step.action, MeasurementAction):
            step.validate_action()
            return (_executable_check(resources, CADENCE_SPECTRE_TOOL),
                    _executable_check(resources, "runtime.python"))
        if step.action is None:
            self._configuration(step)
        elif not isinstance(step.action, _SpectreAction):
            raise ContractError("Spectre Step has an invalid typed action")
        step.validate_action()
        from sigilicon.execution.runtime import preflight_environment

        return (
            _executable_check(resources, CADENCE_SPECTRE_TOOL),
            *preflight_environment(step.runtime, resources),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        if isinstance(context.step.action, MeasurementAction):
            return run_measurement(context)
        from sigilicon.adapters.cadence.spectre import run_spectre_deck

        step = context.step
        step.validate_action()
        action = step.action
        if not isinstance(action, _SpectreAction):
            raise ExecutionError("Spectre Step has no typed action")
        deck, outputs, timeout = action.deck, action.outputs, action.timeout_seconds
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
                context.source_path(name),
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
    _fields = frozenset({"hdl", "success_marker", "timeout_seconds"})

    @staticmethod
    def _observation(config):
        read_access = config.get("read_access", False)
        if type(read_access) is not bool:
            raise ContractError("Xcelium read_access must be a boolean")
        raw = config.get("outputs", ())
        if not isinstance(raw, tuple) or any(not isinstance(name, str) for name in raw):
            raise ContractError("Xcelium outputs must be an array of relative paths")
        outputs = tuple(_relative(name, "Xcelium output") for name in raw)
        reserved = {"stdout.log", "stderr.log", "xrun.log", "summary.json"}
        if len(set(outputs)) != len(outputs) or any(name in reserved for name in outputs):
            raise ContractError("Xcelium outputs must be unique and cannot replace adapter reports")
        return read_access, outputs

    def contract(self, project: Project, step: Step) -> StepContract:
        config = _strict_config(step, self._fields, optional=frozenset({"read_access", "outputs"}))
        HdlCompilation.resolve(config.get("hdl"), {item.reference: item.path for item in step.source_closure})
        _text(config, "success_marker")
        _positive_integer(config, "timeout_seconds")
        _, outputs = self._observation(config)
        return StepContract(produces=(*(ArtifactProduct("xcelium", "log.cadence-xcelium", path=f"xcelium/{name}")
            for name in ("stdout.log", "stderr.log", "xrun.log")),
            ArtifactProduct("xcelium", "summary.cadence-xcelium", path="xcelium/summary.json"),
            *(ArtifactProduct("simulation-output", "data.cadence-xcelium", path=f"xcelium/{name}") for name in outputs)))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        return (_executable_check(resources, _XRUN),)

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        del project
        config = _strict_config(step, self._fields, optional=frozenset({"read_access", "outputs"}))
        action = _XceliumAction(
            HdlCompilation.resolve(step.config.get("hdl"), {item.reference: item.path for item in step.source_closure}),
            _text(config, "success_marker"),
            _positive_integer(config, "timeout_seconds"),
            *self._observation(config),
        )
        return AdapterPreparation(
            action=action,
            resources=_runtime_bindings(resources, _XRUN),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.xcelium import execute_xcelium_invocation

        step.validate_action()
        action = step.action
        if not isinstance(action, _XceliumAction):
            raise ExecutionError("Xcelium Step has no typed action")
        source_names = action.hdl.sources
        sources = tuple(context.source_path(source) for source in source_names)
        timeout, marker = action.timeout_seconds, action.success_marker
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
                    *(["-access", "+r"] if action.read_access else []),
                    "-timescale",
                    "1ns/1ps",
                    "-xmlibdirname",
                    library,
                    "-log",
                    f"{work}/xrun.log",
                    "-top", action.hdl.top,
                    *(arg for directory in action.hdl.include_dirs for arg in ("-incdir", str(context.source_directory / directory))),
                    *(arg for name, value in action.hdl.defines for arg in ("-define", name + ("=" + value if value else ""))),
                    *(str(source) for source in sources),
                ],
                resources=context.runtime,
                environment_values=context.runtime.environment,
                timeout=timeout,
                output_names=action.outputs,
            )
        artifacts = (
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "xcelium/stdout.log", completed.stdout),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "xcelium/stderr.log", completed.stderr or ""),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "xcelium/xrun.log", completed.native_log),
            ),
            Artifact(
                "xcelium",
                "summary.cadence-xcelium",
                completed.run_summary,
            ),
        )
        artifacts += tuple(Artifact("simulation-output", "data.cadence-xcelium", path)
                           for _, path in completed.output_files)
        outputs_complete = {name for name, _ in completed.output_files} == set(action.outputs)
        return (
            StepResult.succeeded(artifacts=artifacts)
            if completed.passed and outputs_complete
            else StepResult(
                "failed",
                artifacts,
                message="Xcelium did not prove a successful declared testbench",
            )
        )
