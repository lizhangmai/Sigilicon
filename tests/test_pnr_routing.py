from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.experimental.reference_pnr._routing_check import check_routing_solution
from sigilicon.experimental.reference_pnr._routing_constraints import evaluate_routing_constraints
from sigilicon.experimental.reference_pnr._routing_problem import compile_routing_problem
from sigilicon.experimental.reference_pnr import (
    ConstraintStatus,
    CutSpacingRule,
    EnclosureRule,
    GridlessRoutingResource,
    InstancePlacement,
    LayerKind,
    LayerShape,
    MasterPin,
    MinimumSpacingRule,
    MinimumWidthRule,
    Orientation,
    PhysicalDesign,
    ReferencePnrJob,
    PhysicalInstance,
    PhysicalLayer,
    PhysicalMaster,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PnrInputError,
    ReferencePnrExecutionPolicy,
    PhysicalDesignRequest,
    PhysicalDesignStage,
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
    Axis,
    run,
)


def _technology() -> PhysicalTechnology:
    return PhysicalTechnology(
        "neutral-gridless",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),
        ),
        routing_resources=(
            GridlessRoutingResource("route-region", "route"),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )


def _job(
    *,
    technology: PhysicalTechnology | None = None,
    obstruction: bool = False,
    maximum_route_states: int = 200_000,
) -> ReferencePnrJob:
    masters: tuple[PhysicalMaster, ...] = ()
    instances: tuple[PhysicalInstance, ...] = ()
    if obstruction:
        master = PhysicalMaster(
            "wall",
            4,
            12,
            obstructions=(LayerShape("route", Rect(0, 0, 4, 12)),),
            allowed_orientations=(Orientation.R0,),
        )
        masters = (master,)
        instances = (
            PhysicalInstance("wall", master.name, Placement(Point(18, 0))),
        )
    return ReferencePnrJob(
        technology or _technology(),
        PhysicalDesign(
            "route-two-ports",
            Rect(0, 0, 40, 40),
            masters,
            instances,
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(2, 5, 4, 7)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(36, 5, 38, 7)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PhysicalDesignRequest(
            stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING),
        ),
        execution_policy=ReferencePnrExecutionPolicy(
            maximum_route_states=maximum_route_states,
        ),
    )


def _multilayer_job(*, three_layers: bool = False) -> ReferencePnrJob:
    layers = [
        PhysicalLayer("m1", LayerKind.ROUTING, RoutingDirection.ANY),
        PhysicalLayer("v1", LayerKind.CUT),
        PhysicalLayer("m2", LayerKind.ROUTING, RoutingDirection.ANY),
    ]
    resources = [
        GridlessRoutingResource("m1-region", "m1"),
        GridlessRoutingResource("m2-region", "m2"),
    ]
    vias = [
        ViaDefinition(
            "via12",
            "m1",
            "v1",
            "m2",
            lower_shapes=(Rect(-2, -2, 2, 2),),
            cut_shapes=(Rect(-1, -1, 1, 1),),
            upper_shapes=(Rect(-2, -2, 2, 2),),
        ),
    ]
    rules = [
        MinimumWidthRule("m1-width", "m1", 2),
        MinimumSpacingRule("m1-spacing", "m1", 2),
        MinimumWidthRule("m2-width", "m2", 2),
        MinimumSpacingRule("m2-spacing", "m2", 2),
        EnclosureRule("m1-v1-enclosure", "m1", "v1", 1, 1),
        EnclosureRule("m2-v1-enclosure", "m2", "v1", 1, 1),
        CutSpacingRule("v1-spacing", "v1", 2, 2),
    ]
    sink_layer = "m2"
    if three_layers:
        layers.extend(
            (
                PhysicalLayer("v2", LayerKind.CUT),
                PhysicalLayer("m3", LayerKind.ROUTING, RoutingDirection.ANY),
            )
        )
        resources.append(GridlessRoutingResource("m3-region", "m3"))
        vias.append(
            ViaDefinition(
                "via23",
                "m2",
                "v2",
                "m3",
                lower_shapes=(Rect(-2, -2, 2, 2),),
                cut_shapes=(Rect(-1, -1, 1, 1),),
                upper_shapes=(Rect(-2, -2, 2, 2),),
            )
        )
        rules.extend(
            (
                MinimumWidthRule("m3-width", "m3", 2),
                MinimumSpacingRule("m3-spacing", "m3", 2),
                EnclosureRule("m2-v2-enclosure", "m2", "v2", 1, 1),
                EnclosureRule("m3-v2-enclosure", "m3", "v2", 1, 1),
                CutSpacingRule("v2-spacing", "v2", 2, 2),
            )
        )
        sink_layer = "m3"
    technology = PhysicalTechnology(
        "neutral-multilayer",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=tuple(layers),
        routing_resources=tuple(resources),
        via_definitions=tuple(vias),
        rules=tuple(rules),
    )
    return ReferencePnrJob(
        technology,
        PhysicalDesign(
            "route-across-layers",
            Rect(0, 0, 40, 40),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("m1", Rect(2, 5, 4, 7)),)),
                PhysicalPort(
                    "sink",
                    (PinAccess(sink_layer, Rect(36, 5, 38, 7)),),
                ),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
    )


