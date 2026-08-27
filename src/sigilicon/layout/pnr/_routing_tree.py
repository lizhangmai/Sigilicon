"""Internal multi-terminal route trees with stable branch ownership."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.pnr._routing_resources import (
    RoutingNode,
    RoutingResourceGraph,
    RoutingResourceIdentity,
)
from sigilicon.layout.pnr.model import NetRoute, RouteSegment, RouteVia


@dataclass(frozen=True)
class RouteBranch:
    identity: str
    net: str
    endpoint_index: int
    nodes: tuple[RoutingNode, ...]
    segments: tuple[RouteSegment, ...]
    vias: tuple[RouteVia, ...]
    local_ripup_safe: bool

    @property
    def route(self) -> NetRoute:
        return NetRoute(self.net, self.segments, self.vias)


@dataclass(frozen=True)
class RoutingTree:
    """A deterministic trunk-and-branch representation behind ``NetRoute``."""

    net: str
    expected_branches: tuple[str, ...]
    branches: tuple[RouteBranch, ...]

    @property
    def complete(self) -> bool:
        return tuple(branch.identity for branch in self.branches) == (
            self.expected_branches
        )

    @property
    def route(self) -> NetRoute:
        segments = tuple(
            dict.fromkeys(
                segment for branch in self.branches for segment in branch.segments
            )
        )
        vias = tuple(
            dict.fromkeys(via for branch in self.branches for via in branch.vias)
        )
        return NetRoute(self.net, segments, vias)

    def branch(self, identity: str) -> RouteBranch | None:
        return next(
            (branch for branch in self.branches if branch.identity == identity),
            None,
        )

    def without_branch(self, identity: str) -> RoutingTree:
        return RoutingTree(
            self.net,
            self.expected_branches,
            tuple(branch for branch in self.branches if branch.identity != identity),
        )

    def branch_occupants(
        self,
        graph: RoutingResourceGraph,
    ) -> dict[RoutingResourceIdentity, tuple[str, ...]]:
        occupants: dict[RoutingResourceIdentity, set[str]] = {}
        for branch in self.branches:
            for demand in graph.route_demands(branch.route):
                occupants.setdefault(demand.resource, set()).add(branch.identity)
        return {
            resource: tuple(sorted(branches))
            for resource, branches in sorted(occupants.items())
        }


def whole_route_tree(route: NetRoute) -> RoutingTree:
    """Represent geometry without branch provenance as one unsafe primary branch."""

    identity = f"{route.net}:primary"
    return RoutingTree(
        route.net,
        (identity,),
        (
            RouteBranch(
                identity,
                route.net,
                0,
                (),
                route.segments,
                route.vias,
                False,
            ),
        ),
    )
