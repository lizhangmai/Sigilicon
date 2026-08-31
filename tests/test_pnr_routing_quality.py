from __future__ import annotations

from dataclasses import replace

from sigilicon.experimental.reference_pnr._routing_conflicts import RoutingTerminationReason
from sigilicon.experimental.reference_pnr._routing_quality import (
    RoutingClosureQuality,
    RoutingClosureQualityDecision,
    RoutingClosureQualityPolicy,
)


def _quality() -> RoutingClosureQuality:
    return RoutingClosureQuality(
        resource_overflow=0,
        unrouted_branches=1,
        hard_blockers=1,
        group_violations=0,
        via_failures=0,
        topology_failures=0,
        unsupported_failures=0,
        budget_exhaustions=0,
        checker_violations=1,
        constraint_violations=0,
        constraint_not_evaluated=0,
        aggregate_placement_pressure=2,
        routed_nets=0,
        routed_branches=0,
        placement_displacement_dbu=0,
        routing_termination=RoutingTerminationReason.INFEASIBLE,
        closed=False,
    )


def test_overflow_regression_dominates_fewer_other_failures() -> None:
    policy = RoutingClosureQualityPolicy()
    current = replace(
        _quality(),
        hard_blockers=8,
        topology_failures=8,
        aggregate_placement_pressure=16,
    )
    fewer_conflicts_with_overflow = replace(
        current,
        resource_overflow=1,
        hard_blockers=0,
        topology_failures=0,
        aggregate_placement_pressure=0,
    )

    assert policy.compare(fewer_conflicts_with_overflow, current) is (
        RoutingClosureQualityDecision.REGRESSED
    )


def test_typed_owner_pressure_breaks_an_otherwise_equal_failure_tie() -> None:
    policy = RoutingClosureQualityPolicy()
    current = replace(_quality(), aggregate_placement_pressure=4)
    candidate = replace(
        current,
        aggregate_placement_pressure=2,
        placement_displacement_dbu=3,
    )

    assert policy.compare(candidate, current) is (
        RoutingClosureQualityDecision.IMPROVED
    )
    assert policy.compare(current, current) is (
        RoutingClosureQualityDecision.EQUIVALENT
    )


def test_lower_overflow_is_improvement_even_before_full_closure() -> None:
    policy = RoutingClosureQualityPolicy()
    current = replace(_quality(), resource_overflow=2)
    candidate = replace(
        current,
        resource_overflow=1,
        placement_displacement_dbu=4,
    )

    assert not current.closed
    assert not candidate.closed
    assert policy.compare(candidate, current) is (
        RoutingClosureQualityDecision.IMPROVED
    )


def test_displacement_cannot_make_equal_routing_quality_better() -> None:
    policy = RoutingClosureQualityPolicy()
    current = _quality()
    displaced = replace(current, placement_displacement_dbu=1)

    assert policy.compare(displaced, current) is (
        RoutingClosureQualityDecision.REGRESSED
    )