def _ripup_job(*, maximum_routing_iterations: int = 8) -> ReferencePnrJob:
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
    return ReferencePnrJob(
        _technology(),
        PhysicalDesign(
            "order-dependent-pocket",
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
                PhysicalNet(
                    "a-flexible",
                    (PinReference("a-left"), PinReference("a-right")),
                ),
                PhysicalNet(
                    "z-critical",
                    (PinReference("z-inside"), PinReference("z-outside")),
                ),
            ),
        ),
        request=PhysicalDesignRequest(
            stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING),
        ),
        execution_policy=ReferencePnrExecutionPolicy(
            maximum_routing_iterations=maximum_routing_iterations,
        ),
    )


def _wire_length(job: ReferencePnrJob) -> int:
    result = run(job)
    assert result.status is ResultStatus.SUCCEEDED
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in result.routes
        for segment in route.segments
    )


def _parallel_net_job(*, congestion_bins_y: int) -> ReferencePnrJob:
    job = _job()
    design = replace(
        job.design,
        ports=job.design.ports
        + (
            PhysicalPort("source-b", (PinAccess("route", Rect(2, 9, 4, 11)),)),
            PhysicalPort("sink-b", (PinAccess("route", Rect(36, 9, 38, 11)),)),
        ),
        nets=job.design.nets
        + (
            PhysicalNet(
                "signal-b",
                (PinReference("source-b"), PinReference("sink-b")),
            ),
        ),
    )
    return replace(
        job,
        design=design,
        execution_policy=replace(
            job.execution_policy,
            routing_congestion_bins_x=4,
            routing_congestion_bins_y=congestion_bins_y,
        ),
    )


def test_gridless_router_returns_exact_deterministic_manhattan_geometry() -> None:
    job = _job()

    first = run(job)
    second = run(job)

    assert first == second
    assert first.status is ResultStatus.SUCCEEDED
    assert len(first.routes) == 1
    assert first.routes[0].net == "signal"
    assert first.routes[0].vias == ()
    assert all(
        segment.start.x == segment.end.x or segment.start.y == segment.end.y
        for segment in first.routes[0].segments
    )
    assert _wire_length(job) == 34
    routing_report = first.stage_reports[-1]
    assert routing_report.stage is PhysicalDesignStage.ROUTING
    assert routing_report.status is ResultStatus.SUCCEEDED
    assert {metric.name: metric.value for metric in routing_report.metrics}[
        "routed_net_count"
    ] == 1


