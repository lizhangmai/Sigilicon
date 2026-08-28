"""Typed multi-round physical closure above the deterministic FlowEngine seam."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from sigilicon.canonical import canonical_from_json, canonical_json
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    LvsEvidence,
    PhysicalVerificationStatus,
    drc_evidence_id,
    drc_evidence_from_json,
    lvs_evidence_from_json,
    lvs_evidence_id,
)
from sigilicon.domain.post_layout import (
    PexEvidence,
    PexStatus,
    PhysicalAnalysisStatus,
    PostLayoutEvidence,
    QualificationEvidence,
    pex_evidence_from_json,
    pex_evidence_id,
    post_layout_evidence_from_json,
    post_layout_evidence_id,
    qualification_evidence_from_json,
    qualification_evidence_id,
)
from sigilicon.flow.model import (
    ActionArtifact,
    ExecutionEnvironment,
    FlowExecutionError,
    FlowPlan,
    FlowResult,
    identifier,
    owner_identity,
    run_identity,
)
from sigilicon.flow.engine import FlowEngine
from sigilicon.flow.physical_design import (
    MATERIALIZATION_RECEIPT_KIND,
    PHYSICAL_CLOSURE_EVIDENCE_KIND,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
)
from sigilicon.flow.physical_verification import (
    CANONICAL_SOURCE_NETLIST_KIND,
    DRC_EVIDENCE_KIND,
    LVS_EVIDENCE_KIND,
    MATERIALIZED_LAYOUT_KIND,
)
from sigilicon.flow.post_layout import (
    PEX_EVIDENCE_KIND,
    PEX_NETLIST_KIND,
    PHYSICAL_QUALIFICATION_EVIDENCE_KIND,
    PHYSICAL_QUALIFICATION_SPEC_KIND,
    POST_LAYOUT_EVIDENCE_KIND,
    POST_LAYOUT_SPEC_KIND,
)
from sigilicon.layout.materialization import (
    MaterializationDecision,
    MaterializationPlan,
    MaterializationReason,
    materialization_plan_from_json,
    validate_materialization_plan,
)
from sigilicon.layout.materialization_execution import (
    MaterializationExecutionStatus,
    MaterializationReceipt,
    materialization_receipt_from_json,
    materialization_receipt_id,
    validate_materialization_receipt,
)
from sigilicon.layout.pnr import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    PhysicalOwnerMobility,
    PlacementRoutingClosureEvidence,
    PlacementRoutingTerminationReason,
    ResultStatus,
    RoutingClosureQuality,
    RoutingTerminationReason,
)
from sigilicon.layout.pnr.serialization import (
    physical_closure_evidence_id,
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
    placement_routing_closure_evidence_from_json,
)
from sigilicon.workflows.closure_repair import (
    ClosureFeedbackKind,
    ClosureFeedbackScope,
    ClosureRepairDecision,
    ClosureRepairPolicy,
    RepairPlan,
    compile_closure_repair,
)


class ClosureCampaignError(ValueError):
    """A campaign or one of its managed evidence contracts is invalid."""


class ClosureStageStatus(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    NOT_EVALUATED = "not_evaluated"
    NOT_REQUESTED = "not_requested"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_IDENTITY = "invalid_identity"


class ClosureCampaignTermination(str, Enum):
    CLOSED = "closed"
    REPAIR = "repair"
    UNSUPPORTED = "unsupported"
    PROVEN_INFEASIBLE = "proven_infeasible"
    STATE_BUDGET = "state_budget"
    ITERATION_BUDGET = "iteration_budget"
    EXECUTION_FAILED = "execution_failed"


class ClosureQualityDecision(str, Enum):
    IMPROVED = "improved"
    EQUIVALENT = "equivalent"
    REGRESSED = "regressed"


@dataclass(frozen=True)
class CampaignArtifactReference:
    node: str
    role: str

    def __post_init__(self) -> None:
        identifier(self.node, "campaign artifact node")
        identifier(self.role, "campaign artifact role")


@dataclass(frozen=True)
class ClosureArtifactBindings:
    job: CampaignArtifactReference
    result: CampaignArtifactReference
    materialization_plan: CampaignArtifactReference
    closure_evidence: CampaignArtifactReference | None = None
    materialization_receipt: CampaignArtifactReference | None = None
    layout: CampaignArtifactReference | None = None
    source: CampaignArtifactReference | None = None
    drc: CampaignArtifactReference | None = None
    lvs: CampaignArtifactReference | None = None
    parasitics: CampaignArtifactReference | None = None
    pex: CampaignArtifactReference | None = None
    post_layout_specification: CampaignArtifactReference | None = None
    post_layout: CampaignArtifactReference | None = None
    qualification_specification: CampaignArtifactReference | None = None
    qualification: CampaignArtifactReference | None = None


@dataclass(frozen=True)
class ClosureCampaignScope:
    require_drc: bool = True
    require_lvs: bool = True
    require_pex: bool = False
    require_post_layout: bool = False
    require_qualification: bool = False

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.require_drc,
                self.require_lvs,
                self.require_pex,
                self.require_post_layout,
                self.require_qualification,
            )
        ):
            raise ClosureCampaignError("campaign scope requirements must be booleans")


@dataclass(frozen=True)
class ClosureIteration:
    iteration_id: str
    plan: FlowPlan
    artifacts: ClosureArtifactBindings

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "closure iteration")
        planned = set(self.plan.topology)
        references = tuple(
            reference
            for reference in (
                self.artifacts.job,
                self.artifacts.result,
                self.artifacts.materialization_plan,
                self.artifacts.closure_evidence,
                self.artifacts.materialization_receipt,
                self.artifacts.layout,
                self.artifacts.source,
                self.artifacts.drc,
                self.artifacts.lvs,
                self.artifacts.parasitics,
                self.artifacts.pex,
                self.artifacts.post_layout_specification,
                self.artifacts.post_layout,
                self.artifacts.qualification_specification,
                self.artifacts.qualification,
            )
            if reference is not None
        )
        unknown = sorted({item.node for item in references} - planned)
        if unknown:
            raise ClosureCampaignError(
                f"closure iteration references unplanned nodes: {unknown}"
            )


@dataclass(frozen=True)
class ClosureCampaign:
    owner: str
    campaign_id: str
    iterations: tuple[ClosureIteration, ...]
    state_budget: int
    iteration_budget: int
    scope: ClosureCampaignScope = ClosureCampaignScope()
    repair_policy: ClosureRepairPolicy | None = None

    def __post_init__(self) -> None:
        owner_identity(self.owner, "closure campaign owner")
        identifier(self.campaign_id, "closure campaign identity")
        if not self.iterations:
            raise ClosureCampaignError("closure campaign needs an iteration")
        if type(self.state_budget) is not int or self.state_budget <= 0:
            raise ClosureCampaignError("closure campaign state budget must be positive")
        if type(self.iteration_budget) is not int or self.iteration_budget <= 0:
            raise ClosureCampaignError(
                "closure campaign iteration budget must be positive"
            )
        identities = tuple(item.iteration_id for item in self.iterations)
        if len(identities) != len(set(identities)):
            raise ClosureCampaignError("closure iteration identities must be unique")
        for iteration in self.iterations:
            if iteration.plan.spec.owner != self.owner:
                raise ClosureCampaignError(
                    "closure iteration Flow owner must match campaign owner"
                )
        if self.repair_policy is not None and self.repair_policy.owner != self.owner:
            raise ClosureCampaignError(
                "closure repair policy owner must match campaign owner"
            )


@dataclass(frozen=True)
class ClosureCost:
    displacement_dbu: int
    area_dbu2: int | None = None
    power_femtowatts: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("displacement", self.displacement_dbu),
            ("area", self.area_dbu2),
            ("power", self.power_femtowatts),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ClosureCampaignError(
                    f"closure {label} cost must be a non-negative integer or null"
                )


_STATUS_RANK = {
    ClosureStageStatus.SATISFIED: 0,
    ClosureStageStatus.NOT_REQUESTED: 0,
    ClosureStageStatus.VIOLATED: 1,
    ClosureStageStatus.NOT_EVALUATED: 2,
    ClosureStageStatus.UNSUPPORTED: 3,
    ClosureStageStatus.BACKEND_UNAVAILABLE: 4,
    ClosureStageStatus.EXECUTION_FAILED: 5,
    ClosureStageStatus.INVALID_IDENTITY: 6,
}


@dataclass(frozen=True)
class ClosureQuality:
    """Typed lexicographic closure state; never a flattened scalar score."""

    identity: ClosureStageStatus
    materialization: ClosureStageStatus
    materialization_decision: MaterializationDecision | None
    materialization_reason: MaterializationReason | None
    drc: ClosureStageStatus
    drc_findings: int
    lvs: ClosureStageStatus
    lvs_findings: int
    routing: RoutingClosureQuality | None
    independent_evaluator: ClosureStageStatus
    pex: ClosureStageStatus
    post_layout: ClosureStageStatus
    qualification: ClosureStageStatus
    cost: ClosureCost

    def __post_init__(self) -> None:
        statuses = (
            self.identity,
            self.materialization,
            self.drc,
            self.lvs,
            self.independent_evaluator,
            self.pex,
            self.post_layout,
            self.qualification,
        )
        if any(not isinstance(value, ClosureStageStatus) for value in statuses):
            raise ClosureCampaignError("closure quality stages must be typed statuses")
        if type(self.drc_findings) is not int or self.drc_findings < 0:
            raise ClosureCampaignError("DRC finding count must be non-negative")
        if type(self.lvs_findings) is not int or self.lvs_findings < 0:
            raise ClosureCampaignError("LVS finding count must be non-negative")

    @property
    def closed(self) -> bool:
        return (
            self.identity is ClosureStageStatus.SATISFIED
            and self.materialization is ClosureStageStatus.SATISFIED
            and self.drc
            in {ClosureStageStatus.SATISFIED, ClosureStageStatus.NOT_REQUESTED}
            and self.lvs
            in {ClosureStageStatus.SATISFIED, ClosureStageStatus.NOT_REQUESTED}
            and self.routing is not None
            and self.routing.closed
            and self.independent_evaluator is ClosureStageStatus.SATISFIED
            and self.pex
            in {ClosureStageStatus.SATISFIED, ClosureStageStatus.NOT_REQUESTED}
            and self.post_layout
            in {ClosureStageStatus.SATISFIED, ClosureStageStatus.NOT_REQUESTED}
            and self.qualification
            in {ClosureStageStatus.SATISFIED, ClosureStageStatus.NOT_REQUESTED}
        )

    def dominance_key(self) -> tuple[object, ...]:
        """Return the documented stage-order key; lower is strictly better."""

        routing = self.routing
        routing_key: tuple[int, ...] = (
            (1,) + (0,) * 10
            if routing is None
            else (
                0,
                routing.resource_overflow,
                routing.unrouted_branches,
                routing.hard_blockers,
                routing.group_violations,
                routing.via_failures,
                routing.topology_failures,
                routing.unsupported_failures,
                routing.budget_exhaustions,
                -routing.routed_nets,
                -routing.routed_branches,
            )
        )
        independent_key = (
            _STATUS_RANK[self.independent_evaluator],
            0 if routing is None else routing.checker_violations,
            0 if routing is None else routing.constraint_violations,
            0 if routing is None else routing.constraint_not_evaluated,
        )

        def optional_cost(value: int | None) -> tuple[int, int]:
            return (1, 0) if value is None else (0, value)

        return (
            _STATUS_RANK[self.identity],
            _STATUS_RANK[self.materialization],
            _STATUS_RANK[self.drc],
            self.drc_findings,
            _STATUS_RANK[self.lvs],
            self.lvs_findings,
            routing_key,
            independent_key,
            _STATUS_RANK[self.pex],
            _STATUS_RANK[self.post_layout],
            _STATUS_RANK[self.qualification],
            self.cost.displacement_dbu,
            optional_cost(self.cost.area_dbu2),
            optional_cost(self.cost.power_femtowatts),
        )

    def canonical_json(self) -> str:
        return canonical_json(self)


def compare_closure_quality(
    candidate: ClosureQuality,
    reference: ClosureQuality,
) -> ClosureQualityDecision:
    if candidate.dominance_key() < reference.dominance_key():
        return ClosureQualityDecision.IMPROVED
    if candidate.dominance_key() > reference.dominance_key():
        return ClosureQualityDecision.REGRESSED
    return ClosureQualityDecision.EQUIVALENT


@dataclass(frozen=True)
class CampaignArtifactIdentity:
    label: str
    kind: str
    identity: str
    producer: str
    role: str

    def __post_init__(self) -> None:
        identifier(self.label, "campaign artifact label")
        identifier(self.kind, "campaign artifact kind")
        identifier(self.producer, "campaign artifact producer")
        identifier(self.role, "campaign artifact role")
        if not isinstance(self.identity, str) or not self.identity:
            raise ClosureCampaignError("campaign artifact needs a semantic identity")


@dataclass(frozen=True)
class ClosureIterationProvenance:
    iteration_id: str
    run_id: str
    plan_identity: str
    flow_id: str
    target: str
    execution_profile: str
    artifacts: tuple[CampaignArtifactIdentity, ...]

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "closure iteration")
        run_identity(self.run_id)
        if not isinstance(self.plan_identity, str) or not self.plan_identity:
            raise ClosureCampaignError("closure iteration needs a plan semantic identity")
        identifier(self.flow_id, "closure Flow")
        identifier(self.target, "closure Flow target")
        identifier(self.execution_profile, "closure Execution Profile")


@dataclass(frozen=True)
class ClosureIterationResult:
    provenance: ClosureIterationProvenance
    flow_status: str
    quality: ClosureQuality
    quality_decision: ClosureQualityDecision | None
    feedback: tuple[ClosureFeedbackScope, ...]
    decision: ClosureCampaignTermination
    repair_plan: RepairPlan | None
    message: str

    def __post_init__(self) -> None:
        if self.decision is ClosureCampaignTermination.REPAIR and (
            self.repair_plan is None or not self.repair_plan.accepted
        ):
            raise ClosureCampaignError(
                "repair continuation requires an accepted public RepairPlan"
            )


@dataclass(frozen=True)
class ClosureCampaignResult:
    owner: str
    campaign_id: str
    campaign_identity: str
    termination: ClosureCampaignTermination
    iterations: tuple[ClosureIterationResult, ...]
    final_quality: ClosureQuality
    message: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "closure campaign result owner")
        identifier(self.campaign_id, "closure campaign result identity")
        if not isinstance(self.campaign_identity, str) or not self.campaign_identity:
            raise ClosureCampaignError("closure campaign result needs a semantic identity")
        if not self.iterations:
            raise ClosureCampaignError("closure campaign result needs an iteration")
        if self.iterations[-1].quality != self.final_quality:
            raise ClosureCampaignError(
                "campaign final quality must equal the last iteration quality"
            )
        if self.iterations[-1].decision is not self.termination:
            raise ClosureCampaignError(
                "campaign termination must equal the last iteration decision"
            )
        if any(
            item.repair_plan is not None
            and item.repair_plan.owner != self.owner
            for item in self.iterations
        ):
            raise ClosureCampaignError(
                "campaign RepairPlan owner must match campaign result owner"
            )

    @property
    def closed(self) -> bool:
        return self.termination is ClosureCampaignTermination.CLOSED

    def canonical_json(self) -> str:
        return canonical_json(self)


def closure_campaign_result_from_json(text: str) -> ClosureCampaignResult:
    return canonical_from_json(text, ClosureCampaignResult)


@dataclass(frozen=True)
class _ObservedClosure:
    job: PhysicalDesignJob
    result: PhysicalDesignResult
    plan: MaterializationPlan
    receipt: MaterializationReceipt | None
    drc: DrcEvidence | None
    lvs: LvsEvidence | None
    pex: PexEvidence | None
    post_layout: PostLayoutEvidence | None
    qualification: QualificationEvidence | None
    quality: ClosureQuality
    feedback: tuple[ClosureFeedbackScope, ...]
    provenance: ClosureIterationProvenance


def _artifact_identity(artifact: ActionArtifact) -> str:
    parsers = {
        PHYSICAL_DESIGN_JOB_KIND: physical_design_job_from_json,
        PHYSICAL_DESIGN_RESULT_KIND: physical_design_result_from_json,
        PHYSICAL_MATERIALIZATION_PLAN_KIND: materialization_plan_from_json,
        MATERIALIZATION_RECEIPT_KIND: materialization_receipt_from_json,
        PHYSICAL_CLOSURE_EVIDENCE_KIND: placement_routing_closure_evidence_from_json,
        DRC_EVIDENCE_KIND: drc_evidence_from_json,
        LVS_EVIDENCE_KIND: lvs_evidence_from_json,
        PEX_EVIDENCE_KIND: pex_evidence_from_json,
        POST_LAYOUT_EVIDENCE_KIND: post_layout_evidence_from_json,
        PHYSICAL_QUALIFICATION_EVIDENCE_KIND: qualification_evidence_from_json,
    }
    parser = parsers.get(artifact.kind)
    if parser is not None:
        value = parser(artifact.path.read_text(encoding="utf-8"))
        if isinstance(value, PhysicalDesignJob):
            return physical_design_job_id(value)
        if isinstance(value, PhysicalDesignResult):
            return physical_design_result_id(value)
        if isinstance(value, MaterializationPlan):
            return value.artifact_id
        if isinstance(value, MaterializationReceipt):
            return materialization_receipt_id(value)
        if isinstance(value, PlacementRoutingClosureEvidence):
            return physical_closure_evidence_id(value)
        if isinstance(value, DrcEvidence):
            return drc_evidence_id(value)
        if isinstance(value, LvsEvidence):
            return lvs_evidence_id(value)
        if isinstance(value, PexEvidence):
            return pex_evidence_id(value)
        if isinstance(value, PostLayoutEvidence):
            return post_layout_evidence_id(value)
        if isinstance(value, QualificationEvidence):
            return qualification_evidence_id(value)
    for name in (
        "artifact-identity",
        "evidence-identity",
        "layout-identity",
        "receipt-identity",
        "result-identity",
        "job-identity",
        "plan-identity",
        "source-identity",
        "parasitics-identity",
        "specification-identity",
    ):
        value = artifact.qualifiers.get(name)
        if isinstance(value, str) and value:
            return value
    return f"{artifact.producer}:{artifact.role}:{artifact.path.name}"


def _artifact(
    result: FlowResult,
    reference: CampaignArtifactReference,
    expected_kind: str,
) -> ActionArtifact:
    outcome = result.nodes.get(reference.node)
    if outcome is None:
        raise ClosureCampaignError(
            f"Flow result omitted campaign node {reference.node!r}"
        )
    if outcome.result_status != "valid":
        raise ClosureCampaignError(
            f"campaign node {reference.node!r} has no valid typed result: "
            f"{outcome.reason or outcome.status}"
        )
    item = outcome.artifacts.get(reference.role)
    if item is None:
        raise ClosureCampaignError(
            f"campaign node {reference.node!r} omitted role {reference.role!r}"
        )
    if item.kind != expected_kind:
        raise ClosureCampaignError(
            f"campaign artifact {reference.node}.{reference.role} has kind "
            f"{item.kind!r}, expected {expected_kind!r}"
        )
    return item


def _optional_artifact(
    result: FlowResult,
    reference: CampaignArtifactReference,
    expected_kind: str,
) -> ActionArtifact | None:
    """Read an optional stage output without guessing a node or filename."""

    outcome = result.nodes.get(reference.node)
    if outcome is None:
        raise ClosureCampaignError(
            f"Flow result omitted campaign node {reference.node!r}"
        )
    item = outcome.artifacts.get(reference.role)
    if item is None:
        return None
    if item.kind != expected_kind:
        raise ClosureCampaignError(
            f"campaign artifact {reference.node}.{reference.role} has kind "
            f"{item.kind!r}, expected {expected_kind!r}"
        )
    return item


def _read_typed(
    artifact: ActionArtifact,
    loader,
    label: str,
):
    try:
        text = artifact.path.read_text(encoding="utf-8")
        value = loader(text)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ClosureCampaignError(f"invalid {label} artifact: {exc}") from exc
    if text != value.canonical_json():
        raise ClosureCampaignError(f"{label} artifact is not canonical JSON")
    return value


def _expect_qualifier(
    artifact: ActionArtifact,
    name: str,
    expected: object,
    issues: list[str],
) -> None:
    if artifact.qualifiers.get(name) != expected:
        issues.append(
            f"{artifact.producer}.{artifact.role} qualifier {name!r} "
            f"does not match typed evidence"
        )


def _verification_status(value: PhysicalVerificationStatus) -> ClosureStageStatus:
    return {
        PhysicalVerificationStatus.CLEAN: ClosureStageStatus.SATISFIED,
        PhysicalVerificationStatus.VIOLATED: ClosureStageStatus.VIOLATED,
        PhysicalVerificationStatus.UNSUPPORTED: ClosureStageStatus.UNSUPPORTED,
        PhysicalVerificationStatus.BACKEND_UNAVAILABLE: (
            ClosureStageStatus.BACKEND_UNAVAILABLE
        ),
        PhysicalVerificationStatus.EXECUTION_FAILED: (
            ClosureStageStatus.EXECUTION_FAILED
        ),
    }[value]


def _materialization_status(
    receipt: MaterializationReceipt | None,
    plan: MaterializationPlan,
    *,
    execution_failed: bool = False,
) -> ClosureStageStatus:
    if receipt is None:
        if execution_failed:
            return ClosureStageStatus.EXECUTION_FAILED
        return (
            ClosureStageStatus.NOT_EVALUATED
            if plan.executable
            else ClosureStageStatus.VIOLATED
        )
    return {
        MaterializationExecutionStatus.MATERIALIZED: ClosureStageStatus.SATISFIED,
        MaterializationExecutionStatus.UNSUPPORTED: ClosureStageStatus.UNSUPPORTED,
        MaterializationExecutionStatus.BACKEND_UNAVAILABLE: (
            ClosureStageStatus.BACKEND_UNAVAILABLE
        ),
        MaterializationExecutionStatus.EXECUTION_FAILED: (
            ClosureStageStatus.EXECUTION_FAILED
        ),
        MaterializationExecutionStatus.INVALID_PLAN_IDENTITY: (
            ClosureStageStatus.INVALID_IDENTITY
        ),
    }[receipt.status]


def _reference_execution_failed(
    result: FlowResult,
    reference: CampaignArtifactReference | None,
) -> bool:
    if reference is None:
        return False
    outcome = result.nodes.get(reference.node)
    if outcome is None:
        return True
    return outcome.execution_status in {"failed", "cancelled"} or (
        outcome.result_status == "failed"
    )


def _independent_status(
    quality: RoutingClosureQuality | None,
) -> ClosureStageStatus:
    if quality is None:
        return ClosureStageStatus.NOT_EVALUATED
    if quality.checker_violations or quality.constraint_violations:
        return ClosureStageStatus.VIOLATED
    if quality.constraint_not_evaluated:
        return ClosureStageStatus.NOT_EVALUATED
    return ClosureStageStatus.SATISFIED


def _future_status(required: bool) -> ClosureStageStatus:
    return (
        ClosureStageStatus.UNSUPPORTED
        if required
        else ClosureStageStatus.NOT_REQUESTED
    )


def _pex_status(value: PexStatus) -> ClosureStageStatus:
    return {
        PexStatus.EXTRACTED: ClosureStageStatus.SATISFIED,
        PexStatus.UNSUPPORTED: ClosureStageStatus.UNSUPPORTED,
        PexStatus.BACKEND_UNAVAILABLE: ClosureStageStatus.BACKEND_UNAVAILABLE,
        PexStatus.EXECUTION_FAILED: ClosureStageStatus.EXECUTION_FAILED,
    }[value]


def _analysis_status(value: PhysicalAnalysisStatus) -> ClosureStageStatus:
    return {
        PhysicalAnalysisStatus.PASSED: ClosureStageStatus.SATISFIED,
        PhysicalAnalysisStatus.VIOLATED: ClosureStageStatus.VIOLATED,
        PhysicalAnalysisStatus.UNSUPPORTED: ClosureStageStatus.UNSUPPORTED,
        PhysicalAnalysisStatus.BACKEND_UNAVAILABLE: (
            ClosureStageStatus.BACKEND_UNAVAILABLE
        ),
        PhysicalAnalysisStatus.EXECUTION_FAILED: (
            ClosureStageStatus.EXECUTION_FAILED
        ),
    }[value]


def _downstream_status(
    *,
    required: bool,
    reference: CampaignArtifactReference | None,
    evidence_status: ClosureStageStatus | None,
    result: FlowResult,
) -> ClosureStageStatus:
    if not required:
        return ClosureStageStatus.NOT_REQUESTED
    if evidence_status is not None:
        return evidence_status
    if reference is None:
        return ClosureStageStatus.UNSUPPORTED
    if _reference_execution_failed(result, reference):
        return ClosureStageStatus.EXECUTION_FAILED
    return ClosureStageStatus.NOT_EVALUATED


def _invalid_quality(scope: ClosureCampaignScope) -> ClosureQuality:
    return ClosureQuality(
        identity=ClosureStageStatus.INVALID_IDENTITY,
        materialization=ClosureStageStatus.NOT_EVALUATED,
        materialization_decision=None,
        materialization_reason=None,
        drc=(
            ClosureStageStatus.NOT_EVALUATED
            if scope.require_drc
            else ClosureStageStatus.NOT_REQUESTED
        ),
        drc_findings=0,
        lvs=(
            ClosureStageStatus.NOT_EVALUATED
            if scope.require_lvs
            else ClosureStageStatus.NOT_REQUESTED
        ),
        lvs_findings=0,
        routing=None,
        independent_evaluator=ClosureStageStatus.NOT_EVALUATED,
        pex=_future_status(scope.require_pex),
        post_layout=_future_status(scope.require_post_layout),
        qualification=_future_status(scope.require_qualification),
        cost=ClosureCost(0),
    )


def _feedback(
    evidence: PlacementRoutingClosureEvidence | None,
    plan: MaterializationPlan,
    receipt: MaterializationReceipt | None,
    drc: DrcEvidence | None,
    lvs: LvsEvidence | None,
) -> tuple[ClosureFeedbackScope, ...]:
    scopes: list[ClosureFeedbackScope] = []
    if evidence is not None:
        for pressure in evidence.placement_pressure:
            owners = tuple(
                sorted(
                    {
                        (
                            summary.repair_owner or summary.identity
                        ).stable_name
                        for summary in pressure.physical_owner_candidates
                        if summary.mobility is PhysicalOwnerMobility.MOVABLE
                    }
                )
            )
            if owners:
                scopes.append(
                    ClosureFeedbackScope(
                        ClosureFeedbackKind.PHYSICAL_OWNER,
                        owners,
                        (pressure.identity,),
                        tuple(sorted(pressure.involved_nets)),
                        tuple(sorted(pressure.involved_groups)),
                        True,
                    )
                )
        if not scopes and evidence.conflicts:
            fixed = tuple(
                sorted(
                    {
                        owner.stable_name
                        for conflict in evidence.conflicts
                        for owner in conflict.physical_owners
                    }
                )
            )
            scopes.append(
                ClosureFeedbackScope(
                    ClosureFeedbackKind.FIXED_PHYSICAL_BLOCKER,
                    fixed or tuple(item.identity for item in evidence.conflicts),
                    tuple(item.identity for item in evidence.conflicts),
                    tuple(
                        sorted(
                            {
                                net
                                for item in evidence.conflicts
                                for net in (*item.aggressor_nets, *item.occupant_nets)
                            }
                        )
                    ),
                    (),
                    False,
                )
            )
    if drc is not None and drc.status is PhysicalVerificationStatus.VIOLATED:
        scopes.append(
            ClosureFeedbackScope(
                ClosureFeedbackKind.DRC_RULE,
                tuple(item.rule for item in drc.violations),
                (drc.layout.artifact_identity,),
                repairable=True,
            )
        )
    if lvs is not None and lvs.status is PhysicalVerificationStatus.VIOLATED:
        scopes.append(
            ClosureFeedbackScope(
                ClosureFeedbackKind.LVS_MISMATCH,
                tuple(item.category for item in lvs.mismatches),
                (
                    lvs.layout.artifact_identity,
                    lvs.source.artifact_identity,
                ),
                repairable=True,
            )
        )
    if (
        (not plan.executable or receipt is None or not receipt.materialized)
        and not scopes
    ):
        identities = (
            (
                "receipt_not_evaluated"
                if plan.executable
                else plan.acceptance.reason.value,
            )
            if receipt is None
            else (
                receipt.status.value,
                *(issue.code for issue in receipt.issues),
            )
        )
        source_evidence = (
            plan.provenance.result_identity,
            plan.artifact_id,
            *(() if receipt is None else (materialization_receipt_id(receipt),)),
        )
        scopes.append(
            ClosureFeedbackScope(
                ClosureFeedbackKind.MATERIALIZATION,
                identities,
                source_evidence,
                repairable=False,
            )
        )
    return tuple(scopes)


class ClosureCampaignRunner:
    """Execute explicit Flow attempts and decide only from typed stage artifacts."""

    def __init__(
        self,
        engine: FlowEngine,
        *,
        artifact_root: Path,
        environment: ExecutionEnvironment | None = None,
    ) -> None:
        root = Path(artifact_root).resolve()
        if root == Path(root.anchor):
            raise ClosureCampaignError("campaign artifact root cannot be a filesystem root")
        self._engine = engine
        self._artifact_root = root
        self._environment = environment

    def _campaign_identity(self, campaign: ClosureCampaign) -> str:
        return f"{campaign.owner}:closure-campaign:{campaign.campaign_id}"

    def _provenance(
        self,
        iteration: ClosureIteration,
        result: FlowResult,
        plan_identity: str,
        artifacts: Mapping[str, ActionArtifact],
    ) -> ClosureIterationProvenance:
        return ClosureIterationProvenance(
            iteration_id=iteration.iteration_id,
            run_id=result.run_id,
            plan_identity=plan_identity,
            flow_id=iteration.plan.spec.flow_id,
            target=iteration.plan.target.target_id,
            execution_profile=iteration.plan.profile.profile_id,
            artifacts=tuple(
                CampaignArtifactIdentity(
                    label,
                    artifact.kind,
                    _artifact_identity(artifact),
                    artifact.producer,
                    artifact.role,
                )
                for label, artifact in sorted(artifacts.items())
            ),
        )

    def _observe(
        self,
        campaign: ClosureCampaign,
        iteration: ClosureIteration,
        flow_result: FlowResult,
        plan_identity: str,
    ) -> _ObservedClosure:
        bindings = iteration.artifacts
        artifacts: dict[str, ActionArtifact] = {}
        artifacts["job"] = _artifact(
            flow_result, bindings.job, PHYSICAL_DESIGN_JOB_KIND
        )
        artifacts["result"] = _artifact(
            flow_result, bindings.result, PHYSICAL_DESIGN_RESULT_KIND
        )
        artifacts["materialization-plan"] = _artifact(
            flow_result,
            bindings.materialization_plan,
            PHYSICAL_MATERIALIZATION_PLAN_KIND,
        )
        if bindings.closure_evidence is not None:
            artifacts["closure-evidence"] = _artifact(
                flow_result,
                bindings.closure_evidence,
                PHYSICAL_CLOSURE_EVIDENCE_KIND,
            )
        if bindings.materialization_receipt is not None:
            receipt_artifact = _optional_artifact(
                flow_result,
                bindings.materialization_receipt,
                MATERIALIZATION_RECEIPT_KIND,
            )
            if receipt_artifact is not None:
                artifacts["materialization-receipt"] = receipt_artifact
        for label, reference, kind in (
            ("layout", bindings.layout, MATERIALIZED_LAYOUT_KIND),
            ("source", bindings.source, CANONICAL_SOURCE_NETLIST_KIND),
            ("drc", bindings.drc, DRC_EVIDENCE_KIND),
            ("lvs", bindings.lvs, LVS_EVIDENCE_KIND),
            ("parasitics", bindings.parasitics, PEX_NETLIST_KIND),
            ("pex", bindings.pex, PEX_EVIDENCE_KIND),
            (
                "post-layout-specification",
                bindings.post_layout_specification,
                POST_LAYOUT_SPEC_KIND,
            ),
            (
                "post-layout",
                bindings.post_layout,
                POST_LAYOUT_EVIDENCE_KIND,
            ),
            (
                "qualification-specification",
                bindings.qualification_specification,
                PHYSICAL_QUALIFICATION_SPEC_KIND,
            ),
            (
                "qualification",
                bindings.qualification,
                PHYSICAL_QUALIFICATION_EVIDENCE_KIND,
            ),
        ):
            if reference is not None:
                artifact = _optional_artifact(flow_result, reference, kind)
                if artifact is not None:
                    artifacts[label] = artifact

        job: PhysicalDesignJob = _read_typed(
            artifacts["job"], physical_design_job_from_json, "Physical Design Job"
        )
        result: PhysicalDesignResult = _read_typed(
            artifacts["result"],
            physical_design_result_from_json,
            "Physical Design Result",
        )
        plan: MaterializationPlan = _read_typed(
            artifacts["materialization-plan"],
            materialization_plan_from_json,
            "Materialization Plan",
        )
        closure = None
        if "closure-evidence" in artifacts:
            closure = _read_typed(
                artifacts["closure-evidence"],
                placement_routing_closure_evidence_from_json,
                "Placement-Routing Closure Evidence",
            )
        receipt = (
            None
            if "materialization-receipt" not in artifacts
            else _read_typed(
                artifacts["materialization-receipt"],
                materialization_receipt_from_json,
                "Materialization Receipt",
            )
        )
        drc = (
            None
            if "drc" not in artifacts
            else _read_typed(artifacts["drc"], drc_evidence_from_json, "DRC evidence")
        )
        lvs = (
            None
            if "lvs" not in artifacts
            else _read_typed(artifacts["lvs"], lvs_evidence_from_json, "LVS evidence")
        )
        pex = (
            None
            if "pex" not in artifacts
            else _read_typed(artifacts["pex"], pex_evidence_from_json, "PEX evidence")
        )
        post_layout = (
            None
            if "post-layout" not in artifacts
            else _read_typed(
                artifacts["post-layout"],
                post_layout_evidence_from_json,
                "post-layout evidence",
            )
        )
        qualification = (
            None
            if "qualification" not in artifacts
            else _read_typed(
                artifacts["qualification"],
                qualification_evidence_from_json,
                "qualification evidence",
            )
        )

        job_identity = physical_design_job_id(job)
        result_identity = physical_design_result_id(result)
        plan_identity_value = plan.artifact_id
        issues: list[str] = []
        if result.provenance.job != job:
            issues.append("Physical Design Result typed job does not match the input")
        validation = validate_materialization_plan(job, result, plan)
        if not validation.valid:
            issues.extend(item.code for item in validation.issues)
        if result.closure_evidence != closure:
            issues.append("standalone closure evidence does not match the result")

        _expect_qualifier(artifacts["result"], "job-identity", job_identity, issues)
        _expect_qualifier(
            artifacts["result"], "result-identity", result_identity, issues
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "job-identity",
            job_identity,
            issues,
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "result-identity",
            result_identity,
            issues,
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "plan-identity",
            plan_identity_value,
            issues,
        )
        if "closure-evidence" in artifacts:
            _expect_qualifier(
                artifacts["closure-evidence"],
                "closure-identity",
                physical_closure_evidence_id(closure),
                issues,
            )
        receipt_identity = None if receipt is None else materialization_receipt_id(receipt)
        if receipt is not None:
            receipt_artifact = artifacts["materialization-receipt"]
            layout_path = (
                None if "layout" not in artifacts else artifacts["layout"].path
            )
            receipt_validation = validate_materialization_receipt(
                job,
                result,
                plan,
                receipt.target,
                receipt,
                layout_path=layout_path,
                run_root=flow_result.run_root if layout_path is not None else None,
            )
            if not receipt_validation.valid:
                issues.extend(item.code for item in receipt_validation.issues)
            for name, expected in (
                ("owner", receipt.target.owner),
                ("name", receipt.target.name),
                ("format", receipt.target.format.value),
                ("job-identity", job_identity),
                ("result-identity", result_identity),
                ("plan-identity", plan_identity_value),
                ("receipt-identity", receipt_identity),
                ("status", receipt.status.value),
                ("backend", receipt.completion.backend),
            ):
                _expect_qualifier(receipt_artifact, name, expected, issues)
            if "layout" in artifacts and (
                artifacts["layout"].producer != receipt_artifact.producer
            ):
                issues.append(
                    "layout and Materialization Receipt have different producers"
                )
        elif "layout" in artifacts:
            issues.append("checked layout has no Materialization Receipt")
        if "layout" in artifacts:
            layout_artifact = artifacts["layout"]
            layout_identity_value = _artifact_identity(layout_artifact)
            expected_layout = {
                "job-identity": job_identity,
                "result-identity": result_identity,
                "plan-identity": plan_identity_value,
                "owner": plan.target.owner,
                "name": plan.target.name,
                "layout-identity": layout_identity_value,
            }
            if receipt is not None:
                expected_layout.update(
                    {
                        "receipt-identity": str(receipt_identity),
                        "format": receipt.target.format.value,
                        "status": receipt.status.value,
                        "backend": receipt.completion.backend,
                    }
                )
            for name, expected in expected_layout.items():
                _expect_qualifier(layout_artifact, name, expected, issues)

        layout_identity = (
            None if "layout" not in artifacts else _artifact_identity(artifacts["layout"])
        )
        source_identity = (
            None if "source" not in artifacts else _artifact_identity(artifacts["source"])
        )
        for label, evidence in (("DRC", drc), ("LVS", lvs)):
            if evidence is None:
                continue
            if layout_identity is None:
                issues.append(f"{label} evidence has no bound checked layout artifact")
            elif evidence.layout.artifact_identity != layout_identity:
                issues.append(f"{label} checked layout artifact identity mismatch")
            if evidence.layout.plan_identity != plan_identity_value:
                issues.append(f"{label} checked Materialization Plan identity mismatch")
            if evidence.layout.result_identity != result_identity:
                issues.append(f"{label} checked Physical Design Result identity mismatch")
            if evidence.layout.job_identity != job_identity:
                issues.append(f"{label} checked Physical Design Job identity mismatch")
            if evidence.layout.receipt_identity != receipt_identity:
                issues.append(f"{label} checked Materialization Receipt identity mismatch")
            if receipt is not None:
                if evidence.layout.format != receipt.target.format.value:
                    issues.append(f"{label} checked layout format mismatch")
                if (
                    evidence.layout.owner,
                    evidence.layout.name,
                ) != (receipt.target.owner, receipt.target.name):
                    issues.append(f"{label} checked layout target mismatch")
            evidence_artifact = artifacts[label.lower()]
            for name, expected in (
                ("layout-identity", evidence.layout.artifact_identity),
                ("receipt-identity", evidence.layout.receipt_identity),
                ("job-identity", evidence.layout.job_identity),
                ("result-identity", evidence.layout.result_identity),
                ("plan-identity", evidence.layout.plan_identity),
                ("status", evidence.status.value),
            ):
                _expect_qualifier(evidence_artifact, name, expected, issues)
        if lvs is not None:
            if source_identity is None:
                issues.append("LVS evidence has no bound checked source artifact")
            elif lvs.source.artifact_identity != source_identity:
                issues.append("LVS checked source identity mismatch")
            if "source" in artifacts:
                _expect_qualifier(
                    artifacts["source"], "owner", lvs.source.owner, issues
                )
                _expect_qualifier(
                    artifacts["source"], "name", lvs.source.name, issues
                )
                _expect_qualifier(
                    artifacts["source"],
                    "source-identity",
                    lvs.source.artifact_identity,
                    issues,
                )
            _expect_qualifier(
                artifacts["lvs"],
                "source-identity",
                lvs.source.artifact_identity,
                issues,
            )
        if drc is not None and lvs is not None and drc.layout != lvs.layout:
            issues.append("DRC and LVS did not check the same layout identity")

        def validate_downstream_subject(
            label: str,
            checked_layout: CheckedLayoutIdentity,
            checked_source: CheckedSourceIdentity,
        ) -> None:
            if layout_identity is None:
                issues.append(f"{label} evidence has no bound checked layout artifact")
            elif checked_layout.artifact_identity != layout_identity:
                issues.append(f"{label} checked layout artifact identity mismatch")
            if checked_layout.plan_identity != plan_identity_value:
                issues.append(f"{label} checked Materialization Plan identity mismatch")
            if checked_layout.result_identity != result_identity:
                issues.append(f"{label} checked Physical Design Result identity mismatch")
            if checked_layout.job_identity != job_identity:
                issues.append(f"{label} checked Physical Design Job identity mismatch")
            if checked_layout.receipt_identity != receipt_identity:
                issues.append(f"{label} checked Materialization Receipt identity mismatch")
            if receipt is not None:
                if checked_layout.format != receipt.target.format.value:
                    issues.append(f"{label} checked layout format mismatch")
                if (checked_layout.owner, checked_layout.name) != (
                    receipt.target.owner,
                    receipt.target.name,
                ):
                    issues.append(f"{label} checked layout target mismatch")
            if source_identity is None:
                issues.append(f"{label} evidence has no bound checked source artifact")
            elif checked_source.artifact_identity != source_identity:
                issues.append(f"{label} checked source artifact identity mismatch")
            if "source" in artifacts:
                _expect_qualifier(
                    artifacts["source"], "owner", checked_source.owner, issues
                )
                _expect_qualifier(
                    artifacts["source"], "name", checked_source.name, issues
                )
                _expect_qualifier(
                    artifacts["source"],
                    "source-identity",
                    checked_source.artifact_identity,
                    issues,
                )

        pex_identity = None
        parasitics_identity = None
        if pex is not None:
            validate_downstream_subject("PEX", pex.layout, pex.source)
            pex_artifact = artifacts["pex"]
            pex_identity = _artifact_identity(pex_artifact)
            for name, expected in (
                ("layout-identity", pex.layout.artifact_identity),
                ("receipt-identity", pex.layout.receipt_identity),
                ("job-identity", pex.layout.job_identity),
                ("result-identity", pex.layout.result_identity),
                ("plan-identity", pex.layout.plan_identity),
                ("source-identity", pex.source.artifact_identity),
                ("status", pex.status.value),
                ("evidence-identity", pex_identity),
            ):
                _expect_qualifier(pex_artifact, name, expected, issues)
            if pex.parasitics is not None:
                if "parasitics" not in artifacts:
                    issues.append("PEX evidence has no bound parasitic artifact")
                else:
                    parasitics_artifact = artifacts["parasitics"]
                    parasitics_identity = _artifact_identity(parasitics_artifact)
                    if pex.parasitics.identity != parasitics_identity:
                        issues.append("PEX parasitic artifact identity mismatch")
                    if pex.parasitics.kind != parasitics_artifact.kind:
                        issues.append("PEX parasitic artifact kind mismatch")
                    _expect_qualifier(
                        parasitics_artifact,
                        "pex-evidence-identity",
                        pex_identity,
                        issues,
                    )
                    _expect_qualifier(
                        parasitics_artifact,
                        "parasitics-identity",
                        pex.parasitics.identity,
                        issues,
                    )

        post_layout_identity = None
        if post_layout is not None:
            validate_downstream_subject(
                "post-layout",
                post_layout.layout,
                post_layout.source,
            )
            post_artifact = artifacts["post-layout"]
            post_layout_identity = _artifact_identity(post_artifact)
            if post_layout.pex_evidence_identity != pex_identity:
                issues.append("post-layout PEX evidence identity mismatch")
            if post_layout.parasitics_identity != parasitics_identity:
                issues.append("post-layout parasitic identity mismatch")
            if "post-layout-specification" not in artifacts:
                issues.append("post-layout evidence has no bound specification")
            elif post_layout.specification_identity != _artifact_identity(
                artifacts["post-layout-specification"]
            ):
                issues.append("post-layout specification identity mismatch")
            else:
                _expect_qualifier(
                    artifacts["post-layout-specification"],
                    "specification-identity",
                    post_layout.specification_identity,
                    issues,
                )
            for name, expected in (
                ("layout-identity", post_layout.layout.artifact_identity),
                ("receipt-identity", post_layout.layout.receipt_identity),
                ("source-identity", post_layout.source.artifact_identity),
                ("pex-evidence-identity", post_layout.pex_evidence_identity),
                ("parasitics-identity", post_layout.parasitics_identity),
                ("specification-identity", post_layout.specification_identity),
                ("status", post_layout.status.value),
                ("evidence-identity", post_layout_identity),
            ):
                _expect_qualifier(post_artifact, name, expected, issues)

        if qualification is not None:
            validate_downstream_subject(
                "qualification",
                qualification.layout,
                qualification.source,
            )
            qualification_artifact = artifacts["qualification"]
            qualification_identity = _artifact_identity(qualification_artifact)
            if (
                "drc" not in artifacts
                or qualification.drc_evidence_identity
                != _artifact_identity(artifacts["drc"])
            ):
                issues.append("qualification DRC evidence identity mismatch")
            if (
                "lvs" not in artifacts
                or qualification.lvs_evidence_identity
                != _artifact_identity(artifacts["lvs"])
            ):
                issues.append("qualification LVS evidence identity mismatch")
            if qualification.pex_evidence_identity != pex_identity:
                issues.append("qualification PEX evidence identity mismatch")
            if qualification.post_layout_evidence_identity != post_layout_identity:
                issues.append("qualification post-layout evidence identity mismatch")
            if "qualification-specification" not in artifacts:
                issues.append("qualification evidence has no bound specification")
            elif qualification.specification_identity != _artifact_identity(
                artifacts["qualification-specification"]
            ):
                issues.append("qualification specification identity mismatch")
            else:
                _expect_qualifier(
                    artifacts["qualification-specification"],
                    "specification-identity",
                    qualification.specification_identity,
                    issues,
                )
            for name, expected in (
                ("layout-identity", qualification.layout.artifact_identity),
                ("receipt-identity", qualification.layout.receipt_identity),
                ("source-identity", qualification.source.artifact_identity),
                ("drc-evidence-identity", qualification.drc_evidence_identity),
                ("lvs-evidence-identity", qualification.lvs_evidence_identity),
                ("pex-evidence-identity", qualification.pex_evidence_identity),
                (
                    "post-layout-evidence-identity",
                    qualification.post_layout_evidence_identity,
                ),
                ("specification-identity", qualification.specification_identity),
                ("status", qualification.status.value),
                ("evidence-identity", qualification_identity),
            ):
                _expect_qualifier(qualification_artifact, name, expected, issues)

        routing_quality = None if closure is None else closure.quality
        quality = ClosureQuality(
            identity=(
                ClosureStageStatus.SATISFIED
                if not issues
                else ClosureStageStatus.INVALID_IDENTITY
            ),
            materialization=_materialization_status(
                receipt,
                plan,
                execution_failed=_reference_execution_failed(
                    flow_result,
                    bindings.materialization_receipt,
                ),
            ),
            materialization_decision=plan.acceptance.decision,
            materialization_reason=plan.acceptance.reason,
            drc=(
                _verification_status(drc.status)
                if drc is not None
                else ClosureStageStatus.EXECUTION_FAILED
                if campaign.scope.require_drc
                and _reference_execution_failed(flow_result, bindings.drc)
                else ClosureStageStatus.NOT_EVALUATED
                if campaign.scope.require_drc
                else ClosureStageStatus.NOT_REQUESTED
            ),
            drc_findings=(
                0 if drc is None else sum(item.count for item in drc.violations)
            ),
            lvs=(
                _verification_status(lvs.status)
                if lvs is not None
                else ClosureStageStatus.EXECUTION_FAILED
                if campaign.scope.require_lvs
                and _reference_execution_failed(flow_result, bindings.lvs)
                else ClosureStageStatus.NOT_EVALUATED
                if campaign.scope.require_lvs
                else ClosureStageStatus.NOT_REQUESTED
            ),
            lvs_findings=(
                0 if lvs is None else sum(item.count for item in lvs.mismatches)
            ),
            routing=routing_quality,
            independent_evaluator=_independent_status(routing_quality),
            pex=_downstream_status(
                required=campaign.scope.require_pex,
                reference=bindings.pex,
                evidence_status=None if pex is None else _pex_status(pex.status),
                result=flow_result,
            ),
            post_layout=_downstream_status(
                required=campaign.scope.require_post_layout,
                reference=bindings.post_layout,
                evidence_status=(
                    None
                    if post_layout is None
                    else _analysis_status(post_layout.status)
                ),
                result=flow_result,
            ),
            qualification=_downstream_status(
                required=campaign.scope.require_qualification,
                reference=bindings.qualification,
                evidence_status=(
                    None
                    if qualification is None
                    else _analysis_status(qualification.status)
                ),
                result=flow_result,
            ),
            cost=ClosureCost(
                0
                if routing_quality is None
                else routing_quality.placement_displacement_dbu,
                None if qualification is None else qualification.area_dbu2,
                None if qualification is None else qualification.power_femtowatts,
            ),
        )
        if issues:
            raise ClosureCampaignError("; ".join(issues))
        return _ObservedClosure(
            job,
            result,
            plan,
            receipt,
            drc,
            lvs,
            pex,
            post_layout,
            qualification,
            quality,
            _feedback(closure, plan, receipt, drc, lvs),
            self._provenance(iteration, flow_result, plan_identity, artifacts),
        )

    @staticmethod
    def _direct_termination(observed: _ObservedClosure) -> ClosureCampaignTermination | None:
        result = observed.result
        evidence = result.closure_evidence
        quality = observed.quality
        statuses = (
            quality.materialization,
            quality.drc,
            quality.lvs,
            quality.pex,
            quality.post_layout,
            quality.qualification,
        )
        if quality.closed:
            return ClosureCampaignTermination.CLOSED
        if quality.identity is ClosureStageStatus.INVALID_IDENTITY or any(
            status is ClosureStageStatus.INVALID_IDENTITY for status in statuses
        ):
            return ClosureCampaignTermination.EXECUTION_FAILED
        if result.status is ResultStatus.UNSUPPORTED or any(
            status
            in {
                ClosureStageStatus.UNSUPPORTED,
                ClosureStageStatus.BACKEND_UNAVAILABLE,
            }
            for status in statuses
        ):
            return ClosureCampaignTermination.UNSUPPORTED
        if any(status is ClosureStageStatus.EXECUTION_FAILED for status in statuses):
            return ClosureCampaignTermination.EXECUTION_FAILED
        if evidence is not None and (
            evidence.routing_termination is RoutingTerminationReason.STATE_BUDGET
            or evidence.termination
            is PlacementRoutingTerminationReason.REPAIR_STATE_BUDGET
        ):
            return ClosureCampaignTermination.STATE_BUDGET
        if evidence is not None and (
            evidence.routing_termination is RoutingTerminationReason.ITERATION_BUDGET
            or evidence.termination
            is PlacementRoutingTerminationReason.REPAIR_ITERATION_BUDGET
        ):
            return ClosureCampaignTermination.ITERATION_BUDGET
        if evidence is not None and (
            evidence.routing_termination is RoutingTerminationReason.INFEASIBLE
            or evidence.termination
            is PlacementRoutingTerminationReason.NO_LEGAL_REPAIR
        ):
            return ClosureCampaignTermination.PROVEN_INFEASIBLE
        return None

    def run(self, campaign: ClosureCampaign) -> ClosureCampaignResult:
        campaign_identity = self._campaign_identity(campaign)
        outcomes: list[ClosureIterationResult] = []
        qualities: set[ClosureQuality] = set()
        previous: ClosureQuality | None = None
        for index, iteration in enumerate(campaign.iterations):
            plan_identity = self._engine.plan_id(iteration.plan)
            run_id = f"closure-{campaign.campaign_id}-{iteration.iteration_id}"
            try:
                flow_result = self._engine.run(
                    iteration.plan,
                    artifact_root=self._artifact_root,
                    environment=self._environment,
                    run_id=run_id,
                )
                observed = self._observe(
                    campaign,
                    iteration,
                    flow_result,
                    plan_identity,
                )
            except (FlowExecutionError, ClosureCampaignError) as exc:
                quality = _invalid_quality(campaign.scope)
                provenance = ClosureIterationProvenance(
                    iteration.iteration_id,
                    run_id,
                    plan_identity,
                    iteration.plan.spec.flow_id,
                    iteration.plan.target.target_id,
                    iteration.plan.profile.profile_id,
                    (),
                )
                outcome = ClosureIterationResult(
                    provenance,
                    "failed",
                    quality,
                    None if previous is None else compare_closure_quality(quality, previous),
                    (
                        ClosureFeedbackScope(
                            ClosureFeedbackKind.IDENTITY,
                            ("invalid-campaign-evidence",),
                            (plan_identity,),
                            repairable=False,
                        ),
                    ),
                    ClosureCampaignTermination.EXECUTION_FAILED,
                    None,
                    str(exc),
                )
                outcomes.append(outcome)
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_identity,
                    outcome.decision,
                    tuple(outcomes),
                    quality,
                    str(exc),
                )

            quality = observed.quality
            qualities.add(quality)
            quality_decision = (
                None
                if previous is None
                else compare_closure_quality(quality, previous)
            )
            direct = self._direct_termination(observed)
            repair_plan = None
            if direct is not None:
                decision = direct
            elif not any(scope.repairable for scope in observed.feedback):
                decision = ClosureCampaignTermination.UNSUPPORTED
            elif campaign.repair_policy is None:
                decision = ClosureCampaignTermination.UNSUPPORTED
            else:
                repair_plan = compile_closure_repair(
                    owner=campaign.owner,
                    job=observed.job,
                    result=observed.result,
                    feedback=observed.feedback,
                    policy=campaign.repair_policy,
                    provenance=observed.provenance,
                )
                if repair_plan.decision is ClosureRepairDecision.INVALID_IDENTITY:
                    decision = ClosureCampaignTermination.EXECUTION_FAILED
                elif not repair_plan.accepted:
                    decision = ClosureCampaignTermination.UNSUPPORTED
                elif len(qualities) >= campaign.state_budget:
                    decision = ClosureCampaignTermination.STATE_BUDGET
                elif index + 1 >= campaign.iteration_budget:
                    decision = ClosureCampaignTermination.ITERATION_BUDGET
                else:
                    decision = ClosureCampaignTermination.REPAIR

            message = {
                ClosureCampaignTermination.CLOSED: (
                    "all required typed closure evidence is satisfied"
                ),
                ClosureCampaignTermination.REPAIR: "an attributed repair scope is available",
                ClosureCampaignTermination.UNSUPPORTED: (
                    "closure lacks a supported attributed continuation"
                ),
                ClosureCampaignTermination.PROVEN_INFEASIBLE: (
                    "typed physical evidence proves no legal repair"
                ),
                ClosureCampaignTermination.STATE_BUDGET: "closure state budget is exhausted",
                ClosureCampaignTermination.ITERATION_BUDGET: (
                    "closure iteration budget is exhausted"
                ),
                ClosureCampaignTermination.EXECUTION_FAILED: (
                    "a Flow attempt failed to produce valid typed evidence"
                ),
            }[decision]
            if (
                repair_plan is not None
                and not repair_plan.accepted
                and decision
                in {
                    ClosureCampaignTermination.UNSUPPORTED,
                    ClosureCampaignTermination.EXECUTION_FAILED,
                }
            ):
                message = repair_plan.reason
            outcome = ClosureIterationResult(
                observed.provenance,
                flow_result.status,
                quality,
                quality_decision,
                observed.feedback,
                decision,
                repair_plan,
                message,
            )
            outcomes.append(outcome)
            if decision is not ClosureCampaignTermination.REPAIR:
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_identity,
                    decision,
                    tuple(outcomes),
                    quality,
                    message,
                )
            if index + 1 >= len(campaign.iterations):
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_identity,
                    decision,
                    tuple(outcomes),
                    quality,
                    message,
                )
            previous = quality

        raise AssertionError("validated closure campaign produced no iteration")


__all__ = [
    "CampaignArtifactIdentity",
    "CampaignArtifactReference",
    "ClosureArtifactBindings",
    "ClosureCampaign",
    "ClosureCampaignError",
    "ClosureCampaignResult",
    "ClosureCampaignRunner",
    "ClosureCampaignScope",
    "ClosureCampaignTermination",
    "ClosureCost",
    "ClosureFeedbackKind",
    "ClosureFeedbackScope",
    "ClosureIteration",
    "ClosureIterationProvenance",
    "ClosureIterationResult",
    "ClosureQuality",
    "ClosureQualityDecision",
    "ClosureStageStatus",
    "closure_campaign_result_from_json",
    "compare_closure_quality",
]
