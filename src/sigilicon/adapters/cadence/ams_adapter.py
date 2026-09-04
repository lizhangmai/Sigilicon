"""Xcelium mixed-signal execution adapter."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, CADENCE_SPECTRE_TOOL, ContractError, ExecutionError,
    ExecutionIO, PreflightCheck, Resources, Step, StepResult,
    _CadenceInputs, _CadencePlanningProject, _XRUN, _executable_check,
    _positive_integer, _prepare_cadence_inputs, _relative, _strict_config,
    _text, canonical_digest, json, owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.adapters.cadence.xcelium_ams import XceliumAmsCellPlan


@dataclass(frozen=True)
class _XceliumAmsAction:
    plan: XceliumAmsCellPlan
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, XceliumAmsCellPlan):
            raise ContractError("Xcelium AMS action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "xcelium-ams", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class XceliumAmsAdapter:
    """Execute one locked-release Verilog-AMS migration cell."""

    name = "cadence.xcelium-ams"
    _fields = frozenset({"owner", "cell", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        cell = _relative(_text(config, "cell"), "verification cell")
        if cell not in step.sources:
            raise ContractError("Xcelium AMS cell must be inside the source closure")
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("Xcelium AMS execution requires an evidence envelope")
        return (
            _executable_check(resources, _XRUN),
            _executable_check(resources, CADENCE_SPECTRE_TOOL),
        )

    def prepare(
        self,
        project: _CadencePlanningProject,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.adapters.cadence.xcelium_ams import plan_xcelium_ams_cell

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        contract = selected_owner.root / _relative(
            _text(config, "cell"), "verification cell"
        )
        planning = plan_xcelium_ams_cell(
            contract,
            project=project,
            resources=resources,
        )
        required = frozenset(
            {
                *planning.source_records,
                *planning.sources,
                planning.circuit_netlist,
                *planning.model_set.paths,
            }
        )
        if required != frozenset(planning.source_records):
            raise ContractError("Xcelium AMS plan source snapshot is incomplete")
        prepared = _prepare_cadence_inputs(
            project,
            step,
            resources,
            owner=owner,
            plan_identity=canonical_digest(planning.as_dict()),
            source_records=planning.source_records,
            resource_identities=planning.resource_identities,
            runtime_identities=(_XRUN, CADENCE_SPECTRE_TOOL),
        )
        return prepared.bind(_XceliumAmsAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.xcelium_ams import execute_xcelium_ams_cell

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _XceliumAmsAction):
            raise ExecutionError("Xcelium AMS Step has no typed action")
        action.inputs.validate(context)
        planning = action.plan
        with owned_scratch_directory(
            prefix=f"sigilicon-xcelium-ams-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = context.workspace(
                "xcelium-ams",
                {
                    "owner": owner,
                    "cell": str(config["cell"]),
                },
                tool_work_root=scratch.path,
            )
            bound_sources = dict(action.inputs.source_paths(context))
            bound_sources.update(action.inputs.resource_paths(context))
            result = execute_xcelium_ams_cell(
                planning,
                artifacts=artifacts,
                resources=context.runtime,
                source_paths=bound_sources,
                environment_values=context.runtime.environment,
                timeout=_positive_integer(config, "timeout_seconds"),
            )
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("Xcelium AMS lost its evidence envelope")
        context.write_text(
            "xcelium-ams",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "cadence-execution-evidence",
                    "plan_identity": context.plan_identity,
                    "tool": "xcelium-ams",
                    "cell": planning.spec.cell,
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "passed": result.passed,
                    "product_qualification_conclusion": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = context.output_artifacts(
            "xcelium-ams", "evidence.xcelium-ams"
        )
        return (
            StepResult.succeeded(artifacts=published)
            if result.passed
            else StepResult(
                "failed",
                published,
                message=(
                    "Xcelium AMS did not prove the declared migration testbench"
                ),
            )
        )
