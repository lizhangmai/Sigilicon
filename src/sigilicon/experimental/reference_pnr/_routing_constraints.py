"""Validation and evaluation for typed Routing Constraints."""

from __future__ import annotations

from sigilicon.layout.physical_geometry import (
    route_segment_shape,
    transformed_pin_accesses,
    via_occurrence_shapes,
)
from sigilicon.experimental.reference_pnr.model import (
    ConstraintOutcome,
    ConstraintStatus,
    InstancePlacement,
    LayerShape,
    NetRoute,
    PhysicalDesignJob,
    PinReference,
    Placement,
    Point,
    Rect,
    RouteSegment,
    RouteVia,
    RoutingConstraint,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
    RoutingRegionConstraint,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingViaCountConstraint,
    ViaDefinition,
)


def validate_routing_constraint(
    constraint: RoutingConstraint,
    *,
    known_nets: frozenset[str],
    known_layers: frozenset[str],
    grid: int,
    die: Rect,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(
        constraint,
        (
            RoutingLayerConstraint,
            RoutingLengthConstraint,
            RoutingViaCountConstraint,
            RoutingSkewConstraint,
            RoutingRegionConstraint,
            RoutingShieldConstraint,
        ),
    ):
        return (
            f"routing constraint has unknown type {type(constraint).__name__}",
        )
    if not constraint.name:
        errors.append("routing constraint names must be non-empty")
    if not isinstance(
        constraint,
        (RoutingSkewConstraint, RoutingShieldConstraint),
    ) and constraint.net not in known_nets:
        errors.append(
            f"routing constraint {constraint.name} uses unknown net {constraint.net}"
        )
    if isinstance(constraint, RoutingLayerConstraint):
        if not constraint.allowed_layers:
            errors.append(
                f"routing layer constraint {constraint.name} needs an allowed layer"
            )
        if len(set(constraint.allowed_layers)) != len(constraint.allowed_layers):
            errors.append(
                f"routing layer constraint {constraint.name} repeats a layer"
            )
        unknown = tuple(
            layer for layer in constraint.allowed_layers if layer not in known_layers
        )
        if unknown:
            errors.append(
                f"routing layer constraint {constraint.name} uses unknown layers: "
                f"{', '.join(unknown)}"
            )
    elif isinstance(constraint, RoutingLengthConstraint):
        if constraint.minimum_length_dbu < 0 or (
            constraint.maximum_length_dbu is not None
            and constraint.maximum_length_dbu < constraint.minimum_length_dbu
        ):
            errors.append(
                f"routing length constraint {constraint.name} has an invalid range"
            )
        if constraint.minimum_length_dbu % grid != 0 or (
            constraint.maximum_length_dbu is not None
            and constraint.maximum_length_dbu % grid != 0
        ):
            errors.append(
                f"routing length constraint {constraint.name} must be on-grid"
            )
    elif isinstance(constraint, RoutingRegionConstraint):
        if not constraint.required_regions:
            errors.append(
                f"routing region constraint {constraint.name} needs a region"
            )
        for region in constraint.required_regions:
            if region.layer not in known_layers:
                errors.append(
                    f"routing region constraint {constraint.name} uses unknown "
                    f"layer {region.layer}"
                )
            if any(
                coordinate % grid != 0
                for coordinate in (
                    region.shape.x_min,
                    region.shape.y_min,
                    region.shape.x_max,
                    region.shape.y_max,
                )
            ):
                errors.append(
                    f"routing region constraint {constraint.name} is off-grid"
                )
            if not die.contains(region.shape):
                errors.append(
                    f"routing region constraint {constraint.name} is outside the die"
                )
    elif isinstance(constraint, RoutingShieldConstraint):
        unknown = tuple(
            net
            for net in (constraint.signal_net, constraint.shield_net)
            if net not in known_nets
        )
        if unknown:
            errors.append(
                f"routing shield constraint {constraint.name} uses unknown nets: "
                f"{', '.join(unknown)}"
            )
        if constraint.signal_net == constraint.shield_net:
            errors.append(
                f"routing shield constraint {constraint.name} needs distinct nets"
            )
        if constraint.maximum_spacing_dbu < 0:
            errors.append(
                f"routing shield constraint {constraint.name} maximum spacing "
                "must be non-negative"
            )
        elif constraint.maximum_spacing_dbu % grid != 0:
            errors.append(
                f"routing shield constraint {constraint.name} must be on-grid"
            )
        if len(set(constraint.layers)) != len(constraint.layers):
            errors.append(
                f"routing shield constraint {constraint.name} repeats a layer"
            )
        unknown_layers = tuple(
            layer for layer in constraint.layers if layer not in known_layers
        )
        if unknown_layers:
            errors.append(
                f"routing shield constraint {constraint.name} uses unknown layers: "
                f"{', '.join(unknown_layers)}"
            )
    elif (
        isinstance(constraint, RoutingViaCountConstraint)
        and constraint.maximum_vias < 0
    ):
        errors.append(
            f"routing via constraint {constraint.name} maximum must be non-negative"
        )
    elif isinstance(constraint, RoutingSkewConstraint):
        if len(constraint.nets) < 2:
            errors.append(
                f"routing skew constraint {constraint.name} needs at least two nets"
            )
        if len(set(constraint.nets)) != len(constraint.nets):
            errors.append(
                f"routing skew constraint {constraint.name} repeats a net"
            )
        unknown = tuple(net for net in constraint.nets if net not in known_nets)
        if unknown:
            errors.append(
                f"routing skew constraint {constraint.name} uses unknown nets: "
                f"{', '.join(unknown)}"
            )
        if constraint.maximum_skew_dbu < 0:
            errors.append(
                f"routing skew constraint {constraint.name} maximum must be "
                "non-negative"
            )
        elif constraint.maximum_skew_dbu % grid != 0:
            errors.append(
                f"routing skew constraint {constraint.name} must be on-grid"
            )
    return tuple(errors)


def _route_length(route: NetRoute) -> int:
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in route.segments
    )


