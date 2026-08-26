"""Constraint interpretation shared by placement search and result reporting."""

from __future__ import annotations

from collections.abc import Mapping

from sigilicon.layout.pnr.model import (
    AlignmentAnchor,
    AlignmentConstraint,
    ArrayConstraint,
    Axis,
    ConstraintMode,
    ConstraintOutcome,
    ConstraintStatus,
    FenceConstraint,
    OrderingConstraint,
    Placement,
    PlacementConstraint,
    Rect,
    SeparationAxis,
    SeparationConstraint,
    SymmetryConstraint,
)


def constraint_instances(constraint: PlacementConstraint) -> tuple[str, ...]:
    if isinstance(constraint, (FenceConstraint, AlignmentConstraint, ArrayConstraint)):
        return constraint.instances
    if isinstance(constraint, (OrderingConstraint, SeparationConstraint)):
        return constraint.first, constraint.second
    if isinstance(constraint, SymmetryConstraint):
        return tuple(instance for pair in constraint.pairs for instance in pair)
    raise TypeError(f"unknown placement constraint: {type(constraint).__name__}")


def _on_grid(value: int, grid: int) -> bool:
    return value % grid == 0


def validate_constraint(
    constraint: PlacementConstraint,
    *,
    known_instances: frozenset[str],
    grid: int,
) -> tuple[str, ...]:
    """Validate one normalized constraint without interpreting a project format."""

    errors: list[str] = []
    if not constraint.name:
        errors.append("placement constraint names must be non-empty")
    if not isinstance(constraint.mode, ConstraintMode):
        errors.append(f"constraint {constraint.name} has an invalid mode")
    if constraint.weight <= 0:
        errors.append(f"constraint {constraint.name} weight must be positive")

    instances = constraint_instances(constraint)
    if not instances:
        errors.append(f"constraint {constraint.name} must target an instance")
    unknown = tuple(name for name in instances if name not in known_instances)
    if unknown:
        errors.append(
            f"constraint {constraint.name} targets unknown instances: "
            + ", ".join(unknown)
        )

    if isinstance(constraint, FenceConstraint):
        if len(set(constraint.instances)) != len(constraint.instances):
            errors.append(f"constraint {constraint.name} repeats an instance")
        coordinates = (
            constraint.region.x_min,
            constraint.region.y_min,
            constraint.region.x_max,
            constraint.region.y_max,
        )
        if any(not _on_grid(value, grid) for value in coordinates):
            errors.append(f"constraint {constraint.name} region is off-grid")

    elif isinstance(constraint, AlignmentConstraint):
        if len(constraint.instances) < 2:
            errors.append(
                f"alignment constraint {constraint.name} needs at least two instances"
            )
        if len(set(constraint.instances)) != len(constraint.instances):
            errors.append(f"constraint {constraint.name} repeats an instance")
        if not isinstance(constraint.axis, Axis):
            errors.append(f"constraint {constraint.name} has an invalid axis")
        if not isinstance(constraint.anchor, AlignmentAnchor):
            errors.append(f"constraint {constraint.name} has an invalid anchor")

    elif isinstance(constraint, OrderingConstraint):
        if constraint.first == constraint.second:
            errors.append(f"constraint {constraint.name} must name distinct instances")
        if not isinstance(constraint.axis, Axis):
            errors.append(f"constraint {constraint.name} has an invalid axis")
        if constraint.minimum_gap_dbu < 0 or not _on_grid(
            constraint.minimum_gap_dbu, grid
        ):
            errors.append(
                f"constraint {constraint.name} minimum gap must be non-negative and on-grid"
            )

    elif isinstance(constraint, SymmetryConstraint):
        if not constraint.pairs:
            errors.append(f"symmetry constraint {constraint.name} needs a pair")
        if any(len(pair) != 2 for pair in constraint.pairs):
            errors.append(
                f"symmetry constraint {constraint.name} pairs must contain two instances"
            )
        if any(pair[0] == pair[1] for pair in constraint.pairs if len(pair) == 2):
            errors.append(f"constraint {constraint.name} must pair distinct instances")
        if len(set(instances)) != len(instances):
            errors.append(f"constraint {constraint.name} repeats an instance")
        if not isinstance(constraint.axis, Axis):
            errors.append(f"constraint {constraint.name} has an invalid axis")
        if not _on_grid(constraint.coordinate_dbu, grid):
            errors.append(f"constraint {constraint.name} coordinate is off-grid")

    elif isinstance(constraint, ArrayConstraint):
        if len(constraint.instances) < 2:
            errors.append(f"array constraint {constraint.name} needs at least two instances")
        if len(set(constraint.instances)) != len(constraint.instances):
            errors.append(f"constraint {constraint.name} repeats an instance")
        if constraint.columns <= 0 or constraint.columns > len(constraint.instances):
            errors.append(f"constraint {constraint.name} has an invalid column count")
        if constraint.x_pitch_dbu <= 0 or not _on_grid(
            constraint.x_pitch_dbu, grid
        ):
            errors.append(
                f"constraint {constraint.name} x pitch must be positive and on-grid"
            )
        if constraint.y_pitch_dbu <= 0 or not _on_grid(
            constraint.y_pitch_dbu, grid
        ):
            errors.append(
                f"constraint {constraint.name} y pitch must be positive and on-grid"
            )
        if not isinstance(constraint.require_same_orientation, bool):
            errors.append(
                f"constraint {constraint.name} same-orientation flag must be boolean"
            )

    elif isinstance(constraint, SeparationConstraint):
        if constraint.first == constraint.second:
            errors.append(f"constraint {constraint.name} must name distinct instances")
        if not isinstance(constraint.axis, SeparationAxis):
            errors.append(f"constraint {constraint.name} has an invalid separation axis")
        if constraint.minimum_gap_dbu < 0 or not _on_grid(
            constraint.minimum_gap_dbu, grid
        ):
            errors.append(
                f"constraint {constraint.name} minimum gap must be non-negative and on-grid"
            )

    else:
        errors.append(
            f"constraint {constraint.name} has unknown type {type(constraint).__name__}"
        )
    return tuple(errors)


