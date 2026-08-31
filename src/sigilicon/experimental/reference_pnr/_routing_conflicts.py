"""Typed conflict attribution and deterministic victim selection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sigilicon.experimental.reference_pnr._routing_resources import (
    BlockedResource,
    RoutingResourceIdentity,
    RoutingResourceKind,
    RoutingResourceOverflow,
)
from sigilicon.layout.physical_design import PhysicalOwnerIdentity
from sigilicon.experimental.reference_pnr.model import (
    RoutingConflictKind,
    RoutingTerminationReason,
)


@dataclass(frozen=True)
class RoutingConflict:
    """One stable, attributed reason that the current route state cannot close."""

    identity: str
    kind: RoutingConflictKind
    resource: RoutingResourceIdentity | None
    aggressor_nets: tuple[str, ...]
    occupant_nets: tuple[str, ...]
    affected_group: str | None
    severity: int
    cost: int
    victim_candidates: tuple[str, ...]
    victim_branches: tuple[str, ...]
    reroute_scope: tuple[str, ...]
    evidence: str
    branch: str | None = None
    physical_owners: tuple[PhysicalOwnerIdentity, ...] = ()
    resource_overflow: int = 0


@dataclass(frozen=True)
class RoutingConflictSet:
    """Canonical collection used by policy, diagnostics, and termination."""

    conflicts: tuple[RoutingConflict, ...] = ()

    @classmethod
    def from_iterable(
        cls,
        conflicts: Iterable[RoutingConflict],
    ) -> RoutingConflictSet:
        by_identity = {conflict.identity: conflict for conflict in conflicts}
        return cls(tuple(by_identity[key] for key in sorted(by_identity)))

    @property
    def resources(self) -> tuple[RoutingResourceIdentity, ...]:
        return tuple(
            sorted(
                {
                    conflict.resource
                    for conflict in self.conflicts
                    if conflict.resource is not None
                }
            )
        )

    @property
    def victim_candidates(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    net
                    for conflict in self.conflicts
                    for net in conflict.victim_candidates
                }
            )
        )

    @property
    def maximum_severity(self) -> int:
        return max((item.severity for item in self.conflicts), default=0)


@dataclass(frozen=True)
class RoutingVictimSelection:
    conflict: str
    victim: str
    reroute_scope: tuple[str, ...]
    branch: str | None = None


@dataclass(frozen=True)
class DeterministicVictimPolicy:
    """Select the latest routed candidate for the strongest stable conflict."""

    def select(
        self,
        conflicts: RoutingConflictSet,
        *,
        routed_order: tuple[str, ...],
        routed_nets: Iterable[str],
        reroute_scope_by_net: Mapping[str, tuple[str, ...]],
        branch_safety: Mapping[str, bool],
    ) -> RoutingVictimSelection | None:
        routed = frozenset(routed_nets)
        priority = {net: index for index, net in enumerate(routed_order)}
        ordered_conflicts = sorted(
            conflicts.conflicts,
            key=lambda conflict: (
                -conflict.severity,
                -conflict.cost,
                conflict.identity,
            ),
        )
        for conflict in ordered_conflicts:
            candidates = routed & frozenset(conflict.victim_candidates)
            if not candidates:
                continue
            victim = max(
                candidates,
                key=lambda net: (priority.get(net, -1), net),
            )
            scope = reroute_scope_by_net.get(victim, (victim,))
            safe_branches = tuple(
                branch
                for branch in conflict.victim_branches
                if branch.startswith(f"{victim}:")
                and branch_safety.get(branch, False)
            )
            branch = (
                safe_branches[0]
                if len(safe_branches) == 1 and scope == (victim,)
                else None
            )
            return RoutingVictimSelection(
                conflict.identity,
                victim,
                scope or (victim,),
                branch,
            )
        return None


@dataclass(frozen=True)
class RoutingTerminationEvidence:
    reason: RoutingTerminationReason
    routing_iterations: int
    route_states: int
    routed_nets: tuple[str, ...]
    conflict_identities: tuple[str, ...] = ()


def capacity_conflicts(
    overflows: Iterable[RoutingResourceOverflow],
    *,
    group_by_net: Mapping[str, str | None],
    reroute_scope_by_net: Mapping[str, tuple[str, ...]],
    branch_occupants: Mapping[RoutingResourceIdentity, tuple[str, ...]],
) -> RoutingConflictSet:
    conflicts: list[RoutingConflict] = []
    for overflow in overflows:
        groups = tuple(
            sorted(
                {
                    group
                    for net in overflow.occupants
                    if (group := group_by_net.get(net)) is not None
                }
            )
        )
        scope = tuple(
            sorted(
                {
                    candidate
                    for net in overflow.occupants
                    for candidate in reroute_scope_by_net.get(net, (net,))
                }
            )
        )
        conflicts.append(
            RoutingConflict(
                identity=(
                    f"capacity:{overflow.resource.stable_name}:"
                    f"{','.join(overflow.occupants)}"
                ),
                kind=(
                    RoutingConflictKind.VIA_EXHAUSTION
                    if overflow.resource.kind == RoutingResourceKind.VIA.value
                    else RoutingConflictKind.CAPACITY_OVERFLOW
                ),
                resource=overflow.resource,
                aggressor_nets=overflow.occupants,
                occupant_nets=overflow.occupants,
                affected_group="+".join(groups) if groups else None,
                severity=overflow.amount,
                cost=overflow.amount,
                victim_candidates=overflow.occupants,
                victim_branches=branch_occupants.get(overflow.resource, ()),
                reroute_scope=scope,
                evidence=(
                    f"resource usage {overflow.usage} exceeds capacity "
                    f"{overflow.capacity}"
                ),
                physical_owners=(),
                resource_overflow=overflow.amount,
            )
        )
    return RoutingConflictSet.from_iterable(conflicts)


def attributed_failure_conflicts(
    *,
    net: str,
    kind: RoutingConflictKind,
    blocked: Iterable[BlockedResource],
    affected_group: str | None,
    reroute_scope: tuple[str, ...],
    evidence: str,
    branch_occupants: Mapping[RoutingResourceIdentity, tuple[str, ...]],
) -> RoutingConflictSet:
    blocked_items = tuple(blocked)
    if not blocked_items:
        return RoutingConflictSet.from_iterable(
            (
                RoutingConflict(
                    identity=f"{kind.value}:{net}:none",
                    kind=kind,
                    resource=None,
                    aggressor_nets=(net,),
                    occupant_nets=(),
                    affected_group=affected_group,
                    severity=1,
                    cost=1,
                    victim_candidates=(),
                    victim_branches=(),
                    reroute_scope=reroute_scope,
                    evidence=evidence,
                    physical_owners=(),
                    resource_overflow=0,
                ),
            )
        )
    return RoutingConflictSet.from_iterable(
        RoutingConflict(
            identity=(
                f"{kind.value}:{net}:"
                f"{item.resource.stable_name if item.resource is not None else 'hard'}:"
                f"{','.join(item.owners)}:"
                f"{','.join(owner.stable_name for owner in item.physical_owners)}"
            ),
            kind=(
                RoutingConflictKind.HARD_BLOCKER
                if item.hard and not item.owners
                else (
                    RoutingConflictKind.VIA_EXHAUSTION
                    if item.resource is not None
                    and item.resource.kind == RoutingResourceKind.VIA.value
                    else kind
                )
            ),
            resource=item.resource,
            aggressor_nets=(net,),
            occupant_nets=item.owners,
            affected_group=affected_group,
            severity=1,
            cost=1,
            victim_candidates=item.owners,
            victim_branches=tuple(
                sorted(
                    set(item.branches)
                    | (
                        set()
                        if item.resource is None
                        else set(branch_occupants.get(item.resource, ()))
                    )
                )
            ),
            reroute_scope=reroute_scope,
            evidence=f"{evidence}: {item.reason}",
            physical_owners=item.physical_owners,
            resource_overflow=0,
        )
        for item in blocked_items
    )
