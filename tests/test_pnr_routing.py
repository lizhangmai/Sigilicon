from __future__ import annotations

from dataclasses import replace

from sigilicon.layout.pnr import (
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
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    RoutingDirection,
    RoutingTrackPattern,
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


def _wire_length(job: PhysicalDesignJob) -> int:
    result = run(job)
    assert result.status is ResultStatus.SUCCEEDED
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in result.routes
        for segment in route.segments
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

    result = run(replace(job, design=design))

    assert result.status is ResultStatus.SUCCEEDED
    assert tuple(len(route.segments) for route in result.routes) == (1, 1)
    assert sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in result.routes
        for segment in route.segments
    ) == 68


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


def test_track_only_technology_is_explicitly_unsupported_by_reference_router() -> None:
    technology = replace(
        _technology(),
        routing_resources=(
            RoutingTrackPattern("route-tracks", "route", Axis.Y, 0, 2, 20),
        ),
    )

    result = run(_job(technology=technology))

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.placements == ()
    assert result.routes == ()
    assert result.stage_reports[-1].diagnostics[0].code == (
        "routing_gridless_resource_required"
    )


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
