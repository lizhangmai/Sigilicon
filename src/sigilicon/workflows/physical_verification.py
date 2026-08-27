"""Offline Adapter for testing physical-verification Flow semantics only."""

from __future__ import annotations

from sigilicon.domain.physical_verification import (
    DrcEvidence,
    LvsEvidence,
    PhysicalVerificationStatus,
    VerificationCompletion,
    drc_evidence_from_json,
    lvs_evidence_from_json,
)
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.physical_verification import (
    DRC_ACTION,
    DRC_EVIDENCE_KIND,
    LVS_ACTION,
    LVS_EVIDENCE_KIND,
)
from sigilicon.workflows.layout_verification import (
    load_receipt_bound_verification_inputs,
)


_NON_CONCLUSIONS = {
    PhysicalVerificationStatus.UNSUPPORTED,
    PhysicalVerificationStatus.BACKEND_UNAVAILABLE,
    PhysicalVerificationStatus.EXECUTION_FAILED,
}


class OfflinePhysicalVerificationAdapter:
    """Emit only non-conclusive evidence; never claim clean or violated layout."""

    def _status(self, context: ActionContext) -> PhysicalVerificationStatus:
        if context.action_config:
            raise FlowExecutionError(
                "offline physical-verification Action config must be empty"
            )
        if set(context.adapter_config) - {"outcome"}:
            raise FlowExecutionError(
                "offline physical-verification Adapter config accepts only 'outcome'"
            )
        raw = context.adapter_config.get(
            "outcome",
            PhysicalVerificationStatus.BACKEND_UNAVAILABLE.value,
        )
        try:
            status = PhysicalVerificationStatus(raw)
        except ValueError as exc:
            raise FlowExecutionError(
                f"unknown offline physical-verification outcome: {raw!r}"
            ) from exc
        if status not in _NON_CONCLUSIONS:
            raise FlowExecutionError(
                "offline physical verification cannot claim clean or violated"
            )
        return status

    def _completion(
        self,
        status: PhysicalVerificationStatus,
    ) -> VerificationCompletion:
        return VerificationCompletion(
            backend="sigilicon.offline-physical-verification",
            executed=status is PhysicalVerificationStatus.EXECUTION_FAILED,
            report_parsed=False,
            exit_code=(
                1 if status is PhysicalVerificationStatus.EXECUTION_FAILED else None
            ),
        )

    def _evidence(self, context: ActionContext) -> DrcEvidence | LvsEvidence:
        status = self._status(context)
        completion = self._completion(status)
        inputs = load_receipt_bound_verification_inputs(context)
        layout = inputs.layout
        if context.action.kind == DRC_ACTION:
            return DrcEvidence(
                status,
                layout,
                completion,
                (),
                "offline Adapter cannot produce a DRC conclusion",
            )
        if context.action.kind == LVS_ACTION:
            assert inputs.source is not None
            return LvsEvidence(
                status,
                layout,
                inputs.source,
                completion,
                (),
                "offline Adapter cannot produce an LVS conclusion",
            )
        raise FlowExecutionError(
            f"offline physical verification cannot implement {context.action.kind!r}"
        )

    @staticmethod
    def _facts(evidence: DrcEvidence | LvsEvidence) -> dict[str, object]:
        prefix = "drc" if isinstance(evidence, DrcEvidence) else "lvs"
        return {
            f"{prefix}-status": evidence.status.value,
            f"{prefix}-clean": evidence.clean,
            f"{prefix}-completed": evidence.completion.proven,
        }

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            self._evidence(context)
        except (FlowExecutionError, ValueError, OSError) as exc:
            return (str(exc),)
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        evidence = self._evidence(context)
        context.output_path("evidence", "physical-verification-evidence.json").write_text(
            evidence.canonical_json(),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded(details=self._facts(evidence))

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        path = context.output_path(
            "evidence",
            "physical-verification-evidence.json",
        )
        try:
            evidence = (
                drc_evidence_from_json(path.read_text(encoding="utf-8"))
                if context.action.kind == DRC_ACTION
                else lvs_evidence_from_json(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise FlowExecutionError(
                f"invalid physical-verification evidence artifact: {exc}"
            ) from exc
        facts = self._facts(evidence)
        if dict(execution.details) != facts:
            raise FlowExecutionError(
                "physical-verification execution details disagree with evidence"
            )
        return CollectedActionResult(
            status="valid",
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    DRC_EVIDENCE_KIND
                    if isinstance(evidence, DrcEvidence)
                    else LVS_EVIDENCE_KIND,
                    path,
                    qualifiers={
                        "layout-sha256": evidence.layout.artifact_sha256,
                        "receipt-sha256": str(evidence.layout.receipt_sha256),
                        "job-sha256": str(evidence.layout.job_sha256),
                        "plan-sha256": evidence.layout.plan_sha256,
                        "result-sha256": str(evidence.layout.result_sha256),
                        **(
                            {"source-sha256": evidence.source.artifact_sha256}
                            if isinstance(evidence, LvsEvidence)
                            else {}
                        ),
                        "status": evidence.status.value,
                    },
                ),
            ),
            facts=facts,
        )


__all__ = ["OfflinePhysicalVerificationAdapter"]
