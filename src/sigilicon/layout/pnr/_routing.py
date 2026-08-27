"""Deterministic Manhattan routing over normalized routing resources."""

from __future__ import annotations

from dataclasses import dataclass, replace
import heapq

from sigilicon.layout.pnr._geometry import (
    translated_rect,
)
from sigilicon.layout.pnr._routing_problem import (
    NetRoutingProblem,
    RoutingProblem,
    compile_routing_problem,
)
from sigilicon.layout.pnr._routing_conflicts import (
    DeterministicVictimPolicy,
    RoutingConflictKind,
    RoutingConflictSet,
    RoutingTerminationEvidence,
    RoutingTerminationReason,
    RoutingVictimSelection,
    attributed_failure_conflicts,
    capacity_conflicts,
)
from sigilicon.layout.pnr._routing_policy import RoutingGroupPolicy
from sigilicon.layout.pnr._routing_ownership import PhysicalOwnerIdentity
from sigilicon.layout.pnr._routing_pressure import (
    RoutingPlacementPressure,
    attribute_routing_pressure,
)
from sigilicon.layout.pnr._routing_resources import (
    BlockedResource,
    RoutingDomain,
    RoutingLayerDomain,
    RoutingNode,
    RoutingObstacle,
    RoutingResourceIdentity,
    RoutingResourceKind,
    RoutingSearchView,
)
from sigilicon.layout.pnr._routing_state import RoutingState
from sigilicon.layout.pnr._routing_tree import (
    RouteBranch,
    RoutingTree,
    whole_route_tree,
)
from sigilicon.layout.pnr.model import (
    Diagnostic,
    InstancePlacement,
    Metric,
    NetRoute,
    PhysicalDesignJob,
    Point,
    PnrStage,
    Rect,
    ResultStatus,
    RouteSegment,
    RouteVia,
    RoutingBlockagePlacement,
    StageReport,
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
    ripped_branch_count: int = 0
    conflicts: RoutingConflictSet = RoutingConflictSet()
    termination: RoutingTerminationEvidence = RoutingTerminationEvidence(
        RoutingTerminationReason.CLOSED,
        0,
        0,
        (),
    )
    placement_pressure: RoutingPlacementPressure = RoutingPlacementPressure()


_RouteState = RoutingNode


@dataclass(frozen=True)
class _RoutePath:
    states: tuple[_RouteState, ...]
    transition_vias: tuple[str | None, ...]


@dataclass(frozen=True)
class _Blocker:
    shape: Rect
    owner: str | None
    branch: str | None = None
    physical_owners: tuple[PhysicalOwnerIdentity, ...] = ()


@dataclass(frozen=True)
class _RouteSearchResult:
    path: _RoutePath | None
    states: int
    exhausted: bool
    blocked_resources: tuple[BlockedResource, ...] = ()


@dataclass(frozen=True)
class _NetRouteAttempt:
    status: ResultStatus
    route: NetRoute | None
    diagnostic: Diagnostic | None
    route_states: int
    conflict_kind: RoutingConflictKind = RoutingConflictKind.TOPOLOGY_CONFLICT
    blocked_resources: tuple[BlockedResource, ...] = ()
    tree: RoutingTree | None = None


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
    conflicts: RoutingConflictSet = RoutingConflictSet(),
    termination_reason: RoutingTerminationReason | None = None,
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
        conflicts=conflicts,
        termination=RoutingTerminationEvidence(
            termination_reason
            or {
                ResultStatus.SUCCEEDED: RoutingTerminationReason.CLOSED,
                ResultStatus.FAILED: RoutingTerminationReason.INFEASIBLE,
                ResultStatus.UNSUPPORTED: RoutingTerminationReason.UNSUPPORTED,
                ResultStatus.EXHAUSTED: RoutingTerminationReason.STATE_BUDGET,
            }[status],
            0,
            route_states,
            tuple(route.net for route in routes),
            tuple(conflict.identity for conflict in conflicts.conflicts),
        ),
        placement_pressure=RoutingPlacementPressure(),
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


def _route_resource_demands(
    problem: RoutingProblem,
    routes: tuple[NetRoute, ...],
) -> dict[RoutingResourceIdentity, int]:
    demands: dict[RoutingResourceIdentity, int] = {}
    for route in routes:
        for demand in problem.resource_graph.route_demands(route):
            demands[demand.resource] = demands.get(demand.resource, 0) + demand.amount
    return demands


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
        layer: [
            _Blocker(region.shape, None, physical_owners=region.owners)
            for region in regions
        ]
        for layer, regions in net.static_blockers.items()
    }
    for layer, occupancy in state.occupancy_by_layer.items():
        blockers.setdefault(layer, []).extend(
            _Blocker(item.shape, item.net, item.branch)
            for item in occupancy
            if item.net != net.name
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
                blocker.branch,
                blocker.physical_owners,
            )
            for blocker in raw_blockers.get(layer, ())
        )
        for layer, context in contexts.items()
    }


