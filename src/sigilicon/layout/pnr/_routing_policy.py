"""Compile typed Routing Constraints into reference-router policy."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sigilicon.layout.pnr.model import (
    LayerShape,
    RoutingConstraint,
    RoutingLayerConstraint,
    RoutingRegionConstraint,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingViaCountConstraint,
)


@dataclass(frozen=True)
class NetRoutingPolicy:
    """All search-time policy for one net after constraint compilation."""

    allowed_layers: frozenset[str] | None
    maximum_vias: int | None
    required_regions: tuple[LayerShape, ...]
    congestion_cost_enabled: bool


@dataclass(frozen=True)
class RoutingPolicy:
    """Immutable reference-router policy indexed by net name."""

    by_net: Mapping[str, NetRoutingPolicy]

    def for_net(self, net: str) -> NetRoutingPolicy:
        return self.by_net[net]


def compile_routing_policy(
    constraints: tuple[RoutingConstraint, ...],
    net_names: tuple[str, ...],
) -> RoutingPolicy:
    """Translate declarative constraints once at the router seam."""

    layer_sets: dict[str, list[frozenset[str]]] = {}
    via_limits: dict[str, list[int]] = {}
    required_regions: dict[str, list[LayerShape]] = {}
    coupled_nets: set[str] = set()

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
        elif isinstance(constraint, RoutingSkewConstraint):
            coupled_nets.update(constraint.nets)
        elif isinstance(constraint, RoutingShieldConstraint):
            coupled_nets.update((constraint.signal_net, constraint.shield_net))

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
            congestion_cost_enabled=net not in coupled_nets,
        )
    return RoutingPolicy(MappingProxyType(policies))
