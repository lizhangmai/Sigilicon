"""Deterministic Manhattan routing over normalized routing resources."""

from __future__ import annotations

from dataclasses import dataclass
import heapq

from sigilicon.layout.pnr._geometry import (
    translated_rect,
    via_occurrence_shapes,
)
from sigilicon.layout.pnr._routing_problem import (
    NetRoutingProblem,
    RoutingDomain,
    RoutingLayerDomain,
    RoutingProblem,
    compile_routing_problem,
)
from sigilicon.layout.pnr._routing_policy import RoutingGroupPolicy
from sigilicon.layout.pnr._routing_state import RoutingState
from sigilicon.layout.pnr.model import (
    Diagnostic,
    InstancePlacement,
    LayerShape,
    Metric,
    NetRoute,
    PhysicalDesignJob,
    Point,
    PnrStage,
    Rect,
    ResultStatus,
    RouteSegment,
    RouteVia,
    StageReport,
    ViaDefinition,
)


@dataclass(frozen=True)
class RoutingSolveResult:
    status: ResultStatus
    routes: tuple[NetRoute, ...]
    report: StageReport
    route_states: int
    routing_iterations: int = 0
    route_attempts: int = 0
    ripped_net_count: int = 0


@dataclass(frozen=True)
class _RouteState:
    layer: str
    point: Point


@dataclass(frozen=True)
class _RoutePath:
    states: tuple[_RouteState, ...]
    transition_vias: tuple[str | None, ...]


@dataclass(frozen=True)
class _Blocker:
    shape: Rect
    owner: str | None


@dataclass(frozen=True)
class _RouteSearchResult:
    path: _RoutePath | None
    states: int
    exhausted: bool
    blocking_nets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _NetRouteAttempt:
    status: ResultStatus
    route: NetRoute | None
    diagnostic: Diagnostic | None
    route_states: int
    blocking_nets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _RouteCompensation:
    route: NetRoute | None
    route_states: int
    exhausted: bool = False


def _result(
    status: ResultStatus,
    *,
    routes: tuple[NetRoute, ...] = (),
    code: str | None = None,
    message: str = "",
    entities: tuple[str, ...] = (),
    route_states: int = 0,
) -> RoutingSolveResult:
    routes = tuple(sorted(routes, key=lambda route: route.net))
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
    via_count = sum(len(route.vias) for route in routes)
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
                Metric("via_count", via_count, "count"),
                Metric("route_states", route_states, "count"),
            ),
        ),
        route_states=route_states,
    )


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


def _access_states(
    accesses: tuple[LayerShape, ...],
    contexts: dict[str, RoutingLayerDomain],
    grid: int,
) -> tuple[_RouteState, ...]:
    states: set[_RouteState] = set()
    for layer, context in contexts.items():
        for region in context.regions:
            point = _access_point(
                accesses,
                layer=layer,
                width=context.width,
                grid=grid,
                region=region,
            )
            if point is not None:
                states.add(_RouteState(layer, point))
        margin = context.width // 2
        for access in accesses:
            if access.layer != layer:
                continue
            low_x = max(
                access.shape.x_min + margin,
                min(region.x_min for region in context.raw_regions) + margin,
            )
            high_x = min(
                access.shape.x_max - margin,
                max(region.x_max for region in context.raw_regions) - margin,
            )
            low_y = max(
                access.shape.y_min + margin,
                min(region.y_min for region in context.raw_regions) + margin,
            )
            high_y = min(
                access.shape.y_max - margin,
                max(region.y_max for region in context.raw_regions) - margin,
            )
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
            if x is not None:
                states.update(
                    _RouteState(layer, Point(x, track))
                    for track in context.horizontal_tracks
                    if low_y <= track <= high_y
                )
            if y is not None:
                states.update(
                    _RouteState(layer, Point(track, y))
                    for track in context.vertical_tracks
                    if low_x <= track <= high_x
                )
    return tuple(
        sorted(states, key=lambda state: (state.layer, state.point.y, state.point.x))
    )


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


def _point_in_context(point: Point, context: RoutingLayerDomain) -> bool:
    margin = context.width // 2
    conductor = Rect(
        point.x - margin,
        point.y - margin,
        point.x + margin,
        point.y + margin,
    )
    return context.covers_gridless(conductor) or (
        context.covers(conductor)
        and (
            point.y in context.horizontal_tracks
            or point.x in context.vertical_tracks
        )
    )


def _move_allowed(
    current: Point,
    neighbor: Point,
    context: RoutingLayerDomain,
) -> bool:
    margin = context.width // 2
    movement = Rect(
        min(current.x, neighbor.x) - margin,
        min(current.y, neighbor.y) - margin,
        max(current.x, neighbor.x) + margin,
        max(current.y, neighbor.y) + margin,
    )
    if context.covers_gridless(movement):
        return True
    if current.y == neighbor.y:
        return current.y in context.horizontal_tracks
    return current.x in context.vertical_tracks


