"""PDK-neutral routed transmission-gate construction."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.mos import MosDevice, build_mos_placement, nanometers
from sigilicon.layout.spec import LayoutSpec


@dataclass(frozen=True)
class TransmissionGateRecipe:
    """Cell-owned topology roles consumed by the common TG router."""

    n_device: str
    p_device: str
    source_net: str
    drain_net: str
    n_control_net: str
    p_control_net: str
    ground_net: str
    power_net: str


def build_transmission_gate_routed(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
    recipe: TransmissionGateRecipe,
    well_top: int = 8500,
):
    """Build a contacted transmission gate from a cell-owned recipe."""

    import laygo2

    technology = spec.layout_pdk.generation.technology
    geometry = spec.layout_pdk.generation.geometry
    routed_pitch = geometry.pair("placement", "routed_pitch")
    wire_half_width = geometry.integer("access", "wire_half_width")
    source_offset_x = geometry.integer("access", "source_offset_x")
    drain_offset_x = geometry.integer("access", "drain_offset_x")
    gate_offset_x, gate_offset_y = geometry.pair("access", "gate_offset")
    gate_half_x, gate_half_y = geometry.pair(
        "access", "gate_landing_half_size"
    )
    locations = {recipe.n_device: (3, 4), recipe.p_device: (3, 8)}
    design = build_mos_placement(
        spec,
        devices,
        locations=locations,
        pitch=routed_pitch,
        gate_contact_parameters=True,
        gate_contact_selection="Bottom",
    )
    rect = laygo2.object.physical.Rect
    pin = laygo2.object.physical.Pin
    via = laygo2.object.physical.Via
    index = 0

    def add_wire(
        layer: str,
        bbox: tuple[tuple[int, int], tuple[int, int]],
        net: str,
    ) -> None:
        nonlocal index
        index += 1
        design.append(
            rect(
                xy=bbox,
                layer=[technology.layer(layer), "drawing"],
                name=f"R{index}_{net}",
                netname=net,
            )
        )

    def hwire(layer: str, x0: int, x1: int, y: int, net: str) -> None:
        add_wire(
            layer,
            (
                (min(x0, x1), y - wire_half_width),
                (max(x0, x1), y + wire_half_width),
            ),
            net,
        )

    def vwire(layer: str, x: int, y0: int, y1: int, net: str) -> None:
        add_wire(
            layer,
            (
                (x - wire_half_width, min(y0, y1)),
                (x + wire_half_width, max(y0, y1)),
            ),
            net,
        )

    def add_via(via_role: str, x: int, y: int, net: str) -> None:
        nonlocal index
        index += 1
        via_interface = technology.via(via_role)
        design.append(
            via(
                xy=[x, y],
                name=f"V{index}_{net}",
                netname=net,
                params={"via_definition": via_interface.definition},
            )
        )
        for layer_role, (half_x, half_y) in via_interface.landing_half_sizes.items():
            add_wire(
                layer_role,
                ((x - half_x, y - half_y), (x + half_x, y + half_y)),
                net,
            )

    def add_pin(
        name: str,
        layer: str,
        bbox: tuple[tuple[int, int], tuple[int, int]],
    ) -> None:
        design.append(
            pin(
                xy=bbox,
                layer=[technology.layer(layer), "pin"],
                name=name,
                netname=name,
                params={"direction": spec.directions[name]},
            )
        )

    widths = {
        device.name: nanometers(device.parameters["w"], f"{device.name}.w")
        for device in devices
    }
    origins = {
        name: (mn[0] * 1000, mn[1] * 1000) for name, mn in locations.items()
    }

    def diffusion_center(name: str, terminal: str) -> tuple[int, int]:
        x, y = origins[name]
        x += source_offset_x if terminal == "S" else drain_offset_x
        y += widths[name] // 2
        return x, y

    def gate_contact(name: str) -> tuple[int, int]:
        x, y = origins[name]
        return x + gate_offset_x, y + gate_offset_y

    # A and B join the NMOS/PMOS diffusion terminals on separate M2 trunks.
    for net, terminal, trunk_x in (
        (recipe.source_net, "S", 1600),
        (recipe.drain_net, "D", 4400),
    ):
        ys: list[int] = []
        for device_name in (recipe.n_device, recipe.p_device):
            diffusion_x, diffusion_y = diffusion_center(device_name, terminal)
            hwire("routing1", trunk_x, diffusion_x, diffusion_y, net)
            add_via("routing1_routing2", trunk_x, diffusion_y, net)
            ys.append(diffusion_y)
        vwire("routing2", trunk_x, min(ys), max(ys), net)

    # PDK lower gate contacts carry EN/ENB; the explicit M2 landing is the
    # same minimum-area construction qualified for the PDK gate contact.
    for device_name, net, via_x, pin_x0, pin_x1 in (
        (recipe.n_device, recipe.n_control_net, 2200, 1000, 1300),
        (recipe.p_device, recipe.p_control_net, 3800, 4700, 5000),
    ):
        gate_x, gate_y = gate_contact(device_name)
        add_wire(
            "routing2",
            (
                (gate_x - gate_half_x, gate_y - gate_half_y),
                (gate_x + gate_half_x, gate_y + gate_half_y),
            ),
            net,
        )
        hwire("routing1", gate_x, via_x, gate_y, net)
        add_via("routing1_routing2", via_x, gate_y, net)
        add_via("routing2_routing3", via_x, gate_y, net)
        if net == recipe.n_control_net:
            hwire("routing3", pin_x0, via_x, gate_y, net)
        else:
            hwire("routing3", via_x, pin_x1, gate_y, net)

    # One explicit substrate tap and one N-well tap bind the body terminals.
    add_via("substrate_tap", 6000, 4300, recipe.ground_net)
    add_wire("diffusion", ((5900, 4200), (6100, 4400)), recipe.ground_net)
    add_wire("p_implant", ((5800, 4150), (6200, 4450)), recipe.ground_net)
    vwire("routing1", 6000, 4300, 4650, recipe.ground_net)
    add_via("routing1_routing2", 6000, 4650, recipe.ground_net)
    hwire("routing2", 5200, 6800, 4800, recipe.ground_net)
    vwire("routing2", 6000, 4650, 4800, recipe.ground_net)

    add_wire("n_well", ((2700, 7500), (6300, well_top)), recipe.power_net)
    add_via("well_tap", 6000, 8200, recipe.power_net)
    add_wire("diffusion", ((5900, 8100), (6100, 8300)), recipe.power_net)
    add_wire("n_implant", ((5800, 8050), (6200, 8350)), recipe.power_net)
    vwire("routing1", 6000, 8200, 8550, recipe.power_net)
    add_via("routing1_routing2", 6000, 8550, recipe.power_net)
    hwire("routing2", 5200, 6800, 8700, recipe.power_net)
    vwire("routing2", 6000, 8550, 8700, recipe.power_net)

    add_pin(recipe.source_net, "routing2", ((1575, 6000), (1625, 6300)))
    add_pin(recipe.drain_net, "routing2", ((4375, 6000), (4425, 6300)))
    add_pin(recipe.n_control_net, "routing3", ((1000, 3860), (1300, 3910)))
    add_pin(recipe.p_control_net, "routing3", ((4700, 7860), (5000, 7910)))
    add_pin(recipe.ground_net, "routing2", ((6400, 4775), (6700, 4825)))
    add_pin(recipe.power_net, "routing2", ((6400, 8675), (6700, 8725)))
    return design