def _search_resources(
    problem: RoutingProblem,
    state: RoutingState,
    raw_blockers: dict[str, tuple[_Blocker, ...]],
    *,
    net: str,
    allowed_layers: frozenset[str] | None,
    allow_vias: bool,
    present_weight: int,
    history_weight: int,
    path_length_weight: int,
) -> RoutingSearchView:
    obstacles = {
        layer: tuple(
            RoutingObstacle(
                layer,
                blocker.shape,
                blocker.owner,
                "physical" if blocker.physical_owners else "route",
                blocker.branch,
                blocker.physical_owners,
            )
            for blocker in blockers
        )
        for layer, blockers in raw_blockers.items()
    }
    return problem.resource_graph.search_view(
        allowed_layers=allowed_layers,
        allow_vias=allow_vias,
        obstacles=obstacles,
        present_usage=state.usage_without(net),
        history_costs=state.history_costs,
        present_weight=present_weight,
        history_weight=history_weight,
        path_length_weight=path_length_weight,
    )


def _state_blocked(
    state: _RouteState,
    blockers: dict[str, tuple[_Blocker, ...]],
) -> bool:
    return any(
        _point_in_interior(state.point, blocker.shape)
        for blocker in blockers.get(state.layer, ())
    )


def _astar(
    starts: frozenset[_RouteState],
    targets: frozenset[_RouteState],
    *,
    resources: RoutingSearchView,
    forbidden_states: frozenset[_RouteState],
    remaining_states: int,
) -> _RouteSearchResult:
    def heuristic(state: _RouteState) -> int:
        return min(
            abs(state.point.x - target.point.x)
            + abs(state.point.y - target.point.y)
            for target in targets
        )

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
    encountered_blockers: set[BlockedResource] = set()
    states = 0
    while frontier:
        if states >= remaining_states:
            return _RouteSearchResult(
                None,
                states,
                True,
                tuple(sorted(encountered_blockers, key=_blocked_resource_key)),
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

        query = resources.neighbors(
            current,
            frozenset(state for state in forbidden_states if state not in targets),
        )
        encountered_blockers.update(query.blocked)
        for transition in query.transitions:
            neighbor = transition.end
            via_name = transition.via_definition
            neighbor_cost = current_cost + resources.transition_cost(transition)
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
        tuple(sorted(encountered_blockers, key=_blocked_resource_key)),
    )


