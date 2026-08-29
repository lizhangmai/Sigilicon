"""Technology-neutral routed transmission-gate construction."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.layout.contacted_mos import place_contacted_mos
from sigilicon.layout.mos import MosDevice
from sigilicon.layout.routing import RoutingCanvas
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.technology import LayoutTechnology


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
    technology: LayoutTechnology,
    well_top: int = 8500,
):
    """Build a contacted transmission gate from a cell-owned recipe."""

    locations = {recipe.n_device: (3, 4), recipe.p_device: (3, 8)}
    placement = place_contacted_mos(
        spec,
        devices,
        locations=locations,
        technology=technology,
    )
    design = placement.design
    gate_half_x, gate_half_y = placement.gate_landing_half_size
    canvas = RoutingCanvas(
        design,
        technology=technology,
        directions=spec.directions,
        wire_half_width=placement.wire_half_width,
    )
    add_wire = canvas.add_wire
    hwire = canvas.hwire
    vwire = canvas.vwire
    add_via = canvas.add_via
    add_pin = canvas.add_pin

    for net, terminal, trunk_x in (
        (recipe.source_net, "S", 1600),
        (recipe.drain_net, "D", 4400),
    ):
        ys: list[int] = []
        for device_name in (recipe.n_device, recipe.p_device):
            diffusion_x, diffusion_y = placement.diffusion_center(
                device_name,
                terminal,
            )
            hwire("routing1", trunk_x, diffusion_x, diffusion_y, net)
            add_via("routing1_routing2", trunk_x, diffusion_y, net)
            ys.append(diffusion_y)
        vwire("routing2", trunk_x, min(ys), max(ys), net)

    for device_name, net, via_x, pin_x0, pin_x1 in (
        (recipe.n_device, recipe.n_control_net, 2200, 1000, 1300),
        (recipe.p_device, recipe.p_control_net, 3800, 4700, 5000),
    ):
        gate_x, gate_y = placement.gate_contact(device_name)
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


__all__ = ["TransmissionGateRecipe", "build_transmission_gate_routed"]