def _intersects(first: Rect, second: Rect) -> bool:
    return not (
        first.x_max < second.x_min
        or second.x_max < first.x_min
        or first.y_max < second.y_min
        or second.y_max < first.y_min
    )


def _route_shapes(
    job: PhysicalDesignJob,
    route: NetRoute,
) -> tuple[tuple[str, Rect], ...]:
    shapes = tuple(
        (segment.layer, route_segment_shape(segment)) for segment in route.segments
    )
    vias = {via.name: via for via in job.technology.via_definitions}
    return shapes + tuple(
        (layer, shape)
        for route_via in route.vias
        if route_via.via_definition in vias
        for via in (vias[route_via.via_definition],)
        for layer, shape in via_occurrence_shapes(via, route_via.origin)
        if layer in (via.lower_layer, via.upper_layer)
    )


def _shield_interval(
    signal: RouteSegment,
    shield: RouteSegment,
    maximum_spacing: int,
) -> tuple[int, int] | None:
    if signal.layer != shield.layer:
        return None
    if signal.start.y == signal.end.y and shield.start.y == shield.end.y:
        edge_spacing = (
            abs(signal.start.y - shield.start.y)
            - (signal.width_dbu + shield.width_dbu) // 2
        )
        low = max(
            min(signal.start.x, signal.end.x),
            min(shield.start.x, shield.end.x),
        )
        high = min(
            max(signal.start.x, signal.end.x),
            max(shield.start.x, shield.end.x),
        )
    elif signal.start.x == signal.end.x and shield.start.x == shield.end.x:
        edge_spacing = (
            abs(signal.start.x - shield.start.x)
            - (signal.width_dbu + shield.width_dbu) // 2
        )
        low = max(
            min(signal.start.y, signal.end.y),
            min(shield.start.y, shield.end.y),
        )
        high = min(
            max(signal.start.y, signal.end.y),
            max(shield.start.y, shield.end.y),
        )
    else:
        return None
    if edge_spacing > maximum_spacing or low >= high:
        return None
    return low, high


