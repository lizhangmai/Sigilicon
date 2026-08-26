"""Validation and evaluation for typed Routing Constraints."""

from __future__ import annotations

from sigilicon.layout.pnr.model import (
    ConstraintOutcome,
    ConstraintStatus,
    NetRoute,
    PhysicalDesignJob,
    RoutingConstraint,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
    RoutingViaCountConstraint,
)


def validate_routing_constraint(
    constraint: RoutingConstraint,
    *,
    known_nets: frozenset[str],
    known_layers: frozenset[str],
    grid: int,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(
        constraint,
        (RoutingLayerConstraint, RoutingLengthConstraint, RoutingViaCountConstraint),
    ):
        return (
            f"routing constraint has unknown type {type(constraint).__name__}",
        )
    if not constraint.name:
        errors.append("routing constraint names must be non-empty")
    if constraint.net not in known_nets:
        errors.append(
            f"routing constraint {constraint.name} uses unknown net {constraint.net}"
        )
    if isinstance(constraint, RoutingLayerConstraint):
        if not constraint.allowed_layers:
            errors.append(
                f"routing layer constraint {constraint.name} needs an allowed layer"
            )
        if len(set(constraint.allowed_layers)) != len(constraint.allowed_layers):
            errors.append(
                f"routing layer constraint {constraint.name} repeats a layer"
            )
        unknown = tuple(
            layer for layer in constraint.allowed_layers if layer not in known_layers
        )
        if unknown:
            errors.append(
                f"routing layer constraint {constraint.name} uses unknown layers: "
                f"{', '.join(unknown)}"
            )
    elif isinstance(constraint, RoutingLengthConstraint):
        if constraint.minimum_length_dbu < 0 or (
            constraint.maximum_length_dbu is not None
            and constraint.maximum_length_dbu < constraint.minimum_length_dbu
        ):
            errors.append(
                f"routing length constraint {constraint.name} has an invalid range"
            )
        if constraint.minimum_length_dbu % grid != 0 or (
            constraint.maximum_length_dbu is not None
            and constraint.maximum_length_dbu % grid != 0
        ):
            errors.append(
                f"routing length constraint {constraint.name} must be on-grid"
            )
    elif constraint.maximum_vias < 0:
        errors.append(
            f"routing via constraint {constraint.name} maximum must be non-negative"
        )
    return tuple(errors)


def allowed_routing_layers(
    job: PhysicalDesignJob,
    net: str,
) -> frozenset[str] | None:
    constraints = tuple(
        constraint
        for constraint in job.routing_constraints
        if isinstance(constraint, RoutingLayerConstraint) and constraint.net == net
    )
    if not constraints:
        return None
    allowed = set(constraints[0].allowed_layers)
    for constraint in constraints[1:]:
        allowed.intersection_update(constraint.allowed_layers)
    return frozenset(allowed)


def maximum_vias(job: PhysicalDesignJob, net: str) -> int | None:
    limits = tuple(
        constraint.maximum_vias
        for constraint in job.routing_constraints
        if isinstance(constraint, RoutingViaCountConstraint)
        and constraint.net == net
    )
    return min(limits) if limits else None


def _route_length(route: NetRoute) -> int:
    return sum(
        abs(segment.end.x - segment.start.x)
        + abs(segment.end.y - segment.start.y)
        for segment in route.segments
    )


def evaluate_routing_constraints(
    job: PhysicalDesignJob,
    routes: tuple[NetRoute, ...],
) -> tuple[ConstraintOutcome, ...]:
    route_by_net = {route.net: route for route in routes}
    vias = {via.name: via for via in job.technology.via_definitions}
    outcomes: list[ConstraintOutcome] = []
    for constraint in job.routing_constraints:
        route = route_by_net.get(constraint.net)
        if route is None:
            outcomes.append(
                ConstraintOutcome(
                    constraint.name,
                    ConstraintStatus.NOT_EVALUATED,
                    "routing constraint has no completed Routing Solution",
                )
            )
            continue
        satisfied = True
        message = "routing constraint is satisfied"
        if isinstance(constraint, RoutingLayerConstraint):
            used_layers = {segment.layer for segment in route.segments}
            for route_via in route.vias:
                via = vias.get(route_via.via_definition)
                if via is not None:
                    used_layers.update((via.lower_layer, via.upper_layer))
            disallowed = sorted(used_layers - set(constraint.allowed_layers))
            satisfied = not disallowed
            if disallowed:
                message = f"route uses disallowed layers: {', '.join(disallowed)}"
        elif isinstance(constraint, RoutingLengthConstraint):
            length = _route_length(route)
            satisfied = length >= constraint.minimum_length_dbu and (
                constraint.maximum_length_dbu is None
                or length <= constraint.maximum_length_dbu
            )
            if not satisfied:
                message = f"route length {length} dbu is outside the allowed range"
        elif isinstance(constraint, RoutingViaCountConstraint):
            count = len(route.vias)
            satisfied = count <= constraint.maximum_vias
            if not satisfied:
                message = (
                    f"route uses {count} vias; maximum is {constraint.maximum_vias}"
                )
        outcomes.append(
            ConstraintOutcome(
                constraint.name,
                (
                    ConstraintStatus.SATISFIED
                    if satisfied
                    else ConstraintStatus.VIOLATED
                ),
                message,
            )
        )
    return tuple(outcomes)
