"""Deterministic gridless Manhattan reference routing."""

from __future__ import annotations

from dataclasses import dataclass
import heapq

from sigilicon.layout.pnr._geometry import (
    transformed_obstructions,
    transformed_pin_accesses,
)
from sigilicon.layout.pnr.model import (
    Diagnostic,
    GridlessRoutingResource,
    InstancePlacement,
    LayerShape,
    Metric,
    MinimumSpacingRule,
    MinimumWidthRule,
    NetRoute,
    PhysicalDesignJob,
    PinReference,
    Placement,
    Point,
    PnrStage,
    Rect,
    ResultStatus,
    RouteSegment,
    StageReport,
)


@dataclass(frozen=True)
class RoutingSolveResult:
    status: ResultStatus
    routes: tuple[NetRoute, ...]
    report: StageReport


def _result(
    status: ResultStatus,
    *,
    routes: tuple[NetRoute, ...] = (),
    code: str | None = None,
    message: str = "",
    entities: tuple[str, ...] = (),
    route_states: int = 0,
) -> RoutingSolveResult:
    diagnostics = (
        ()
        if code is None
        else (Diagnostic(code=code, message=message, entities=entities),)
    )
    wire_length = sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for route in routes
        for segment in route.segments
    )
    return RoutingSolveResult(
        status=status,
        routes=routes,
        report=StageReport(
            stage=PnrStage.ROUTING,
            status=status,
            diagnostics=diagnostics,
            metrics=(
                Metric("routed_net_count", len(routes), "count"),
                Metric("wire_length", wire_length, "dbu"),
                Metric("route_states", route_states, "count"),
            ),
        ),
    )


def _endpoint_accesses(
    job: PhysicalDesignJob,
    reference: PinReference,
    placements: dict[str, Placement],
) -> tuple[LayerShape, ...]:
    if reference.instance is None:
        port = next(port for port in job.design.ports if port.name == reference.pin)
        return tuple(
            LayerShape(layer=access.layer, shape=access.shape)
            for access in port.accesses
        )
    instance = next(
        instance
        for instance in job.design.instances
        if instance.name == reference.instance
    )
    master = next(
        master for master in job.design.masters if master.name == instance.master
    )
    return transformed_pin_accesses(
        master,
        reference.pin,
        placements[reference.instance],
    )


def _common_gridless_resource(
    job: PhysicalDesignJob,
    accesses: tuple[tuple[LayerShape, ...], ...],
) -> GridlessRoutingResource | None:
    resources = tuple(
        sorted(
            (
                resource
                for resource in job.technology.routing_resources
                if isinstance(resource, GridlessRoutingResource)
            ),
            key=lambda resource: (resource.layer, resource.name),
        )
    )
    return next(
        (
            resource
            for resource in resources
            if all(
                any(access.layer == resource.layer for access in endpoint)
                for endpoint in accesses
            )
        ),
        None,
    )


def _route_rules(job: PhysicalDesignJob, layer: str) -> tuple[int, int] | None:
    widths = tuple(
        rule.width_dbu
        for rule in job.technology.rules
        if isinstance(rule, MinimumWidthRule) and rule.layer == layer
    )
    spacings = tuple(
        rule.spacing_dbu
        for rule in job.technology.rules
        if isinstance(rule, MinimumSpacingRule) and rule.layer == layer
    )
    if not widths or not spacings:
        return None
    return max(widths), max(spacings)


