from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.layout.pnr import (
    Axis,
    ConstraintStatus,
    CutSpacingRule,
    EnclosureRule,
    GridlessRoutingResource,
    LayerKind,
    LayerShape,
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
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    RoutingDirection,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingTrackPattern,
    RoutingViaCountConstraint,
    ViaDefinition,
    run,
)
from sigilicon.layout.pnr._placement import solve_placement
from sigilicon.layout.pnr._routing import solve_routing
from sigilicon.layout.pnr._routing_conflicts import (
    RoutingConflictKind,
    RoutingTerminationReason,
)


def _two_layer_technology(
    *,
    resources: tuple[GridlessRoutingResource | RoutingTrackPattern, ...],
) -> PhysicalTechnology:
    return PhysicalTechnology(
        "benchmark-two-layer",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer("lower", LayerKind.ROUTING, RoutingDirection.ANY),
            PhysicalLayer("cut", LayerKind.CUT),
            PhysicalLayer("upper", LayerKind.ROUTING, RoutingDirection.ANY),
        ),
        routing_resources=resources,
        via_definitions=(
            ViaDefinition(
                "lower-upper",
                "lower",
                "cut",
                "upper",
                lower_shapes=(Rect(-2, -2, 2, 2),),
                cut_shapes=(Rect(-1, -1, 1, 1),),
                upper_shapes=(Rect(-2, -2, 2, 2),),
            ),
        ),
        rules=(
            MinimumWidthRule("lower-width", "lower", 2),
            MinimumSpacingRule("lower-spacing", "lower", 2),
            MinimumWidthRule("upper-width", "upper", 2),
            MinimumSpacingRule("upper-spacing", "upper", 2),
            EnclosureRule("lower-cut-enclosure", "lower", "cut", 1, 1),
            EnclosureRule("upper-cut-enclosure", "upper", "cut", 1, 1),
            CutSpacingRule("cut-spacing", "cut", 2, 2),
        ),
    )


