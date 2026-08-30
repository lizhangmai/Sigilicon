"""Bounded whole-design attempts above the single-attempt deterministic FlowEngine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import json
from pathlib import Path

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
)
from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_SIZING_PROBLEM_KIND,
    CIRCUIT_SIZING_RESULT_KIND,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_BRIEF_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_DECISION_KIND,
    DESIGN_EVIDENCE_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CanonicalDesignArtifact,
    CircuitSizingProblem,
    CircuitSizingResult,
    CircuitTopologyProposal,
    DesignCandidate,
    DesignDecision,
    DesignDecisionConclusion,
    DesignEvidence,
    EvidenceConclusion,
    EvidenceLevel,
    EvidenceRole,
    design_artifact_from_json,
    validate_design_candidate,
    validate_design_decision,
)
from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ExecutionEnvironment,
    FlowContractError,
    FlowEngine,
    FlowPlan,
)
from sigilicon.identifiers import bounded_identity
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.paths import ArtifactLayout
from sigilicon.workflows.design_repair import (
    DesignRepairAttribution,
    DesignRepairProposal,
    RepairCompileDecision,
    SizingRepairPlan,
    SizingRepairPolicy,
    TopologyRepairPlan,
    TopologyRepairPolicy,
    attribute_design_failure,
    compile_design_repair,
)
from sigilicon.workflows.project_flow import ProjectFlow


DESIGN_CAMPAIGN_ITERATION_EXTENSION = "design_campaign_iteration"


@dataclass(frozen=True)
class DesignCampaignIterationInput:
    """Exact cross-round input owned and interpreted by Design Campaign."""

    campaign_identity: str
    iteration: int
    parent_candidate_identity: str
    attribution_json: str
    proposal_json: str
    repair_plan_json: str
    schema: int = 1
    contract_kind: str = "design-campaign-iteration-input"

    def __post_init__(self) -> None:
        if self.schema != 1 or self.contract_kind != "design-campaign-iteration-input":
            raise ValueError("invalid Design Campaign iteration input contract")
        bounded_identity(self.campaign_identity, "Design Campaign identity")
        bounded_identity(
            self.parent_candidate_identity,
            "parent Candidate identity",
        )
        if type(self.iteration) is not int or self.iteration < 2:
            raise ValueError("Design Campaign child iteration must be at least two")
        for value, label in (
            (self.attribution_json, "attribution record"),
            (self.proposal_json, "proposal record"),
            (self.repair_plan_json, "Repair Plan record"),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"Design Campaign {label} must be exact JSON text")


def design_campaign_iteration_input(value: object) -> DesignCampaignIterationInput:
    """Decode the Campaign-owned payload carried by a generic Flow extension."""

    if not isinstance(value, Mapping):
        raise ValueError("Design Campaign iteration extension must be a mapping")
    expected = {
        "campaign_identity",
        "iteration",
        "parent_candidate_identity",
        "attribution_json",
        "proposal_json",
        "repair_plan_json",
        "schema",
        "contract_kind",
    }
    if set(value) != expected:
        raise ValueError("Design Campaign iteration extension fields are invalid")
    return DesignCampaignIterationInput(**dict(value))


class DesignStage(str, Enum):
    TOPOLOGY = "topology"
    L0 = "l0"
    SIZING = "sizing"
    L1 = "l1"
    L2 = "l2"
    PHYSICAL = "physical"
    MATERIALIZATION = "materialization"
    DRC = "drc"
    LVS = "lvs"
    PEX = "pex"
    POST_LAYOUT = "post-layout"
    QUALIFICATION = "qualification"


_STAGE_ORDER = {stage: index for index, stage in enumerate(DesignStage)}


class DesignStageStatus(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    NOT_EVALUATED = "not_evaluated"
    NOT_REQUESTED = "not_requested"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_IDENTITY = "invalid_identity"


class DesignCampaignTermination(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    REPAIR_REQUIRED = "repair_required"
    NON_CONCLUSION = "non_conclusion"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_IDENTITY = "invalid_identity"
    STATE_BUDGET = "state_budget"
    ITERATION_BUDGET = "iteration_budget"
    COST_BUDGET = "cost_budget"
    TIME_BUDGET = "time_budget"


class DesignCampaignPhase(str, Enum):
    PROPOSAL_REQUIRED = "proposal_required"
    COMPLETED = "completed"


@dataclass(frozen=True)
class DesignArtifactBinding:
    label: str
    node: str
    role: str

    def __post_init__(self) -> None:
        identifier(self.label, "Design Campaign artifact label")
        identifier(self.node, "Design Campaign artifact node")
        identifier(self.role, "Design Campaign artifact role")


@dataclass(frozen=True)
class DesignStageBinding:
    stage: DesignStage
    artifact: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, DesignStage):
            raise ValueError("Design Campaign stage binding must be typed")
        identifier(self.artifact, "Design Campaign stage artifact")


DesignRepairPlan = TopologyRepairPlan | SizingRepairPlan


@dataclass(frozen=True)
class DesignCampaignAttemptSpec:
    """Portable selectors and output bindings for the baseline Flow attempt."""

    iteration_id: str
    flow: str
    target: str
    profile: str | None
    candidate: DesignArtifactBinding
    artifacts: tuple[DesignArtifactBinding, ...]
    stages: tuple[DesignStageBinding, ...]

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "Design Campaign iteration")
        identifier(self.flow, "Design Campaign Flow")
        identifier(self.target, "Design Campaign Flow target")
        if self.profile is not None:
            identifier(self.profile, "Design Campaign Execution Profile")


@dataclass(frozen=True)
class DesignCampaignContinuationSpec:
    """Portable selector for repeated attempts derived after proposal validation."""

    flow: str
    target: str
    profile: str | None
    candidate: DesignArtifactBinding
    artifacts: tuple[DesignArtifactBinding, ...]
    stages: tuple[DesignStageBinding, ...]
    proposal_node: str
    repair_policy: DesignRepairPolicy

    def __post_init__(self) -> None:
        identifier(self.flow, "Design Campaign continuation Flow")
        identifier(self.target, "Design Campaign continuation target")
        if self.profile is not None:
            identifier(self.profile, "Design Campaign continuation profile")
        identifier(self.proposal_node, "Design Campaign continuation proposal node")


@dataclass(frozen=True)
class DesignCampaignSpec:
    """Strict client-neutral Campaign input with no paths or backend commands."""

    owner: str
    campaign_id: str
    baseline: DesignCampaignAttemptSpec
    budget: DesignCampaignBudget
    scope: DesignCampaignScope
    continuation: DesignCampaignContinuationSpec | None = None

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign owner")
        identifier(self.campaign_id, "Design Campaign identity")
        if self.scope.decision_policy.owner != self.owner:
            raise ValueError("Design Campaign specification policy owner drift")
        if self.continuation is not None:
            if self.continuation.repair_policy.owner != self.owner:
                raise ValueError("Design Campaign continuation policy owner drift")

    def canonical_json(self) -> str:
        return canonical_json(self)


def design_campaign_spec_from_json(text: str) -> DesignCampaignSpec:
    return canonical_from_exact_json(text, DesignCampaignSpec)


@dataclass(frozen=True)
class DesignCampaignAttempt:
    iteration_id: str
    plan: FlowPlan
    candidate: DesignArtifactBinding
    artifacts: tuple[DesignArtifactBinding, ...]
    stages: tuple[DesignStageBinding, ...]
    repair_plan: DesignRepairPlan | None

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "Design Campaign iteration")
        planned = set(self.plan.topology)
        bindings = (self.candidate, *self.artifacts)
        if any(item.node not in planned for item in bindings):
            raise ValueError("Design Campaign artifact binding references an unplanned node")
        labels = tuple(item.label for item in bindings)
        if len(labels) != len(set(labels)):
            raise ValueError("Design Campaign artifact labels must be unique")
        stage_values = tuple(item.stage for item in self.stages)
        if len(stage_values) != len(set(stage_values)):
            raise ValueError("Design Campaign stages must be unique per attempt")
        known = set(labels)
        if any(item.artifact not in known for item in self.stages):
            raise ValueError("Design Campaign stage references an unknown artifact label")
        if self.repair_plan is not None and not self.repair_plan.accepted:
            raise ValueError("Design Campaign continuation requires an accepted Repair Plan")


DesignRepairPolicy = TopologyRepairPolicy | SizingRepairPolicy


@dataclass(frozen=True)
class DesignCampaignContinuation:
    """Reusable project-owned attempt template; no future result is enumerated."""

    plan: FlowPlan
    candidate: DesignArtifactBinding
    artifacts: tuple[DesignArtifactBinding, ...]
    stages: tuple[DesignStageBinding, ...]
    proposal_node: str
    repair_policy: DesignRepairPolicy

    def __post_init__(self) -> None:
        identifier(self.proposal_node, "Design Campaign proposal input node")
        if self.proposal_node not in self.plan.topology:
            raise ValueError("Design Campaign proposal input node is outside the plan")
        if (
            DESIGN_CAMPAIGN_ITERATION_EXTENSION
            in self.plan.spec.node(self.proposal_node).extensions
        ):
            raise ValueError("Design Campaign continuation reserves its iteration input")
        if self.repair_policy.owner != self.plan.spec.owner:
            raise ValueError("Design Campaign continuation repair policy owner drift")
        DesignCampaignAttempt(
            "continuation-template",
            self.plan,
            self.candidate,
            self.artifacts,
            self.stages,
            None,
        )


@dataclass(frozen=True)
class DesignCampaignBudget:
    state_budget: int
    iteration_budget: int
    maximum_flow_nodes: int
    maximum_seconds: int = 86_400

    def __post_init__(self) -> None:
        if type(self.state_budget) is not int or self.state_budget <= 0:
            raise ValueError("Design Campaign state budget must be positive")
        if type(self.iteration_budget) is not int or self.iteration_budget <= 0:
            raise ValueError("Design Campaign iteration budget must be positive")
        if type(self.maximum_flow_nodes) is not int or self.maximum_flow_nodes < 0:
            raise ValueError("Design Campaign Flow-node cost budget must be non-negative")
        if (
            type(self.maximum_seconds) is not int
            or not 1 <= self.maximum_seconds <= 86_400
        ):
            raise ValueError("Design Campaign time budget must be between 1 and 86400 seconds")


@dataclass(frozen=True)
class DesignCampaignScope:
    required_stages: tuple[DesignStage, ...]
    decision_role: EvidenceRole
    decision_level: EvidenceLevel
    decision_scope: tuple[str, ...]
    decision_policy: ArtifactReference

    def __post_init__(self) -> None:
        if not self.required_stages:
            raise ValueError("Design Campaign scope needs at least one required stage")
        if any(not isinstance(item, DesignStage) for item in self.required_stages):
            raise ValueError("Design Campaign required stages must be typed")
        expected = tuple(sorted(set(self.required_stages), key=_STAGE_ORDER.__getitem__))
        if self.required_stages != expected:
            raise ValueError("Design Campaign required stages must be unique and ordered")
        if not isinstance(self.decision_role, EvidenceRole) or not isinstance(
            self.decision_level, EvidenceLevel
        ):
            raise ValueError("Design Campaign decision role and level must be typed")
        if not self.decision_scope:
            raise ValueError("Design Campaign decision scope must not be empty")
        if self.decision_scope != tuple(sorted(set(self.decision_scope))):
            raise ValueError("Design Campaign decision scope must be unique and sorted")
        for value in self.decision_scope:
            identifier(value, "Design Campaign decision scope")


@dataclass(frozen=True)
class DesignCampaign:
    owner: str
    campaign_id: str
    baseline: DesignCampaignAttempt
    budget: DesignCampaignBudget
    scope: DesignCampaignScope
    continuation: DesignCampaignContinuation | None = None

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign owner")
        identifier(self.campaign_id, "Design Campaign identity")
        if self.baseline.plan.spec.owner != self.owner:
            raise ValueError("Design Campaign attempt owner drift")
        if self.scope.decision_policy.owner != self.owner:
            raise ValueError("Design Campaign decision policy owner drift")
        if self.baseline.repair_plan is not None:
            raise ValueError("Design Campaign baseline attempt cannot have a Repair Plan")
        if self.continuation is not None:
            if self.continuation.plan.spec.owner != self.owner:
                raise ValueError("Design Campaign continuation owner drift")


@dataclass(frozen=True)
class DesignStageAssessment:
    stage: DesignStage
    status: DesignStageStatus
    evidence: ArtifactReference | None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, DesignStage) or not isinstance(
            self.status, DesignStageStatus
        ):
            raise ValueError("Design Campaign assessment must be typed")
        if self.evidence is not None and self.evidence.kind != DESIGN_EVIDENCE_KIND:
            raise ValueError("Design Campaign assessment evidence has the wrong kind")


@dataclass(frozen=True)
class DesignQuality:
    stages: tuple[DesignStageAssessment, ...]
    flow_nodes: int

    def __post_init__(self) -> None:
        values = tuple(item.stage for item in self.stages)
        if values != tuple(sorted(set(values), key=_STAGE_ORDER.__getitem__)):
            raise ValueError("Design quality stages must be unique and ordered")
        if type(self.flow_nodes) is not int or self.flow_nodes < 0:
            raise ValueError("Design quality Flow-node cost must be non-negative")

    def status(self, stage: DesignStage) -> DesignStageStatus:
        return next(
            (item.status for item in self.stages if item.stage is stage),
            DesignStageStatus.NOT_REQUESTED,
        )


@dataclass(frozen=True)
class DesignCampaignArtifactIdentity:
    label: str
    kind: str
    identity: str
    producer: str
    role: str
    record_json: str

    def __post_init__(self) -> None:
        identifier(self.label, "Design Campaign artifact identity label")
        identifier(self.kind, "Design Campaign artifact kind")
        bounded_identity(self.identity, "Design Campaign artifact")
        identifier(self.producer, "Design Campaign artifact producer")
        identifier(self.role, "Design Campaign artifact role")
        parsed = design_artifact_from_json(self.record_json)
        if (
            parsed.canonical_json() != self.record_json
            or parsed.metadata.kind != self.kind
            or parsed.identity != self.identity
        ):
            raise ValueError("Design Campaign artifact typed record drift")


@dataclass(frozen=True)
class DesignAttemptProvenance:
    iteration_id: str
    run_id: str
    plan_identity: str
    flow_id: str
    target: str
    execution_profile: str
    artifacts: tuple[DesignCampaignArtifactIdentity, ...]

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "Design Campaign provenance iteration")
        run_identity(self.run_id)
        bounded_identity(self.plan_identity, "Design Campaign Flow Plan")
        identifier(self.flow_id, "Design Campaign Flow")
        identifier(self.target, "Design Campaign Flow target")
        identifier(self.execution_profile, "Design Campaign Execution Profile")
        labels = tuple(item.label for item in self.artifacts)
        if labels != tuple(sorted(set(labels))):
            raise ValueError("Design Campaign provenance labels must be unique and sorted")


@dataclass(frozen=True)
class DesignCampaignIterationResult:
    provenance: DesignAttemptProvenance
    candidate: DesignCandidate
    quality: DesignQuality
    decision: DesignDecision
    message: str

    def __post_init__(self) -> None:
        if self.decision.candidate != self.candidate.reference():
            raise ValueError("Design Campaign iteration decision Candidate drift")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Design Campaign iteration needs a message")


@dataclass(frozen=True)
class DesignCampaignResult:
    owner: str
    campaign_id: str
    campaign_identity: str
    termination: DesignCampaignTermination
    iterations: tuple[DesignCampaignIterationResult, ...]
    final_quality: DesignQuality | None
    final_decision: DesignDecision | None
    message: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign result owner")
        identifier(self.campaign_id, "Design Campaign result identity")
        bounded_identity(self.campaign_identity, "Design Campaign")
        if not isinstance(self.termination, DesignCampaignTermination):
            raise ValueError("Design Campaign termination must be typed")
        if self.iterations:
            if self.final_quality != self.iterations[-1].quality:
                raise ValueError("Design Campaign final quality drift")
            if self.final_decision != self.iterations[-1].decision:
                raise ValueError("Design Campaign final decision drift")
        elif self.final_quality is not None or self.final_decision is not None:
            raise ValueError("unexecuted Design Campaign cannot have a final decision")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Design Campaign result needs a message")

    def canonical_json(self) -> str:
        return canonical_json(self)


def design_campaign_result_from_json(text: str) -> DesignCampaignResult:
    return canonical_from_exact_json(text, DesignCampaignResult)


@dataclass(frozen=True)
class DesignCampaignState:
    """One resumable immutable Campaign checkpoint."""

    owner: str
    campaign_id: str
    campaign_identity: str
    phase: DesignCampaignPhase
    termination: DesignCampaignTermination
    iterations: tuple[DesignCampaignIterationResult, ...]
    attribution: DesignRepairAttribution | None
    repair_plan: DesignRepairPlan | None
    last_proposal_identity: str | None
    message: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign state owner")
        identifier(self.campaign_id, "Design Campaign state identity")
        bounded_identity(self.campaign_identity, "Design Campaign state")
        if not isinstance(self.phase, DesignCampaignPhase):
            raise ValueError("Design Campaign phase must be typed")
        if not isinstance(self.termination, DesignCampaignTermination):
            raise ValueError("Design Campaign state termination must be typed")
        if not self.iterations:
            if (
                self.phase is not DesignCampaignPhase.COMPLETED
                or self.termination
                in {
                    DesignCampaignTermination.PASSED,
                    DesignCampaignTermination.FAILED,
                    DesignCampaignTermination.REPAIR_REQUIRED,
                }
                or self.attribution is not None
                or self.repair_plan is not None
                or self.last_proposal_identity is not None
            ):
                raise ValueError(
                    "unobserved Design Campaign state must be a terminal fail-closed result"
                )
        if self.phase is DesignCampaignPhase.PROPOSAL_REQUIRED:
            if (
                self.termination is not DesignCampaignTermination.REPAIR_REQUIRED
                or self.attribution is None
            ):
                raise ValueError("proposal-required Campaign needs typed attribution")
        elif self.termination is DesignCampaignTermination.REPAIR_REQUIRED:
            raise ValueError("completed Campaign cannot require a proposal")
        if self.attribution is not None:
            if (
                self.attribution.owner != self.owner
                or self.attribution.candidate
                != self.iterations[-1].candidate.reference()
            ):
                raise ValueError("Design Campaign attribution identity drift")
        if self.repair_plan is not None and self.repair_plan.owner != self.owner:
            raise ValueError("Design Campaign Repair Plan owner drift")
        if self.last_proposal_identity is not None:
            bounded_identity(self.last_proposal_identity, "Design Campaign proposal")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Design Campaign state needs a message")

    def canonical_json(self) -> str:
        return canonical_json(self)


def design_campaign_state_from_json(text: str) -> DesignCampaignState:
    return canonical_from_exact_json(text, DesignCampaignState)


_CONCLUSION_STATUS = {
    EvidenceConclusion.SATISFIED: DesignStageStatus.SATISFIED,
    EvidenceConclusion.VIOLATED: DesignStageStatus.VIOLATED,
    EvidenceConclusion.NOT_EVALUATED: DesignStageStatus.NOT_EVALUATED,
    EvidenceConclusion.UNSUPPORTED: DesignStageStatus.UNSUPPORTED,
    EvidenceConclusion.BACKEND_UNAVAILABLE: DesignStageStatus.BACKEND_UNAVAILABLE,
    EvidenceConclusion.EXECUTION_FAILED: DesignStageStatus.EXECUTION_FAILED,
    EvidenceConclusion.INVALID_IDENTITY: DesignStageStatus.INVALID_IDENTITY,
}


class DesignCampaignRunner:
    """Execute explicit typed attempts without adding state to FlowEngine."""

    def __init__(
        self,
        engine: FlowEngine,
        *,
        artifact_root: Path,
        environment: ExecutionEnvironment | None = None,
        execution_context_identity: str = "default-execution-context",
    ) -> None:
        root = Path(artifact_root).resolve()
        if root == Path(root.anchor):
            raise ValueError("Design Campaign artifact root cannot be a filesystem root")
        self._engine = engine
        self._artifact_root = root
        self._environment = environment
        if (
            not isinstance(execution_context_identity, str)
            or not execution_context_identity
            or len(execution_context_identity) > 128
            or any(ord(character) < 0x20 for character in execution_context_identity)
        ):
            raise ValueError("Design Campaign execution context identity is invalid")
        self._execution_context_identity = execution_context_identity

    def plan_record(self, campaign: DesignCampaign) -> dict[str, object]:
        """Return the canonical portable record authorized by owner policy."""

        self._validate_bindings(campaign)
        value = {
            "owner": campaign.owner,
            "campaign_id": campaign.campaign_id,
            "budget": campaign.budget,
            "scope": campaign.scope,
            "baseline": {
                "iteration_id": campaign.baseline.iteration_id,
                "plan": self._engine.plan_record(campaign.baseline.plan),
                "candidate": campaign.baseline.candidate,
                "artifacts": campaign.baseline.artifacts,
                "stages": campaign.baseline.stages,
            },
            "continuation": (
                None
                if campaign.continuation is None
                else {
                    "plan": self._engine.plan_record(campaign.continuation.plan),
                    "candidate": campaign.continuation.candidate,
                    "artifacts": campaign.continuation.artifacts,
                    "stages": campaign.continuation.stages,
                    "proposal_node": campaign.continuation.proposal_node,
                    "repair_policy": campaign.continuation.repair_policy,
                }
            ),
        }
        return json.loads(canonical_json(value))

    def _validate_bindings(self, campaign: DesignCampaign) -> None:
        canonical_kinds = {
            DESIGN_BRIEF_KIND,
            DESIGN_CANDIDATE_KIND,
            CIRCUIT_TOPOLOGY_KIND,
            CIRCUIT_SIZING_PROBLEM_KIND,
            CIRCUIT_SIZING_RESULT_KIND,
            DESIGN_EVIDENCE_KIND,
        }
        for attempt in (campaign.baseline,):
            candidate = self._engine.planned_output(
                attempt.plan,
                attempt.candidate.node,
                attempt.candidate.role,
            )
            if candidate.kind != DESIGN_CANDIDATE_KIND:
                raise ValueError(
                    "Design Campaign candidate binding must produce a Design Candidate"
                )
            labels: dict[str, str] = {}
            for binding in attempt.artifacts:
                port = self._engine.planned_output(
                    attempt.plan,
                    binding.node,
                    binding.role,
                )
                if port.kind not in canonical_kinds:
                    raise ValueError(
                        "Design Campaign artifact binding must produce a canonical design artifact"
                    )
                labels[binding.label] = port.kind
            for binding in attempt.stages:
                if labels.get(binding.artifact) != DESIGN_EVIDENCE_KIND:
                    raise ValueError(
                        "Design Campaign stage binding must produce Design Evidence"
                    )
        if campaign.continuation is not None:
            try:
                self._engine.validate_extensions(
                    campaign.continuation.plan,
                    campaign.continuation.proposal_node,
                    (DESIGN_CAMPAIGN_ITERATION_EXTENSION,),
                )
            except FlowContractError as exc:
                raise FlowContractError(
                    "typed Design Campaign continuation Action and Adapter must "
                    "accept the iteration extension"
                ) from exc
            template = DesignCampaignAttempt(
                "continuation-template",
                campaign.continuation.plan,
                campaign.continuation.candidate,
                campaign.continuation.artifacts,
                campaign.continuation.stages,
                None,
            )
            self._validate_bindings(
                DesignCampaign(
                    campaign.owner,
                    "continuation-validation",
                    template,
                    campaign.budget,
                    campaign.scope,
                )
            )

    def campaign_identity(self, campaign: DesignCampaign) -> str:
        return f"{campaign.owner}:design-campaign:{campaign.campaign_id}"

    @staticmethod
    def _artifact(flow_result: object, binding: DesignArtifactBinding) -> object:
        try:
            node = flow_result.nodes[binding.node]
            artifact = node.artifacts[binding.role]
        except (AttributeError, KeyError) as exc:
            raise ValueError(
                f"Design Campaign artifact is unavailable: {binding.node}.{binding.role}"
            ) from exc
        return artifact

    @staticmethod
    def _load_artifact(artifact: object) -> CanonicalDesignArtifact:
        value = design_artifact_from_json(read_nofollow_text(artifact.path))
        if value.metadata.kind != artifact.kind:
            raise ValueError("Design Campaign artifact kind identity drift")
        return value

    def _observe(
        self,
        campaign: DesignCampaign,
        attempt: DesignCampaignAttempt,
        flow_result: object,
        plan_identity: str,
        lineage: tuple[DesignCandidate, ...],
    ) -> DesignCampaignIterationResult:
        candidate_artifact = self._artifact(flow_result, attempt.candidate)
        candidate_value = self._load_artifact(candidate_artifact)
        if not isinstance(candidate_value, DesignCandidate):
            raise ValueError("Design Campaign candidate binding has the wrong artifact kind")
        artifacts: dict[str, CanonicalDesignArtifact] = {}
        produced = {attempt.candidate.label: candidate_artifact}
        for binding in attempt.artifacts:
            raw = self._artifact(flow_result, binding)
            artifacts[binding.label] = self._load_artifact(raw)
            produced[binding.label] = raw
        validate_design_candidate(
            candidate_value,
            (*tuple(artifacts.values()), *lineage),
        )
        if isinstance(attempt.repair_plan, SizingRepairPlan):
            repaired_problem = next(
                (
                    item
                    for item in artifacts.values()
                    if isinstance(item, CircuitSizingProblem)
                    and item.reference() == candidate_value.sizing_problem
                ),
                None,
            )
            if (
                repaired_problem is None
                or attempt.repair_plan.proposed_candidate
                not in repaired_problem.candidates
                or candidate_value.sizing_problem != repaired_problem.reference()
            ):
                raise ValueError(
                    "Design Campaign sizing repair proposal is not bound by the child Candidate"
                )
            repaired_result = next(
                (
                    item
                    for item in artifacts.values()
                    if isinstance(item, CircuitSizingResult)
                    and item.reference() == candidate_value.sizing_result
                ),
                None,
            )
            if (
                repaired_result is None
                or repaired_result.problem != repaired_problem.reference()
                or repaired_result.selected_candidate
                != attempt.repair_plan.proposed_candidate.name
            ):
                raise ValueError(
                    "Design Campaign sizing child did not select the proposed Candidate"
                )
            if not any(
                isinstance(item, DesignEvidence)
                and item.subject == repaired_result.reference()
                and item.reference() in candidate_value.evidence
                for item in artifacts.values()
            ):
                raise ValueError(
                    "Design Campaign sizing child lacks result-bound verification evidence"
                )

        stage_bindings = {item.stage: item.artifact for item in attempt.stages}
        assessments: list[DesignStageAssessment] = []
        evidence_by_stage: dict[DesignStage, DesignEvidence] = {}
        observed_stages = tuple(
            sorted(
                set(campaign.scope.required_stages) | set(stage_bindings),
                key=_STAGE_ORDER.__getitem__,
            )
        )
        for stage in observed_stages:
            label = stage_bindings.get(stage)
            if label is None:
                assessments.append(
                    DesignStageAssessment(stage, DesignStageStatus.NOT_EVALUATED, None)
                )
                continue
            artifact = artifacts.get(label)
            if not isinstance(artifact, DesignEvidence):
                raise ValueError("Design Campaign stage binding must resolve Design Evidence")
            if stage in campaign.scope.required_stages and (
                artifact.role is not campaign.scope.decision_role
                or artifact.level is not campaign.scope.decision_level
                or artifact.specification != campaign.scope.decision_policy
                or not set(campaign.scope.decision_scope).issubset(artifact.scope)
            ):
                raise ValueError(
                    "Design Campaign required-stage evidence role, level, scope, "
                    "or specification drift"
                )
            evidence_by_stage[stage] = artifact
            assessments.append(
                DesignStageAssessment(
                    stage,
                    _CONCLUSION_STATUS[artifact.conclusion],
                    artifact.reference(),
                )
            )
        quality = DesignQuality(tuple(assessments), len(attempt.plan.nodes))
        decision_evidence = tuple(
            evidence_by_stage[stage]
            for stage in campaign.scope.required_stages
            if stage in evidence_by_stage
            and evidence_by_stage[stage].role is campaign.scope.decision_role
            and evidence_by_stage[stage].level is campaign.scope.decision_level
            and evidence_by_stage[stage].specification == campaign.scope.decision_policy
            and set(campaign.scope.decision_scope).issubset(
                evidence_by_stage[stage].scope
            )
        )
        required_statuses = tuple(
            quality.status(stage) for stage in campaign.scope.required_stages
        )
        if len(decision_evidence) == len(campaign.scope.required_stages) and all(
            status is DesignStageStatus.SATISFIED for status in required_statuses
        ):
            conclusion = DesignDecisionConclusion.PASSED
            rationale = "every required stage has exact satisfied policy-bound evidence"
        elif any(
            status is DesignStageStatus.VIOLATED for status in required_statuses
        ) and any(
            item.conclusion is EvidenceConclusion.VIOLATED
            for item in decision_evidence
        ):
            conclusion = DesignDecisionConclusion.FAILED
            rationale = "typed policy-bound evidence records a required-stage violation"
        else:
            conclusion = DesignDecisionConclusion.NON_CONCLUSION
            rationale = "required policy-bound evidence is incomplete or non-conclusive"
        decision = DesignDecision(
            ArtifactMetadata(
                ARTIFACT_SCHEMA,
                DESIGN_DECISION_KIND,
                campaign.owner,
                f"{campaign.owner}:design-decision:{campaign.campaign_id}:{attempt.iteration_id}",
            ),
            candidate_value.reference(),
            campaign.scope.decision_policy,
            campaign.scope.decision_role,
            campaign.scope.decision_level,
            campaign.scope.decision_scope,
            tuple(item.reference() for item in decision_evidence),
            conclusion,
            rationale,
        )
        validate_design_decision(decision, candidate_value, decision_evidence)
        identities = tuple(
            DesignCampaignArtifactIdentity(
                label,
                artifact.kind,
                parsed.identity,
                artifact.producer,
                artifact.role,
                parsed.canonical_json(),
            )
            for label, artifact in sorted(produced.items())
            for parsed in (
                candidate_value if label == attempt.candidate.label else artifacts[label],
            )
        )
        provenance = DesignAttemptProvenance(
            attempt.iteration_id,
            flow_result.run_id,
            plan_identity,
            attempt.plan.spec.flow_id,
            attempt.plan.target.target_id,
            attempt.plan.profile.profile_id,
            identities,
        )
        return DesignCampaignIterationResult(
            provenance,
            candidate_value,
            quality,
            decision,
            rationale,
        )

    @staticmethod
    def _termination(iteration: DesignCampaignIterationResult) -> DesignCampaignTermination:
        if iteration.decision.conclusion is DesignDecisionConclusion.PASSED:
            return DesignCampaignTermination.PASSED
        if iteration.decision.conclusion is DesignDecisionConclusion.FAILED:
            return DesignCampaignTermination.FAILED
        statuses = tuple(item.status for item in iteration.quality.stages)
        for status, termination in (
            (DesignStageStatus.INVALID_IDENTITY, DesignCampaignTermination.INVALID_IDENTITY),
            (DesignStageStatus.EXECUTION_FAILED, DesignCampaignTermination.EXECUTION_FAILED),
            (DesignStageStatus.BACKEND_UNAVAILABLE, DesignCampaignTermination.BACKEND_UNAVAILABLE),
            (DesignStageStatus.UNSUPPORTED, DesignCampaignTermination.UNSUPPORTED),
        ):
            if status in statuses:
                return termination
        return DesignCampaignTermination.NON_CONCLUSION

    def _empty_result(
        self,
        campaign: DesignCampaign,
        campaign_identity: str,
        termination: DesignCampaignTermination,
        message: str,
    ) -> DesignCampaignResult:
        return DesignCampaignResult(
            campaign.owner,
            campaign.campaign_id,
            campaign_identity,
            termination,
            (),
            None,
            None,
            message,
        )

    @staticmethod
    def _repair_parent_matches(
        previous: DesignCampaignIterationResult,
        repair: DesignRepairPlan,
    ) -> bool:
        if repair.owner != previous.candidate.metadata.owner:
            return False
        if repair.parent_candidate_identity != previous.candidate.identity:
            return False
        if not repair.evidence_identity or not set(repair.evidence_identity).issubset(
            reference.identity for reference in previous.candidate.evidence
        ):
            return False
        if isinstance(repair, TopologyRepairPlan):
            return repair.parent_topology_identity == previous.candidate.topology.identity
        return (
            previous.candidate.sizing_problem is not None
            and previous.candidate.sizing_result is not None
            and repair.parent_problem_identity
            == previous.candidate.sizing_problem.identity
            and repair.parent_result_identity
            == previous.candidate.sizing_result.identity
        )

    @staticmethod
    def _repair_child_matches(
        previous: DesignCampaignIterationResult,
        current: DesignCampaignIterationResult,
        repair: DesignRepairPlan,
    ) -> bool:
        if current.candidate.parent_candidate != previous.candidate.reference():
            return False
        if isinstance(repair, TopologyRepairPlan):
            assert repair.proposed_topology is not None
            return current.candidate.topology == repair.proposed_topology.reference()
        return (
            current.candidate.topology == previous.candidate.topology
            and current.candidate.sizing_problem is not None
            and current.candidate.sizing_result is not None
            and current.candidate.sizing_problem != previous.candidate.sizing_problem
            and current.candidate.sizing_result != previous.candidate.sizing_result
        )

    def _execute_attempt(
        self,
        campaign: DesignCampaign,
        attempt: DesignCampaignAttempt,
        lineage: tuple[DesignCandidate, ...],
    ) -> DesignCampaignIterationResult:
        plan_identity = self._engine.plan_id(attempt.plan)
        run_id = (
            f"design-{campaign.campaign_id}-{attempt.iteration_id}-"
            f"{self._execution_context_identity}"
        )
        run_paths = ArtifactLayout(self._artifact_root).execution(
            owner=attempt.plan.spec.owner,
            target=attempt.plan.target.target_id,
            flow=attempt.plan.spec.flow_id,
            variant="default",
            identity=run_id,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        flow_result = (
            self._engine.restore_result(
                attempt.plan,
                artifact_root=self._artifact_root,
                run_id=run_id,
            )
            if run_paths.root.exists()
            else self._engine.run(
                attempt.plan,
                artifact_root=self._artifact_root,
                environment=self._environment,
                run_id=run_id,
            )
        )
        if flow_result.status != "accepted":
            raise RuntimeError("Design Campaign Flow attempt did not reach accepted state")
        return self._observe(
            campaign,
            attempt,
            flow_result,
            plan_identity,
            lineage,
        )

    def _reload_iteration_values(
        self,
        attempt: DesignCampaignAttempt,
        iteration: DesignCampaignIterationResult,
    ) -> tuple[CanonicalDesignArtifact, ...]:
        restored = self._engine.restore_result(
            attempt.plan,
            artifact_root=self._artifact_root,
            run_id=iteration.provenance.run_id,
        )
        expected = {item.label: item for item in iteration.provenance.artifacts}
        values: list[CanonicalDesignArtifact] = []
        for binding in (attempt.candidate, *attempt.artifacts):
            try:
                artifact = restored.nodes[binding.node].artifacts[binding.role]
            except KeyError as exc:
                raise ValueError("Design Campaign persisted artifact binding drift") from exc
            value = self._load_artifact(artifact)
            identity = expected.get(binding.label)
            if (
                identity is None
                or identity.kind != artifact.kind
                or identity.identity != value.identity
                or identity.producer != artifact.producer
                or identity.role != artifact.role
                or identity.record_json != value.canonical_json()
            ):
                raise ValueError("Design Campaign persisted artifact typed record drift")
            values.append(value)
        return tuple(values)

    @staticmethod
    def _failure_inputs(
        candidate: DesignCandidate,
        values: tuple[CanonicalDesignArtifact, ...],
    ) -> tuple[
        tuple[DesignEvidence, ...],
        CircuitTopologyProposal | CircuitSizingProblem,
        object | None,
    ]:
        declared = {item.identity for item in candidate.evidence}
        evidence = tuple(
            item
            for item in values
            if isinstance(item, DesignEvidence)
            and item.identity in declared
            and item.conclusion is EvidenceConclusion.VIOLATED
        )
        if not evidence:
            raise ValueError("Design Campaign has no Candidate-bound violated evidence")
        subjects = {item.subject for item in evidence}
        if len(subjects) != 1:
            raise ValueError("Design Campaign violated evidence has multiple subjects")
        subject = next(iter(subjects))
        parent = next(
            (
                item
                for item in values
                if isinstance(item, (CircuitTopologyProposal, CircuitSizingProblem))
                and item.reference() == subject
            ),
            None,
        )
        parent_result = next(
            (
                item
                for item in values
                if isinstance(item, CircuitSizingResult) and item.reference() == subject
            ),
            None,
        )
        if parent is None and parent_result is not None:
            parent = next(
                (
                    item
                    for item in values
                    if isinstance(item, CircuitSizingProblem)
                    and item.reference() == parent_result.problem
                ),
                None,
            )
        if parent is None:
            raise ValueError("Design Campaign violated evidence subject is unavailable")
        return evidence, parent, parent_result

    def _proposal_required_state(
        self,
        campaign: DesignCampaign,
        iterations: tuple[DesignCampaignIterationResult, ...],
        *,
        repair_plan: DesignRepairPlan | None = None,
        last_proposal_identity: str | None = None,
        elapsed_seconds: int = 0,
    ) -> DesignCampaignState:
        continuation = campaign.continuation
        if continuation is None:
            raise ValueError("Design Campaign has no continuation template")
        budget_stop = self._budget_stop(
            campaign,
            iterations,
            next_node_cost=len(continuation.plan.nodes),
            elapsed_seconds=elapsed_seconds,
        )
        if budget_stop is not None:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                self.campaign_identity(campaign),
                DesignCampaignPhase.COMPLETED,
                budget_stop,
                iterations,
                None,
                repair_plan,
                last_proposal_identity,
                f"Design Campaign stopped with {budget_stop.value}",
            )
        attempt = (
            campaign.baseline
            if len(iterations) == 1
            else DesignCampaignAttempt(
                iterations[-1].provenance.iteration_id,
                continuation.plan,
                continuation.candidate,
                continuation.artifacts,
                continuation.stages,
                repair_plan,
            )
        )
        values = self._reload_iteration_values(attempt, iterations[-1])
        evidence, _parent, _parent_result = self._failure_inputs(
            iterations[-1].candidate,
            values,
        )
        attribution = attribute_design_failure(iterations[-1].candidate, evidence)
        return DesignCampaignState(
            campaign.owner,
            campaign.campaign_id,
            self.campaign_identity(campaign),
            DesignCampaignPhase.PROPOSAL_REQUIRED,
            DesignCampaignTermination.REPAIR_REQUIRED,
            iterations,
            attribution,
            repair_plan,
            last_proposal_identity,
            "typed violated evidence requires a semantic repair proposal",
        )

    @staticmethod
    def _budget_stop(
        campaign: DesignCampaign,
        iterations: tuple[DesignCampaignIterationResult, ...],
        *,
        next_node_cost: int,
        elapsed_seconds: int,
    ) -> DesignCampaignTermination | None:
        if type(elapsed_seconds) is not int or elapsed_seconds < 0:
            raise ValueError("Design Campaign elapsed time must be non-negative seconds")
        if elapsed_seconds >= campaign.budget.maximum_seconds:
            return DesignCampaignTermination.TIME_BUDGET
        if len(iterations) >= campaign.budget.iteration_budget:
            return DesignCampaignTermination.ITERATION_BUDGET
        quality_states = {item.quality for item in iterations}
        if len(quality_states) >= campaign.budget.state_budget:
            return DesignCampaignTermination.STATE_BUDGET
        if (
            sum(item.quality.flow_nodes for item in iterations) + next_node_cost
            > campaign.budget.maximum_flow_nodes
        ):
            return DesignCampaignTermination.COST_BUDGET
        return None

    def _derived_attempt(
        self,
        campaign: DesignCampaign,
        state: DesignCampaignState,
        proposal: DesignRepairProposal,
        repair_plan: DesignRepairPlan,
    ) -> DesignCampaignAttempt:
        continuation = campaign.continuation
        assert continuation is not None
        payload = DesignCampaignIterationInput(
            state.campaign_identity,
            len(state.iterations) + 1,
            state.iterations[-1].candidate.identity,
            state.attribution.canonical_json(),
            proposal.canonical_json(),
            repair_plan.canonical_json(),
        )
        nodes = tuple(
            replace(
                node,
                extensions={
                    **node.extensions,
                    DESIGN_CAMPAIGN_ITERATION_EXTENSION: json.loads(
                        canonical_json(payload)
                    ),
                },
            )
            if node.node_id == continuation.proposal_node
            else node
            for node in continuation.plan.spec.nodes
        )
        spec = replace(continuation.plan.spec, nodes=nodes)
        plan = self._engine.plan(
            spec,
            continuation.plan.target.target_id,
            continuation.plan.profile,
        )
        return DesignCampaignAttempt(
            f"iteration-{len(state.iterations) + 1}",
            plan,
            continuation.candidate,
            continuation.artifacts,
            continuation.stages,
            repair_plan,
        )

    def start(
        self,
        campaign: DesignCampaign,
        *,
        elapsed_seconds: int = 0,
    ) -> DesignCampaignState:
        """Execute only the baseline and stop when semantic input is required."""

        self._validate_bindings(campaign)
        attempt = campaign.baseline
        budget_stop = self._budget_stop(
            campaign,
            (),
            next_node_cost=len(attempt.plan.nodes),
            elapsed_seconds=elapsed_seconds,
        )
        if budget_stop is not None:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                self.campaign_identity(campaign),
                DesignCampaignPhase.COMPLETED,
                budget_stop,
                (),
                None,
                None,
                None,
                f"Design Campaign stopped before baseline with {budget_stop.value}",
            )
        try:
            observed = self._execute_attempt(campaign, attempt, ())
        except RuntimeError as exc:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                self.campaign_identity(campaign),
                DesignCampaignPhase.COMPLETED,
                DesignCampaignTermination.EXECUTION_FAILED,
                (),
                None,
                None,
                None,
                f"Design Campaign execution failed closed: {exc}",
            )
        except (OSError, ValueError) as exc:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                self.campaign_identity(campaign),
                DesignCampaignPhase.COMPLETED,
                DesignCampaignTermination.INVALID_IDENTITY,
                (),
                None,
                None,
                None,
                f"Design Campaign identity failed closed: {exc}",
            )
        termination = self._termination(observed)
        if (
            termination is DesignCampaignTermination.FAILED
            and campaign.continuation is not None
        ):
            try:
                return self._proposal_required_state(
                    campaign,
                    (observed,),
                    elapsed_seconds=elapsed_seconds,
                )
            except (OSError, ValueError) as exc:
                return DesignCampaignState(
                    campaign.owner,
                    campaign.campaign_id,
                    self.campaign_identity(campaign),
                    DesignCampaignPhase.COMPLETED,
                    DesignCampaignTermination.INVALID_IDENTITY,
                    (observed,),
                    None,
                    None,
                    None,
                    f"Design Campaign attribution failed closed: {exc}",
                )
        return DesignCampaignState(
            campaign.owner,
            campaign.campaign_id,
            self.campaign_identity(campaign),
            DesignCampaignPhase.COMPLETED,
            termination,
            (observed,),
            None,
            None,
            None,
            f"Design Campaign stopped with {termination.value}",
        )

    def resume(
        self,
        campaign: DesignCampaign,
        state: DesignCampaignState,
        proposal: DesignRepairProposal,
        *,
        elapsed_seconds: int = 0,
    ) -> DesignCampaignState:
        """Compile one proposal, derive one child attempt, and stop or ask again."""

        campaign_identity = self.campaign_identity(campaign)
        if (
            state.campaign_identity != campaign_identity
            or state.owner != campaign.owner
            or state.campaign_id != campaign.campaign_id
        ):
            raise ValueError("Design Campaign resume identity drift")
        if state.phase is not DesignCampaignPhase.PROPOSAL_REQUIRED or state.attribution is None:
            raise ValueError("Design Campaign is not awaiting a proposal")
        continuation = campaign.continuation
        if continuation is None:
            raise ValueError("Design Campaign has no continuation template")
        budget_stop = self._budget_stop(
            campaign,
            state.iterations,
            next_node_cost=len(continuation.plan.nodes),
            elapsed_seconds=elapsed_seconds,
        )
        if budget_stop is not None:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                campaign_identity,
                DesignCampaignPhase.COMPLETED,
                budget_stop,
                state.iterations,
                None,
                state.repair_plan,
                proposal.identity,
                f"Design Campaign stopped with {budget_stop.value}",
            )
        previous_attempt = (
            campaign.baseline
            if len(state.iterations) == 1
            else DesignCampaignAttempt(
                state.iterations[-1].provenance.iteration_id,
                continuation.plan,
                continuation.candidate,
                continuation.artifacts,
                continuation.stages,
                state.repair_plan,
            )
        )
        values = self._reload_iteration_values(previous_attempt, state.iterations[-1])
        evidence, parent, parent_result = self._failure_inputs(
            state.iterations[-1].candidate,
            values,
        )
        repair_plan = compile_design_repair(
            campaign_identity=campaign_identity,
            attribution=state.attribution,
            proposal=proposal,
            candidate=state.iterations[-1].candidate,
            parent=parent,
            parent_result=parent_result,
            evidence=evidence,
            policy=continuation.repair_policy,
        )
        if repair_plan.decision is not RepairCompileDecision.ACCEPTED:
            raise ValueError(f"Design Repair Proposal rejected: {repair_plan.reason}")
        attempt = self._derived_attempt(campaign, state, proposal, repair_plan)
        try:
            observed = self._execute_attempt(
                campaign,
                attempt,
                tuple(item.candidate for item in state.iterations),
            )
        except RuntimeError as exc:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                campaign_identity,
                DesignCampaignPhase.COMPLETED,
                DesignCampaignTermination.EXECUTION_FAILED,
                state.iterations,
                None,
                repair_plan,
                proposal.identity,
                f"Design Campaign continuation execution failed closed: {exc}",
            )
        except (OSError, ValueError) as exc:
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                campaign_identity,
                DesignCampaignPhase.COMPLETED,
                DesignCampaignTermination.INVALID_IDENTITY,
                state.iterations,
                None,
                repair_plan,
                proposal.identity,
                f"Design Campaign continuation identity failed closed: {exc}",
            )
        if not self._repair_parent_matches(
            state.iterations[-1], repair_plan
        ) or not self._repair_child_matches(
            state.iterations[-1], observed, repair_plan
        ):
            return DesignCampaignState(
                campaign.owner,
                campaign.campaign_id,
                campaign_identity,
                DesignCampaignPhase.COMPLETED,
                DesignCampaignTermination.INVALID_IDENTITY,
                (*state.iterations, observed),
                None,
                repair_plan,
                proposal.identity,
                "Design Campaign child Candidate lineage drift",
            )
        iterations = (*state.iterations, observed)
        termination = self._termination(observed)
        if termination is DesignCampaignTermination.FAILED:
            try:
                return self._proposal_required_state(
                    campaign,
                    iterations,
                    repair_plan=repair_plan,
                    last_proposal_identity=proposal.identity,
                    elapsed_seconds=elapsed_seconds,
                )
            except (OSError, ValueError) as exc:
                return DesignCampaignState(
                    campaign.owner,
                    campaign.campaign_id,
                    campaign_identity,
                    DesignCampaignPhase.COMPLETED,
                    DesignCampaignTermination.INVALID_IDENTITY,
                    iterations,
                    None,
                    repair_plan,
                    proposal.identity,
                    f"Design Campaign attribution failed closed: {exc}",
                )
        return DesignCampaignState(
            campaign.owner,
            campaign.campaign_id,
            campaign_identity,
            DesignCampaignPhase.COMPLETED,
            termination,
            iterations,
            None,
            repair_plan,
            proposal.identity,
            f"Design Campaign stopped with {termination.value}",
        )


@dataclass(frozen=True)
class ProjectDesignCampaignPlan:
    """One semantic Campaign compiled against one exact Project."""

    campaign: DesignCampaign
    _engine: FlowEngine = field(repr=False, compare=False)
    _artifact_root: Path = field(repr=False, compare=False)

    @property
    def identity(self) -> str:
        return self.runner().campaign_identity(self.campaign)

    @property
    def record(self) -> dict[str, object]:
        return self.runner().plan_record(self.campaign)

    def runner(
        self,
        *,
        environment: ExecutionEnvironment | None = None,
        execution_context_identity: str = "default-execution-context",
    ) -> DesignCampaignRunner:
        return DesignCampaignRunner(
            self._engine,
            artifact_root=self._artifact_root,
            environment=environment,
            execution_context_identity=execution_context_identity,
        )


def resolve_project_design_campaign(
    project: Project,
    campaign_json: str,
) -> ProjectDesignCampaignPlan:
    """Compile portable Campaign selectors through project-owned Flow catalogs."""

    source = design_campaign_spec_from_json(campaign_json)
    project_flow = ProjectFlow(project, source.owner)
    source_baseline = source.baseline
    baseline_plan = project_flow.plan(
        flow=source_baseline.flow,
        target=source_baseline.target,
        profile=source_baseline.profile,
    )
    baseline = DesignCampaignAttempt(
        source_baseline.iteration_id,
        baseline_plan.plan,
        source_baseline.candidate,
        source_baseline.artifacts,
        source_baseline.stages,
        None,
    )
    continuation = None
    if source.continuation is not None:
        template = source.continuation
        continuation_plan = project_flow.plan(
            flow=template.flow,
            target=template.target,
            profile=template.profile,
        )
        continuation = DesignCampaignContinuation(
            continuation_plan.plan,
            template.candidate,
            template.artifacts,
            template.stages,
            template.proposal_node,
            template.repair_policy,
        )
    campaign = DesignCampaign(
        source.owner,
        source.campaign_id,
        baseline,
        source.budget,
        source.scope,
        continuation,
    )
    planned = ProjectDesignCampaignPlan(
        campaign,
        baseline_plan.engine,
        project.artifact_root,
    )
    _ = planned.record
    return planned


__all__ = [
    "DesignArtifactBinding",
    "DesignCampaign",
    "DesignCampaignAttempt",
    "DesignCampaignAttemptSpec",
    "DesignCampaignBudget",
    "DesignCampaignContinuation",
    "DesignCampaignContinuationSpec",
    "DesignCampaignPhase",
    "DesignCampaignResult",
    "DesignCampaignRunner",
    "DesignCampaignScope",
    "DesignCampaignSpec",
    "DesignCampaignState",
    "DesignCampaignTermination",
    "DesignQuality",
    "DesignStage",
    "DesignStageAssessment",
    "DesignStageBinding",
    "DesignStageStatus",
    "ProjectDesignCampaignPlan",
    "design_campaign_result_from_json",
    "design_campaign_spec_from_json",
    "design_campaign_state_from_json",
    "resolve_project_design_campaign",
]
