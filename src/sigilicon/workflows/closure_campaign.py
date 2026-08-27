"""Typed multi-round physical closure above the deterministic FlowEngine seam."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
from typing import Mapping

from sigilicon.canonical import canonical_from_json, canonical_json, canonical_sha256
from sigilicon.domain.physical_verification import (
    DrcEvidence,
    LvsEvidence,
    PhysicalVerificationStatus,
    drc_evidence_from_json,
    lvs_evidence_from_json,
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
    physical_design_intent_sha256,
    physical_design_job_from_json,
    physical_design_result_from_json,
    placement_routing_closure_evidence_from_json,
    pnr_execution_sha256,
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


class ClosureFeedbackKind(str, Enum):
    PHYSICAL_OWNER = "physical_owner"
    FIXED_PHYSICAL_BLOCKER = "fixed_physical_blocker"
    DRC_RULE = "drc_rule"
    LVS_MISMATCH = "lvs_mismatch"
    MATERIALIZATION = "materialization"
    IDENTITY = "identity"


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
class ClosureFeedbackScope:
    kind: ClosureFeedbackKind
    identities: tuple[str, ...]
    source_evidence: tuple[str, ...]
    involved_nets: tuple[str, ...] = ()
    involved_groups: tuple[str, ...] = ()
    repairable: bool = False

    def __post_init__(self) -> None:
        if not self.identities or any(
            not isinstance(value, str) or not value for value in self.identities
        ):
            raise ClosureCampaignError("closure feedback needs typed identities")
        if any(
            not isinstance(value, str) or not value
            for value in (
                *self.source_evidence,
                *self.involved_nets,
                *self.involved_groups,
            )
        ):
            raise ClosureCampaignError("closure feedback scope must contain text")
        if type(self.repairable) is not bool:
            raise ClosureCampaignError("closure feedback repairable flag must be bool")


@dataclass(frozen=True)
class CampaignArtifactIdentity:
    label: str
    kind: str
    sha256: str
    producer: str
    role: str

    def __post_init__(self) -> None:
        identifier(self.label, "campaign artifact label")
        identifier(self.kind, "campaign artifact kind")
        identifier(self.producer, "campaign artifact producer")
        identifier(self.role, "campaign artifact role")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ClosureCampaignError("campaign artifact needs a SHA-256 identity")


@dataclass(frozen=True)
class ClosureIterationProvenance:
    iteration_id: str
    run_id: str
    plan_sha256: str
    flow_id: str
    target: str
    execution_profile: str
    artifacts: tuple[CampaignArtifactIdentity, ...]

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "closure iteration")
        run_identity(self.run_id)
        if len(self.plan_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.plan_sha256
        ):
            raise ClosureCampaignError("closure iteration needs a plan SHA-256")
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
    message: str


@dataclass(frozen=True)
class ClosureCampaignResult:
    owner: str
    campaign_id: str
    campaign_sha256: str
    termination: ClosureCampaignTermination
    iterations: tuple[ClosureIterationResult, ...]
    final_quality: ClosureQuality
    message: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "closure campaign result owner")
        identifier(self.campaign_id, "closure campaign result identity")
        if len(self.campaign_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.campaign_sha256
        ):
            raise ClosureCampaignError("closure campaign result needs a SHA-256")
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

    @property
    def closed(self) -> bool:
        return self.termination is ClosureCampaignTermination.CLOSED

    def canonical_json(self) -> str:
        return canonical_json(self)


def closure_campaign_result_from_json(text: str) -> ClosureCampaignResult:
    return canonical_from_json(text, ClosureCampaignResult)


@dataclass(frozen=True)
class _ObservedClosure:
    result: PhysicalDesignResult
    plan: MaterializationPlan
    receipt: MaterializationReceipt | None
    drc: DrcEvidence | None
    lvs: LvsEvidence | None
    quality: ClosureQuality
    feedback: tuple[ClosureFeedbackScope, ...]
    provenance: ClosureIterationProvenance


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
                (drc.layout.artifact_sha256,),
                repairable=True,
            )
        )
    if lvs is not None and lvs.status is PhysicalVerificationStatus.VIOLATED:
        scopes.append(
            ClosureFeedbackScope(
                ClosureFeedbackKind.LVS_MISMATCH,
                tuple(item.category for item in lvs.mismatches),
                (
                    lvs.layout.artifact_sha256,
                    lvs.source.artifact_sha256,
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
            plan.provenance.result_sha256,
            canonical_sha256(plan),
            *(() if receipt is None else (canonical_sha256(receipt),)),
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
        return canonical_sha256(
            {
                "owner": campaign.owner,
                "campaign_id": campaign.campaign_id,
                "state_budget": campaign.state_budget,
                "iteration_budget": campaign.iteration_budget,
                "scope": campaign.scope,
                "iterations": [
                    {
                        "iteration_id": iteration.iteration_id,
                        "plan_sha256": canonical_sha256(
                            self._engine.plan_record(iteration.plan)
                        ),
                        "artifacts": iteration.artifacts,
                    }
                    for iteration in campaign.iterations
                ],
            }
        )

    def _provenance(
        self,
        iteration: ClosureIteration,
        result: FlowResult,
        plan_sha256: str,
        artifacts: Mapping[str, ActionArtifact],
    ) -> ClosureIterationProvenance:
        return ClosureIterationProvenance(
            iteration_id=iteration.iteration_id,
            run_id=result.run_id,
            plan_sha256=plan_sha256,
            flow_id=iteration.plan.spec.flow_id,
            target=iteration.plan.target.target_id,
            execution_profile=iteration.plan.profile.profile_id,
            artifacts=tuple(
                CampaignArtifactIdentity(
                    label,
                    artifact.kind,
                    _file_sha256(artifact.path),
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
        plan_sha256: str,
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

        job_sha256 = canonical_sha256(job)
        result_sha256 = canonical_sha256(result)
        plan_sha256_value = canonical_sha256(plan)
        issues: list[str] = []
        if result.provenance.input_sha256 != physical_design_intent_sha256(job):
            issues.append("Physical Design Result input identity does not match the job")
        if result.provenance.execution_sha256 != pnr_execution_sha256(
            job.execution_policy
        ):
            issues.append(
                "Physical Design Result execution identity does not match the job"
            )
        validation = validate_materialization_plan(job, result, plan)
        if not validation.valid:
            issues.extend(item.code for item in validation.issues)
        if result.closure_evidence != closure:
            issues.append("standalone closure evidence does not match the result")

        _expect_qualifier(artifacts["result"], "job-sha256", job_sha256, issues)
        _expect_qualifier(
            artifacts["result"], "result-sha256", result_sha256, issues
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "job-sha256",
            job_sha256,
            issues,
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "result-sha256",
            result_sha256,
            issues,
        )
        _expect_qualifier(
            artifacts["materialization-plan"],
            "plan-sha256",
            plan_sha256_value,
            issues,
        )
        if "closure-evidence" in artifacts:
            _expect_qualifier(
                artifacts["closure-evidence"],
                "closure-sha256",
                canonical_sha256(closure),
                issues,
            )
        receipt_sha256 = None if receipt is None else canonical_sha256(receipt)
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
                ("job-sha256", job_sha256),
                ("result-sha256", result_sha256),
                ("plan-sha256", plan_sha256_value),
                ("receipt-sha256", receipt_sha256),
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
            layout_sha = _file_sha256(layout_artifact.path)
            expected_layout = {
                "job-sha256": job_sha256,
                "result-sha256": result_sha256,
                "plan-sha256": plan_sha256_value,
                "owner": plan.target.owner,
                "name": plan.target.name,
                "layout-sha256": layout_sha,
            }
            if receipt is not None:
                expected_layout.update(
                    {
                        "receipt-sha256": str(receipt_sha256),
                        "format": receipt.target.format.value,
                        "status": receipt.status.value,
                        "backend": receipt.completion.backend,
                    }
                )
            for name, expected in expected_layout.items():
                _expect_qualifier(layout_artifact, name, expected, issues)

        layout_sha256 = (
            None if "layout" not in artifacts else _file_sha256(artifacts["layout"].path)
        )
        source_sha256 = (
            None if "source" not in artifacts else _file_sha256(artifacts["source"].path)
        )
        for label, evidence in (("DRC", drc), ("LVS", lvs)):
            if evidence is None:
                continue
            if layout_sha256 is None:
                issues.append(f"{label} evidence has no bound checked layout artifact")
            elif evidence.layout.artifact_sha256 != layout_sha256:
                issues.append(f"{label} checked layout artifact identity mismatch")
            if evidence.layout.plan_sha256 != plan_sha256_value:
                issues.append(f"{label} checked Materialization Plan identity mismatch")
            if evidence.layout.result_sha256 != result_sha256:
                issues.append(f"{label} checked Physical Design Result identity mismatch")
            if evidence.layout.job_sha256 != job_sha256:
                issues.append(f"{label} checked Physical Design Job identity mismatch")
            if evidence.layout.receipt_sha256 != receipt_sha256:
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
                ("layout-sha256", evidence.layout.artifact_sha256),
                ("receipt-sha256", evidence.layout.receipt_sha256),
                ("job-sha256", evidence.layout.job_sha256),
                ("result-sha256", evidence.layout.result_sha256),
                ("plan-sha256", evidence.layout.plan_sha256),
                ("status", evidence.status.value),
            ):
                _expect_qualifier(evidence_artifact, name, expected, issues)
        if lvs is not None:
            if source_sha256 is None:
                issues.append("LVS evidence has no bound checked source artifact")
            elif lvs.source.artifact_sha256 != source_sha256:
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
                    "source-sha256",
                    lvs.source.artifact_sha256,
                    issues,
                )
            _expect_qualifier(
                artifacts["lvs"],
                "source-sha256",
                lvs.source.artifact_sha256,
                issues,
            )
        if drc is not None and lvs is not None and drc.layout != lvs.layout:
            issues.append("DRC and LVS did not check the same layout identity")

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
            pex=_future_status(campaign.scope.require_pex),
            post_layout=_future_status(campaign.scope.require_post_layout),
            qualification=_future_status(campaign.scope.require_qualification),
            cost=ClosureCost(
                0
                if routing_quality is None
                else routing_quality.placement_displacement_dbu
            ),
        )
        if issues:
            raise ClosureCampaignError("; ".join(issues))
        return _ObservedClosure(
            result,
            plan,
            receipt,
            drc,
            lvs,
            quality,
            _feedback(closure, plan, receipt, drc, lvs),
            self._provenance(iteration, flow_result, plan_sha256, artifacts),
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
        campaign_sha256 = self._campaign_identity(campaign)
        outcomes: list[ClosureIterationResult] = []
        qualities: set[str] = set()
        previous: ClosureQuality | None = None
        for index, iteration in enumerate(campaign.iterations):
            plan_sha256 = canonical_sha256(self._engine.plan_record(iteration.plan))
            run_id = hashlib.sha256(
                f"{campaign_sha256}:{iteration.iteration_id}:{plan_sha256}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
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
                    plan_sha256,
                )
            except (FlowExecutionError, ClosureCampaignError) as exc:
                quality = _invalid_quality(campaign.scope)
                provenance = ClosureIterationProvenance(
                    iteration.iteration_id,
                    run_id,
                    plan_sha256,
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
                            (plan_sha256,),
                            repairable=False,
                        ),
                    ),
                    ClosureCampaignTermination.EXECUTION_FAILED,
                    str(exc),
                )
                outcomes.append(outcome)
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_sha256,
                    outcome.decision,
                    tuple(outcomes),
                    quality,
                    str(exc),
                )

            quality = observed.quality
            quality_identity = canonical_sha256(quality)
            qualities.add(quality_identity)
            quality_decision = (
                None
                if previous is None
                else compare_closure_quality(quality, previous)
            )
            direct = self._direct_termination(observed)
            if direct is not None:
                decision = direct
            elif not any(scope.repairable for scope in observed.feedback):
                decision = ClosureCampaignTermination.UNSUPPORTED
            elif len(qualities) >= campaign.state_budget:
                decision = ClosureCampaignTermination.STATE_BUDGET
            elif index + 1 >= campaign.iteration_budget:
                decision = ClosureCampaignTermination.ITERATION_BUDGET
            elif index + 1 >= len(campaign.iterations):
                decision = ClosureCampaignTermination.REPAIR
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
            outcome = ClosureIterationResult(
                observed.provenance,
                flow_result.status,
                quality,
                quality_decision,
                observed.feedback,
                decision,
                message,
            )
            outcomes.append(outcome)
            if decision is not ClosureCampaignTermination.REPAIR:
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_sha256,
                    decision,
                    tuple(outcomes),
                    quality,
                    message,
                )
            if index + 1 >= len(campaign.iterations):
                return ClosureCampaignResult(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_sha256,
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