def test_routing_problem_compiles_transformed_static_design_facts() -> None:
    master = PhysicalMaster(
        "endpoint",
        4,
        4,
        pins=(
            MasterPin(
                "pin",
                (PinAccess("route", Rect(0, 0, 2, 2)),),
            ),
        ),
        obstructions=(LayerShape("route", Rect(2, 0, 4, 4)),),
        allowed_orientations=(Orientation.R0,),
    )
    placement = Placement(Point(10, 12))
    job = ReferencePnrJob(
        _technology(),
        PhysicalDesign(
            "compiled-route-problem",
            Rect(0, 0, 40, 40),
            (master,),
            (PhysicalInstance("sink", master.name, placement),),
            ports=(
                PhysicalPort(
                    "source",
                    (PinAccess("route", Rect(2, 5, 4, 7)),),
                ),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (
                        PinReference("source"),
                        PinReference("pin", "sink"),
                    ),
                ),
            ),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
    )

    problem = compile_routing_problem(
        job,
        (InstancePlacement("sink", placement),),
    )

    assert problem.issue is None
    assert problem.net_names == ("signal",)
    assert problem.domain.rules_for("route") == (2, 2)
    assert problem.layers["route"].regions == (Rect(1, 1, 39, 39),)
    net = problem.net("signal")
    assert net.terminal_accesses == (
        (LayerShape("route", Rect(2, 5, 4, 7)),),
        (LayerShape("route", Rect(10, 12, 12, 14)),),
    )
    assert tuple(
        (region.shape, tuple(owner.stable_name for owner in region.owners))
        for region in net.static_blockers["route"]
    ) == ((Rect(12, 12, 14, 16), ("instance:sink",)),)
    assert tuple(
        owner.identity.stable_name
        for owner in problem.physical_ownership.owners
    ) == (
        "instance:sink",
        "pin:sink:pin",
        "port:source",
    )
    pin_owner = problem.physical_ownership.owner_for_reference(
        PinReference("pin", "sink")
    )
    assert tuple(
        region.source
        for region in problem.physical_ownership.regions_for_owner(
            pin_owner.identity
        )
    ) == ("pin-access",)


def test_adjacent_gridless_regions_form_one_exact_routing_domain() -> None:
    job = _job()
    technology = replace(
        job.technology,
        routing_resources=(
            GridlessRoutingResource("left", "route", Rect(0, 0, 20, 40)),
            GridlessRoutingResource("right", "route", Rect(20, 0, 40, 40)),
        ),
    )

    result = run(replace(job, technology=technology))

    assert result.status is ResultStatus.SUCCEEDED
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in result.routes[0].segments
    ) == 34


def test_gridless_router_detours_around_transformed_master_obstruction() -> None:
    direct_length = _wire_length(_job())
    obstructed = run(_job(obstruction=True))

    assert obstructed.status is ResultStatus.SUCCEEDED
    route = obstructed.routes[0]
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in route.segments
    ) > direct_length
    assert any(segment.start.y > 12 or segment.end.y > 12 for segment in route.segments)


def test_exact_minimum_spacing_between_routed_nets_is_legal() -> None:
    result = run(_parallel_net_job(congestion_bins_y=1))

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(len(route.segments) for route in result.routes) == (1, 1)
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in result.routes
        for segment in route.segments
    ) == 68
    metrics = {metric.name: metric.value for metric in result.stage_reports[-1].metrics}
    assert metrics["routing_peak_horizontal_demand"] == 2
    assert metrics["routing_peak_vertical_demand"] == 0
    assert metrics["routing_congested_bin_count"] == 0
    assert metrics["routing_total_overflow"] == 0


def test_congestion_demand_guides_later_net_into_a_less_used_bin() -> None:
    independent_bins = run(_parallel_net_job(congestion_bins_y=8))
    shared_bins = run(_parallel_net_job(congestion_bins_y=2))

    independent_second_length = sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in independent_bins.routes[1].segments
    )
    guided_second_length = sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in shared_bins.routes[1].segments
    )
    guided_metrics = {
        metric.name: metric.value for metric in shared_bins.stage_reports[-1].metrics
    }

    assert independent_second_length == 34
    assert guided_second_length == 54
    assert guided_metrics["routing_peak_horizontal_demand"] == 1


def test_skew_constraint_prioritizes_matched_shortest_routes_over_bin_spreading() -> None:
    job = _parallel_net_job(congestion_bins_y=2)
    result = run(
        replace(
            job,
            routing_constraints=(
                RoutingSkewConstraint(
                    "matched-pair",
                    ("signal", "signal-b"),
                    0,
                ),
            ),
        )
    )

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(
        sum(
            abs(segment.end.x - segment.start.x)
            + abs(segment.end.y - segment.start.y)
            for segment in route.segments
        )
        for route in result.routes
    ) == (34, 34)
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED


