"""Custom-layout generation and verification adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, Any, ContractError, ExecutionError, ExecutionIO,
    PreflightCheck, Resources, Step, StepResult, _BRIDGE_RESOURCES,
    _CadenceInputs, _OA_CAPABILITIES, Project,
    _PYTHON, _bridge_check, _capability_checks, _executable_check,
    _positive_integer, _prepare_cadence_inputs, _relative, _strict_config,
    _text, canonical_digest, owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.adapters.cadence.layout_generation import LayoutPlanningResult


@dataclass(frozen=True)
class _LayoutAction:
    plan: LayoutPlanningResult
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, LayoutPlanningResult):
            raise ContractError("layout action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "layout", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class LayoutAdapter:
    """Generate one source-authored layout through a bound OA mutation lease."""

    name = "cadence.layout"
    _fields = frozenset({"owner", "spec", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError("layout spec must be inside the operation source closure")
        _positive_integer(config, "timeout_seconds")
        return (
            _bridge_check(resources),
            _executable_check(resources, _PYTHON),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import (
            load_platforms,
            platform_resource_identities,
        )
        from sigilicon.adapters.cadence.layout_generation import plan_layout_spec

        initial = step
        config = _strict_config(initial, self._fields)
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
        prepared = _prepare_cadence_inputs(
            project,
            step,
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
        return prepared.bind(_LayoutAction(planning, prepared.inputs))

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

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-layout-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.workspace(
                    "layout",
                    {"owner": owner, "spec": str(config["spec"])},
                    tool_work_root=scratch.path,
                )
                result = generate_layout(
                    planning,
                    get_client(context.runtime),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.register_mutation,
                    record_uncertainty=uncertainty.append,
                    timeout=_positive_integer(config, "timeout_seconds"),
                )
        except Exception:
            published = context.output_artifacts(
                "layout", "evidence.cadence-layout"
            )
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    message=" | ".join(uncertainty),
                )
            raise
        published = context.output_artifacts(
            "layout", "evidence.cadence-layout"
        )
        if not published:
            raise ExecutionError("layout generation produced no managed evidence")
        context.write_text("lvs-source", "source.cdl", render_canonical_source_cdl(planning.spec))
        return StepResult.succeeded(artifacts=(*published, *context.output_artifacts("lvs-source", "netlist.cdl")))
