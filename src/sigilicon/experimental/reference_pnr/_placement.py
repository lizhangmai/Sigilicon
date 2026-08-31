"""Deterministic reference placement implementation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sigilicon.experimental.reference_pnr._constraints import (
    constraint_penalty,
    constraint_outcomes,
    evaluate_constraint,
)
from sigilicon.experimental.reference_pnr._legality import (
    hard_constraints_hold,
    instance_region,
    rectangles_conflict,
)
from sigilicon.experimental.reference_pnr._objectives import objective_unit, objective_value
from sigilicon.layout.physical_geometry import oriented_size, placed_rect
from sigilicon.experimental.reference_pnr.model import (
    ArrayConstraint,
    ConstraintMode,
    ConstraintOutcome,
    Diagnostic,
    InstancePlacement,
    Metric,
    Orientation,
    ReferencePnrJob,
    PhysicalInstance,
    PhysicalMaster,
    Placement,
    Point,
    PhysicalDesignStage,
    Rect,
    ResultStatus,
    StageReport,
)


@dataclass(frozen=True)
class PlacementSolveResult:
    status: ResultStatus
    placements: tuple[InstancePlacement, ...]
    constraint_outcomes: tuple[ConstraintOutcome, ...]
    report: StageReport


def _snap_up(value: int, grid: int) -> int:
    return -(-value // grid) * grid


def _candidate_placements(
    master: PhysicalMaster,
    region: Rect,
    *,
    grid: int,
) -> Iterator[tuple[Placement, Rect]]:
    yield from _candidate_sized_placements(
        master.width_dbu,
        master.height_dbu,
        master.allowed_orientations,
        region,
        grid=grid,
    )


def _candidate_sized_placements(
    width_dbu: int,
    height_dbu: int,
    allowed_orientations: tuple[Orientation, ...],
    region: Rect,
    *,
    grid: int,
) -> Iterator[tuple[Placement, Rect]]:
    for orientation in allowed_orientations:
        width, height = oriented_size(width_dbu, height_dbu, orientation)
        x_start = _snap_up(region.x_min, grid)
        y_start = _snap_up(region.y_min, grid)
        x_stop = region.x_max - width
        y_stop = region.y_max - height
        if x_start > x_stop or y_start > y_stop:
            continue
        for y in range(y_start, y_stop + 1, grid):
            for x in range(x_start, x_stop + 1, grid):
                placement = Placement(Point(x, y), orientation)
                yield placement, Rect(x, y, x + width, y + height)


def _search_order(
    instances: tuple[PhysicalInstance, ...],
    job: ReferencePnrJob,
    masters: dict[str, PhysicalMaster],
) -> tuple[PhysicalInstance, ...]:
    array_rank: dict[str, int] = {}
    for constraint in job.constraints:
        if not isinstance(constraint, ArrayConstraint):
            continue
        for index, instance_name in enumerate(constraint.instances):
            array_rank[instance_name] = min(
                index,
                array_rank.get(instance_name, index),
            )
    unconstrained_rank = len(job.design.instances)
    return tuple(
        sorted(
            instances,
            key=lambda instance: (
                array_rank.get(instance.name, unconstrained_rank),
                -(
                    masters[instance.master].width_dbu
                    * masters[instance.master].height_dbu
                ),
                instance.name,
            ),
        )
    )


def _placement_key(placements: dict[str, Placement]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            name,
            placements[name].origin.x,
            placements[name].origin.y,
            placements[name].orientation.value,
        )
        for name in sorted(placements)
    )


def _placement_score(
    job: ReferencePnrJob,
    rectangles: dict[str, Rect],
    placements: dict[str, Placement],
) -> tuple[float, float, tuple[tuple[str, float, str], ...]]:
    soft_penalty = sum(
        constraint.weight * constraint_penalty(constraint, rectangles, placements)
        for constraint in job.constraints
        if constraint.mode is ConstraintMode.SOFT
    )
    objective_values = tuple(
        (
            objective.name,
            objective_value(objective, job, rectangles, placements),
            objective_unit(objective),
        )
        for objective in job.request.objectives
    )
    objectives_by_name = {name: value for name, value, _ in objective_values}
    objective_cost = sum(
        objective.weight * objectives_by_name[objective.name]
        for objective in job.request.objectives
    )
    return soft_penalty + objective_cost, soft_penalty, objective_values


def _ordered_placements(
    placements: dict[str, Placement],
) -> tuple[InstancePlacement, ...]:
    return tuple(
        InstancePlacement(instance=name, placement=placements[name])
        for name in sorted(placements)
    )


def _failure(
    *,
    code: str,
    message: str,
    entities: tuple[str, ...],
    placements: dict[str, Placement],
    rectangles: dict[str, Rect],
    job: ReferencePnrJob,
    status: ResultStatus = ResultStatus.FAILED,
    search_states: int = 0,
) -> PlacementSolveResult:
    return PlacementSolveResult(
        status=status,
        placements=_ordered_placements(placements),
        constraint_outcomes=constraint_outcomes(
            job.constraints,
            rectangles,
            placements,
        ),
        report=StageReport(
            stage=PhysicalDesignStage.PLACEMENT,
            status=status,
            diagnostics=(Diagnostic(code=code, message=message, entities=entities),),
            metrics=(Metric("search_states", search_states, "count"),),
        ),
    )


def solve_placement(job: ReferencePnrJob) -> PlacementSolveResult:
    masters = {master.name: master for master in job.design.masters}
    spacing = job.request.minimum_instance_spacing_dbu
    placements: dict[str, Placement] = {}
    rectangles: dict[str, Rect] = {}

    fixed = tuple(
        sorted(
            (
                instance
                for instance in job.design.instances
                if instance.fixed_placement is not None
            ),
            key=lambda instance: instance.name,
        )
    )
    for instance in fixed:
        placement = instance.fixed_placement
        assert placement is not None
        shape = placed_rect(masters[instance.master], placement)
        region = instance_region(job, instance.name)
        if region is None or not region.contains(shape):
            return _failure(
                code="fixed_instance_outside_region",
                message=f"fixed instance {instance.name} is outside its legal region",
                entities=(instance.name,),
                placements=placements,
                rectangles=rectangles,
                job=job,
            )
        conflicts = tuple(
            name
            for name, other in rectangles.items()
            if rectangles_conflict(shape, other, spacing)
        )
        if conflicts:
            return _failure(
                code="fixed_instance_overlap",
                message=f"fixed instance {instance.name} overlaps another fixed instance",
                entities=(instance.name, *conflicts),
                placements=placements,
                rectangles=rectangles,
                job=job,
            )
        placements[instance.name] = placement
        rectangles[instance.name] = shape

    if not hard_constraints_hold(job, rectangles, placements):
        return _failure(
            code="fixed_constraint_violation",
            message="fixed placements violate a hard placement constraint",
            entities=tuple(instance.name for instance in fixed),
            placements=placements,
            rectangles=rectangles,
            job=job,
        )

    movable = _search_order(
        tuple(
            instance
            for instance in job.design.instances
            if instance.fixed_placement is None
        ),
        job,
        masters,
    )
    empty_regions = tuple(
        instance.name
        for instance in movable
        if instance_region(job, instance.name) is None
    )
    if empty_regions:
        return _failure(
            code="empty_instance_region",
            message="instances have empty legal regions",
            entities=empty_regions,
            placements=placements,
            rectangles=rectangles,
            job=job,
        )

    search_states = 0
    exhausted = False
    optimizing = bool(job.request.objectives) or any(
        constraint.mode is ConstraintMode.SOFT for constraint in job.constraints
    )
    best_placements: dict[str, Placement] | None = None
    best_rectangles: dict[str, Rect] | None = None
    best_key: tuple[float, tuple[tuple[object, ...], ...]] | None = None

    def search(index: int) -> bool:
        nonlocal search_states, exhausted, best_placements, best_rectangles, best_key
        if index == len(movable):
            if all(
                evaluate_constraint(constraint, rectangles, placements) is True
                for constraint in job.constraints
                if constraint.mode is ConstraintMode.HARD
            ):
                score, _, _ = _placement_score(job, rectangles, placements)
                candidate_key = score, _placement_key(placements)
                if best_key is None or candidate_key < best_key:
                    best_key = candidate_key
                    best_placements = dict(placements)
                    best_rectangles = dict(rectangles)
                return not optimizing
            return False

        instance = movable[index]
        master = masters[instance.master]
        region = instance_region(job, instance.name)
        assert region is not None
        for placement, shape in _candidate_placements(
            master,
            region,
            grid=job.technology.manufacturing_grid_dbu,
        ):
            if search_states >= job.execution_policy.maximum_search_states:
                exhausted = True
                return True
            search_states += 1
            if any(
                rectangles_conflict(shape, other, spacing)
                for other in rectangles.values()
            ):
                continue
            placements[instance.name] = placement
            rectangles[instance.name] = shape
            stop = False
            if hard_constraints_hold(job, rectangles, placements):
                stop = search(index + 1)
            placements.pop(instance.name)
            rectangles.pop(instance.name)
            if stop:
                return True
        return False

    search(0)
    if best_placements is None or best_rectangles is None or (exhausted and optimizing):
        status = ResultStatus.EXHAUSTED if exhausted else ResultStatus.FAILED
        code = "placement_search_exhausted" if exhausted else "placement_infeasible"
        message = (
            "reference placer exhausted its deterministic search limit"
            if exhausted
            else "reference placer found no placement satisfying all hard constraints"
        )
        return _failure(
            code=code,
            message=message,
            entities=tuple(instance.name for instance in movable),
            placements=placements,
            rectangles=rectangles,
            job=job,
            status=status,
            search_states=search_states,
        )

    placements, rectangles = best_placements, best_rectangles
    total_score, soft_penalty, objective_values = _placement_score(
        job,
        rectangles,
        placements,
    )
    occupied_area = sum(shape.area for shape in rectangles.values())
    return PlacementSolveResult(
        status=ResultStatus.SUCCEEDED,
        placements=_ordered_placements(placements),
        constraint_outcomes=constraint_outcomes(
            job.constraints,
            rectangles,
            placements,
        ),
        report=StageReport(
            stage=PhysicalDesignStage.PLACEMENT,
            status=ResultStatus.SUCCEEDED,
            metrics=(
                Metric("placed_instance_count", len(placements), "count"),
                Metric("occupied_area", occupied_area, "dbu^2"),
                Metric(
                    "die_utilization",
                    occupied_area / job.design.die.area,
                    "ratio",
                ),
                Metric("search_states", search_states, "count"),
                Metric("placement_score", total_score, "score"),
                Metric("soft_constraint_penalty", soft_penalty, "score"),
                *(
                    Metric(f"objective.{name}", value, unit)
                    for name, value, unit in objective_values
                ),
            ),
        ),
    )
