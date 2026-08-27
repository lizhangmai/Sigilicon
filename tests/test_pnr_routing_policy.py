from __future__ import annotations

from sigilicon.layout.pnr import (
    LayerShape,
    Rect,
    RoutingLayerConstraint,
    RoutingLengthConstraint,
    RoutingRegionConstraint,
    RoutingShieldConstraint,
    RoutingSkewConstraint,
    RoutingViaCountConstraint,
)
from sigilicon.layout.pnr._routing_policy import (
    RoutingLengthWindow,
    RoutingOrderDependency,
    compile_routing_policy,
)


def test_compiler_builds_one_explicit_transitive_routing_group_policy() -> None:
    required_region = LayerShape("route", Rect(10, 10, 20, 20))
    policy = compile_routing_policy(
        (
            RoutingLayerConstraint("signal-layer", "signal", ("route",)),
            RoutingLengthConstraint("signal-length", "signal", 40, 40),
            RoutingLengthConstraint("peer-length", "peer", 30, 50),
            RoutingViaCountConstraint("peer-vias", "peer", 2),
            RoutingRegionConstraint("peer-region", "peer", (required_region,)),
            RoutingSkewConstraint("matched-pair", ("signal", "peer"), 4),
            RoutingShieldConstraint(
                "signal-shield",
                "signal",
                "shield",
                maximum_spacing_dbu=2,
                layers=("route",),
            ),
        ),
        ("other", "peer", "shield", "signal"),
    )

    assert len(policy.groups) == 1
    group = policy.groups[0]
    assert group.name == "routing-group:peer,shield,signal"
    assert group.constraints == ("matched-pair", "signal-shield")
    assert group.nets == ("peer", "shield", "signal")
    assert group.reroute_scope == group.nets
    assert group.length_windows == {
        "peer": RoutingLengthWindow(30, 50),
        "shield": RoutingLengthWindow(),
        "signal": RoutingLengthWindow(40, 40),
    }
    assert group.length_windows["signal"].target_dbu == 40
    assert group.length_matches[0].constraint == "matched-pair"
    assert group.length_matches[0].maximum_skew_dbu == 4
    assert group.shields[0].constraint == "signal-shield"
    assert group.shields[0].layers == ("route",)
    assert group.order_dependencies == (
        RoutingOrderDependency("signal", "shield"),
    )
    assert policy.route_order == ("other", "peer", "signal", "shield")
    assert policy.reroute_scope("peer") == group.nets
    assert policy.reroute_scope("other") == ("other",)

    signal = policy.for_net("signal")
    peer = policy.for_net("peer")
    independent = policy.for_net("other")
    assert signal.allowed_layers == frozenset(("route",))
    assert signal.length_window == RoutingLengthWindow(40, 40)
    assert peer.maximum_vias == 2
    assert peer.required_regions == (required_region,)
    assert peer.length_window == RoutingLengthWindow(30, 50)
    assert signal.cost == group.cost
    assert signal.cost.congestion_weight == 0
    assert signal.cost.history_weight == 1
    assert independent.cost.congestion_weight == 1
    assert independent.cost.group_violation_weight == 0


def test_compiler_intersects_length_windows_without_router_type_checks() -> None:
    policy = compile_routing_policy(
        (
            RoutingLengthConstraint("lower", "signal", 20, 80),
            RoutingLengthConstraint("upper", "signal", 40, 60),
        ),
        ("signal",),
    )

    window = policy.for_net("signal").length_window
    assert window == RoutingLengthWindow(40, 60)
    assert window.feasible
    assert window.target_dbu is None
    assert policy.groups == ()
