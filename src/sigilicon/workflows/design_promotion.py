"""Compile evidence-bound, non-writing Design Candidate Promotion Plans."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.canonical import canonical_from_exact_json, canonical_json
from sigilicon.domain.circuit_design import (
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    ArtifactReference,
    CanonicalDesignArtifact,
    DesignCandidate,
    DesignDecision,
    DesignDecisionConclusion,
    DesignEvidence,
    EvidenceLevel,
    EvidenceRole,
    validate_design_candidate,
    validate_design_decision,
)
from sigilicon.flow.model import identifier, owner_identity


@dataclass(frozen=True)
class PromotionChange:
    """One semantic source role; deliberately not a repository path or patch."""

    source_role: str
    candidate_artifact: ArtifactReference
    summary: str

    def __post_init__(self) -> None:
        identifier(self.source_role, "Promotion Plan source role")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("Promotion Plan change summary must be non-empty")


@dataclass(frozen=True)
class PromotionRequirements:
    owner: str
    policy: ArtifactReference
    role: EvidenceRole
    level: EvidenceLevel
    scope: tuple[str, ...]
    required_regressions: tuple[str, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Promotion requirements owner")
        if self.policy.owner != self.owner:
            raise ValueError("Promotion requirements policy owner drift")
        if not isinstance(self.role, EvidenceRole) or not isinstance(
            self.level, EvidenceLevel
        ):
            raise ValueError("Promotion requirements role and level must be typed")
        if not self.scope or self.scope != tuple(sorted(set(self.scope))):
            raise ValueError("Promotion requirements scope must be non-empty and sorted")
        for value in self.scope:
            identifier(value, "Promotion requirements scope")
        if (
            not self.required_regressions
            or self.required_regressions
            != tuple(sorted(set(self.required_regressions)))
        ):
            raise ValueError("Promotion requirements regressions must be non-empty and sorted")
        for value in self.required_regressions:
            identifier(value, "Promotion requirements regression")


@dataclass(frozen=True)
class PromotionRequest:
    owner: str
    candidate_identity: str
    decision_identity: str
    requirements: PromotionRequirements
    changes: tuple[PromotionChange, ...]
    unresolved_risks: tuple[str, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Promotion request owner")
        for value, label in (
            (self.candidate_identity, "Promotion request Candidate"),
            (self.decision_identity, "Promotion request Decision"),
        ):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError(f"invalid {label}: {value!r}")
        if self.requirements.owner != self.owner:
            raise ValueError("Promotion request requirements owner drift")
        if not self.changes:
            raise ValueError("Promotion request needs at least one semantic change")
        roles = tuple(item.source_role for item in self.changes)
        if roles != tuple(sorted(set(roles))):
            raise ValueError("Promotion request source roles must be unique and sorted")
        for risk in self.unresolved_risks:
            if not isinstance(risk, str) or not risk.strip() or "\x00" in risk:
                raise ValueError("Promotion request risks must be non-empty text")
        if len(self.unresolved_risks) != len(set(self.unresolved_risks)):
            raise ValueError("Promotion request risks must be unique")

    def canonical_json(self) -> str:
        return canonical_json(self)


def promotion_request_from_json(text: str) -> PromotionRequest:
    return canonical_from_exact_json(text, PromotionRequest)


@dataclass(frozen=True)
class PromotionPlan:
    owner: str
    candidate: ArtifactReference
    decision_identity: str
    policy: ArtifactReference
    evidence: tuple[ArtifactReference, ...]
    changes: tuple[PromotionChange, ...]
    required_regressions: tuple[str, ...]
    unresolved_risks: tuple[str, ...]
    human_approval_required: bool
    writes_canonical_source: bool
    reason: str

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Promotion Plan owner")
        if (
            self.candidate.owner != self.owner
            or self.candidate.kind != DESIGN_CANDIDATE_KIND
        ):
            raise ValueError("Promotion Plan Candidate owner drift")
        if not isinstance(self.decision_identity, str) or not self.decision_identity:
            raise ValueError("Promotion Plan Decision identity must be non-empty")
        if self.policy.owner != self.owner:
            raise ValueError("Promotion Plan policy owner drift")
        if not self.evidence:
            raise ValueError("Promotion Plan needs accepted evidence")
        evidence_identities = tuple(item.identity for item in self.evidence)
        if len(evidence_identities) != len(set(evidence_identities)):
            raise ValueError("Promotion Plan evidence must be unique")
        if any(
            item.owner != self.owner or item.kind != DESIGN_EVIDENCE_KIND
            for item in self.evidence
        ):
            raise ValueError("Promotion Plan evidence owner or kind drift")
        if not self.changes:
            raise ValueError("Promotion Plan needs at least one semantic change")
        roles = tuple(item.source_role for item in self.changes)
        if roles != tuple(sorted(set(roles))):
            raise ValueError("Promotion Plan source roles must be unique and sorted")
        if any(item.candidate_artifact.owner != self.owner for item in self.changes):
            raise ValueError("Promotion Plan change owner drift")
        if (
            not self.required_regressions
            or self.required_regressions
            != tuple(sorted(set(self.required_regressions)))
        ):
            raise ValueError("Promotion Plan regressions must be non-empty and sorted")
        for value in self.required_regressions:
            identifier(value, "Promotion Plan regression")
        if len(self.unresolved_risks) != len(set(self.unresolved_risks)) or any(
            not isinstance(risk, str) or not risk.strip() or "\x00" in risk
            for risk in self.unresolved_risks
        ):
            raise ValueError("Promotion Plan risks must be unique non-empty text")
        if self.human_approval_required is not True:
            raise ValueError("Promotion Plan must require explicit human approval")
        if self.writes_canonical_source is not False:
            raise ValueError("Promotion Plan cannot write canonical source")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Promotion Plan needs a reason")

    @property
    def identity(self) -> str:
        return f"{self.owner}:promotion-plan:{self.candidate.identity}"

    def canonical_json(self) -> str:
        return canonical_json(self)


def promotion_plan_from_json(text: str) -> PromotionPlan:
    return canonical_from_exact_json(text, PromotionPlan)


def compile_promotion_plan(
    *,
    candidate: DesignCandidate,
    artifacts: tuple[CanonicalDesignArtifact, ...],
    decision: DesignDecision,
    request: PromotionRequest,
) -> PromotionPlan:
    """Validate review readiness without touching a repository or selecting a path."""

    if (
        request.owner != candidate.metadata.owner
        or request.candidate_identity != candidate.identity
        or request.decision_identity != decision.identity
    ):
        raise ValueError("Promotion request identity drift")
    validate_design_candidate(candidate, artifacts)
    evidence_by_identity = {
        item.identity: item for item in artifacts if isinstance(item, DesignEvidence)
    }
    evidence = tuple(
        evidence_by_identity[item.identity]
        for item in decision.evidence
        if item.identity in evidence_by_identity
    )
    validate_design_decision(decision, candidate, evidence)
    requirements = request.requirements
    if (
        decision.conclusion is not DesignDecisionConclusion.PASSED
        or decision.policy != requirements.policy
        or decision.role is not requirements.role
        or decision.level is not requirements.level
        or not set(requirements.scope).issubset(decision.scope)
        or len(evidence) != len(decision.evidence)
    ):
        raise ValueError("Promotion Plan has insufficient accepted evidence")
    candidate_references = {
        (reference.owner, reference.kind, reference.identity)
        for reference in (
            candidate.topology,
            candidate.sizing_problem,
            candidate.sizing_result,
        )
        if reference is not None
    }
    if any(
        change.candidate_artifact.owner != candidate.metadata.owner
        or (
            change.candidate_artifact.owner,
            change.candidate_artifact.kind,
            change.candidate_artifact.identity,
        )
        not in candidate_references
        for change in request.changes
    ):
        raise ValueError("Promotion change is outside the validated Candidate stages")
    return PromotionPlan(
        candidate.metadata.owner,
        candidate.reference(),
        decision.identity,
        requirements.policy,
        decision.evidence,
        request.changes,
        requirements.required_regressions,
        request.unresolved_risks,
        True,
        False,
        "evidence is accepted; ordinary Git review and explicit human approval remain required",
    )


__all__ = [
    "PromotionChange",
    "PromotionPlan",
    "PromotionRequest",
    "PromotionRequirements",
    "compile_promotion_plan",
    "promotion_plan_from_json",
    "promotion_request_from_json",
]
