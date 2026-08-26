"""Deterministic reference placement implementation."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.pnr.model import (
    ConstraintOutcome,
    ConstraintStatus,
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
    Rect,
    ResultStatus,
    StageReport,
    PnrStage,
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


def _instance_region(
    job: PhysicalDesignJob,
    instance_name: str,
) -> Rect | None:
    region: Rect | None = job.design.die
    for constraint in job.constraints:
        if instance_name not in constraint.instances:
            continue
        region = region.intersection(constraint.region) if region is not None else None
    return region


def _candidate_origins(
    region: Rect,
    occupied: tuple[Rect, ...],
    *,
    spacing: int,
    grid: int,
) -> tuple[Point, ...]:
    x_values = {_snap_up(region.x_min, grid)}
    y_values = {_snap_up(region.y_min, grid)}
    for shape in occupied:
        x_values.add(_snap_up(shape.x_max + spacing, grid))
        y_values.add(_snap_up(shape.y_max + spacing, grid))
    return tuple(
        Point(x=x, y=y)
        for y in sorted(y_values)
        for x in sorted(x_values)
        if x < region.x_max and y < region.y_max
    )


def _constraint_outcomes(
    constraints: tuple[FenceConstraint, ...],
    rectangles: dict[str, Rect],
) -> tuple[ConstraintOutcome, ...]:
    outcomes: list[ConstraintOutcome] = []
    for constraint in constraints:
        missing = tuple(name for name in constraint.instances if name not in rectangles)
        outside = tuple(
            name
            for name in constraint.instances
            if name in rectangles and not constraint.region.contains(rectangles[name])
        )
        if missing or outside:
            entities = ", ".join((*missing, *outside))
            outcomes.append(
                ConstraintOutcome(
                    constraint=constraint.name,
                    status=ConstraintStatus.VIOLATED,
                    message=f"instances do not satisfy fence: {entities}",
                )
            )
        else:
            outcomes.append(
                ConstraintOutcome(
                    constraint=constraint.name,
                    status=ConstraintStatus.SATISFIED,
                    message="all constrained instances are inside the fence",
                )
            )
    return tuple(outcomes)


def _failure(
    *,
    code: str,
    message: str,
    entities: tuple[str, ...],
    placements: dict[str, Placement],
    rectangles: dict[str, Rect],
    constraints: tuple[FenceConstraint, ...],
) -> PlacementSolveResult:
    ordered = tuple(
        InstancePlacement(instance=name, placement=placements[name])
        for name in sorted(placements)
    )
    return PlacementSolveResult(
        status=ResultStatus.FAILED,
        placements=ordered,
        constraint_outcomes=_constraint_outcomes(constraints, rectangles),
        report=StageReport(
            stage=PnrStage.PLACEMENT,
            status=ResultStatus.FAILED,
            diagnostics=(Diagnostic(code=code, message=message, entities=entities),),
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
                constraints=job.constraints,
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
                constraints=job.constraints,
            )
        placements[instance.name] = placement
        rectangles[instance.name] = shape

    movable = tuple(
        sorted(
            (
                instance
                for instance in job.design.instances
                if instance.fixed_placement is None
            ),
            key=lambda instance: (
                -(masters[instance.master].width_dbu * masters[instance.master].height_dbu),
                instance.name,
            ),
        )
    )
    for instance in movable:
        master = masters[instance.master]
        region = _instance_region(job, instance.name)
        if region is None:
            return _failure(
                code="empty_instance_region",
                message=f"instance {instance.name} has an empty legal region",
                entities=(instance.name,),
                placements=placements,
                rectangles=rectangles,
                constraints=job.constraints,
            )
        chosen: tuple[Placement, Rect] | None = None
        origins = _candidate_origins(
            region,
            tuple(rectangles.values()),
            spacing=spacing,
            grid=job.technology.manufacturing_grid_dbu,
        )
        for orientation in master.allowed_orientations:
            for origin in origins:
                placement = Placement(origin=origin, orientation=orientation)
                shape = _placed_rect(master, placement)
                if not region.contains(shape):
                    continue
                if any(
                    _conflicts(shape, other, spacing)
                    for other in rectangles.values()
                ):
                    continue
                chosen = placement, shape
                break
            if chosen is not None:
                break
        if chosen is None:
            return _failure(
                code="placement_infeasible",
                message=f"reference placer found no legal location for {instance.name}",
                entities=(instance.name,),
                placements=placements,
                rectangles=rectangles,
                constraints=job.constraints,
            )
        placements[instance.name], rectangles[instance.name] = chosen

    occupied_area = sum(shape.area for shape in rectangles.values())
    result_placements = tuple(
        InstancePlacement(instance=name, placement=placements[name])
        for name in sorted(placements)
    )
    return PlacementSolveResult(
        status=ResultStatus.SUCCEEDED,
        placements=result_placements,
        constraint_outcomes=_constraint_outcomes(job.constraints, rectangles),
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
            ),
        ),
    )
