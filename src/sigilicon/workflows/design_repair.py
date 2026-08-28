"""Compile bounded topology and sizing proposals into non-mutating Repair Plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
    canonical_sha256,
)
from sigilicon.domain.circuit_design import (
    ArtifactReference,
    CircuitSizingProblem,
    CircuitSizingResult,
    CircuitTopologyProposal,
    DesignCandidate,
    DesignEvidence,
    EvidenceConclusion,
    SizingCandidate,
    TopologyOrigin,
)
from sigilicon.flow.model import identifier, owner_identity


_CIRCUIT_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$]*(?:[._-][A-Za-z0-9_$]+)*\Z"
)

class RepairCompileDecision(str, Enum):
    ACCEPTED = "accepted"
    INVALID_IDENTITY = "invalid_identity"
    UNSUPPORTED = "unsupported"
    ILLEGAL_CHANGE = "illegal_change"
    BUDGET_EXHAUSTED = "budget_exhausted"


def _sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a SHA-256 identity")


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
    decision: RepairCompileDecision
    owner: str
    policy_id: str
    parent_candidate_sha256: str
    parent_topology_sha256: str
    evidence_sha256: tuple[str, ...]
    policy_sha256: str
    proposal_sha256: str
    required_regressions: tuple[str, ...]
    changed_instances: tuple[str, ...]
    proposed_topology: CircuitTopologyProposal | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RepairCompileDecision):
            raise ValueError("topology repair decision must be typed")
        owner_identity(self.owner, "topology Repair Plan owner")
        identifier(self.policy_id, "topology Repair Plan policy")
        for value, label in (
            (self.parent_candidate_sha256, "parent Candidate"),
            (self.parent_topology_sha256, "parent topology"),
            (self.policy_sha256, "repair policy"),
            (self.proposal_sha256, "topology proposal"),
        ):
            _sha256(value, label)
        if self.evidence_sha256 != tuple(sorted(set(self.evidence_sha256))):
            raise ValueError("topology repair evidence must be unique and sorted")
        for value in self.evidence_sha256:
            _sha256(value, "topology repair evidence")
        _identities(self.required_regressions, "topology Repair Plan regressions")
        if self.changed_instances != tuple(sorted(set(self.changed_instances))):
            raise ValueError("changed topology instances must be unique and sorted")
        _circuit_identities(self.changed_instances, "changed topology instances")
        if self.decision is RepairCompileDecision.ACCEPTED:
            if (
                self.proposed_topology is None
                or not self.changed_instances
                or not self.evidence_sha256
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
        return canonical_sha256(self)


@dataclass(frozen=True)
class SizingRepairPlan:
    decision: RepairCompileDecision
    owner: str
    policy_id: str
    parent_candidate_sha256: str
    parent_problem_sha256: str
    parent_result_sha256: str
    evidence_sha256: tuple[str, ...]
    policy_sha256: str
    proposal_sha256: str
    requested_evaluations: int
    required_regressions: tuple[str, ...]
    changed_parameters: tuple[str, ...]
    proposed_candidate: SizingCandidate | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RepairCompileDecision):
            raise ValueError("sizing repair decision must be typed")
        owner_identity(self.owner, "sizing Repair Plan owner")
        identifier(self.policy_id, "sizing Repair Plan policy")
        for value, label in (
            (self.parent_candidate_sha256, "parent Candidate"),
            (self.parent_problem_sha256, "parent sizing problem"),
            (self.parent_result_sha256, "parent sizing result"),
            (self.policy_sha256, "repair policy"),
            (self.proposal_sha256, "sizing proposal"),
        ):
            _sha256(value, label)
        if self.evidence_sha256 != tuple(sorted(set(self.evidence_sha256))):
            raise ValueError("sizing repair evidence must be unique and sorted")
        for value in self.evidence_sha256:
            _sha256(value, "sizing repair evidence")
        if type(self.requested_evaluations) is not int or self.requested_evaluations <= 0:
            raise ValueError("sizing Repair Plan evaluation count must be positive")
        _identities(self.required_regressions, "sizing Repair Plan regressions")
        _circuit_identities(self.changed_parameters, "changed sizing parameters")
        if self.decision is RepairCompileDecision.ACCEPTED:
            if (
                self.proposed_candidate is None
                or not self.changed_parameters
                or not self.evidence_sha256
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
        return canonical_sha256(self)


def topology_repair_plan_from_json(text: str) -> TopologyRepairPlan:
    return canonical_from_exact_json(text, TopologyRepairPlan)


def sizing_repair_plan_from_json(text: str) -> SizingRepairPlan:
    return canonical_from_exact_json(text, SizingRepairPlan)


def _reference_key(reference: ArtifactReference) -> tuple[str, str, str]:
    return reference.owner, reference.kind, reference.sha256


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
        decision,
        candidate.metadata.owner,
        policy.policy_id,
        candidate.identity,
        parent.identity,
        tuple(sorted(item.identity for item in evidence)),
        canonical_sha256(policy),
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
        or proposed.source_snapshot_sha256 != parent.source_snapshot_sha256
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
        decision,
        candidate.metadata.owner,
        policy.policy_id,
        candidate.identity,
        problem.identity,
        parent_result.identity,
        tuple(sorted(item.identity for item in evidence)),
        canonical_sha256(policy),
        canonical_sha256(proposed),
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


__all__ = [
    "RepairCompileDecision",
    "SizingRepairPlan",
    "SizingRepairPolicy",
    "TopologyRepairPlan",
    "TopologyRepairPolicy",
    "compile_sizing_repair",
    "compile_topology_repair",
    "sizing_repair_plan_from_json",
    "topology_repair_plan_from_json",
]