def _blocked_resource_key(blocked: BlockedResource) -> tuple[object, ...]:
    return (
        "" if blocked.resource is None else blocked.resource.stable_name,
        blocked.hard,
        blocked.owners,
        blocked.branches,
        blocked.reason,
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


def _state_distance(first: _RouteState, second: _RouteState) -> int:
    layer_penalty = 0 if first.layer == second.layer else 2**32
    return (
        abs(first.point.x - second.point.x)
        + abs(first.point.y - second.point.y)
        + layer_penalty
    )


def _ordered_shield_segments(
    segments: tuple[RouteSegment, ...],
) -> tuple[RouteSegment, ...]:
    """Orient one planar route chain before deriving shield waypoints.

    Ordered required regions can split a straight route into segments whose
    stored directions do not form a traversal.  A shield follows geometry,
    not that incidental serialization.  Only an unambiguous single-layer
    chain is normalized; branch and disconnected topology retains the
    original ordering for the existing conservative behavior.
    """

    if len(segments) < 2 or len({segment.layer for segment in segments}) != 1:
        return segments
    incident: dict[Point, list[int]] = {}
    for index, segment in enumerate(segments):
        incident.setdefault(segment.start, []).append(index)
        incident.setdefault(segment.end, []).append(index)
    if any(len(indices) > 2 for indices in incident.values()):
        return segments
    endpoints = tuple(
        point for point, indices in incident.items() if len(indices) == 1
    )
    if len(endpoints) != 2:
        return segments

    current = min(endpoints, key=lambda point: (point.y, point.x))
    remaining = set(range(len(segments)))
    ordered: list[RouteSegment] = []
    while remaining:
        candidates = tuple(
            index for index in incident[current] if index in remaining
        )
        if len(candidates) != 1:
            return segments
        index = candidates[0]
        segment = segments[index]
        other = segment.end if segment.start == current else segment.start
        ordered.append(replace(segment, start=current, end=other))
        remaining.remove(index)
        current = other
    return tuple(ordered) if current in endpoints else segments


def _shield_guidance_states(
    problem: RoutingProblem,
    net: NetRoutingProblem,
    state: RoutingState,
    contexts: dict[str, RoutingLayerDomain],
    pin_endpoint_states: tuple[tuple[_RouteState, ...], ...],
    blockers: dict[str, tuple[_Blocker, ...]],
) -> tuple[
    tuple[tuple[_RouteState, ...], ...],
    Diagnostic | None,
    tuple[BlockedResource, ...],
]:
    group = problem.policy.group_for_net(net.name)
    if group is None:
        return (), None, ()
    relationships = tuple(
        shield for shield in group.shields if shield.shield_net == net.name
    )
    if not relationships:
        return (), None, ()

    guidance: list[tuple[_RouteState, ...]] = []
    blocked_resources: set[BlockedResource] = set()
    for relationship in relationships:
        signal_route = state.routes_by_net.get(relationship.signal_net)
        if signal_route is None:
            return (
                (),
                Diagnostic(
                    "routing_shield_dependency_missing",
                    f"shield net {net.name} needs signal geometry before routing",
                    (relationship.signal_net, net.name),
                ),
                (),
            )
        signal_segments = _ordered_shield_segments(
            tuple(
                segment
                for segment in signal_route.segments
                if not relationship.layers
                or segment.layer in relationship.layers
            )
        )
        if not signal_segments:
            continue
        candidates: list[tuple[int, tuple[_RouteState, ...]]] = []
        if any(segment.layer not in contexts for segment in signal_segments):
            return (
                (),
                Diagnostic(
                    "routing_shield_geometry_infeasible",
                    f"shield net {net.name} cannot use every selected signal layer",
                    (relationship.signal_net, net.name),
                ),
                (),
            )
        minimum_spacing = max(
            contexts[segment.layer].spacing
            for segment in signal_segments
            if segment.layer in contexts
        )
        for edge_spacing in range(
            minimum_spacing,
            relationship.maximum_spacing_dbu + 1,
            problem.domain.grid,
        ):
            for sign in (-1, 1):
                candidate: list[_RouteState] = []
                supported = True
                for segment in signal_segments:
                    context = contexts.get(segment.layer)
                    if context is None or context.spacing > edge_spacing:
                        supported = False
                        break
                    distance = (
                        segment.width_dbu + context.width
                    ) // 2 + edge_spacing
                    offset_x, offset_y = (
                        (0, sign * distance)
                        if segment.start.y == segment.end.y
                        else (sign * distance, 0)
                    )
                    for point in (segment.start, segment.end):
                        route_state = _RouteState(
                            segment.layer,
                            Point(point.x + offset_x, point.y + offset_y),
                        )
                        if not _point_in_context(route_state.point, context):
                            supported = False
                            break
                        blocked = tuple(
                            blocker
                            for blocker in blockers.get(route_state.layer, ())
                            if _point_in_interior(
                                route_state.point,
                                blocker.shape,
                            )
                        )
                        if blocked:
                            route_owners = tuple(
                                sorted(
                                    {
                                        blocker.owner
                                        for blocker in blocked
                                        if blocker.owner is not None
                                    }
                                )
                            )
                            physical_owners = tuple(
                                sorted(
                                    {
                                        owner
                                        for blocker in blocked
                                        for owner in blocker.physical_owners
                                    },
                                    key=lambda item: item.stable_name,
                                )
                            )
                            blocked_resources.add(
                                BlockedResource(
                                    RoutingResourceIdentity(
                                        RoutingResourceKind.LAYER_SEGMENT.value,
                                        route_state.layer,
                                        (
                                            route_state.point.x,
                                            route_state.point.y,
                                            route_state.point.x,
                                            route_state.point.y,
                                        ),
                                    ),
                                    route_owners,
                                    any(
                                        blocker.owner is None
                                        for blocker in blocked
                                    ),
                                    "shield guidance blockage",
                                    tuple(
                                        sorted(
                                            {
                                                blocker.branch
                                                for blocker in blocked
                                                if blocker.branch is not None
                                            }
                                        )
                                    ),
                                    physical_owners,
                                )
                            )
                            supported = False
                            break
                        if not candidate or candidate[-1] != route_state:
                            candidate.append(route_state)
                    if not supported:
                        break
                if not supported:
                    continue
                for ordered in (tuple(candidate), tuple(reversed(candidate))):
                    score = min(
                        _state_distance(source, ordered[0])
                        + _state_distance(ordered[-1], sink)
                        for source in pin_endpoint_states[0]
                        for sink in pin_endpoint_states[1]
                    )
                    candidates.append((score, ordered))
        if not candidates:
            return (
                (),
                Diagnostic(
                    "routing_shield_geometry_infeasible",
                    (
                        f"shield net {net.name} has no legal adjacent guidance for "
                        f"signal net {relationship.signal_net}"
                    ),
                    (relationship.signal_net, net.name),
                ),
                tuple(
                    sorted(
                        blocked_resources,
                        key=lambda item: (
                            ""
                            if item.resource is None
                            else item.resource.stable_name,
                            item.owners,
                            tuple(
                                owner.stable_name
                                for owner in item.physical_owners
                            ),
                        ),
                    )
                ),
            )
        _, selected = min(
            candidates,
            key=lambda item: (
                item[0],
                tuple(
                    (state.layer, state.point.y, state.point.x)
                    for state in item[1]
                ),
            ),
        )
        guidance.extend((route_state,) for route_state in selected)
    return tuple(guidance), None, ()


def _net_route_failure(
    net: str,
    status: ResultStatus,
    code: str,
    message: str,
    *,
    route_states: int = 0,
    conflict_kind: RoutingConflictKind = RoutingConflictKind.TOPOLOGY_CONFLICT,
    blocked_resources: tuple[BlockedResource, ...] = (),
) -> _NetRouteAttempt:
    return _NetRouteAttempt(
        status=status,
        route=None,
        diagnostic=Diagnostic(code, message, (net,)),
        route_states=route_states,
        conflict_kind=conflict_kind,
        blocked_resources=blocked_resources,
    )


def _branch_local_ripup_safe(
    problem: RoutingProblem,
    net: str,
    endpoint_index: int,
    ordered_topology_end: int,
) -> bool:
    policy = problem.policy.for_net(net)
    return (
        endpoint_index > ordered_topology_end
        and problem.policy.group_for_net(net) is None
        and not policy.required_regions
        and policy.maximum_vias is None
        and policy.length_window.minimum_dbu == 0
        and policy.length_window.maximum_dbu is None
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
    accesses = net.terminal_accesses
    if any(not endpoint for endpoint in accesses):
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            "routing_pin_access_missing",
            f"net {net.name} has a terminal without access geometry",
            conflict_kind=RoutingConflictKind.UNROUTED_TERMINAL,
        )
    retained_tree = state.trees_by_net.get(net.name)
    raw_blockers = _raw_blockers(net, state)
    if retained_tree is not None:
        mutable_blockers = {
            layer: list(shapes) for layer, shapes in raw_blockers.items()
        }
        for retained_branch in retained_tree.branches:
            for route_via in retained_branch.vias:
                via = problem.via_definitions[route_via.via_definition]
                mutable_blockers.setdefault(via.cut_layer, []).extend(
                    _Blocker(translated_rect(shape, route_via.origin), None)
                    for shape in via.cut_shapes
                )
        raw_blockers = {
            layer: tuple(shapes) for layer, shapes in mutable_blockers.items()
        }
    search_resources = _search_resources(
        problem,
        state,
        raw_blockers,
        net=net.name,
        allowed_layers=allowed_layers,
        allow_vias=via_limit != 0,
        present_weight=net_policy.cost.congestion_weight,
        history_weight=net_policy.cost.history_weight,
        path_length_weight=net_policy.cost.group_violation_weight,
    )
    pin_endpoint_states = tuple(
        search_resources.access_states(endpoint) for endpoint in accesses
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
            conflict_kind=RoutingConflictKind.UNROUTED_TERMINAL,
        )
    required_regions = net_policy.required_regions
    region_states = tuple(
        search_resources.access_states((region,)) for region in required_regions
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
            conflict_kind=RoutingConflictKind.GROUP_CONSTRAINT,
        )
    center_blockers = _center_blockers(raw_blockers, net_contexts)
    shield_states, shield_diagnostic, shield_blocked = _shield_guidance_states(
        problem,
        net,
        state,
        net_contexts,
        pin_endpoint_states,
        center_blockers,
    )
    if shield_diagnostic is not None:
        return _net_route_failure(
            net.name,
            ResultStatus.FAILED,
            shield_diagnostic.code,
            shield_diagnostic.message,
            conflict_kind=RoutingConflictKind.GROUP_CONSTRAINT,
            blocked_resources=shield_blocked,
        )
    endpoint_states = (
        (pin_endpoint_states[0],)
        + region_states
        + shield_states
        + (pin_endpoint_states[1],)
        + pin_endpoint_states[2:]
    )
    endpoint_kinds = (
        ("pin",)
        + ("region",) * len(region_states)
        + ("shield",) * len(shield_states)
        + ("pin",) * (len(pin_endpoint_states) - 1)
    )
    if not search_resources.layers_connect(endpoint_states):
        constrained = (
            allowed_layers is not None
            or via_limit is not None
            or bool(required_regions)
            or bool(shield_states)
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
            conflict_kind=(
                RoutingConflictKind.GROUP_CONSTRAINT
                if constrained
                else RoutingConflictKind.TOPOLOGY_CONFLICT
            ),
        )
    legal_endpoint_states = tuple(
        tuple(
            endpoint_state
            for endpoint_state in states
            if search_resources.blockage(endpoint_state) is None
        )
        for states in endpoint_states
    )
    if any(not states for states in legal_endpoint_states):
        blocked_index = next(
            index
            for index, states in enumerate(legal_endpoint_states)
            if not states
        )
        region_blocked = endpoint_kinds[blocked_index] == "region"
        blocked_resources = tuple(
            blocked
            for endpoint_state in endpoint_states[blocked_index]
            if (blocked := search_resources.blockage(endpoint_state)) is not None
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
            conflict_kind=(
                RoutingConflictKind.GROUP_CONSTRAINT
                if region_blocked
                else RoutingConflictKind.UNROUTED_TERMINAL
            ),
            blocked_resources=blocked_resources,
        )
    ordered_topology_end = 1 + len(region_states) + len(shield_states)
    expected_branches = tuple(
        (
            f"{net.name}:primary:{endpoint_index}"
            if endpoint_index <= ordered_topology_end
            else f"{net.name}:terminal:{endpoint_index}"
        )
        for endpoint_index in range(1, len(legal_endpoint_states))
    )
    retained_branches = (
        {}
        if retained_tree is None
        or retained_tree.expected_branches != expected_branches
        else {branch.identity: branch for branch in retained_tree.branches}
    )
    tree: set[_RouteState] = set(legal_endpoint_states[0])
    for retained_branch in retained_branches.values():
        tree.update(retained_branch.nodes)
    previous_terminal: set[_RouteState] = set(legal_endpoint_states[0])
    branches = dict(retained_branches)
    segments: list[RouteSegment] = [
        segment
        for branch in retained_branches.values()
        for segment in branch.segments
    ]
    route_vias: list[RouteVia] = [
        via for branch in retained_branches.values() for via in branch.vias
    ]
    for endpoint_index, terminal_states in enumerate(
        legal_endpoint_states[1:],
        start=1,
    ):
        starts = frozenset(terminal_states)
        targets = frozenset(
            previous_terminal if endpoint_index <= ordered_topology_end else tree
        )
        branch_identity = expected_branches[endpoint_index - 1]
        retained_branch = retained_branches.get(branch_identity)
        if retained_branch is not None:
            previous_terminal = set(starts)
            continue
        if starts & targets:
            branches[branch_identity] = RouteBranch(
                branch_identity,
                net.name,
                endpoint_index,
                tuple(
                    sorted(
                        starts,
                        key=lambda item: (
                            item.layer,
                            item.point.y,
                            item.point.x,
                        ),
                    )
                ),
                (),
                (),
                _branch_local_ripup_safe(
                    problem,
                    net.name,
                    endpoint_index,
                    ordered_topology_end,
                ),
            )
            tree.update(starts)
            previous_terminal = set(starts)
            continue
        search_resources = _search_resources(
            problem,
            state,
            raw_blockers,
            net=net.name,
            allowed_layers=allowed_layers,
            allow_vias=via_limit != 0,
            present_weight=net_policy.cost.congestion_weight,
            history_weight=net_policy.cost.history_weight,
            path_length_weight=net_policy.cost.group_violation_weight,
        )
        search = _astar(
            starts,
            targets,
            resources=search_resources,
            forbidden_states=(
                frozenset(tree)
                if endpoint_index <= ordered_topology_end
                else frozenset()
            ),
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
                conflict_kind=(
                    RoutingConflictKind.BUDGET_EXHAUSTION
                    if search.exhausted
                    else RoutingConflictKind.TOPOLOGY_CONFLICT
                ),
                blocked_resources=search.blocked_resources,
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
                conflict_kind=RoutingConflictKind.VIA_EXHAUSTION,
            )
        segments.extend(new_segments)
        route_vias.extend(new_vias)
        branches[branch_identity] = RouteBranch(
            branch_identity,
            net.name,
            endpoint_index,
            search.path.states,
            new_segments,
            new_vias,
            _branch_local_ripup_safe(
                problem,
                net.name,
                endpoint_index,
                ordered_topology_end,
            ),
        )
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
        previous_terminal = set(starts)
    routing_tree = RoutingTree(
        net.name,
        expected_branches,
        tuple(branches[identity] for identity in expected_branches),
    )
    route = routing_tree.route
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
            conflict_kind=RoutingConflictKind.GROUP_CONSTRAINT,
        )
    if current_length < target_length:
        if (target_length - current_length) % (2 * grid) != 0:
            return _net_route_failure(
                net.name,
                ResultStatus.FAILED,
                "routing_length_window_infeasible",
                f"net {net.name} needs an unreachable Manhattan route length",
                route_states=route_states,
                conflict_kind=RoutingConflictKind.GROUP_CONSTRAINT,
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
                conflict_kind=(
                    RoutingConflictKind.BUDGET_EXHAUSTION
                    if compensation.exhausted
                    else RoutingConflictKind.GROUP_CONSTRAINT
                ),
            )
        route = compensation.route
        routing_tree = whole_route_tree(route)
    return _NetRouteAttempt(
        ResultStatus.SUCCEEDED,
        route,
        None,
        route_states,
        tree=routing_tree,
    )