def _snap_nearest(value2: int, low: int, high: int, grid: int) -> int | None:
    first = -(-low // grid) * grid
    last = high // grid * grid
    if first > last:
        return None
    floor = value2 // (2 * grid) * grid
    candidates = {
        first,
        last,
        min(max(floor, first), last),
        min(max(floor + grid, first), last),
    }
    return min(candidates, key=lambda value: (abs(2 * value - value2), value))


def _access_point(
    accesses: tuple[LayerShape, ...],
    *,
    layer: str,
    width: int,
    grid: int,
    region: Rect,
) -> Point | None:
    margin = width // 2
    candidates: list[Point] = []
    for access in accesses:
        if access.layer != layer:
            continue
        low_x = max(access.shape.x_min + margin, region.x_min)
        high_x = min(access.shape.x_max - margin, region.x_max)
        low_y = max(access.shape.y_min + margin, region.y_min)
        high_y = min(access.shape.y_max - margin, region.y_max)
        x = _snap_nearest(
            access.shape.x_min + access.shape.x_max,
            low_x,
            high_x,
            grid,
        )
        y = _snap_nearest(
            access.shape.y_min + access.shape.y_max,
            low_y,
            high_y,
            grid,
        )
        if x is not None and y is not None:
            candidates.append(Point(x, y))
    return min(candidates, key=lambda point: (point.y, point.x)) if candidates else None


def _expanded(rectangle: Rect, distance: int) -> Rect:
    return Rect(
        rectangle.x_min - distance,
        rectangle.y_min - distance,
        rectangle.x_max + distance,
        rectangle.y_max + distance,
    )


def _point_in_interior(point: Point, rectangle: Rect) -> bool:
    return (
        rectangle.x_min < point.x < rectangle.x_max
        and rectangle.y_min < point.y < rectangle.y_max
    )


def _segment_blocks_point(
    segment: RouteSegment,
    point: Point,
    *,
    width: int,
    spacing: int,
) -> bool:
    clearance = (segment.width_dbu + width) // 2 + spacing
    x_min = min(segment.start.x, segment.end.x) - clearance
    x_max = max(segment.start.x, segment.end.x) + clearance
    y_min = min(segment.start.y, segment.end.y) - clearance
    y_max = max(segment.start.y, segment.end.y) + clearance
    return x_min < point.x < x_max and y_min < point.y < y_max


def _astar(
    start: Point,
    targets: frozenset[Point],
    *,
    region: Rect,
    grid: int,
    blocked_rectangles: tuple[Rect, ...],
    blocked_segments: tuple[RouteSegment, ...],
    width: int,
    spacing: int,
    remaining_states: int,
) -> tuple[tuple[Point, ...] | None, int, bool]:
    def heuristic(point: Point) -> int:
        return min(
            abs(point.x - target.x) + abs(point.y - target.y)
            for target in targets
        )

    frontier: list[tuple[int, int, int, int, Point]] = []
    heapq.heappush(frontier, (heuristic(start), 0, start.y, start.x, start))
    came_from: dict[Point, Point] = {}
    cost: dict[Point, int] = {start: 0}
    states = 0
    while frontier:
        if states >= remaining_states:
            return None, states, True
        _, current_cost, _, _, current = heapq.heappop(frontier)
        if current_cost != cost[current]:
            continue
        states += 1
        if current in targets:
            path = [current]
            while path[-1] != start:
                path.append(came_from[path[-1]])
            path.reverse()
            return tuple(path), states, False
        for dx, dy in ((grid, 0), (0, grid), (-grid, 0), (0, -grid)):
            neighbor = Point(current.x + dx, current.y + dy)
            if not (
                region.x_min <= neighbor.x <= region.x_max
                and region.y_min <= neighbor.y <= region.y_max
            ):
                continue
            if neighbor not in targets and neighbor != start:
                if any(
                    _point_in_interior(neighbor, rectangle)
                    for rectangle in blocked_rectangles
                ):
                    continue
                if any(
                    _segment_blocks_point(
                        segment,
                        neighbor,
                        width=width,
                        spacing=spacing,
                    )
                    for segment in blocked_segments
                ):
                    continue
            neighbor_cost = current_cost + grid
            if neighbor_cost >= cost.get(neighbor, 2**63 - 1):
                continue
            cost[neighbor] = neighbor_cost
            came_from[neighbor] = current
            heapq.heappush(
                frontier,
                (
                    neighbor_cost + heuristic(neighbor),
                    neighbor_cost,
                    neighbor.y,
                    neighbor.x,
                    neighbor,
                ),
            )
    return None, states, False


def _segments(
    net: str,
    layer: str,
    width: int,
    path: tuple[Point, ...],
) -> tuple[RouteSegment, ...]:
    if len(path) < 2:
        return ()
    segments: list[RouteSegment] = []
    start = path[0]
    previous = path[0]
    direction = (
        path[1].x - path[0].x,
        path[1].y - path[0].y,
    )
    for point in path[1:]:
        next_direction = point.x - previous.x, point.y - previous.y
        if next_direction != direction:
            segments.append(RouteSegment(net, layer, start, previous, width))
            start = previous
            direction = next_direction
        previous = point
    segments.append(RouteSegment(net, layer, start, previous, width))
    return tuple(segment for segment in segments if segment.start != segment.end)


def solve_routing(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingSolveResult:
    if not job.design.nets:
        return _result(ResultStatus.SUCCEEDED)
    placements = {
        item.instance: item.placement for item in instance_placements
    }
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    all_pin_references = tuple(
        PinReference(port.name) for port in job.design.ports
    ) + tuple(
        PinReference(pin.name, instance.name)
        for instance in job.design.instances
        for pin in masters[instance.master].pins
    )
    all_routes: list[NetRoute] = []
    route_states = 0

    for net in sorted(job.design.nets, key=lambda item: item.name):
        accesses = tuple(
            _endpoint_accesses(job, reference, placements)
            for reference in net.pins
        )
        if any(not endpoint for endpoint in accesses):
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code="routing_pin_access_missing",
                message=f"net {net.name} has a terminal without access geometry",
                entities=(net.name,),
                route_states=route_states,
            )
        resource = _common_gridless_resource(job, accesses)
        if resource is None:
            has_gridless_resource = any(
                isinstance(candidate, GridlessRoutingResource)
                for candidate in job.technology.routing_resources
            )
            return _result(
                ResultStatus.UNSUPPORTED,
                routes=tuple(all_routes),
                code=(
                    "routing_layer_transition_required"
                    if has_gridless_resource
                    else "routing_gridless_resource_required"
                ),
                message=(
                    f"net {net.name} has no common gridless routing layer"
                    if has_gridless_resource
                    else "reference router requires a gridless routing resource"
                ),
                entities=(net.name,) if has_gridless_resource else (),
                route_states=route_states,
            )
        rules = _route_rules(job, resource.layer)
        if rules is None:
            return _result(
                ResultStatus.UNSUPPORTED,
                routes=tuple(all_routes),
                code="routing_rule_capability_missing",
                message=f"layer {resource.layer} needs minimum width and spacing rules",
                entities=(resource.layer,),
                route_states=route_states,
            )
        width, spacing = rules
        grid = job.technology.manufacturing_grid_dbu
        if width % (2 * grid) != 0:
            return _result(
                ResultStatus.UNSUPPORTED,
                routes=tuple(all_routes),
                code="routing_width_resolution_unsupported",
                message="reference router needs width divisible by twice the grid",
                entities=(resource.layer,),
                route_states=route_states,
            )
        base_region = resource.region or job.design.die
        region = base_region.intersection(job.design.die)
        margin = width // 2
        if region is None or region.width <= width or region.height <= width:
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code="routing_region_empty",
                message=f"layer {resource.layer} has no usable routing region",
                entities=(resource.layer,),
                route_states=route_states,
            )
        center_region = Rect(
            region.x_min + margin,
            region.y_min + margin,
            region.x_max - margin,
            region.y_max - margin,
        )
        endpoints = tuple(
            _access_point(
                endpoint,
                layer=resource.layer,
                width=width,
                grid=grid,
                region=center_region,
            )
            for endpoint in accesses
        )
        if any(point is None for point in endpoints):
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code="routing_pin_access_too_small",
                message=f"net {net.name} access cannot contain the legal route width",
                entities=(net.name,),
                route_states=route_states,
            )
        points = tuple(point for point in endpoints if point is not None)
        net_references = frozenset(net.pins)
        blocked_rectangles = tuple(
            _expanded(obstruction.shape, margin + spacing)
            for instance_name, placement in placements.items()
            for obstruction in transformed_obstructions(
                masters[instances[instance_name].master],
                placement,
            )
            if obstruction.layer == resource.layer
        ) + tuple(
            _expanded(access.shape, margin + spacing)
            for reference in all_pin_references
            if reference not in net_references
            for access in _endpoint_accesses(job, reference, placements)
            if access.layer == resource.layer
        )
        blocked_segments = tuple(
            segment
            for route in all_routes
            for segment in route.segments
            if segment.layer == resource.layer
        )
        tree: set[Point] = {points[0]}
        segments: list[RouteSegment] = []
        for endpoint in points[1:]:
            if endpoint in tree:
                continue
            path, states, exhausted = _astar(
                endpoint,
                frozenset(tree),
                region=center_region,
                grid=grid,
                blocked_rectangles=blocked_rectangles,
                blocked_segments=blocked_segments,
                width=width,
                spacing=spacing,
                remaining_states=job.request.maximum_route_states - route_states,
            )
            route_states += states
            if path is None:
                return _result(
                    ResultStatus.EXHAUSTED if exhausted else ResultStatus.FAILED,
                    routes=tuple(all_routes),
                    code=(
                        "routing_search_exhausted"
                        if exhausted
                        else "routing_infeasible"
                    ),
                    message=f"reference router could not connect net {net.name}",
                    entities=(net.name,),
                    route_states=route_states,
                )
            segments.extend(_segments(net.name, resource.layer, width, path))
            tree.update(path)
        all_routes.append(NetRoute(net.name, tuple(segments)))

    return _result(
        ResultStatus.SUCCEEDED,
        routes=tuple(all_routes),
        route_states=route_states,
    )
