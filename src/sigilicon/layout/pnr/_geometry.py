"""Orthogonal master geometry transforms in normalized DBU coordinates."""

from __future__ import annotations

from sigilicon.layout.pnr.model import (
    LayerShape,
    Orientation,
    PhysicalMaster,
    Placement,
    Point,
    Rect,
)


def _transform_local_point(
    point: Point,
    master: PhysicalMaster,
    orientation: Orientation,
) -> Point:
    x, y = point.x, point.y
    width, height = master.width_dbu, master.height_dbu
    if orientation is Orientation.R0:
        return Point(x, y)
    if orientation is Orientation.R90:
        return Point(height - y, x)
    if orientation is Orientation.R180:
        return Point(width - x, height - y)
    if orientation is Orientation.R270:
        return Point(y, width - x)
    if orientation is Orientation.MX:
        return Point(x, height - y)
    if orientation is Orientation.MY:
        return Point(width - x, y)
    # Compound orientation names apply their mirror first, then R90.
    if orientation is Orientation.MXR90:
        return Point(y, x)
    if orientation is Orientation.MYR90:
        return Point(height - y, width - x)
    raise ValueError(f"unsupported orientation: {orientation}")


def transform_rect(
    rectangle: Rect,
    master: PhysicalMaster,
    placement: Placement,
) -> Rect:
    corners = tuple(
        _transform_local_point(point, master, placement.orientation)
        for point in (
            Point(rectangle.x_min, rectangle.y_min),
            Point(rectangle.x_min, rectangle.y_max),
            Point(rectangle.x_max, rectangle.y_min),
            Point(rectangle.x_max, rectangle.y_max),
        )
    )
    return Rect(
        placement.origin.x + min(point.x for point in corners),
        placement.origin.y + min(point.y for point in corners),
        placement.origin.x + max(point.x for point in corners),
        placement.origin.y + max(point.y for point in corners),
    )


def transformed_pin_accesses(
    master: PhysicalMaster,
    pin_name: str,
    placement: Placement,
) -> tuple[LayerShape, ...]:
    pin = next(pin for pin in master.pins if pin.name == pin_name)
    return tuple(
        LayerShape(
            layer=access.layer,
            shape=transform_rect(access.shape, master, placement),
        )
        for access in pin.accesses
    )


def transformed_obstructions(
    master: PhysicalMaster,
    placement: Placement,
) -> tuple[LayerShape, ...]:
    return tuple(
        LayerShape(
            layer=obstruction.layer,
            shape=transform_rect(obstruction.shape, master, placement),
        )
        for obstruction in master.obstructions
    )
