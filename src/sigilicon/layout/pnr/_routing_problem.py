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
    PhysicalInstance,
    PhysicalMaster,
    PhysicalPort,
    PinReference,
    Placement,
    Rect,
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
    terminal_accesses: tuple[tuple[LayerShape, ...], ...]
    static_blockers: Mapping[str, tuple[Rect, ...]]


@dataclass(frozen=True)
class RoutingProblem:
    """Compiled static problem consumed by search and routing iteration."""

    domain: RoutingDomain
    layers: Mapping[str, RoutingLayerDomain]
    resource_graph: RoutingResourceGraph
    policy: RoutingPolicy
    nets: tuple[NetRoutingProblem, ...]
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
    placements = {item.instance: item.placement for item in instance_placements}
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    ports = {port.name: port for port in job.design.ports}
    nets = {net.name: net for net in job.design.nets}
    policy = compile_routing_policy(job.routing_constraints, tuple(sorted(nets)))

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
            layer: list(shapes) for layer, shapes in fixed_obstructions.items()
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

    compiled_nets_tuple = tuple(compiled_nets)
    return RoutingProblem(
        domain=resource_graph.domain,
        layers=resource_graph.layers,
        resource_graph=resource_graph,
        policy=policy,
        nets=compiled_nets_tuple,
        issue=issue,
        _nets_by_name=MappingProxyType(
            {net.name: net for net in compiled_nets_tuple}
        ),
    )
