"""Deterministic gridless Manhattan reference routing."""

from __future__ import annotations

from dataclasses import dataclass
import heapq

from sigilicon.layout.pnr._geometry import (
    transformed_obstructions,
    transformed_pin_accesses,
)
from sigilicon.layout.pnr.model import (
    CutSpacingRule,
    Diagnostic,
    EnclosureRule,
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
    RouteVia,
    StageReport,
    ViaDefinition,
)


@dataclass(frozen=True)
class RoutingSolveResult:
    status: ResultStatus
    routes: tuple[NetRoute, ...]
    report: StageReport


@dataclass(frozen=True)
class _LayerContext:
    layer: str
    width: int
    spacing: int
    regions: tuple[Rect, ...]
    raw_regions: tuple[Rect, ...]


@dataclass(frozen=True)
class _RouteState:
    layer: str
    point: Point


@dataclass(frozen=True)
class _RoutePath:
    states: tuple[_RouteState, ...]
    transition_vias: tuple[str | None, ...]


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


def _routing_contexts(
    job: PhysicalDesignJob,
) -> tuple[dict[str, _LayerContext], dict[str, tuple[Rect, ...]], str | None]:
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
    if not resources:
        return {}, {}, "routing_gridless_resource_required"
    grid = job.technology.manufacturing_grid_dbu
    grouped: dict[str, list[GridlessRoutingResource]] = {}
    for resource in resources:
        grouped.setdefault(resource.layer, []).append(resource)
    contexts: dict[str, _LayerContext] = {}
    routing_regions: dict[str, tuple[Rect, ...]] = {}
    for layer, layer_resources in sorted(grouped.items()):
        rules = _route_rules(job, layer)
        if rules is None:
            return {}, {}, "routing_rule_capability_missing"
        width, spacing = rules
        if width % (2 * grid) != 0:
            return {}, {}, "routing_width_resolution_unsupported"
        raw_regions = tuple(
            region
            for resource in layer_resources
            if (
                region := (resource.region or job.design.die).intersection(
                    job.design.die
                )
            )
            is not None
        )
        routing_regions[layer] = raw_regions
        margin = width // 2
        center_regions = tuple(
            Rect(
                region.x_min + margin,
                region.y_min + margin,
                region.x_max - margin,
                region.y_max - margin,
            )
            for region in raw_regions
            if region.width > width and region.height > width
        )
        if center_regions:
            contexts[layer] = _LayerContext(
                layer=layer,
                width=width,
                spacing=spacing,
                regions=center_regions,
                raw_regions=raw_regions,
            )
    if not contexts:
        return {}, routing_regions, "routing_region_empty"
    return contexts, routing_regions, None


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
    contexts: dict[str, _LayerContext],
    grid: int,
) -> tuple[_RouteState, ...]:
    states = {
        _RouteState(layer, point)
        for layer, context in contexts.items()
        for region in context.regions
        if (
            point := _access_point(
                accesses,
                layer=layer,
                width=context.width,
                grid=grid,
                region=region,
            )
        )
        is not None
    }
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


def _translated(rectangle: Rect, origin: Point) -> Rect:
    return Rect(
        rectangle.x_min + origin.x,
        rectangle.y_min + origin.y,
        rectangle.x_max + origin.x,
        rectangle.y_max + origin.y,
    )


def _point_in_interior(point: Point, rectangle: Rect) -> bool:
    return (
        rectangle.x_min < point.x < rectangle.x_max
        and rectangle.y_min < point.y < rectangle.y_max
    )


def _rect_covered_by_regions(shape: Rect, regions: tuple[Rect, ...]) -> bool:
    x_breaks = sorted(
        {shape.x_min, shape.x_max}
        | {
            coordinate
            for region in regions
            for coordinate in (region.x_min, region.x_max)
            if shape.x_min < coordinate < shape.x_max
        }
    )
    for x_min, x_max in zip(x_breaks, x_breaks[1:]):
        intervals = sorted(
            (
                max(shape.y_min, region.y_min),
                min(shape.y_max, region.y_max),
            )
            for region in regions
            if region.x_min <= x_min
            and x_max <= region.x_max
            and region.y_min < shape.y_max
            and shape.y_min < region.y_max
        )
        covered_to = shape.y_min
        for y_min, y_max in intervals:
            if y_min > covered_to:
                break
            covered_to = max(covered_to, y_max)
            if covered_to >= shape.y_max:
                break
        if covered_to < shape.y_max:
            return False
    return bool(x_breaks)