def _dense_multilayer_job() -> PhysicalDesignJob:
    wall = PhysicalMaster(
        "lower-wall",
        10,
        40,
        obstructions=(LayerShape("lower", Rect(0, 0, 10, 40)),),
        allowed_orientations=(Orientation.R0,),
    )
    pillar = PhysicalMaster(
        "upper-pillar",
        6,
        12,
        obstructions=(LayerShape("upper", Rect(0, 0, 6, 12)),),
        allowed_orientations=(Orientation.R0,),
    )
    technology = _two_layer_technology(
        resources=(
            GridlessRoutingResource("lower-domain", "lower"),
            GridlessRoutingResource("upper-domain", "upper"),
        )
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "dense-multilayer-channel",
            Rect(0, 0, 60, 40),
            (wall, pillar),
            (
                PhysicalInstance("wall", wall.name, Placement(Point(25, 0))),
                PhysicalInstance("pillar-left", pillar.name, Placement(Point(12, 0))),
                PhysicalInstance("pillar-mid", pillar.name, Placement(Point(37, 20))),
                PhysicalInstance("pillar-right", pillar.name, Placement(Point(48, 0))),
            ),
            ports=(
                PhysicalPort("source", (PinAccess("lower", Rect(2, 5, 4, 7)),)),
                PhysicalPort("sink", (PinAccess("lower", Rect(56, 5, 58, 7)),)),
            ),
            nets=(
                PhysicalNet(
                    "crossing",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(maximum_route_states=600_000),
    )


def _explicit_track_job() -> PhysicalDesignJob:
    technology = _two_layer_technology(
        resources=(
            RoutingTrackPattern("lower-tracks", "lower", Axis.Y, 2, 4, 10),
            RoutingTrackPattern("upper-tracks", "upper", Axis.X, 3, 4, 10),
        )
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "orthogonal-track-bottleneck",
            Rect(0, 0, 40, 40),
            (),
            (),
            ports=(
                PhysicalPort("a-source", (PinAccess("lower", Rect(2, 5, 4, 7)),)),
                PhysicalPort("a-sink", (PinAccess("upper", Rect(34, 29, 36, 31)),)),
            ),
            nets=(
                PhysicalNet(
                    "track-a",
                    (PinReference("a-source"), PinReference("a-sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        routing_constraints=(
            RoutingViaCountConstraint("track-a-via", "track-a", 1),
        ),
        execution_policy=PnrExecutionPolicy(
            routing_congestion_bins_x=4,
            routing_congestion_bins_y=4,
        ),
    )


def _multi_net_group_job() -> PhysicalDesignJob:
    technology = PhysicalTechnology(
        "benchmark-group-gridless",
        1000,
        1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "three-net-closure-group",
            Rect(0, 0, 60, 40),
            (),
            (),
            ports=(
                PhysicalPort("a-source", (PinAccess("route", Rect(2, 5, 4, 7)),)),
                PhysicalPort("a-sink", (PinAccess("route", Rect(56, 5, 58, 7)),)),
                PhysicalPort("b-source", (PinAccess("route", Rect(2, 19, 4, 21)),)),
                PhysicalPort("b-sink", (PinAccess("route", Rect(56, 19, 58, 21)),)),
                PhysicalPort("c-source", (PinAccess("route", Rect(2, 29, 4, 31)),)),
                PhysicalPort("c-sink", (PinAccess("route", Rect(44, 29, 46, 31)),)),
            ),
            nets=(
                PhysicalNet("signal", (PinReference("a-source"), PinReference("a-sink"))),
                PhysicalNet("shield", (PinReference("b-source"), PinReference("b-sink"))),
                PhysicalNet("matched", (PinReference("c-source"), PinReference("c-sink"))),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        routing_constraints=(
            RoutingShieldConstraint("signal-shield", "signal", "shield", 2),
            RoutingSkewConstraint("signal-match", ("signal", "matched"), 0),
        ),
    )


def _infeasible_wall_job() -> PhysicalDesignJob:
    job = _dense_multilayer_job()
    wall = job.design.masters[0]
    return replace(
        job,
        technology=PhysicalTechnology(
            "benchmark-blocked-gridless",
            1000,
            1,
            layers=(
                PhysicalLayer("lower", LayerKind.ROUTING, RoutingDirection.ANY),
            ),
            routing_resources=(GridlessRoutingResource("lower-domain", "lower"),),
            rules=(
                MinimumWidthRule("lower-width", "lower", 2),
                MinimumSpacingRule("lower-spacing", "lower", 2),
            ),
        ),
        design=replace(
            job.design,
            masters=(wall,),
            instances=(job.design.instances[0],),
        ),
    )


def _iteration_exhausted_job() -> PhysicalDesignJob:
    route_technology = PhysicalTechnology(
        "benchmark-negotiation-gridless",
        1000,
        1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )


    side_wall = PhysicalMaster(
        "side-wall",
        3,
        20,
        obstructions=(LayerShape("route", Rect(0, 0, 3, 20)),),
        allowed_orientations=(Orientation.R0,),
    )
    top_wall = PhysicalMaster(
        "top-wall",
        12,
        3,
        obstructions=(LayerShape("route", Rect(0, 0, 12, 3)),),
        allowed_orientations=(Orientation.R0,),
    )
    return PhysicalDesignJob(
        route_technology,
        PhysicalDesign(
            "iteration-limited-pocket",
            Rect(0, 0, 40, 40),
            (side_wall, top_wall),
            (
                PhysicalInstance("left-wall", side_wall.name, Placement(Point(15, 10))),
                PhysicalInstance("right-wall", side_wall.name, Placement(Point(30, 10))),
                PhysicalInstance("top-wall", top_wall.name, Placement(Point(18, 27))),
            ),
            ports=(
                PhysicalPort("a-left", (PinAccess("route", Rect(2, 5, 4, 7)),)),
                PhysicalPort("a-right", (PinAccess("route", Rect(36, 5, 38, 7)),)),
                PhysicalPort("z-inside", (PinAccess("route", Rect(23, 19, 25, 21)),)),
                PhysicalPort("z-outside", (PinAccess("route", Rect(23, 1, 25, 3)),)),
            ),
            nets=(
                PhysicalNet("a-flexible", (PinReference("a-left"), PinReference("a-right"))),
                PhysicalNet("z-critical", (PinReference("z-inside"), PinReference("z-outside"))),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(maximum_routing_iterations=1),
    )


def _capacity_negotiation_job(
    *, maximum_routing_iterations: int = 8
) -> PhysicalDesignJob:
    technology = PhysicalTechnology(
        "capacity-benchmark",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),
        ),
        routing_resources=(
            GridlessRoutingResource("route-domain", "route"),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "capacity-negotiation",
            Rect(0, 0, 8, 10),
            (),
            (),
            ports=(
                PhysicalPort("a-source", (PinAccess("route", Rect(1, 0, 3, 2)),)),
                PhysicalPort("a-sink", (PinAccess("route", Rect(3, 0, 5, 2)),)),
                PhysicalPort("b-source", (PinAccess("route", Rect(1, 3, 3, 5)),)),
                PhysicalPort("b-sink", (PinAccess("route", Rect(3, 3, 5, 5)),)),
            ),
            nets=(
                PhysicalNet(
                    "a-direct",
                    (PinReference("a-source"), PinReference("a-sink")),
                ),
                PhysicalNet(
                    "b-negotiated",
                    (PinReference("b-source"), PinReference("b-sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(
            maximum_routing_iterations=maximum_routing_iterations,
            routing_congestion_bins_x=1,
            routing_congestion_bins_y=2,
        ),
    )


@pytest.mark.parametrize(
    "job",
    (_dense_multilayer_job(), _explicit_track_job(), _multi_net_group_job()),
    ids=("dense-gridless", "explicit-track", "multi-net-group"),
)
def test_general_routing_benchmarks_close_deterministically(
    job: PhysicalDesignJob,
) -> None:
    first = run(job)
    second = run(job)

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert len(first.routes) == len(job.design.nets)
    assert all(
        outcome.status is ConstraintStatus.SATISFIED
        for outcome in first.constraint_outcomes
    )
    assert first.provenance.deterministic


def test_dense_gridless_benchmark_uses_both_layers_around_obstructions() -> None:
    result = run(_dense_multilayer_job())

    assert result.status is ResultStatus.SUCCEEDED
    assert {segment.layer for segment in result.routes[0].segments} == {
        "lower",
        "upper",
    }
    assert len(result.routes[0].vias) == 2


def test_explicit_track_benchmark_closes_with_finite_via_policy() -> None:
    result = run(_explicit_track_job())

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(len(route.vias) for route in result.routes) == (1,)
    assert all(
        segment.start.y == segment.end.y
        for route in result.routes
        for segment in route.segments
        if segment.layer == "lower"
    )
    assert all(
        segment.start.x == segment.end.x
        for route in result.routes
        for segment in route.segments
        if segment.layer == "upper"
    )


def test_benchmark_corpus_distinguishes_infeasible_and_budget_exhausted() -> None:
    infeasible = run(_infeasible_wall_job())
    exhausted = run(_iteration_exhausted_job())

    assert infeasible.status is ResultStatus.FAILED
    assert infeasible.stage_reports[-1].diagnostics[0].code == "routing_infeasible"
    assert exhausted.status is ResultStatus.EXHAUSTED
    assert exhausted.stage_reports[-1].diagnostics[0].code == (
        "routing_iteration_exhausted"
    )
    assert tuple(route.net for route in exhausted.routes) == ("a-flexible",)


def test_gridless_capacity_bottleneck_closes_through_historical_cost() -> None:
    first = run(_capacity_negotiation_job())
    second = run(_capacity_negotiation_job())

    routing_metrics = {
        metric.name: metric.value for metric in first.stage_reports[-1].metrics
    }
    negotiated = next(route for route in first.routes if route.net == "b-negotiated")

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert routing_metrics["routing_iterations"] == 2
    assert routing_metrics["routing_ripped_net_count"] == 1
    assert routing_metrics["routing_total_overflow"] == 0
    assert any(
        segment.start.y >= 5 and segment.end.y >= 5
        for segment in negotiated.segments
        if segment.start.y == segment.end.y
    )


def test_capacity_iteration_exhaustion_returns_maximum_legal_partial_route() -> None:
    result = run(_capacity_negotiation_job(maximum_routing_iterations=1))

    assert result.status is ResultStatus.EXHAUSTED
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_iteration_exhausted"
    )
    assert tuple(route.net for route in result.routes) == ("a-direct",)


def test_capacity_benchmark_has_typed_closed_and_iteration_evidence() -> None:
    closed_job = _capacity_negotiation_job()
    closed_placement = solve_placement(closed_job)
    closed = solve_routing(closed_job, closed_placement.placements)
    exhausted_job = _capacity_negotiation_job(maximum_routing_iterations=1)
    exhausted_placement = solve_placement(exhausted_job)
    exhausted = solve_routing(exhausted_job, exhausted_placement.placements)

    assert closed.termination.reason is RoutingTerminationReason.CLOSED
    assert closed.termination.conflict_identities == ()
    assert exhausted.termination.reason is RoutingTerminationReason.ITERATION_BUDGET
    assert (
        exhausted.conflicts.conflicts[0].kind
        is RoutingConflictKind.CAPACITY_OVERFLOW
    )
    assert exhausted.termination.conflict_identities == tuple(
        conflict.identity for conflict in exhausted.conflicts.conflicts
    )