def _segment_is_shielded(
    signal: RouteSegment,
    shield_segments: tuple[RouteSegment, ...],
    maximum_spacing: int,
) -> bool:
    intervals = sorted(
        interval
        for shield in shield_segments
        if (
            interval := _shield_interval(signal, shield, maximum_spacing)
        )
        is not None
    )
    signal_low, signal_high = (
        (
            min(signal.start.x, signal.end.x),
            max(signal.start.x, signal.end.x),
        )
        if signal.start.y == signal.end.y
        else (
            min(signal.start.y, signal.end.y),
            max(signal.start.y, signal.end.y),
        )
    )
    covered_to = signal_low
    for low, high in intervals:
        if low > covered_to:
            return False
        covered_to = max(covered_to, high)
        if covered_to >= signal_high:
            return True
    return covered_to >= signal_high


def _rectangle_edge_spacing(first: Rect, second: Rect) -> int:
    x_spacing = max(
        0,
        first.x_min - second.x_max,
        second.x_min - first.x_max,
    )
    y_spacing = max(
        0,
        first.y_min - second.y_max,
        second.y_min - first.y_max,
    )
    return max(x_spacing, y_spacing)


def _via_is_shielded(
    signal_via: RouteVia,
    shield_vias: tuple[RouteVia, ...],
    via_definitions: dict[str, ViaDefinition],
    maximum_spacing: int,
) -> bool:
    signal_definition = via_definitions.get(signal_via.via_definition)
    if signal_definition is None:
        return False
    signal_cuts = tuple(
        shape
        for layer, shape in via_occurrence_shapes(
            signal_definition,
            signal_via.origin,
        )
        if layer == signal_definition.cut_layer
    )
    for shield_via in shield_vias:
        shield_definition = via_definitions.get(shield_via.via_definition)
        if shield_definition is None or (
            shield_definition.lower_layer,
            shield_definition.upper_layer,
        ) != (
            signal_definition.lower_layer,
            signal_definition.upper_layer,
        ):
            continue
        shield_cuts = tuple(
            shape
            for layer, shape in via_occurrence_shapes(
                shield_definition,
                shield_via.origin,
            )
            if layer == shield_definition.cut_layer
        )
        if any(
            _rectangle_edge_spacing(signal_cut, shield_cut) <= maximum_spacing
            for signal_cut in signal_cuts
            for shield_cut in shield_cuts
        ):
            return True
    return False


def _terminal_accesses(
    job: PhysicalDesignJob,
    reference: PinReference,
    placements: dict[str, Placement],
) -> tuple[LayerShape, ...] | None:
    if reference.instance is None:
        port = next(port for port in job.design.ports if port.name == reference.pin)
        return tuple(LayerShape(access.layer, access.shape) for access in port.accesses)
    placement = placements.get(reference.instance)
    if placement is None:
        return None
    instances = {instance.name: instance for instance in job.design.instances}
    masters = {master.name: master for master in job.design.masters}
    instance = instances[reference.instance]
    return transformed_pin_accesses(
        masters[instance.master],
        reference.pin,
        placement,
    )


def _point_in_shape(point: Point, shape: Rect) -> bool:
    return (
        shape.x_min <= point.x <= shape.x_max
        and shape.y_min <= point.y <= shape.y_max
    )


def _route_graph(
    job: PhysicalDesignJob,
    route: NetRoute,
) -> dict[tuple[str, Point], set[tuple[str, Point]]]:
    grid = job.technology.manufacturing_grid_dbu
    graph: dict[tuple[str, Point], set[tuple[str, Point]]] = {}

    def connect(first: tuple[str, Point], second: tuple[str, Point]) -> None:
        graph.setdefault(first, set()).add(second)
        graph.setdefault(second, set()).add(first)

    for segment in route.segments:
        dx = (segment.end.x > segment.start.x) - (
            segment.end.x < segment.start.x
        )
        dy = (segment.end.y > segment.start.y) - (
            segment.end.y < segment.start.y
        )
        current = segment.start
        graph.setdefault((segment.layer, current), set())
        while current != segment.end:
            neighbor = Point(current.x + dx * grid, current.y + dy * grid)
            connect((segment.layer, current), (segment.layer, neighbor))
            current = neighbor
    vias = {via.name: via for via in job.technology.via_definitions}
    for route_via in route.vias:
        via = vias.get(route_via.via_definition)
        if via is not None:
            connect(
                (via.lower_layer, route_via.origin),
                (via.upper_layer, route_via.origin),
            )
    return graph


