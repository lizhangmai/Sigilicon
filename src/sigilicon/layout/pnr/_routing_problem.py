"""Compile normalized design intent into immutable router input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sigilicon.layout.pnr._geometry import (
    transformed_obstructions,
    transformed_pin_accesses,
)
from sigilicon.layout.pnr._routing_policy import RoutingPolicy, compile_routing_policy
from sigilicon.layout.pnr.model import (
    Axis,
    CutSpacingRule,
    EnclosureRule,
    GridlessRoutingResource,
    InstancePlacement,
    LayerShape,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalMaster,
    PhysicalPort,
    PinReference,
    Placement,
    Point,
    Rect,
    ResultStatus,
    RoutingTrackPattern,
    ViaDefinition,
)


@dataclass(frozen=True)
class RoutingProblemIssue:
    """A static capability or legality issue found while compiling the problem."""

    status: ResultStatus
    code: str


@dataclass(frozen=True)
class RoutingDomain:
    """Technology and analysis facts needed by route search."""

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
class RoutingLayerDomain:
    """Canonical legal center-line domain for one routing layer."""

    layer: str
    width: int
    spacing: int
    regions: tuple[Rect, ...]
    raw_regions: tuple[Rect, ...]
    gridless_regions: tuple[Rect, ...]
    horizontal_tracks: tuple[int, ...]
    vertical_tracks: tuple[int, ...]

    def covers(self, shape: Rect) -> bool:
        return _rect_covered_by_regions(shape, self.raw_regions)

    def covers_gridless(self, shape: Rect) -> bool:
        return _rect_covered_by_regions(shape, self.gridless_regions)


@dataclass(frozen=True)
class NetRoutingProblem:
    """All static route-search input for one net."""

    name: str
    terminal_accesses: tuple[tuple[LayerShape, ...], ...]
    static_blockers: Mapping[str, tuple[Rect, ...]]


@dataclass(frozen=True)
class RoutingProblem:
    """Compiled static problem consumed by search and routing iteration."""

    domain: RoutingDomain
    layers: Mapping[str, RoutingLayerDomain]
    vias: tuple[ViaDefinition, ...]
    via_definitions: Mapping[str, ViaDefinition]
    policy: RoutingPolicy
    nets: tuple[NetRoutingProblem, ...]
    issue: RoutingProblemIssue | None
    _nets_by_name: Mapping[str, NetRoutingProblem]

    @property
    def net_names(self) -> tuple[str, ...]:
        return tuple(net.name for net in self.nets)

    def net(self, name: str) -> NetRoutingProblem:
        return self._nets_by_name[name]

    def nets_in_order(
        self,
        names: tuple[str, ...] | None = None,
    ) -> tuple[NetRoutingProblem, ...]:
        if names is None:
            return self.nets
        return tuple(self._nets_by_name[name] for name in names)


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


def _compile_domain(job: PhysicalDesignJob) -> RoutingDomain:
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
    return RoutingDomain(
        die=job.design.die,
        grid=job.technology.manufacturing_grid_dbu,
        congestion_bins_x=job.execution_policy.routing_congestion_bins_x,
        congestion_bins_y=job.execution_policy.routing_congestion_bins_y,
        route_rules=MappingProxyType(route_rules),
        cut_spacings=MappingProxyType(normalized_cut_spacings),
    )


def _compile_layers(
    job: PhysicalDesignJob,
    domain: RoutingDomain,
) -> tuple[Mapping[str, RoutingLayerDomain], RoutingProblemIssue | None]:
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
        return MappingProxyType({}), RoutingProblemIssue(
            ResultStatus.UNSUPPORTED,
            "routing_resource_required",
        )

    grouped: dict[
        str,
        list[GridlessRoutingResource | RoutingTrackPattern],
    ] = {}
    for resource in resources:
        grouped.setdefault(resource.layer, []).append(resource)

    layers: dict[str, RoutingLayerDomain] = {}
    for layer, layer_resources in sorted(grouped.items()):
        rules = domain.rules_for(layer)
        if rules is None:
            return MappingProxyType({}), RoutingProblemIssue(
                ResultStatus.UNSUPPORTED,
                "routing_rule_capability_missing",
            )
        width, spacing = rules
        if width % (2 * domain.grid) != 0:
            return MappingProxyType({}), RoutingProblemIssue(
                ResultStatus.UNSUPPORTED,
                "routing_width_resolution_unsupported",
            )
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
                region := (resource.region or domain.die).intersection(domain.die)
            )
            is not None
        )
        raw_regions = gridless_regions + ((domain.die,) if track_resources else ())
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
                    if domain.die.y_min + margin
                    <= resource.start_dbu + resource.pitch_dbu * index
                    <= domain.die.y_max - margin
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
                    if domain.die.x_min + margin
                    <= resource.start_dbu + resource.pitch_dbu * index
                    <= domain.die.x_max - margin
                }
            )
        )
        if center_regions or horizontal_tracks or vertical_tracks:
            layers[layer] = RoutingLayerDomain(
                layer=layer,
                width=width,
                spacing=spacing,
                regions=center_regions,
                raw_regions=raw_regions,
                gridless_regions=gridless_regions,
                horizontal_tracks=horizontal_tracks,
                vertical_tracks=vertical_tracks,
            )
    if not layers:
        return MappingProxyType({}), RoutingProblemIssue(
            ResultStatus.FAILED,
            "routing_region_empty",
        )
    return MappingProxyType(layers), None


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


def _compile_enclosures(
    job: PhysicalDesignJob,
) -> Mapping[tuple[str, str], tuple[int, int]]:
    rules: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for rule in job.technology.rules:
        if isinstance(rule, EnclosureRule):
            rules.setdefault((rule.outer_layer, rule.inner_layer), []).append(
                (rule.enclosure_x_dbu, rule.enclosure_y_dbu)
            )
    return MappingProxyType(
        {
            layers: (
                max(enclosure[0] for enclosure in enclosures),
                max(enclosure[1] for enclosure in enclosures),
            )
            for layers, enclosures in rules.items()
        }
    )


def _via_definition_supported(
    domain: RoutingDomain,
    layers: Mapping[str, RoutingLayerDomain],
    enclosures: Mapping[tuple[str, str], tuple[int, int]],
    via: ViaDefinition,
) -> bool:
    if via.lower_layer not in layers or via.upper_layer not in layers:
        return False
    cut_spacing = domain.cut_spacing_for(via.cut_layer)
    if cut_spacing is None:
        return False
    if any(
        _rectangles_too_close(first, second, *cut_spacing)
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
        width = layers[layer].width
        if any(shape.width < width or shape.height < width for shape in shapes):
            return False
        enclosure = enclosures.get((layer, via.cut_layer))
        if enclosure is None:
            return False
        if any(
            not _shape_enclosed(cut, shapes, enclosure[0], enclosure[1])
            for cut in via.cut_shapes
        ):
            return False
    return True


def _endpoint_accesses(
    reference: PinReference,
    placements: Mapping[str, Placement],
    masters: Mapping[str, PhysicalMaster],
    instances: Mapping[str, PhysicalInstance],
    ports: Mapping[str, PhysicalPort],
) -> tuple[LayerShape, ...]:
    if reference.instance is None:
        port = ports[reference.pin]
        return tuple(
            LayerShape(layer=access.layer, shape=access.shape)
            for access in port.accesses
        )
    instance = instances[reference.instance]
    master = masters[instance.master]
    return transformed_pin_accesses(
        master,
        reference.pin,
        placements[reference.instance],
    )


def _freeze_blockers(
    blockers: Mapping[str, list[Rect]],
) -> Mapping[str, tuple[Rect, ...]]:
    return MappingProxyType(
        {layer: tuple(shapes) for layer, shapes in sorted(blockers.items())}
    )


def compile_routing_problem(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingProblem:
    """Interpret a job and Placement Solution once for all routing callers."""

    domain = _compile_domain(job)
    layers, issue = _compile_layers(job, domain)
    placements = {
        item.instance: item.placement for item in instance_placements
    }
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    ports = {port.name: port for port in job.design.ports}
    nets = {net.name: net for net in job.design.nets}
    policy = compile_routing_policy(
        job.routing_constraints,
        tuple(sorted(nets)),
    )

    references = tuple(PinReference(port.name) for port in job.design.ports) + tuple(
        PinReference(pin.name, instance.name)
        for instance in job.design.instances
        for pin in masters[instance.master].pins
    )
    accesses_by_reference = MappingProxyType(
        {
            reference: _endpoint_accesses(
                reference,
                placements,
                masters,
                instances,
                ports,
            )
            for reference in references
        }
    )
    fixed_obstructions: dict[str, list[Rect]] = {}
    for instance_name, placement in placements.items():
        master = masters[instances[instance_name].master]
        for obstruction in transformed_obstructions(master, placement):
            fixed_obstructions.setdefault(obstruction.layer, []).append(
                obstruction.shape
            )

    compiled_nets: list[NetRoutingProblem] = []
    for net_name, net in sorted(nets.items()):
        blockers = {
            layer: list(shapes)
            for layer, shapes in fixed_obstructions.items()
        }
        connected = frozenset(net.pins)
        for reference, accesses in accesses_by_reference.items():
            if reference in connected:
                continue
            for access in accesses:
                blockers.setdefault(access.layer, []).append(access.shape)
        compiled_nets.append(
            NetRoutingProblem(
                name=net_name,
                terminal_accesses=tuple(
                    accesses_by_reference[reference] for reference in net.pins
                ),
                static_blockers=_freeze_blockers(blockers),
            )
        )

    via_definitions = MappingProxyType(
        {via.name: via for via in job.technology.via_definitions}
    )
    enclosures = _compile_enclosures(job)
    usable_vias = tuple(
        via
        for via in sorted(via_definitions.values(), key=lambda item: item.name)
        if _via_definition_supported(domain, layers, enclosures, via)
    )
    compiled_nets_tuple = tuple(compiled_nets)
    return RoutingProblem(
        domain=domain,
        layers=layers,
        vias=usable_vias,
        via_definitions=via_definitions,
        policy=policy,
        nets=compiled_nets_tuple,
        issue=issue,
        _nets_by_name=MappingProxyType(
            {net.name: net for net in compiled_nets_tuple}
        ),
    )
