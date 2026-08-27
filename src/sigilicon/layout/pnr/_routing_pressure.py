"""Attribute terminal routing conflicts to local placement pressure."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.pnr._routing_conflicts import RoutingConflictSet
from sigilicon.layout.pnr._routing_problem import RoutingProblem
from sigilicon.layout.pnr._routing_resources import RoutingResourceIdentity
from sigilicon.layout.pnr.model import Rect


@dataclass(frozen=True)
class RoutingPressureSite:
    identity: str
    net: str
    instance: str | None
    pin: str | None
    resource: RoutingResourceIdentity | None
    region: Rect | None
    severity: int
    reason: str


@dataclass(frozen=True)
class RoutingPlacementPressure:
    """Stable resource and terminal evidence eligible for local placement repair."""

    sites: tuple[RoutingPressureSite, ...] = ()

    @property
    def movable_instances(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    site.instance
                    for site in self.sites
                    if site.instance is not None
                }
            )
        )

    @property
    def total_severity(self) -> int:
        return sum(site.severity for site in self.sites)


def attribute_routing_pressure(
    problem: RoutingProblem,
    conflicts: RoutingConflictSet,
) -> RoutingPlacementPressure:
    sites: dict[str, RoutingPressureSite] = {}
    for conflict in conflicts.conflicts:
        candidate_nets = conflict.aggressor_nets + conflict.occupant_nets
        for net_name in candidate_nets:
            if net_name not in problem.net_names:
                continue
            net = problem.net(net_name)
            movable_terminals = tuple(
                reference
                for reference in net.terminal_references
                if reference.instance in problem.movable_instances
            )
            if not movable_terminals:
                identity = f"pressure:{conflict.identity}:{net_name}:resource"
                sites[identity] = RoutingPressureSite(
                    identity,
                    net_name,
                    None,
                    None,
                    conflict.resource,
                    (
                        None
                        if conflict.resource is None
                        else problem.resource_graph.bounds(conflict.resource)
                    ),
                    conflict.severity,
                    conflict.evidence,
                )
                continue
            for reference in movable_terminals:
                identity = (
                    f"pressure:{conflict.identity}:{net_name}:"
                    f"{reference.instance}:{reference.pin}"
                )
                sites[identity] = RoutingPressureSite(
                    identity,
                    net_name,
                    reference.instance,
                    reference.pin,
                    conflict.resource,
                    (
                        None
                        if conflict.resource is None
                        else problem.resource_graph.bounds(conflict.resource)
                    ),
                    conflict.severity,
                    conflict.evidence,
                )
    return RoutingPlacementPressure(
        tuple(sites[identity] for identity in sorted(sites))
    )
