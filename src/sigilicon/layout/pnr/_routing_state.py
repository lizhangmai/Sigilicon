"""Dynamic route occupancy and history for deterministic negotiation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sigilicon.layout.pnr._geometry import (
    route_segment_shape,
    via_occurrence_shapes,
)
from sigilicon.layout.pnr.model import NetRoute, Rect, ViaDefinition


RoutingCostKey = tuple[str, int, int, str]


@dataclass(frozen=True)
class RouteOccupancy:
    """One exact dynamic shape owned by a routed net."""

    net: str
    layer: str
    shape: Rect


@dataclass(frozen=True)
class RoutingState:
    """Immutable dynamic state updated by negotiated routing."""

    routes_by_net: Mapping[str, NetRoute]
    occupancy_by_layer: Mapping[str, tuple[RouteOccupancy, ...]]
    routed_order: tuple[str, ...]
    history_costs: Mapping[RoutingCostKey, int]
    length_targets: Mapping[str, int]

    @classmethod
    def empty(cls) -> RoutingState:
        return cls(
            routes_by_net=MappingProxyType({}),
            occupancy_by_layer=MappingProxyType({}),
            routed_order=(),
            history_costs=MappingProxyType({}),
            length_targets=MappingProxyType({}),
        )

    @property
    def routes(self) -> tuple[NetRoute, ...]:
        return tuple(
            self.routes_by_net[net] for net in sorted(self.routes_by_net)
        )

    def with_route(
        self,
        route: NetRoute,
        via_definitions: Mapping[str, ViaDefinition],
    ) -> RoutingState:
        routes = dict(self.routes_by_net)
        routes[route.net] = route
        order = tuple(net for net in self.routed_order if net != route.net) + (
            route.net,
        )
        return self._replace_routes(routes, order, via_definitions)

    def rip_up(
        self,
        nets: Iterable[str],
        via_definitions: Mapping[str, ViaDefinition],
    ) -> RoutingState:
        victims = frozenset(nets)
        routes = {
            net: route
            for net, route in self.routes_by_net.items()
            if net not in victims
        }
        order = tuple(net for net in self.routed_order if net not in victims)
        return self._replace_routes(routes, order, via_definitions)

    def with_history_penalty(
        self,
        costs: Mapping[RoutingCostKey, int],
    ) -> RoutingState:
        history = dict(self.history_costs)
        for key, cost in sorted(costs.items()):
            history[key] = history.get(key, 0) + max(1, cost)
        return RoutingState(
            routes_by_net=self.routes_by_net,
            occupancy_by_layer=self.occupancy_by_layer,
            routed_order=self.routed_order,
            history_costs=MappingProxyType(history),
            length_targets=self.length_targets,
        )

    def with_length_targets(self, targets: Mapping[str, int]) -> RoutingState:
        merged = dict(self.length_targets)
        for net, target in sorted(targets.items()):
            merged[net] = max(merged.get(net, 0), target)
        return RoutingState(
            routes_by_net=self.routes_by_net,
            occupancy_by_layer=self.occupancy_by_layer,
            routed_order=self.routed_order,
            history_costs=self.history_costs,
            length_targets=MappingProxyType(merged),
        )

    def select_victim(self, conflicting_nets: Iterable[str]) -> str | None:
        """Choose the latest routed actual blocker, then break ties by name."""

        conflicts = frozenset(conflicting_nets) & self.routes_by_net.keys()
        if not conflicts:
            return None
        priority = {net: index for index, net in enumerate(self.routed_order)}
        return max(conflicts, key=lambda net: (priority.get(net, -1), net))

    def _replace_routes(
        self,
        routes: Mapping[str, NetRoute],
        order: tuple[str, ...],
        via_definitions: Mapping[str, ViaDefinition],
    ) -> RoutingState:
        occupancy: dict[str, list[RouteOccupancy]] = {}
        for net, route in sorted(routes.items()):
            for segment in route.segments:
                occupancy.setdefault(segment.layer, []).append(
                    RouteOccupancy(net, segment.layer, route_segment_shape(segment))
                )
            for route_via in route.vias:
                via = via_definitions[route_via.via_definition]
                for layer, shape in via_occurrence_shapes(via, route_via.origin):
                    occupancy.setdefault(layer, []).append(
                        RouteOccupancy(net, layer, shape)
                    )
        return RoutingState(
            routes_by_net=MappingProxyType(dict(routes)),
            occupancy_by_layer=MappingProxyType(
                {
                    layer: tuple(shapes)
                    for layer, shapes in sorted(occupancy.items())
                }
            ),
            routed_order=order,
            history_costs=self.history_costs,
            length_targets=self.length_targets,
        )
