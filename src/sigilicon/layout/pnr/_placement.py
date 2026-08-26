"""Deterministic reference placement implementation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sigilicon.layout.pnr._constraints import (
    constraint_outcomes,
    evaluate_constraint,
)
from sigilicon.layout.pnr.model import (
    ArrayConstraint,
    ConstraintOutcome,
    Diagnostic,
    FenceConstraint,
    InstancePlacement,
    Metric,
    Orientation,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalMaster,
    Placement,
    Point,
    PnrStage,
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


def _dimensions(master: PhysicalMaster, orientation: Orientation) -> tuple[int, int]:
    if orientation in {
        Orientation.R90,
        Orientation.R270,
        Orientation.MXR90,
        Orientation.MYR90,
    }:
        return master.height_dbu, master.width_dbu
    return master.width_dbu, master.height_dbu


def _placed_rect(master: PhysicalMaster, placement: Placement) -> Rect:
    width, height = _dimensions(master, placement.orientation)
    return Rect(
        x_min=placement.origin.x,
        y_min=placement.origin.y,
        x_max=placement.origin.x + width,
        y_max=placement.origin.y + height,
    )


def _snap_up(value: int, grid: int) -> int:
    return -(-value // grid) * grid


def _conflicts(candidate: Rect, other: Rect, spacing: int) -> bool:
    return not (
        candidate.x_max + spacing <= other.x_min
        or other.x_max + spacing <= candidate.x_min
        or candidate.y_max + spacing <= other.y_min
        or other.y_max + spacing <= candidate.y_min
    )


def _instance_region(job: PhysicalDesignJob, instance_name: str) -> Rect | None:
    region: Rect | None = job.design.die
    for constraint in job.constraints:
        if not isinstance(constraint, FenceConstraint):
            continue
        if instance_name not in constraint.instances:
            continue
        region = region.intersection(constraint.region) if region is not None else None
    return region


def _candidate_placements(
    master: PhysicalMaster,
    region: Rect,
    *,
    grid: int,
) -> Iterator[tuple[Placement, Rect]]:
    for orientation in master.allowed_orientations:
        width, height = _dimensions(master, orientation)
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
    job: PhysicalDesignJob,
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


def _hard_constraints_hold(
    job: PhysicalDesignJob,
    rectangles: dict[str, Rect],
    placements: dict[str, Placement],
) -> bool:
    return all(
        evaluate_constraint(constraint, rectangles, placements) is not False
        for constraint in job.constraints
    )


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
    job: PhysicalDesignJob,
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
            stage=PnrStage.PLACEMENT,
            status=status,
            diagnostics=(Diagnostic(code=code, message=message, entities=entities),),
            metrics=(Metric("search_states", search_states, "count"),),
        ),
    )


def solve_placement(job: PhysicalDesignJob) -> PlacementSolveResult:
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
        shape = _placed_rect(masters[instance.master], placement)
        region = _instance_region(job, instance.name)
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
            name for name, other in rectangles.items() if _conflicts(shape, other, spacing)
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

    if not _hard_constraints_hold(job, rectangles, placements):
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
        if _instance_region(job, instance.name) is None
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

    def search(index: int) -> tuple[dict[str, Placement], dict[str, Rect]] | None:
        nonlocal search_states, exhausted
        if index == len(movable):
            if all(
                evaluate_constraint(constraint, rectangles, placements) is True
                for constraint in job.constraints
            ):
                return dict(placements), dict(rectangles)
            return None

        instance = movable[index]
        master = masters[instance.master]
        region = _instance_region(job, instance.name)
        assert region is not None
        for placement, shape in _candidate_placements(
            master,
            region,
            grid=job.technology.manufacturing_grid_dbu,
        ):
            if search_states >= job.request.maximum_search_states:
                exhausted = True
                return None
            search_states += 1
            if any(
                _conflicts(shape, other, spacing)
                for other in rectangles.values()
            ):
                continue
            placements[instance.name] = placement
            rectangles[instance.name] = shape
            result = None
            if _hard_constraints_hold(job, rectangles, placements):
                result = search(index + 1)
            placements.pop(instance.name)
            rectangles.pop(instance.name)
            if result is not None:
                return result
            if exhausted:
                return None
        return None

    solved = search(0)
    if solved is None:
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

    placements, rectangles = solved
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
            stage=PnrStage.PLACEMENT,
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
            ),
        ),
    )
