"""Compile typed Routing Constraints into reference-router policy."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sigilicon.layout.pnr.model import (
    LayerShape,
    RoutingConstraint,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
    RoutingRegionConstraint,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingViaCountConstraint,
)


@dataclass(frozen=True)
class RoutingLengthWindow:
    """Inclusive legal route-length interval for one net."""

    minimum_dbu: int = 0
    maximum_dbu: int | None = None

    @property
    def target_dbu(self) -> int | None:
        if self.maximum_dbu == self.minimum_dbu:
            return self.minimum_dbu
        return None

    @property
    def feasible(self) -> bool:
        return self.maximum_dbu is None or self.minimum_dbu <= self.maximum_dbu


@dataclass(frozen=True)
class RoutingCostPolicy:
    """Explicit weights used by search and negotiated routing."""

    congestion_weight: int
    history_weight: int
    group_violation_weight: int


@dataclass(frozen=True)
class RoutingOrderDependency:
    """Require one group member's route before another member is attempted."""

    before: str
    after: str


@dataclass(frozen=True)
class LengthMatchPolicy:
    """One compiled skew relationship within a routing group."""

    constraint: str
    nets: tuple[str, ...]
    maximum_skew_dbu: int


@dataclass(frozen=True)
class ShieldRoutingPolicy:
    """One compiled signal/shield relationship within a routing group."""

    constraint: str
    signal_net: str
    shield_net: str
    maximum_spacing_dbu: int
    layers: tuple[str, ...]


@dataclass(frozen=True)
class NetRoutingPolicy:
    """All search-time policy for one net after constraint compilation."""

    allowed_layers: frozenset[str] | None
    maximum_vias: int | None
    required_regions: tuple[LayerShape, ...]
    length_window: RoutingLengthWindow
    cost: RoutingCostPolicy


@dataclass(frozen=True)
class RoutingGroupPolicy:
    """Compiled multi-net closure and reroute policy."""

    name: str
    constraints: tuple[str, ...]
    nets: tuple[str, ...]
    length_windows: Mapping[str, RoutingLengthWindow]
    length_matches: tuple[LengthMatchPolicy, ...]
    shields: tuple[ShieldRoutingPolicy, ...]
    order_dependencies: tuple[RoutingOrderDependency, ...]
    reroute_scope: tuple[str, ...]
    cost: RoutingCostPolicy


@dataclass(frozen=True)
class RoutingPolicy:
    """Immutable per-net and multi-net policy consumed by the router."""

    by_net: Mapping[str, NetRoutingPolicy]
    groups: tuple[RoutingGroupPolicy, ...]
    route_order: tuple[str, ...]
    _group_by_net: Mapping[str, RoutingGroupPolicy]

    def for_net(self, net: str) -> NetRoutingPolicy:
        return self.by_net[net]

    def group_for_net(self, net: str) -> RoutingGroupPolicy | None:
        return self._group_by_net.get(net)

    def reroute_scope(self, net: str) -> tuple[str, ...]:
        group = self.group_for_net(net)
        return (net,) if group is None else group.reroute_scope


@dataclass(frozen=True)
class _Relationship:
    constraint: str
    nets: tuple[str, ...]
    length_match: LengthMatchPolicy | None = None
    shield: ShieldRoutingPolicy | None = None


def _length_windows(
    constraints: tuple[RoutingConstraint, ...],
    net_names: tuple[str, ...],
) -> Mapping[str, RoutingLengthWindow]:
    minimums = {net: 0 for net in net_names}
    maximums: dict[str, list[int]] = {net: [] for net in net_names}
    for constraint in constraints:
        if not isinstance(constraint, RoutingLengthConstraint):
            continue
        minimums[constraint.net] = max(
            minimums[constraint.net],
            constraint.minimum_length_dbu,
        )
        if constraint.maximum_length_dbu is not None:
            maximums[constraint.net].append(constraint.maximum_length_dbu)
    return MappingProxyType(
        {
            net: RoutingLengthWindow(
                minimum_dbu=minimums[net],
                maximum_dbu=min(maximums[net]) if maximums[net] else None,
            )
            for net in net_names
        }
    )


def _relationships(
    constraints: tuple[RoutingConstraint, ...],
) -> tuple[_Relationship, ...]:
    relationships: list[_Relationship] = []
    for constraint in constraints:
        if isinstance(constraint, RoutingSkewConstraint):
            match = LengthMatchPolicy(
                constraint=constraint.name,
                nets=constraint.nets,
                maximum_skew_dbu=constraint.maximum_skew_dbu,
            )
            relationships.append(
                _Relationship(constraint.name, constraint.nets, length_match=match)
            )
        elif isinstance(constraint, RoutingShieldConstraint):
            shield = ShieldRoutingPolicy(
                constraint=constraint.name,
                signal_net=constraint.signal_net,
                shield_net=constraint.shield_net,
                maximum_spacing_dbu=constraint.maximum_spacing_dbu,
                layers=constraint.layers,
            )
            relationships.append(
                _Relationship(
                    constraint.name,
                    (constraint.signal_net, constraint.shield_net),
                    shield=shield,
                )
            )
    return tuple(relationships)


