"""Bounded whole-design attempts above the single-attempt deterministic FlowEngine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
    canonical_sha256,
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
from sigilicon.flow import ExecutionEnvironment, FlowEngine, FlowPlan
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.workflows.design_repair import SizingRepairPlan, TopologyRepairPlan


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


def _sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a SHA-256 identity")


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
    """Portable selectors and output bindings for one cataloged Flow attempt."""

    iteration_id: str
    flow: str
    target: str
    profile: str | None
    candidate: DesignArtifactBinding
    artifacts: tuple[DesignArtifactBinding, ...]
    stages: tuple[DesignStageBinding, ...]
    repair_plan: DesignRepairPlan | None

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "Design Campaign iteration")
        identifier(self.flow, "Design Campaign Flow")
        identifier(self.target, "Design Campaign Flow target")
        if self.profile is not None:
            identifier(self.profile, "Design Campaign Execution Profile")


@dataclass(frozen=True)
class DesignCampaignSpec:
    """Strict client-neutral Campaign input with no paths or backend commands."""

    owner: str
    campaign_id: str
    attempts: tuple[DesignCampaignAttemptSpec, ...]
    budget: DesignCampaignBudget
    scope: DesignCampaignScope

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign owner")
        identifier(self.campaign_id, "Design Campaign identity")
        if not self.attempts:
            raise ValueError("Design Campaign specification needs at least one attempt")
        identities = tuple(item.iteration_id for item in self.attempts)
        if len(identities) != len(set(identities)):
            raise ValueError("Design Campaign specification iterations must be unique")
        if self.scope.decision_policy.owner != self.owner:
            raise ValueError("Design Campaign specification policy owner drift")
        if self.attempts[0].repair_plan is not None:
            raise ValueError("Design Campaign baseline cannot have a Repair Plan")
        if any(item.repair_plan is None for item in self.attempts[1:]):
            raise ValueError("Design Campaign continuation requires a Repair Plan")

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


@dataclass(frozen=True)
class DesignCampaignBudget:
    state_budget: int
    iteration_budget: int
    maximum_flow_nodes: int

    def __post_init__(self) -> None:
        if type(self.state_budget) is not int or self.state_budget <= 0:
            raise ValueError("Design Campaign state budget must be positive")
        if type(self.iteration_budget) is not int or self.iteration_budget <= 0:
            raise ValueError("Design Campaign iteration budget must be positive")
        if type(self.maximum_flow_nodes) is not int or self.maximum_flow_nodes < 0:
            raise ValueError("Design Campaign Flow-node cost budget must be non-negative")


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
    attempts: tuple[DesignCampaignAttempt, ...]
    budget: DesignCampaignBudget
    scope: DesignCampaignScope

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign owner")
        identifier(self.campaign_id, "Design Campaign identity")
        if not self.attempts:
            raise ValueError("Design Campaign needs at least one attempt")
        identities = tuple(item.iteration_id for item in self.attempts)
        if len(identities) != len(set(identities)):
            raise ValueError("Design Campaign iteration identities must be unique")
        if any(item.plan.spec.owner != self.owner for item in self.attempts):
            raise ValueError("Design Campaign attempt owner drift")
        if self.scope.decision_policy.owner != self.owner:
            raise ValueError("Design Campaign decision policy owner drift")
        if self.attempts[0].repair_plan is not None:
            raise ValueError("Design Campaign baseline attempt cannot have a Repair Plan")
        if any(item.repair_plan is None for item in self.attempts[1:]):
            raise ValueError("Design Campaign continuation attempts require Repair Plans")


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
    sha256: str
    producer: str
    role: str

    def __post_init__(self) -> None:
        identifier(self.label, "Design Campaign artifact identity label")
        identifier(self.kind, "Design Campaign artifact kind")
        _sha256(self.sha256, "Design Campaign artifact")
        identifier(self.producer, "Design Campaign artifact producer")
        identifier(self.role, "Design Campaign artifact role")


@dataclass(frozen=True)
class DesignAttemptProvenance:
    iteration_id: str
    run_id: str
    plan_sha256: str
    flow_id: str
    target: str
    execution_profile: str
    artifacts: tuple[DesignCampaignArtifactIdentity, ...]

    def __post_init__(self) -> None:
        identifier(self.iteration_id, "Design Campaign provenance iteration")
        run_identity(self.run_id)
        _sha256(self.plan_sha256, "Design Campaign Flow Plan")
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
    campaign_sha256: str
    termination: DesignCampaignTermination
    iterations: tuple[DesignCampaignIterationResult, ...]
    final_quality: DesignQuality | None
    final_decision: DesignDecision | None
    message: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Design Campaign result owner")
        identifier(self.campaign_id, "Design Campaign result identity")
        _sha256(self.campaign_sha256, "Design Campaign")
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
    ) -> None:
        root = Path(artifact_root).resolve()
        if root == Path(root.anchor):
            raise ValueError("Design Campaign artifact root cannot be a filesystem root")
        self._engine = engine
        self._artifact_root = root
        self._environment = environment

    def plan_record(self, campaign: DesignCampaign) -> dict[str, object]:
        """Return the canonical portable record whose hash authorizes execution."""

        self._validate_bindings(campaign)
        value = {
            "owner": campaign.owner,
            "campaign_id": campaign.campaign_id,
            "budget": campaign.budget,
            "scope": campaign.scope,
            "attempts": [
                {
                    "iteration_id": attempt.iteration_id,
                    "plan": self._engine.plan_record(attempt.plan),
                    "candidate": attempt.candidate,
                    "artifacts": attempt.artifacts,
                    "stages": attempt.stages,
                    "repair_plan": attempt.repair_plan,
                }
                for attempt in campaign.attempts
            ],
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
        for attempt in campaign.attempts:
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

    def campaign_identity(self, campaign: DesignCampaign) -> str:
        return canonical_sha256(self.plan_record(campaign))

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
        plan_sha256: str,
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
            ):
                raise ValueError(
                    "Design Campaign sizing repair proposal is not bound by the child Candidate"
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
            ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_DECISION_KIND, campaign.owner),
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
            )
            for label, artifact in sorted(produced.items())
            for parsed in (
                candidate_value if label == attempt.candidate.label else artifacts[label],
            )
        )
        provenance = DesignAttemptProvenance(
            attempt.iteration_id,
            flow_result.run_id,
            plan_sha256,
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
        campaign_sha256: str,
        termination: DesignCampaignTermination,
        message: str,
    ) -> DesignCampaignResult:
        return DesignCampaignResult(
            campaign.owner,
            campaign.campaign_id,
            campaign_sha256,
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
        if repair.parent_candidate_sha256 != previous.candidate.identity:
            return False
        if not repair.evidence_sha256 or not set(repair.evidence_sha256).issubset(
            reference.sha256 for reference in previous.candidate.evidence
        ):
            return False
        if isinstance(repair, TopologyRepairPlan):
            return repair.parent_topology_sha256 == previous.candidate.topology.sha256
        return (
            previous.candidate.sizing_problem is not None
            and previous.candidate.sizing_result is not None
            and repair.parent_problem_sha256
            == previous.candidate.sizing_problem.sha256
            and repair.parent_result_sha256
            == previous.candidate.sizing_result.sha256
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
        return True

    def run(self, campaign: DesignCampaign) -> DesignCampaignResult:
        campaign_sha256 = self.campaign_identity(campaign)
        iterations: list[DesignCampaignIterationResult] = []
        consumed_nodes = 0
        for index, attempt in enumerate(campaign.attempts):
            if index >= campaign.budget.iteration_budget:
                termination = DesignCampaignTermination.ITERATION_BUDGET
                break
            if index >= campaign.budget.state_budget:
                termination = DesignCampaignTermination.STATE_BUDGET
                break
            node_cost = len(attempt.plan.nodes)
            if consumed_nodes + node_cost > campaign.budget.maximum_flow_nodes:
                termination = DesignCampaignTermination.COST_BUDGET
                break
            if index:
                assert iterations
                assert attempt.repair_plan is not None
                if not self._repair_parent_matches(
                    iterations[-1], attempt.repair_plan
                ):
                    termination = DesignCampaignTermination.INVALID_IDENTITY
                    break
            consumed_nodes += node_cost
            plan_sha256 = canonical_sha256(self._engine.plan_record(attempt.plan))
            run_id = hashlib.sha256(
                f"{campaign_sha256}:{attempt.iteration_id}:{plan_sha256}".encode("utf-8")
            ).hexdigest()[:32]
            try:
                flow_result = self._engine.run(
                    attempt.plan,
                    artifact_root=self._artifact_root,
                    environment=self._environment,
                    run_id=run_id,
                )
            except (OSError, RuntimeError) as exc:
                if not iterations:
                    return self._empty_result(
                        campaign,
                        campaign_sha256,
                        DesignCampaignTermination.EXECUTION_FAILED,
                        f"Design Campaign execution failed closed: {exc}",
                    )
                termination = DesignCampaignTermination.EXECUTION_FAILED
                break
            if flow_result.status != "accepted":
                if not iterations:
                    return self._empty_result(
                        campaign,
                        campaign_sha256,
                        DesignCampaignTermination.EXECUTION_FAILED,
                        "Design Campaign Flow attempt did not reach accepted state",
                    )
                termination = DesignCampaignTermination.EXECUTION_FAILED
                break
            try:
                observed = self._observe(
                    campaign,
                    attempt,
                    flow_result,
                    plan_sha256,
                    tuple(item.candidate for item in iterations),
                )
            except ValueError as exc:
                if not iterations:
                    return self._empty_result(
                        campaign,
                        campaign_sha256,
                        DesignCampaignTermination.INVALID_IDENTITY,
                        f"Design Campaign artifact identity failed closed: {exc}",
                    )
                termination = DesignCampaignTermination.INVALID_IDENTITY
                break
            if index:
                assert attempt.repair_plan is not None
                if not self._repair_child_matches(
                    iterations[-1], observed, attempt.repair_plan
                ):
                    termination = DesignCampaignTermination.INVALID_IDENTITY
                    break
            iterations.append(observed)
            termination = self._termination(observed)
            if termination is DesignCampaignTermination.PASSED:
                break
            if termination is DesignCampaignTermination.FAILED:
                if index + 1 < len(campaign.attempts):
                    next_attempt = campaign.attempts[index + 1]
                    if next_attempt.repair_plan is not None:
                        continue
                break
            break
        else:
            termination = self._termination(iterations[-1])
        if not iterations:
            return self._empty_result(
                campaign,
                campaign_sha256,
                termination,
                f"Design Campaign stopped before execution: {termination.value}",
            )
        return DesignCampaignResult(
            campaign.owner,
            campaign.campaign_id,
            campaign_sha256,
            termination,
            tuple(iterations),
            iterations[-1].quality,
            iterations[-1].decision,
            f"Design Campaign stopped with {termination.value}",
        )


__all__ = [
    "DesignArtifactBinding",
    "DesignCampaign",
    "DesignCampaignAttempt",
    "DesignCampaignAttemptSpec",
    "DesignCampaignBudget",
    "DesignCampaignResult",
    "DesignCampaignRunner",
    "DesignCampaignScope",
    "DesignCampaignSpec",
    "DesignCampaignTermination",
    "DesignQuality",
    "DesignStage",
    "DesignStageAssessment",
    "DesignStageBinding",
    "DesignStageStatus",
    "design_campaign_result_from_json",
    "design_campaign_spec_from_json",
]
