"""Compile deterministic, local placement repair candidates."""

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
from sigilicon.layout.pnr._routing_ownership import (
    OwnedRoutingRegion,
    PhysicalOwnerIdentity,
    RoutingPhysicalOwnership,
    compile_routing_physical_ownership,
)
from sigilicon.layout.pnr._routing_pressure import (
    RoutingPlacementPressure,
    RoutingPressureSite,
)
from sigilicon.layout.pnr._routing_problem import (
    RoutingProblem,
    compile_routing_problem,
)
from sigilicon.layout.pnr._routing_resources import (
    RoutingResourceIdentity,
    RoutingResourceKind,
)
from sigilicon.layout.pnr.model import (
    InstancePlacement,
    PhysicalDesignJob,
    Placement,
    Point,
    Rect,
)


PlacementIdentity = tuple[tuple[str, int, int, str], ...]


class PlacementRepairStatus(str, Enum):
    REPAIRED = "repaired"
    INFEASIBLE = "infeasible"
    UNSUPPORTED = "unsupported"
    STATE_BUDGET = "state_budget"


@dataclass(frozen=True)
class PlacementRepairPrediction:
    released_resources: tuple[RoutingResourceIdentity, ...]
    released_pressure: int
    remaining_pressure: int
    pin_access_gain: int
    pin_access_loss: int


@dataclass(frozen=True)
class PlacementRepairCandidate:
    placements: tuple[InstancePlacement, ...]
    moved_instance: str
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    displacement_dbu: int
    identity: PlacementIdentity
    prediction: PlacementRepairPrediction


@dataclass(frozen=True)
class PlacementRepairResult:
    status: PlacementRepairStatus
    placements: tuple[InstancePlacement, ...]
    moved_instance: str | None
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    displacement_dbu: int
    search_states: int
    identity: PlacementIdentity
    prediction: PlacementRepairPrediction | None
    reason: str


@dataclass(frozen=True)
class PlacementRepairProblem:
    """Immutable, ordered local repair search behind one narrow Interface."""

    current_placements: tuple[InstancePlacement, ...]
    candidates: tuple[PlacementRepairCandidate, ...]
    rejected: frozenset[PlacementIdentity]
    search_states: int
    state_budget_exhausted: bool
    movable_scope: tuple[str, ...]

    def next_candidate(self) -> PlacementRepairResult:
        current_identity = placement_identity(self.current_placements)
        if self.candidates:
            candidate = self.candidates[0]
            return PlacementRepairResult(
                PlacementRepairStatus.REPAIRED,
                candidate.placements,
                candidate.moved_instance,
                candidate.attributed_owners,
                candidate.displacement_dbu,
                self.search_states,
                candidate.identity,
                candidate.prediction,
                "selected the strongest legal attributed repair candidate",
            )
        if not self.movable_scope:
            status = PlacementRepairStatus.UNSUPPORTED
            reason = "routing pressure has no supported movable physical owner"
        elif self.state_budget_exhausted:
            status = PlacementRepairStatus.STATE_BUDGET
            reason = "placement repair exhausted its candidate-state budget"
        else:
            status = PlacementRepairStatus.INFEASIBLE
            reason = (
                "all legal local placement repairs were rejected"
                if self.rejected
                else "no legal local placement repair exists"
            )
        return PlacementRepairResult(
            status,
            self.current_placements,
            None,
            (),
            0,
            self.search_states,
            current_identity,
            None,
            reason,
        )


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


def _expanded(rectangle: Rect, distance: int) -> Rect:
    return Rect(
        rectangle.x_min - distance,
        rectangle.y_min - distance,
        rectangle.x_max + distance,
        rectangle.y_max + distance,
    )


def _point_in_interior(point: Point, rectangle: Rect) -> bool:
    return (
        rectangle.x_min < point.x < rectangle.x_max
        and rectangle.y_min < point.y < rectangle.y_max
    )


def _owner_regions(
    ownership: RoutingPhysicalOwnership,
    site: RoutingPressureSite,
    instance: str,
) -> tuple[OwnedRoutingRegion, ...]:
    identities = tuple(
        owner.identity
        for owner in site.physical_owner_candidates
        if owner.repair_instance == instance
    )
    return tuple(
        region
        for identity in identities
        for region in ownership.regions_for_owner(identity)
    )


def _region_affects_site(
    problem: RoutingProblem,
    site: RoutingPressureSite,
    region: OwnedRoutingRegion,
) -> bool:
    if site.region is None:
        return False
    resource = site.resource
    if (
        resource is not None
        and resource.kind != RoutingResourceKind.VIA.value
        and resource.layer != region.layer
    ):
        return False
    context = problem.layers.get(region.layer)
    margin = 0 if context is None else context.width // 2 + context.spacing
    affected = _expanded(region.shape, margin) if margin else region.shape
    if (
        resource is not None
        and resource.kind != RoutingResourceKind.GRIDLESS_CORRIDOR.value
    ):
        coordinates = tuple(
            item for item in resource.locator if isinstance(item, int)
        )
        if (
            resource.kind
            in (
                RoutingResourceKind.LAYER_SEGMENT.value,
                RoutingResourceKind.EXPLICIT_TRACK.value,
            )
            and len(coordinates) >= 4
        ):
            return any(
                _point_in_interior(Point(x, y), affected)
                for x, y in (
                    coordinates[-4:-2],
                    coordinates[-2:],
                )
            )
        center = Point(
            (site.region.x_min + site.region.x_max) // 2,
            (site.region.y_min + site.region.y_max) // 2,
        )
        return _point_in_interior(center, affected)
    return affected.intersection(site.region) is not None


