import pytest

from sigilicon.layout.routing import RoutingStack
from sigilicon.layout.technology import LayoutTechnology, MosPcellInterface


def _technology() -> LayoutTechnology:
    return LayoutTechnology(
        owner="test-owner",
        model_polarities={"nch": "nmos"},
        layers={
            "routing1": "M1",
            "routing2": "M2",
            "routing3": "M3",
            "routing4": "M4",
        },
        vias={
            "routing1_routing2": "V12",
            "routing2_routing3": "V23",
            "routing3_routing4": "V34",
        },
        via_landings={
            "routing1_routing2": {"routing1": (10, 10), "routing2": (10, 10)},
            "routing2_routing3": {"routing2": (10, 10), "routing3": (10, 10)},
            "routing3_routing4": {"routing3": (10, 10), "routing4": (10, 10)},
        },
        mos_pcell=MosPcellInterface(
            length_parameter="channelLength",
            width_parameter="fingerWidth",
            finger_count_parameter="fingerCount",
            source_terminal="source",
            drain_terminal="drain",
            source_alias_prefix="source_",
            drain_alias_prefix="drain_",
            cdf_callback_parameter="gateContactSide",
            cdf_callback_bypass_parameters=(),
            gate_contact_value="lower",
            gate_contact_enhancement_parameter="enhanceGateContacts",
            gate_contact_enhancement_value="lower",
        ),
    )


def test_routing_stack_derives_the_declared_contiguous_stack() -> None:
    stack = RoutingStack(_technology())

    assert stack.layers == ("M1", "M2", "M3", "M4")
    assert stack.vias == ("V12", "V23", "V34")
    assert stack.vias_between("M2", "M4") == ("V23", "V34")


def test_routing_stack_applies_recipe_owned_landing_overrides() -> None:
    stack = RoutingStack(
        _technology(),
        landing_overrides={"routing2_routing3": {"routing3": (20, 30)}},
    )

    assert stack.landing_shapes("V23") == (
        ("M2", 10, 10),
        ("M3", 20, 30),
    )


def test_routing_stack_rejects_override_outside_the_via_landing() -> None:
    with pytest.raises(ValueError, match="has no landing"):
        RoutingStack(
            _technology(),
            landing_overrides={"routing2_routing3": {"routing1": (20, 30)}},
        )
