"""PDK-neutral MOS and hierarchical-netlist layout contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from sigilicon.domain.netlist import (
    extract_subckt_body,
    iter_spectre_logical_lines,
    lower_subckt_default_parameters,
)
from sigilicon.layout.generator import LayoutGeneratorInput
from sigilicon.layout.technology import LayoutTechnology


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
    spec: LayoutGeneratorInput,
    *,
    allowed_hierarchical_instances: tuple[str, ...] = (),
) -> tuple[MosDevice, ...]:
    """Parse the MOS leaf devices owned by one canonical layout netlist."""

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
                raise ValueError(f"unexpected hierarchical layout instance: {name}")
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


def parse_hierarchical_devices(
    spec: LayoutGeneratorInput,
) -> tuple[HierarchicalDevice, ...]:
    """Parse hierarchical instances while resolving subcircuit defaults."""

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


def nanometers(value: str, field: str) -> int:
    """Decode one explicit, positive, integral nanometer literal."""

    match = _NANOMETERS.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must use an explicit nanometer literal")
    result = float(match.group("value"))
    if not result.is_integer() or result <= 0:
        raise ValueError(f"{field} must be a positive integral number of nanometers")
    return int(result)


def validate_static_logic(
    devices: tuple[MosDevice, ...],
    contract: Mapping[str, tuple[tuple[str, str, str, str], str]],
    *,
    label: str,
) -> None:
    """Validate an exact static-CMOS topology and supported sizing envelope."""

    by_name = {device.name: device for device in devices}
    if len(by_name) != len(devices):
        raise ValueError(f"canonical {label} contains duplicate device names")
    if set(by_name) != set(contract):
        raise ValueError(f"{label} generator requires exactly " + ", ".join(contract))
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


def create_mos_pcell_instance(
    spec: LayoutGeneratorInput,
    device: MosDevice,
    *,
    technology: LayoutTechnology,
    origin_dbu: tuple[int, int],
):
    """Create one MOS PCell instance from a technology-declared interface."""

    import laygo2

    interface = technology.mos_pcell
    parameters = [
        [interface.length_parameter, "string", device.parameters["l"]],
        [interface.width_parameter, "string", device.parameters["w"]],
        [interface.finger_count_parameter, "string", device.parameters["nf"]],
        [
            interface.cdf_callback_parameter,
            "string",
            interface.gate_contact_value,
        ],
        *[list(parameter) for parameter in interface.gate_contact_parameters],
        [
            interface.gate_contact_enhancement_parameter,
            "string",
            interface.gate_contact_enhancement_value,
        ],
    ]
    if technology.polarity(device.model) == "pmos":
        parameters.extend(
            list(parameter) for parameter in interface.pmos_contact_parameters
        )
    return laygo2.object.physical.Instance(
        xy=list(origin_dbu),
        libname=spec.technology_library,
        cellname=device.model,
        name=device.name,
        params={"pcell_params": parameters},
    )


__all__ = [
    "HierarchicalDevice",
    "MosDevice",
    "create_mos_pcell_instance",
    "nanometers",
    "parse_hierarchical_devices",
    "parse_mos_devices",
    "validate_static_logic",
]
