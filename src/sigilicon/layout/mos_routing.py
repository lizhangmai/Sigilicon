"""Technology-driven static and arbitrary nf=1 MOS routing recipes."""

from __future__ import annotations

from typing import Mapping

from sigilicon.layout.contacted_mos import place_contacted_mos
from sigilicon.layout.mos import MosDevice
from sigilicon.layout.routing import RoutingCanvas
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.technology import LayoutTechnology


def build_static_logic_routed(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
    technology: LayoutTechnology,
    locations: Mapping[str, tuple[int, int]],
    input_devices: Mapping[str, tuple[str, str]],
    output: str,
    output_devices: tuple[str, ...],
    series_connections: tuple[tuple[str, str, str], ...],
    vss_devices: tuple[str, ...],
    vdd_devices: tuple[str, ...],
    tap_x: int,
    output_x: int,
    extend_gate_m1: bool = False,
):
    """Route a static-logic cell from an explicit cell-owned topology map."""

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

    for input_name, (nmos, pmos) in input_devices.items():
        gate_x, n_y = placement.gate_contact(nmos)
        _, p_y = placement.gate_contact(pmos)
        for gate_y in (n_y, p_y):
            add_wire(
                "routing2",
                (
                    (gate_x - gate_half_x, gate_y - gate_half_y),
                    (gate_x + gate_half_x, gate_y + gate_half_y),
                ),
                input_name,
            )
            if extend_gate_m1:
                hwire("routing1", gate_x - 300, gate_x + 300, gate_y, input_name)
        vwire("routing2", gate_x, n_y, p_y, input_name)
        add_pin(
            input_name,
            "routing2",
            ((gate_x - 25, 5800), (gate_x + 25, 6200)),
        )

    output_ys: list[int] = []
    for device_name in output_devices:
        drain_x, drain_y = placement.diffusion_center(device_name, "D")
        hwire("routing1", drain_x, output_x, drain_y, output)
        add_via("routing1_routing2", output_x, drain_y, output)
        output_ys.append(drain_y)
    vwire("routing2", output_x, min(output_ys), max(output_ys), output)
    add_pin(
        output,
        "routing2",
        ((output_x - 25, 5800), (output_x + 25, 6200)),
    )

    for upstream, downstream, net in series_connections:
        upstream_sx, upstream_sy = placement.diffusion_center(upstream, "S")
        downstream_dx, downstream_dy = placement.diffusion_center(downstream, "D")
        hwire("routing1", upstream_sx, downstream_dx, upstream_sy, net)
        if upstream_sy != downstream_dy:
            raise ValueError(f"series devices for {net} must share one diffusion row")

    vss_y = 5000
    vdd_y = 9000
    for net, source_devices, rail_y in (
        ("VSS", vss_devices, vss_y),
        ("VDD", vdd_devices, vdd_y),
    ):
        for device_name in source_devices:
            source_x, source_y = placement.diffusion_center(device_name, "S")
            via_x = source_x - 450
            hwire("routing1", via_x, source_x, source_y, net)
            add_via("routing1_routing2", via_x, source_y, net)
            add_via("routing2_routing3", via_x, source_y, net)
            vwire("routing3", via_x, source_y, rail_y, net)
        hwire("routing3", 2000, tap_x + 800, rail_y, net)

    add_via("substrate_tap", tap_x, 4300, "VSS")
    add_wire("diffusion", ((tap_x - 100, 4200), (tap_x + 100, 4400)), "VSS")
    add_wire("p_implant", ((tap_x - 200, 4150), (tap_x + 200, 4450)), "VSS")
    vwire("routing1", tap_x, 4300, 4650, "VSS")
    add_via("routing1_routing2", tap_x, 4650, "VSS")
    add_via("routing2_routing3", tap_x, 4650, "VSS")
    vwire("routing3", tap_x, 4650, vss_y, "VSS")

    add_wire("n_well", ((2700, 7500), (tap_x + 300, 8600)), "VDD")
    add_via("well_tap", tap_x, 8200, "VDD")
    add_wire("diffusion", ((tap_x - 100, 8100), (tap_x + 100, 8300)), "VDD")
    add_wire("n_implant", ((tap_x - 200, 8050), (tap_x + 200, 8350)), "VDD")
    vwire("routing1", tap_x, 8200, 8550, "VDD")
    add_via("routing1_routing2", tap_x, 8550, "VDD")
    add_via("routing2_routing3", tap_x, 8550, "VDD")
    vwire("routing3", tap_x, 8550, vdd_y, "VDD")

    add_pin("VSS", "routing3", ((tap_x + 400, vss_y - 25), (tap_x + 700, vss_y + 25)))
    add_pin("VDD", "routing3", ((tap_x + 400, vdd_y - 25), (tap_x + 700, vdd_y + 25)))
    return design


