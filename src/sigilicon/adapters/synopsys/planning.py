"""Compile owner runner contracts into immutable tool invocations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import ClassVar, Self

from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution._values import ContractError
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.project import Project
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
    _source_members,
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
    def record(self) -> dict[str, object]:
        return {"kind": self.kind, **asdict(self)}


class RunnerAdapter:
    """Shared compile/readiness boundary for the four owner runner protocols."""

    action_type: type[Action]

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


def hdl_sources(step: Step, field: str, *, required: bool = True) -> tuple[str, ...]:
    # Preserve fileset order, including packages before modules and mixed HDL suffixes.
    selected = _source_members(step, field, suffix=(".v", ".sv"))
    if required and not selected:
        raise ContractError(f"{field} must select at least one Verilog or SystemVerilog source")
    return selected


@dataclass(frozen=True)
class VcsAction(Action):
    kind: ClassVar[str] = "synopsys.vcs"
    invocation: Invocation
    target: str
    rtl: tuple[str, ...]
    testbench: tuple[str, ...]
    success_marker: str
    synthesis_step: str | None

    @classmethod
    def compile(cls, step: Step) -> VcsAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "target", "rtl_root",
            "testbench_root", "success_marker", "synthesis_step",
        }))
        target = _target(config)
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_VCS"), target,
            hdl_sources(step, "rtl_root", required=target != "gate"),
            hdl_sources(step, "testbench_root", required=target != "structural"),
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
    rtl: tuple[str, ...]
    reports: tuple[str, ...]
    verdict: str

    @classmethod
    def compile(cls, step: Step) -> DcAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "corner", "constraints",
            "evaluator", "rtl_root", "reports", "verdict_report",
        }))
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_DC_SHELL"), _text(config, "corner"),
            source(step, "constraints"), source(step, "evaluator"),
            hdl_sources(step, "rtl_root"),
            tuple(_safe_relative(name, "DC report") for name in _strings(config, "reports")),
            _safe_relative(_text(config, "verdict_report"), "DC verdict report"),
        )


FC_OUTPUT_ENVIRONMENT = MappingProxyType({
    "routed-netlist": "SIGILICON_FC_ROUTED_NETLIST",
    "routed-constraints": "SIGILICON_FC_ROUTED_CONSTRAINTS",
    "layout-stream": "SIGILICON_FC_GDS",
    "checkpoint": "SIGILICON_FC_CHECKPOINT",
    "design-check-report": "SIGILICON_FC_DESIGN_CHECK_REPORT",
    "structural-report": "SIGILICON_FC_STRUCTURAL_REPORT",
    "qor-report": "SIGILICON_FC_QOR_REPORT",
    "timing-report": "SIGILICON_FC_TIMING_REPORT",
    "area-report": "SIGILICON_FC_AREA_REPORT",
    "power-report": "SIGILICON_FC_POWER_REPORT",
    "drc-report": "SIGILICON_FC_DRC_REPORT",
    "physical-completion-report": "SIGILICON_FC_PHYSICAL_COMPLETION_REPORT",
    "tie-off-check-report": "SIGILICON_FC_TIE_OFF_CHECK_REPORT",
    "execution-verdict": "SIGILICON_FC_EXECUTION_VERDICT",
})


@dataclass(frozen=True)
class FcAction(Action):
    kind: ClassVar[str] = "synopsys.fc"
    invocation: Invocation
    target: str
    corner: str
    top: str
    reference_library: str
    synthesis_step: str | None
    reference_step: str | None
    evaluator: str | None
    outputs: tuple[Output, ...]

    @classmethod
    def compile(cls, step: Step) -> FcAction:
        config = _strict_config(step, frozenset({
            "runner", "variant", "timeout_seconds", "target", "corner", "top",
            "reference_library_output", "synthesis_step", "reference_step",
            "evaluator", "outputs",
        }))
        target = _target(config)
        if target not in {"library", "pnr"}:
            raise ContractError(f"unsupported FC target {target!r}")
        outputs = _mapping(config, "outputs")
        if target == "pnr" and set(outputs) != set(FC_OUTPUT_ENVIRONMENT):
            raise ContractError("FC outputs do not match the physical result contract")
        if target == "library" and outputs:
            raise ContractError("FC library action cannot declare physical outputs")
        return cls(
            Invocation.compile(step, "SIGILICON_SYNOPSYS_LM_SHELL" if target == "library"
                               else "SIGILICON_SYNOPSYS_FC_SHELL"),
            target, _text(config, "corner"), _text(config, "top"),
            _safe_relative(_text(config, "reference_library_output"), "reference library output"),
            dependency(step, "synthesis_step") if target == "pnr" else None,
            dependency(step, "reference_step") if target == "pnr" else None,
            source(step, "evaluator") if target == "pnr" else None,
            tuple(Output(role, _safe_relative(path, f"FC output {role}"))
                  for role, path in sorted(outputs.items())),
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
