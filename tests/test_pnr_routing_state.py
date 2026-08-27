from __future__ import annotations

from sigilicon.layout.pnr import (
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    NetRoute,
    PhysicalLayer,
    PhysicalTechnology,
    Point,
    Rect,
    RouteSegment,
    RoutingDirection,
)
from sigilicon.layout.pnr._routing_resources import (
    RoutingResourceIdentity,
    compile_routing_resource_graph,
)
from sigilicon.layout.pnr._routing_state import RoutingState


def _route(net: str, y: int) -> NetRoute:
    return NetRoute(
        net,
        (RouteSegment(net, "route", Point(2, y), Point(18, y), 2),),
    )


def _resource_graph():
    return compile_routing_resource_graph(
        PhysicalTechnology(
            "state-test",
            1000,
            1,
            layers=(
                PhysicalLayer(
                    "route", LayerKind.ROUTING, RoutingDirection.ANY
                ),
            ),
            routing_resources=(
                GridlessRoutingResource("routing-region", "route"),
            ),
            rules=(
                MinimumWidthRule("width", "route", 2),
                MinimumSpacingRule("spacing", "route", 2),
            ),
        ),
        Rect(0, 0, 20, 12),
        congestion_bins_x=2,
        congestion_bins_y=1,
    )


def test_routing_state_selects_latest_actual_blocker_and_rips_only_scope() -> None:
    state = RoutingState.empty()
    resource_graph = _resource_graph()
    state = state.with_route(_route("first", 4), resource_graph)
    state = state.with_route(_route("second", 8), resource_graph)

    assert state.select_victim(("unrouted", "first", "second")) == "second"
    assert state.select_victim(("unrouted",)) is None
    assert tuple(item.net for item in state.occupancy_by_layer["route"]) == (
        "first",
        "second",
    )

    cost_key = RoutingResourceIdentity(
        "gridless_corridor", "route", (0, 0, "horizontal")
    )
    updated = state.with_history_penalty({cost_key: 2}).rip_up(
        ("second",), resource_graph
    )

    assert tuple(updated.routes_by_net) == ("first",)
    assert updated.routed_order == ("first",)
    assert updated.history_costs == {cost_key: 2}
    assert tuple(item.net for item in updated.occupancy_by_layer["route"]) == (
        "first",
    )
    assert set(updated.resource_occupants.values()) == {("first",)}

    targeted = updated.with_length_targets({"first": 40})
    assert targeted.length_targets == {"first": 40}
