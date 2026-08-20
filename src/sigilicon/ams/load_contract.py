"""Backend-neutral semantic contract for AMS output loads."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


_CAPACITANCE = re.compile(
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)"
    r"(?P<suffix>a|f|p|n|u|m|k|meg|g)?\Z",
    re.IGNORECASE,
)
_SCALE = {
    "": Decimal(1),
    "a": Decimal("1e-18"),
    "f": Decimal("1e-15"),
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "meg": Decimal("1e6"),
    "g": Decimal("1e9"),
}
_GROUND_NETS = {"0", "gnd!"}


def canonical_capacitance(value: object) -> str:
    token = str(value).strip().strip('"')
    match = _CAPACITANCE.fullmatch(token)
    if match is None:
        raise RuntimeError(f"invalid OA capacitance value: {value!r}")
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation as exc:
        raise RuntimeError(f"invalid OA capacitance value: {value!r}") from exc
    suffix = (match.group("suffix") or "").lower()
    farads = number * _SCALE[suffix]
    if farads <= 0:
        raise RuntimeError(f"OA capacitance must be positive: {value!r}")
    return str(farads.normalize())


def validate_load_attestation(
    outputs: tuple[str, ...],
    load_cap: str,
    observed: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate exact OA CLOAD instances and return canonical provenance."""

    rows = {str(row.get("instance")): row for row in observed}
    expected_names = {f"CLOAD{index}" for index in range(len(outputs))}
    if set(rows) != expected_names:
        raise RuntimeError(
            "ADE OA load instances do not match the common load contract: "
            f"got {sorted(rows)}, expected {sorted(expected_names)}"
        )
    expected_capacitance = canonical_capacitance(load_cap)
    instances: list[dict[str, str]] = []
    for index, output in enumerate(outputs):
        name = f"CLOAD{index}"
        row = rows[name]
        if row.get("cell") != "cap":
            raise RuntimeError(
                f"ADE OA load {name} uses {row.get('cell')!r}, expected capacitor cell 'cap'"
            )
        nets = tuple(sorted(str(net) for net in row.get("nets", ())))
        if len(nets) != 2 or output not in nets:
            raise RuntimeError(
                f"ADE OA load {name} is connected to {nets}, expected {output} and ground"
            )
        ground = next((net for net in nets if net != output), "")
        if ground not in _GROUND_NETS:
            raise RuntimeError(
                f"ADE OA load {name} ground net is {ground!r}, expected 0 or gnd!"
            )
        actual_capacitance = canonical_capacitance(row.get("value"))
        if actual_capacitance != expected_capacitance:
            raise RuntimeError(
                f"ADE OA load {name} is {actual_capacitance} F, "
                f"expected {expected_capacitance} F"
            )
        instances.append(
            {
                "instance": name,
                "device_library": str(row.get("library") or ""),
                "device_cell": "cap",
                "output": output,
                "ground": ground,
                "capacitance_farads": actual_capacitance,
            }
        )
    return {
        "load_cap": load_cap,
        "load_cap_farads": expected_capacitance,
        "instances": instances,
    }
