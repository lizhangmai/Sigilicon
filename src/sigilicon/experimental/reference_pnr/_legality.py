"""Placement legality independent of search and objective strategy."""

from __future__ import annotations

from collections.abc import Mapping

from sigilicon.experimental.reference_pnr._constraints import evaluate_constraint
from sigilicon.experimental.reference_pnr.model import (
    ConstraintMode,
    FenceConstraint,
    PhysicalDesignJob,
    Placement,
    Rect,
)


def rectangles_conflict(candidate: Rect, other: Rect, spacing: int) -> bool:
    return not (
        candidate.x_max + spacing <= other.x_min
        or other.x_max + spacing <= candidate.x_min
        or candidate.y_max + spacing <= other.y_min
        or other.y_max + spacing <= candidate.y_min
    )


def instance_region(job: PhysicalDesignJob, instance_name: str) -> Rect | None:
    region: Rect | None = job.design.die
    for constraint in job.constraints:
        if not isinstance(constraint, FenceConstraint):
            continue
        if constraint.mode is not ConstraintMode.HARD:
            continue
        if instance_name not in constraint.instances:
            continue
        region = region.intersection(constraint.region) if region is not None else None
    return region


def hard_constraints_hold(
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> bool:
    return all(
        evaluate_constraint(constraint, rectangles, placements) is not False
        for constraint in job.constraints
        if constraint.mode is ConstraintMode.HARD
    )