def _bin_index(value: int, low: int, span: int, bins: int) -> int:
    return min(bins - 1, max(0, (value - low) * bins // span))


def _route_bin_demands(
    domain: RoutingDomain,
    routes: tuple[NetRoute, ...],
) -> dict[tuple[str, int, int, str], int]:
    die = domain.die
    bins_x = domain.congestion_bins_x
    bins_y = domain.congestion_bins_y
    demands: dict[tuple[str, int, int, str], int] = {}
    for route in routes:
        for segment in route.segments:
            if segment.start.y == segment.end.y:
                y_bin = _bin_index(
                    segment.start.y,
                    die.y_min,
                    die.height,
                    bins_y,
                )
                first = _bin_index(
                    min(segment.start.x, segment.end.x),
                    die.x_min,
                    die.width,
                    bins_x,
                )
                last = _bin_index(
                    max(segment.start.x, segment.end.x),
                    die.x_min,
                    die.width,
                    bins_x,
                )
                keys = (
                    (segment.layer, x_bin, y_bin, "horizontal")
                    for x_bin in range(first, last + 1)
                )
            else:
                x_bin = _bin_index(
                    segment.start.x,
                    die.x_min,
                    die.width,
                    bins_x,
                )
                first = _bin_index(
                    min(segment.start.y, segment.end.y),
                    die.y_min,
                    die.height,
                    bins_y,
                )
                last = _bin_index(
                    max(segment.start.y, segment.end.y),
                    die.y_min,
                    die.height,
                    bins_y,
                )
                keys = (
                    (segment.layer, x_bin, y_bin, "vertical")
                    for y_bin in range(first, last + 1)
                )
            for key in keys:
                demands[key] = demands.get(key, 0) + 1
    return demands


def _weighted_congestion_demands(
    domain: RoutingDomain,
    routes: tuple[NetRoute, ...],
    weight: int,
) -> dict[tuple[str, int, int, str], int]:
    if weight == 0:
        return {}
    return {
        key: demand * weight
        for key, demand in _route_bin_demands(domain, routes).items()
    }


def _route_search_costs(
    domain: RoutingDomain,
    state: RoutingState,
    *,
    congestion_weight: int,
    history_weight: int,
) -> dict[tuple[str, int, int, str], int]:
    costs = _weighted_congestion_demands(
        domain,
        state.routes,
        congestion_weight,
    )
    for key, history_cost in state.history_costs.items():
        costs[key] = costs.get(key, 0) + history_weight * history_cost
    return costs


def _congestion_metrics(
    domain: RoutingDomain,
    routes: tuple[NetRoute, ...],
) -> tuple[Metric, ...]:
    demands = _route_bin_demands(domain, routes)
    die = domain.die
    bins_x = domain.congestion_bins_x
    bins_y = domain.congestion_bins_y
    congested_bins: set[tuple[str, int, int]] = set()
    total_overflow = 0
    for (layer, x_bin, y_bin, direction), demand in demands.items():
        rules = domain.rules_for(layer)
        if rules is None:
            continue
        width, spacing = rules
        pitch = width + spacing
        bin_width = (
            die.x_min + die.width * (x_bin + 1) // bins_x
            - (die.x_min + die.width * x_bin // bins_x)
        )
        bin_height = (
            die.y_min + die.height * (y_bin + 1) // bins_y
            - (die.y_min + die.height * y_bin // bins_y)
        )
        capacity = (
            bin_height // pitch
            if direction == "horizontal"
            else bin_width // pitch
        )
        overflow = max(0, demand - capacity)
        if overflow:
            congested_bins.add((layer, x_bin, y_bin))
            total_overflow += overflow
    horizontal_demands = tuple(
        demand
        for (*_, direction), demand in demands.items()
        if direction == "horizontal"
    )
    vertical_demands = tuple(
        demand
        for (*_, direction), demand in demands.items()
        if direction == "vertical"
    )
    return (
        Metric(
            "routing_peak_horizontal_demand",
            max(horizontal_demands, default=0),
            "tracks",
        ),
        Metric(
            "routing_peak_vertical_demand",
            max(vertical_demands, default=0),
            "tracks",
        ),
        Metric("routing_congested_bin_count", len(congested_bins), "count"),
        Metric("routing_total_overflow", total_overflow, "tracks"),
    )


def _raw_blockers(
    net: NetRoutingProblem,
    state: RoutingState,
) -> dict[str, tuple[_Blocker, ...]]:
    blockers: dict[str, list[_Blocker]] = {
        layer: [_Blocker(shape, None) for shape in shapes]
        for layer, shapes in net.static_blockers.items()
    }
    for layer, occupancy in state.occupancy_by_layer.items():
        blockers.setdefault(layer, []).extend(
            _Blocker(item.shape, item.net) for item in occupancy
        )
    return {layer: tuple(shapes) for layer, shapes in blockers.items()}


def _center_blockers(
    raw_blockers: dict[str, tuple[_Blocker, ...]],
    contexts: dict[str, RoutingLayerDomain],
) -> dict[str, tuple[_Blocker, ...]]:
    return {
        layer: tuple(
            _Blocker(
                _expanded(blocker.shape, context.width // 2 + context.spacing),
                blocker.owner,
            )
            for blocker in raw_blockers.get(layer, ())
        )
        for layer, context in contexts.items()
    }


def _blocking_nets(
    state: _RouteState,
    blockers: dict[str, tuple[_Blocker, ...]],
) -> frozenset[str]:
    return frozenset(
        blocker.owner
        for blocker in blockers.get(state.layer, ())
        if blocker.owner is not None
        and _point_in_interior(state.point, blocker.shape)
    )


def _state_blocked(
    state: _RouteState,
    blockers: dict[str, tuple[_Blocker, ...]],
) -> bool:
    return any(
        _point_in_interior(state.point, blocker.shape)
        for blocker in blockers.get(state.layer, ())
    )


def _rectangles_too_close(
    first: Rect,
    second: Rect,
    spacing_x: int,
    spacing_y: int,
) -> bool:
    return not (
        first.x_max + spacing_x <= second.x_min
        or second.x_max + spacing_x <= first.x_min
        or first.y_max + spacing_y <= second.y_min
        or second.y_max + spacing_y <= first.y_min
    )


def _via_allowed(
    domain: RoutingDomain,
    via: ViaDefinition,
    origin: Point,
    contexts: dict[str, RoutingLayerDomain],
    raw_blockers: dict[str, tuple[_Blocker, ...]],
) -> tuple[bool, frozenset[str]]:
    blocking_nets: set[str] = set()
    translated_shapes = via_occurrence_shapes(via, origin)
    for layer, shape in translated_shapes:
        if not domain.die.contains(shape):
            return False, frozenset()
        if layer in contexts and not contexts[layer].covers(shape):
            return False, frozenset()
        if layer in contexts:
            spacing_x = spacing_y = contexts[layer].spacing
        else:
            cut_spacing = domain.cut_spacing_for(layer)
            if cut_spacing is None:
                return False
            spacing_x, spacing_y = cut_spacing
        blocked = tuple(
            blocker
            for blocker in raw_blockers.get(layer, ())
            if _rectangles_too_close(
                shape,
                blocker.shape,
                spacing_x,
                spacing_y,
            )
        )
        if blocked:
            blocking_nets.update(
                blocker.owner for blocker in blocked if blocker.owner is not None
            )
            return False, frozenset(blocking_nets)
    return True, frozenset()


def _via_adjacency(
    vias: tuple[ViaDefinition, ...],
) -> dict[str, tuple[tuple[str, ViaDefinition], ...]]:
    adjacency: dict[str, list[tuple[str, ViaDefinition]]] = {}
    for via in vias:
        adjacency.setdefault(via.lower_layer, []).append((via.upper_layer, via))
        adjacency.setdefault(via.upper_layer, []).append((via.lower_layer, via))
    return {
        layer: tuple(sorted(edges, key=lambda edge: (edge[0], edge[1].name)))
        for layer, edges in adjacency.items()
    }


def _layers_connect(
    endpoint_states: tuple[tuple[_RouteState, ...], ...],
    adjacency: dict[str, tuple[tuple[str, ViaDefinition], ...]],
) -> bool:
    endpoint_layers = tuple(
        frozenset(state.layer for state in states) for states in endpoint_states
    )
    for start_layer in sorted(endpoint_layers[0]):
        reachable = {start_layer}
        frontier = [start_layer]
        while frontier:
            layer = frontier.pop()
            for neighbor, _ in adjacency.get(layer, ()):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    frontier.append(neighbor)
        if all(reachable & layers for layers in endpoint_layers[1:]):
            return True
    return False


def _edge_congestion_demand(
    domain: RoutingDomain,
    current: _RouteState,
    neighbor: _RouteState,
    via_name: str | None,
    demands: dict[tuple[str, int, int, str], int],
) -> int:
    die = domain.die
    x_bin = _bin_index(
        neighbor.point.x,
        die.x_min,
        die.width,
        domain.congestion_bins_x,
    )
    y_bin = _bin_index(
        neighbor.point.y,
        die.y_min,
        die.height,
        domain.congestion_bins_y,
    )
    if via_name is None:
        direction = (
            "horizontal"
            if current.point.y == neighbor.point.y
            else "vertical"
        )
        return demands.get((current.layer, x_bin, y_bin, direction), 0)
    return sum(
        demands.get((layer, x_bin, y_bin, direction), 0)
        for layer in (current.layer, neighbor.layer)
        for direction in ("horizontal", "vertical")
    )


def _astar(
    starts: frozenset[_RouteState],
    targets: frozenset[_RouteState],
    *,
    contexts: dict[str, RoutingLayerDomain],
    center_blockers: dict[str, tuple[_Blocker, ...]],
    vias: tuple[ViaDefinition, ...],
    raw_blockers: dict[str, tuple[_Blocker, ...]],
    congestion_demands: dict[tuple[str, int, int, str], int],
    domain: RoutingDomain,
    remaining_states: int,
) -> _RouteSearchResult:
    def heuristic(state: _RouteState) -> int:
        return min(
            abs(state.point.x - target.point.x)
            + abs(state.point.y - target.point.y)
            for target in targets
        )

    grid = domain.grid
    adjacency = _via_adjacency(vias)
    frontier: list[tuple[int, int, str, int, int, _RouteState]] = []
    cost: dict[_RouteState, int] = {}
    for start in sorted(
        starts,
        key=lambda state: (state.layer, state.point.y, state.point.x),
    ):
        cost[start] = 0
        heapq.heappush(
            frontier,
            (heuristic(start), 0, start.layer, start.point.y, start.point.x, start),
        )
    came_from: dict[_RouteState, tuple[_RouteState, str | None]] = {}
    via_cache: dict[tuple[str, Point], tuple[bool, frozenset[str]]] = {}
    encountered_blockers: set[str] = set()
    states = 0
    while frontier:
        if states >= remaining_states:
            return _RouteSearchResult(
                None,
                states,
                True,
                frozenset(encountered_blockers),
            )
        _, current_cost, _, _, _, current = heapq.heappop(frontier)
        if current_cost != cost[current]:
            continue
        states += 1
        if current in targets:
            path_states = [current]
            path_vias: list[str | None] = []
            while path_states[-1] not in starts:
                previous, via_name = came_from[path_states[-1]]
                path_states.append(previous)
                path_vias.append(via_name)
            path_states.reverse()
            path_vias.reverse()
            return _RouteSearchResult(
                _RoutePath(tuple(path_states), tuple(path_vias)),
                states,
                False,
            )

        context = contexts[current.layer]
        neighbors: list[tuple[_RouteState, str | None]] = []
        for dx, dy in ((grid, 0), (0, grid), (-grid, 0), (0, -grid)):
            neighbor = _RouteState(
                current.layer,
                Point(current.point.x + dx, current.point.y + dy),
            )
            if not _move_allowed(current.point, neighbor.point, context):
                continue
            if not _point_in_context(neighbor.point, context):
                continue
            if _state_blocked(neighbor, center_blockers):
                encountered_blockers.update(
                    _blocking_nets(neighbor, center_blockers)
                )
                continue
            neighbors.append((neighbor, None))
        for next_layer, via in adjacency.get(current.layer, ()):
            neighbor = _RouteState(next_layer, current.point)
            if not _point_in_context(neighbor.point, contexts[next_layer]):
                continue
            cache_key = via.name, current.point
            cached = via_cache.get(cache_key)
            if cached is None:
                cached = _via_allowed(
                    domain,
                    via,
                    current.point,
                    contexts,
                    raw_blockers,
                )
                via_cache[cache_key] = cached
            allowed, blocking_nets = cached
            if allowed:
                neighbors.append((neighbor, via.name))
            else:
                encountered_blockers.update(blocking_nets)
        for neighbor, via_name in neighbors:
            congestion_demand = _edge_congestion_demand(
                domain,
                current,
                neighbor,
                via_name,
                congestion_demands,
            )
            neighbor_cost = current_cost + grid * (1 + congestion_demand)
            if neighbor_cost >= cost.get(neighbor, 2**63 - 1):
                continue
            cost[neighbor] = neighbor_cost
            came_from[neighbor] = current, via_name
            heapq.heappush(
                frontier,
                (
                    neighbor_cost + heuristic(neighbor),
                    neighbor_cost,
                    neighbor.layer,
                    neighbor.point.y,
                    neighbor.point.x,
                    neighbor,
                ),
            )
    return _RouteSearchResult(
        None,
        states,
        False,
        frozenset(encountered_blockers),
    )


def _segments(
    net: str,
    layer: str,
    width: int,
    points: tuple[Point, ...],
) -> tuple[RouteSegment, ...]:
    if len(points) < 2:
        return ()
    segments: list[RouteSegment] = []
    start = points[0]
    previous = points[0]
    direction = (
        points[1].x - points[0].x,
        points[1].y - points[0].y,
    )
    for point in points[1:]:
        next_direction = point.x - previous.x, point.y - previous.y
        if next_direction != direction:
            segments.append(RouteSegment(net, layer, start, previous, width))
            start = previous
            direction = next_direction
        previous = point
    segments.append(RouteSegment(net, layer, start, previous, width))
    return tuple(segment for segment in segments if segment.start != segment.end)


def _path_geometry(
    net: str,
    path: _RoutePath,
    contexts: dict[str, RoutingLayerDomain],
) -> tuple[tuple[RouteSegment, ...], tuple[RouteVia, ...]]:
    segments: list[RouteSegment] = []
    vias: list[RouteVia] = []
    planar_points = [path.states[0].point]
    planar_layer = path.states[0].layer
    for index, via_name in enumerate(path.transition_vias):
        next_state = path.states[index + 1]
        if via_name is None:
            planar_points.append(next_state.point)
            continue
        segments.extend(
            _segments(
                net,
                planar_layer,
                contexts[planar_layer].width,
                tuple(planar_points),
            )
        )
        vias.append(RouteVia(net, via_name, path.states[index].point))
        planar_layer = next_state.layer
        planar_points = [next_state.point]
    segments.extend(
        _segments(
            net,
            planar_layer,
            contexts[planar_layer].width,
            tuple(planar_points),
        )
    )
    return tuple(segments), tuple(vias)


def _route_length(route: NetRoute) -> int:
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in route.segments
    )


def _planar_path_legal(
    points: tuple[Point, ...],
    context: RoutingLayerDomain,
    blockers: dict[str, tuple[_Blocker, ...]],
    *,
    grid: int,
    maximum_route_states: int,
    route_states: int,
) -> tuple[bool, int, bool]:
    current_states = route_states
    for start, end in zip(points, points[1:]):
        dx = (end.x > start.x) - (end.x < start.x)
        dy = (end.y > start.y) - (end.y < start.y)
        current = start
        while current != end:
            if current_states >= maximum_route_states:
                return False, current_states, True
            neighbor = Point(
                current.x + dx * grid,
                current.y + dy * grid,
            )
            if not _move_allowed(current, neighbor, context):
                return False, current_states, False
            state = _RouteState(context.layer, neighbor)
            current_states += 1
            if not _point_in_context(neighbor, context) or _state_blocked(
                state,
                blockers,
            ):
                return False, current_states, False
            current = neighbor
    return True, current_states, False


def _compensate_route_length(
    route: NetRoute,
    target_length: int,
    *,
    contexts: dict[str, RoutingLayerDomain],
    blockers: dict[str, tuple[_Blocker, ...]],
    grid: int,
    maximum_route_states: int,
) -> _RouteCompensation:
    current_length = _route_length(route)
    delta = target_length - current_length
    if delta <= 0:
        return _RouteCompensation(route, 0)
    if delta % (2 * grid) != 0:
        return _RouteCompensation(None, 0)
    offset = delta // 2
    route_states = 0
    for index, segment in enumerate(route.segments):
        context = contexts[segment.layer]
        if segment.start.y == segment.end.y:
            offsets = ((0, -offset), (0, offset))
        else:
            offsets = ((-offset, 0), (offset, 0))
        for offset_x, offset_y in offsets:
            shifted_start = Point(
                segment.start.x + offset_x,
                segment.start.y + offset_y,
            )
            shifted_end = Point(
                segment.end.x + offset_x,
                segment.end.y + offset_y,
            )
            points = (
                segment.start,
                shifted_start,
                shifted_end,
                segment.end,
            )
            legal, route_states, exhausted = _planar_path_legal(
                points,
                context,
                blockers,
                grid=grid,
                maximum_route_states=maximum_route_states,
                route_states=route_states,
            )
            if exhausted:
                return _RouteCompensation(None, route_states, True)
            if not legal:
                continue
            replacement = _segments(
                route.net,
                segment.layer,
                segment.width_dbu,
                points,
            )
            compensated = NetRoute(
                route.net,
                route.segments[:index] + replacement + route.segments[index + 1 :],
                route.vias,
            )
            if _route_length(compensated) == target_length:
                return _RouteCompensation(compensated, route_states)
    return _RouteCompensation(None, route_states)


def _net_route_failure(
    net: str,
    status: ResultStatus,
    code: str,
    message: str,
    *,
    route_states: int = 0,
    blocking_nets: frozenset[str] = frozenset(),
) -> _NetRouteAttempt:
    return _NetRouteAttempt(
        status=status,
        route=None,
        diagnostic=Diagnostic(code, message, (net,)),
        route_states=route_states,
        blocking_nets=blocking_nets,
    )


def _route_net(
    problem: RoutingProblem,
    net: NetRoutingProblem,
    state: RoutingState,
    *,
    maximum_route_states: int,
) -> _NetRouteAttempt:
    domain = problem.domain
    contexts = problem.layers
    route_states = 0
    grid = domain.grid
    net_policy = problem.policy.for_net(net.name)
    allowed_layers = net_policy.allowed_layers
    net_contexts = {
        layer: context
        for layer, context in contexts.items()
        if allowed_layers is None or layer in allowed_layers
    }
    via_limit = net_policy.maximum_vias
    net_usable_vias = tuple(
        via
        for via in problem.vias
        if via.lower_layer in net_contexts
        and via.upper_layer in net_contexts
        and via_limit != 0
    )
    net_adjacency = _via_adjacency(net_usable_vias)
    accesses = net.terminal_accesses
    if any(not endpoint for endpoint in accesses):
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            "routing_pin_access_missing",
            f"net {net.name} has a terminal without access geometry",
        )
    pin_endpoint_states = tuple(
        _access_states(endpoint, net_contexts, grid) for endpoint in accesses
    )
    if any(not states for states in pin_endpoint_states):
        constrained = allowed_layers is not None
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED if constrained else ResultStatus.UNSUPPORTED,
            (
                "routing_layer_constraint_unsatisfied"
                if constrained
                else "routing_pin_access_layer_unsupported"
            ),
            f"net {net.name} has no access on a usable routing layer",
        )
    required_regions = net_policy.required_regions
    region_states = tuple(
        _access_states((region,), net_contexts, grid) for region in required_regions
    )
    if any(not states for states in region_states):
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            "routing_region_constraint_unsatisfied",
            (
                f"net {net.name} has a required region without a legal "
                "routing-resource access"
            ),
        )
    endpoint_states = pin_endpoint_states + region_states
    if not _layers_connect(endpoint_states, net_adjacency):
        constrained = (
            allowed_layers is not None
            or via_limit is not None
            or bool(required_regions)
        )
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED if constrained else ResultStatus.UNSUPPORTED,
            (
                "routing_constraint_infeasible"
                if constrained
                else "routing_layer_transition_unsupported"
            ),
            (
                f"net {net.name} access layers cannot be connected by supported "
                "via definitions and rules"
            ),
        )
    raw_blockers = _raw_blockers(net, state)
    center_blockers = _center_blockers(raw_blockers, net_contexts)
    legal_endpoint_states = tuple(
        tuple(
            endpoint_state
            for endpoint_state in states
            if not _state_blocked(endpoint_state, center_blockers)
        )
        for states in endpoint_states
    )
    if any(not states for states in legal_endpoint_states):
        blocked_index = next(
            index
            for index, states in enumerate(legal_endpoint_states)
            if not states
        )
        region_blocked = blocked_index >= len(pin_endpoint_states)
        blockers = frozenset(
            owner
            for endpoint_state in endpoint_states[blocked_index]
            for owner in _blocking_nets(endpoint_state, center_blockers)
        )
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            (
                "routing_region_constraint_blocked"
                if region_blocked
                else "routing_pin_access_blocked"
            ),
            (
                f"net {net.name} has a blocked required routing region"
                if region_blocked
                else f"net {net.name} has no unblocked terminal access"
            ),
            blocking_nets=blockers,
        )
    tree: set[_RouteState] = set(legal_endpoint_states[0])
    segments: list[RouteSegment] = []
    route_vias: list[RouteVia] = []
    for terminal_states in legal_endpoint_states[1:]:
        starts = frozenset(terminal_states)
        if starts & tree:
            tree.update(starts)
            continue
        search = _astar(
            starts,
            frozenset(tree),
            contexts=net_contexts,
            center_blockers=center_blockers,
            vias=net_usable_vias,
            raw_blockers=raw_blockers,
            congestion_demands=_route_search_costs(
                domain,
                state,
                congestion_weight=net_policy.cost.congestion_weight,
                history_weight=net_policy.cost.history_weight,
            ),
            domain=domain,
            remaining_states=maximum_route_states - route_states,
        )
        route_states += search.states
        if search.path is None:
            return _net_route_failure(
                net.name,
                (
                    ResultStatus.EXHAUSTED
                    if search.exhausted
                    else ResultStatus.FAILED
                ),
                (
                    "routing_search_exhausted"
                    if search.exhausted
                    else "routing_infeasible"
                ),
                f"reference router could not connect net {net.name}",
                route_states=route_states,
                blocking_nets=search.blocking_nets,
            )
        new_segments, new_vias = _path_geometry(
            net.name,
            search.path,
            net_contexts,
        )
        prospective_vias = tuple(dict.fromkeys((*route_vias, *new_vias)))
        if via_limit is not None and len(prospective_vias) > via_limit:
            return _net_route_failure(
                net.name,
                ResultStatus.FAILED,
                "routing_via_count_constraint_unsatisfied",
                (
                    f"net {net.name} cannot satisfy its maximum via count "
                    f"of {via_limit}"
                ),
                route_states=route_states,
            )
        segments.extend(new_segments)
        route_vias.extend(new_vias)
        mutable_blockers = {
            layer: list(shapes) for layer, shapes in raw_blockers.items()
        }
        for route_via in new_vias:
            via = problem.via_definitions[route_via.via_definition]
            mutable_blockers.setdefault(via.cut_layer, []).extend(
                _Blocker(translated_rect(shape, route_via.origin), None)
                for shape in via.cut_shapes
            )
        raw_blockers = {
            layer: tuple(shapes) for layer, shapes in mutable_blockers.items()
        }
        tree.update(search.path.states)
        tree.update(starts)
    route = NetRoute(
        net.name,
        tuple(segments),
        tuple(dict.fromkeys(route_vias)),
    )
    window = net_policy.length_window
    target_length = max(
        window.minimum_dbu,
        state.length_targets.get(net.name, 0),
    )
    current_length = _route_length(route)
    if window.maximum_dbu is not None and (
        current_length > window.maximum_dbu
        or target_length > window.maximum_dbu
    ):
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            "routing_length_window_infeasible",
            f"net {net.name} cannot satisfy its compiled route-length window",
            route_states=route_states,
        )
    if current_length < target_length:
        if (target_length - current_length) % (2 * grid) != 0:
            return _net_route_failure(
                net.name,
                ResultStatus.FAILED,
                "routing_length_window_infeasible",
                f"net {net.name} needs an unreachable Manhattan route length",
                route_states=route_states,
            )
        compensation = _compensate_route_length(
            route,
            target_length,
            contexts=net_contexts,
            blockers=center_blockers,
            grid=grid,
            maximum_route_states=maximum_route_states - route_states,
        )
        route_states += compensation.route_states
        if compensation.route is None:
            return _net_route_failure(
                net.name,
                (
                    ResultStatus.EXHAUSTED
                    if compensation.exhausted
                    else ResultStatus.UNSUPPORTED
                ),
                (
                    "routing_search_exhausted"
                    if compensation.exhausted
                    else "routing_length_compensation_unsupported"
                ),
                (
                    f"net {net.name} could not construct deterministic "
                    "length compensation"
                ),
                route_states=route_states,
            )
        route = compensation.route
    return _NetRouteAttempt(
        ResultStatus.SUCCEEDED,
        route,
        None,
        route_states,
    )