def test_skew_constraint_actively_compensates_the_shorter_group_route() -> None:
    job = _parallel_net_job(congestion_bins_y=8)
    ports = tuple(
        (
            PhysicalPort("sink-b", (PinAccess("route", Rect(24, 9, 26, 11)),))
            if port.name == "sink-b"
            else port
        )
        for port in job.design.ports
    )
    result = run(
        replace(
            job,
            design=replace(job.design, ports=ports),
            routing_constraints=(
                RoutingSkewConstraint(
                    "matched-pair",
                    ("signal", "signal-b"),
                    0,
                ),
            ),
        )
    )

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(
        sum(
            abs(segment.end.x - segment.start.x)
            + abs(segment.end.y - segment.start.y)
            for segment in route.segments
        )
        for route in result.routes
    ) == (34, 34)
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    metrics = {metric.name: metric.value for metric in result.stage_reports[-1].metrics}
    assert metrics["routing_iterations"] == 2
    assert metrics["routing_ripped_net_count"] == 2


def test_skew_constraint_reports_unreachable_matching_parity_during_search() -> None:
    job = _parallel_net_job(congestion_bins_y=8)
    ports = tuple(
        (
            PhysicalPort("sink-b", (PinAccess("route", Rect(19, 9, 21, 11)),))
            if port.name == "sink-b"
            else port
        )
        for port in job.design.ports
    )
    result = run(
        replace(
            job,
            design=replace(job.design, ports=ports),
            routing_constraints=(
                RoutingSkewConstraint(
                    "matched-pair",
                    ("signal", "signal-b"),
                    0,
                ),
            ),
        )
    )

    assert result.status is ResultStatus.FAILED
    assert tuple(route.net for route in result.routes) == ("signal",)
    assert result.constraint_outcomes[-1].status is ConstraintStatus.NOT_EVALUATED
    assert result.stage_reports[-1].diagnostics[-1].code == (
        "routing_length_window_infeasible"
    )


def test_shield_constraint_accepts_continuous_parallel_coverage() -> None:
    job = _parallel_net_job(congestion_bins_y=2)
    result = run(
        replace(
            job,
            routing_constraints=(
                RoutingShieldConstraint(
                    "signal-shield",
                    "signal",
                    "signal-b",
                    maximum_spacing_dbu=2,
                    layers=("route",),
                ),
            ),
        )
    )

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert tuple(len(route.segments) for route in result.routes) == (1, 1)

    shield = result.routes[1]
    segment = shield.segments[0]
    split_shield = replace(
        shield,
        segments=(
            replace(segment, end=Point(20, segment.end.y)),
            replace(segment, start=Point(20, segment.start.y)),
        ),
    )
    split_outcome = evaluate_routing_constraints(
        replace(
            job,
            routing_constraints=(
                RoutingShieldConstraint(
                    "signal-shield",
                    "signal",
                    "signal-b",
                    maximum_spacing_dbu=2,
                ),
            ),
        ),
        (result.routes[0], split_shield),
    )
    assert split_outcome[0].status is ConstraintStatus.SATISFIED


def test_shield_constraint_actively_routes_from_remote_shield_terminals() -> None:
    job = _parallel_net_job(congestion_bins_y=8)
    ports = tuple(
        (
            PhysicalPort(
                port.name,
                (
                    PinAccess(
                        "route",
                        (
                            Rect(2, 19, 4, 21)
                            if port.name == "source-b"
                            else Rect(36, 19, 38, 21)
                        ),
                    ),
                ),
            )
            if port.name in ("source-b", "sink-b")
            else port
        )
        for port in job.design.ports
    )
    unconstrained_job = replace(job, design=replace(job.design, ports=ports))
    constrained_job = replace(
        unconstrained_job,
        routing_constraints=(
            RoutingShieldConstraint(
                "signal-shield",
                "signal",
                "signal-b",
                maximum_spacing_dbu=2,
            ),
        ),
    )
    unconstrained = run(unconstrained_job)
    independent_outcome = evaluate_routing_constraints(
        constrained_job,
        unconstrained.routes,
    )
    result = run(constrained_job)

    assert independent_outcome[0].status is ConstraintStatus.VIOLATED
    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert any(
        segment.start.y == segment.end.y == 10
        for segment in result.routes[1].segments
    )


