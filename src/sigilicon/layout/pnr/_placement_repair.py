"""Deterministic local placement repair from attributed routing pressure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sigilicon.layout.pnr._legality import (
    hard_constraints_hold,
    instance_region,
    placed_rect,
    rectangles_conflict,
)
from sigilicon.layout.pnr._placement import _candidate_placements
from sigilicon.layout.pnr._routing_pressure import RoutingPlacementPressure
from sigilicon.layout.pnr.model import (
    InstancePlacement,
    PhysicalDesignJob,
    Placement,
)


PlacementIdentity = tuple[tuple[str, int, int, str], ...]


class PlacementRepairStatus(str, Enum):
    REPAIRED = "repaired"
    INFEASIBLE = "infeasible"
    UNSUPPORTED = "unsupported"
    STATE_BUDGET = "state_budget"


@dataclass(frozen=True)
class PlacementRepairResult:
    status: PlacementRepairStatus
    placements: tuple[InstancePlacement, ...]
    moved_instance: str | None
    displacement_dbu: int
    search_states: int
    identity: PlacementIdentity
    reason: str


def placement_identity(
    placements: tuple[InstancePlacement, ...],
) -> PlacementIdentity:
    return tuple(
        (
            item.instance,
            item.placement.origin.x,
            item.placement.origin.y,
            item.placement.orientation.value,
        )
        for item in sorted(placements, key=lambda item: item.instance)
    )


def repair_placement(
    job: PhysicalDesignJob,
    placements: tuple[InstancePlacement, ...],
    pressure: RoutingPlacementPressure,
    *,
    rejected: frozenset[PlacementIdentity],
) -> PlacementRepairResult:
    current = {item.instance: item.placement for item in placements}
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    movable = tuple(
        instance
        for instance in pressure.movable_instances
        if instance in instances and instances[instance].fixed_placement is None
    )
    current_identity = placement_identity(placements)
    if not movable:
        return PlacementRepairResult(
            PlacementRepairStatus.UNSUPPORTED,
            placements,
            None,
            0,
            0,
            current_identity,
            "routing pressure is not attributed to a movable instance",
        )

    rectangles = {
        name: placed_rect(masters[instances[name].master], placement)
        for name, placement in current.items()
    }
    spacing = job.request.minimum_instance_spacing_dbu
    search_states = 0
    best: tuple[
        tuple[int, int, int, str, str],
        str,
        Placement,
        PlacementIdentity,
    ] | None = None
    for instance_name in movable:
        instance = instances[instance_name]
        master = masters[instance.master]
        region = instance_region(job, instance_name)
        if region is None:
            continue
        original = current[instance_name]
        for candidate, shape in _candidate_placements(
            master,
            region,
            grid=job.technology.manufacturing_grid_dbu,
        ):
            if search_states >= job.execution_policy.maximum_search_states:
                break
            search_states += 1
            if candidate == original:
                continue
            if any(
                rectangles_conflict(shape, other, spacing)
                for name, other in rectangles.items()
                if name != instance_name
            ):
                continue
            proposed = dict(current)
            proposed[instance_name] = candidate
            proposed_rectangles = dict(rectangles)
            proposed_rectangles[instance_name] = shape
            if not hard_constraints_hold(
                job,
                proposed_rectangles,
                proposed,
            ):
                continue
            ordered = tuple(
                InstancePlacement(name, proposed[name]) for name in sorted(proposed)
            )
            identity = placement_identity(ordered)
            if identity in rejected:
                continue
            displacement = (
                abs(candidate.origin.x - original.origin.x)
                + abs(candidate.origin.y - original.origin.y)
            )
            key = (
                displacement,
                candidate.origin.y,
                candidate.origin.x,
                candidate.orientation.value,
                instance_name,
            )
            if best is None or key < best[0]:
                best = key, instance_name, candidate, identity

    if best is None:
        status = (
            PlacementRepairStatus.STATE_BUDGET
            if search_states >= job.execution_policy.maximum_search_states
            else PlacementRepairStatus.INFEASIBLE
        )
        return PlacementRepairResult(
            status,
            placements,
            None,
            0,
            search_states,
            current_identity,
            (
                "placement repair exhausted its candidate-state budget"
                if status is PlacementRepairStatus.STATE_BUDGET
                else "no legal local placement repair exists"
            ),
        )

    key, moved_instance, candidate, identity = best
    proposed = dict(current)
    proposed[moved_instance] = candidate
    ordered = tuple(
        InstancePlacement(name, proposed[name]) for name in sorted(proposed)
    )
    return PlacementRepairResult(
        PlacementRepairStatus.REPAIRED,
        ordered,
        moved_instance,
        key[0],
        search_states,
        identity,
        "moved one routing-pressure-attributed instance",
    )