def build_nf1_mos_network_routed(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
    technology: LayoutTechnology,
):
    """Route an arbitrary nf=1 MOS leaf through isolated M3 net tracks."""

    if any(device.parameters.get("nf") != "1" for device in devices):
        raise ValueError("nf1 MOS network router supports only nf=1 devices")
    locations: dict[str, tuple[int, int]] = {}
    for index, device in enumerate(devices):
        polarity = technology.polarity(device.model)
        if polarity == "nmos":
            row = 4
        elif polarity == "pmos":
            row = 8
        else:
            raise ValueError(f"unsupported MOS polarity for {device.name}: {device.model}")
        locations[device.name] = (3 + index * 3, row)

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

    ordered_nets = list(spec.ports)
    ordered_nets.extend(
        sorted(
            {
                net
                for device in devices
                for net in device.nodes[:3]
                if net not in set(spec.ports)
            }
        )
    )
    track_y = {net: 12000 + position * 600 for position, net in enumerate(ordered_nets)}
    connections: dict[str, list[int]] = {net: [] for net in ordered_nets}

    for device in devices:
        origin_x, _ = placement.origins[device.name]
        center_y = placement.diffusion_center(device.name, "D")[1]
        for net, terminal_x, access_x in (
            (
                device.nodes[0],
                placement.diffusion_center(device.name, "D")[0],
                origin_x + 550,
            ),
            (
                device.nodes[2],
                placement.diffusion_center(device.name, "S")[0],
                origin_x - 550,
            ),
        ):
            hwire("routing1", terminal_x, access_x, center_y, net)
            add_via("routing1_routing2", access_x, center_y, net)
            vwire("routing2", access_x, center_y, track_y[net], net)
            add_via("routing2_routing3", access_x, track_y[net], net)
            connections[net].append(access_x)

        gate_net = device.nodes[1]
        gate_x, gate_y = placement.gate_contact(device.name)
        hwire("routing1", gate_x - 300, gate_x + 300, gate_y, gate_net)
        add_wire(
            "routing2",
            (
                (gate_x - gate_half_x, gate_y - gate_half_y),
                (gate_x + gate_half_x, gate_y + gate_half_y),
            ),
            gate_net,
        )
        vwire("routing2", gate_x, gate_y, track_y[gate_net], gate_net)
        add_via("routing2_routing3", gate_x, track_y[gate_net], gate_net)
        connections[gate_net].append(gate_x)

    left_x = 1500
    right_x = max(x for values in connections.values() for x in values) + 1500
    add_via("substrate_tap", left_x, 4300, "VSS")
    add_wire("diffusion", ((left_x - 100, 4200), (left_x + 100, 4400)), "VSS")
    add_wire("p_implant", ((left_x - 200, 4150), (left_x + 200, 4450)), "VSS")
    vwire("routing1", left_x, 4300, 4650, "VSS")
    add_via("routing1_routing2", left_x, 4650, "VSS")
    vwire("routing2", left_x, 4650, track_y["VSS"], "VSS")
    add_via("routing2_routing3", left_x, track_y["VSS"], "VSS")
    connections["VSS"].append(left_x)

    add_wire("n_well", ((2400, 7500), (right_x + 300, 8700)), "VDD")
    add_via("well_tap", right_x, 8200, "VDD")
    add_wire("diffusion", ((right_x - 100, 8100), (right_x + 100, 8300)), "VDD")
    add_wire("n_implant", ((right_x - 200, 8050), (right_x + 200, 8350)), "VDD")
    vwire("routing1", right_x, 8200, 8550, "VDD")
    add_via("routing1_routing2", right_x, 8550, "VDD")
    vwire("routing2", right_x, 8550, track_y["VDD"], "VDD")
    add_via("routing2_routing3", right_x, track_y["VDD"], "VDD")
    connections["VDD"].append(right_x)

    for net in ordered_nets:
        xs = connections[net]
        if not xs:
            raise ValueError(f"canonical net {net} has no device connection")
        hwire("routing3", min(xs), right_x + 800, track_y[net], net)
        if net in spec.directions:
            canvas.add_pin(
                net,
                "routing3",
                (
                    (right_x + 400, track_y[net] - 25),
                    (right_x + 700, track_y[net] + 25),
                ),
            )
    return design


__all__ = ["build_nf1_mos_network_routed", "build_static_logic_routed"]