def test_router_constructs_continuous_multilayer_shield_with_via() -> None:
    job = _multilayer_job()
    design = replace(
        job.design,
        ports=job.design.ports
        + (
            PhysicalPort("source-b", (PinAccess("m1", Rect(2, 11, 4, 13)),)),
            PhysicalPort("sink-b", (PinAccess("m2", Rect(36, 11, 38, 13)),)),
        ),
        nets=job.design.nets
        + (
            PhysicalNet(
                "signal-b",
                (PinReference("source-b"), PinReference("sink-b")),
            ),
        ),
    )
    constrained = replace(
        job,
        design=design,
        routing_constraints=(
            RoutingShieldConstraint(
                "multilayer-shield",
                "signal",
                "signal-b",
                maximum_spacing_dbu=4,
            ),
        ),
    )

    result = run(constrained)

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert tuple(len(route.vias) for route in result.routes) == (1, 1)
    independently_rejected = evaluate_routing_constraints(
        constrained,
        (result.routes[0], replace(result.routes[1], vias=())),
    )
    assert independently_rejected[0].status is ConstraintStatus.VIOLATED
    assert "vias=0" in independently_rejected[0].message


def test_shield_constraint_requires_distinct_known_nets() -> None:
    job = _job()

    with pytest.raises(PnrInputError, match="needs distinct nets"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingShieldConstraint(
                        "self-shield",
                        "signal",
                        "signal",
                        maximum_spacing_dbu=2,
                    ),
                ),
            )
        )


def test_multi_terminal_net_connects_each_terminal_to_the_route_tree() -> None:
    job = _job()
    design = replace(
        job.design,
        ports=job.design.ports
        + (
            PhysicalPort("branch", (PinAccess("route", Rect(19, 29, 21, 31)),)),
        ),
        nets=(
            replace(
                job.design.nets[0],
                pins=job.design.nets[0].pins + (PinReference("branch"),),
            ),
        ),
    )

    result = run(replace(job, design=design))

    assert result.status is ResultStatus.SUCCEEDED
    route_points = {
        point
        for segment in result.routes[0].segments
        for point in (segment.start, segment.end)
    }
    assert Point(3, 6) in route_points
    assert Point(37, 6) in route_points
    assert Point(20, 30) in route_points
    assert _wire_length(replace(job, design=design)) == 58


def test_routing_region_constraint_adds_required_geometry_to_the_route_tree() -> None:
    job = _job()
    result = run(
        replace(
            job,
            routing_constraints=(
                RoutingRegionConstraint(
                    "required-channel",
                    "signal",
                    (LayerShape("route", Rect(19, 19, 21, 21)),),
                ),
            ),
        )
    )

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in result.routes[0].segments
    ) == 62


def test_routing_regions_form_an_ordered_primary_topology() -> None:
    job = _job()
    regions = (
        LayerShape("route", Rect(9, 29, 11, 31)),
        LayerShape("route", Rect(29, 9, 31, 11)),
    )
    ordered_job = replace(
        job,
        routing_constraints=(
            RoutingRegionConstraint(
                "ordered-channel",
                "signal",
                regions,
            ),
        ),
    )
    result = run(ordered_job)

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in result.routes[0].segments
    ) == 82
    reverse_outcome = evaluate_routing_constraints(
        replace(
            ordered_job,
            routing_constraints=(
                RoutingRegionConstraint(
                    "reverse-channel",
                    "signal",
                    tuple(reversed(regions)),
                ),
            ),
        ),
        result.routes,
    )
    assert reverse_outcome[0].status is ConstraintStatus.VIOLATED
    assert "in order" in reverse_outcome[0].message


def test_routing_region_constraint_must_be_inside_the_die() -> None:
    job = _job()

    with pytest.raises(PnrInputError, match="outside the die"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingRegionConstraint(
                        "outside",
                        "signal",
                        (LayerShape("route", Rect(39, 39, 41, 41)),),
                    ),
                ),
            )
        )


def test_router_avoids_unconnected_terminal_geometry() -> None:
    job = _job()
    design = replace(
        job.design,
        ports=job.design.ports
        + (
            PhysicalPort("unused", (PinAccess("route", Rect(18, 5, 22, 7)),)),
        ),
    )

    result = run(replace(job, design=design))

    assert result.status is ResultStatus.SUCCEEDED
    assert _wire_length(replace(job, design=design)) > 34


def test_failed_greedy_order_is_ripped_up_and_routed_in_another_order() -> None:
    result = run(_ripup_job())

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(route.net for route in result.routes) == (
        "a-flexible",
        "z-critical",
    )
    metrics = {metric.name: metric.value for metric in result.stage_reports[-1].metrics}
    assert metrics["routing_iterations"] == 2
    assert metrics["routing_route_attempts"] == 4
    assert metrics["routing_ripped_net_count"] == 1


