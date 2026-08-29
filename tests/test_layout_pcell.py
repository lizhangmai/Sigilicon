from sigilicon.layout.ir import LayoutInstance, LayoutPlan
from sigilicon.layout.pcell import apply_pcell_semantics
from sigilicon.layout.technology import (
    LayoutTechnology,
    MosPcellInterface,
)


def test_layout_technology_requires_an_explicit_pcell_interface() -> None:
    mos = MosPcellInterface(
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
    )
    technology = LayoutTechnology(
        "test-owner",
        {"nch": "nmos"},
        {},
        {},
        {},
        mos,
    )

    assert technology.mos_pcell.length_parameter == "channelLength"


def _technology() -> LayoutTechnology:
    return LayoutTechnology(
        owner="test-owner",
        model_polarities={"nch": "nmos"},
        layers={},
        vias={},
        via_landings={},
        mom_pcell=None,
        resistor_pcell=None,
        mos_pcell=MosPcellInterface(
            length_parameter="l",
            width_parameter="Wfg",
            finger_count_parameter="fingers",
            source_terminal="S",
            drain_terminal="D",
            source_alias_prefix="S_",
            drain_alias_prefix="D_",
            cdf_callback_parameter="routePolydir",
            cdf_callback_bypass_parameters=("polyContacts",),
            gate_contact_value="Bottom",
            gate_contact_enhancement_parameter="polyContactsEnh",
            gate_contact_enhancement_value="Bottom",
        ),
    )


def _plan(instance: LayoutInstance) -> LayoutPlan:
    return LayoutPlan(
        library="test",
        cell="CELL",
        view="layout",
        stage="routed",
        generator="test",
        dbu_per_micron=1000,
        instances=(instance,),
    )


def test_pcell_semantics_expand_multifinger_terminal_aliases() -> None:
    instance = LayoutInstance(
        name="M0",
        library="pdk",
        cell="nch",
        view="layout",
        origin_dbu=(0, 0),
        transform="R0",
        parameters=(("fingers", "string", "4"),),
        terminals=(
            ("B", "VSS"),
            ("D", "Y"),
            ("G", "A"),
            ("S", "VSS"),
        ),
    )

    lowered = apply_pcell_semantics(_plan(instance), _technology())

    assert lowered.instances[0].expected_master_terminals == (
        "B",
        "D",
        "D_1",
        "G",
        "S",
        "S_1",
        "S_2",
    )
    assert lowered.instances[0].callback_parameters == ()


def test_pcell_semantics_preserve_callback_only_without_bypass_parameter() -> None:
    instance = LayoutInstance(
        name="M0",
        library="pdk",
        cell="nch",
        view="layout",
        origin_dbu=(0, 0),
        transform="R0",
        parameters=(
            ("fingers", "string", "1"),
            ("routePolydir", "string", "Bottom"),
        ),
        terminals=(("D", "Y"), ("G", "A"), ("S", "VSS")),
    )

    lowered = apply_pcell_semantics(_plan(instance), _technology())

    assert lowered.instances[0].callback_parameters == ("routePolydir",)
