"""Compiled routing resources and narrow route-search queries.

The resource graph owns technology-resource interpretation, stable resource
identity, capacity, route demand, and legal transition generation.  Search
consumers see only :class:`RoutingSearchView`; they do not reinterpret the
technology model or public routing constraints.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from sigilicon.layout.physical_geometry import via_occurrence_shapes
from sigilicon.experimental.reference_pnr._routing_ownership import PhysicalOwnerIdentity
from sigilicon.experimental.reference_pnr.model import (
    Axis,
    CutSpacingRule,
    EnclosureRule,
    GridlessRoutingResource,
    LayerShape,
    MinimumSpacingRule,
    MinimumWidthRule,
    NetRoute,
    PhysicalTechnology,
    Point,
    Rect,
    RoutingTrackPattern,
    ViaDefinition,
)


class RoutingResourceKind(str, Enum):
    """Kinds of capacity-bearing resources understood by negotiation."""

    GRIDLESS_CORRIDOR = "gridless_corridor"
    EXPLICIT_TRACK = "explicit_track"
    LAYER_SEGMENT = "layer_segment"
    VIA = "via"


@dataclass(frozen=True, order=True)
class RoutingResourceIdentity:
    """Stable logical identity independent of object allocation or route order."""

    kind: str
    layer: str
    locator: tuple[str | int, ...]

    @property
    def stable_name(self) -> str:
        locator = ":".join(str(item) for item in self.locator)
        return f"{self.kind}:{self.layer}:{locator}"


@dataclass(frozen=True)
class RoutingResourceDefinition:
    """Static capacity and provenance for one logical routing resource."""

    identity: RoutingResourceIdentity
    capacity: int
    base_cost: int
    source: str


@dataclass(frozen=True, order=True)
class RoutingNode:
    layer: str
    point: Point


@dataclass(frozen=True)
class RoutingResourceDemand:
    resource: RoutingResourceIdentity
    amount: int = 1


@dataclass(frozen=True)
class RoutingResourceOverflow:
    resource: RoutingResourceIdentity
    usage: int
    capacity: int
    occupants: tuple[str, ...]

    @property
    def amount(self) -> int:
        return self.usage - self.capacity


@dataclass(frozen=True)
class RoutingTransition:
    start: RoutingNode
    end: RoutingNode
    via_definition: str | None
    demands: tuple[RoutingResourceDemand, ...]


@dataclass(frozen=True)
class RoutingObstacle:
    """Exact hard or routed geometry presented to a per-net search view."""

    layer: str
    shape: Rect
    owner: str | None
    source: str
    branch: str | None = None
    physical_owners: tuple[PhysicalOwnerIdentity, ...] = ()


@dataclass(frozen=True)
class BlockedResource:
    resource: RoutingResourceIdentity | None
    owners: tuple[str, ...]
    hard: bool
    reason: str
    branches: tuple[str, ...] = ()
    physical_owners: tuple[PhysicalOwnerIdentity, ...] = ()


@dataclass(frozen=True)
class RoutingNeighborQuery:
    transitions: tuple[RoutingTransition, ...]
    blocked: tuple[BlockedResource, ...]


@dataclass(frozen=True)
class RoutingDomain:
    """Technology scale and rule facts used by the compiled resource graph."""

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
class RoutingResourceGraphIssue:
    code: str
    unsupported: bool


@dataclass(frozen=True)
class RoutingResourceGraph:
    """Immutable compiled resources with one narrow search-view Interface."""

    domain: RoutingDomain
    layers: Mapping[str, RoutingLayerDomain]
    vias: tuple[ViaDefinition, ...]
    via_definitions: Mapping[str, ViaDefinition]
    issue: RoutingResourceGraphIssue | None
    _corridors: Mapping[RoutingResourceIdentity, RoutingResourceDefinition]

    def search_view(
        self,
        *,
        allowed_layers: frozenset[str] | None,
        allow_vias: bool,
        obstacles: Mapping[str, tuple[RoutingObstacle, ...]],
        present_usage: Mapping[RoutingResourceIdentity, int],
        history_costs: Mapping[RoutingResourceIdentity, int],
        present_weight: int,
        history_weight: int,
        path_length_weight: int,
    ) -> RoutingSearchView:
        layers = frozenset(self.layers) if allowed_layers is None else allowed_layers
        usable_vias = tuple(
            via
            for via in self.vias
            if allow_vias
            and via.lower_layer in layers
            and via.upper_layer in layers
        )
        return RoutingSearchView(
            graph=self,
            allowed_layers=layers,
            vias=usable_vias,
            obstacles=obstacles,
            present_usage=present_usage,
            history_costs=history_costs,
            present_weight=present_weight,
            history_weight=history_weight,
            path_length_weight=path_length_weight,
        )

    def definition(
        self,
        identity: RoutingResourceIdentity,
    ) -> RoutingResourceDefinition:
        corridor = self._corridors.get(identity)
        if corridor is not None:
            return corridor
        if identity.kind == RoutingResourceKind.VIA.value:
            return RoutingResourceDefinition(identity, 1, self.domain.grid, "via")
        if identity.kind == RoutingResourceKind.EXPLICIT_TRACK.value:
            return RoutingResourceDefinition(identity, 1, 0, "track")
        if identity.kind == RoutingResourceKind.LAYER_SEGMENT.value:
            return RoutingResourceDefinition(identity, 1, self.domain.grid, "layer")
        raise KeyError(identity)

    @property
    def resources(self) -> tuple[RoutingResourceDefinition, ...]:
        """Return eagerly compiled aggregate resources in stable order.

        Point resources (layer steps, explicit-track steps, and via sites) are
        definitionally static but materialized lazily through ``definition`` so
        graph size does not scale with die area before search visits them.
        """

        return tuple(self._corridors[key] for key in sorted(self._corridors))

    def route_demands(
        self,
        route: NetRoute,
    ) -> tuple[RoutingResourceDemand, ...]:
        identities: set[RoutingResourceIdentity] = set()
        grid = self.domain.grid
        for segment in route.segments:
            dx = (segment.end.x > segment.start.x) - (
                segment.end.x < segment.start.x
            )
            dy = (segment.end.y > segment.start.y) - (
                segment.end.y < segment.start.y
            )
            current = RoutingNode(segment.layer, segment.start)
            while current.point != segment.end:
                neighbor = RoutingNode(
                    segment.layer,
                    Point(current.point.x + dx * grid, current.point.y + dy * grid),
                )
                transition = self._planar_transition(current, neighbor)
                if transition is None:
                    raise ValueError("route segment is outside compiled resources")
                identities.update(demand.resource for demand in transition.demands)
                current = neighbor
        for occurrence in route.vias:
            via = self.via_definitions[occurrence.via_definition]
            identities.add(_via_identity(via, occurrence.origin))
        return tuple(RoutingResourceDemand(identity) for identity in sorted(identities))

    def overflows(
        self,
        usage: Mapping[RoutingResourceIdentity, int],
        occupants: Mapping[RoutingResourceIdentity, tuple[str, ...]],
    ) -> tuple[RoutingResourceOverflow, ...]:
        overflows = (
            RoutingResourceOverflow(
                resource,
                amount,
                self.definition(resource).capacity,
                occupants.get(resource, ()),
            )
            for resource, amount in usage.items()
            if amount > self.definition(resource).capacity
        )
        return tuple(sorted(overflows, key=lambda item: item.resource))

    def bounds(self, identity: RoutingResourceIdentity) -> Rect | None:
        if identity.kind == RoutingResourceKind.GRIDLESS_CORRIDOR.value:
            x_bin, y_bin, _ = identity.locator
            if isinstance(x_bin, int) and isinstance(y_bin, int):
                return _bin_rect(self.domain, x_bin, y_bin)
        coordinates = tuple(
            item for item in identity.locator if isinstance(item, int)
        )
        if len(coordinates) >= 2:
            x, y = coordinates[-2:]
            half = self.domain.grid
            return Rect(x - half, y - half, x + half, y + half)
        return None

    def _planar_transition(
        self,
        current: RoutingNode,
        neighbor: RoutingNode,
    ) -> RoutingTransition | None:
        if current.layer != neighbor.layer:
            return None
        context = self.layers.get(current.layer)
        if context is None or not _move_allowed(current.point, neighbor.point, context):
            return None
        if not _point_in_context(neighbor.point, context):
            return None
        locator = _normalized_edge(current.point, neighbor.point)
        demands = [
            RoutingResourceDemand(
                RoutingResourceIdentity(
                    RoutingResourceKind.LAYER_SEGMENT.value,
                    current.layer,
                    locator,
                )
            )
        ]
        movement = _movement_shape(current.point, neighbor.point, context.width)
        if context.covers_gridless(movement):
            demands.append(
                RoutingResourceDemand(
                    _corridor_identity(self.domain, current, neighbor)
                )
            )
        else:
            coordinate = (
                current.point.y
                if current.point.y == neighbor.point.y
                else current.point.x
            )
            direction = (
                "horizontal"
                if current.point.y == neighbor.point.y
                else "vertical"
            )
            demands.append(
                RoutingResourceDemand(
                    RoutingResourceIdentity(
                        RoutingResourceKind.EXPLICIT_TRACK.value,
                        current.layer,
                        (direction, coordinate, *locator),
                    )
                )
            )
        return RoutingTransition(current, neighbor, None, tuple(demands))


@dataclass(frozen=True)
class RoutingSearchView:
    """Per-net legal-resource and cost Interface consumed by A*."""

    graph: RoutingResourceGraph
    allowed_layers: frozenset[str]
    vias: tuple[ViaDefinition, ...]
    obstacles: Mapping[str, tuple[RoutingObstacle, ...]]
    present_usage: Mapping[RoutingResourceIdentity, int]
    history_costs: Mapping[RoutingResourceIdentity, int]
    present_weight: int
    history_weight: int
    path_length_weight: int

    @property
    def grid(self) -> int:
        return self.graph.domain.grid

    def access_states(
        self,
        accesses: tuple[LayerShape, ...],
    ) -> tuple[RoutingNode, ...]:
        states: set[RoutingNode] = set()
        for layer in sorted(self.allowed_layers):
            context = self.graph.layers.get(layer)
            if context is None:
                continue
            for region in context.regions:
                point = _access_point(
                    accesses,
                    layer=layer,
                    width=context.width,
                    grid=self.grid,
                    region=region,
                )
                if point is not None:
                    states.add(RoutingNode(layer, point))
            if not context.raw_regions:
                continue
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
                    self.grid,
                )
                y = _snap_nearest(
                    access.shape.y_min + access.shape.y_max,
                    low_y,
                    high_y,
                    self.grid,
                )
                if x is not None:
                    states.update(
                        RoutingNode(layer, Point(x, track))
                        for track in context.horizontal_tracks
                        if low_y <= track <= high_y
                    )
                if y is not None:
                    states.update(
                        RoutingNode(layer, Point(track, y))
                        for track in context.vertical_tracks
                        if low_x <= track <= high_x
                    )
        return tuple(sorted(states, key=_node_key))

    def layers_connect(
        self,
        endpoints: tuple[tuple[RoutingNode, ...], ...],
    ) -> bool:
        endpoint_layers = tuple(
            frozenset(state.layer for state in states) for states in endpoints
        )
        adjacency = _via_adjacency(self.vias)
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

    def blockage(self, node: RoutingNode) -> BlockedResource | None:
        context = self.graph.layers.get(node.layer)
        if context is None or node.layer not in self.allowed_layers:
            return BlockedResource(None, (), True, "routing layer is unavailable")
        resource = RoutingResourceIdentity(
            RoutingResourceKind.LAYER_SEGMENT.value,
            node.layer,
            (node.point.x, node.point.y, node.point.x, node.point.y),
        )
        blocked = tuple(
            obstacle
            for obstacle in self.obstacles.get(node.layer, ())
            if _point_in_interior(
                node.point,
                _expanded(obstacle.shape, context.width // 2 + context.spacing),
            )
        )
        if not blocked:
            return None
        return _blocked_resource(resource, blocked, "conductor blockage")

    def neighbors(
        self,
        current: RoutingNode,
        forbidden: frozenset[RoutingNode],
    ) -> RoutingNeighborQuery:
        transitions: list[RoutingTransition] = []
        blocked: list[BlockedResource] = []
        for dx, dy in (
            (self.grid, 0),
            (0, self.grid),
            (-self.grid, 0),
            (0, -self.grid),
        ):
            neighbor = RoutingNode(
                current.layer,
                Point(current.point.x + dx, current.point.y + dy),
            )
            transition = self.graph._planar_transition(current, neighbor)
            if transition is None or neighbor in forbidden:
                continue
            evidence = self.blockage(neighbor)
            if evidence is None:
                transitions.append(transition)
            else:
                blocked.append(
                    BlockedResource(
                        transition.demands[0].resource,
                        evidence.owners,
                        evidence.hard,
                        evidence.reason,
                        evidence.branches,
                        evidence.physical_owners,
                    )
                )
        adjacency = _via_adjacency(self.vias)
        for next_layer, via in adjacency.get(current.layer, ()):
            neighbor = RoutingNode(next_layer, current.point)
            if neighbor in forbidden or next_layer not in self.allowed_layers:
                continue
            next_context = self.graph.layers[next_layer]
            if not _point_in_context(neighbor.point, next_context):
                continue
            identity = _via_identity(via, current.point)
            via_blockage = self._via_blockage(via, current.point)
            if via_blockage is not None:
                blocked.append(
                    BlockedResource(
                        identity,
                        via_blockage.owners,
                        via_blockage.hard,
                        via_blockage.reason,
                        via_blockage.branches,
                        via_blockage.physical_owners,
                    )
                )
                continue
            transitions.append(
                RoutingTransition(
                    current,
                    neighbor,
                    via.name,
                    (RoutingResourceDemand(identity),),
                )
            )
        return RoutingNeighborQuery(
            tuple(sorted(transitions, key=_transition_key)),
            tuple(sorted(set(blocked), key=_blocked_key)),
        )

    def transition_cost(self, transition: RoutingTransition) -> int:
        base = sum(
            self.graph.definition(demand.resource).base_cost * demand.amount
            for demand in transition.demands
        )
        congestion = sum(
            demand.amount
            * (
                self.present_weight * self.present_usage.get(demand.resource, 0)
                + self.history_weight * self.history_costs.get(demand.resource, 0)
            )
            for demand in transition.demands
        )
        return base * (1 + self.path_length_weight + congestion)

    def _via_blockage(
        self,
        via: ViaDefinition,
        origin: Point,
    ) -> BlockedResource | None:
        domain = self.graph.domain
        blocked: list[RoutingObstacle] = []
        for layer, shape in via_occurrence_shapes(via, origin):
            if not domain.die.contains(shape):
                return BlockedResource(None, (), True, "via leaves design die")
            context = self.graph.layers.get(layer)
            if context is not None and not context.covers(shape):
                return BlockedResource(None, (), True, "via leaves routing resource")
            if context is not None:
                spacing_x = spacing_y = context.spacing
            else:
                spacing = domain.cut_spacing_for(layer)
                if spacing is None:
                    return BlockedResource(None, (), True, "via cut rule unavailable")
                spacing_x, spacing_y = spacing
            blocked.extend(
                obstacle
                for obstacle in self.obstacles.get(layer, ())
                if _rectangles_too_close(
                    shape,
                    obstacle.shape,
                    spacing_x,
                    spacing_y,
                )
            )
        if not blocked:
            return None
        return _blocked_resource(
            _via_identity(via, origin),
            tuple(blocked),
            "via resource blockage",
        )


def compile_routing_resource_graph(
    technology: PhysicalTechnology,
    die: Rect,
    *,
    congestion_bins_x: int,
    congestion_bins_y: int,
) -> RoutingResourceGraph:
    """Compile technology resources once without expanding the route-state grid."""

    domain = _compile_domain(
        technology,
        die,
        congestion_bins_x=congestion_bins_x,
        congestion_bins_y=congestion_bins_y,
    )
    layers, issue = _compile_layers(technology, domain)
    via_definitions = MappingProxyType(
        {via.name: via for via in technology.via_definitions}
    )
    enclosures = _compile_enclosures(technology)
    vias = tuple(
        via
        for via in sorted(via_definitions.values(), key=lambda item: item.name)
        if _via_definition_supported(domain, layers, enclosures, via)
    )
    corridors = _compile_corridors(domain, layers)
    return RoutingResourceGraph(
        domain=domain,
        layers=layers,
        vias=vias,
        via_definitions=via_definitions,
        issue=issue,
        _corridors=MappingProxyType(corridors),
    )


def _compile_domain(
    technology: PhysicalTechnology,
    die: Rect,
    *,
    congestion_bins_x: int,
    congestion_bins_y: int,
) -> RoutingDomain:
    widths: dict[str, list[int]] = {}
    spacings: dict[str, list[int]] = {}
    cut_spacings: dict[str, list[tuple[int, int]]] = {}
    for rule in technology.rules:
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
        die=die,
        grid=technology.manufacturing_grid_dbu,
        congestion_bins_x=congestion_bins_x,
        congestion_bins_y=congestion_bins_y,
        route_rules=MappingProxyType(route_rules),
        cut_spacings=MappingProxyType(normalized_cut_spacings),
    )


def _compile_layers(
    technology: PhysicalTechnology,
    domain: RoutingDomain,
) -> tuple[Mapping[str, RoutingLayerDomain], RoutingResourceGraphIssue | None]:
    resources = tuple(
        sorted(
            technology.routing_resources,
            key=lambda resource: (
                resource.layer,
                type(resource).__name__,
                resource.name,
            ),
        )
    )
    if not resources:
        return MappingProxyType({}), RoutingResourceGraphIssue(
            "routing_resource_required", True
        )
    grouped: dict[str, list[GridlessRoutingResource | RoutingTrackPattern]] = {}
    for resource in resources:
        grouped.setdefault(resource.layer, []).append(resource)

    layers: dict[str, RoutingLayerDomain] = {}
    for layer, layer_resources in sorted(grouped.items()):
        rules = domain.rules_for(layer)
        if rules is None:
            return MappingProxyType({}), RoutingResourceGraphIssue(
                "routing_rule_capability_missing", True
            )
        width, spacing = rules
        if width % (2 * domain.grid) != 0:
            return MappingProxyType({}), RoutingResourceGraphIssue(
                "routing_width_resolution_unsupported", True
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
            if (region := (resource.region or domain.die).intersection(domain.die))
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
                layer,
                width,
                spacing,
                center_regions,
                raw_regions,
                gridless_regions,
                horizontal_tracks,
                vertical_tracks,
            )
    if not layers:
        return MappingProxyType({}), RoutingResourceGraphIssue(
            "routing_region_empty", False
        )
    return MappingProxyType(layers), None


def _compile_corridors(
    domain: RoutingDomain,
    layers: Mapping[str, RoutingLayerDomain],
) -> dict[RoutingResourceIdentity, RoutingResourceDefinition]:
    definitions: dict[RoutingResourceIdentity, RoutingResourceDefinition] = {}
    for layer, context in sorted(layers.items()):
        if not context.gridless_regions:
            continue
        pitch = context.width + context.spacing
        for x_bin in range(domain.congestion_bins_x):
            for y_bin in range(domain.congestion_bins_y):
                bounds = _bin_rect(domain, x_bin, y_bin)
                covered = tuple(
                    intersection
                    for region in context.gridless_regions
                    if (intersection := region.intersection(bounds)) is not None
                )
                if not covered:
                    continue
                for direction in ("horizontal", "vertical"):
                    cross_section = max(
                        (
                            region.height
                            if direction == "horizontal"
                            else region.width
                        )
                        for region in covered
                    )
                    identity = RoutingResourceIdentity(
                        RoutingResourceKind.GRIDLESS_CORRIDOR.value,
                        layer,
                        (x_bin, y_bin, direction),
                    )
                    definitions[identity] = RoutingResourceDefinition(
                        identity,
                        max(1, cross_section // pitch),
                        0,
                        "gridless",
                    )
    return definitions


def _compile_enclosures(
    technology: PhysicalTechnology,
) -> Mapping[tuple[str, str], tuple[int, int]]:
    rules: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for rule in technology.rules:
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
    if not any(_point_in_rect(origin, shape) for shape in via.lower_shapes):
        return False
    if not any(_point_in_rect(origin, shape) for shape in via.upper_shapes):
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
            access.shape.x_min + access.shape.x_max, low_x, high_x, grid
        )
        y = _snap_nearest(
            access.shape.y_min + access.shape.y_max, low_y, high_y, grid
        )
        if x is not None and y is not None:
            candidates.append(Point(x, y))
    return min(candidates, key=lambda point: (point.y, point.x)) if candidates else None


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
    movement = _movement_shape(current, neighbor, context.width)
    if context.covers_gridless(movement):
        return True
    if current.y == neighbor.y:
        return current.y in context.horizontal_tracks
    return current.x in context.vertical_tracks


def _movement_shape(current: Point, neighbor: Point, width: int) -> Rect:
    margin = width // 2
    return Rect(
        min(current.x, neighbor.x) - margin,
        min(current.y, neighbor.y) - margin,
        max(current.x, neighbor.x) + margin,
        max(current.y, neighbor.y) + margin,
    )


def _normalized_edge(first: Point, second: Point) -> tuple[int, int, int, int]:
    low, high = sorted((first, second), key=lambda point: (point.y, point.x))
    return low.x, low.y, high.x, high.y


def _corridor_identity(
    domain: RoutingDomain,
    current: RoutingNode,
    neighbor: RoutingNode,
) -> RoutingResourceIdentity:
    point = neighbor.point
    x_bin = _bin_index(
        point.x,
        domain.die.x_min,
        domain.die.width,
        domain.congestion_bins_x,
    )
    y_bin = _bin_index(
        point.y,
        domain.die.y_min,
        domain.die.height,
        domain.congestion_bins_y,
    )
    direction = "horizontal" if current.point.y == neighbor.point.y else "vertical"
    return RoutingResourceIdentity(
        RoutingResourceKind.GRIDLESS_CORRIDOR.value,
        current.layer,
        (x_bin, y_bin, direction),
    )


def _via_identity(via: ViaDefinition, origin: Point) -> RoutingResourceIdentity:
    return RoutingResourceIdentity(
        RoutingResourceKind.VIA.value,
        via.cut_layer,
        (via.name, origin.x, origin.y),
    )


def _bin_index(value: int, low: int, span: int, bins: int) -> int:
    return min(bins - 1, max(0, (value - low) * bins // span))


def _bin_rect(domain: RoutingDomain, x_bin: int, y_bin: int) -> Rect:
    return Rect(
        domain.die.x_min + domain.die.width * x_bin // domain.congestion_bins_x,
        domain.die.y_min + domain.die.height * y_bin // domain.congestion_bins_y,
        domain.die.x_min + domain.die.width * (x_bin + 1) // domain.congestion_bins_x,
        domain.die.y_min + domain.die.height * (y_bin + 1) // domain.congestion_bins_y,
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


def _point_in_rect(point: Point, rectangle: Rect) -> bool:
    return (
        rectangle.x_min <= point.x <= rectangle.x_max
        and rectangle.y_min <= point.y <= rectangle.y_max
    )


def _via_adjacency(
    vias: Iterable[ViaDefinition],
) -> dict[str, tuple[tuple[str, ViaDefinition], ...]]:
    adjacency: dict[str, list[tuple[str, ViaDefinition]]] = {}
    for via in vias:
        adjacency.setdefault(via.lower_layer, []).append((via.upper_layer, via))
        adjacency.setdefault(via.upper_layer, []).append((via.lower_layer, via))
    return {
        layer: tuple(sorted(edges, key=lambda edge: (edge[0], edge[1].name)))
        for layer, edges in adjacency.items()
    }


def _node_key(node: RoutingNode) -> tuple[str, int, int]:
    return node.layer, node.point.y, node.point.x


def _transition_key(
    transition: RoutingTransition,
) -> tuple[str, int, int, str]:
    return (
        transition.end.layer,
        transition.end.point.y,
        transition.end.point.x,
        transition.via_definition or "",
    )


def _blocked_resource(
    resource: RoutingResourceIdentity | None,
    obstacles: Iterable[RoutingObstacle],
    reason: str,
) -> BlockedResource:
    items = tuple(obstacles)
    owners = tuple(sorted({item.owner for item in items if item.owner is not None}))
    branches = tuple(
        sorted({item.branch for item in items if item.branch is not None})
    )
    physical_owners = tuple(
        sorted(
            {
                owner
                for item in items
                for owner in item.physical_owners
            },
            key=lambda item: item.stable_name,
        )
    )
    return BlockedResource(
        resource,
        owners,
        any(item.owner is None for item in items),
        reason,
        branches,
        physical_owners,
    )


def _blocked_key(
    blocked: BlockedResource,
) -> tuple[object, ...]:
    return (
        blocked.resource is None,
        blocked.resource,
        blocked.hard,
        blocked.owners,
        blocked.branches,
        tuple(owner.stable_name for owner in blocked.physical_owners),
        blocked.reason,
    )
