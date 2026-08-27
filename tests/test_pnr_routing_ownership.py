from __future__ import annotations

from dataclasses import replace

from sigilicon.layout.pnr import (
    Axis,
    GridlessRoutingResource,
    InstancePlacement,
    LayerKind,
    LayerShape,
    MasterPin,
    MinimumSpacingRule,
    MinimumWidthRule,
    Orientation,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalLayer,
    PhysicalMaster,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PnrExecutionPolicy,
    Point,
    Rect,
    RoutingDirection,
    RoutingTrackPattern,
)
from sigilicon.layout.pnr._routing import solve_routing
from sigilicon.layout.pnr._routing_ownership import (
    PhysicalOwnerKind,
    PhysicalOwnerMobility,
)
from sigilicon.layout.pnr._routing_problem import compile_routing_problem
from sigilicon.layout.pnr._routing_resources import RoutingResourceKind


def _technology() -> PhysicalTechnology:
    return PhysicalTechnology(
        "owner-attribution-technology",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer(
                "route",
                LayerKind.ROUTING,
                RoutingDirection.HORIZONTAL,
            ),
        ),
        routing_resources=(
            RoutingTrackPattern(
                "only-horizontal-track",
                "route",
                Axis.Y,
                2,
                4,
                1,
            ),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )


def _blocker_job(
    *,
    fixed: bool = False,
    pin_access: bool = False,
    two_owners: bool = False,
) -> tuple[PhysicalDesignJob, tuple[InstancePlacement, ...]]:
    blocker = PhysicalMaster(
        "blocker-master",
        2,
        2,
        pins=(
            (
                MasterPin(
                    "unused",
                    (PinAccess("route", Rect(0, 0, 2, 2)),),
                ),
            )
            if pin_access
            else ()
        ),
        obstructions=(
            ()
            if pin_access
            else (LayerShape("route", Rect(0, 0, 2, 2)),)
        ),
        allowed_orientations=(Orientation.R0,),
    )
    names = ("blocker-a", "blocker-b") if two_owners else ("blocker-a",)
    placements = tuple(
        InstancePlacement(name, Placement(Point(8 + index * 2, 0)))
        for index, name in enumerate(names)
    )
    instances = tuple(
        PhysicalInstance(
            name,
            blocker.name,
            placements[index].placement if fixed else None,
        )
        for index, name in enumerate(names)
    )
    job = PhysicalDesignJob(
        (
            replace(
                _technology(),
                routing_resources=(
                    GridlessRoutingResource("shared-domain", "route"),
                ),
            )
            if two_owners
            else _technology()
        ),
        PhysicalDesign(
            "unrelated-physical-blocker",
            Rect(0, 0, 20, 8),
            (blocker,),
            instances,
            ports=(
                PhysicalPort(
                    "source",
                    (PinAccess("route", Rect(1, 1, 3, 3)),),
                ),
                PhysicalPort(
                    "sink",
                    (PinAccess("route", Rect(17, 1, 19, 3)),),
                ),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        execution_policy=PnrExecutionPolicy(
            routing_congestion_bins_x=1,
            routing_congestion_bins_y=1,
        ),
    )
    return job, placements


def test_unrelated_obstruction_owner_reaches_typed_routing_pressure() -> None:
    job, placements = _blocker_job()

    first = solve_routing(job, placements)
    second = solve_routing(job, placements)
    site = first.placement_pressure.sites[0]

    assert first == second
    assert first.placement_pressure.movable_instances == ("blocker-a",)
    assert site.source_conflict == first.conflicts.conflicts[0].identity
    assert site.involved_nets == ("signal",)
    assert site.repair_scope == ("blocker-a",)
    assert site.reason == "physical blocker ownership"
    assert tuple(
        owner.identity.stable_name
        for owner in site.physical_owner_candidates
    ) == ("instance:blocker-a",)
    assert site.physical_owner_candidates[0].mobility is (
        PhysicalOwnerMobility.MOVABLE
    )


def test_fixed_obstruction_remains_attributed_but_not_movable() -> None:
    job, placements = _blocker_job(fixed=True)

    routing = solve_routing(job, placements)
    owner = routing.placement_pressure.sites[0].physical_owner_candidates[0]

    assert routing.placement_pressure.movable_instances == ()
    assert owner.identity.stable_name == "instance:blocker-a"
    assert owner.mobility is PhysicalOwnerMobility.FIXED


def test_pin_access_blockage_attributes_the_instance_pin_owner() -> None:
    job, placements = _blocker_job(pin_access=True)

    routing = solve_routing(job, placements)
    site = routing.placement_pressure.sites[0]
    owner = site.physical_owner_candidates[0]

    assert owner.identity.kind is PhysicalOwnerKind.PIN
    assert owner.identity.stable_name == "pin:blocker-a:unused"
    assert owner.repair_instance == "blocker-a"
    assert site.pin == "unused"


def test_one_resource_can_resolve_multiple_physical_owners_stably() -> None:
    job, placements = _blocker_job(two_owners=True)
    problem = compile_routing_problem(job, placements)
    corridor = next(
        definition.identity
        for definition in problem.resource_graph.resources
        if definition.identity.kind
        == RoutingResourceKind.GRIDLESS_CORRIDOR.value
    )
    owners = problem.physical_ownership.owners_for_resource(
        problem.resource_graph,
        corridor,
    )

    assert tuple(owner.identity.stable_name for owner in owners) == (
        "instance:blocker-a",
        "instance:blocker-b",
        "port:sink",
        "port:source",
    )
    assert tuple(
        owner.identity.stable_name
        for owner in owners
        if owner.mobility is PhysicalOwnerMobility.MOVABLE
    ) == ("instance:blocker-a", "instance:blocker-b")
