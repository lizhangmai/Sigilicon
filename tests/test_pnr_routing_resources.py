from __future__ import annotations

from sigilicon.layout.pnr import (
    Axis,
    CutSpacingRule,
    EnclosureRule,
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
    RouteVia,
    RoutingDirection,
    RoutingTrackPattern,
    ViaDefinition,
)
from sigilicon.layout.pnr._routing_resources import (
    RoutingNode,
    RoutingResourceKind,
    compile_routing_resource_graph,
)


def _technology() -> PhysicalTechnology:
    via = ViaDefinition(
        "m1-m2",
        "m1",
        "cut",
        "m2",
        (Rect(-1, -1, 1, 1),),
        (Rect(-1, -1, 1, 1),),
        (Rect(-1, -1, 1, 1),),
    )
    return PhysicalTechnology(
        "resource-test",
        1000,
        1,
        layers=(
            PhysicalLayer("m1", LayerKind.ROUTING, RoutingDirection.ANY),
            PhysicalLayer("cut", LayerKind.CUT),
            PhysicalLayer("m2", LayerKind.ROUTING, RoutingDirection.VERTICAL),
        ),
        routing_resources=(
            GridlessRoutingResource("m1-corridor", "m1"),
            RoutingTrackPattern("m2-tracks", "m2", Axis.X, 2, 4, 3),
        ),
        via_definitions=(via,),
        rules=(
            MinimumWidthRule("m1-width", "m1", 2),
            MinimumSpacingRule("m1-spacing", "m1", 2),
            MinimumWidthRule("m2-width", "m2", 2),
            MinimumSpacingRule("m2-spacing", "m2", 2),
            CutSpacingRule("cut-spacing", "cut", 2, 2),
            EnclosureRule("m1-enclosure", "m1", "cut", 0, 0),
            EnclosureRule("m2-enclosure", "m2", "cut", 0, 0),
        ),
    )


def test_resource_graph_compiles_gridless_track_segment_and_via_demands() -> None:
    graph = compile_routing_resource_graph(
        _technology(),
        Rect(0, 0, 12, 12),
        congestion_bins_x=1,
        congestion_bins_y=1,
    )
    route = NetRoute(
        "signal",
        (
            RouteSegment("signal", "m1", Point(2, 2), Point(10, 2), 2),
            RouteSegment("signal", "m2", Point(10, 2), Point(10, 10), 2),
        ),
        (RouteVia("signal", "m1-m2", Point(10, 2)),),
    )

    first = graph.route_demands(route)
    second = graph.route_demands(route)
    kinds = {demand.resource.kind for demand in first}

    assert graph.issue is None
    assert first == second
    assert kinds == {
        RoutingResourceKind.GRIDLESS_CORRIDOR.value,
        RoutingResourceKind.EXPLICIT_TRACK.value,
        RoutingResourceKind.LAYER_SEGMENT.value,
        RoutingResourceKind.VIA.value,
    }
    assert all(graph.definition(item.resource).capacity >= 1 for item in first)
    assert tuple(item.capacity for item in graph.resources) == (3, 3)


def test_search_view_prices_present_and_historical_resource_usage() -> None:
    graph = compile_routing_resource_graph(
        _technology(),
        Rect(0, 0, 12, 12),
        congestion_bins_x=1,
        congestion_bins_y=1,
    )
    empty = graph.search_view(
        allowed_layers=frozenset(("m1",)),
        allow_vias=False,
        obstacles={},
        present_usage={},
        history_costs={},
        present_weight=1,
        history_weight=1,
    )
    transition = next(
        item
        for item in empty.neighbors(RoutingNode("m1", Point(2, 2)), frozenset()).transitions
        if item.end == RoutingNode("m1", Point(3, 2))
    )
    corridor = next(
        demand.resource
        for demand in transition.demands
        if demand.resource.kind == RoutingResourceKind.GRIDLESS_CORRIDOR.value
    )
    priced = graph.search_view(
        allowed_layers=frozenset(("m1",)),
        allow_vias=False,
        obstacles={},
        present_usage={corridor: 1},
        history_costs={corridor: 2},
        present_weight=1,
        history_weight=1,
    )

    assert empty.transition_cost(transition) == 1
    assert priced.transition_cost(transition) == 4
