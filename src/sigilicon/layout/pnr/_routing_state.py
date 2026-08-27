"""Dynamic route occupancy and history for deterministic negotiation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sigilicon.layout.pnr._geometry import route_segment_shape, via_occurrence_shapes
from sigilicon.layout.pnr._routing_resources import (
    RoutingResourceGraph,
    RoutingResourceIdentity,
)
from sigilicon.layout.pnr._routing_tree import RoutingTree, whole_route_tree
from sigilicon.layout.pnr.model import NetRoute, Rect


RoutingCostKey = RoutingResourceIdentity


@dataclass(frozen=True)
class RouteOccupancy:
    """One exact dynamic shape owned by a routed net and optional branch."""

    net: str
    layer: str
    shape: Rect
    branch: str | None = None


@dataclass(frozen=True)
class RoutingState:
    """Immutable dynamic state updated by negotiated routing."""

    routes_by_net: Mapping[str, NetRoute]
    trees_by_net: Mapping[str, RoutingTree]
    occupancy_by_layer: Mapping[str, tuple[RouteOccupancy, ...]]
    resource_usage: Mapping[RoutingResourceIdentity, int]
    resource_occupants: Mapping[RoutingResourceIdentity, tuple[str, ...]]
    branch_resource_occupants: Mapping[
        RoutingResourceIdentity, tuple[str, ...]
    ]
    routed_order: tuple[str, ...]
    history_costs: Mapping[RoutingCostKey, int]
    length_targets: Mapping[str, int]

    @classmethod
    def empty(cls) -> RoutingState:
        empty = MappingProxyType({})
        return cls(empty, empty, empty, empty, empty, empty, (), empty, empty)

    @property
    def routes(self) -> tuple[NetRoute, ...]:
        return tuple(
            self.routes_by_net[net] for net in sorted(self.routes_by_net)
        )

    def with_route(
        self,
        route: NetRoute,
        resource_graph: RoutingResourceGraph,
    ) -> RoutingState:
        return self.with_tree(whole_route_tree(route), resource_graph)

    def with_tree(
        self,
        tree: RoutingTree,
        resource_graph: RoutingResourceGraph,
    ) -> RoutingState:
        trees = dict(self.trees_by_net)
        trees[tree.net] = tree
        order = tuple(net for net in self.routed_order if net != tree.net)
        if tree.complete:
            order += (tree.net,)
        return self._replace_trees(trees, order, resource_graph)

    def rip_up(
        self,
        nets: Iterable[str],
        resource_graph: RoutingResourceGraph,
    ) -> RoutingState:
        victims = frozenset(nets)
        trees = {
            net: tree
            for net, tree in self.trees_by_net.items()
            if net not in victims
        }
        order = tuple(net for net in self.routed_order if net not in victims)
        return self._replace_trees(trees, order, resource_graph)

    def rip_up_branch(
        self,
        branch: str,
        resource_graph: RoutingResourceGraph,
    ) -> RoutingState:
        tree = next(
            (
                candidate
                for candidate in self.trees_by_net.values()
                if candidate.branch(branch) is not None
            ),
            None,
        )
        if tree is None:
            return self
        selected = tree.branch(branch)
        if selected is None or not selected.local_ripup_safe:
            raise ValueError("routing branch is not safe for local rip-up")
        trees = dict(self.trees_by_net)
        trees[tree.net] = tree.without_branch(branch)
        order = tuple(net for net in self.routed_order if net != tree.net)
        return self._replace_trees(trees, order, resource_graph)

    def with_history_penalty(
        self,
        costs: Mapping[RoutingCostKey, int],
    ) -> RoutingState:
        history = dict(self.history_costs)
        for key, cost in sorted(costs.items()):
            history[key] = history.get(key, 0) + max(1, cost)
        return self._replace_dynamic(
            history_costs=MappingProxyType(history),
            length_targets=self.length_targets,
        )

    def with_length_targets(self, targets: Mapping[str, int]) -> RoutingState:
        merged = dict(self.length_targets)
        for net, target in sorted(targets.items()):
            merged[net] = max(merged.get(net, 0), target)
        return self._replace_dynamic(
            history_costs=self.history_costs,
            length_targets=MappingProxyType(merged),
        )

    def usage_without(
        self,
        net: str,
    ) -> Mapping[RoutingResourceIdentity, int]:
        return MappingProxyType(
            {
                resource: len(tuple(owner for owner in owners if owner != net))
                for resource, owners in self.resource_occupants.items()
                if any(owner != net for owner in owners)
            }
        )

    def _replace_dynamic(
        self,
        *,
        history_costs: Mapping[RoutingCostKey, int],
        length_targets: Mapping[str, int],
    ) -> RoutingState:
        return RoutingState(
            self.routes_by_net,
            self.trees_by_net,
            self.occupancy_by_layer,
            self.resource_usage,
            self.resource_occupants,
            self.branch_resource_occupants,
            self.routed_order,
            history_costs,
            length_targets,
        )

    def _replace_trees(
        self,
        trees: Mapping[str, RoutingTree],
        order: tuple[str, ...],
        resource_graph: RoutingResourceGraph,
    ) -> RoutingState:
        occupancy: dict[str, list[RouteOccupancy]] = {}
        resource_occupants: dict[RoutingResourceIdentity, set[str]] = {}
        branch_occupants: dict[RoutingResourceIdentity, set[str]] = {}
        routes: dict[str, NetRoute] = {}
        for net, tree in sorted(trees.items()):
            if tree.complete:
                routes[net] = tree.route
            for branch in tree.branches:
                for demand in resource_graph.route_demands(branch.route):
                    resource_occupants.setdefault(demand.resource, set()).add(net)
                    branch_occupants.setdefault(demand.resource, set()).add(
                        branch.identity
                    )
                for segment in branch.segments:
                    occupancy.setdefault(segment.layer, []).append(
                        RouteOccupancy(
                            net,
                            segment.layer,
                            route_segment_shape(segment),
                            branch.identity,
                        )
                    )
                for route_via in branch.vias:
                    via = resource_graph.via_definitions[
                        route_via.via_definition
                    ]
                    for layer, shape in via_occurrence_shapes(
                        via, route_via.origin
                    ):
                        occupancy.setdefault(layer, []).append(
                            RouteOccupancy(net, layer, shape, branch.identity)
                        )
        return RoutingState(
            routes_by_net=MappingProxyType(routes),
            trees_by_net=MappingProxyType(dict(trees)),
            occupancy_by_layer=MappingProxyType(
                {
                    layer: tuple(shapes)
                    for layer, shapes in sorted(occupancy.items())
                }
            ),
            resource_usage=MappingProxyType(
                {
                    resource: len(nets)
                    for resource, nets in sorted(resource_occupants.items())
                }
            ),
            resource_occupants=MappingProxyType(
                {
                    resource: tuple(sorted(nets))
                    for resource, nets in sorted(resource_occupants.items())
                }
            ),
            branch_resource_occupants=MappingProxyType(
                {
                    resource: tuple(sorted(branches))
                    for resource, branches in sorted(branch_occupants.items())
                }
            ),
            routed_order=order,
            history_costs=self.history_costs,
            length_targets=self.length_targets,
        )
