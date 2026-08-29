from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.layout.mos import (
    MosDevice,
    create_mos_pcell_instance,
    nanometers,
    parse_hierarchical_devices,
    parse_mos_devices,
    validate_static_logic,
)
from sigilicon.layout.technology import LayoutTechnology, MosPcellInterface


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
        """subckt CELL A Y VDD VSS parameters wn=120n
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


def test_hierarchical_parser_resolves_subcircuit_parameter_defaults() -> None:
    spec = _spec(
        """subckt CELL A Y VDD VSS
parameters child_width=240n
X0 (A Y VDD VSS) CHILD w=child_width
ends CELL
"""
    )

    devices = parse_hierarchical_devices(spec)  # type: ignore[arg-type]

    assert len(devices) == 1
    assert devices[0].cell == "CHILD"
    assert devices[0].parameters == {"w": "240n"}


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


def test_mos_pcell_instance_uses_only_the_declared_technology_interface() -> None:
    device = MosDevice(
        "M0",
        ("Y", "A", "VSS", "VSS"),
        "nch",
        {"l": "30n", "w": "120n", "nf": "1", "multi": "1"},
    )
    technology = LayoutTechnology(
        owner="test-owner",
        model_polarities={"nch": "nmos"},
        layers={},
        vias={},
        via_landings={},
        mos_pcell=MosPcellInterface(
            length_parameter="channelLength",
            width_parameter="fingerWidth",
            finger_count_parameter="fingerCount",
            source_terminal="source",
            drain_terminal="drain",
            source_alias_prefix="source_",
            drain_alias_prefix="drain_",
            cdf_callback_parameter="gateContactSide",
            cdf_callback_bypass_parameters=("gateContacts",),
            gate_contact_value="lower",
            gate_contact_enhancement_parameter="enhanceGateContacts",
            gate_contact_enhancement_value="lower",
            gate_contact_parameters=(("polyContacts", "boolean", "True"),),
        ),
    )
    spec = SimpleNamespace(
        pdk=SimpleNamespace(oa=SimpleNamespace(technology_library="test_pdk"))
    )

    instance = create_mos_pcell_instance(
        spec,  # type: ignore[arg-type]
        device,
        technology=technology,
        origin_dbu=(1000, 2000),
    )

    assert tuple(instance.xy) == (1000, 2000)
    assert instance.libname == "test_pdk"
    assert instance.params["pcell_params"][:4] == [
        ["channelLength", "string", "30n"],
        ["fingerWidth", "string", "120n"],
        ["fingerCount", "string", "1"],
        ["gateContactSide", "string", "lower"],
    ]


@pytest.mark.parametrize("value", ["120", "0n", "1.5n"])
def test_nanometer_literal_is_explicit_positive_and_integral(value: str) -> None:
    with pytest.raises(ValueError):
        nanometers(value, "width")
