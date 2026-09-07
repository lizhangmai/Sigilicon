"""Compile owner runner contracts into immutable tool invocations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import ClassVar, Self, Mapping

from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, ArtifactReference, StepContract
from sigilicon.execution._values import ContractError
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.project import Project
from sigilicon.domain.hdl import HdlCompilation
from sigilicon.adapters.synopsys._common import (
    _ENVIRONMENT,
    _ENVIRONMENT_PREFIX,
    _PYTHON,
    _RUNNER_SHELL,
    _boolean,
    _mapping,
    _positive_integer,
    _runner,
    _safe_relative,
    _strict_config,
    _strings,
    _target,
    _text,
)
from sigilicon.execution.runtime import preflight_environment


@dataclass(frozen=True)
class Invocation:
    runner: str
    variant: str
    timeout_seconds: int

    @classmethod
    def compile(cls, step: Step, tool: str) -> Invocation:
        for name in (_RUNNER_SHELL, tool):
            if name not in step.runtime.tools:
                raise ContractError(f"Synopsys runtime profile must bind {name} as a tool")
        return cls(_runner(step), _text(step.config, "variant"),
                   _positive_integer(step.config, "timeout_seconds"))


@dataclass(frozen=True)
class Output:
    role: str
    path: str
    required: bool = True


@dataclass(frozen=True)
class Action(ABC):
    kind: ClassVar[str]
    invocation: Invocation

    @classmethod
    @abstractmethod
    def compile(cls, step: Step) -> Self:
        """Validate an owner configuration before any tool starts."""
        ...

    @property
    @abstractmethod
    def contract(self) -> StepContract:
        """Artifacts guaranteed and consumed by this tool action."""
        ...

    @property
    def record(self) -> dict[str, object]:
        return {"kind": self.kind, **asdict(self)}


class RunnerAdapter:
    """Shared compile/readiness boundary for the four owner runner protocols."""

    action_type: type[Action]

    def contract(self, project: Project, step: Step) -> StepContract:
        return self.action_type.compile(step).contract

    def prepare(self, project: Project, step: Step, resources: Resources) -> AdapterPreparation:
        return AdapterPreparation(action=self.action_type.compile(step))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        action = require_action(step, self.action_type)
        return (
            PreflightCheck("owner-runner", action.invocation.runner, "ready", "sealed plan source"),
            *preflight_environment(step.runtime, resources),
        )


def require_action[T: Action](step: Step, kind: type[T]) -> T:
    if not isinstance(step.action, kind):
        raise ContractError(f"{step.uses} requires a compiled {kind.kind} action")
    step.validate_action()
    return step.action


def source(step: Step, field: str) -> str:
    selected = _safe_relative(_text(step.config, field), field)
    if selected not in step.sources:
        raise ContractError(f"{field} must be inside the step source closure")
    return selected


def dependency(step: Step, field: str) -> str:
    selected = _text(step.config, field)
    if selected not in step.needs:
        raise ContractError(f"{field} must select a declared step dependency")
    return selected


@dataclass(frozen=True)
class VcsAction(Action):
    kind: ClassVar[str] = "synopsys.vcs"
    invocation: Invocation
    target: str
    hdl: HdlCompilation
    success_marker: str
    synthesis_step: str | None

    @property
    def contract(self) -> StepContract:
        consumes = (ArtifactReference(self.synthesis_step, "mapped-netlist", "netlist.verilog"),) if self.target == "gate" else ()
        return StepContract(consumes, (ArtifactProduct("log", "log.synopsys", "many"),))

    @classmethod
    def compile(cls, step: Step) -> VcsAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "target", "hdl", "success_marker", "synthesis_step",
        }))
        target = _target(config)
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_VCS"), target,
            HdlCompilation.resolve(step.config.get("hdl"), {item.reference: item.path for item in step.source_closure}),
            _text(config, "success_marker"),
            dependency(step, "synthesis_step") if target == "gate" else None,
        )


@dataclass(frozen=True)
class DcAction(Action):
    kind: ClassVar[str] = "synopsys.dc"
    invocation: Invocation
    corner: str
    constraints: str
    evaluator: str
    hdl: HdlCompilation
    reports: tuple[str, ...]
    verdict: str

    @property
    def contract(self) -> StepContract:
        return StepContract(produces=(
            ArtifactProduct("log", "log.synopsys", "many"),
            ArtifactProduct("mapped-netlist", "netlist.verilog", path="mapped.v"),
            ArtifactProduct("mapped-constraints", "constraints.sdc", path="mapped.sdc"),
            ArtifactProduct("checkpoint", "checkpoint.synopsys-ddc", path="mapped.ddc"),
            *(ArtifactProduct("report", "report.synopsys", path=name) for name in self.reports),
            ArtifactProduct("execution-verdict", "evidence.tool-verdict", path=self.verdict)))

    @classmethod
    def compile(cls, step: Step) -> DcAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "corner", "constraints",
            "evaluator", "hdl", "reports", "verdict_report",
        }))
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_DC_SHELL"), _text(config, "corner"),
            source(step, "constraints"), source(step, "evaluator"),
            HdlCompilation.resolve(step.config.get("hdl"), {item.reference: item.path for item in step.source_closure}),
            tuple(_safe_relative(name, "DC report") for name in _strings(config, "reports")),
            _safe_relative(_text(config, "verdict_report"), "DC verdict report"),
        )


@dataclass(frozen=True)
class HspiceAction(Action):
    kind: ClassVar[str] = "synopsys.hspice"
    invocation: Invocation
    target: str
    corner: str
    model_section: str
    environment: tuple[tuple[str, str], ...]
    source_environment: tuple[tuple[str, str], ...]
    output_environment: tuple[tuple[str, str], ...]
    outputs: tuple[Output, ...]

    @property
    def contract(self) -> StepContract:
        return StepContract(produces=(ArtifactProduct("log", "log.synopsys", "many"),
            *(ArtifactProduct(output.role, "evidence.hspice" if output.required else "diagnostic.hspice",
                "one" if output.required else "many", output.path if output.required else None, output.required)
              for output in self.outputs)))

    @classmethod
    def compile(cls, step: Step) -> HspiceAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "target", "corner", "model_section",
            "environment", "environment_prefix", "requires_python", "source_environment",
            "output_environment", "collect", "diagnostics",
        }))
        prefix = _text(config, "environment_prefix")
        environment = _mapping(config, "environment")
        if _ENVIRONMENT_PREFIX.fullmatch(prefix) is None or any(
            _ENVIRONMENT.fullmatch(name) is None or not name.startswith(prefix)
            or not isinstance(value, (str, int, float, bool))
            for name, value in environment.items()
        ):
            raise ContractError("HSPICE owner environment must use its declared uppercase prefix")
        sources = _mapping(config, "source_environment")
        if any(_ENVIRONMENT.fullmatch(name) is None or value not in step.sources
               for name, value in sources.items()):
            raise ContractError("HSPICE source_environment must select step sources")
        outputs = _mapping(config, "output_environment")
        if any(_ENVIRONMENT.fullmatch(name) is None for name in outputs):
            raise ContractError("HSPICE output_environment requires uppercase names")
        if _boolean(config, "requires_python") and _PYTHON not in step.runtime.tools:
            raise ContractError(f"Python-backed HSPICE steps must bind {_PYTHON} as a tool")
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_HSPICE"), _target(config), _text(config, "corner"),
            _text(config, "model_section"),
            tuple((name, str(value)) for name, value in sorted(environment.items())),
            tuple(sorted(sources.items())),
            tuple((name, _safe_relative(path, name)) for name, path in sorted(outputs.items())),
            tuple(Output(role, _safe_relative(path, f"HSPICE {field} {role}"), field == "collect")
                  for field in ("diagnostics", "collect")
                  for role, path in sorted(_mapping(config, field).items())),
        )
