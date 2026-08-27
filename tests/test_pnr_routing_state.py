from __future__ import annotations

from sigilicon.layout.pnr import NetRoute, Point, RouteSegment
from sigilicon.layout.pnr._routing_state import RoutingState


def _route(net: str, y: int) -> NetRoute:
    return NetRoute(
        net,
        (RouteSegment(net, "route", Point(2, y), Point(18, y), 2),),
    )


def test_routing_state_selects_latest_actual_blocker_and_rips_only_scope() -> None:
    state = RoutingState.empty()
    state = state.with_route(_route("first", 4), {})
    state = state.with_route(_route("second", 8), {})

    assert state.select_victim(("unrouted", "first", "second")) == "second"
    assert state.select_victim(("unrouted",)) is None
    assert tuple(item.net for item in state.occupancy_by_layer["route"]) == (
        "first",
        "second",
    )

    cost_key = ("route", 0, 0, "horizontal")
    updated = state.with_history_penalty({cost_key: 2}).rip_up(("second",), {})

    assert tuple(updated.routes_by_net) == ("first",)
    assert updated.routed_order == ("first",)
    assert updated.history_costs == {cost_key: 2}
    assert tuple(item.net for item in updated.occupancy_by_layer["route"]) == (
        "first",
    )
