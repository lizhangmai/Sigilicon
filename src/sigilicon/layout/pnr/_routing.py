"""Deterministic Manhattan routing over normalized routing resources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import heapq
from itertools import islice, permutations
from types import MappingProxyType

from sigilicon.layout.pnr._geometry import (
    route_segment_shape,
    translated_rect,
    transformed_obstructions,
    transformed_pin_accesses,
    via_occurrence_shapes,
)
from sigilicon.layout.pnr._routing_policy import (
    RoutingPolicy,
    compile_routing_policy,
)
from sigilicon.layout.pnr.model import (
    Axis,
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
    RoutingTrackPattern,
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


@dataclass(frozen=True)
class _LayerContext:
    layer: str
    width: int
    spacing: int
    regions: tuple[Rect, ...]
    raw_regions: tuple[Rect, ...]
    gridless_regions: tuple[Rect, ...]
    horizontal_tracks: tuple[int, ...]
    vertical_tracks: tuple[int, ...]


@dataclass(frozen=True)
class _RoutingDomain:
    die: Rect
    grid: int
    congestion_bins_x: int
    congestion_bins_y: int
    route_rules: Mapping[str, tuple[int, int]]
    cut_spacings: Mapping[str, tuple[int, int]]

    def rules_for(self, layer: str) -> tuple[int, int] | None:
        return self.route_rules.get(layer)

    def cut_spacing_for(self, layer: str) -> tuple[int, int] | None:
        return self.cut_spacings.get(layer)


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


def _compile_routing_domain(job: PhysicalDesignJob) -> _RoutingDomain:
    widths: dict[str, list[int]] = {}
    spacings: dict[str, list[int]] = {}
    cut_spacings: dict[str, list[tuple[int, int]]] = {}
    for rule in job.technology.rules:
        if isinstance(rule, MinimumWidthRule):
            widths.setdefault(rule.layer, []).append(rule.width_dbu)
        elif isinstance(rule, MinimumSpacingRule):
            spacings.setdefault(rule.layer, []).append(rule.spacing_dbu)
        elif isinstance(rule, CutSpacingRule):
            cut_spacings.setdefault(rule.cut_layer, []).append(
                (rule.spacing_x_dbu, rule.spacing_y_dbu)
            )
    route_rules = {
        layer: (max(layer_widths), max(spacings[layer]))
        for layer, layer_widths in widths.items()
        if layer in spacings
    }
    normalized_cut_spacings = {
        layer: (
            max(spacing[0] for spacing in layer_spacings),
            max(spacing[1] for spacing in layer_spacings),
        )
        for layer, layer_spacings in cut_spacings.items()
    }
    return _RoutingDomain(
        die=job.design.die,
        grid=job.technology.manufacturing_grid_dbu,
        congestion_bins_x=job.request.routing_congestion_bins_x,
        congestion_bins_y=job.request.routing_congestion_bins_y,
        route_rules=MappingProxyType(route_rules),
        cut_spacings=MappingProxyType(normalized_cut_spacings),
    )


def _routing_contexts(
    job: PhysicalDesignJob,
    domain: _RoutingDomain,
) -> tuple[dict[str, _LayerContext], dict[str, tuple[Rect, ...]], str | None]:
    resources = tuple(
        sorted(
            (
                resource
                for resource in job.technology.routing_resources
                if isinstance(
                    resource,
                    (GridlessRoutingResource, RoutingTrackPattern),
                )
            ),
            key=lambda resource: (
                resource.layer,
                type(resource).__name__,
                resource.name,
            ),
        )
    )
    if not resources:
        return {}, {}, "routing_resource_required"
    grid = domain.grid
    grouped: dict[
        str,
        list[GridlessRoutingResource | RoutingTrackPattern],
    ] = {}
    for resource in resources:
        grouped.setdefault(resource.layer, []).append(resource)
    contexts: dict[str, _LayerContext] = {}
    routing_regions: dict[str, tuple[Rect, ...]] = {}
    for layer, layer_resources in sorted(grouped.items()):
        rules = domain.rules_for(layer)
        if rules is None:
            return {}, {}, "routing_rule_capability_missing"
        width, spacing = rules
        if width % (2 * grid) != 0:
            return {}, {}, "routing_width_resolution_unsupported"
        gridless_resources = tuple(
            resource
            for resource in layer_resources
            if isinstance(resource, GridlessRoutingResource)
        )
        track_resources = tuple(
            resource
            for resource in layer_resources
            if isinstance(resource, RoutingTrackPattern)
        )
        gridless_regions = tuple(
            region
            for resource in gridless_resources
            if (
                region := (resource.region or job.design.die).intersection(
                    job.design.die
                )
            )
            is not None
        )
        raw_regions = gridless_regions + (
            (job.design.die,) if track_resources else ()
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
            for region in gridless_regions
            if region.width > width and region.height > width
        )
        horizontal_tracks = tuple(
            sorted(
                {
                    resource.start_dbu + resource.pitch_dbu * index
                    for resource in track_resources
                    if resource.axis is Axis.Y
                    for index in range(resource.count)
                    if job.design.die.y_min + margin
                    <= resource.start_dbu + resource.pitch_dbu * index
                    <= job.design.die.y_max - margin
                }
            )
        )
        vertical_tracks = tuple(
            sorted(
                {
                    resource.start_dbu + resource.pitch_dbu * index
                    for resource in track_resources
                    if resource.axis is Axis.X
                    for index in range(resource.count)
                    if job.design.die.x_min + margin
                    <= resource.start_dbu + resource.pitch_dbu * index
                    <= job.design.die.x_max - margin
                }
            )
        )
        if center_regions or horizontal_tracks or vertical_tracks:
            contexts[layer] = _LayerContext(
                layer=layer,
                width=width,
                spacing=spacing,
                regions=center_regions,
                raw_regions=raw_regions,
                gridless_regions=gridless_regions,
                horizontal_tracks=horizontal_tracks,
                vertical_tracks=vertical_tracks,
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
    conductor = Rect(
        point.x - margin,
        point.y - margin,
        point.x + margin,
        point.y + margin,
    )
    return _rect_covered_by_regions(conductor, context.gridless_regions) or (
        _rect_covered_by_regions(conductor, context.raw_regions)
        and (
            point.y in context.horizontal_tracks
            or point.x in context.vertical_tracks
        )
    )


def _move_allowed(
    current: Point,
    neighbor: Point,
    context: _LayerContext,
) -> bool:
    margin = context.width // 2
    movement = Rect(
        min(current.x, neighbor.x) - margin,
        min(current.y, neighbor.y) - margin,
        max(current.x, neighbor.x) + margin,
        max(current.y, neighbor.y) + margin,
    )
    if _rect_covered_by_regions(movement, context.gridless_regions):
        return True
    if current.y == neighbor.y:
        return current.y in context.horizontal_tracks
    return current.x in context.vertical_tracks


def _bin_index(value: int, low: int, span: int, bins: int) -> int:
    return min(bins - 1, max(0, (value - low) * bins // span))


def _route_bin_demands(
    domain: _RoutingDomain,
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


def _congestion_metrics(
    domain: _RoutingDomain,
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
            blockers.setdefault(segment.layer, []).append(route_segment_shape(segment))
        for route_via in route.vias:
            for layer, shape in via_occurrence_shapes(
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
    domain: _RoutingDomain,
    via: ViaDefinition,
    contexts: dict[str, _LayerContext],
) -> bool:
    if via.lower_layer not in contexts or via.upper_layer not in contexts:
        return False
    cut_spacing = domain.cut_spacing_for(via.cut_layer)
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
    domain: _RoutingDomain,
    contexts: dict[str, _LayerContext],
) -> tuple[ViaDefinition, ...]:
    return tuple(
        via
        for via in sorted(job.technology.via_definitions, key=lambda item: item.name)
        if _via_definition_supported(job, domain, via, contexts)
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
    domain: _RoutingDomain,
    via: ViaDefinition,
    origin: Point,
    contexts: dict[str, _LayerContext],
    routing_regions: dict[str, tuple[Rect, ...]],
    raw_blockers: dict[str, tuple[Rect, ...]],
) -> bool:
    translated_shapes = via_occurrence_shapes(via, origin)
    for layer, shape in translated_shapes:
        if not domain.die.contains(shape):
            return False
        if layer in contexts and not _rect_covered_by_regions(
            shape,
            routing_regions[layer],
        ):
            return False
        if layer in contexts:
            spacing_x = spacing_y = contexts[layer].spacing
        else:
            cut_spacing = domain.cut_spacing_for(layer)
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


def _edge_congestion_demand(
    domain: _RoutingDomain,
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
    contexts: dict[str, _LayerContext],
    center_blockers: dict[str, tuple[Rect, ...]],
    vias: tuple[ViaDefinition, ...],
    routing_regions: dict[str, tuple[Rect, ...]],
    raw_blockers: dict[str, tuple[Rect, ...]],
    congestion_demands: dict[tuple[str, int, int, str], int],
    domain: _RoutingDomain,
    remaining_states: int,
) -> tuple[_RoutePath | None, int, bool]:
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
            if not _move_allowed(current.point, neighbor.point, context):
                continue
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
                    domain,
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


def _solve_routing_once(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
    *,
    domain: _RoutingDomain,
    policy: RoutingPolicy,
    net_order: tuple[str, ...] | None = None,
    maximum_route_states: int | None = None,
) -> RoutingSolveResult:
    if not job.design.nets:
        return _result(ResultStatus.SUCCEEDED)
    contexts, routing_regions, context_error = _routing_contexts(job, domain)
    if context_error is not None:
        status = (
            ResultStatus.FAILED
            if context_error == "routing_region_empty"
            else ResultStatus.UNSUPPORTED
        )
        return _result(
            status,
            code=context_error,
            message="technology does not provide a usable routing domain",
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
    usable_vias = _usable_vias(job, domain, contexts)
    via_definitions = {
        via.name: via for via in job.technology.via_definitions
    }
    all_routes: list[NetRoute] = []
    route_states = 0
    grid = domain.grid

    nets = {net.name: net for net in job.design.nets}
    ordered_nets = (
        tuple(nets[name] for name in net_order)
        if net_order is not None
        else tuple(sorted(job.design.nets, key=lambda item: item.name))
    )
    route_state_limit = (
        maximum_route_states
        if maximum_route_states is not None
        else job.request.maximum_route_states
    )
    for net in ordered_nets:
        net_policy = policy.for_net(net.name)
        allowed_layers = net_policy.allowed_layers
        net_contexts = {
            layer: context
            for layer, context in contexts.items()
            if allowed_layers is None or layer in allowed_layers
        }
        via_limit = net_policy.maximum_vias
        net_usable_vias = tuple(
            via
            for via in usable_vias
            if via.lower_layer in net_contexts
            and via.upper_layer in net_contexts
            and via_limit != 0
        )
        net_adjacency = _via_adjacency(net_usable_vias)
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
        pin_endpoint_states = tuple(
            _access_states(endpoint, net_contexts, grid) for endpoint in accesses
        )
        if any(not states for states in pin_endpoint_states):
            return _result(
                (
                    ResultStatus.FAILED
                    if allowed_layers is not None
                    else ResultStatus.UNSUPPORTED
                ),
                routes=tuple(all_routes),
                code=(
                    "routing_layer_constraint_unsatisfied"
                    if allowed_layers is not None
                    else "routing_pin_access_layer_unsupported"
                ),
                message=f"net {net.name} has no access on a usable routing layer",
                entities=(net.name,),
                route_states=route_states,
            )
        required_regions = net_policy.required_regions
        region_states = tuple(
            _access_states((region,), net_contexts, grid)
            for region in required_regions
        )
        if any(not states for states in region_states):
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code="routing_region_constraint_unsatisfied",
                message=(
                    f"net {net.name} has a required region without a legal "
                    "routing-resource access"
                ),
                entities=(net.name,),
                route_states=route_states,
            )
        endpoint_states = pin_endpoint_states + region_states
        if not _layers_connect(endpoint_states, net_adjacency):
            return _result(
                (
                    ResultStatus.FAILED
                    if allowed_layers is not None
                    or via_limit is not None
                    or required_regions
                    else ResultStatus.UNSUPPORTED
                ),
                routes=tuple(all_routes),
                code=(
                    "routing_constraint_infeasible"
                    if allowed_layers is not None
                    or via_limit is not None
                    or required_regions
                    else "routing_layer_transition_unsupported"
                ),
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
        center_blockers = _center_blockers(raw_blockers, net_contexts)
        legal_endpoint_states = tuple(
            tuple(
                state
                for state in states
                if not _state_blocked(state, center_blockers)
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
            return _result(
                ResultStatus.FAILED,
                routes=tuple(all_routes),
                code=(
                    "routing_region_constraint_blocked"
                    if region_blocked
                    else "routing_pin_access_blocked"
                ),
                message=(
                    f"net {net.name} has a blocked required routing region"
                    if region_blocked
                    else f"net {net.name} has no unblocked terminal access"
                ),
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
                contexts=net_contexts,
                center_blockers=center_blockers,
                vias=net_usable_vias,
                routing_regions=routing_regions,
                raw_blockers=raw_blockers,
                congestion_demands=(
                    _route_bin_demands(domain, tuple(all_routes))
                    if net_policy.congestion_cost_enabled
                    else {}
                ),
                domain=domain,
                remaining_states=route_state_limit - route_states,
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
            new_segments, new_vias = _path_geometry(
                net.name,
                path,
                net_contexts,
            )
            prospective_vias = tuple(dict.fromkeys((*route_vias, *new_vias)))
            if via_limit is not None and len(prospective_vias) > via_limit:
                return _result(
                    ResultStatus.FAILED,
                    routes=tuple(all_routes),
                    code="routing_via_count_constraint_unsatisfied",
                    message=(
                        f"net {net.name} cannot satisfy its maximum via count "
                        f"of {via_limit}"
                    ),
                    entities=(net.name,),
                    route_states=route_states,
                )
            segments.extend(new_segments)
            route_vias.extend(new_vias)
            mutable_blockers = {
                layer: list(shapes) for layer, shapes in raw_blockers.items()
            }
            for route_via in new_vias:
                via = via_definitions[route_via.via_definition]
                mutable_blockers.setdefault(via.cut_layer, []).extend(
                    translated_rect(shape, route_via.origin)
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


def _with_iteration_metrics(
    result: RoutingSolveResult,
    *,
    route_states: int,
    routing_iterations: int,
) -> RoutingSolveResult:
    metrics = tuple(
        metric for metric in result.report.metrics if metric.name != "route_states"
    ) + (
        Metric("route_states", route_states, "count"),
        Metric("routing_iterations", routing_iterations, "count"),
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
    )


def _with_congestion_metrics(
    domain: _RoutingDomain,
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
    )


def solve_routing(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingSolveResult:
    net_names = tuple(sorted(net.name for net in job.design.nets))
    domain = _compile_routing_domain(job)
    policy = compile_routing_policy(job.routing_constraints, net_names)
    if len(net_names) < 2:
        result = _solve_routing_once(
            job,
            instance_placements,
            domain=domain,
            policy=policy,
        )
        return _with_congestion_metrics(
            domain,
            _with_iteration_metrics(
                result,
                route_states=result.route_states,
                routing_iterations=1,
            ),
        )

    order_limit = job.request.maximum_routing_iterations
    candidate_orders = tuple(
        islice(permutations(net_names), order_limit + 1)
    )
    attempted_orders = candidate_orders[:order_limit]
    total_route_states = 0
    last_result: RoutingSolveResult | None = None
    for iteration, net_order in enumerate(attempted_orders, start=1):
        remaining_states = job.request.maximum_route_states - total_route_states
        if remaining_states <= 0:
            return _with_congestion_metrics(
                domain,
                _with_iteration_metrics(
                    _result(
                        ResultStatus.EXHAUSTED,
                        code="routing_search_exhausted",
                        message=(
                            "routing search consumed its state budget across "
                            "rip-up iterations"
                        ),
                        route_states=total_route_states,
                    ),
                    route_states=total_route_states,
                    routing_iterations=iteration - 1,
                ),
            )
        result = _solve_routing_once(
            job,
            instance_placements,
            domain=domain,
            policy=policy,
            net_order=net_order,
            maximum_route_states=remaining_states,
        )
        total_route_states += result.route_states
        result = _with_iteration_metrics(
            result,
            route_states=total_route_states,
            routing_iterations=iteration,
        )
        if result.status is ResultStatus.SUCCEEDED:
            return _with_congestion_metrics(domain, result)
        if result.status in (ResultStatus.UNSUPPORTED, ResultStatus.EXHAUSTED):
            return _with_congestion_metrics(domain, result)
        last_result = result

    if len(candidate_orders) > order_limit:
        return _with_congestion_metrics(
            domain,
            _with_iteration_metrics(
                _result(
                    ResultStatus.EXHAUSTED,
                    code="routing_iteration_exhausted",
                    message=(
                        "routing could not complete within the rip-up iteration budget"
                    ),
                    route_states=total_route_states,
                ),
                route_states=total_route_states,
                routing_iterations=len(attempted_orders),
            ),
        )
    if last_result is None:
        raise RuntimeError("routing order search produced no result")
    return _with_congestion_metrics(domain, last_result)