def _point_in_context(point: Point, context: _LayerContext) -> bool:
    margin = context.width // 2
    return _rect_covered_by_regions(
        Rect(
            point.x - margin,
            point.y - margin,
            point.x + margin,
            point.y + margin,
        ),
        context.raw_regions,
    )


def _wire_rectangle(segment: RouteSegment) -> Rect:
    margin = segment.width_dbu // 2
    if segment.start.y == segment.end.y:
        return Rect(
            min(segment.start.x, segment.end.x),
            segment.start.y - margin,
            max(segment.start.x, segment.end.x),
            segment.start.y + margin,
        )
    return Rect(
        segment.start.x - margin,
        min(segment.start.y, segment.end.y),
        segment.start.x + margin,
        max(segment.start.y, segment.end.y),
    )


def _via_shapes(
    via: ViaDefinition,
    origin: Point,
) -> tuple[tuple[str, Rect], ...]:
    return tuple(
        (layer, _translated(shape, origin))
        for layer, shapes in (
            (via.lower_layer, via.lower_shapes),
            (via.cut_layer, via.cut_shapes),
            (via.upper_layer, via.upper_shapes),
        )
        for shape in shapes
    )


def _raw_blockers(
    job: PhysicalDesignJob,
    placements: dict[str, Placement],
    net_references: frozenset[PinReference],
    all_pin_references: tuple[PinReference, ...],
    prior_routes: tuple[NetRoute, ...],
) -> dict[str, tuple[Rect, ...]]:
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    blockers: dict[str, list[Rect]] = {}
    for instance_name, placement in placements.items():
        for obstruction in transformed_obstructions(
            masters[instances[instance_name].master],
            placement,
        ):
            blockers.setdefault(obstruction.layer, []).append(obstruction.shape)
    for reference in all_pin_references:
        if reference in net_references:
            continue
        for access in _endpoint_accesses(job, reference, placements):
            blockers.setdefault(access.layer, []).append(access.shape)
    vias = {via.name: via for via in job.technology.via_definitions}
    for route in prior_routes:
        for segment in route.segments:
            blockers.setdefault(segment.layer, []).append(_wire_rectangle(segment))
        for route_via in route.vias:
            for layer, shape in _via_shapes(
                vias[route_via.via_definition],
                route_via.origin,
            ):
                blockers.setdefault(layer, []).append(shape)
    return {layer: tuple(shapes) for layer, shapes in blockers.items()}


