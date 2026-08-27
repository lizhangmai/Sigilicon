"""Flow Adapter for the in-process reference physical-design Module."""

from __future__ import annotations

from pathlib import Path

from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.physical_design import (
    PHYSICAL_CLOSURE_EVIDENCE_KIND,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
)
from sigilicon.layout.pnr import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    PlacementRoutingTerminationReason,
    PnrStage,
    ResultStatus,
    RoutingTerminationReason,
    run,
)
from sigilicon.layout.pnr.serialization import (
    canonical_sha256,
    physical_design_job_from_json,
    physical_design_result_from_json,
    placement_routing_closure_evidence_from_json,
)


def _read_job(path: Path) -> PhysicalDesignJob:
    try:
        return physical_design_job_from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(f"invalid Physical Design Job artifact: {exc}") from exc


def _read_result(path: Path) -> PhysicalDesignResult:
    try:
        return physical_design_result_from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid Physical Design Result artifact: {exc}"
        ) from exc


def _completion_facts(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
) -> dict[str, object]:
    evidence = result.closure_evidence
    routing_requested = PnrStage.ROUTING in job.request.stages
    if routing_requested and result.status is ResultStatus.SUCCEEDED:
        if evidence is None:
            raise FlowExecutionError(
                "successful routed result omitted typed closure evidence"
            )
        closed = (
            evidence.termination is PlacementRoutingTerminationReason.CLOSED
            and evidence.routing_termination is RoutingTerminationReason.CLOSED
            and evidence.quality.closed
        )
    else:
        closed = result.status is ResultStatus.SUCCEEDED and not routing_requested

    closure_termination = (
        "not_evaluated" if evidence is None else evidence.termination.value
    )
    routing_termination = (
        "not_evaluated" if evidence is None else evidence.routing_termination.value
    )
    state_budget = evidence is not None and (
        evidence.routing_termination is RoutingTerminationReason.STATE_BUDGET
        or evidence.termination
        is PlacementRoutingTerminationReason.REPAIR_STATE_BUDGET
    )
    iteration_budget = evidence is not None and (
        evidence.routing_termination is RoutingTerminationReason.ITERATION_BUDGET
        or evidence.termination
        is PlacementRoutingTerminationReason.REPAIR_ITERATION_BUDGET
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
    """Run pure P&R over one managed job and emit canonical typed artifacts."""

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            _read_job(context.input("job").path)
        except FlowExecutionError as exc:
            return (str(exc),)
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        job = _read_job(context.input("job").path)
        result = run(job)
        result_path = context.output_path("result", "physical-design-result.json")
        result_path.write_text(result.canonical_json(), encoding="utf-8")
        if result.closure_evidence is not None:
            evidence_path = context.output_path(
                "closure-evidence",
                "physical-closure-evidence.json",
            )
            evidence_path.write_text(
                result.closure_evidence.canonical_json(),
                encoding="utf-8",
            )
        return AdapterExecution.succeeded(details=_completion_facts(job, result))

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        job = _read_job(context.input("job").path)
        result_path = context.output_path("result", "physical-design-result.json")
        result = _read_result(result_path)
        facts = _completion_facts(job, result)
        if dict(execution.details) != facts:
            raise FlowExecutionError(
                "reference P&R execution details disagree with collected result"
            )
        result_qualifiers = {
            "job-sha256": canonical_sha256(job),
            "result-sha256": canonical_sha256(result),
            "input-sha256": result.provenance.input_sha256,
            "execution-sha256": result.provenance.execution_sha256,
            "deterministic": result.provenance.deterministic,
        }
        artifacts = [
            ProducedArtifact(
                "result",
                PHYSICAL_DESIGN_RESULT_KIND,
                result_path,
                qualifiers=result_qualifiers,
            )
        ]
        if result.closure_evidence is not None:
            evidence_path = context.output_path(
                "closure-evidence",
                "physical-closure-evidence.json",
            )
            try:
                evidence = placement_routing_closure_evidence_from_json(
                    evidence_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise FlowExecutionError(
                    f"invalid physical closure evidence artifact: {exc}"
                ) from exc
            if evidence != result.closure_evidence:
                raise FlowExecutionError(
                    "closure evidence artifact disagrees with Physical Design Result"
                )
            artifacts.append(
                ProducedArtifact(
                    "closure-evidence",
                    PHYSICAL_CLOSURE_EVIDENCE_KIND,
                    evidence_path,
                    qualifiers={
                        **result_qualifiers,
                        "closure-sha256": canonical_sha256(evidence),
                    },
                )
            )
        return CollectedActionResult(
            status="valid",
            artifacts=tuple(artifacts),
            facts=facts,
        )


__all__ = ["ReferencePhysicalDesignAdapter"]
