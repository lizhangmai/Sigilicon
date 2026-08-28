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
from sigilicon.flow.source_assets import SourceAssetsAdapter
from sigilicon.workflows.layout_verification import (
    load_receipt_bound_verification_inputs,
)


_NON_CONCLUSIONS = {
    PhysicalVerificationStatus.UNSUPPORTED,
    PhysicalVerificationStatus.BACKEND_UNAVAILABLE,
    PhysicalVerificationStatus.EXECUTION_FAILED,
}


class ReceiptBoundVerificationSourceAdapter(SourceAssetsAdapter):
    """Snapshot checked source/policy and bind their exact content identities."""

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        collected = super().collect_result(context, execution)
        if {artifact.role for artifact in collected.artifacts} != {
            "source",
            "verification-policy",
        }:
            raise FlowExecutionError(
                "receipt-bound verification source must provide source and policy"
            )
        artifacts: list[ProducedArtifact] = []
        for artifact in collected.artifacts:
            qualifiers = dict(artifact.qualifiers)
            if not isinstance(qualifiers.get("owner"), str):
                raise FlowExecutionError(
                    f"{artifact.role} source qualifier 'owner' is required"
                )
            if artifact.role == "source" and not isinstance(
                qualifiers.get("name"), str
            ):
                raise FlowExecutionError(
                    "canonical source qualifier 'name' is required"
                )
            identity_name = (
                "source-identity"
                if artifact.role == "source"
                else "policy-identity"
            )
            semantic_name = qualifiers.get("name", artifact.path.name)
            qualifiers[identity_name] = (
                f"{qualifiers['owner']}:{semantic_name}:{artifact.role}"
            )
            artifacts.append(
                ProducedArtifact(
                    artifact.role,
                    artifact.kind,
                    artifact.path,
                    qualifiers=qualifiers,
                )
            )
        return CollectedActionResult(
            status=collected.status,
            artifacts=tuple(artifacts),
            facts=collected.facts,
            details=collected.details,
            evidence=collected.evidence,
        )


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
                        "layout-identity": evidence.layout.artifact_identity,
                        "receipt-identity": str(evidence.layout.receipt_identity),
                        "job-identity": str(evidence.layout.job_identity),
                        "plan-identity": evidence.layout.plan_identity,
                        "result-identity": str(evidence.layout.result_identity),
                        **(
                            {"source-identity": evidence.source.artifact_identity}
                            if isinstance(evidence, LvsEvidence)
                            else {}
                        ),
                        "status": evidence.status.value,
                    },
                ),
            ),
            facts=facts,
        )


__all__ = [
    "OfflinePhysicalVerificationAdapter",
    "ReceiptBoundVerificationSourceAdapter",
]
