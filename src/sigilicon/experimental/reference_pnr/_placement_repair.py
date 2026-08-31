"""Compile deterministic, local placement repair candidates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sigilicon.experimental.reference_pnr._legality import (
    hard_constraints_hold,
    instance_region,
    rectangles_conflict,
)
from sigilicon.layout.physical_geometry import placed_rect
from sigilicon.experimental.reference_pnr._placement import (
    _candidate_placements,
    _candidate_sized_placements,
)
from sigilicon.experimental.reference_pnr._routing_ownership import (
    OwnedRoutingRegion,
    RoutingPhysicalOwnership,
    compile_routing_physical_ownership,
)
from sigilicon.experimental.reference_pnr._routing_pressure import (
    RoutingPlacementPressure,
    RoutingPressureSite,
)
from sigilicon.experimental.reference_pnr._routing_problem import (
    RoutingProblem,
    compile_routing_problem,
)
from sigilicon.experimental.reference_pnr._routing_resources import (
    RoutingResourceIdentity,
    RoutingResourceKind,
)
from sigilicon.layout.physical_design import (
    InstancePlacement,
    PhysicalOwnerIdentity,
    PhysicalOwnerKind,
    Placement,
    Point,
    Rect,
    RoutingBlockagePlacement,
)
from sigilicon.experimental.reference_pnr.model import ReferencePnrJob


PlacementIdentity = tuple[tuple[str, str, int, int, str], ...]


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
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...]
    moved_owner: PhysicalOwnerIdentity
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    displacement_dbu: int
    identity: PlacementIdentity
    prediction: PlacementRepairPrediction

    @property
    def moved_instance(self) -> str | None:
        if self.moved_owner.kind is PhysicalOwnerKind.INSTANCE:
            return self.moved_owner.locator[0]
        return None

    @property
    def moved_blockage(self) -> str | None:
        if self.moved_owner.kind is PhysicalOwnerKind.BLOCKAGE:
            return self.moved_owner.locator[0]
        return None


@dataclass(frozen=True)
class PlacementRepairResult:
    status: PlacementRepairStatus
    placements: tuple[InstancePlacement, ...]
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...]
    moved_owner: PhysicalOwnerIdentity | None
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    displacement_dbu: int
    search_states: int
    identity: PlacementIdentity
    prediction: PlacementRepairPrediction | None
    reason: str

    @property
    def moved_instance(self) -> str | None:
        if (
            self.moved_owner is not None
            and self.moved_owner.kind is PhysicalOwnerKind.INSTANCE
        ):
            return self.moved_owner.locator[0]
        return None

    @property
    def moved_blockage(self) -> str | None:
        if (
            self.moved_owner is not None
            and self.moved_owner.kind is PhysicalOwnerKind.BLOCKAGE
        ):
            return self.moved_owner.locator[0]
        return None


@dataclass(frozen=True)
class PlacementRepairProblem:
    """Immutable, ordered local repair search behind one narrow Interface."""

    current_placements: tuple[InstancePlacement, ...]
    current_routing_blockage_placements: tuple[RoutingBlockagePlacement, ...]
    candidates: tuple[PlacementRepairCandidate, ...]
    rejected: frozenset[PlacementIdentity]
    search_states: int
    state_budget_exhausted: bool
    movable_scope: tuple[PhysicalOwnerIdentity, ...]

    def next_candidate(self) -> PlacementRepairResult:
        current_identity = placement_identity(
            self.current_placements,
            self.current_routing_blockage_placements,
        )
        if self.candidates:
            candidate = self.candidates[0]
            return PlacementRepairResult(
                PlacementRepairStatus.REPAIRED,
                candidate.placements,
                candidate.routing_blockage_placements,
                candidate.moved_owner,
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
            self.current_routing_blockage_placements,
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
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] = (),
) -> PlacementIdentity:
    instance_identity = tuple(
        (
            PhysicalOwnerKind.INSTANCE.value,
            item.instance,
            item.placement.origin.x,
            item.placement.origin.y,
            item.placement.orientation.value,
        )
        for item in sorted(placements, key=lambda item: item.instance)
    )
    blockage_identity = tuple(
        (
            PhysicalOwnerKind.BLOCKAGE.value,
            item.blockage,
            item.placement.origin.x,
            item.placement.origin.y,
            item.placement.orientation.value,
        )
        for item in sorted(
            routing_blockage_placements,
            key=lambda item: item.blockage,
        )
    )
    return instance_identity + blockage_identity


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
    repair_owner: PhysicalOwnerIdentity,
) -> tuple[OwnedRoutingRegion, ...]:
    identities = tuple(
        owner.identity
        for owner in site.physical_owner_candidates
        if owner.repair_owner == repair_owner
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
    repair_owner: PhysicalOwnerIdentity,
) -> int:
    if repair_owner.kind is not PhysicalOwnerKind.INSTANCE:
        return 0
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
            if owner.repair_owner != repair_owner:
                continue
            if view.access_states(ownership.terminal_accesses(reference)):
                accessible += 1
    return accessible


def _prediction(
    problem: RoutingProblem,
    pressure: RoutingPlacementPressure,
    current: RoutingPhysicalOwnership,
    proposed: RoutingPhysicalOwnership,
    repair_owner: PhysicalOwnerIdentity,
) -> PlacementRepairPrediction:
    current_access = _terminal_access_count(problem, current, repair_owner)
    proposed_access = _terminal_access_count(problem, proposed, repair_owner)
    pin_gain = max(0, proposed_access - current_access)
    pin_loss = max(0, current_access - proposed_access)
    released: set[RoutingResourceIdentity] = set()
    released_pressure = 0
    remaining_pressure = 0
    for site in pressure.sites:
        if not any(
            owner.repair_owner == repair_owner
            for owner in site.physical_owner_candidates
        ):
            continue
        weight = site.severity + site.cost
        before = any(
            _region_affects_site(problem, site, region)
            for region in _owner_regions(current, site, repair_owner)
        )
        after = any(
            _region_affects_site(problem, site, region)
            for region in _owner_regions(proposed, site, repair_owner)
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
    if candidate.moved_instance is not None:
        placement = next(
            item.placement
            for item in candidate.placements
            if item.instance == candidate.moved_instance
        )
    else:
        placement = next(
            item.placement
            for item in candidate.routing_blockage_placements
            if item.blockage == candidate.moved_blockage
        )
    return (
        -prediction.released_pressure,
        prediction.remaining_pressure,
        -prediction.pin_access_gain,
        prediction.pin_access_loss,
        candidate.displacement_dbu,
        candidate.moved_owner.stable_name,
        placement.origin.y,
        placement.origin.x,
        placement.orientation.value,
        candidate.identity,
    )


def compile_placement_repair_problem(
    job: ReferencePnrJob,
    placements: tuple[InstancePlacement, ...],
    pressure: RoutingPlacementPressure,
    *,
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] | None = None,
    rejected: frozenset[PlacementIdentity] = frozenset(),
) -> PlacementRepairProblem:
    """Compile legal, predicted, and deterministically ordered repair choices."""

    placements = tuple(sorted(placements, key=lambda item: item.instance))
    if routing_blockage_placements is None:
        routing_blockage_placements = tuple(
            RoutingBlockagePlacement(blockage.name, blockage.placement)
            for blockage in sorted(
                job.design.routing_blockages,
                key=lambda item: item.name,
            )
        )
    else:
        routing_blockage_placements = tuple(
            sorted(
                routing_blockage_placements,
                key=lambda item: item.blockage,
            )
        )
    current = {item.instance: item.placement for item in placements}
    current_blockages = {
        item.blockage: item.placement for item in routing_blockage_placements
    }
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    blockages = {
        blockage.name: blockage for blockage in job.design.routing_blockages
    }
    movable_scope = tuple(
        owner
        for owner in pressure.movable_owners
        if (
            owner.kind is PhysicalOwnerKind.INSTANCE
            and owner.locator[0] in instances
            and instances[owner.locator[0]].fixed_placement is None
        )
        or (
            owner.kind is PhysicalOwnerKind.BLOCKAGE
            and owner.locator[0] in blockages
            and blockages[owner.locator[0]].repair_region is not None
        )
    )
    if not movable_scope:
        return PlacementRepairProblem(
            placements,
            routing_blockage_placements,
            (),
            rejected,
            0,
            False,
            (),
        )

    routing_problem = compile_routing_problem(
        job,
        placements,
        routing_blockage_placements,
    )
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
    for repair_owner in movable_scope:
        attributed_owners = tuple(
            sorted(
                {
                    owner.identity
                    for site in pressure.sites
                    for owner in site.physical_owner_candidates
                    if owner.repair_owner == repair_owner
                },
                key=lambda item: item.stable_name,
            )
        )
        if repair_owner.kind is PhysicalOwnerKind.INSTANCE:
            owner_name = repair_owner.locator[0]
            instance = instances[owner_name]
            master = masters[instance.master]
            region = instance_region(job, owner_name)
            if region is None:
                continue
            original = current[owner_name]
            owner_candidates = _candidate_placements(
                master,
                region,
                grid=job.technology.manufacturing_grid_dbu,
            )
        else:
            owner_name = repair_owner.locator[0]
            blockage = blockages[owner_name]
            region = blockage.repair_region
            if region is None:
                continue
            original = current_blockages[owner_name]
            owner_candidates = _candidate_sized_placements(
                blockage.width_dbu,
                blockage.height_dbu,
                blockage.allowed_orientations,
                region,
                grid=job.technology.manufacturing_grid_dbu,
            )

        for candidate, shape in owner_candidates:
            if search_states >= maximum_states:
                state_budget_exhausted = True
                break
            search_states += 1
            if candidate == original:
                continue
            proposed = dict(current)
            proposed_blockages = dict(current_blockages)
            proposed_rectangles = dict(rectangles)
            if repair_owner.kind is PhysicalOwnerKind.INSTANCE:
                if any(
                    rectangles_conflict(shape, other, spacing)
                    for name, other in rectangles.items()
                    if name != owner_name
                ):
                    continue
                proposed[owner_name] = candidate
                proposed_rectangles[owner_name] = shape
                if not hard_constraints_hold(
                    job,
                    proposed_rectangles,
                    proposed,
                ):
                    continue
            else:
                proposed_blockages[owner_name] = candidate
            ordered = tuple(
                InstancePlacement(name, proposed[name]) for name in sorted(proposed)
            )
            ordered_blockages = tuple(
                RoutingBlockagePlacement(name, proposed_blockages[name])
                for name in sorted(proposed_blockages)
            )
            identity = placement_identity(ordered, ordered_blockages)
            if identity in rejected:
                continue
            proposed_ownership = compile_routing_physical_ownership(
                job,
                ordered,
                ordered_blockages,
            )
            displacement = (
                abs(candidate.origin.x - original.origin.x)
                + abs(candidate.origin.y - original.origin.y)
            )
            candidates.append(
                PlacementRepairCandidate(
                    ordered,
                    ordered_blockages,
                    repair_owner,
                    attributed_owners,
                    displacement,
                    identity,
                    _prediction(
                        routing_problem,
                        pressure,
                        current_ownership,
                        proposed_ownership,
                        repair_owner,
                    ),
                )
            )
        if state_budget_exhausted:
            break

    return PlacementRepairProblem(
        placements,
        routing_blockage_placements,
        tuple(sorted(candidates, key=_candidate_key)),
        rejected,
        search_states,
        state_budget_exhausted,
        movable_scope,
    )
