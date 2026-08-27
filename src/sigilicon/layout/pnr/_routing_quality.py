"""Typed Placement↔Routing closure quality and comparison policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sigilicon.layout.pnr._routing import RoutingSolveResult
from sigilicon.layout.pnr._routing_check import check_routing_solution
from sigilicon.layout.pnr._routing_conflicts import (
    RoutingConflictKind,
    RoutingTerminationReason,
)
from sigilicon.layout.pnr._routing_constraints import (
    evaluate_routing_constraints,
)
from sigilicon.layout.pnr.model import (
    ConstraintStatus,
    InstancePlacement,
    PhysicalDesignJob,
    ResultStatus,
    RoutingBlockagePlacement,
)


@dataclass(frozen=True)
class RoutingClosureQuality:
    """Complete typed evidence used to compare two closure states."""

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


class RoutingClosureQualityDecision(str, Enum):
    IMPROVED = "improved"
    EQUIVALENT = "equivalent"
    REGRESSED = "regressed"


@dataclass(frozen=True)
class RoutingClosureQualityPolicy:
    """Conservative deterministic lexicographic closure ordering."""

    def key(self, quality: RoutingClosureQuality) -> tuple[int, ...]:
        return (
            quality.resource_overflow,
            quality.unrouted_branches,
            quality.hard_blockers,
            quality.group_violations,
            quality.via_failures,
            quality.topology_failures,
            quality.unsupported_failures,
            quality.budget_exhaustions,
            quality.checker_violations,
            quality.constraint_violations,
            quality.constraint_not_evaluated,
            int(not quality.closed),
            quality.aggregate_placement_pressure,
            -quality.routed_branches,
            -quality.routed_nets,
            quality.placement_displacement_dbu,
        )

    def compare(
        self,
        candidate: RoutingClosureQuality,
        current: RoutingClosureQuality,
    ) -> RoutingClosureQualityDecision:
        candidate_key = self.key(candidate)
        current_key = self.key(current)
        if candidate_key < current_key:
            return RoutingClosureQualityDecision.IMPROVED
        if candidate_key == current_key:
            return RoutingClosureQualityDecision.EQUIVALENT
        return RoutingClosureQualityDecision.REGRESSED


def _placement_displacement(
    initial: tuple[InstancePlacement, ...],
    current: tuple[InstancePlacement, ...],
) -> int:
    initial_by_name = {item.instance: item.placement for item in initial}
    return sum(
        abs(item.placement.origin.x - initial_by_name[item.instance].origin.x)
        + abs(item.placement.origin.y - initial_by_name[item.instance].origin.y)
        for item in current
    )


def _routing_blockage_displacement(
    initial: tuple[RoutingBlockagePlacement, ...],
    current: tuple[RoutingBlockagePlacement, ...],
) -> int:
    initial_by_name = {item.blockage: item.placement for item in initial}
    return sum(
        abs(item.placement.origin.x - initial_by_name[item.blockage].origin.x)
        + abs(item.placement.origin.y - initial_by_name[item.blockage].origin.y)
        for item in current
    )


def compile_routing_closure_quality(
    job: PhysicalDesignJob,
    routing: RoutingSolveResult,
    placements: tuple[InstancePlacement, ...],
    *,
    initial_placements: tuple[InstancePlacement, ...],
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] = (),
    initial_routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] = (),
) -> RoutingClosureQuality:
    """Compile solver, checker, constraint, pressure, and displacement facts."""

    routed = frozenset(route.net for route in routing.routes)
    branch_count_by_net = {
        net.name: max(0, len(net.pins) - 1)
        for net in job.design.nets
    }
    routed_branches = sum(
        branch_count_by_net.get(net, 0)
        for net in routed
    )
    unrouted_branches = sum(
        count
        for net, count in branch_count_by_net.items()
        if net not in routed
    )
    conflicts = routing.conflicts.conflicts
    overflow = sum(conflict.resource_overflow for conflict in conflicts)
    hard_blockers = sum(
        conflict.severity
        for conflict in conflicts
        if conflict.kind is RoutingConflictKind.HARD_BLOCKER
    )
    group_violations = sum(
        conflict.severity
        for conflict in conflicts
        if conflict.kind is RoutingConflictKind.GROUP_CONSTRAINT
        or conflict.affected_group is not None
    )
    via_failures = sum(
        conflict.severity
        for conflict in conflicts
        if conflict.kind is RoutingConflictKind.VIA_EXHAUSTION
    )
    topology_failures = sum(
        conflict.severity
        for conflict in conflicts
        if conflict.kind
        in (
            RoutingConflictKind.TOPOLOGY_CONFLICT,
            RoutingConflictKind.UNROUTED_TERMINAL,
        )
    )
    budget_conflicts = sum(
        conflict.severity
        for conflict in conflicts
        if conflict.kind is RoutingConflictKind.BUDGET_EXHAUSTION
    )
    budget_exhaustions = budget_conflicts + int(
        routing.termination.reason
        in (
            RoutingTerminationReason.STATE_BUDGET,
            RoutingTerminationReason.ITERATION_BUDGET,
        )
    )
    unsupported_failures = int(
        routing.termination.reason is RoutingTerminationReason.UNSUPPORTED
    )
    checker_diagnostics = check_routing_solution(
        job,
        placements,
        routing.routes,
        routing_blockage_placements,
    )
    outcomes = evaluate_routing_constraints(
        job,
        routing.routes,
        placements,
    )
    constraint_violations = sum(
        outcome.status in (ConstraintStatus.VIOLATED, ConstraintStatus.UNSUPPORTED)
        for outcome in outcomes
    )
    unsupported_failures += sum(
        outcome.status is ConstraintStatus.UNSUPPORTED
        for outcome in outcomes
    )
    constraint_not_evaluated = sum(
        outcome.status is ConstraintStatus.NOT_EVALUATED
        for outcome in outcomes
    )
    aggregate_pressure = sum(
        (site.severity + site.cost)
        * max(1, len(site.physical_owner_candidates))
        for site in routing.placement_pressure.sites
    )
    closed = (
        routing.status is ResultStatus.SUCCEEDED
        and overflow == 0
        and unrouted_branches == 0
        and hard_blockers == 0
        and group_violations == 0
        and via_failures == 0
        and topology_failures == 0
        and not checker_diagnostics
        and constraint_violations == 0
        and constraint_not_evaluated == 0
        and all(
            outcome.status is ConstraintStatus.SATISFIED
            for outcome in outcomes
        )
    )
    return RoutingClosureQuality(
        resource_overflow=overflow,
        unrouted_branches=unrouted_branches,
        hard_blockers=hard_blockers,
        group_violations=group_violations,
        via_failures=via_failures,
        topology_failures=topology_failures,
        unsupported_failures=unsupported_failures,
        budget_exhaustions=budget_exhaustions,
        checker_violations=len(checker_diagnostics),
        constraint_violations=constraint_violations,
        constraint_not_evaluated=constraint_not_evaluated,
        aggregate_placement_pressure=aggregate_pressure,
        routed_nets=len(routed),
        routed_branches=routed_branches,
        placement_displacement_dbu=_placement_displacement(
            initial_placements,
            placements,
        )
        + _routing_blockage_displacement(
            initial_routing_blockage_placements,
            routing_blockage_placements,
        ),
        routing_termination=routing.termination.reason,
        closed=closed,
    )