def _relationship_groups(
    relationships: tuple[_Relationship, ...],
    windows: Mapping[str, RoutingLengthWindow],
) -> tuple[RoutingGroupPolicy, ...]:
    if not relationships:
        return ()

    parents = {
        net: net for relationship in relationships for net in relationship.nets
    }

    def find(net: str) -> str:
        parent = parents[net]
        if parent != net:
            parents[net] = find(parent)
        return parents[net]

    def union(first: str, second: str) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[max(first_root, second_root)] = min(first_root, second_root)

    for relationship in relationships:
        for net in relationship.nets[1:]:
            union(relationship.nets[0], net)

    grouped: dict[str, list[_Relationship]] = {}
    for relationship in relationships:
        grouped.setdefault(find(relationship.nets[0]), []).append(relationship)

    group_cost = RoutingCostPolicy(
        congestion_weight=0,
        history_weight=1,
        group_violation_weight=1,
    )
    groups: list[RoutingGroupPolicy] = []
    for grouped_relationships in grouped.values():
        nets = tuple(
            sorted(
                {
                    net
                    for relationship in grouped_relationships
                    for net in relationship.nets
                }
            )
        )
        shields = tuple(
            sorted(
                (
                    relationship.shield
                    for relationship in grouped_relationships
                    if relationship.shield is not None
                ),
                key=lambda item: item.constraint,
            )
        )
        dependencies = tuple(
            sorted(
                {
                    RoutingOrderDependency(shield.signal_net, shield.shield_net)
                    for shield in shields
                },
                key=lambda item: (item.before, item.after),
            )
        )
        constraints = tuple(
            sorted(relationship.constraint for relationship in grouped_relationships)
        )
        groups.append(
            RoutingGroupPolicy(
                name="routing-group:" + ",".join(nets),
                constraints=constraints,
                nets=nets,
                length_windows=MappingProxyType(
                    {net: windows[net] for net in nets}
                ),
                length_matches=tuple(
                    sorted(
                        (
                            relationship.length_match
                            for relationship in grouped_relationships
                            if relationship.length_match is not None
                        ),
                        key=lambda item: item.constraint,
                    )
                ),
                shields=shields,
                order_dependencies=dependencies,
                reroute_scope=nets,
                cost=group_cost,
            )
        )
    return tuple(sorted(groups, key=lambda group: group.nets))


def _route_order(
    net_names: tuple[str, ...],
    groups: tuple[RoutingGroupPolicy, ...],
) -> tuple[str, ...]:
    predecessors: dict[str, set[str]] = {net: set() for net in net_names}
    successors: dict[str, set[str]] = {net: set() for net in net_names}
    for group in groups:
        for dependency in group.order_dependencies:
            predecessors[dependency.after].add(dependency.before)
            successors[dependency.before].add(dependency.after)

    ready = sorted(net for net, required in predecessors.items() if not required)
    ordered: list[str] = []
    while ready:
        net = ready.pop(0)
        ordered.append(net)
        for successor in sorted(successors[net]):
            predecessors[successor].discard(net)
            if not predecessors[successor] and successor not in ordered:
                ready.append(successor)
        ready.sort()
    ordered.extend(sorted(set(net_names) - set(ordered)))
    return tuple(ordered)


def compile_routing_policy(
    constraints: tuple[RoutingConstraint, ...],
    net_names: tuple[str, ...],
) -> RoutingPolicy:
    """Translate declarative constraints once at the router seam."""

    layer_sets: dict[str, list[frozenset[str]]] = {}
    via_limits: dict[str, list[int]] = {}
    required_regions: dict[str, list[LayerShape]] = {}

    for constraint in constraints:
        if isinstance(constraint, RoutingLayerConstraint):
            layer_sets.setdefault(constraint.net, []).append(
                frozenset(constraint.allowed_layers)
            )
        elif isinstance(constraint, RoutingViaCountConstraint):
            via_limits.setdefault(constraint.net, []).append(
                constraint.maximum_vias
            )
        elif isinstance(constraint, RoutingRegionConstraint):
            required_regions.setdefault(constraint.net, []).extend(
                constraint.required_regions
            )

    windows = _length_windows(constraints, net_names)
    groups = _relationship_groups(_relationships(constraints), windows)
    group_by_net = {
        net: group for group in groups for net in group.nets
    }
    independent_cost = RoutingCostPolicy(
        congestion_weight=1,
        history_weight=1,
        group_violation_weight=0,
    )
    policies: dict[str, NetRoutingPolicy] = {}
    for net in net_names:
        allowed = layer_sets.get(net, [])
        allowed_layers = None
        if allowed:
            intersection = set(allowed[0])
            for layer_set in allowed[1:]:
                intersection.intersection_update(layer_set)
            allowed_layers = frozenset(intersection)
        limits = via_limits.get(net, [])
        policies[net] = NetRoutingPolicy(
            allowed_layers=allowed_layers,
            maximum_vias=min(limits) if limits else None,
            required_regions=tuple(required_regions.get(net, ())),
            length_window=windows[net],
            cost=(
                group_by_net[net].cost
                if net in group_by_net
                else independent_cost
            ),
        )
    return RoutingPolicy(
        by_net=MappingProxyType(policies),
        groups=groups,
        route_order=_route_order(net_names, groups),
        _group_by_net=MappingProxyType(group_by_net),
    )
