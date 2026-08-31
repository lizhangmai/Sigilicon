"""Stable materialization-plan and materialization-receipt Flow Adapters."""

from __future__ import annotations

from pathlib import Path

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
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND,
    MATERIALIZATION_RECEIPT_KIND,
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
    materialization_receipt_id,
    validate_materialization_receipt,
)
from sigilicon.layout.physical_design import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
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


def _materialization_values(plan: MaterializationPlan) -> dict[str, object]:
    return {
        "materialization-decision": plan.acceptance.decision.value,
        "materialization-reason": plan.acceptance.reason.value,
        "materialization-executable": plan.executable,
    }


class MaterializationPlanAdapter:
    """Compile typed P&R artifacts without writing a layout database."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

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

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            self._inputs(context)
        except FlowExecutionError as exc:
            return (str(exc),)
        return ()

    def _prepare(self, context: ActionContext) -> None:
        pass

    def _execute(self, context: ActionContext) -> AdapterExecution:
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
        return AdapterExecution.succeeded()

    def _collect_result(
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
        facts = context.action.fact_schema.project(
            _materialization_values(plan),
            source=FactSource(context.action.kind, context.node_id, plan.artifact_id),
        )
        qualifiers = {
            "job-identity": plan.provenance.job_identity,
            "result-identity": plan.provenance.result_identity,
            "plan-identity": plan.artifact_id,
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
                        "acceptance-identity": f"{plan.artifact_id}:acceptance",
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


def _materialization_execution_values(
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
    receipt_identity = materialization_receipt_id(receipt)
    facts = context.action.fact_schema.project(
        _materialization_execution_values(receipt),
        source=FactSource(context.action.kind, context.node_id, receipt_identity),
    )
    qualifiers = {
        "owner": target.owner,
        "name": target.name,
        "format": target.format.value,
        "job-identity": receipt.provenance.job_identity,
        "result-identity": receipt.provenance.result_identity,
        "plan-identity": receipt.provenance.plan_identity,
        "receipt-identity": receipt_identity,
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
                    "layout-identity": receipt.layout.content_identity,
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
    "collect_materialization_execution_result",
    "read_materialization_execution_request",
    "write_materialization_receipt",
]