def _with_iteration_metrics(
    result: RoutingSolveResult,
    *,
    route_states: int,
    routing_iterations: int,
    route_attempts: int,
    ripped_net_count: int,
) -> RoutingSolveResult:
    metrics = tuple(
        metric for metric in result.report.metrics if metric.name != "route_states"
    ) + (
        Metric("route_states", route_states, "count"),
        Metric("routing_iterations", routing_iterations, "count"),
        Metric("routing_route_attempts", route_attempts, "count"),
        Metric("routing_ripped_net_count", ripped_net_count, "count"),
    )
    return RoutingSolveResult(
        status=result.status,
        routes=result.routes,
        report=StageReport(
            stage=result.report.stage,
            status=result.report.status,
            diagnostics=result.report.diagnostics,
            metrics=metrics,
        ),
        route_states=route_states,
        routing_iterations=routing_iterations,
        route_attempts=route_attempts,
        ripped_net_count=ripped_net_count,
    )


def _with_congestion_metrics(
    domain: RoutingDomain,
    result: RoutingSolveResult,
) -> RoutingSolveResult:
    congestion_metrics = _congestion_metrics(domain, result.routes)
    names = frozenset(metric.name for metric in congestion_metrics)
    return RoutingSolveResult(
        status=result.status,
        routes=result.routes,
        report=StageReport(
            stage=result.report.stage,
            status=result.report.status,
            diagnostics=result.report.diagnostics,
            metrics=tuple(
                metric for metric in result.report.metrics if metric.name not in names
            )
            + congestion_metrics,
        ),
        route_states=result.route_states,
        routing_iterations=result.routing_iterations,
        route_attempts=result.route_attempts,
        ripped_net_count=result.ripped_net_count,
    )


