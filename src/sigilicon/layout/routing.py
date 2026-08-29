"""Technology-driven drawing canvas for custom layout recipes."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class RoutingStack:
    """Resolve a contiguous technology routing stack without PDK layer names."""

    technology: LayoutTechnology
    landing_overrides: Mapping[
        str, Mapping[str, tuple[int, int]]
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for via_role, layer_overrides in self.landing_overrides.items():
            if via_role not in self.technology.vias:
                raise ValueError(f"unknown routing via role {via_role!r}")
            allowed_layers = self.technology.via_landings[via_role]
            for layer_role, half_size in layer_overrides.items():
                if layer_role not in allowed_layers:
                    raise ValueError(
                        f"{via_role!r} has no landing on {layer_role!r}"
                    )
                if (
                    len(half_size) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value <= 0
                        for value in half_size
                    )
                ):
                    raise ValueError(
                        f"{via_role}.{layer_role} override must be two positive integers"
                    )

    @property
    def layer_roles(self) -> tuple[str, ...]:
        roles: list[str] = []
        index = 1
        while f"routing{index}" in self.technology.layers:
            roles.append(f"routing{index}")
            index += 1
        if len(roles) < 3:
            raise ValueError(
                f"{self.technology.owner} must declare at least three routing layers"
            )
        return tuple(roles)

    @property
    def via_roles(self) -> tuple[str, ...]:
        roles = tuple(
            f"routing{index}_routing{index + 1}"
            for index in range(1, len(self.layer_roles))
        )
        missing = tuple(role for role in roles if role not in self.technology.vias)
        if missing:
            raise ValueError(
                f"{self.technology.owner} routing stack is missing vias {missing}"
            )
        return roles

    @property
    def layers(self) -> tuple[str, ...]:
        return tuple(self.technology.layer(role) for role in self.layer_roles)

    @property
    def vias(self) -> tuple[str, ...]:
        return tuple(self.technology.via(role) for role in self.via_roles)

    def layer(self, role: str) -> str:
        return self.technology.layer(role)

    def via(self, role: str) -> str:
        return self.technology.via(role)

    def landing_shapes(
        self, via_definition: str
    ) -> tuple[tuple[str, int, int], ...]:
        matching_roles = tuple(
            role
            for role, definition in self.technology.vias.items()
            if definition == via_definition
        )
        if len(matching_roles) != 1:
            raise ValueError(f"unknown routing via definition {via_definition!r}")
        via_role = matching_roles[0]
        return tuple(
            (
                self.technology.layer(layer_role),
                *self.landing_overrides.get(via_role, {}).get(
                    layer_role,
                    self.technology.via_landing_half_size(via_role, layer_role),
                ),
            )
            for layer_role in self.technology.via_landings[via_role]
        )

    def vias_between(self, start_layer: str, stop_layer: str) -> tuple[str, ...]:
        layers = self.layers
        try:
            start = layers.index(start_layer)
            stop = layers.index(stop_layer)
        except ValueError as exc:
            raise ValueError(
                f"routing stack does not contain {start_layer!r} or {stop_layer!r}"
            ) from exc
        if start >= stop:
            raise ValueError(f"invalid via stack {start_layer}->{stop_layer}")
        return self.vias[start:stop]


__all__ = ["RoutingCanvas", "RoutingStack"]