def _center_blockers(
    raw_blockers: dict[str, tuple[Rect, ...]],
    contexts: dict[str, _LayerContext],
) -> dict[str, tuple[Rect, ...]]:
    return {
        layer: tuple(
            _expanded(shape, context.width // 2 + context.spacing)
            for shape in raw_blockers.get(layer, ())
        )
        for layer, context in contexts.items()
    }


def _state_blocked(
    state: _RouteState,
    blockers: dict[str, tuple[Rect, ...]],
) -> bool:
    return any(
        _point_in_interior(state.point, rectangle)
        for rectangle in blockers.get(state.layer, ())
    )


def _cut_spacing(job: PhysicalDesignJob, layer: str) -> tuple[int, int] | None:
    rules = tuple(
        rule
        for rule in job.technology.rules
        if isinstance(rule, CutSpacingRule) and rule.cut_layer == layer
    )
    if not rules:
        return None
    return (
        max(rule.spacing_x_dbu for rule in rules),
        max(rule.spacing_y_dbu for rule in rules),
    )


def _shape_enclosed(
    inner: Rect,
    outers: tuple[Rect, ...],
    enclosure_x: int,
    enclosure_y: int,
) -> bool:
    return any(
        outer.x_min <= inner.x_min - enclosure_x
        and outer.y_min <= inner.y_min - enclosure_y
        and inner.x_max + enclosure_x <= outer.x_max
        and inner.y_max + enclosure_y <= outer.y_max
        for outer in outers
    )


def _via_definition_supported(
    job: PhysicalDesignJob,
    via: ViaDefinition,
    contexts: dict[str, _LayerContext],
) -> bool:
    if via.lower_layer not in contexts or via.upper_layer not in contexts:
        return False
    cut_spacing = _cut_spacing(job, via.cut_layer)
    if cut_spacing is None:
        return False
    if any(
        _rectangles_too_close(
            first,
            second,
            cut_spacing[0],
            cut_spacing[1],
        )
        for index, first in enumerate(via.cut_shapes)
        for second in via.cut_shapes[index + 1 :]
    ):
        return False
    origin = Point(0, 0)
    if not any(
        shape.x_min <= origin.x <= shape.x_max
        and shape.y_min <= origin.y <= shape.y_max
        for shape in via.lower_shapes
    ):
        return False
    if not any(
        shape.x_min <= origin.x <= shape.x_max
        and shape.y_min <= origin.y <= shape.y_max
        for shape in via.upper_shapes
    ):
        return False
    for layer, shapes in (
        (via.lower_layer, via.lower_shapes),
        (via.upper_layer, via.upper_shapes),
    ):
        width = contexts[layer].width
        if any(shape.width < width or shape.height < width for shape in shapes):
            return False
        enclosure_rules = tuple(
            rule
            for rule in job.technology.rules
            if isinstance(rule, EnclosureRule)
            and rule.outer_layer == layer
            and rule.inner_layer == via.cut_layer
        )
        if not enclosure_rules:
            return False
        enclosure_x = max(rule.enclosure_x_dbu for rule in enclosure_rules)
        enclosure_y = max(rule.enclosure_y_dbu for rule in enclosure_rules)
        if any(
            not _shape_enclosed(
                cut,
                shapes,
                enclosure_x,
                enclosure_y,
            )
            for cut in via.cut_shapes
        ):
            return False
    return True


def _usable_vias(
    job: PhysicalDesignJob,
    contexts: dict[str, _LayerContext],
) -> tuple[ViaDefinition, ...]:
    return tuple(
        via
        for via in sorted(job.technology.via_definitions, key=lambda item: item.name)
        if _via_definition_supported(job, via, contexts)
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
    job: PhysicalDesignJob,
    via: ViaDefinition,
    origin: Point,
    contexts: dict[str, _LayerContext],
    routing_regions: dict[str, tuple[Rect, ...]],
    raw_blockers: dict[str, tuple[Rect, ...]],
) -> bool:
    translated_shapes = _via_shapes(via, origin)
    for layer, shape in translated_shapes:
        if not job.design.die.contains(shape):
            return False
        if layer in contexts and not _rect_covered_by_regions(
            shape,
            routing_regions[layer],
        ):
            return False
        if layer in contexts:
            spacing_x = spacing_y = contexts[layer].spacing
        else:
            cut_spacing = _cut_spacing(job, layer)
            if cut_spacing is None:
                return False
            spacing_x, spacing_y = cut_spacing
        if any(
            _rectangles_too_close(
                shape,
                blocker,
                spacing_x,
                spacing_y,
            )
            for blocker in raw_blockers.get(layer, ())
        ):
            return False
    return True


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


def _astar(
    starts: frozenset[_RouteState],
    targets: frozenset[_RouteState],
    *,
    contexts: dict[str, _LayerContext],
    center_blockers: dict[str, tuple[Rect, ...]],
    vias: tuple[ViaDefinition, ...],
    routing_regions: dict[str, tuple[Rect, ...]],
    raw_blockers: dict[str, tuple[Rect, ...]],
    job: PhysicalDesignJob,
    remaining_states: int,
) -> tuple[_RoutePath | None, int, bool]:
    def heuristic(state: _RouteState) -> int:
        return min(
            abs(state.point.x - target.point.x)
            + abs(state.point.y - target.point.y)
            for target in targets
        )

    grid = job.technology.manufacturing_grid_dbu
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
    via_cache: dict[tuple[str, Point], bool] = {}
    states = 0
    while frontier:
        if states >= remaining_states:
            return None, states, True
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
            return _RoutePath(tuple(path_states), tuple(path_vias)), states, False

        context = contexts[current.layer]
        neighbors: list[tuple[_RouteState, str | None]] = []
        for dx, dy in ((grid, 0), (0, grid), (-grid, 0), (0, -grid)):
            neighbor = _RouteState(
                current.layer,
                Point(current.point.x + dx, current.point.y + dy),
            )
            if not _point_in_context(neighbor.point, context):
                continue
            if _state_blocked(neighbor, center_blockers):
                continue
            neighbors.append((neighbor, None))
        for next_layer, via in adjacency.get(current.layer, ()):
            neighbor = _RouteState(next_layer, current.point)
            if not _point_in_context(neighbor.point, contexts[next_layer]):
                continue
            cache_key = via.name, current.point
            allowed = via_cache.get(cache_key)
            if allowed is None:
                allowed = _via_allowed(
                    job,
                    via,
                    current.point,
                    contexts,
                    routing_regions,
                    raw_blockers,
                )
                via_cache[cache_key] = allowed
            if allowed:
                neighbors.append((neighbor, via.name))
        for neighbor, via_name in neighbors:
            neighbor_cost = current_cost + grid
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
    return None, states, False


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
    contexts: dict[str, _LayerContext],
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


def solve_routing(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingSolveResult:
    if not job.design.nets:
        return _result(ResultStatus.SUCCEEDED)
    contexts, routing_regions, context_error = _routing_contexts(job)
    if context_error is not None:
        status = (
            ResultStatus.FAILED
            if context_error == "routing_region_empty"
            else ResultStatus.UNSUPPORTED
        )
        return _result(
            status,
            code=context_error,
            message="technology does not provide a usable gridless routing domain",
        )
    placements = {
        item.instance: item.placement for item in instance_placements
    }
    masters = {master.name: master for master in job.design.masters}
    all_pin_references = tuple(
        PinReference(port.name) for port in job.design.ports
    ) + tuple(
        PinReference(pin.name, instance.name)
        for instance in job.design.instances
        for pin in masters[instance.master].pins
    )
    usable_vias = _usable_vias(job, contexts)
    via_definitions = {
        via.name: via for via in job.technology.via_definitions
    }
    adjacency = _via_adjacency(usable_vias)
    all_routes: list[NetRoute] = []
    route_states = 0
    grid = job.technology.manufacturing_grid_dbu

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
        endpoint_states = tuple(
            _access_states(endpoint, contexts, grid) for endpoint in accesses
        )
        if any(not states for states in endpoint_states):
            return _result(
                ResultStatus.UNSUPPORTED,
                routes=tuple(all_routes),
                code="routing_pin_access_layer_unsupported",
                message=f"net {net.name} has no access on a usable gridless layer",
                entities=(net.name,),
                route_states=route_states,
            )
        if not _layers_connect(endpoint_states, adjacency):
            return _result(
                ResultStatus.UNSUPPORTED,
                routes=tuple(all_routes),
                code="routing_layer_transition_unsupported",
                message=(
                    f"net {net.name} access layers cannot be connected by supported "
                    "via definitions and rules"
                ),
                entities=(net.name,),
                route_states=route_states,
            )
        raw_blockers = _raw_blockers(
            job,
            placements,
            frozenset(net.pins),
            all_pin_references,
            tuple(all_routes),
        )
        center_blockers = _center_blockers(raw_blockers, contexts)
        legal_endpoint_states = tuple(
            tuple(
                state
                for state in states
                if not _state_blocked(state, center_blockers)
            )
            for states in endpoint_states
        )
        if any(not states for states in legal_endpoint_states):
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code="routing_pin_access_blocked",
                message=f"net {net.name} has no unblocked terminal access",
                entities=(net.name,),
                route_states=route_states,
            )
        tree: set[_RouteState] = set(legal_endpoint_states[0])
        segments: list[RouteSegment] = []
        route_vias: list[RouteVia] = []
        for terminal_states in legal_endpoint_states[1:]:
            starts = frozenset(terminal_states)
            if starts & tree:
                tree.update(starts)
                continue
            path, states, exhausted = _astar(
                starts,
                frozenset(tree),
                contexts=contexts,
                center_blockers=center_blockers,
                vias=usable_vias,
                routing_regions=routing_regions,
                raw_blockers=raw_blockers,
                job=job,
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
            new_segments, new_vias = _path_geometry(net.name, path, contexts)
            segments.extend(new_segments)
            route_vias.extend(new_vias)
            mutable_blockers = {
                layer: list(shapes) for layer, shapes in raw_blockers.items()
            }
            for route_via in new_vias:
                via = via_definitions[route_via.via_definition]
                mutable_blockers.setdefault(via.cut_layer, []).extend(
                    _translated(shape, route_via.origin)
                    for shape in via.cut_shapes
                )
            raw_blockers = {
                layer: tuple(shapes)
                for layer, shapes in mutable_blockers.items()
            }
            tree.update(path.states)
            tree.update(starts)
        all_routes.append(
            NetRoute(
                net.name,
                tuple(segments),
                tuple(dict.fromkeys(route_vias)),
            )
        )

    return _result(
        ResultStatus.SUCCEEDED,
        routes=tuple(all_routes),
        route_states=route_states,
    )