def _finish_negotiation(
    problem: RoutingProblem,
    state: RoutingState,
    status: ResultStatus,
    *,
    route_states: int,
    routing_iterations: int,
    route_attempts: int,
    ripped_net_count: int,
    diagnostic: Diagnostic | None = None,
) -> RoutingSolveResult:
    result = _result(
        status,
        routes=state.routes,
        code=None if diagnostic is None else diagnostic.code,
        message="" if diagnostic is None else diagnostic.message,
        entities=() if diagnostic is None else diagnostic.entities,
        route_states=route_states,
    )
    return _with_congestion_metrics(
        problem.domain,
        _with_iteration_metrics(
            result,
            route_states=route_states,
            routing_iterations=routing_iterations,
            route_attempts=route_attempts,
            ripped_net_count=ripped_net_count,
        ),
    )


def _reroute_order(
    problem: RoutingProblem,
    failed_net: str,
    affected_nets: frozenset[str],
) -> tuple[str, ...]:
    failed_scope = frozenset(problem.policy.reroute_scope(failed_net))
    failed_order = tuple(
        net for net in problem.policy.route_order if net in failed_scope
    )
    remaining_order = tuple(
        net
        for net in problem.policy.route_order
        if net in affected_nets and net not in failed_scope
    )
    if problem.policy.group_for_net(failed_net) is not None:
        return failed_order + remaining_order
    return (failed_net,) + remaining_order


