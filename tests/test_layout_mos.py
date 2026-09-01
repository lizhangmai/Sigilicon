from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.layout.mos import (
    MosDevice,
    parse_mos_devices,
    validate_static_logic,
)


def _spec(text: str, cell: str = "CELL") -> SimpleNamespace:
    return SimpleNamespace(
        cell=cell,
        source_snapshot=NetlistSnapshot(
            Path("source.scs"),
            text,
            {cell: ("A", "Y", "VDD", "VSS")},
        ),
    )


def test_mos_parser_and_static_logic_contract_are_pdk_neutral() -> None:
    spec = _spec(
        """subckt CELL A Y VDD VSS
parameters wn=120n
M0 (Y A VSS VSS) nch l=30n w=wn nf=1 multi=1
M1 (Y A VDD VDD) pch l=30n w=240n nf=1 multi=1
ends CELL
"""
    )

    devices = parse_mos_devices(spec)  # type: ignore[arg-type]

    assert tuple(device.name for device in devices) == ("M0", "M1")
    assert devices[0].parameters["w"] == "120n"
    validate_static_logic(
        devices,
        {
            "M0": (("Y", "A", "VSS", "VSS"), "nch"),
            "M1": (("Y", "A", "VDD", "VDD"), "pch"),
        },
        label="inverter",
    )


def test_mos_contract_rejects_unsupported_finger_count() -> None:
    device = MosDevice(
        "M0",
        ("Y", "A", "VSS", "VSS"),
        "nch",
        {"l": "30n", "w": "120n", "nf": "2", "multi": "1"},
    )

    with pytest.raises(ValueError, match="supports only nf=1"):
        validate_static_logic(
            (device,),
            {"M0": (("Y", "A", "VSS", "VSS"), "nch")},
            label="logic",
        )
