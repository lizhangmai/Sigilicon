"""Experimental Flow Adapter for the in-process reference PNR implementation."""

from __future__ import annotations

from collections.abc import Mapping

from sigilicon.flow.adapter_result import complete_staged_run
from sigilicon.flow.evidence import FactSource
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.physical_design import (
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
)
from sigilicon.experimental.reference_pnr.flow import PHYSICAL_CLOSURE_EVIDENCE_KIND
from sigilicon.experimental.reference_pnr.engine import ENGINE_NAME, run
from sigilicon.experimental.reference_pnr.model import (
    PlacementRoutingClosureEvidence,
    PlacementRoutingTerminationReason,
    ReferencePnrExecutionPolicy,
    ReferencePnrJob,
    ReferencePnrResult,
    RoutingTerminationReason,
)
from sigilicon.experimental.reference_pnr.serialization import (
    physical_closure_evidence_id,
    placement_routing_closure_evidence_from_json,
)
from sigilicon.layout.physical_design import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    ResultStatus,
)
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
)


def _read_job(path) -> PhysicalDesignJob:
    try:
        return physical_design_job_from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(f"invalid Physical Design Job artifact: {exc}") from exc


def _read_result(path) -> PhysicalDesignResult:
    try:
        return physical_design_result_from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid Physical Design Result artifact: {exc}"
        ) from exc


_REFERENCE_POLICY_FIELDS = (
    "maximum_search_states",
    "maximum_route_states",
    "maximum_routing_iterations",
    "maximum_placement_repair_states",
    "maximum_placement_repair_iterations",
    "routing_congestion_bins_x",
    "routing_congestion_bins_y",
)


def _reference_job(
    job: PhysicalDesignJob,
    context: ActionContext,
) -> ReferencePnrJob:
    raw_policy = context.action_config.get("reference-policy")
    if raw_policy is None:
        policy = ReferencePnrExecutionPolicy()
    elif not isinstance(raw_policy, Mapping):
        raise FlowExecutionError("reference-policy must be an object")
    else:
        if set(raw_policy) != set(_REFERENCE_POLICY_FIELDS):
            raise FlowExecutionError(
                "reference-policy fields must be exactly "
                + repr(sorted(_REFERENCE_POLICY_FIELDS))
            )
        if any(type(raw_policy[name]) is not int for name in _REFERENCE_POLICY_FIELDS):
            raise FlowExecutionError("reference-policy values must be integers")
        policy = ReferencePnrExecutionPolicy(
            **{name: raw_policy[name] for name in _REFERENCE_POLICY_FIELDS}
        )
    return ReferencePnrJob(
        technology=job.technology,
        design=job.design,
        constraints=job.constraints,
        request=job.request,
        routing_constraints=job.routing_constraints,
        execution_policy=policy,
    )


def _stable_result(result: ReferencePnrResult) -> PhysicalDesignResult:
    return PhysicalDesignResult(
        status=result.status,
        placements=result.placements,
        constraint_outcomes=result.constraint_outcomes,
        stage_reports=result.stage_reports,
        provenance=result.provenance,
        routes=result.routes,
        routing_blockage_placements=result.routing_blockage_placements,
        closed=result.closed,
        artifact_id=result.artifact_id,
    )