def test_ripup_iteration_budget_exhaustion_is_explicit() -> None:
    result = run(_ripup_job(maximum_routing_iterations=1))

    assert result.status is ResultStatus.EXHAUSTED
    assert tuple(route.net for route in result.routes) == ("a-flexible",)
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_iteration_exhausted"
    )
    assert result.stage_reports[-1].diagnostics[0].entities == (
        "z-critical",
        "a-flexible",
    )
    metrics = {metric.name: metric.value for metric in result.stage_reports[-1].metrics}
    assert metrics["routing_iterations"] == 1
    assert metrics["routing_route_attempts"] == 2
    assert metrics["routing_ripped_net_count"] == 0


def test_routing_iteration_budget_must_be_positive() -> None:
    job = _job()

    with pytest.raises(PnrInputError, match="maximum routing iterations"):
        run(
            replace(
                job,
                execution_policy=replace(
                    job.execution_policy,
                    maximum_routing_iterations=0,
                ),
            )
        )

    with pytest.raises(PnrInputError, match="routing congestion bin counts"):
        run(
            replace(
                job,
                execution_policy=replace(
                    job.execution_policy,
                    routing_congestion_bins_x=0,
                ),
            )
        )

    with pytest.raises(PnrInputError, match="maximum placement repair iterations"):
        run(
            replace(
                job,
                execution_policy=replace(
                    job.execution_policy,
                    maximum_placement_repair_iterations=-1,
                ),
            )
        )


def test_track_only_technology_routes_only_on_declared_track_coordinates() -> None:
    technology = replace(
        _technology(),
        routing_resources=(
            RoutingTrackPattern("route-tracks", "route", Axis.Y, 0, 2, 20),
        ),
    )

    result = run(_job(technology=technology))

    assert result.status is ResultStatus.SUCCEEDED
    assert result.routes[0].segments[0].start.y == 6
    assert result.routes[0].segments[0].end.y == 6


def test_track_resource_requires_a_terminal_access_on_a_legal_track() -> None:
    technology = replace(
        _technology(),
        routing_resources=(
            RoutingTrackPattern("route-tracks", "route", Axis.Y, 0, 4, 10),
        ),
    )

    result = run(_job(technology=technology))

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_pin_access_layer_unsupported"
    )


def test_orthogonal_track_layers_connect_at_a_legal_via_intersection() -> None:
    job = _multilayer_job()
    technology = replace(
        job.technology,
        routing_resources=(
            RoutingTrackPattern("m1-tracks", "m1", Axis.Y, 2, 4, 10),
            RoutingTrackPattern("m2-tracks", "m2", Axis.X, 1, 4, 10),
        ),
    )
    design = replace(
        job.design,
        ports=(
            job.design.ports[0],
            PhysicalPort("sink", (PinAccess("m2", Rect(36, 29, 38, 31)),)),
        ),
    )

    result = run(replace(job, technology=technology, design=design))

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.routes[0].vias) == 1
    assert {segment.layer for segment in result.routes[0].segments} == {"m1", "m2"}
    assert all(
        segment.start.y == segment.end.y
        for segment in result.routes[0].segments
        if segment.layer == "m1"
    )
    assert all(
        segment.start.x == segment.end.x
        for segment in result.routes[0].segments
        if segment.layer == "m2"
    )


def test_router_constructs_legal_via_for_two_layer_connection() -> None:
    result = run(_multilayer_job())

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.routes) == 1
    assert len(result.routes[0].vias) == 1
    assert result.routes[0].vias[0].net == "signal"
    assert result.routes[0].vias[0].via_definition == "via12"
    assert result.routes[0].vias[0].origin == Point(3, 6)
    assert {segment.layer for segment in result.routes[0].segments} <= {"m1", "m2"}
    metrics = {metric.name: metric.value for metric in result.stage_reports[-1].metrics}
    assert metrics["via_count"] == 1


def test_router_crosses_two_vias_without_requiring_a_named_stack() -> None:
    result = run(_multilayer_job(three_layers=True))

    assert result.status is ResultStatus.SUCCEEDED
    assert {via.via_definition for via in result.routes[0].vias} == {
        "via12",
        "via23",
    }
    assert len(result.routes[0].vias) == 2


