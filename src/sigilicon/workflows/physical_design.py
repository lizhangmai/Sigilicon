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
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND,
    MATERIALIZATION_RECEIPT_KIND,
    PHYSICAL_CLOSURE_EVIDENCE_KIND,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
)
from sigilicon.layout.materialization import (
    MaterializationPlan,
    MaterializationTarget,
    compile_materialization_plan,
    materialization_acceptance_from_json,
    materialization_plan_from_json,
    materialization_target_from_mapping,
    validate_materialization_plan,
)
from sigilicon.layout.materialization_execution import (
    MaterializationCompletion,
    MaterializationExecutionStatus,
    MaterializationExecutionTarget,
    MaterializationReceipt,
    identify_managed_layout,
    issue_materialization_receipt,
    materialization_execution_target_from_mapping,
    materialization_receipt_from_json,
    validate_materialization_receipt,
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


def _materialization_facts(plan: MaterializationPlan) -> dict[str, object]:
    return {
        "materialization-decision": plan.acceptance.decision.value,
        "materialization-reason": plan.acceptance.reason.value,
        "materialization-executable": plan.executable,
    }


class MaterializationPlanAdapter:
    """Compile typed P&R artifacts without writing a layout database."""

    def _inputs(
        self,
        context: ActionContext,
    ) -> tuple[PhysicalDesignJob, PhysicalDesignResult, MaterializationTarget]:
        if set(context.action_config) != {"target"}:
            raise FlowExecutionError(
                "materialization Action config must contain only 'target'"
            )
        job = _read_job(context.input("job").path)
        result = _read_result(context.input("result").path)
        try:
            target = materialization_target_from_mapping(
                context.action_config["target"]
            )
        except (ValueError, TypeError) as exc:
            raise FlowExecutionError(f"invalid materialization target: {exc}") from exc
        return job, result, target

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            self._inputs(context)
        except FlowExecutionError as exc:
            return (str(exc),)
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        job, result, target = self._inputs(context)
        try:
            plan = compile_materialization_plan(job, result, target)
        except ValueError as exc:
            raise FlowExecutionError(
                f"cannot compile Materialization Plan: {exc}"
            ) from exc
        context.output_path("plan", "materialization-plan.json").write_text(
            plan.canonical_json(),
            encoding="utf-8",
        )
        context.output_path(
            "acceptance-evidence",
            "materialization-acceptance.json",
        ).write_text(plan.acceptance.canonical_json(), encoding="utf-8")
        return AdapterExecution.succeeded(details=_materialization_facts(plan))

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        job, result, _target = self._inputs(context)
        plan_path = context.output_path("plan", "materialization-plan.json")
        acceptance_path = context.output_path(
            "acceptance-evidence",
            "materialization-acceptance.json",
        )
        try:
            plan = materialization_plan_from_json(
                plan_path.read_text(encoding="utf-8")
            )
            acceptance = materialization_acceptance_from_json(
                acceptance_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise FlowExecutionError(
                f"invalid Materialization Plan artifact: {exc}"
            ) from exc
        validation = validate_materialization_plan(job, result, plan)
        if not validation.valid:
            raise FlowExecutionError(
                "collected Materialization Plan failed validation: "
                + "; ".join(item.code for item in validation.issues)
            )
        if acceptance != plan.acceptance:
            raise FlowExecutionError(
                "acceptance evidence disagrees with Materialization Plan"
            )
        facts = _materialization_facts(plan)
        if dict(execution.details) != facts:
            raise FlowExecutionError(
                "materialization execution details disagree with collected plan"
            )
        qualifiers = {
            "job-sha256": plan.provenance.job_sha256,
            "result-sha256": plan.provenance.result_sha256,
            "plan-sha256": canonical_sha256(plan),
            "executable": plan.executable,
        }
        return CollectedActionResult(
            status="valid",
            artifacts=(
                ProducedArtifact(
                    "plan",
                    PHYSICAL_MATERIALIZATION_PLAN_KIND,
                    plan_path,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "acceptance-evidence",
                    MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND,
                    acceptance_path,
                    qualifiers={
                        **qualifiers,
                        "acceptance-sha256": canonical_sha256(acceptance),
                    },
                ),
            ),
            facts=facts,
        )


def read_materialization_execution_request(
    context: ActionContext,
) -> tuple[
    PhysicalDesignJob,
    PhysicalDesignResult,
    MaterializationPlan,
    MaterializationExecutionTarget,
]:
    """Read the strict artifacts shared by every real materializer Adapter."""

    if set(context.action_config) != {"target"}:
        raise FlowExecutionError(
            "materialization execution config must contain only 'target'"
        )
    job = _read_job(context.input("job").path)
    result = _read_result(context.input("result").path)
    try:
        plan = materialization_plan_from_json(
            context.input("plan").path.read_text(encoding="utf-8")
        )
        target = materialization_execution_target_from_mapping(
            context.action_config["target"]
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid materialization execution request: {exc}"
        ) from exc
    return job, result, plan, target


def materialization_execution_facts(
    receipt: MaterializationReceipt,
) -> dict[str, object]:
    return {
        "materialization-status": receipt.status.value,
        "materialized": receipt.materialized,
        "backend-executed": receipt.completion.executed,
        "backend-completed": receipt.completion.proven,
    }


def write_materialization_receipt(
    context: ActionContext,
    *,
    status: MaterializationExecutionStatus,
    completion: MaterializationCompletion,
    message: str,
    layout_path: Path | None = None,
) -> MaterializationReceipt:
    """Bind one Adapter outcome to the exact request and managed layout bytes."""

    job, result, plan, target = read_materialization_execution_request(context)
    layout = (
        None
        if layout_path is None
        else identify_managed_layout(
            layout_path,
            target=target,
            run_root=context.run_root,
            run_id=context.run_root.name,
            producer=context.node_id,
        )
    )
    receipt = issue_materialization_receipt(
        job,
        result,
        plan,
        target,
        status=status,
        completion=completion,
        layout=layout,
        message=message,
    )
    context.output_path("receipt", "materialization-receipt.json").write_text(
        receipt.canonical_json(),
        encoding="utf-8",
    )
    return receipt


def collect_materialization_execution_result(
    context: ActionContext,
    execution: AdapterExecution,
) -> CollectedActionResult:
    """Collect and independently revalidate one materialization receipt."""

    job, result, plan, target = read_materialization_execution_request(context)
    receipt_path = context.output_path("receipt", "materialization-receipt.json")
    try:
        receipt = materialization_receipt_from_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid Materialization Receipt artifact: {exc}"
        ) from exc
    layout_path = (
        None
        if receipt.layout is None
        else context.output_path("layout", "layout.gds")
    )
    validation = validate_materialization_receipt(
        job,
        result,
        plan,
        target,
        receipt,
        layout_path=layout_path,
        run_root=context.run_root if layout_path is not None else None,
    )
    if not validation.valid:
        raise FlowExecutionError(
            "collected Materialization Receipt failed validation: "
            + "; ".join(issue.code for issue in validation.issues)
        )
    facts = materialization_execution_facts(receipt)
    if dict(execution.details) != facts:
        raise FlowExecutionError(
            "materialization execution details disagree with collected receipt"
        )
    receipt_sha256 = canonical_sha256(receipt)
    qualifiers = {
        "owner": target.owner,
        "name": target.name,
        "format": target.format.value,
        "job-sha256": receipt.provenance.job_sha256,
        "result-sha256": receipt.provenance.result_sha256,
        "plan-sha256": receipt.provenance.plan_sha256,
        "receipt-sha256": receipt_sha256,
        "status": receipt.status.value,
        "backend": receipt.completion.backend,
    }
    artifacts = [
        ProducedArtifact(
            "receipt",
            MATERIALIZATION_RECEIPT_KIND,
            receipt_path,
            qualifiers=qualifiers,
        )
    ]
    if receipt.layout is not None:
        assert layout_path is not None
        artifacts.insert(
            0,
            ProducedArtifact(
                "layout",
                MATERIALIZED_GDS_KIND,
                layout_path,
                qualifiers={
                    **qualifiers,
                    "layout-sha256": receipt.layout.content_sha256,
                },
            ),
        )
    return CollectedActionResult(
        status="valid",
        artifacts=tuple(artifacts),
        facts=facts,
    )


__all__ = [
    "MaterializationPlanAdapter",
    "ReferencePhysicalDesignAdapter",
    "collect_materialization_execution_result",
    "materialization_execution_facts",
    "read_materialization_execution_request",
    "write_materialization_receipt",
]
