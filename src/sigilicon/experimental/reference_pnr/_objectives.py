"""General reference placement objective evaluation."""

from __future__ import annotations

from collections.abc import Mapping

from sigilicon.layout.physical_geometry import transformed_pin_accesses
from sigilicon.layout.physical_design import (
    BoundingBoxAreaObjective,
    BoundingBoxCongestionObjective,
    DensityOverflowObjective,
    EstimatedHpwlObjective,
    PhysicalDesignJob,
    Placement,
    PlacementObjective,
    Rect,
)


def _bins(objective: PlacementObjective) -> tuple[int, int] | None:
    if isinstance(objective, (DensityOverflowObjective, BoundingBoxCongestionObjective)):
        return objective.bins_x, objective.bins_y
    return None


def validate_objective(
    objective: PlacementObjective,
    *,
    job: PhysicalDesignJob,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not objective.name:
        errors.append("placement objective names must be non-empty")
    if objective.weight <= 0:
        errors.append(f"objective {objective.name} weight must be positive")
    bins = _bins(objective)
    if bins is not None:
        bins_x, bins_y = bins
        die = job.design.die
        if bins_x <= 0 or bins_y <= 0:
            errors.append(f"objective {objective.name} bin counts must be positive")
        elif die.width % bins_x != 0 or die.height % bins_y != 0:
            errors.append(
                f"objective {objective.name} bins must divide the die dimensions"
            )
        elif (
            die.width // bins_x < job.technology.manufacturing_grid_dbu
            or die.height // bins_y < job.technology.manufacturing_grid_dbu
        ):
            errors.append(f"objective {objective.name} bins are smaller than the grid")
        elif (
            (die.width // bins_x) % job.technology.manufacturing_grid_dbu != 0
            or (die.height // bins_y) % job.technology.manufacturing_grid_dbu != 0
        ):
            errors.append(f"objective {objective.name} bin edges are off-grid")
    if isinstance(objective, DensityOverflowObjective) and not (
        0 < objective.target_density <= 1
    ):
        errors.append(
            f"objective {objective.name} target density must be in the interval (0, 1]"
        )
    if isinstance(
        objective,
        (EstimatedHpwlObjective, BoundingBoxCongestionObjective),
    ):
        ports = {port.name: port for port in job.design.ports}
        missing = tuple(
            reference.pin
            for net in job.design.nets
            for reference in net.pins
            if reference.instance is None
            and reference.pin in ports
            and not ports[reference.pin].accesses
        )
        if missing:
            errors.append(
                f"objective {objective.name} needs access geometry for ports: "
                + ", ".join(sorted(set(missing)))
            )
    return tuple(errors)


def _bounding_box(rectangles: Mapping[str, Rect]) -> Rect | None:
    if not rectangles:
        return None
    return Rect(
        min(rectangle.x_min for rectangle in rectangles.values()),
        min(rectangle.y_min for rectangle in rectangles.values()),
        max(rectangle.x_max for rectangle in rectangles.values()),
        max(rectangle.y_max for rectangle in rectangles.values()),
    )


def _port_center2(job: PhysicalDesignJob, port_name: str) -> tuple[int, int]:
    port = next(port for port in job.design.ports if port.name == port_name)
    return (
        min(access.shape.x_min for access in port.accesses)
        + max(access.shape.x_max for access in port.accesses),
        min(access.shape.y_min for access in port.accesses)
        + max(access.shape.y_max for access in port.accesses),
    )


def _net_points2(
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> tuple[tuple[tuple[int, int], ...], ...]:
    instances = {instance.name: instance for instance in job.design.instances}
    masters = {master.name: master for master in job.design.masters}
    nets: list[tuple[tuple[int, int], ...]] = []
    for net in job.design.nets:
        points: list[tuple[int, int]] = []
        for reference in net.pins:
            if reference.instance is None:
                points.append(_port_center2(job, reference.pin))
                continue
            rectangle = rectangles[reference.instance]
            instance = instances[reference.instance]
            accesses = transformed_pin_accesses(
                masters[instance.master],
                reference.pin,
                placements[reference.instance],
            )
            if accesses:
                points.append(
                    (
                        min(access.shape.x_min for access in accesses)
                        + max(access.shape.x_max for access in accesses),
                        min(access.shape.y_min for access in accesses)
                        + max(access.shape.y_max for access in accesses),
                    )
                )
            else:
                points.append(
                    (
                        rectangle.x_min + rectangle.x_max,
                        rectangle.y_min + rectangle.y_max,
                    )
                )
        nets.append(tuple(points))
    return tuple(nets)


def _estimated_hpwl(
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> float:
    result = 0.0
    for points in _net_points2(job, rectangles, placements):
        if len(points) < 2:
            continue
        x_values = tuple(point[0] for point in points)
        y_values = tuple(point[1] for point in points)
        result += (
            max(x_values) - min(x_values) + max(y_values) - min(y_values)
        ) / 2
    return result


def _bin_rectangles(die: Rect, bins_x: int, bins_y: int) -> tuple[Rect, ...]:
    width = die.width // bins_x
    height = die.height // bins_y
    return tuple(
        Rect(
            die.x_min + column * width,
            die.y_min + row * height,
            die.x_min + (column + 1) * width,
            die.y_min + (row + 1) * height,
        )
        for row in range(bins_y)
        for column in range(bins_x)
    )


def _intersection_area(first: Rect, second: Rect) -> int:
    width = min(first.x_max, second.x_max) - max(first.x_min, second.x_min)
    height = min(first.y_max, second.y_max) - max(first.y_min, second.y_min)
    return max(width, 0) * max(height, 0)


def _density_overflow(
    objective: DensityOverflowObjective,
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
) -> float:
    overflow = 0.0
    for bin_rectangle in _bin_rectangles(
        job.design.die,
        objective.bins_x,
        objective.bins_y,
    ):
        occupied = sum(
            _intersection_area(bin_rectangle, rectangle)
            for rectangle in rectangles.values()
        )
        density = occupied / bin_rectangle.area
        overflow += max(0.0, density - objective.target_density) ** 2
    return overflow


def _touches_box(
    point_box: tuple[float, float, float, float],
    rectangle: Rect,
) -> bool:
    x_min, y_min, x_max, y_max = point_box
    return not (
        x_max < rectangle.x_min
        or rectangle.x_max < x_min
        or y_max < rectangle.y_min
        or rectangle.y_max < y_min
    )


def _congestion_proxy(
    objective: BoundingBoxCongestionObjective,
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> float:
    bins = _bin_rectangles(job.design.die, objective.bins_x, objective.bins_y)
    demand = [0.0 for _ in bins]
    for points2 in _net_points2(job, rectangles, placements):
        if len(points2) < 2:
            continue
        x_values = tuple(point[0] / 2 for point in points2)
        y_values = tuple(point[1] / 2 for point in points2)
        box = min(x_values), min(y_values), max(x_values), max(y_values)
        hpwl = box[2] - box[0] + box[3] - box[1]
        touched = tuple(
            index
            for index, bin_rectangle in enumerate(bins)
            if _touches_box(box, bin_rectangle)
        )
        if not touched:
            continue
        share = hpwl / len(touched)
        for index in touched:
            demand[index] += share
    return sum(value * value for value in demand)


def objective_value(
    objective: PlacementObjective,
    job: PhysicalDesignJob,
    rectangles: Mapping[str, Rect],
    placements: Mapping[str, Placement],
) -> float:
    if isinstance(objective, BoundingBoxAreaObjective):
        bounding_box = _bounding_box(rectangles)
        return 0.0 if bounding_box is None else float(bounding_box.area)
    if isinstance(objective, EstimatedHpwlObjective):
        return _estimated_hpwl(job, rectangles, placements)
    if isinstance(objective, DensityOverflowObjective):
        return _density_overflow(objective, job, rectangles)
    if isinstance(objective, BoundingBoxCongestionObjective):
        return _congestion_proxy(objective, job, rectangles, placements)
    raise TypeError(f"unknown placement objective: {type(objective).__name__}")


def objective_unit(objective: PlacementObjective) -> str:
    if isinstance(objective, BoundingBoxAreaObjective):
        return "dbu^2"
    if isinstance(objective, EstimatedHpwlObjective):
        return "dbu"
    if isinstance(objective, DensityOverflowObjective):
        return "ratio^2"
    if isinstance(objective, BoundingBoxCongestionObjective):
        return "dbu^2"
    raise TypeError(f"unknown placement objective: {type(objective).__name__}")