def _terminal_access_count(
    problem: RoutingProblem,
    ownership: RoutingPhysicalOwnership,
    instance: str,
) -> int:
    accessible = 0
    for net in problem.nets:
        policy = problem.policy.for_net(net.name)
        view = problem.resource_graph.search_view(
            allowed_layers=policy.allowed_layers,
            allow_vias=policy.maximum_vias != 0,
            obstacles={},
            present_usage={},
            history_costs={},
            present_weight=0,
            history_weight=0,
            path_length_weight=0,
        )
        for reference in net.terminal_references:
            owner = ownership.owner_for_reference(reference)
            if owner.repair_instance != instance:
                continue
            if view.access_states(ownership.terminal_accesses(reference)):
                accessible += 1
    return accessible


def _prediction(
    problem: RoutingProblem,
    pressure: RoutingPlacementPressure,
    current: RoutingPhysicalOwnership,
    proposed: RoutingPhysicalOwnership,
    instance: str,
) -> PlacementRepairPrediction:
    current_access = _terminal_access_count(problem, current, instance)
    proposed_access = _terminal_access_count(problem, proposed, instance)
    pin_gain = max(0, proposed_access - current_access)
    pin_loss = max(0, current_access - proposed_access)
    released: set[RoutingResourceIdentity] = set()
    released_pressure = 0
    remaining_pressure = 0
    for site in pressure.sites:
        if not any(
            owner.repair_instance == instance
            for owner in site.physical_owner_candidates
        ):
            continue
        weight = site.severity + site.cost
        before = any(
            _region_affects_site(problem, site, region)
            for region in _owner_regions(current, site, instance)
        )
        after = any(
            _region_affects_site(problem, site, region)
            for region in _owner_regions(proposed, site, instance)
        )
        if before and not after:
            released_pressure += weight
            if site.resource is not None:
                released.add(site.resource)
        elif after:
            remaining_pressure += weight
        elif site.region is None:
            if pin_gain:
                released_pressure += weight
            else:
                remaining_pressure += weight
    return PlacementRepairPrediction(
        tuple(sorted(released)),
        released_pressure,
        remaining_pressure,
        pin_gain,
        pin_loss,
    )


def _candidate_key(
    candidate: PlacementRepairCandidate,
) -> tuple[object, ...]:
    prediction = candidate.prediction
    placement = next(
        item.placement
        for item in candidate.placements
        if item.instance == candidate.moved_instance
    )
    return (
        -prediction.released_pressure,
        prediction.remaining_pressure,
        -prediction.pin_access_gain,
        prediction.pin_access_loss,
        candidate.displacement_dbu,
        candidate.moved_instance,
        placement.origin.y,
        placement.origin.x,
        placement.orientation.value,
        candidate.identity,
    )


def compile_placement_repair_problem(
    job: PhysicalDesignJob,
    placements: tuple[InstancePlacement, ...],
    pressure: RoutingPlacementPressure,
    *,
    rejected: frozenset[PlacementIdentity] = frozenset(),
) -> PlacementRepairProblem:
    """Compile legal, predicted, and deterministically ordered repair choices."""

    placements = tuple(sorted(placements, key=lambda item: item.instance))
    current = {item.instance: item.placement for item in placements}
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    movable_scope = tuple(
        instance
        for instance in pressure.movable_instances
        if instance in instances and instances[instance].fixed_placement is None
    )
    if not movable_scope:
        return PlacementRepairProblem(
            placements,
            (),
            rejected,
            0,
            False,
            (),
        )

    routing_problem = compile_routing_problem(job, placements)
    current_ownership = routing_problem.physical_ownership
    rectangles = {
        name: placed_rect(masters[instances[name].master], placement)
        for name, placement in current.items()
    }
    spacing = job.request.minimum_instance_spacing_dbu
    maximum_states = job.execution_policy.maximum_placement_repair_states
    search_states = 0
    state_budget_exhausted = False
    candidates: list[PlacementRepairCandidate] = []
    for instance_name in movable_scope:
        instance = instances[instance_name]
        master = masters[instance.master]
        region = instance_region(job, instance_name)
        if region is None:
            continue
        original = current[instance_name]
        attributed_owners = tuple(
            sorted(
                {
                    owner.identity
                    for site in pressure.sites
                    for owner in site.physical_owner_candidates
                    if owner.repair_instance == instance_name
                },
                key=lambda item: item.stable_name,
            )
        )
        for candidate, shape in _candidate_placements(
            master,
            region,
            grid=job.technology.manufacturing_grid_dbu,
        ):
            if search_states >= maximum_states:
                state_budget_exhausted = True
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
                InstancePlacement(name, proposed[name])
                for name in sorted(proposed)
            )
            identity = placement_identity(ordered)
            if identity in rejected:
                continue
            proposed_ownership = compile_routing_physical_ownership(job, ordered)
            displacement = (
                abs(candidate.origin.x - original.origin.x)
                + abs(candidate.origin.y - original.origin.y)
            )
            candidates.append(
                PlacementRepairCandidate(
                    ordered,
                    instance_name,
                    attributed_owners,
                    displacement,
                    identity,
                    _prediction(
                        routing_problem,
                        pressure,
                        current_ownership,
                        proposed_ownership,
                        instance_name,
                    ),
                )
            )
        if state_budget_exhausted:
            break

    return PlacementRepairProblem(
        placements,
        tuple(sorted(candidates, key=_candidate_key)),
        rejected,
        search_states,
        state_budget_exhausted,
        movable_scope,
    )