def _length_closure_targets(
    problem: RoutingProblem,
    state: RoutingState,
    routed_net: str,
) -> tuple[RoutingGroupPolicy | None, dict[str, int], Diagnostic | None]:
    group = problem.policy.group_for_net(routed_net)
    if group is None or not group.length_matches:
        return None, {}, None
    matched_nets = frozenset(
        net for match in group.length_matches for net in match.nets
    )
    if not matched_nets.issubset(state.routes_by_net):
        return group, {}, None

    targets: dict[str, int] = {}
    for match in group.length_matches:
        lengths = {
            net: _route_length(state.routes_by_net[net]) for net in match.nets
        }
        longest = max(lengths.values())
        if longest - min(lengths.values()) <= match.maximum_skew_dbu:
            continue
        for net, length in lengths.items():
            if longest - length > match.maximum_skew_dbu:
                targets[net] = max(targets.get(net, 0), longest)
    for net, target in targets.items():
        maximum = problem.policy.for_net(net).length_window.maximum_dbu
        if maximum is not None and target > maximum:
            return group, {}, Diagnostic(
                "routing_group_length_infeasible",
                f"routing group {group.name} has no common feasible length target",
                group.nets,
            )
    return group, targets, None


def solve_routing(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingSolveResult:
    problem = compile_routing_problem(job, instance_placements)
    state = RoutingState.empty()
    if not problem.nets:
        return _finish_negotiation(
            problem,
            state,
            ResultStatus.SUCCEEDED,
            route_states=0,
            routing_iterations=1,
            route_attempts=0,
            ripped_net_count=0,
        )
    if problem.issue is not None:
        return _finish_negotiation(
            problem,
            state,
            problem.issue.status,
            route_states=0,
            routing_iterations=1,
            route_attempts=0,
            ripped_net_count=0,
            diagnostic=Diagnostic(
                problem.issue.code,
                "technology does not provide a usable routing domain",
            ),
        )

    maximum_route_states = job.execution_policy.maximum_route_states
    maximum_iterations = job.execution_policy.maximum_routing_iterations
    pending = problem.policy.route_order
    total_route_states = 0
    iteration = 1
    route_attempts = 0
    ripped_net_count = 0
    while pending:
        net_name, *remaining_pending = pending
        pending = tuple(remaining_pending)
        remaining_states = maximum_route_states - total_route_states
        if remaining_states <= 0:
            return _finish_negotiation(
                problem,
                state,
                ResultStatus.EXHAUSTED,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                diagnostic=Diagnostic(
                    "routing_search_exhausted",
                    (
                        "routing search consumed its state budget across "
                        "negotiation iterations"
                    ),
                    (net_name,),
                ),
            )
        route_attempts += 1
        attempt = _route_net(
            problem,
            problem.net(net_name),
            state,
            maximum_route_states=remaining_states,
        )
        total_route_states += attempt.route_states
        if attempt.status is ResultStatus.SUCCEEDED:
            if attempt.route is None:
                raise RuntimeError("successful net route attempt has no route")
            state = state.with_route(attempt.route, problem.via_definitions)
            group, length_targets, length_diagnostic = _length_closure_targets(
                problem,
                state,
                net_name,
            )
            if length_diagnostic is not None:
                return _finish_negotiation(
                    problem,
                    state,
                    ResultStatus.FAILED,
                    route_states=total_route_states,
                    routing_iterations=iteration,
                    route_attempts=route_attempts,
                    ripped_net_count=ripped_net_count,
                    diagnostic=length_diagnostic,
                )
            if length_targets:
                if group is None:
                    raise RuntimeError("length targets require a routing group")
                if iteration >= maximum_iterations:
                    return _finish_negotiation(
                        problem,
                        state,
                        ResultStatus.EXHAUSTED,
                        route_states=total_route_states,
                        routing_iterations=iteration,
                        route_attempts=route_attempts,
                        ripped_net_count=ripped_net_count,
                        diagnostic=Diagnostic(
                            "routing_iteration_exhausted",
                            (
                                "routing could not close group length within the "
                                "negotiation iteration budget"
                            ),
                            group.nets,
                        ),
                    )
                affected = frozenset(group.reroute_scope)
                ripped_net_count += len(affected & state.routes_by_net.keys())
                state = state.with_length_targets(length_targets).rip_up(
                    affected,
                    problem.via_definitions,
                )
                iteration += 1
                reroute = _reroute_order(problem, net_name, affected)
                pending = reroute + tuple(
                    net for net in pending if net not in affected
                )
            continue
        if attempt.status in (ResultStatus.UNSUPPORTED, ResultStatus.EXHAUSTED):
            return _finish_negotiation(
                problem,
                state,
                attempt.status,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                diagnostic=attempt.diagnostic,
            )

        victim = state.select_victim(attempt.blocking_nets)
        if victim is None:
            return _finish_negotiation(
                problem,
                state,
                ResultStatus.FAILED,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                diagnostic=attempt.diagnostic,
            )
        if iteration >= maximum_iterations:
            return _finish_negotiation(
                problem,
                state,
                ResultStatus.EXHAUSTED,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                diagnostic=Diagnostic(
                    "routing_iteration_exhausted",
                    (
                        "routing could not resolve an attributed conflict within "
                        "the negotiation iteration budget"
                    ),
                    (net_name, victim),
                ),
            )

        affected = frozenset(
            problem.policy.reroute_scope(net_name)
            + problem.policy.reroute_scope(victim)
        )
        routed_victims = tuple(
            state.routes_by_net[net]
            for net in sorted(affected & state.routes_by_net.keys())
        )
        ripped_net_count += len(routed_victims)
        history_penalty = _route_bin_demands(problem.domain, routed_victims)
        state = state.with_history_penalty(history_penalty).rip_up(
            affected,
            problem.via_definitions,
        )
        iteration += 1
        reroute = _reroute_order(problem, net_name, affected)
        pending = reroute + tuple(
            net for net in pending if net not in affected
        )

    return _finish_negotiation(
        problem,
        state,
        ResultStatus.SUCCEEDED,
        route_states=total_route_states,
        routing_iterations=iteration,
        route_attempts=route_attempts,
        ripped_net_count=ripped_net_count,
    )
