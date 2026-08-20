"""PDK-neutral MOS parsing, placement, and Laygo2 routing algorithms."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from sigilicon.domain.netlist import (
    extract_subckt_body,
    iter_spectre_logical_lines,
    lower_subckt_default_parameters,
)
from sigilicon.layout.spec import LayoutSpec


_MOS = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s+"
    r"\((?P<nodes>[^)]+)\)\s+"
    r"(?P<model>[A-Za-z_][A-Za-z0-9_$]*)\s*(?P<params>.*)$"
)
_ASSIGNMENT = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)=(?P<value>[^\s]+)\Z")
_NANOMETERS = re.compile(r"(?P<value>\d+(?:\.\d+)?)n\Z", re.IGNORECASE)
_HIERARCHICAL_INSTANCE = re.compile(
    r"^(?P<name>X[A-Za-z0-9_$]+)\s+\((?P<nodes>[^)]+)\)\s+"
    r"(?P<cell>[A-Za-z_][A-Za-z0-9_$]*)(?:\s+(?P<params>.*))?$"
)


@dataclass(frozen=True)
class MosDevice:
    name: str
    nodes: tuple[str, str, str, str]
    model: str
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class HierarchicalDevice:
    name: str
    nodes: tuple[str, ...]
    cell: str
    parameters: Mapping[str, str]



def parse_mos_devices(
    spec: LayoutSpec,
    *,
    allowed_hierarchical_instances: tuple[str, ...] = (),
) -> tuple[MosDevice, ...]:
    lowered = lower_subckt_default_parameters(spec.source_snapshot, spec.cell)
    body = extract_subckt_body(lowered, spec.cell)
    devices: list[MosDevice] = []
    seen_hierarchical: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.partition("//")[0].strip()
        if not line:
            continue
        hierarchical = _HIERARCHICAL_INSTANCE.fullmatch(line)
        if hierarchical is not None:
            name = hierarchical.group("name")
            if name not in allowed_hierarchical_instances:
                raise ValueError(
                    f"unexpected hierarchical layout instance: {name}"
                )
            seen_hierarchical.append(name)
            continue
        match = _MOS.fullmatch(line)
        if match is None:
            raise ValueError(f"unsupported canonical layout device statement: {line}")
        nodes = tuple(match.group("nodes").split())
        if len(nodes) != 4:
            raise ValueError(f"MOS {match.group('name')} must have D G S B terminals")
        parameters: dict[str, str] = {}
        for token in match.group("params").split():
            assignment = _ASSIGNMENT.fullmatch(token)
            if assignment is None:
                raise ValueError(f"unsupported MOS parameter token: {token}")
            parameters[assignment.group("name")] = assignment.group("value")
        devices.append(
            MosDevice(
                name=match.group("name"),
                nodes=nodes,  # type: ignore[arg-type]
                model=match.group("model"),
                parameters=parameters,
            )
        )
    if len(seen_hierarchical) != len(set(seen_hierarchical)):
        raise ValueError("canonical layout contains duplicate hierarchical instances")
    if set(seen_hierarchical) != set(allowed_hierarchical_instances):
        raise ValueError(
            "canonical layout hierarchical instances differ from the allowed set"
        )
    return tuple(devices)



def validate_static_logic(
    devices: tuple[MosDevice, ...],
    contract: Mapping[str, tuple[tuple[str, str, str, str], str]],
    *,
    label: str,
) -> None:
    by_name = {device.name: device for device in devices}
    if len(by_name) != len(devices):
        raise ValueError(f"canonical {label} contains duplicate device names")
    if set(by_name) != set(contract):
        raise ValueError(
            f"{label} generator requires exactly " + ", ".join(contract)
        )
    for name, (nodes, model) in contract.items():
        device = by_name[name]
        if device.nodes != nodes or device.model != model:
            raise ValueError(
                f"canonical topology mismatch for {name}: "
                f"got {device.nodes}/{device.model}, expected {nodes}/{model}"
            )
        required = {"l", "w", "nf", "multi"}
        if set(device.parameters) != required:
            raise ValueError(f"{name} parameters must be exactly {sorted(required)}")
        if device.parameters["nf"] != "1" or device.parameters["multi"] != "1":
            raise ValueError(f"{name} routed generator supports only nf=1 and multi=1")


def parse_hierarchical_devices(spec: LayoutSpec) -> tuple[HierarchicalDevice, ...]:
    body = extract_subckt_body(spec.source_snapshot, spec.cell)
    logical_lines = tuple(iter_spectre_logical_lines(body))
    defaults: dict[str, str] = {}
    for line in logical_lines:
        if not line.lower().startswith("parameters "):
            continue
        for token in line.split()[1:]:
            assignment = _ASSIGNMENT.fullmatch(token)
            if assignment is None:
                raise ValueError(f"unsupported subckt parameter token: {token}")
            defaults[assignment.group("name")] = assignment.group("value")
    devices: list[HierarchicalDevice] = []
    for line in logical_lines:
        match = _HIERARCHICAL_INSTANCE.fullmatch(line)
        if match is None:
            continue
        parameters: dict[str, str] = {}
        for token in (match.group("params") or "").split():
            assignment = _ASSIGNMENT.fullmatch(token)
            if assignment is None:
                raise ValueError(
                    f"unsupported hierarchical parameter token for "
                    f"{match.group('name')}: {token}"
                )
            raw_value = assignment.group("value")
            parameters[assignment.group("name")] = defaults.get(raw_value, raw_value)
        devices.append(
            HierarchicalDevice(
                name=match.group("name"),
                nodes=tuple(match.group("nodes").split()),
                cell=match.group("cell"),
                parameters=parameters,
            )
        )
    return tuple(devices)


def build_mos_placement(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
    locations: Mapping[str, tuple[int, int]],
    pitch: tuple[int, int] | None = None,
    route_poly_direction: str | None = None,
    gate_contact_parameters: bool = False,
    gate_contact_selection: str = "Both",
):
    import laygo2

    generation = spec.layout_pdk.generation
    technology = generation.technology
    geometry = generation.geometry
    if pitch is None:
        pitch = geometry.pair("placement", "default_pitch")
    template_bbox = geometry.pair("placement", "template_bbox")
    design = laygo2.object.database.Design(name=spec.cell, libname=spec.library)
    gx = laygo2.object.grid.OneDimGrid(
        name="mos_x", scope=[0, pitch[0]], elements=[0]
    )
    gy = laygo2.object.grid.OneDimGrid(
        name="mos_y", scope=[0, pitch[1]], elements=[0]
    )
    grid = laygo2.object.grid.PlacementGrid(name="mos_place", vgrid=gx, hgrid=gy)
    templates = {
        model: laygo2.object.template.NativeInstanceTemplate(
            libname=spec.pdk.technology_library,
            cellname=model,
            bbox=[[0, 0], list(template_bbox)],
            pins={},
        )
        for model in {device.model for device in devices}
    }
    for device in devices:
        pcell = technology.mos_pcell
        finger_parameter = spec.layout_pdk.pcell_policy.finger_count_parameter
        if finger_parameter is None:
            raise ValueError("layout PCell policy must declare a finger parameter")
        pcell_params = [
            [pcell.length_parameter, "string", device.parameters["l"]],
            [pcell.width_parameter, "string", device.parameters["w"]],
            [finger_parameter, "string", device.parameters["nf"]],
        ]
        if gate_contact_parameters:
            pcell_params.extend(
                [parameter.name, parameter.value_type, parameter.value]
                for parameter in pcell.gate_contact_parameters
            )
            pcell_params.append(
                [
                    pcell.gate_contact_selection_parameter,
                    "string",
                    gate_contact_selection,
                ]
            )
            pcell_params.extend(
                [parameter.name, parameter.value_type, parameter.value]
                for parameter in pcell.polarity_parameters.get(
                    technology.polarity(device.model), ()
                )
            )
        elif route_poly_direction is not None:
            route_parameter = spec.layout_pdk.pcell_policy.cdf_callback_parameter
            if route_parameter is None:
                raise ValueError("layout PCell policy must declare a route callback")
            pcell_params.append(
                [route_parameter, "string", route_poly_direction]
            )
        instance = templates[device.model].generate(
            name=device.name,
            params={"pcell_params": pcell_params},
        )
        design.place(instance, grid=grid, mn=locations[device.name])
    return design


def nanometers(value: str, field: str) -> int:
    match = _NANOMETERS.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must use an explicit nanometer literal")
    result = float(match.group("value"))
    if not result.is_integer() or result <= 0:
        raise ValueError(f"{field} must be a positive integral number of nanometers")
    return int(result)


def build_static_logic_routed(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
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

    # Each input is a vertical M2 strap between the matching N/P gate
    # contacts.  The contacts themselves remain PDK-generated.
    for input_name, (nmos, pmos) in input_devices.items():
        gate_x, n_y = gate_contact(nmos)
        _, p_y = gate_contact(pmos)
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

    # Output drains join on M1 and rise on one M2 trunk.  Supply routing uses
    # M3 so the vertical input/output straps can cross it without shorts.
    output_ys: list[int] = []
    for device_name in output_devices:
        drain_x, drain_y = diffusion_center(device_name, "D")
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
        upstream_sx, upstream_sy = diffusion_center(upstream, "S")
        downstream_dx, downstream_dy = diffusion_center(downstream, "D")
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
            source_x, source_y = diffusion_center(device_name, "S")
            via_x = source_x - 450
            hwire("routing1", via_x, source_x, source_y, net)
            add_via("routing1_routing2", via_x, source_y, net)
            add_via("routing2_routing3", via_x, source_y, net)
            vwire("routing3", via_x, source_y, rail_y, net)
        hwire("routing3", 2000, tap_x + 800, rail_y, net)

    # Explicit taps bind both body terminals and provide macro-level wells.
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
):
    """Route an arbitrary nf=1 MOS leaf through isolated M3 net tracks.

    Every gate and diffusion access uses its own M2 branch.  Branches meet only
    on the canonical net's horizontal M3 track, which makes the construction
    suitable for irregular controller gates without relying on diffusion
    sharing or schematic-specific terminal reordering.
    """

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

    widths = {
        device.name: nanometers(device.parameters["w"], f"{device.name}.w")
        for device in devices
    }
    origins = {
        name: (mn[0] * 1000, mn[1] * 1000) for name, mn in locations.items()
    }
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
        origin_x, origin_y = origins[device.name]
        center_y = origin_y + widths[device.name] // 2
        for terminal, net, terminal_x, access_x in (
            (
                "D",
                device.nodes[0],
                origin_x + drain_offset_x,
                origin_x + 550,
            ),
            (
                "S",
                device.nodes[2],
                origin_x + source_offset_x,
                origin_x - 550,
            ),
        ):
            del terminal
            hwire("routing1", terminal_x, access_x, center_y, net)
            add_via("routing1_routing2", access_x, center_y, net)
            vwire("routing2", access_x, center_y, track_y[net], net)
            add_via("routing2_routing3", access_x, track_y[net], net)
            connections[net].append(access_x)

        gate_net = device.nodes[1]
        gate_x = origin_x + gate_offset_x
        gate_y = origin_y + gate_offset_y
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
    # Substrate and N-well taps bind every canonical body terminal.
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
            design.append(
                pin(
                    xy=((right_x + 400, track_y[net] - 25),
                        (right_x + 700, track_y[net] + 25)),
                    layer=[technology.layer("routing3"), "pin"],
                    name=net,
                    netname=net,
                    params={"direction": spec.directions[net]},
                )
            )
    return design
