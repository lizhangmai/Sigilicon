"""Compile typed routing conflicts into local placement pressure."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.pnr._routing_conflicts import (
    RoutingConflict,
    RoutingConflictKind,
    RoutingConflictSet,
)
from sigilicon.layout.pnr._routing_ownership import (
    PhysicalOwner,
    PhysicalOwnerKind,
    PhysicalOwnerMobility,
)
from sigilicon.layout.pnr._routing_problem import RoutingProblem
from sigilicon.layout.pnr._routing_resources import RoutingResourceIdentity
from sigilicon.layout.pnr.model import Rect


@dataclass(frozen=True)
class RoutingPressureSite:
    """One stable conflict with complete physical-owner provenance."""

    identity: str
    source_conflict: str
    conflict_kind: RoutingConflictKind
    resource: RoutingResourceIdentity | None
    region: Rect | None
    physical_owner_candidates: tuple[PhysicalOwner, ...]
    involved_nets: tuple[str, ...]
    involved_groups: tuple[str, ...]
    severity: int
    cost: int
    evidence: str
    reason: str
    repair_scope: tuple[str, ...]

    @property
    def instance(self) -> str | None:
        instances = {
            owner.repair_instance
            for owner in self.physical_owner_candidates
            if owner.repair_instance is not None
        }
        return next(iter(instances)) if len(instances) == 1 else None

    @property
    def pin(self) -> str | None:
        pins = {
            owner.identity.locator[-1]
            for owner in self.physical_owner_candidates
            if owner.identity.kind is PhysicalOwnerKind.PIN
        }
        return next(iter(pins)) if len(pins) == 1 else None


@dataclass(frozen=True)
class RoutingPlacementPressure:
    """Stable resource/conflict/owner evidence eligible for placement repair."""

    sites: tuple[RoutingPressureSite, ...] = ()

    @property
    def movable_instances(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    owner.repair_instance
                    for site in self.sites
                    for owner in site.physical_owner_candidates
                    if owner.mobility is PhysicalOwnerMobility.MOVABLE
                    and owner.repair_instance is not None
                }
            )
        )

    @property
    def total_severity(self) -> int:
        return sum(site.severity for site in self.sites)


def _terminal_owners(
    problem: RoutingProblem,
    conflict: RoutingConflict,
) -> tuple[PhysicalOwner, ...]:
    names = frozenset(conflict.aggressor_nets + conflict.occupant_nets)
    identities = {
        problem.physical_ownership.owner_for_reference(reference).identity
        for net_name in names
        if net_name in problem.net_names
        for reference in problem.net(net_name).terminal_references
    }
    return tuple(
        problem.physical_ownership.owner(identity)
        for identity in sorted(
            identities,
            key=lambda item: item.stable_name,
        )
    )


def _owner_candidates(
    problem: RoutingProblem,
    conflict: RoutingConflict,
) -> tuple[tuple[PhysicalOwner, ...], str]:
    if conflict.physical_owners:
        return (
            tuple(
                problem.physical_ownership.owner(identity)
                for identity in conflict.physical_owners
            ),
            "physical blocker ownership",
        )
    if conflict.resource is not None:
        resource_owners = problem.physical_ownership.owners_for_resource(
            problem.resource_graph,
            conflict.resource,
        )
        if resource_owners:
            return resource_owners, "physical owners overlap the affected resource"
    terminal_owners = _terminal_owners(problem, conflict)
    if terminal_owners:
        return terminal_owners, "routing-terminal repair scope"
    return (), "resource-only pressure has no legal physical owner"


def attribute_routing_pressure(
    problem: RoutingProblem,
    conflicts: RoutingConflictSet,
) -> RoutingPlacementPressure:
    sites: list[RoutingPressureSite] = []
    for conflict in conflicts.conflicts:
        candidates, reason = _owner_candidates(problem, conflict)
        involved_nets = tuple(
            sorted(set(conflict.aggressor_nets + conflict.occupant_nets))
        )
        involved_groups = (
            () if conflict.affected_group is None else (conflict.affected_group,)
        )
        repair_scope = tuple(
            sorted(
                {
                    owner.repair_instance
                    for owner in candidates
                    if owner.repair_instance is not None
                }
            )
        )
        sites.append(
            RoutingPressureSite(
                identity=f"pressure:{conflict.identity}",
                source_conflict=conflict.identity,
                conflict_kind=conflict.kind,
                resource=conflict.resource,
                region=(
                    None
                    if conflict.resource is None
                    else problem.resource_graph.bounds(conflict.resource)
                ),
                physical_owner_candidates=candidates,
                involved_nets=involved_nets,
                involved_groups=involved_groups,
                severity=conflict.severity,
                cost=conflict.cost,
                evidence=conflict.evidence,
                reason=reason,
                repair_scope=repair_scope,
            )
        )
    return RoutingPlacementPressure(
        tuple(sorted(sites, key=lambda item: item.identity))
    )
