"""Xcelium mixed-signal execution adapter."""

from __future__ import annotations

from dataclasses import dataclass

import json
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from sigilicon.external_tools import (
    CADENCE_SPECTRE_TOOL,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.execution._result import StepResult
from sigilicon.project import Project
from sigilicon.canonical import canonical_digest
from sigilicon.adapters.cadence._common import (
    _CadenceInputs,
    _XRUN,
    _executable_check,
    _positive_integer,
    _prepare_cadence_inputs,
    _relative,
    _strict_config,
    _text,
)
from sigilicon.adapters.cadence.xcelium_ams import XceliumAmsCellPlan


@dataclass(frozen=True)
class _XceliumAmsAction:
    plan: XceliumAmsCellPlan
    inputs: _CadenceInputs
    owner: str
    cell: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.plan, XceliumAmsCellPlan):
            raise ContractError("Xcelium AMS action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "xcelium-ams", "inputs": self.inputs.record, "owner": self.owner, "cell": self.cell, "timeout_seconds": self.timeout_seconds}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class XceliumAmsAdapter:
    """Execute one locked-release Verilog-AMS migration cell."""

    name = "cadence.xcelium-ams"
    _fields = frozenset({"owner", "cell", "timeout_seconds"})

    def _configuration(self, step: Step):
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        cell = _relative(_text(config, "cell"), "verification cell")
        if cell not in step.sources:
            raise ContractError("Xcelium AMS cell must be inside the source closure")
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("Xcelium AMS execution requires an evidence envelope")
        return config

    def contract(self, project: Project, step: Step) -> StepContract:
        config = self._configuration(step)
        from sigilicon.domain.verification_cell import load_verification_cell
        from sigilicon.adapters.cadence.xcelium_ams import _AMS_HDL_SUFFIXES
        contract = project.owner(config["owner"]).root / config["cell"]
        spec = load_verification_cell(contract, project=project)
        if spec.simulator.lower() != "xcelium-ams" or spec.ams is None:
            raise ContractError("verification cell must declare typed xcelium-ams inputs")
        if spec.success_marker is None:
            raise ContractError("Xcelium AMS verification cell must declare success_marker")
        if any(path.suffix.lower() not in _AMS_HDL_SUFFIXES for path in (spec.canonical_source, *spec.compile_sources)):
            raise ContractError("Spectre circuits must come from the AMS circuit selection")
        return StepContract(produces=(ArtifactProduct("xcelium-ams", "evidence.xcelium-ams", "many"),))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._configuration(step)
        return (
            _executable_check(resources, _XRUN),
            _executable_check(resources, CADENCE_SPECTRE_TOOL),
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.adapters.cadence.xcelium_ams import plan_xcelium_ams_cell

        initial = step
        config = self._configuration(initial)
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
            resources,
            owner=owner,
            plan_identity=canonical_digest(planning.as_dict()),
            source_records=planning.source_records,
            resource_identities=planning.resource_identities,
            runtime_identities=(_XRUN, CADENCE_SPECTRE_TOOL),
        )
        return prepared.bind(_XceliumAmsAction(planning, prepared.inputs, owner, _text(config, "cell"), _positive_integer(config, "timeout_seconds")))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.xcelium_ams import execute_xcelium_ams_cell

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
                    "owner": action.owner,
                    "cell": action.cell,
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
                timeout=action.timeout_seconds,
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
