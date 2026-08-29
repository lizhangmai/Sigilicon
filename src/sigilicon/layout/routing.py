"""Technology-driven drawing canvas for custom layout recipes."""

from __future__ import annotations

from typing import Any, Mapping

from sigilicon.layout.technology import LayoutTechnology


class RoutingCanvas:
    """Append named wires, vias, and pins under one technology policy."""

    def __init__(
        self,
        design: Any,
        *,
        technology: LayoutTechnology,
        directions: Mapping[str, str],
        wire_half_width: int,
    ) -> None:
        import laygo2

        self.design = design
        self.technology = technology
        self.directions = directions
        self.wire_half_width = wire_half_width
        self._rect = laygo2.object.physical.Rect
        self._pin = laygo2.object.physical.Pin
        self._via = laygo2.object.physical.Via
        self._index = 0

    def add_wire(
        self,
        layer: str,
        bbox: tuple[tuple[int, int], tuple[int, int]],
        net: str,
    ) -> None:
        self._index += 1
        self.design.append(
            self._rect(
                xy=bbox,
                layer=[self.technology.layer(layer), "drawing"],
                name=f"R{self._index}_{net}",
                netname=net,
            )
        )

    def hwire(self, layer: str, x0: int, x1: int, y: int, net: str) -> None:
        self.add_wire(
            layer,
            (
                (min(x0, x1), y - self.wire_half_width),
                (max(x0, x1), y + self.wire_half_width),
            ),
            net,
        )

    def vwire(self, layer: str, x: int, y0: int, y1: int, net: str) -> None:
        self.add_wire(
            layer,
            (
                (x - self.wire_half_width, min(y0, y1)),
                (x + self.wire_half_width, max(y0, y1)),
            ),
            net,
        )

    def add_via(self, via_role: str, x: int, y: int, net: str) -> None:
        self._index += 1
        via_definition = self.technology.via(via_role)
        self.design.append(
            self._via(
                xy=[x, y],
                name=f"V{self._index}_{net}",
                netname=net,
                params={"via_definition": via_definition},
            )
        )
        for layer_role in self.technology.via_landings[via_role]:
            half_x, half_y = self.technology.via_landing_half_size(
                via_role,
                layer_role,
            )
            self.add_wire(
                layer_role,
                ((x - half_x, y - half_y), (x + half_x, y + half_y)),
                net,
            )

    def add_pin(
        self,
        name: str,
        layer: str,
        bbox: tuple[tuple[int, int], tuple[int, int]],
    ) -> None:
        self.design.append(
            self._pin(
                xy=bbox,
                layer=[self.technology.layer(layer), "pin"],
                name=name,
                netname=name,
                params={"direction": self.directions[name]},
            )
        )


__all__ = ["RoutingCanvas"]