def _regions_are_ordered(
    job: PhysicalDesignJob,
    route: NetRoute,
    required_regions: tuple[LayerShape, ...],
    placements: dict[str, Placement],
) -> bool | None:
    net = next(net for net in job.design.nets if net.name == route.net)
    source_accesses = _terminal_accesses(job, net.pins[0], placements)
    sink_accesses = _terminal_accesses(job, net.pins[1], placements)
    if source_accesses is None or sink_accesses is None:
        return None
    graph = _route_graph(job, route)
    sources = tuple(
        node
        for node in graph
        for access in source_accesses
        if node[0] == access.layer and _point_in_shape(node[1], access.shape)
    )
    sinks = frozenset(
        node
        for node in graph
        for access in sink_accesses
        if node[0] == access.layer and _point_in_shape(node[1], access.shape)
    )
    frontier = list(sources)
    distances = {source: 0 for source in sources}
    while frontier:
        node = frontier.pop(0)
        for neighbor in sorted(
            graph.get(node, ()),
            key=lambda item: (item[0], item[1].y, item[1].x),
        ):
            if neighbor not in distances:
                distances[neighbor] = distances[node] + 1
                frontier.append(neighbor)
    sink_distances = tuple(distances[sink] for sink in sinks if sink in distances)
    if not sink_distances:
        return False
    region_distances: list[int] = []
    for region in required_regions:
        hits = tuple(
            distance
            for node, distance in distances.items()
            if node[0] == region.layer and _point_in_shape(node[1], region.shape)
        )
        if not hits:
            return False
        region_distances.append(min(hits))
    return region_distances == sorted(region_distances) and (
        not region_distances or region_distances[-1] <= min(sink_distances)
    )