def _with_iteration_metrics(
    result: RoutingSolveResult,
    *,
    route_states: int,
    routing_iterations: int,
    route_attempts: int,
    ripped_net_count: int,
    ripped_branch_count: int,
) -> RoutingSolveResult:
    metrics = tuple(
        metric for metric in result.report.metrics if metric.name != "route_states"
    ) + (
        Metric("route_states", route_states, "count"),
        Metric("routing_iterations", routing_iterations, "count"),
        Metric("routing_route_attempts", route_attempts, "count"),
        Metric("routing_ripped_net_count", ripped_net_count, "count"),
        Metric("routing_ripped_branch_count", ripped_branch_count, "count"),
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
        ripped_branch_count=ripped_branch_count,
        conflicts=result.conflicts,
        termination=RoutingTerminationEvidence(
            result.termination.reason,
            routing_iterations,
            route_states,
            tuple(route.net for route in result.routes),
            tuple(
                conflict.identity for conflict in result.conflicts.conflicts
            ),
        ),
        placement_pressure=result.placement_pressure,
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
        ripped_branch_count=result.ripped_branch_count,
        conflicts=result.conflicts,
        termination=result.termination,
        placement_pressure=result.placement_pressure,
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
    ripped_branch_count: int = 0,
    diagnostic: Diagnostic | None = None,
    conflicts: RoutingConflictSet = RoutingConflictSet(),
    termination_reason: RoutingTerminationReason | None = None,
) -> RoutingSolveResult:
    result = _result(
        status,
        routes=state.routes,
        code=None if diagnostic is None else diagnostic.code,
        message="" if diagnostic is None else diagnostic.message,
        entities=() if diagnostic is None else diagnostic.entities,
        route_states=route_states,
        conflicts=conflicts,
        termination_reason=termination_reason,
    )
    result = replace(
        result,
        placement_pressure=attribute_routing_pressure(problem, conflicts),
    )
    return _with_congestion_metrics(
        problem.domain,
        _with_iteration_metrics(
            result,
            route_states=route_states,
            routing_iterations=routing_iterations,
            route_attempts=route_attempts,
            ripped_net_count=ripped_net_count,
            ripped_branch_count=ripped_branch_count,
        ),
    )


