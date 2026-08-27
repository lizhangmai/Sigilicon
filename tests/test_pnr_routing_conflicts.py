from __future__ import annotations

from sigilicon.layout.pnr._routing_conflicts import (
    DeterministicVictimPolicy,
    RoutingConflictKind,
    capacity_conflicts,
)
from sigilicon.layout.pnr._routing_resources import (
    RoutingResourceIdentity,
    RoutingResourceOverflow,
)


def test_capacity_conflicts_and_victim_selection_are_stable_and_explicit() -> None:
    resource = RoutingResourceIdentity(
        "gridless_corridor", "route", (0, 1, "horizontal")
    )
    overflow = RoutingResourceOverflow(
        resource,
        usage=3,
        capacity=1,
        occupants=("alpha", "beta", "gamma"),
    )

    first = capacity_conflicts(
        (overflow,),
        group_by_net={"alpha": None, "beta": "pair", "gamma": "pair"},
        reroute_scope_by_net={
            "alpha": ("alpha",),
            "beta": ("beta", "gamma"),
            "gamma": ("beta", "gamma"),
        },
        branch_occupants={resource: ("alpha:terminal:2",)},
    )
    second = capacity_conflicts(
        (overflow,),
        group_by_net={"gamma": "pair", "beta": "pair", "alpha": None},
        reroute_scope_by_net={
            "gamma": ("beta", "gamma"),
            "beta": ("beta", "gamma"),
            "alpha": ("alpha",),
        },
        branch_occupants={resource: ("alpha:terminal:2",)},
    )
    selection = DeterministicVictimPolicy().select(
        first,
        routed_order=("gamma", "alpha", "beta"),
        routed_nets=("alpha", "beta", "gamma"),
        reroute_scope_by_net={
            "alpha": ("alpha",),
            "beta": ("beta", "gamma"),
            "gamma": ("beta", "gamma"),
        },
        branch_safety={"alpha:terminal:2": True},
    )

    assert first == second
    assert first.conflicts[0].kind is RoutingConflictKind.CAPACITY_OVERFLOW
    assert first.conflicts[0].resource == resource
    assert first.conflicts[0].severity == 2
    assert first.conflicts[0].affected_group == "pair"
    assert first.conflicts[0].victim_candidates == ("alpha", "beta", "gamma")
    assert selection is not None
    assert selection.victim == "beta"
    assert selection.reroute_scope == ("beta", "gamma")