def evaluate_routing_constraints(
    job: PhysicalDesignJob,
    routes: tuple[NetRoute, ...],
    instance_placements: tuple[InstancePlacement, ...] = (),
) -> tuple[ConstraintOutcome, ...]:
    route_by_net = {route.net: route for route in routes}
    vias = {via.name: via for via in job.technology.via_definitions}
    placements = {
        item.instance: item.placement for item in instance_placements
    }
    outcomes: list[ConstraintOutcome] = []
    for constraint in job.routing_constraints:
        if isinstance(constraint, RoutingShieldConstraint):
            signal_route = route_by_net.get(constraint.signal_net)
            shield_route = route_by_net.get(constraint.shield_net)
            if signal_route is None or shield_route is None:
                outcomes.append(
                    ConstraintOutcome(
                        constraint.name,
                        ConstraintStatus.NOT_EVALUATED,
                        "routing shield constraint has an incomplete Routing Solution",
                    )
                )
                continue
            signal_segments = tuple(
                segment
                for segment in signal_route.segments
                if not constraint.layers or segment.layer in constraint.layers
            )
            unshielded = tuple(
                index
                for index, segment in enumerate(signal_segments)
                if not _segment_is_shielded(
                    segment,
                    shield_route.segments,
                    constraint.maximum_spacing_dbu,
                )
            )
            signal_vias = tuple(
                route_via
                for route_via in signal_route.vias
                if route_via.via_definition in vias
                for via in (vias[route_via.via_definition],)
                if not constraint.layers
                or (
                    via.lower_layer in constraint.layers
                    and via.upper_layer in constraint.layers
                )
            )
            unshielded_vias = tuple(
                index
                for index, route_via in enumerate(signal_vias)
                if not _via_is_shielded(
                    route_via,
                    shield_route.vias,
                    vias,
                    constraint.maximum_spacing_dbu,
                )
            )
            satisfied = not unshielded and not unshielded_vias
            outcomes.append(
                ConstraintOutcome(
                    constraint.name,
                    (
                        ConstraintStatus.SATISFIED
                        if satisfied
                        else ConstraintStatus.VIOLATED
                    ),
                    (
                        "routing shield constraint is satisfied"
                        if satisfied
                        else "signal route has unshielded geometry: "
                        + (
                            "segments=" + ",".join(map(str, unshielded))
                            if unshielded
                            else ""
                        )
                        + ("; " if unshielded and unshielded_vias else "")
                        + (
                            "vias=" + ",".join(map(str, unshielded_vias))
                            if unshielded_vias
                            else ""
                        )
                    ),
                )
            )
            continue
        if isinstance(constraint, RoutingSkewConstraint):
            constrained_routes = tuple(
                route_by_net.get(net) for net in constraint.nets
            )
            if any(route is None for route in constrained_routes):
                outcomes.append(
                    ConstraintOutcome(
                        constraint.name,
                        ConstraintStatus.NOT_EVALUATED,
                        "routing skew constraint has an incomplete Routing Solution",
                    )
                )
                continue
            lengths = tuple(
                _route_length(route)
                for route in constrained_routes
                if route is not None
            )
            skew = max(lengths) - min(lengths)
            satisfied = skew <= constraint.maximum_skew_dbu
            outcomes.append(
                ConstraintOutcome(
                    constraint.name,
                    (
                        ConstraintStatus.SATISFIED
                        if satisfied
                        else ConstraintStatus.VIOLATED
                    ),
                    (
                        "routing skew constraint is satisfied"
                        if satisfied
                        else (
                            f"route length skew {skew} dbu exceeds maximum "
                            f"{constraint.maximum_skew_dbu} dbu"
                        )
                    ),
                )
            )
            continue
        route = route_by_net.get(constraint.net)
        if route is None:
            outcomes.append(
                ConstraintOutcome(
                    constraint.name,
                    ConstraintStatus.NOT_EVALUATED,
                    "routing constraint has no completed Routing Solution",
                )
            )
            continue
        satisfied = True
        message = "routing constraint is satisfied"
        if isinstance(constraint, RoutingLayerConstraint):
            used_layers = {segment.layer for segment in route.segments}
            for route_via in route.vias:
                via = vias.get(route_via.via_definition)
                if via is not None:
                    used_layers.update((via.lower_layer, via.upper_layer))
            disallowed = sorted(used_layers - set(constraint.allowed_layers))
            satisfied = not disallowed
            if disallowed:
                message = f"route uses disallowed layers: {', '.join(disallowed)}"
        elif isinstance(constraint, RoutingLengthConstraint):
            length = _route_length(route)
            satisfied = length >= constraint.minimum_length_dbu and (
                constraint.maximum_length_dbu is None
                or length <= constraint.maximum_length_dbu
            )
            if not satisfied:
                message = f"route length {length} dbu is outside the allowed range"
        elif isinstance(constraint, RoutingRegionConstraint):
            route_shapes = _route_shapes(job, route)
            missed = tuple(
                index
                for index, region in enumerate(constraint.required_regions)
                if not any(
                    layer == region.layer and _intersects(shape, region.shape)
                    for layer, shape in route_shapes
                )
            )
            satisfied = not missed
            if missed:
                message = (
                    "route misses required region indices: "
                    + ", ".join(str(index) for index in missed)
                )
            else:
                ordered = _regions_are_ordered(
                    job,
                    route,
                    constraint.required_regions,
                    placements,
                )
                if ordered is False:
                    satisfied = False
                    message = "route does not visit required regions in order"
        elif isinstance(constraint, RoutingViaCountConstraint):
            count = len(route.vias)
            satisfied = count <= constraint.maximum_vias
            if not satisfied:
                message = (
                    f"route uses {count} vias; maximum is {constraint.maximum_vias}"
                )
        outcomes.append(
            ConstraintOutcome(
                constraint.name,
                (
                    ConstraintStatus.SATISFIED
                    if satisfied
                    else ConstraintStatus.VIOLATED
                ),
                message,
            )
        )
    return tuple(outcomes)