def _attempt_conflicts(
    problem: RoutingProblem,
    state: RoutingState,
    net: str,
    attempt: _NetRouteAttempt,
) -> RoutingConflictSet:
    group = problem.policy.group_for_net(net)
    diagnostic = attempt.diagnostic
    return attributed_failure_conflicts(
        net=net,
        kind=attempt.conflict_kind,
        blocked=attempt.blocked_resources,
        affected_group=None if group is None else group.name,
        reroute_scope=problem.policy.reroute_scope(net),
        evidence=(
            f"net {net} could not be routed"
            if diagnostic is None
            else diagnostic.message
        ),
        branch_occupants=state.branch_resource_occupants,
    )


def _capacity_conflicts(
    problem: RoutingProblem,
    state: RoutingState,
) -> RoutingConflictSet:
    overflows = problem.resource_graph.overflows(
        state.resource_usage,
        state.resource_occupants,
    )
    return capacity_conflicts(
        overflows,
        group_by_net={
            net: (
                None
                if problem.policy.group_for_net(net) is None
                else problem.policy.group_for_net(net).name
            )
            for net in problem.net_names
        },
        reroute_scope_by_net={
            net: problem.policy.reroute_scope(net) for net in problem.net_names
        },
        branch_occupants=state.branch_resource_occupants,
    )


