"""Placement legality independent of search and objective strategy."""

from __future__ import annotations

from collections.abc import Mapping

from sigilicon.layout.pnr._constraints import evaluate_constraint
from sigilicon.layout.pnr.model import (
    ConstraintMode,
    FenceConstraint,
    Orientation,
    PhysicalDesignJob,
    PhysicalMaster,
    Placement,
    Rect,
)


def oriented_dimensions(
    master: PhysicalMaster,
    orientation: Orientation,
) -> tuple[int, int]:
    return oriented_size(
        master.width_dbu,
        master.height_dbu,
        orientation,
    )


def oriented_size(
    width_dbu: int,
    height_dbu: int,
    orientation: Orientation,
) -> tuple[int, int]:
    if orientation in {
        Orientation.R90,
        Orientation.R270,
        Orientation.MXR90,
        Orientation.MYR90,
    }:
        return height_dbu, width_dbu
    return width_dbu, height_dbu


def placed_rect(master: PhysicalMaster, placement: Placement) -> Rect:
    return placed_sized_rect(
        master.width_dbu,
        master.height_dbu,
        placement,
    )


def placed_sized_rect(
    width_dbu: int,
    height_dbu: int,
    placement: Placement,
) -> Rect:
    width, height = oriented_size(
        width_dbu,
        height_dbu,
        placement.orientation,
    )
    return Rect(
        x_min=placement.origin.x,
        y_min=placement.origin.y,
        x_max=placement.origin.x + width,
        y_max=placement.origin.y + height,
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
