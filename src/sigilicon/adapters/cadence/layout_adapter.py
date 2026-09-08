"""Custom-layout generation and verification adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from typing import Any
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.project import Project
from sigilicon.canonical import canonical_digest
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty
from sigilicon.adapters.cadence._common import (
    _BRIDGE_RESOURCES,
    _CadenceInputs,
    _OA_CAPABILITIES,
    _PYTHON,
    _bridge_check,
    _capability_checks,
    _executable_check,
    _positive_integer,
    _prepare_cadence_inputs,
    _relative,
    _strict_config,
    _text,
)
from sigilicon.adapters.cadence.layout_generation import LayoutPlanningResult


@dataclass(frozen=True)
class _LayoutAction:
    plan: LayoutPlanningResult
    inputs: _CadenceInputs
    owner: str
    spec: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.plan, LayoutPlanningResult):
            raise ContractError("layout action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "layout", "inputs": self.inputs.record, "owner": self.owner, "spec": self.spec, "timeout_seconds": self.timeout_seconds}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class LayoutAdapter:
    """Generate one source-authored layout through a bound OA mutation lease."""

    name = "cadence.layout"
    _fields = frozenset({"owner", "spec", "timeout_seconds"})

    def _configuration(self, step: Step):
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError("layout spec must be inside the operation source closure")
        _positive_integer(config, "timeout_seconds")
        return config

    def contract(self, project: Project, step: Step) -> StepContract:
        self._source_plan(project, step)
        return StepContract(produces=(ArtifactProduct("layout", "evidence.cadence-layout", "many"), ArtifactProduct("lvs-source", "netlist.cdl", path="source.cdl")))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._configuration(step)
        return (
            _bridge_check(resources),
            _executable_check(resources, _PYTHON),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def _source_plan(self, project: Project, step: Step, resources: Resources | None = None):
        from sigilicon.domain.platform import (
            load_platforms,
        )
        from sigilicon.adapters.cadence.layout_generation import plan_layout_spec

        initial = step
        config = self._configuration(initial)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        platforms = load_platforms(project, resources=resources)
        planning = plan_layout_spec(
            spec,
            project=project,
            platform=platforms,
        )
        if planning.spec.pdk.oa is None:
            raise ContractError("OA layout generation requires a platform OA capability")
        return config, planning

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import platform_resource_identities
        config, planning = self._source_plan(project, step, resources)
        owner = config["owner"]
        prepared = _prepare_cadence_inputs(
            project,
            resources,
            owner=owner,
            plan_identity=canonical_digest(
                {
                    "library": planning.spec.library,
                    "cell": planning.spec.cell,
                    "view": planning.spec.view,
                    "generator": planning.spec.generator,
                    "stage": planning.spec.stage,
                }
            ),
            source_records=planning.source_records,
            resource_identities=platform_resource_identities(planning.spec.pdk),
            runtime_identities=(*_BRIDGE_RESOURCES, _PYTHON),
        )
        return prepared.bind(_LayoutAction(planning, prepared.inputs, owner, _text(config, "spec"), _positive_integer(config, "timeout_seconds")))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.layout_generation import build_managed_layout_ir

        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _LayoutAction):
            raise ExecutionError("layout Step has no typed action")
        action.inputs.validate(context)
        planning = build_managed_layout_ir(
            action.plan,
            source_paths=action.inputs.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        return self._execute(context, planning)

    def _execute(self, context: ExecutionIO, planning: Any) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.layout_generation import generate_layout, render_canonical_source_cdl

        action = context.step.action
        if not isinstance(action, _LayoutAction):
            raise ExecutionError("layout Step has no typed action")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-layout-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.workspace(
                    "layout",
                    {"owner": action.owner, "spec": action.spec},
                    tool_work_root=scratch.path,
                )
                result = generate_layout(
                    planning,
                    get_client(context.runtime),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.register_mutation,
                    record_uncertainty=uncertainty.append,
                    timeout=action.timeout_seconds,
                )
        except Exception:
            published = context.output_artifacts(
                "layout", "evidence.cadence-layout", directory="layout",
            )
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    message=" | ".join(uncertainty),
                )
            raise
        published = context.output_artifacts(
            "layout", "evidence.cadence-layout", directory="layout",
        )
        if not published:
            raise ExecutionError("layout generation produced no managed evidence")
        path = context.write_text("lvs-source", "source.cdl", render_canonical_source_cdl(planning.spec))
        return StepResult.succeeded(artifacts=(*published, Artifact("lvs-source", "netlist.cdl", path)))