def _conflict_history_penalty(
    conflicts: RoutingConflictSet,
) -> dict[RoutingResourceIdentity, int]:
    penalties: dict[RoutingResourceIdentity, int] = {}
    for conflict in conflicts.conflicts:
        if conflict.resource is None:
            continue
        penalties[conflict.resource] = max(
            penalties.get(conflict.resource, 0),
            max(1, conflict.severity),
        )
    return penalties


def _branch_safety(state: RoutingState) -> dict[str, bool]:
    return {
        branch.identity: branch.local_ripup_safe
        for tree in state.trees_by_net.values()
        for branch in tree.branches
    }


def _rip_up_selection(
    state: RoutingState,
    selection: RoutingVictimSelection,
    problem: RoutingProblem,
) -> tuple[RoutingState, int, int]:
    if selection.branch is not None:
        return (
            state.rip_up_branch(selection.branch, problem.resource_graph),
            0,
            1,
        )
    affected = frozenset(selection.reroute_scope)
    return (
        state.rip_up(affected, problem.resource_graph),
        len(affected & state.routes_by_net.keys()),
        0,
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
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] | None = None,
) -> RoutingSolveResult:
    problem = compile_routing_problem(
        job,
        instance_placements,
        routing_blockage_placements,
    )
    state = RoutingState.empty()
    ripped_net_count = 0
    ripped_branch_count = 0
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
    victim_policy = DeterministicVictimPolicy()
    reroute_scopes = {
        net: problem.policy.reroute_scope(net) for net in problem.net_names
    }
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
                ripped_branch_count=ripped_branch_count,
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
            if attempt.tree is None:
                raise RuntimeError("successful net route attempt has no route tree")
            state = state.with_tree(attempt.tree, problem.resource_graph)
            group, length_targets, length_diagnostic = _length_closure_targets(
                problem,
                state,
                net_name,
            )
            if length_diagnostic is not None:
                length_conflicts = attributed_failure_conflicts(
                    net=net_name,
                    kind=RoutingConflictKind.GROUP_CONSTRAINT,
                    blocked=(),
                    affected_group=None if group is None else group.name,
                    reroute_scope=problem.policy.reroute_scope(net_name),
                    evidence=length_diagnostic.message,
                    branch_occupants=state.branch_resource_occupants,
                )
                return _finish_negotiation(
                    problem,
                    state,
                    ResultStatus.FAILED,
                    route_states=total_route_states,
                    routing_iterations=iteration,
                    route_attempts=route_attempts,
                    ripped_net_count=ripped_net_count,
                    ripped_branch_count=ripped_branch_count,
                    diagnostic=length_diagnostic,
                    conflicts=length_conflicts,
                    termination_reason=RoutingTerminationReason.INFEASIBLE,
                )
            if length_targets:
                if group is None:
                    raise RuntimeError("length targets require a routing group")
                if iteration >= maximum_iterations:
                    group_conflicts = attributed_failure_conflicts(
                        net=net_name,
                        kind=RoutingConflictKind.GROUP_CONSTRAINT,
                        blocked=(),
                        affected_group=group.name,
                        reroute_scope=group.reroute_scope,
                        evidence=(
                            "routing group length did not close within the "
                            "iteration budget"
                        ),
                        branch_occupants=state.branch_resource_occupants,
                    )
                    return _finish_negotiation(
                        problem,
                        state,
                        ResultStatus.EXHAUSTED,
                        route_states=total_route_states,
                        routing_iterations=iteration,
                        route_attempts=route_attempts,
                        ripped_net_count=ripped_net_count,
                        ripped_branch_count=ripped_branch_count,
                        diagnostic=Diagnostic(
                            "routing_iteration_exhausted",
                            (
                                "routing could not close group length within the "
                                "negotiation iteration budget"
                            ),
                            group.nets,
                        ),
                        conflicts=group_conflicts,
                        termination_reason=RoutingTerminationReason.ITERATION_BUDGET,
                    )
                affected = frozenset(group.reroute_scope)
                ripped_net_count += len(affected & state.routes_by_net.keys())
                state = state.with_length_targets(length_targets).rip_up(
                    affected,
                    problem.resource_graph,
                )
                iteration += 1
                reroute = _reroute_order(problem, net_name, affected)
                pending = reroute + tuple(
                    net for net in pending if net not in affected
                )
                continue

            overflow_conflicts = _capacity_conflicts(problem, state)
            if overflow_conflicts.conflicts:
                selection = victim_policy.select(
                    overflow_conflicts,
                    routed_order=state.routed_order,
                    routed_nets=state.routes_by_net,
                    reroute_scope_by_net=reroute_scopes,
                    branch_safety=_branch_safety(state),
                )
                if selection is None:
                    return _finish_negotiation(
                        problem,
                        state,
                        ResultStatus.FAILED,
                        route_states=total_route_states,
                        routing_iterations=iteration,
                        route_attempts=route_attempts,
                        ripped_net_count=ripped_net_count,
                        ripped_branch_count=ripped_branch_count,
                        diagnostic=Diagnostic(
                            "routing_capacity_infeasible",
                            (
                                "routing resources remain over capacity without "
                                "a legal victim"
                            ),
                            overflow_conflicts.victim_candidates,
                        ),
                        conflicts=overflow_conflicts,
                        termination_reason=RoutingTerminationReason.INFEASIBLE,
                    )
                if iteration >= maximum_iterations:
                    (
                        returned_state,
                        returned_ripped_nets,
                        returned_ripped_branches,
                    ) = _rip_up_selection(
                        state,
                        selection,
                        problem,
                    )
                    return _finish_negotiation(
                        problem,
                        returned_state,
                        ResultStatus.EXHAUSTED,
                        route_states=total_route_states,
                        routing_iterations=iteration,
                        route_attempts=route_attempts,
                        ripped_net_count=ripped_net_count + returned_ripped_nets,
                        ripped_branch_count=(
                            ripped_branch_count + returned_ripped_branches
                        ),
                        diagnostic=Diagnostic(
                            "routing_iteration_exhausted",
                            (
                                "routing resources remain over capacity at the "
                                "iteration budget"
                            ),
                            overflow_conflicts.victim_candidates,
                        ),
                        conflicts=overflow_conflicts,
                        termination_reason=RoutingTerminationReason.ITERATION_BUDGET,
                    )
                affected = frozenset(selection.reroute_scope)
                penalized = state.with_history_penalty(
                    _conflict_history_penalty(overflow_conflicts)
                )
                state, ripped, ripped_branches = _rip_up_selection(
                    penalized,
                    selection,
                    problem,
                )
                ripped_net_count += ripped
                ripped_branch_count += ripped_branches
                iteration += 1
                reroute = _reroute_order(problem, selection.victim, affected)
                pending = reroute + tuple(
                    net for net in pending if net not in affected
                )
            continue
        attempt_conflicts = _attempt_conflicts(problem, state, net_name, attempt)
        if attempt.status in (ResultStatus.UNSUPPORTED, ResultStatus.EXHAUSTED):
            return _finish_negotiation(
                problem,
                state,
                attempt.status,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                ripped_branch_count=ripped_branch_count,
                diagnostic=attempt.diagnostic,
                conflicts=attempt_conflicts,
                termination_reason=(
                    RoutingTerminationReason.UNSUPPORTED
                    if attempt.status is ResultStatus.UNSUPPORTED
                    else RoutingTerminationReason.STATE_BUDGET
                ),
            )

        selection = victim_policy.select(
            attempt_conflicts,
            routed_order=state.routed_order,
            routed_nets=state.routes_by_net,
            reroute_scope_by_net=reroute_scopes,
            branch_safety=_branch_safety(state),
        )
        if selection is None:
            return _finish_negotiation(
                problem,
                state,
                ResultStatus.FAILED,
                route_states=total_route_states,
                routing_iterations=iteration,
                route_attempts=route_attempts,
                ripped_net_count=ripped_net_count,
                ripped_branch_count=ripped_branch_count,
                diagnostic=attempt.diagnostic,
                conflicts=attempt_conflicts,
                termination_reason=RoutingTerminationReason.INFEASIBLE,
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
                ripped_branch_count=ripped_branch_count,
                diagnostic=Diagnostic(
                    "routing_iteration_exhausted",
                    (
                        "routing could not resolve an attributed conflict within "
                        "the negotiation iteration budget"
                    ),
                    (net_name, selection.victim),
                ),
                conflicts=attempt_conflicts,
                termination_reason=RoutingTerminationReason.ITERATION_BUDGET,
            )

        affected = frozenset(
            problem.policy.reroute_scope(net_name)
            + selection.reroute_scope
        )
        routed_victims = tuple(
            state.routes_by_net[net]
            for net in sorted(affected & state.routes_by_net.keys())
        )
        ripped_net_count += len(routed_victims)
        history_penalty = _conflict_history_penalty(attempt_conflicts)
        if not history_penalty:
            history_penalty = _route_resource_demands(problem, routed_victims)
        penalized = state.with_history_penalty(history_penalty)
        if (
            selection.branch is not None
            and problem.policy.reroute_scope(net_name) == (net_name,)
        ):
            state, ripped, ripped_branches = _rip_up_selection(
                penalized,
                selection,
                problem,
            )
            ripped_net_count -= len(routed_victims)
            ripped_net_count += ripped
            ripped_branch_count += ripped_branches
        else:
            state = penalized.rip_up(affected, problem.resource_graph)
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
        ripped_branch_count=ripped_branch_count,
    )
