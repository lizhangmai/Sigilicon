"""Compile bounded topology and sizing proposals into non-mutating Repair Plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
)
from sigilicon.domain.circuit_design import (
    ArtifactReference,
    CircuitSizingProblem,
    CircuitSizingResult,
    CircuitTopologyProposal,
    DesignCandidate,
    DesignEvidence,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    EvidenceConclusion,
    ProposalProvenance,
    SizingCandidate,
    TopologyOrigin,
)
from sigilicon.flow.model import identifier, owner_identity
from sigilicon.identifiers import bounded_identity


_CIRCUIT_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$]*(?:[._-][A-Za-z0-9_$]+)*\Z"
)

class RepairCompileDecision(str, Enum):
    ACCEPTED = "accepted"
    INVALID_IDENTITY = "invalid_identity"
    UNSUPPORTED = "unsupported"
    ILLEGAL_CHANGE = "illegal_change"
    BUDGET_EXHAUSTED = "budget_exhausted"


class DesignRepairKind(str, Enum):
    TOPOLOGY = "topology"
    SIZING = "sizing"


@dataclass(frozen=True)
class DesignRepairAttribution:
    """Typed violated evidence projected into one supported repair scope."""

    attribution_id: str
    owner: str
    candidate: ArtifactReference
    subject: ArtifactReference
    evidence: tuple[ArtifactReference, ...]
    repair_kind: DesignRepairKind
    finding_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        bounded_identity(self.attribution_id, "design repair attribution")
        owner_identity(self.owner, "design repair attribution owner")
        if self.candidate.owner != self.owner or self.candidate.kind != DESIGN_CANDIDATE_KIND:
            raise ValueError("design repair attribution Candidate identity drift")
        if self.subject.owner != self.owner:
            raise ValueError("design repair attribution subject owner drift")
        if not self.evidence or any(
            item.owner != self.owner or item.kind != DESIGN_EVIDENCE_KIND
            for item in self.evidence
        ):
            raise ValueError("design repair attribution needs owner-bound Design Evidence")
        evidence_ids = tuple(item.identity for item in self.evidence)
        if evidence_ids != tuple(sorted(set(evidence_ids))):
            raise ValueError("design repair attribution evidence must be unique and sorted")
        if not isinstance(self.repair_kind, DesignRepairKind):
            raise ValueError("design repair attribution kind must be typed")
        _identities(self.finding_codes, "design repair attribution findings")
        if not self.finding_codes:
            raise ValueError("design repair attribution needs typed findings")

    @property
    def identity(self) -> str:
        return self.attribution_id

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class DesignRepairProposal:
    """A semantic client proposal with no authority to claim verification."""

    proposal_id: str
    owner: str
    campaign_identity: str
    parent_candidate_identity: str
    attribution_identity: str
    evidence_identity: tuple[str, ...]
    proposed_topology: CircuitTopologyProposal | None
    proposed_sizing: SizingCandidate | None
    requested_evaluations: int | None
    required_regressions: tuple[str, ...]
    provenance: ProposalProvenance

    def __post_init__(self) -> None:
        bounded_identity(self.proposal_id, "design repair proposal")
        owner_identity(self.owner, "design repair proposal owner")
        for value, label in (
            (self.campaign_identity, "design Campaign"),
            (self.parent_candidate_identity, "proposal parent Candidate"),
            (self.attribution_identity, "proposal attribution"),
        ):
            bounded_identity(value, label)
        if self.evidence_identity != tuple(sorted(set(self.evidence_identity))):
            raise ValueError("design repair proposal evidence must be unique and sorted")
        for value in self.evidence_identity:
            bounded_identity(value, "design repair proposal evidence")
        _identities(self.required_regressions, "design repair proposal regressions")
        topology = self.proposed_topology is not None
        sizing = self.proposed_sizing is not None
        if topology == sizing:
            raise ValueError("design repair proposal must contain exactly one change kind")
        if topology:
            if self.proposed_topology.metadata.owner != self.owner:
                raise ValueError("design repair topology proposal owner drift")
            if self.requested_evaluations is not None:
                raise ValueError("topology proposal cannot request sizing evaluations")
        elif (
            type(self.requested_evaluations) is not int
            or self.requested_evaluations <= 0
        ):
            raise ValueError("sizing proposal needs a positive evaluation request")

    @property
    def repair_kind(self) -> DesignRepairKind:
        return (
            DesignRepairKind.TOPOLOGY
            if self.proposed_topology is not None
            else DesignRepairKind.SIZING
        )

    @property
    def identity(self) -> str:
        return self.proposal_id

    def canonical_json(self) -> str:
        return canonical_json(self)


def design_repair_proposal_from_json(text: str) -> DesignRepairProposal:
    return canonical_from_exact_json(text, DesignRepairProposal)


def _identities(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and sorted")
    for value in values:
        identifier(value, label)


def _circuit_identities(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and sorted")
    if any(_CIRCUIT_IDENTIFIER.fullmatch(value) is None for value in values):
        raise ValueError(f"{label} contain an invalid circuit identifier")


@dataclass(frozen=True)
class TopologyRepairPolicy:
    owner: str
    policy_id: str
    allowed_masters: tuple[str, ...]
    maximum_instance_changes: int
    required_regressions: tuple[str, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "topology repair policy owner")
        identifier(self.policy_id, "topology repair policy")
        if not self.allowed_masters:
            raise ValueError("topology repair policy must allow at least one master")
        _circuit_identities(self.allowed_masters, "allowed topology masters")
        if type(self.maximum_instance_changes) is not int or self.maximum_instance_changes <= 0:
            raise ValueError("topology repair change budget must be positive")
        _identities(self.required_regressions, "topology repair regressions")


@dataclass(frozen=True)
class SizingRepairPolicy:
    owner: str
    policy_id: str
    allowed_parameters: tuple[str, ...]
    maximum_evaluations: int
    required_regressions: tuple[str, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "sizing repair policy owner")
        identifier(self.policy_id, "sizing repair policy")
        if not self.allowed_parameters:
            raise ValueError("sizing repair policy must allow at least one parameter")
        _circuit_identities(self.allowed_parameters, "allowed sizing parameters")
        if type(self.maximum_evaluations) is not int or self.maximum_evaluations <= 0:
            raise ValueError("sizing repair evaluation budget must be positive")
        _identities(self.required_regressions, "sizing repair regressions")


@dataclass(frozen=True)
class TopologyRepairPlan:
    plan_id: str
    decision: RepairCompileDecision
    owner: str
    policy_id: str
    parent_candidate_identity: str
    parent_topology_identity: str
    evidence_identity: tuple[str, ...]
    policy_identity: str
    proposal_identity: str
    required_regressions: tuple[str, ...]
    changed_instances: tuple[str, ...]
    proposed_topology: CircuitTopologyProposal | None
    reason: str

    def __post_init__(self) -> None:
        bounded_identity(self.plan_id, "topology Repair Plan")
        if not isinstance(self.decision, RepairCompileDecision):
            raise ValueError("topology repair decision must be typed")
        owner_identity(self.owner, "topology Repair Plan owner")
        identifier(self.policy_id, "topology Repair Plan policy")
        for value, label in (
            (self.parent_candidate_identity, "parent Candidate"),
            (self.parent_topology_identity, "parent topology"),
            (self.policy_identity, "repair policy"),
            (self.proposal_identity, "topology proposal"),
        ):
            bounded_identity(value, label)
        if self.evidence_identity != tuple(sorted(set(self.evidence_identity))):
            raise ValueError("topology repair evidence must be unique and sorted")
        for value in self.evidence_identity:
            bounded_identity(value, "topology repair evidence")
        _identities(self.required_regressions, "topology Repair Plan regressions")
        if self.changed_instances != tuple(sorted(set(self.changed_instances))):
            raise ValueError("changed topology instances must be unique and sorted")
        _circuit_identities(self.changed_instances, "changed topology instances")
        if self.decision is RepairCompileDecision.ACCEPTED:
            if (
                self.proposed_topology is None
                or not self.changed_instances
                or not self.evidence_identity
            ):
                raise ValueError(
                    "accepted topology Repair Plan needs evidence and a concrete change"
                )
            if self.proposed_topology.metadata.owner != self.owner:
                raise ValueError("topology Repair Plan proposal owner drift")
        elif self.proposed_topology is not None or self.changed_instances:
            raise ValueError("rejected topology Repair Plan cannot carry an action")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("topology Repair Plan needs a reason")

    @property
    def accepted(self) -> bool:
        return self.decision is RepairCompileDecision.ACCEPTED

    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def identity(self) -> str:
        return self.plan_id


@dataclass(frozen=True)
class SizingRepairPlan:
    plan_id: str
    decision: RepairCompileDecision
    owner: str
    policy_id: str
    parent_candidate_identity: str
    parent_problem_identity: str
    parent_result_identity: str
    evidence_identity: tuple[str, ...]
    policy_identity: str
    proposal_identity: str
    requested_evaluations: int
    required_regressions: tuple[str, ...]
    changed_parameters: tuple[str, ...]
    proposed_candidate: SizingCandidate | None
    reason: str

    def __post_init__(self) -> None:
        bounded_identity(self.plan_id, "sizing Repair Plan")
        if not isinstance(self.decision, RepairCompileDecision):
            raise ValueError("sizing repair decision must be typed")
        owner_identity(self.owner, "sizing Repair Plan owner")
        identifier(self.policy_id, "sizing Repair Plan policy")
        for value, label in (
            (self.parent_candidate_identity, "parent Candidate"),
            (self.parent_problem_identity, "parent sizing problem"),
            (self.parent_result_identity, "parent sizing result"),
            (self.policy_identity, "repair policy"),
            (self.proposal_identity, "sizing proposal"),
        ):
            bounded_identity(value, label)
        if self.evidence_identity != tuple(sorted(set(self.evidence_identity))):
            raise ValueError("sizing repair evidence must be unique and sorted")
        for value in self.evidence_identity:
            bounded_identity(value, "sizing repair evidence")
        if type(self.requested_evaluations) is not int or self.requested_evaluations <= 0:
            raise ValueError("sizing Repair Plan evaluation count must be positive")
        _identities(self.required_regressions, "sizing Repair Plan regressions")
        _circuit_identities(self.changed_parameters, "changed sizing parameters")
        if self.decision is RepairCompileDecision.ACCEPTED:
            if (
                self.proposed_candidate is None
                or not self.changed_parameters
                or not self.evidence_identity
            ):
                raise ValueError(
                    "accepted sizing Repair Plan needs evidence and a concrete change"
                )
        elif self.proposed_candidate is not None or self.changed_parameters:
            raise ValueError("rejected sizing Repair Plan cannot carry an action")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("sizing Repair Plan needs a reason")

    @property
    def accepted(self) -> bool:
        return self.decision is RepairCompileDecision.ACCEPTED

    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def identity(self) -> str:
        return self.plan_id


def topology_repair_plan_from_json(text: str) -> TopologyRepairPlan:
    return canonical_from_exact_json(text, TopologyRepairPlan)


def sizing_repair_plan_from_json(text: str) -> SizingRepairPlan:
    return canonical_from_exact_json(text, SizingRepairPlan)


def _reference_key(reference: ArtifactReference) -> tuple[str, str, str]:
    return reference.owner, reference.kind, reference.identity


def _evidence_issue(
    candidate: DesignCandidate,
    parent: ArtifactReference,
    evidence: tuple[DesignEvidence, ...],
) -> tuple[RepairCompileDecision, str] | None:
    if not evidence:
        return RepairCompileDecision.UNSUPPORTED, "repair compilation requires typed failure evidence"
    declared = {_reference_key(item) for item in candidate.evidence}
    for item in evidence:
        if item.metadata.owner != candidate.metadata.owner:
            return RepairCompileDecision.INVALID_IDENTITY, "repair evidence owner drift"
        if _reference_key(item.reference()) not in declared:
            return RepairCompileDecision.INVALID_IDENTITY, "repair evidence is not bound by the parent Candidate"
        if item.subject != parent or item.source != candidate.source:
            return RepairCompileDecision.INVALID_IDENTITY, "repair evidence parent identity drift"
    if not any(item.conclusion is EvidenceConclusion.VIOLATED for item in evidence):
        return RepairCompileDecision.UNSUPPORTED, "repair compilation requires a typed violated conclusion"
    return None


def _regression_issue(
    requested: tuple[str, ...],
    required: tuple[str, ...],
) -> str | None:
    try:
        _identities(requested, "requested repair regressions")
    except ValueError as exc:
        return str(exc)
    missing = sorted(set(required) - set(requested))
    return None if not missing else f"repair proposal omitted required regressions: {missing}"


def _topology_plan(
    decision: RepairCompileDecision,
    *,
    candidate: DesignCandidate,
    parent: CircuitTopologyProposal,
    proposed: CircuitTopologyProposal,
    evidence: tuple[DesignEvidence, ...],
    policy: TopologyRepairPolicy,
    regressions: tuple[str, ...],
    reason: str,
    changed: tuple[str, ...] = (),
) -> TopologyRepairPlan:
    return TopologyRepairPlan(
        f"{candidate.identity}:topology-repair:{proposed.identity}",
        decision,
        candidate.metadata.owner,
        policy.policy_id,
        candidate.identity,
        parent.identity,
        tuple(sorted(item.identity for item in evidence)),
        f"{policy.owner}:topology-repair-policy:{policy.policy_id}",
        proposed.identity,
        regressions,
        changed if decision is RepairCompileDecision.ACCEPTED else (),
        proposed if decision is RepairCompileDecision.ACCEPTED else None,
        reason,
    )


def compile_topology_repair(
    *,
    candidate: DesignCandidate,
    parent: CircuitTopologyProposal,
    proposed: CircuitTopologyProposal,
    evidence: tuple[DesignEvidence, ...],
    policy: TopologyRepairPolicy,
    required_regressions: tuple[str, ...],
) -> TopologyRepairPlan:
    """Validate one full topology proposal; never mutate source or synthesize a change."""

    regressions = tuple(required_regressions)
    if policy.owner != candidate.metadata.owner or parent.metadata.owner != candidate.metadata.owner:
        return _topology_plan(
            RepairCompileDecision.INVALID_IDENTITY,
            candidate=candidate,
            parent=parent,
            proposed=proposed,
            evidence=evidence,
            policy=policy,
            regressions=regressions,
            reason="topology repair owner identity drift",
        )
    if candidate.topology != parent.reference():
        return _topology_plan(
            RepairCompileDecision.INVALID_IDENTITY,
            candidate=candidate,
            parent=parent,
            proposed=proposed,
            evidence=evidence,
            policy=policy,
            regressions=regressions,
            reason="topology repair parent Candidate identity drift",
        )
    issue = _evidence_issue(candidate, parent.reference(), evidence)
    if issue is not None:
        return _topology_plan(
            issue[0], candidate=candidate, parent=parent, proposed=proposed,
            evidence=evidence, policy=policy, regressions=regressions, reason=issue[1]
        )
    regression_issue = _regression_issue(regressions, policy.required_regressions)
    if regression_issue is not None:
        return _topology_plan(
            RepairCompileDecision.ILLEGAL_CHANGE,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions, reason=regression_issue,
        )
    if (
        proposed.metadata.owner != candidate.metadata.owner
        or proposed.design != parent.design
        or proposed.source_snapshot_identity != parent.source_snapshot_identity
        or proposed.origin is not TopologyOrigin.PROPOSED
    ):
        return _topology_plan(
            RepairCompileDecision.INVALID_IDENTITY,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions,
            reason="proposed topology owner, design, source, or origin identity drift",
        )
    if (
        proposed.ports != parent.ports
        or proposed.parameters != parent.parameters
        or proposed.states != parent.states
    ):
        return _topology_plan(
            RepairCompileDecision.ILLEGAL_CHANGE,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions,
            reason="topology repair cannot drift ports, top-level parameters, or legal states",
        )
    parents = {item.name: item for item in parent.instances}
    children = {item.name: item for item in proposed.instances}
    if set(parents) != set(children):
        return _topology_plan(
            RepairCompileDecision.ILLEGAL_CHANGE,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions,
            reason="topology repair cannot add or remove owner instances",
        )
    changed: list[str] = []
    for name in sorted(parents):
        before, after = parents[name], children[name]
        if before.nodes != after.nodes or before.role != after.role:
            return _topology_plan(
                RepairCompileDecision.ILLEGAL_CHANGE,
                candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
                policy=policy, regressions=regressions,
                reason="topology repair cannot drift instance connectivity or role",
            )
        if before != after:
            if after.master not in policy.allowed_masters:
                return _topology_plan(
                    RepairCompileDecision.ILLEGAL_CHANGE,
                    candidate=candidate, parent=parent, proposed=proposed,
                    evidence=evidence, policy=policy, regressions=regressions,
                    reason="topology repair selected a master outside owner policy",
                )
            changed.append(name)
    if not changed:
        return _topology_plan(
            RepairCompileDecision.UNSUPPORTED,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions,
            reason="topology proposal does not change the parent topology",
        )
    if len(changed) > policy.maximum_instance_changes:
        return _topology_plan(
            RepairCompileDecision.BUDGET_EXHAUSTED,
            candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
            policy=policy, regressions=regressions,
            reason="topology proposal exceeds the owner change budget",
        )
    return _topology_plan(
        RepairCompileDecision.ACCEPTED,
        candidate=candidate, parent=parent, proposed=proposed, evidence=evidence,
        policy=policy, regressions=regressions, changed=tuple(changed),
        reason="topology proposal is parent-bound, policy-legal, and regression-complete",
    )


def _sizing_plan(
    decision: RepairCompileDecision,
    *,
    candidate: DesignCandidate,
    problem: CircuitSizingProblem,
    parent_result: CircuitSizingResult,
    proposed: SizingCandidate,
    requested_evaluations: int,
    evidence: tuple[DesignEvidence, ...],
    policy: SizingRepairPolicy,
    regressions: tuple[str, ...],
    reason: str,
    changed: tuple[str, ...] = (),
) -> SizingRepairPlan:
    return SizingRepairPlan(
        f"{candidate.identity}:sizing-repair:{proposed.name}",
        decision,
        candidate.metadata.owner,
        policy.policy_id,
        candidate.identity,
        problem.identity,
        parent_result.identity,
        tuple(sorted(item.identity for item in evidence)),
        f"{policy.owner}:sizing-repair-policy:{policy.policy_id}",
        proposed.name,
        requested_evaluations,
        regressions,
        changed if decision is RepairCompileDecision.ACCEPTED else (),
        proposed if decision is RepairCompileDecision.ACCEPTED else None,
        reason,
    )


def _values(candidate: SizingCandidate) -> dict[str, object]:
    return {item.name: item.quantity for item in candidate.values}


def compile_sizing_repair(
    *,
    candidate: DesignCandidate,
    problem: CircuitSizingProblem,
    parent_result: CircuitSizingResult,
    proposed: SizingCandidate,
    requested_evaluations: int,
    evidence: tuple[DesignEvidence, ...],
    policy: SizingRepairPolicy,
    required_regressions: tuple[str, ...],
) -> SizingRepairPlan:
    """Compile a bounded sizing candidate against declared values and matching groups."""

    regressions = tuple(required_regressions)
    common = dict(
        candidate=candidate,
        problem=problem,
        parent_result=parent_result,
        proposed=proposed,
        requested_evaluations=requested_evaluations,
        evidence=evidence,
        policy=policy,
        regressions=regressions,
    )
    if (
        policy.owner != candidate.metadata.owner
        or problem.metadata.owner != candidate.metadata.owner
        or parent_result.metadata.owner != candidate.metadata.owner
        or candidate.sizing_problem != problem.reference()
        or candidate.sizing_result != parent_result.reference()
        or parent_result.problem != problem.reference()
    ):
        return _sizing_plan(
            RepairCompileDecision.INVALID_IDENTITY,
            **common,
            reason="sizing repair owner or parent identity drift",
        )
    issue = _evidence_issue(candidate, parent_result.reference(), evidence)
    if issue is not None:
        return _sizing_plan(issue[0], **common, reason=issue[1])
    regression_issue = _regression_issue(regressions, policy.required_regressions)
    if regression_issue is not None:
        return _sizing_plan(
            RepairCompileDecision.ILLEGAL_CHANGE, **common, reason=regression_issue
        )
    if (
        type(requested_evaluations) is not int
        or requested_evaluations <= 0
        or requested_evaluations > policy.maximum_evaluations
        or requested_evaluations > problem.budget.maximum_evaluations
    ):
        return _sizing_plan(
            RepairCompileDecision.BUDGET_EXHAUSTED,
            **common,
            reason="sizing proposal exceeds the declared evaluation budget",
        )
    parameters = {item.name: item for item in problem.parameters}
    values = _values(proposed)
    if set(values) != set(parameters) or len(values) != len(proposed.values):
        return _sizing_plan(
            RepairCompileDecision.ILLEGAL_CHANGE,
            **common,
            reason="sizing proposal must bind every parameter exactly once",
        )
    for name, quantity in values.items():
        parameter = parameters[name]
        if quantity.unit != parameter.unit or quantity not in parameter.discrete_values:
            return _sizing_plan(
                RepairCompileDecision.ILLEGAL_CHANGE,
                **common,
                reason="sizing proposal contains a value outside declared discrete bounds",
            )
    for group in problem.matching_groups:
        group_values = {values[name] for name in group.parameters}
        if len(group_values) != 1:
            return _sizing_plan(
                RepairCompileDecision.ILLEGAL_CHANGE,
                **common,
                reason="sizing proposal violates a declared matching group",
            )
    parent_values: dict[str, object] = {}
    if parent_result.selected_candidate is not None:
        selected = next(
            (item for item in problem.candidates if item.name == parent_result.selected_candidate),
            None,
        )
        if selected is None:
            return _sizing_plan(
                RepairCompileDecision.INVALID_IDENTITY,
                **common,
                reason="parent sizing result selected an undeclared candidate",
            )
        parent_values = _values(selected)
    changed = tuple(
        sorted(name for name, value in values.items() if parent_values.get(name) != value)
    )
    if not changed:
        return _sizing_plan(
            RepairCompileDecision.UNSUPPORTED,
            **common,
            reason="sizing proposal does not change the selected parent candidate",
        )
    if not set(changed).issubset(policy.allowed_parameters):
        return _sizing_plan(
            RepairCompileDecision.ILLEGAL_CHANGE,
            **common,
            reason="sizing proposal changes a parameter outside owner policy",
        )
    return _sizing_plan(
        RepairCompileDecision.ACCEPTED,
        **common,
        changed=changed,
        reason="sizing proposal is parent-bound, range-legal, matched, and regression-complete",
    )


def attribute_design_failure(
    candidate: DesignCandidate,
    evidence: tuple[DesignEvidence, ...],
) -> DesignRepairAttribution:
    """Attribute exact Candidate-bound violations without interpreting free text."""

    if not evidence:
        raise ValueError("design repair attribution requires violated evidence")
    declared = {_reference_key(item) for item in candidate.evidence}
    subjects = {_reference_key(item.subject) for item in evidence}
    if len(subjects) != 1:
        raise ValueError("design repair attribution requires one exact subject")
    for item in evidence:
        if item.metadata.owner != candidate.metadata.owner:
            raise ValueError("design repair attribution evidence owner drift")
        if _reference_key(item.reference()) not in declared:
            raise ValueError("design repair attribution evidence is outside the Candidate")
        if item.source != candidate.source:
            raise ValueError("design repair attribution source identity drift")
        if item.conclusion is not EvidenceConclusion.VIOLATED:
            raise ValueError("design repair attribution requires violated conclusions")
    subject = evidence[0].subject
    if subject == candidate.topology:
        kind = DesignRepairKind.TOPOLOGY
    elif candidate.sizing_result is not None and subject == candidate.sizing_result:
        kind = DesignRepairKind.SIZING
    else:
        raise ValueError("violated evidence has no supported repair subject")
    return DesignRepairAttribution(
        f"{candidate.identity}:repair-attribution:{kind.value}",
        candidate.metadata.owner,
        candidate.reference(),
        subject,
        tuple(sorted((item.reference() for item in evidence), key=lambda item: item.identity)),
        kind,
        tuple(sorted({finding.code for item in evidence for finding in item.findings})),
    )


def compile_design_repair(
    *,
    campaign_identity: str,
    attribution: DesignRepairAttribution,
    proposal: DesignRepairProposal,
    candidate: DesignCandidate,
    parent: CircuitTopologyProposal | CircuitSizingProblem,
    parent_result: CircuitSizingResult | None,
    evidence: tuple[DesignEvidence, ...],
    policy: TopologyRepairPolicy | SizingRepairPolicy,
) -> TopologyRepairPlan | SizingRepairPlan:
    """Compile one semantic proposal through its project-owned typed policy."""

    if (
        proposal.owner != candidate.metadata.owner
        or proposal.campaign_identity != campaign_identity
        or proposal.parent_candidate_identity != candidate.identity
        or proposal.attribution_identity != attribution.identity
        or proposal.evidence_identity
        != tuple(reference.identity for reference in attribution.evidence)
        or proposal.repair_kind is not attribution.repair_kind
    ):
        raise ValueError("design repair proposal identity drift")
    if proposal.repair_kind is DesignRepairKind.TOPOLOGY:
        if (
            not isinstance(policy, TopologyRepairPolicy)
            or not isinstance(parent, CircuitTopologyProposal)
            or proposal.proposed_topology is None
        ):
            raise ValueError("topology repair proposal does not match owner policy")
        return compile_topology_repair(
            candidate=candidate,
            parent=parent,
            proposed=proposal.proposed_topology,
            evidence=evidence,
            policy=policy,
            required_regressions=proposal.required_regressions,
        )
    if (
        not isinstance(policy, SizingRepairPolicy)
        or not isinstance(parent, CircuitSizingProblem)
        or not isinstance(parent_result, CircuitSizingResult)
        or proposal.proposed_sizing is None
        or proposal.requested_evaluations is None
    ):
        raise ValueError("sizing repair proposal does not match owner policy")
    return compile_sizing_repair(
        candidate=candidate,
        problem=parent,
        parent_result=parent_result,
        proposed=proposal.proposed_sizing,
        requested_evaluations=proposal.requested_evaluations,
        evidence=evidence,
        policy=policy,
        required_regressions=proposal.required_regressions,
    )


__all__ = [
    "DesignRepairAttribution",
    "DesignRepairKind",
    "DesignRepairProposal",
    "RepairCompileDecision",
    "SizingRepairPlan",
    "SizingRepairPolicy",
    "TopologyRepairPlan",
    "TopologyRepairPolicy",
    "attribute_design_failure",
    "compile_design_repair",
    "compile_sizing_repair",
    "compile_topology_repair",
    "design_repair_proposal_from_json",
    "sizing_repair_plan_from_json",
    "topology_repair_plan_from_json",
]
