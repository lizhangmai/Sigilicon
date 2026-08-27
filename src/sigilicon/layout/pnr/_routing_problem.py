"""Compile normalized design intent into immutable router input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sigilicon.layout.pnr._routing_ownership import (
    OwnedRoutingRegion,
    RoutingPhysicalOwnership,
    compile_routing_physical_ownership,
)
from sigilicon.layout.pnr._routing_policy import RoutingPolicy, compile_routing_policy
from sigilicon.layout.pnr._routing_resources import (
    RoutingDomain,
    RoutingLayerDomain,
    RoutingResourceGraph,
    compile_routing_resource_graph,
)
from sigilicon.layout.pnr.model import (
    InstancePlacement,
    LayerShape,
    PhysicalDesignJob,
    PinReference,
    ResultStatus,
    ViaDefinition,
)


@dataclass(frozen=True)
class RoutingProblemIssue:
    """A static capability or legality issue found while compiling the problem."""

    status: ResultStatus
    code: str


@dataclass(frozen=True)
class NetRoutingProblem:
    """All static route-search input for one net."""

    name: str
    terminal_references: tuple[PinReference, ...]
    terminal_accesses: tuple[tuple[LayerShape, ...], ...]
    static_blockers: Mapping[str, tuple[OwnedRoutingRegion, ...]]


@dataclass(frozen=True)
class RoutingProblem:
    """Compiled static problem consumed by search and routing iteration."""

    domain: RoutingDomain
    layers: Mapping[str, RoutingLayerDomain]
    resource_graph: RoutingResourceGraph
    policy: RoutingPolicy
    nets: tuple[NetRoutingProblem, ...]
    physical_ownership: RoutingPhysicalOwnership
    movable_instances: frozenset[str]
    issue: RoutingProblemIssue | None
    _nets_by_name: Mapping[str, NetRoutingProblem]

    @property
    def net_names(self) -> tuple[str, ...]:
        return tuple(net.name for net in self.nets)

    def net(self, name: str) -> NetRoutingProblem:
        return self._nets_by_name[name]

    @property
    def vias(self) -> tuple[ViaDefinition, ...]:
        return self.resource_graph.vias

    @property
    def via_definitions(self) -> Mapping[str, ViaDefinition]:
        return self.resource_graph.via_definitions

    def nets_in_order(
        self,
        names: tuple[str, ...] | None = None,
    ) -> tuple[NetRoutingProblem, ...]:
        if names is None:
            return self.nets
        return tuple(self._nets_by_name[name] for name in names)


def _freeze_blockers(
    blockers: Mapping[str, list[OwnedRoutingRegion]],
) -> Mapping[str, tuple[OwnedRoutingRegion, ...]]:
    return MappingProxyType(
        {
            layer: tuple(sorted(shapes, key=lambda item: item.identity))
            for layer, shapes in sorted(blockers.items())
        }
    )


def compile_routing_problem(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
) -> RoutingProblem:
    """Interpret a job and Placement Solution once for all routing callers."""

    resource_graph = compile_routing_resource_graph(
        job.technology,
        job.design.die,
        congestion_bins_x=job.execution_policy.routing_congestion_bins_x,
        congestion_bins_y=job.execution_policy.routing_congestion_bins_y,
    )
    issue = (
        None
        if resource_graph.issue is None
        else RoutingProblemIssue(
            (
                ResultStatus.UNSUPPORTED
                if resource_graph.issue.unsupported
                else ResultStatus.FAILED
            ),
            resource_graph.issue.code,
        )
    )
    ownership = compile_routing_physical_ownership(job, instance_placements)
    nets = {net.name: net for net in job.design.nets}
    policy = compile_routing_policy(job.routing_constraints, tuple(sorted(nets)))

    compiled_nets: list[NetRoutingProblem] = []
    for net_name, net in sorted(nets.items()):
        connected = frozenset(net.pins)
        blockers: dict[str, list[OwnedRoutingRegion]] = {}
        for region in ownership.blocking_regions(connected):
            blockers.setdefault(region.layer, []).append(region)
        compiled_nets.append(
            NetRoutingProblem(
                name=net_name,
                terminal_references=net.pins,
                terminal_accesses=tuple(
                    ownership.terminal_accesses(reference)
                    for reference in net.pins
                ),
                static_blockers=_freeze_blockers(blockers),
            )
        )

    compiled_nets_tuple = tuple(compiled_nets)
    return RoutingProblem(
        domain=resource_graph.domain,
        layers=resource_graph.layers,
        resource_graph=resource_graph,
        policy=policy,
        nets=compiled_nets_tuple,
        physical_ownership=ownership,
        movable_instances=frozenset(
            instance.name
            for instance in job.design.instances
            if instance.fixed_placement is None
        ),
        issue=issue,
        _nets_by_name=MappingProxyType(
            {net.name: net for net in compiled_nets_tuple}
        ),
    )
