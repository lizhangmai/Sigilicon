"""Independent connectivity and geometry checks for Routing Solutions."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.pnr._geometry import (
    route_segment_shape,
    translated_rect,
    transformed_obstructions,
    transformed_pin_accesses,
    via_occurrence_shapes,
)
from sigilicon.layout.pnr.model import (
    Axis,
    CutSpacingRule,
    Diagnostic,
    EnclosureRule,
    GridlessRoutingResource,
    InstancePlacement,
    LayerShape,
    MinimumSpacingRule,
    MinimumWidthRule,
    NetRoute,
    PhysicalDesignJob,
    PinReference,
    Placement,
    Point,
    Rect,
    RouteSegment,
    RoutingTrackPattern,
    ViaDefinition,
)


@dataclass(frozen=True)
class _Conductor:
    net: str
    layer: str
    shape: Rect
    node: tuple[str, int]
    entity: str


class _DisjointSet:
    def __init__(self) -> None:
        self._parents: dict[tuple[str, int], tuple[str, int]] = {}

    def add(self, item: tuple[str, int]) -> None:
        self._parents.setdefault(item, item)

    def find(self, item: tuple[str, int]) -> tuple[str, int]:
        parent = self._parents[item]
        if parent != item:
            self._parents[item] = self.find(parent)
        return self._parents[item]

    def union(self, first: tuple[str, int], second: tuple[str, int]) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parents[max(first_root, second_root)] = min(first_root, second_root)


def _on_grid(value: int, grid: int) -> bool:
    return value % grid == 0


def _intersects(first: Rect, second: Rect) -> bool:
    return not (
        first.x_max < second.x_min
        or second.x_max < first.x_min
        or first.y_max < second.y_min
        or second.y_max < first.y_min
    )


def _too_close(
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


def _route_width(job: PhysicalDesignJob, layer: str) -> int | None:
    widths = tuple(
        rule.width_dbu
        for rule in job.technology.rules
        if isinstance(rule, MinimumWidthRule) and rule.layer == layer
    )
    return max(widths) if widths else None


def _spacing(
    job: PhysicalDesignJob,
    layer: str,
) -> tuple[int, int] | None:
    route_spacings = tuple(
        rule.spacing_dbu
        for rule in job.technology.rules
        if isinstance(rule, MinimumSpacingRule) and rule.layer == layer
    )
    if route_spacings:
        spacing = max(route_spacings)
        return spacing, spacing
    cut_rules = tuple(
        rule
        for rule in job.technology.rules
        if isinstance(rule, CutSpacingRule) and rule.cut_layer == layer
    )
    if cut_rules:
        return (
            max(rule.spacing_x_dbu for rule in cut_rules),
            max(rule.spacing_y_dbu for rule in cut_rules),
        )
    return None


def _gridless_regions(
    job: PhysicalDesignJob,
    layer: str,
) -> tuple[Rect, ...]:
    return tuple(
        region
        for resource in job.technology.routing_resources
        if isinstance(resource, GridlessRoutingResource) and resource.layer == layer
        if (
            region := (resource.region or job.design.die).intersection(job.design.die)
        )
        is not None
    )


def _track_coordinates(
    job: PhysicalDesignJob,
    layer: str,
    axis: Axis,
) -> frozenset[int]:
    return frozenset(
        resource.start_dbu + resource.pitch_dbu * index
        for resource in job.technology.routing_resources
        if isinstance(resource, RoutingTrackPattern)
        and resource.layer == layer
        and resource.axis is axis
        for index in range(resource.count)
    )


def _covered_by_regions(shape: Rect, regions: tuple[Rect, ...]) -> bool:
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


def _wire_in_regions(
    segment: RouteSegment,
    regions: tuple[Rect, ...],
) -> bool:
    return _covered_by_regions(route_segment_shape(segment), regions)


def _segment_on_resource(
    job: PhysicalDesignJob,
    segment: RouteSegment,
) -> bool:
    if _wire_in_regions(segment, _gridless_regions(job, segment.layer)):
        return True
    if segment.start.y == segment.end.y:
        return segment.start.y in _track_coordinates(job, segment.layer, Axis.Y)
    return segment.start.x in _track_coordinates(job, segment.layer, Axis.X)


def _via_on_resource(
    job: PhysicalDesignJob,
    layer: str,
    origin: Point,
    shape: Rect,
) -> bool:
    if _covered_by_regions(shape, _gridless_regions(job, layer)):
        return True
    return (
        origin.y in _track_coordinates(job, layer, Axis.Y)
        or origin.x in _track_coordinates(job, layer, Axis.X)
    ) and job.design.die.contains(shape)


def _endpoint_accesses(
    job: PhysicalDesignJob,
    reference: PinReference,
    placements: dict[str, Placement],
) -> tuple[LayerShape, ...]:
    if reference.instance is None:
        port = next(port for port in job.design.ports if port.name == reference.pin)
        return tuple(
            LayerShape(access.layer, access.shape) for access in port.accesses
        )
    instances = {instance.name: instance for instance in job.design.instances}
    masters = {master.name: master for master in job.design.masters}
    instance = instances[reference.instance]
    return transformed_pin_accesses(
        masters[instance.master],
        reference.pin,
        placements[reference.instance],
    )


def _all_terminal_accesses(
    job: PhysicalDesignJob,
    placements: dict[str, Placement],
) -> tuple[tuple[PinReference, LayerShape], ...]:
    masters = {master.name: master for master in job.design.masters}
    references = tuple(
        PinReference(port.name) for port in job.design.ports
    ) + tuple(
        PinReference(pin.name, instance.name)
        for instance in job.design.instances
        for pin in masters[instance.master].pins
    )
    return tuple(
        (reference, access)
        for reference in references
        for access in _endpoint_accesses(job, reference, placements)
    )


def _obstructions(
    job: PhysicalDesignJob,
    placements: dict[str, Placement],
) -> tuple[LayerShape, ...]:
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    return tuple(
        obstruction
        for instance_name, placement in placements.items()
        for obstruction in transformed_obstructions(
            masters[instances[instance_name].master],
            placement,
        )
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


def check_routing_solution(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
    routes: tuple[NetRoute, ...],
) -> tuple[Diagnostic, ...]:
    """Return deterministic violations without trusting router implementation state."""

    diagnostics: list[Diagnostic] = []

    def report(code: str, message: str, *entities: str) -> None:
        diagnostics.append(Diagnostic(code, message, tuple(entities)))

    grid = job.technology.manufacturing_grid_dbu
    placements = {
        item.instance: item.placement for item in instance_placements
    }
    design_nets = {net.name: net for net in job.design.nets}
    route_names = tuple(route.net for route in routes)
    if len(set(route_names)) != len(route_names) or set(route_names) != set(design_nets):
        report(
            "routing_solution_net_set_mismatch",
            "routing solution must contain exactly one route for every design net",
        )
    via_definitions = {
        via.name: via for via in job.technology.via_definitions
    }
    conductors: list[_Conductor] = []
    route_by_net = {route.net: route for route in routes if route.net in design_nets}

    for route in routes:
        for index, segment in enumerate(route.segments):
            entity = f"{route.net}:segment:{index}"
            if segment.net != route.net:
                report(
                    "routing_segment_net_mismatch",
                    "route segment owner does not match its Net Route",
                    entity,
                )
            if segment.start == segment.end or not (
                segment.start.x == segment.end.x
                or segment.start.y == segment.end.y
            ):
                report(
                    "routing_segment_non_manhattan",
                    "route segment must be non-zero and axis aligned",
                    entity,
                )
                continue
            if any(
                not _on_grid(value, grid)
                for value in (
                    segment.start.x,
                    segment.start.y,
                    segment.end.x,
                    segment.end.y,
                    segment.width_dbu,
                )
            ):
                report(
                    "routing_segment_off_grid",
                    "route segment coordinates and width must be on-grid",
                    entity,
                )
            if segment.width_dbu <= 0 or segment.width_dbu % (2 * grid) != 0:
                report(
                    "routing_segment_width_invalid",
                    "route segment needs a positive even-grid width",
                    entity,
                )
                continue
            minimum_width = _route_width(job, segment.layer)
            has_resource = any(
                isinstance(
                    resource,
                    (GridlessRoutingResource, RoutingTrackPattern),
                )
                and resource.layer == segment.layer
                for resource in job.technology.routing_resources
            )
            if minimum_width is None or not has_resource:
                report(
                    "routing_segment_layer_unsupported",
                    "route segment layer lacks routing resources or width rules",
                    entity,
                    segment.layer,
                )
                continue
            if segment.width_dbu < minimum_width:
                report(
                    "routing_segment_minimum_width_violation",
                    "route segment is narrower than the technology minimum",
                    entity,
                )
            shape = route_segment_shape(segment)
            if not job.design.die.contains(shape):
                report(
                    "routing_segment_outside_die",
                    "route segment conductor is outside the design die",
                    entity,
                )
            if not _segment_on_resource(job, segment):
                report(
                    "routing_segment_outside_resource",
                    "route segment conductor leaves its routing resources",
                    entity,
                )
            conductors.append(
                _Conductor(route.net, segment.layer, shape, ("segment", len(conductors)), entity)
            )

        for index, route_via in enumerate(route.vias):
            entity = f"{route.net}:via:{index}"
            if route_via.net != route.net:
                report(
                    "routing_via_net_mismatch",
                    "route via owner does not match its Net Route",
                    entity,
                )
            via = via_definitions.get(route_via.via_definition)
            if via is None:
                report(
                    "routing_via_definition_unknown",
                    "route via uses an unknown Via Definition",
                    entity,
                    route_via.via_definition,
                )
                continue
            if not _on_grid(route_via.origin.x, grid) or not _on_grid(
                route_via.origin.y,
                grid,
            ):
                report(
                    "routing_via_off_grid",
                    "route via origin must be on-grid",
                    entity,
                )
            via_node = ("via", len(conductors))
            for layer, shape in via_occurrence_shapes(via, route_via.origin):
                if not job.design.die.contains(shape):
                    report(
                        "routing_via_outside_die",
                        "route via geometry is outside the design die",
                        entity,
                        layer,
                    )
                if layer in (
                    via.lower_layer,
                    via.upper_layer,
                ) and not _via_on_resource(
                    job,
                    layer,
                    route_via.origin,
                    shape,
                ):
                    report(
                        "routing_via_outside_resource",
                        "route via conductor leaves a routing resource",
                        entity,
                        layer,
                    )
                conductors.append(
                    _Conductor(route.net, layer, shape, via_node, entity)
                )
            cut_spacing = _spacing(job, via.cut_layer)
            if cut_spacing is None:
                report(
                    "routing_via_rule_capability_missing",
                    "route via cut layer lacks a spacing rule",
                    entity,
                    via.cut_layer,
                )
            elif any(
                _too_close(
                    translated_rect(first, route_via.origin),
                    translated_rect(second, route_via.origin),
                    cut_spacing[0],
                    cut_spacing[1],
                )
                for cut_index, first in enumerate(via.cut_shapes)
                for second in via.cut_shapes[cut_index + 1 :]
            ):
                report(
                    "routing_via_cut_spacing_violation",
                    "Via Definition cut geometry violates cut spacing",
                    entity,
                    via.cut_layer,
                )
            for layer, shapes in (
                (via.lower_layer, via.lower_shapes),
                (via.upper_layer, via.upper_shapes),
            ):
                enclosure_rules = tuple(
                    rule
                    for rule in job.technology.rules
                    if isinstance(rule, EnclosureRule)
                    and rule.outer_layer == layer
                    and rule.inner_layer == via.cut_layer
                )
                if not enclosure_rules:
                    report(
                        "routing_via_rule_capability_missing",
                        "route via lacks a conductor enclosure rule",
                        entity,
                        layer,
                        via.cut_layer,
                    )
                    continue
                enclosure_x = max(
                    rule.enclosure_x_dbu for rule in enclosure_rules
                )
                enclosure_y = max(
                    rule.enclosure_y_dbu for rule in enclosure_rules
                )
                if any(
                    not _shape_enclosed(
                        cut,
                        shapes,
                        enclosure_x,
                        enclosure_y,
                    )
                    for cut in via.cut_shapes
                ):
                    report(
                        "routing_via_enclosure_violation",
                        "Via Definition geometry violates its enclosure rule",
                        entity,
                        layer,
                    )

    terminal_accesses = _all_terminal_accesses(job, placements)
    obstructions = _obstructions(job, placements)
    for conductor in conductors:
        spacing = _spacing(job, conductor.layer)
        if spacing is None:
            report(
                "routing_spacing_rule_capability_missing",
                "conductive geometry layer lacks a spacing rule",
                conductor.entity,
                conductor.layer,
            )
            continue
        spacing_x, spacing_y = spacing
        for obstruction in obstructions:
            if obstruction.layer == conductor.layer and _too_close(
                conductor.shape,
                obstruction.shape,
                spacing_x,
                spacing_y,
            ):
                report(
                    "routing_obstruction_spacing_violation",
                    "conductive geometry violates obstruction spacing",
                    conductor.entity,
                    conductor.layer,
                )
        owned_references = frozenset(
            design_nets[conductor.net].pins
            if conductor.net in design_nets
            else ()
        )
        for reference, access in terminal_accesses:
            if reference not in owned_references and access.layer == conductor.layer:
                if _too_close(
                    conductor.shape,
                    access.shape,
                    spacing_x,
                    spacing_y,
                ):
                    report(
                        "routing_terminal_spacing_violation",
                        "conductive geometry violates another terminal's spacing",
                        conductor.entity,
                        conductor.layer,
                    )

    for index, first in enumerate(conductors):
        for second in conductors[index + 1 :]:
            if first.layer != second.layer:
                continue
            spacing = _spacing(job, first.layer)
            same_net_cut_occurrences = (
                first.net == second.net
                and first.entity != second.entity
                and any(
                    isinstance(rule, CutSpacingRule)
                    and rule.cut_layer == first.layer
                    for rule in job.technology.rules
                )
            )
            if first.net == second.net and not same_net_cut_occurrences:
                continue
            if spacing is not None and _too_close(
                first.shape,
                second.shape,
                spacing[0],
                spacing[1],
            ):
                report(
                    (
                        "routing_same_net_cut_spacing_violation"
                        if same_net_cut_occurrences
                        else "routing_inter_net_spacing_violation"
                    ),
                    (
                        "separate cut occurrences on one net violate spacing"
                        if same_net_cut_occurrences
                        else "conductive geometry from different nets violates spacing"
                    ),
                    first.entity,
                    second.entity,
                    first.layer,
                )

    for net_name, net in design_nets.items():
        route = route_by_net.get(net_name)
        if route is None:
            continue
        net_conductors = tuple(
            conductor for conductor in conductors if conductor.net == net_name
        )
        terminal_shapes = tuple(
            _endpoint_accesses(job, reference, placements) for reference in net.pins
        )
        sets = _DisjointSet()
        for conductor in net_conductors:
            sets.add(conductor.node)
        terminal_nodes = tuple(("terminal", index) for index in range(len(net.pins)))
        for node in terminal_nodes:
            sets.add(node)
        for index, first in enumerate(net_conductors):
            for second in net_conductors[index + 1 :]:
                if first.layer == second.layer and _intersects(
                    first.shape,
                    second.shape,
                ):
                    sets.union(first.node, second.node)
        for terminal_index, accesses in enumerate(terminal_shapes):
            terminal_node = terminal_nodes[terminal_index]
            for conductor in net_conductors:
                if any(
                    access.layer == conductor.layer
                    and _intersects(access.shape, conductor.shape)
                    for access in accesses
                ):
                    sets.union(terminal_node, conductor.node)
            for other_index in range(terminal_index):
                if any(
                    first.layer == second.layer
                    and _intersects(first.shape, second.shape)
                    for first in accesses
                    for second in terminal_shapes[other_index]
                ):
                    sets.union(terminal_node, terminal_nodes[other_index])
        if len({sets.find(node) for node in terminal_nodes}) != 1:
            report(
                "routing_connectivity_open",
                "Routing Solution does not connect every terminal in the net",
                net_name,
            )

    return tuple(
        sorted(
            set(diagnostics),
            key=lambda item: (item.code, item.entities, item.message),
        )
    )