def _anchor2(rectangle: Rect, axis: Axis, anchor: AlignmentAnchor) -> int:
    low, high = (
        (rectangle.x_min, rectangle.x_max)
        if axis is Axis.X
        else (rectangle.y_min, rectangle.y_max)
    )
    if anchor is AlignmentAnchor.LOW:
        return 2 * low
    if anchor is AlignmentAnchor.HIGH:
        return 2 * high
    return low + high


def _ordered(
    constraint: OrderingConstraint,
    rectangles: Mapping[str, Rect],
) -> bool | None:
    first = rectangles.get(constraint.first)
    second = rectangles.get(constraint.second)
    if first is None or second is None:
        return None
    first_high, second_low = (
        (first.x_max, second.x_min)
        if constraint.axis is Axis.X
        else (first.y_max, second.y_min)
    )
    return first_high + constraint.minimum_gap_dbu <= second_low


def _separated(
    constraint: SeparationConstraint,
    rectangles: Mapping[str, Rect],
) -> bool | None:
    first = rectangles.get(constraint.first)
    second = rectangles.get(constraint.second)
    if first is None or second is None:
        return None
    gap = constraint.minimum_gap_dbu
    separated_x = (
        first.x_max + gap <= second.x_min
        or second.x_max + gap <= first.x_min
    )
    separated_y = (
        first.y_max + gap <= second.y_min
        or second.y_max + gap <= first.y_min
    )
    if constraint.axis is SeparationAxis.X:
        return separated_x
    if constraint.axis is SeparationAxis.Y:
        return separated_y
    return separated_x or separated_y


def _symmetric_pair(first: Rect, second: Rect, constraint: SymmetryConstraint) -> bool:
    coordinate2 = 2 * constraint.coordinate_dbu
    if constraint.axis is Axis.X:
        return (
            first.x_min + second.x_max == coordinate2
            and first.x_max + second.x_min == coordinate2
            and first.y_min == second.y_min
            and first.y_max == second.y_max
        )
    return (
        first.y_min + second.y_max == coordinate2
        and first.y_max + second.y_min == coordinate2
        and first.x_min == second.x_min
        and first.x_max == second.x_max
    )


def evaluate_constraint(
    constraint: PlacementConstraint,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> bool | None:
    """Return true, false, or unknown for a complete or partial placement."""

    if isinstance(constraint, FenceConstraint):
        present = tuple(
            rectangles[name] for name in constraint.instances if name in rectangles
        )
        if any(not constraint.region.contains(rectangle) for rectangle in present):
            return False
        return True if len(present) == len(constraint.instances) else None

    if isinstance(constraint, AlignmentConstraint):
        present = tuple(
            _anchor2(rectangles[name], constraint.axis, constraint.anchor)
            for name in constraint.instances
            if name in rectangles
        )
        if len(set(present)) > 1:
            return False
        return True if len(present) == len(constraint.instances) else None

    if isinstance(constraint, OrderingConstraint):
        return _ordered(constraint, rectangles)

    if isinstance(constraint, SeparationConstraint):
        return _separated(constraint, rectangles)

    if isinstance(constraint, SymmetryConstraint):
        complete = True
        for first_name, second_name in constraint.pairs:
            first = rectangles.get(first_name)
            second = rectangles.get(second_name)
            if first is None or second is None:
                complete = False
                continue
            if not _symmetric_pair(first, second, constraint):
                return False
        return True if complete else None

    if isinstance(constraint, ArrayConstraint):
        reference_name = constraint.instances[0]
        reference = rectangles.get(reference_name)
        reference_placement = placements.get(reference_name)
        if reference is None or reference_placement is None:
            return None
        complete = True
        for index, instance_name in enumerate(constraint.instances):
            rectangle = rectangles.get(instance_name)
            placement = placements.get(instance_name)
            if rectangle is None or placement is None:
                complete = False
                continue
            row, column = divmod(index, constraint.columns)
            if (
                rectangle.x_min
                != reference.x_min + column * constraint.x_pitch_dbu
                or rectangle.y_min
                != reference.y_min + row * constraint.y_pitch_dbu
            ):
                return False
            if (
                constraint.require_same_orientation
                and placement.orientation is not reference_placement.orientation
            ):
                return False
        return True if complete else None

    raise TypeError(f"unknown placement constraint: {type(constraint).__name__}")


def constraint_outcomes(
    constraints: tuple[PlacementConstraint, ...],
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> tuple[ConstraintOutcome, ...]:
    outcomes: list[ConstraintOutcome] = []
    for constraint in constraints:
        evaluated = evaluate_constraint(constraint, rectangles, placements)
        if evaluated is True:
            status = ConstraintStatus.SATISFIED
            message = "constraint is satisfied"
        elif evaluated is False:
            status = ConstraintStatus.VIOLATED
            message = "constraint is violated"
        else:
            status = ConstraintStatus.NOT_EVALUATED
            message = "constraint is not fully evaluable"
        outcomes.append(
            ConstraintOutcome(
                constraint=constraint.name,
                status=status,
                message=message,
            )
        )
    return tuple(outcomes)
