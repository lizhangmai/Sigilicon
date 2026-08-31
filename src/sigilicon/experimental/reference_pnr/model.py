"""Reference PNR policy, repair and closure values.

The normalized physical-design contract lives in
``sigilicon.layout.physical_design``.  These values are deliberately kept in
the experimental reference implementation because they describe one solver's
search budgets and repair strategy rather than physical-design intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sigilicon.layout.physical_design import *  # noqa: F401,F403


class PhysicalOwnerMobility(str, Enum):
    MOVABLE = "movable"
    FIXED = "fixed"
    UNSUPPORTED = "unsupported"


class RoutingConflictKind(str, Enum):
    HARD_BLOCKER = "hard_blocker"
    CAPACITY_OVERFLOW = "capacity_overflow"
    UNROUTED_TERMINAL = "unrouted_terminal"
    GROUP_CONSTRAINT = "group_constraint_failure"
    VIA_EXHAUSTION = "via_resource_exhaustion"
    TOPOLOGY_CONFLICT = "topology_conflict"
    BUDGET_EXHAUSTION = "budget_exhaustion"


class RoutingTerminationReason(str, Enum):
    CLOSED = "closed"
    INFEASIBLE = "infeasible"
    UNSUPPORTED = "unsupported"
    STATE_BUDGET = "state_budget"
    ITERATION_BUDGET = "iteration_budget"


class PlacementRoutingTerminationReason(str, Enum):
    CLOSED = "closed"
    ROUTING_TERMINATED = "routing_terminated"
    NO_LEGAL_REPAIR = "no_legal_repair"
    REPAIR_STATE_BUDGET = "repair_state_budget"
    REPAIR_ITERATION_BUDGET = "repair_iteration_budget"
    INDEPENDENT_EVALUATION_FAILED = "independent_evaluation_failed"


class RoutingClosureQualityDecision(str, Enum):
    IMPROVED = "improved"
    EQUIVALENT = "equivalent"
    REGRESSED = "regressed"


@dataclass(frozen=True)
class ReferencePnrExecutionPolicy(CanonicalValue):
    """Reference-engine budgets and analysis resolution."""

    maximum_search_states: int = 100_000
    maximum_route_states: int = 200_000
    maximum_routing_iterations: int = 8
    maximum_placement_repair_states: int = 100_000
    maximum_placement_repair_iterations: int = 4
    routing_congestion_bins_x: int = 8
    routing_congestion_bins_y: int = 8


@dataclass(frozen=True)
class ReferencePnrJobLineage(CanonicalValue):
    """Immediate attributed-repair parentage for a reference run."""

    owner: str
    parent_job_identity: str
    parent_result_identity: str
    feedback_identity: str
    repair_plan_identity: str
    source_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("repair lineage owner must not be empty")
        for label, value in (
            ("parent job", self.parent_job_identity),
            ("parent result", self.parent_result_identity),
            ("feedback", self.feedback_identity),
            ("repair plan", self.repair_plan_identity),
        ):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError(f"repair lineage {label} must be non-empty")
        if not self.source_evidence or any(
            not isinstance(value, str) or not value for value in self.source_evidence
        ):
            raise ValueError("repair lineage needs source evidence identities")
        if not isinstance(self.source_evidence, tuple):
            raise ValueError("repair lineage source evidence must be an immutable tuple")


@dataclass(frozen=True)
class ReferencePnrJob(PhysicalDesignJob):
    """Stable normalized Job plus reference-only search state."""

    execution_policy: ReferencePnrExecutionPolicy = ReferencePnrExecutionPolicy()
    repair_lineage: ReferencePnrJobLineage | None = None


@dataclass(frozen=True)
class RoutingClosureQuality(CanonicalValue):
    resource_overflow: int
    unrouted_branches: int
    hard_blockers: int
    group_violations: int
    via_failures: int
    topology_failures: int
    unsupported_failures: int
    budget_exhaustions: int
    checker_violations: int
    constraint_violations: int
    constraint_not_evaluated: int
    aggregate_placement_pressure: int
    routed_nets: int
    routed_branches: int
    placement_displacement_dbu: int
    routing_termination: RoutingTerminationReason
    closed: bool


@dataclass(frozen=True)
class PhysicalOwnerSummary(CanonicalValue):
    identity: PhysicalOwnerIdentity
    mobility: PhysicalOwnerMobility
    repair_owner: PhysicalOwnerIdentity | None


@dataclass(frozen=True)
class RoutingConflictSummary(CanonicalValue):
    identity: str
    kind: RoutingConflictKind
    resource: str | None
    aggressor_nets: tuple[str, ...]
    occupant_nets: tuple[str, ...]
    affected_group: str | None
    severity: int
    cost: int
    evidence: str
    physical_owners: tuple[PhysicalOwnerIdentity, ...]
    resource_overflow: int


@dataclass(frozen=True)
class RoutingPlacementPressureSummary(CanonicalValue):
    identity: str
    source_conflict: str
    conflict_kind: RoutingConflictKind
    resource: str | None
    region: Rect | None
    physical_owner_candidates: tuple[PhysicalOwnerSummary, ...]
    involved_nets: tuple[str, ...]
    involved_groups: tuple[str, ...]
    severity: int
    cost: int
    evidence: str
    reason: str
    repair_scope: tuple[str, ...]


@dataclass(frozen=True)
class PlacementRoutingRepairSummary(CanonicalValue):
    iteration: int
    moved_owner: PhysicalOwnerIdentity
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    source_conflicts: tuple[str, ...]
    source_pressures: tuple[str, ...]
    source_pressure: tuple[RoutingPlacementPressureSummary, ...]
    displacement_dbu: int
    predicted_released_resources: tuple[str, ...]
    predicted_released_pressure: int
    predicted_remaining_pressure: int
    predicted_pin_access_gain: int
    predicted_pin_access_loss: int
    current_quality: RoutingClosureQuality
    candidate_quality: RoutingClosureQuality
    decision: RoutingClosureQualityDecision
    accepted: bool


@dataclass(frozen=True)
class PlacementRoutingClosureEvidence(CanonicalValue):
    """Reference solver's complete placement-routing closure evidence."""

    termination: PlacementRoutingTerminationReason
    routing_termination: RoutingTerminationReason
    quality: RoutingClosureQuality
    conflict_identities: tuple[str, ...]
    pressure_identities: tuple[str, ...]
    conflicts: tuple[RoutingConflictSummary, ...]
    placement_pressure: tuple[RoutingPlacementPressureSummary, ...]
    repairs: tuple[PlacementRoutingRepairSummary, ...]
    artifact_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise ValueError("physical closure evidence needs an explicit artifact ID")


@dataclass(frozen=True)
class ReferencePnrResult(PhysicalDesignResult):
    """Stable result plus reference-only closure evidence."""

    closure_evidence: PlacementRoutingClosureEvidence | None = None


__all__ = [
    "PhysicalOwnerMobility",
    "PlacementRoutingClosureEvidence",
    "PlacementRoutingRepairSummary",
    "PlacementRoutingTerminationReason",
    "ReferencePnrExecutionPolicy",
    "ReferencePnrJob",
    "ReferencePnrJobLineage",
    "ReferencePnrResult",
    "RoutingClosureQuality",
    "RoutingClosureQualityDecision",
    "RoutingConflictKind",
    "RoutingConflictSummary",
    "RoutingPlacementPressureSummary",
    "RoutingTerminationReason",
]
