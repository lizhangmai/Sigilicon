from __future__ import annotations

import pytest

from sigilicon.experimental.reference_pnr import (
    LayerShape,
    Orientation,
    PhysicalMaster,
    Placement,
    Point,
    Rect,
)
from sigilicon.layout.physical_geometry import transform_rect, transformed_obstructions


@pytest.mark.parametrize(
    ("orientation", "expected"),
    (
        (Orientation.R0, Rect(102, 201, 106, 204)),
        (Orientation.R90, Rect(106, 202, 109, 206)),
        (Orientation.R180, Rect(114, 206, 118, 209)),
        (Orientation.R270, Rect(101, 214, 104, 218)),
        (Orientation.MX, Rect(102, 206, 106, 209)),
        (Orientation.MY, Rect(114, 201, 118, 204)),
        (Orientation.MXR90, Rect(101, 202, 104, 206)),
        (Orientation.MYR90, Rect(106, 214, 109, 218)),
    ),
)
def test_master_geometry_uses_all_orthogonal_orientations(
    orientation: Orientation,
    expected: Rect,
) -> None:
    local = Rect(2, 1, 6, 4)
    master = PhysicalMaster(
        "master",
        20,
        10,
        obstructions=(LayerShape("routing", local),),
    )
    placement = Placement(Point(100, 200), orientation)

    assert transform_rect(local, master, placement) == expected
    assert transformed_obstructions(master, placement) == (
        LayerShape("routing", expected),
    )