def test_routing_layer_constraint_is_enforced_during_search() -> None:
    job = _multilayer_job()
    constrained = replace(
        job,
        routing_constraints=(
            RoutingLayerConstraint("signal-layers", "signal", ("m1", "m2")),
        ),
    )

    result = run(constrained)

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED

    impossible = run(
        replace(
            job,
            routing_constraints=(
                RoutingLayerConstraint("m1-only", "signal", ("m1",)),
            ),
        )
    )
    assert impossible.status is ResultStatus.FAILED
    assert impossible.stage_reports[-1].diagnostics[0].code == (
        "routing_layer_constraint_unsatisfied"
    )


def test_routing_length_constraint_actively_compensates_the_route() -> None:
    job = _job()
    exact_shortest = run(
        replace(
            job,
            routing_constraints=(
                RoutingLengthConstraint("exact-length", "signal", 34, 34),
            ),
        )
    )
    exact_compensated = run(
        replace(
            job,
            routing_constraints=(
                RoutingLengthConstraint("compensated-length", "signal", 54, 54),
            ),
        )
    )
    too_short = run(
        replace(
            job,
            routing_constraints=(
                RoutingLengthConstraint("maximum-length", "signal", 0, 33),
            ),
        )
    )

    assert exact_shortest.status is ResultStatus.SUCCEEDED
    assert exact_shortest.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert exact_compensated.status is ResultStatus.SUCCEEDED
    assert exact_compensated.constraint_outcomes[-1].status is (
        ConstraintStatus.SATISFIED
    )
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in exact_compensated.routes[0].segments
    ) == 54
    assert len(exact_compensated.routes[0].segments) == 3
    assert too_short.status is ResultStatus.FAILED
    assert too_short.routes == ()
    assert too_short.constraint_outcomes[-1].status is (
        ConstraintStatus.NOT_EVALUATED
    )
    assert too_short.stage_reports[-1].diagnostics[-1].code == (
        "routing_length_window_infeasible"
    )

    independently_violated = evaluate_routing_constraints(
        replace(
            job,
            routing_constraints=(
                RoutingLengthConstraint("maximum-length", "signal", 0, 33),
            ),
        ),
        exact_shortest.routes,
    )
    assert independently_violated[0].status is ConstraintStatus.VIOLATED


def test_routing_via_count_constraint_is_checked_end_to_end() -> None:
    two_layer = _multilayer_job()
    accepted = run(
        replace(
            two_layer,
            routing_constraints=(
                RoutingViaCountConstraint("one-via", "signal", 1),
            ),
        )
    )
    three_layer = _multilayer_job(three_layers=True)
    rejected = run(
        replace(
            three_layer,
            routing_constraints=(
                RoutingViaCountConstraint("one-via", "signal", 1),
            ),
        )
    )

    assert accepted.status is ResultStatus.SUCCEEDED
    assert accepted.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert rejected.status is ResultStatus.FAILED
    assert rejected.stage_reports[-1].diagnostics[0].code == (
        "routing_via_count_constraint_unsatisfied"
    )


def test_routing_constraints_validate_net_layer_and_range() -> None:
    job = _job()

    with pytest.raises(PnrInputError, match="unknown net missing"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingLengthConstraint("unknown-net", "missing", 0, 10),
                ),
            )
        )
    with pytest.raises(PnrInputError, match="unknown layers: missing"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingLayerConstraint("unknown-layer", "signal", ("missing",)),
                ),
            )
        )
    with pytest.raises(PnrInputError, match="invalid range"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingLengthConstraint("bad-range", "signal", 20, 10),
                ),
            )
        )
    with pytest.raises(PnrInputError, match="needs at least two nets"):
        run(
            replace(
                job,
                routing_constraints=(
                    RoutingSkewConstraint("bad-skew", ("signal",), 0),
                ),
            )
        )


def test_via_search_avoids_cut_layer_obstruction() -> None:
    job = _multilayer_job()
    blocker = PhysicalMaster(
        "cut-blocker",
        6,
        12,
        obstructions=(LayerShape("v1", Rect(0, 0, 6, 12)),),
        allowed_orientations=(Orientation.R0,),
    )
    design = replace(
        job.design,
        masters=(blocker,),
        instances=(
            PhysicalInstance("cut-blocker", blocker.name, Placement(Point(0, 0))),
        ),
    )

    result = run(replace(job, design=design))

    assert result.status is ResultStatus.SUCCEEDED
    via_origin = result.routes[0].vias[0].origin
    assert via_origin.x >= 9 or via_origin.y >= 15
    assert {segment.layer for segment in result.routes[0].segments} == {"m1", "m2"}


