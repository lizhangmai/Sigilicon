"""Canonical physical-geometry value transforms in normalized DBU coordinates."""

from __future__ import annotations

from sigilicon.layout.physical_design import (
    Orientation,
    Placement,
    Point,
    Rect,
)


def oriented_size(
    width_dbu: int,
    height_dbu: int,
    orientation: Orientation,
) -> tuple[int, int]:
    """Return occurrence dimensions after applying one physical orientation."""

    if orientation in {
        Orientation.R90,
        Orientation.R270,
        Orientation.MXR90,
        Orientation.MYR90,
    }:
        return height_dbu, width_dbu
    return width_dbu, height_dbu


def placed_sized_rect(
    width_dbu: int,
    height_dbu: int,
    placement: Placement,
) -> Rect:
    """Return the die-space box of an explicitly sized placed occurrence."""

    width, height = oriented_size(width_dbu, height_dbu, placement.orientation)
    return Rect(
        x_min=placement.origin.x,
        y_min=placement.origin.y,
        x_max=placement.origin.x + width,
        y_max=placement.origin.y + height,
    )


def _transform_local_point(
    point: Point,
    width: int,
    height: int,
    orientation: Orientation,
) -> Point:
    x, y = point.x, point.y
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


def transform_sized_rect(
    rectangle: Rect,
    width_dbu: int,
    height_dbu: int,
    placement: Placement,
) -> Rect:
    """Transform local geometry owned by an explicitly sized occurrence."""

    corners = tuple(
        _transform_local_point(
            point,
            width_dbu,
            height_dbu,
            placement.orientation,
        )
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
