"""Canonical physical-geometry value transforms in normalized DBU coordinates."""

from __future__ import annotations

from sigilicon.layout.pnr.model import (
    LayerShape,
    Orientation,
    PhysicalMaster,
    Placement,
    Point,
    Rect,
    RouteSegment,
    RoutingBlockage,
    ViaDefinition,
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


def transform_rect(
    rectangle: Rect,
    master: PhysicalMaster,
    placement: Placement,
) -> Rect:
    return transform_sized_rect(
        rectangle,
        master.width_dbu,
        master.height_dbu,
        placement,
    )


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


def transformed_routing_blockage_shapes(
    blockage: RoutingBlockage,
    placement: Placement,
) -> tuple[LayerShape, ...]:
    return tuple(
        LayerShape(
            layer=item.layer,
            shape=transform_sized_rect(
                item.shape,
                blockage.width_dbu,
                blockage.height_dbu,
                placement,
            ),
        )
        for item in blockage.shapes
    )


def translated_rect(rectangle: Rect, origin: Point) -> Rect:
    """Translate a local rectangle to an occurrence origin."""

    return Rect(
        rectangle.x_min + origin.x,
        rectangle.y_min + origin.y,
        rectangle.x_max + origin.x,
        rectangle.y_max + origin.y,
    )


def route_segment_shape(segment: RouteSegment) -> Rect:
    """Return the exact conductor rectangle represented by a Route Segment."""

    margin = segment.width_dbu // 2
    if segment.start.y == segment.end.y:
        return Rect(
            min(segment.start.x, segment.end.x),
            segment.start.y - margin,
            max(segment.start.x, segment.end.x),
            segment.start.y + margin,
        )
    return Rect(
        segment.start.x - margin,
        min(segment.start.y, segment.end.y),
        segment.start.x + margin,
        max(segment.start.y, segment.end.y),
    )


def via_occurrence_shapes(
    via: ViaDefinition,
    origin: Point,
) -> tuple[tuple[str, Rect], ...]:
    """Expand one Route Via occurrence into all of its exact layer shapes."""

    return tuple(
        (layer, translated_rect(shape, origin))
        for layer, shapes in (
            (via.lower_layer, via.lower_shapes),
            (via.cut_layer, via.cut_shapes),
            (via.upper_layer, via.upper_shapes),
        )
        for shape in shapes
    )
