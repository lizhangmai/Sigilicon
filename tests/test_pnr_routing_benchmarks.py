from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.layout.pnr import (
    Axis,
    ConstraintStatus,
    CutSpacingRule,
    EnclosureRule,
    FenceConstraint,
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
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    RoutingDirection,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
    RoutingRegionConstraint,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingTrackPattern,
    RoutingViaCountConstraint,
    ViaDefinition,
    run,
)
from sigilicon.layout.pnr._placement import solve_placement
from sigilicon.layout.pnr._placement_repair import (
    PlacementRepairStatus,
    compile_placement_repair_problem,
)
from sigilicon.layout.pnr._closure import (
    PlacementRoutingTerminationReason,
    close_placement_routing,
)
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
            RoutingLengthConstraint("signal-length", "signal", 54, 54),
            RoutingRegionConstraint(
                "signal-required-channel",
                "signal",
                (LayerShape("route", Rect(29, 5, 31, 7)),),
            ),
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


def _state_exhausted_job() -> PhysicalDesignJob:
    job = _dense_multilayer_job()
    return replace(
        job,
        execution_policy=replace(job.execution_policy, maximum_route_states=1),
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


def _branch_repair_job() -> PhysicalDesignJob:
    technology = _two_layer_technology(
        resources=(
            GridlessRoutingResource("lower-bottom", "lower", Rect(0, 0, 20, 4)),
            GridlessRoutingResource("lower-vertical", "lower", Rect(8, 0, 12, 16)),
            GridlessRoutingResource("lower-crossing", "lower", Rect(0, 5, 20, 9)),
            GridlessRoutingResource("upper-domain", "upper"),
        )
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "local-branch-repair",
            Rect(0, 0, 20, 20),
            (),
            (),
            ports=(
                PhysicalPort("tree-left", (PinAccess("lower", Rect(1, 1, 3, 3)),)),
                PhysicalPort("tree-right", (PinAccess("lower", Rect(17, 1, 19, 3)),)),
                PhysicalPort("tree-leaf", (PinAccess("lower", Rect(9, 13, 11, 15)),)),
                PhysicalPort("cross-left", (PinAccess("lower", Rect(1, 6, 3, 8)),)),
                PhysicalPort("cross-right", (PinAccess("lower", Rect(17, 6, 19, 8)),)),
            ),
            nets=(
                PhysicalNet(
                    "a-tree",
                    (
                        PinReference("tree-left"),
                        PinReference("tree-right"),
                        PinReference("tree-leaf"),
                    ),
                ),
                PhysicalNet(
                    "z-cross",
                    (PinReference("cross-left"), PinReference("cross-right")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        routing_constraints=(
            RoutingLayerConstraint("cross-lower-only", "z-cross", ("lower",)),
        ),
        execution_policy=PnrExecutionPolicy(
            maximum_route_states=300_000,
            maximum_routing_iterations=6,
            routing_congestion_bins_x=2,
            routing_congestion_bins_y=2,
        ),
    )


def _placement_repair_job(
    *, maximum_placement_repair_iterations: int = 2
) -> PhysicalDesignJob:
    master = PhysicalMaster(
        "movable-terminal",
        2,
        2,
        pins=(
            MasterPin(
                "signal",
                (PinAccess("route", Rect(0, 0, 2, 2)),),
            ),
        ),
        allowed_orientations=(Orientation.R0,),
    )
    technology = PhysicalTechnology(
        "placement-repair-tracks",
        1000,
        1,
        layers=(
            PhysicalLayer(
                "route", LayerKind.ROUTING, RoutingDirection.HORIZONTAL
            ),
        ),
        routing_resources=(
            RoutingTrackPattern("horizontal-track", "route", Axis.Y, 2, 4, 1),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "placement-routing-repair",
            Rect(0, 0, 20, 8),
            (master,),
            (PhysicalInstance("driver", master.name),),
            ports=(
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("signal", "driver"), PinReference("sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(
            maximum_placement_repair_iterations=(
                maximum_placement_repair_iterations
            ),
        ),
    )


def _physical_blocker_job(
    *,
    fixed: bool = False,
    two_blockers: bool = False,
    maximum_placement_repair_iterations: int = 4,
) -> PhysicalDesignJob:
    blocker = PhysicalMaster(
        "routing-blocker",
        2,
        2,
        obstructions=(LayerShape("route", Rect(0, 0, 2, 2)),),
        allowed_orientations=(Orientation.R0,),
    )
    spectator = PhysicalMaster(
        "unrelated-spectator",
        2,
        2,
        allowed_orientations=(Orientation.R0,),
    )
    blocker_names = (
        ("blocker-a", "blocker-b")
        if two_blockers
        else ("blocker-a",)
    )
    fixed_locations = {
        "blocker-a": Placement(Point(7, 0)),
        "blocker-b": Placement(Point(10, 0)),
    }
    instances = tuple(
        PhysicalInstance(
            name,
            blocker.name,
            fixed_locations[name] if fixed else None,
        )
        for name in blocker_names
    ) + (PhysicalInstance("spectator", spectator.name),)
    constraints = (
        ()
        if fixed
        else tuple(
            FenceConstraint(
                f"{name}-local-region",
                (name,),
                Rect(
                    fixed_locations[name].origin.x,
                    0,
                    fixed_locations[name].origin.x + 2,
                    8,
                ),
            )
            for name in blocker_names
        )
    ) + (
        FenceConstraint(
            "spectator-local-region",
            ("spectator",),
            Rect(0, 4, 4, 8),
        ),
    )
    technology = PhysicalTechnology(
        "physical-blocker-track",
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
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "physical-blocker-closure",
            Rect(0, 0, 20, 8),
            (blocker, spectator),
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
        constraints=constraints,
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(
            maximum_placement_repair_iterations=(
                maximum_placement_repair_iterations
            ),
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
    state_exhausted = run(_state_exhausted_job())
    exhausted = run(_iteration_exhausted_job())

    assert infeasible.status is ResultStatus.FAILED
    assert infeasible.stage_reports[-1].diagnostics[0].code == "routing_infeasible"
    assert state_exhausted.status is ResultStatus.EXHAUSTED
    assert state_exhausted.stage_reports[-1].diagnostics[0].code == (
        "routing_search_exhausted"
    )
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
    assert all(
        site.resource is not None and site.region is not None
        for site in exhausted.placement_pressure.sites
    )
    assert (
        exhausted.conflicts.conflicts[0].kind
        is RoutingConflictKind.CAPACITY_OVERFLOW
    )
    assert exhausted.termination.conflict_identities == tuple(
        conflict.identity for conflict in exhausted.conflicts.conflicts
    )


def test_state_budget_benchmark_has_typed_termination_evidence() -> None:
    job = _state_exhausted_job()
    placement = solve_placement(job)
    routing = solve_routing(job, placement.placements)

    assert routing.termination.reason is RoutingTerminationReason.STATE_BUDGET


def test_multi_terminal_conflict_repairs_only_the_attributed_leaf_branch() -> None:
    first = run(_branch_repair_job())
    second = run(_branch_repair_job())
    metrics = {
        metric.name: metric.value for metric in first.stage_reports[-1].metrics
    }
    tree_route = next(route for route in first.routes if route.net == "a-tree")

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert metrics["routing_iterations"] == 2
    assert metrics["routing_ripped_net_count"] == 0
    assert metrics["routing_ripped_branch_count"] == 1
    assert {segment.layer for segment in tree_route.segments} == {
        "lower",
        "upper",
    }
    assert len(tree_route.vias) == 2


def test_multi_terminal_length_policy_expands_branch_conflict_to_net_scope() -> None:
    job = _branch_repair_job()
    result = run(
        replace(
            job,
            routing_constraints=job.routing_constraints
            + (
                RoutingLengthConstraint(
                    "tree-length-window",
                    "a-tree",
                    maximum_length_dbu=100,
                ),
            ),
        )
    )
    metrics = {
        metric.name: metric.value for metric in result.stage_reports[-1].metrics
    }

    assert result.status is ResultStatus.SUCCEEDED
    assert metrics["routing_ripped_branch_count"] == 0
    assert metrics["routing_ripped_net_count"] >= 1


def test_placement_routing_outer_loop_repairs_only_attributed_instance() -> None:
    first = run(_placement_repair_job())
    second = run(_placement_repair_job())
    placement_metrics = {
        metric.name: metric.value for metric in first.stage_reports[0].metrics
    }

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert first.placements == (
        InstancePlacement("driver", Placement(Point(0, 1))),
    )
    assert placement_metrics["placement_repair_iterations"] == 1
    assert placement_metrics["placement_repair_accepted_count"] == 1
    assert placement_metrics["placement_repair_displacement"] == 1


def test_repair_problem_ranks_pin_access_and_hides_rejection_history() -> None:
    job = _placement_repair_job()
    placement = solve_placement(job)
    routing = solve_routing(job, placement.placements)
    problem = compile_placement_repair_problem(
        job,
        placement.placements,
        routing.placement_pressure,
    )

    first = problem.next_candidate()
    repeated = problem.next_candidate()
    assert first == repeated
    assert first.status is PlacementRepairStatus.REPAIRED
    assert first.moved_instance == "driver"
    assert first.placements == (
        InstancePlacement("driver", Placement(Point(0, 1))),
    )
    assert first.prediction is not None
    assert first.prediction.pin_access_gain == 1
    assert first.prediction.pin_access_loss == 0

    next_problem = compile_placement_repair_problem(
        job,
        placement.placements,
        routing.placement_pressure,
        rejected=frozenset((first.identity,)),
    )
    second = next_problem.next_candidate()
    assert second.status is PlacementRepairStatus.REPAIRED
    assert second.identity != first.identity


def test_repair_problem_has_an_independent_candidate_state_budget() -> None:
    job = _placement_repair_job()
    budgeted = replace(
        job,
        execution_policy=replace(
            job.execution_policy,
            maximum_placement_repair_states=1,
        ),
    )
    placement = solve_placement(budgeted)
    routing = solve_routing(budgeted, placement.placements)

    repair = compile_placement_repair_problem(
        budgeted,
        placement.placements,
        routing.placement_pressure,
    ).next_candidate()

    assert repair.status is PlacementRepairStatus.STATE_BUDGET
    assert repair.search_states == 1


def test_one_ranked_placement_repair_closes_with_one_iteration() -> None:
    job = _placement_repair_job(maximum_placement_repair_iterations=1)
    first = run(job)
    second = run(job)

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert first.placements == (
        InstancePlacement("driver", Placement(Point(0, 1))),
    )


def test_routing_pressure_and_outer_termination_are_typed() -> None:
    job = _placement_repair_job(maximum_placement_repair_iterations=1)
    placement = solve_placement(job)
    routing = solve_routing(job, placement.placements)
    closure = close_placement_routing(job, placement)

    assert routing.placement_pressure.movable_instances == ("driver",)
    assert routing.placement_pressure.sites[0].pin == "signal"
    assert closure.termination is PlacementRoutingTerminationReason.CLOSED
    assert closure.repairs[0].predicted_pin_access_gain == 1
    assert closure.repairs[0].predicted_pin_access_loss == 0


def test_unrelated_movable_blocker_is_the_only_repaired_owner() -> None:
    job = _physical_blocker_job()
    first = run(job)
    second = run(job)
    placements = {
        item.instance: item.placement
        for item in first.placements
    }

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert placements["blocker-a"] == Placement(Point(7, 5))
    assert placements["spectator"] == Placement(Point(0, 4))

    initial = solve_placement(job)
    initial_routing = solve_routing(job, initial.placements)
    closure = close_placement_routing(job, initial)
    assert initial_routing.placement_pressure.movable_instances == (
        "blocker-a",
    )
    assert tuple(
        owner.stable_name
        for owner in closure.repairs[0].attributed_owners
    ) == ("instance:blocker-a",)
    assert closure.repairs[0].predicted_released_resources


def test_fixed_blocker_does_not_move_an_unrelated_movable_instance() -> None:
    job = _physical_blocker_job(fixed=True)
    initial = solve_placement(job)
    first = run(job)
    second = run(job)

    assert first == second
    assert first.status is ResultStatus.FAILED
    assert first.placements == initial.placements
    assert first.stage_reports[-1].diagnostics[0].code == "routing_infeasible"


def test_fixed_pressure_source_has_no_legal_placement_repair() -> None:
    job = _placement_repair_job()
    fixed_design = replace(
        job.design,
        instances=(
            replace(
                job.design.instances[0],
                fixed_placement=Placement(Point(0, 0)),
            ),
        ),
    )
    result = run(replace(job, design=fixed_design))

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.placements == (
        InstancePlacement("driver", Placement(Point(0, 0))),
    )
