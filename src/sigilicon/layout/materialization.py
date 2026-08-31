"""Compile physical-design results into database-neutral write instructions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping
from typing import Iterable

from sigilicon.canonical import canonical_from_json, canonical_json
from sigilicon.layout.physical_design import (
    LayerKind,
    PhysicalDesignJob,
    PhysicalDesignResult,
    Placement,
    Point,
    ResultStatus,
    physical_design_job_id,
    physical_design_result_id,
)
from sigilicon.layout.physical_geometry import placed_sized_rect


class MaterializationError(ValueError):
    """A result cannot be compiled into a trustworthy plan."""


class MaterializationDecision(str, Enum):
    EXECUTABLE = "executable"
    DIAGNOSTIC = "diagnostic"
    REJECTED = "rejected"


class MaterializationReason(str, Enum):
    ACCEPTED = "accepted"
    STATE_BUDGET = "state_budget"
    ITERATION_BUDGET = "iteration_budget"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNSUPPORTED = "unsupported"
    INFEASIBLE = "infeasible"
    INVALID_SOLUTION = "invalid_solution"


class MaterializationOwnerKind(str, Enum):
    INSTANCE = "instance"
    BLOCKAGE = "blockage"
    NET = "net"


@dataclass(frozen=True)
class MaterializationTarget:
    owner: str
    name: str

    def __post_init__(self) -> None:
        for label, value in (("owner", self.owner), ("name", self.name)):
            if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
                raise MaterializationError(
                    f"materialization target {label} must be non-empty text"
                )


@dataclass(frozen=True)
class MaterializationOwner:
    kind: MaterializationOwnerKind
    locator: tuple[str, ...]

    @property
    def stable_name(self) -> str:
        return ":".join((self.kind.value, *self.locator))


@dataclass(frozen=True)
class MaterializationAcceptance:
    decision: MaterializationDecision
    reason: MaterializationReason
    message: str

    @property
    def executable(self) -> bool:
        return self.decision is MaterializationDecision.EXECUTABLE

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class MaterializationProvenance:
    job_identity: str
    result_identity: str
    result_engine: str


@dataclass(frozen=True)
class MaterializedInstancePlacement:
    instance: str
    master: str
    placement: Placement
    owner: MaterializationOwner


@dataclass(frozen=True)
class MaterializedRoutingBlockagePlacement:
    blockage: str
    placement: Placement
    owner: MaterializationOwner


@dataclass(frozen=True)
class MaterializedRouteSegment:
    identity: str
    net: str
    layer: str
    start: Point
    end: Point
    width_dbu: int
    owner: MaterializationOwner


@dataclass(frozen=True)
class MaterializedRouteVia:
    identity: str
    net: str
    via_definition: str
    origin: Point
    owner: MaterializationOwner


@dataclass(frozen=True)
class MaterializationIssue:
    code: str
    message: str
    entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializationValidation:
    valid: bool
    issues: tuple[MaterializationIssue, ...]


@dataclass(frozen=True)
class MaterializationPlan:
    target: MaterializationTarget
    acceptance: MaterializationAcceptance
    provenance: MaterializationProvenance
    instances: tuple[MaterializedInstancePlacement, ...]
    routing_blockages: tuple[MaterializedRoutingBlockagePlacement, ...]
    route_segments: tuple[MaterializedRouteSegment, ...]
    route_vias: tuple[MaterializedRouteVia, ...]
    artifact_id: str

    @property
    def executable(self) -> bool:
        return self.acceptance.executable

    def canonical_json(self) -> str:
        return canonical_json(self)


def materialization_target_from_mapping(value: object) -> MaterializationTarget:
    if not isinstance(value, Mapping):
        raise MaterializationError("materialization target must be an object")
    if set(value) != {"owner", "name"}:
        raise MaterializationError(
            "materialization target fields must be exactly ['name', 'owner']"
        )
    return MaterializationTarget(owner=value["owner"], name=value["name"])


def materialization_plan_from_json(text: str) -> MaterializationPlan:
    return canonical_from_json(text, MaterializationPlan)


def materialization_acceptance_from_json(text: str) -> MaterializationAcceptance:
    return canonical_from_json(text, MaterializationAcceptance)


def _acceptance(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
) -> MaterializationAcceptance:
    if result.status is ResultStatus.SUCCEEDED:
        if result.closed:
            return MaterializationAcceptance(
                MaterializationDecision.EXECUTABLE,
                MaterializationReason.ACCEPTED,
                "all requested physical-design stages are closed",
            )
        return MaterializationAcceptance(
            MaterializationDecision.REJECTED,
            MaterializationReason.INVALID_SOLUTION,
            "successful result does not carry closed typed routing evidence",
        )
    if result.status is ResultStatus.UNSUPPORTED:
        return MaterializationAcceptance(
            MaterializationDecision.REJECTED,
            MaterializationReason.UNSUPPORTED,
            "physical-design result requests an unsupported capability",
        )
    if result.status is ResultStatus.EXHAUSTED:
        return MaterializationAcceptance(
            MaterializationDecision.DIAGNOSTIC,
            MaterializationReason.BUDGET_EXHAUSTED,
            "maximum legal partial result is diagnostic only",
        )
    return MaterializationAcceptance(
        MaterializationDecision.REJECTED,
        MaterializationReason.INFEASIBLE,
        "physical-design result is not closed and cannot be executed",
    )


def _provenance(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
) -> MaterializationProvenance:
    return MaterializationProvenance(
        job_identity=physical_design_job_id(job),
        result_identity=physical_design_result_id(result),
        result_engine=result.provenance.backend,
    )


def _instructions(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
) -> tuple[
    tuple[MaterializedInstancePlacement, ...],
    tuple[MaterializedRoutingBlockagePlacement, ...],
    tuple[MaterializedRouteSegment, ...],
    tuple[MaterializedRouteVia, ...],
]:
    masters = {instance.name: instance.master for instance in job.design.instances}
    instances = tuple(
        MaterializedInstancePlacement(
            item.instance,
            masters.get(item.instance, ""),
            item.placement,
            MaterializationOwner(
                MaterializationOwnerKind.INSTANCE,
                (item.instance,),
            ),
        )
        for item in result.placements
    )
    blockages = tuple(
        MaterializedRoutingBlockagePlacement(
            item.blockage,
            item.placement,
            MaterializationOwner(
                MaterializationOwnerKind.BLOCKAGE,
                (item.blockage,),
            ),
        )
        for item in result.routing_blockage_placements
    )
    segments: list[MaterializedRouteSegment] = []
    vias: list[MaterializedRouteVia] = []
    for route_index, route in enumerate(result.routes):
        owner = MaterializationOwner(MaterializationOwnerKind.NET, (route.net,))
        segments.extend(
            MaterializedRouteSegment(
                f"route:{route_index}:segment:{index}",
                segment.net,
                segment.layer,
                segment.start,
                segment.end,
                segment.width_dbu,
                owner,
            )
            for index, segment in enumerate(route.segments)
        )
        vias.extend(
            MaterializedRouteVia(
                f"route:{route_index}:via:{index}",
                via.net,
                via.via_definition,
                via.origin,
                owner,
            )
            for index, via in enumerate(route.vias)
        )
    return instances, blockages, tuple(segments), tuple(vias)


def _unique(values: Iterable[str]) -> bool:
    items = tuple(values)
    return len(items) == len(set(items))


def validate_materialization_plan(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
) -> MaterializationValidation:
    """Check provenance and every database-neutral write instruction."""

    issues: list[MaterializationIssue] = []

    def issue(code: str, message: str, *entities: str) -> None:
        issues.append(MaterializationIssue(code, message, tuple(entities)))

    if plan.provenance != _provenance(job, result):
        issue("provenance_mismatch", "plan provenance does not identify its job and result")
    if result.provenance.job_identity != physical_design_job_id(job):
        issue("result_input_mismatch", "result does not contain the supplied typed job")
    expected_plan_id = (
        f"{result.artifact_id}:materialization-plan:"
        f"{plan.target.owner}:{plan.target.name}"
    )
    if plan.artifact_id != expected_plan_id:
        issue("plan_id_mismatch", "plan artifact ID disagrees with its typed inputs")
    if plan.acceptance != _acceptance(job, result):
        issue("acceptance_mismatch", "plan acceptance disagrees with the typed result")

    expected = _instructions(job, result)
    actual = (
        plan.instances,
        plan.routing_blockages,
        plan.route_segments,
        plan.route_vias,
    )
    if actual != expected:
        issue("instruction_mismatch", "plan instructions do not exactly lower the result")

    grid = job.technology.manufacturing_grid_dbu
    die = job.design.die
    masters = {item.name: item for item in job.design.masters}
    instances = {item.name: item for item in job.design.instances}
    blockages = {item.name: item for item in job.design.routing_blockages}
    nets = {item.name for item in job.design.nets}
    routing_layers = {
        item.name for item in job.technology.layers if item.kind is LayerKind.ROUTING
    }
    via_definitions = {item.name for item in job.technology.via_definitions}

    if not _unique(item.instance for item in plan.instances):
        issue("duplicate_instance", "plan repeats an instance placement")
    for item in plan.instances:
        source = instances.get(item.instance)
        master = masters.get(item.master)
        expected_owner = MaterializationOwner(
            MaterializationOwnerKind.INSTANCE,
            (item.instance,),
        )
        if source is None or master is None or source.master != item.master:
            issue("unknown_instance", "instance or master is not in the job", item.instance)
            continue
        if item.owner != expected_owner:
            issue("owner_mismatch", "instance placement has the wrong owner", item.instance)
        if item.placement.orientation not in master.allowed_orientations:
            issue("illegal_orientation", "instance orientation is not allowed", item.instance)
        rect = placed_sized_rect(master.width_dbu, master.height_dbu, item.placement)
        if not die.contains(rect):
            issue("coordinate_outside_die", "instance placement is outside the die", item.instance)
        if item.placement.origin.x % grid or item.placement.origin.y % grid:
            issue("coordinate_off_grid", "instance placement is off grid", item.instance)

    if not _unique(item.blockage for item in plan.routing_blockages):
        issue("duplicate_blockage", "plan repeats a Routing Blockage placement")
    for item in plan.routing_blockages:
        source = blockages.get(item.blockage)
        expected_owner = MaterializationOwner(
            MaterializationOwnerKind.BLOCKAGE,
            (item.blockage,),
        )
        if source is None:
            issue("unknown_blockage", "Routing Blockage is not in the job", item.blockage)
            continue
        if item.owner != expected_owner:
            issue("owner_mismatch", "Routing Blockage has the wrong owner", item.blockage)
        if item.placement.orientation not in source.allowed_orientations:
            issue("illegal_orientation", "Routing Blockage orientation is not allowed", item.blockage)
        rect = placed_sized_rect(source.width_dbu, source.height_dbu, item.placement)
        if not die.contains(rect):
            issue("coordinate_outside_die", "Routing Blockage is outside the die", item.blockage)
        if item.placement.origin.x % grid or item.placement.origin.y % grid:
            issue("coordinate_off_grid", "Routing Blockage placement is off grid", item.blockage)

    if not _unique(item.identity for item in (*plan.route_segments, *plan.route_vias)):
        issue("duplicate_route_instruction", "route instruction identities are not unique")
    for item in plan.route_segments:
        expected_owner = MaterializationOwner(MaterializationOwnerKind.NET, (item.net,))
        if item.net not in nets:
            issue("unknown_net", "route segment net is not in the job", item.net)
        if item.layer not in routing_layers:
            issue("unknown_layer", "route segment layer is not routable", item.layer)
        if item.owner != expected_owner:
            issue("owner_mismatch", "route segment has the wrong owner", item.identity)
        coordinates = (item.start.x, item.start.y, item.end.x, item.end.y, item.width_dbu)
        if any(value % grid for value in coordinates):
            issue("coordinate_off_grid", "route segment is off grid", item.identity)
        if item.width_dbu <= 0 or (
            item.start.x != item.end.x and item.start.y != item.end.y
        ) or item.start == item.end:
            issue("invalid_segment", "route segment geometry is invalid", item.identity)
        half_low = item.width_dbu // 2
        half_high = item.width_dbu - half_low
        x_min = min(item.start.x, item.end.x) - half_low
        y_min = min(item.start.y, item.end.y) - half_low
        x_max = max(item.start.x, item.end.x) + half_high
        y_max = max(item.start.y, item.end.y) + half_high
        if (
            x_min < die.x_min
            or y_min < die.y_min
            or x_max > die.x_max
            or y_max > die.y_max
        ):
            issue("coordinate_outside_die", "route segment is outside the die", item.identity)

    for item in plan.route_vias:
        expected_owner = MaterializationOwner(MaterializationOwnerKind.NET, (item.net,))
        if item.net not in nets:
            issue("unknown_net", "route via net is not in the job", item.net)
        if item.via_definition not in via_definitions:
            issue("unknown_via", "route via definition is not in the job", item.via_definition)
        if item.owner != expected_owner:
            issue("owner_mismatch", "route via has the wrong owner", item.identity)
        if item.origin.x % grid or item.origin.y % grid:
            issue("coordinate_off_grid", "route via is off grid", item.identity)
        if not (
            die.x_min <= item.origin.x <= die.x_max
            and die.y_min <= item.origin.y <= die.y_max
        ):
            issue("coordinate_outside_die", "route via is outside the die", item.identity)
        definition = next(
            (
                value
                for value in job.technology.via_definitions
                if value.name == item.via_definition
            ),
            None,
        )
        if definition is not None:
            for shape in (
                *definition.lower_shapes,
                *definition.cut_shapes,
                *definition.upper_shapes,
            ):
                translated = type(shape)(
                    shape.x_min + item.origin.x,
                    shape.y_min + item.origin.y,
                    shape.x_max + item.origin.x,
                    shape.y_max + item.origin.y,
                )
                if not die.contains(translated):
                    issue(
                        "coordinate_outside_die",
                        "route via geometry is outside the die",
                        item.identity,
                    )
                    break

    return MaterializationValidation(not issues, tuple(issues))


def compile_materialization_plan(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    target: MaterializationTarget,
) -> MaterializationPlan:
    """Lower the maximum legal result while refusing false executability."""

    instructions = _instructions(job, result)
    plan = MaterializationPlan(
        target=target,
        acceptance=_acceptance(job, result),
        provenance=_provenance(job, result),
        instances=instructions[0],
        routing_blockages=instructions[1],
        route_segments=instructions[2],
        route_vias=instructions[3],
        artifact_id=(
            f"{result.artifact_id}:materialization-plan:"
            f"{target.owner}:{target.name}"
        ),
    )
    validation = validate_materialization_plan(job, result, plan)
    if not validation.valid:
        raise MaterializationError(
            "invalid materialization inputs: "
            + "; ".join(f"{item.code}: {item.message}" for item in validation.issues)
        )
    return plan


__all__ = [
    "MaterializationAcceptance",
    "MaterializationDecision",
    "MaterializationError",
    "MaterializationIssue",
    "MaterializationOwner",
    "MaterializationOwnerKind",
    "MaterializationPlan",
    "MaterializationProvenance",
    "MaterializationReason",
    "MaterializationTarget",
    "MaterializationValidation",
    "MaterializedInstancePlacement",
    "MaterializedRouteSegment",
    "MaterializedRouteVia",
    "MaterializedRoutingBlockagePlacement",
    "compile_materialization_plan",
    "materialization_acceptance_from_json",
    "materialization_plan_from_json",
    "materialization_target_from_mapping",
    "validate_materialization_plan",
]