def test_layer_transition_without_complete_via_rules_is_unsupported() -> None:
    job = _multilayer_job()
    technology = replace(
        job.technology,
        rules=tuple(
            rule
            for rule in job.technology.rules
            if getattr(rule, "name", "") != "m2-v1-enclosure"
        ),
    )

    result = run(replace(job, technology=technology))

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.routes == ()
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_layer_transition_unsupported"
    )


def test_layer_transition_rejects_via_definition_with_illegal_cut_spacing() -> None:
    job = _multilayer_job()
    via = replace(
        job.technology.via_definitions[0],
        cut_shapes=(Rect(-1, -1, 0, 1), Rect(0, -1, 1, 1)),
    )

    result = run(
        replace(
            job,
            technology=replace(job.technology, via_definitions=(via,)),
        )
    )

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_layer_transition_unsupported"
    )


def test_independent_checker_accepts_single_and_multilayer_reference_routes() -> None:
    for job in (_job(), _multilayer_job(), _multilayer_job(three_layers=True)):
        result = run(job)

        assert result.status is ResultStatus.SUCCEEDED
        assert check_routing_solution(job, result.placements, result.routes) == ()


def test_independent_checker_rejects_non_manhattan_and_open_routes() -> None:
    job = _job()
    result = run(job)
    segment = result.routes[0].segments[0]
    diagonal = replace(
        segment,
        end=Point(segment.end.x + 1, segment.end.y + 1),
    )

    malformed = check_routing_solution(
        job,
        result.placements,
        (replace(result.routes[0], segments=(diagonal,)),),
    )
    open_route = check_routing_solution(
        job,
        result.placements,
        (replace(result.routes[0], segments=(), vias=()),),
    )

    assert "routing_segment_non_manhattan" in {
        diagnostic.code for diagnostic in malformed
    }
    assert "routing_connectivity_open" in {
        diagnostic.code for diagnostic in open_route
    }


def test_independent_checker_rejects_inter_net_spacing_violation() -> None:
    job = _job()
    design = replace(
        job.design,
        ports=job.design.ports
        + (
            PhysicalPort("source-b", (PinAccess("route", Rect(2, 9, 4, 11)),)),
            PhysicalPort("sink-b", (PinAccess("route", Rect(36, 9, 38, 11)),)),
        ),
        nets=job.design.nets
        + (
            PhysicalNet(
                "signal-b",
                (PinReference("source-b"), PinReference("sink-b")),
            ),
        ),
    )
    job = replace(job, design=design)
    result = run(job)
    second = result.routes[1]
    too_close = tuple(
        replace(
            segment,
            start=Point(segment.start.x, segment.start.y - 2),
            end=Point(segment.end.x, segment.end.y - 2),
        )
        for segment in second.segments
    )

    diagnostics = check_routing_solution(
        job,
        result.placements,
        (result.routes[0], replace(second, segments=too_close)),
    )

    assert "routing_inter_net_spacing_violation" in {
        diagnostic.code for diagnostic in diagnostics
    }


def test_independent_checker_applies_cut_spacing_between_same_net_vias() -> None:
    job = _multilayer_job()
    result = run(job)
    route = result.routes[0]
    duplicate = replace(
        route.vias[0],
        origin=Point(route.vias[0].origin.x + 1, route.vias[0].origin.y),
    )

    diagnostics = check_routing_solution(
        job,
        result.placements,
        (replace(route, vias=route.vias + (duplicate,)),),
    )

    assert "routing_same_net_cut_spacing_violation" in {
        diagnostic.code for diagnostic in diagnostics
    }


def test_missing_route_rules_are_explicitly_unsupported() -> None:
    technology = replace(
        _technology(),
        rules=(MinimumWidthRule("route-width", "route", 2),),
    )

    result = run(_job(technology=technology))

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_rule_capability_missing"
    )


def test_route_search_exhaustion_is_not_reported_as_infeasibility() -> None:
    result = run(_job(maximum_route_states=1))

    assert result.status is ResultStatus.EXHAUSTED
    assert result.stage_reports[-1].diagnostics[0].code == "routing_search_exhausted"
