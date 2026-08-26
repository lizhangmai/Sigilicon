from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.layout.pnr._routing_check import check_routing_solution
from sigilicon.layout.pnr import (
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
    PnrInputError,
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    RoutingDirection,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
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
) -> PhysicalDesignJob:
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
    return PhysicalDesignJob(
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
        request=PnrRequest(
            stages=(PnrStage.PLACEMENT, PnrStage.ROUTING),
            maximum_route_states=maximum_route_states,
        ),
    )


def _multilayer_job(*, three_layers: bool = False) -> PhysicalDesignJob:
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
    return PhysicalDesignJob(
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
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
    )


def _ripup_job(*, maximum_routing_iterations: int = 8) -> PhysicalDesignJob:
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
        request=PnrRequest(
            stages=(PnrStage.PLACEMENT, PnrStage.ROUTING),
            maximum_routing_iterations=maximum_routing_iterations,
        ),
    )


def _wire_length(job: PhysicalDesignJob) -> int:
    result = run(job)
    assert result.status is ResultStatus.SUCCEEDED
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in result.routes
        for segment in route.segments
    )


def _parallel_net_job(*, congestion_bins_y: int) -> PhysicalDesignJob:
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
        request=replace(
            job.request,
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
    assert routing_report.stage is PnrStage.ROUTING
    assert routing_report.status is ResultStatus.SUCCEEDED
    assert {metric.name: metric.value for metric in routing_report.metrics}[
        "routed_net_count"
    ] == 1


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


def test_skew_constraint_rejects_unmatched_route_lengths() -> None:
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
    assert result.routes == ()
    assert result.constraint_outcomes[-1].status is ConstraintStatus.VIOLATED
    assert result.stage_reports[-1].diagnostics[-1].code == (
        "routing_constraint_violated"
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


def test_ripup_iteration_budget_exhaustion_is_explicit() -> None:
    result = run(_ripup_job(maximum_routing_iterations=1))

    assert result.status is ResultStatus.EXHAUSTED
    assert result.routes == ()
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_iteration_exhausted"
    )


def test_routing_iteration_budget_must_be_positive() -> None:
    job = _job()

    with pytest.raises(PnrInputError, match="maximum routing iterations"):
        run(
            replace(
                job,
                request=replace(job.request, maximum_routing_iterations=0),
            )
        )

    with pytest.raises(PnrInputError, match="routing congestion bin counts"):
        run(
            replace(
                job,
                request=replace(job.request, routing_congestion_bins_x=0),
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


def test_routing_length_constraint_gates_the_returned_solution() -> None:
    job = _job()
    exact = run(
        replace(
            job,
            routing_constraints=(
                RoutingLengthConstraint("exact-length", "signal", 34, 34),
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

    assert exact.status is ResultStatus.SUCCEEDED
    assert exact.constraint_outcomes[-1].status is ConstraintStatus.SATISFIED
    assert too_short.status is ResultStatus.FAILED
    assert too_short.routes == ()
    assert too_short.constraint_outcomes[-1].status is ConstraintStatus.VIOLATED
    assert too_short.stage_reports[-1].diagnostics[-1].code == (
        "routing_constraint_violated"
    )


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