def _completion_values(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    evidence: PlacementRoutingClosureEvidence | None,
) -> dict[str, object]:
    routing_requested = any(stage.value == "routing" for stage in job.request.stages)
    if routing_requested and evidence is None:
        raise FlowExecutionError("routed reference result omitted closure evidence")
    if not routing_requested and evidence is not None:
        raise FlowExecutionError("placement-only reference result emitted closure evidence")
    if evidence is not None:
        evidence_closed = (
            evidence.termination is PlacementRoutingTerminationReason.CLOSED
            and evidence.routing_termination is RoutingTerminationReason.CLOSED
            and evidence.quality.closed
        )
        if evidence_closed is not result.closed:
            raise FlowExecutionError(
                "reference closure evidence disagrees with Physical Design Result"
            )
    closed = result.closed
    closure_termination = "not_evaluated" if evidence is None else evidence.termination.value
    routing_termination = (
        "not_evaluated" if evidence is None else evidence.routing_termination.value
    )
    state_budget = evidence is not None and (
        evidence.routing_termination is RoutingTerminationReason.STATE_BUDGET
        or evidence.termination is PlacementRoutingTerminationReason.REPAIR_STATE_BUDGET
    )
    iteration_budget = evidence is not None and (
        evidence.routing_termination is RoutingTerminationReason.ITERATION_BUDGET
        or evidence.termination is PlacementRoutingTerminationReason.REPAIR_ITERATION_BUDGET
    )
    return {
        "physical-design-status": result.status.value,
        "physical-design-succeeded": result.status is ResultStatus.SUCCEEDED,
        "physical-design-closed": closed,
        "closure-termination": closure_termination,
        "routing-termination": routing_termination,
        "state-budget-exhausted": state_budget,
        "iteration-budget-exhausted": iteration_budget,
    }


class ReferencePhysicalDesignAdapter:
    """Run the experimental reference solver behind an explicit adapter seam."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=lambda _context: None,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            _reference_job(_read_job(context.input("job").path), context)
        except FlowExecutionError as exc:
            return (str(exc),)
        return ()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        job = _read_job(context.input("job").path)
        result = run(_reference_job(job, context))
        stable_result = _stable_result(result)
        context.output_path("result", "physical-design-result.json").write_text(
            stable_result.canonical_json(), encoding="utf-8"
        )
        if result.closure_evidence is not None:
            context.output_path(
                "closure-evidence", "physical-closure-evidence.json"
            ).write_text(result.closure_evidence.canonical_json(), encoding="utf-8")
        return AdapterExecution.succeeded()

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        job = _read_job(context.input("job").path)
        result_path = context.output_path("result", "physical-design-result.json")
        result = _read_result(result_path)
        job_identity = physical_design_job_id(job)
        result_identity = physical_design_result_id(result)
        if result.provenance.job_identity != job_identity:
            raise FlowExecutionError(
                "reference result provenance does not match Physical Design Job"
            )
        if result.provenance.backend != ENGINE_NAME:
            raise FlowExecutionError(
                "reference result provenance names a different backend"
            )
        if not result.provenance.deterministic:
            raise FlowExecutionError(
                "reference result provenance must declare deterministic execution"
            )
        if result_identity != f"{job_identity}:result":
            raise FlowExecutionError(
                "reference result identity does not match Physical Design Job"
            )
        evidence = None
        evidence_path = context.output_path(
            "closure-evidence", "physical-closure-evidence.json"
        )
        if evidence_path.exists():
            try:
                evidence = placement_routing_closure_evidence_from_json(
                    evidence_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise FlowExecutionError(
                    f"invalid physical closure evidence artifact: {exc}"
                ) from exc
        facts = context.action.fact_schema.project(
            _completion_values(job, result, evidence),
            source=FactSource(
                context.action.kind,
                context.node_id,
                result_identity,
            ),
        )
        qualifiers = {
            "job-identity": job_identity,
            "result-identity": result_identity,
            "deterministic": result.provenance.deterministic,
        }
        artifacts = [
            ProducedArtifact(
                "result", PHYSICAL_DESIGN_RESULT_KIND, result_path, qualifiers=qualifiers
            )
        ]
        if evidence is not None:
            if evidence.artifact_id != f"{result_identity}:closure-evidence":
                raise FlowExecutionError(
                    "closure evidence identity does not match Physical Design Result"
                )
            artifacts.append(
                ProducedArtifact(
                    "closure-evidence",
                    PHYSICAL_CLOSURE_EVIDENCE_KIND,
                    evidence_path,
                    qualifiers={
                        **qualifiers,
                        "closure-identity": physical_closure_evidence_id(evidence),
                    },
                )
            )
        return CollectedActionResult(status="valid", artifacts=tuple(artifacts), facts=facts)


__all__ = ["ReferencePhysicalDesignAdapter"]
