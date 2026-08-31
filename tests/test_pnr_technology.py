from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.experimental.reference_pnr import (
    Axis,
    CutSpacingRule,
    EnclosureRule,
    ExtensionRule,
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    ReferencePnrJob,
    PhysicalLayer,
    PhysicalTechnology,
    PnrInputError,
    PhysicalDesignRequest,
    Rect,
    ResultStatus,
    RoutingDirection,
    RoutingTrackPattern,
    TechnologyCapability,
    ViaDefinition,
    ViaStack,
    run,
)


def _technology() -> PhysicalTechnology:
    layers = (
        PhysicalLayer("m1", LayerKind.ROUTING, RoutingDirection.HORIZONTAL),
        PhysicalLayer("v1", LayerKind.CUT),
        PhysicalLayer("m2", LayerKind.ROUTING, RoutingDirection.VERTICAL),
        PhysicalLayer("v2", LayerKind.CUT),
        PhysicalLayer("m3", LayerKind.ROUTING, RoutingDirection.HORIZONTAL),
    )
    via12 = ViaDefinition(
        "via12",
        "m1",
        "v1",
        "m2",
        lower_shapes=(Rect(-2, -2, 2, 2),),
        cut_shapes=(Rect(-1, -1, 1, 1),),
        upper_shapes=(Rect(-2, -2, 2, 2),),
    )
    via23 = ViaDefinition(
        "via23",
        "m2",
        "v2",
        "m3",
        lower_shapes=(Rect(-2, -2, 2, 2),),
        cut_shapes=(Rect(-1, -1, 1, 1),),
        upper_shapes=(Rect(-2, -2, 2, 2),),
    )
    return PhysicalTechnology(
        "neutral-routing",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=layers,
        routing_resources=(
            RoutingTrackPattern("m1-tracks", "m1", Axis.Y, 0, 4, 20),
            GridlessRoutingResource("m2-gridless", "m2"),
            RoutingTrackPattern("m3-tracks", "m3", Axis.Y, 0, 4, 20),
        ),
        via_definitions=(via12, via23),
        via_stacks=(ViaStack("m1-to-m3", ("via12", "via23")),),
        rules=(
            MinimumWidthRule("m1-width", "m1", 2),
            MinimumSpacingRule("m1-spacing", "m1", 2),
            EnclosureRule("m1-v1-enclosure", "m1", "v1", 1, 1),
            ExtensionRule("m1-v1-extension", "m1", "v1", Axis.X, 1),
            CutSpacingRule("v1-spacing", "v1", 2, 2),
        ),
    )


def _job(technology: PhysicalTechnology) -> ReferencePnrJob:
    return ReferencePnrJob(
        technology,
        PhysicalDesign("empty", Rect(0, 0, 40, 40), (), ()),
    )


def test_required_capabilities_are_derived_from_normalized_technology_facts() -> None:
    required = tuple(TechnologyCapability)
    result = run(
        replace(
            _job(_technology()),
            request=PhysicalDesignRequest(required_technology_capabilities=required),
        )
    )

    assert result.status is ResultStatus.SUCCEEDED


def test_missing_technology_capability_is_explicitly_unsupported() -> None:
    result = run(
        replace(
            _job(PhysicalTechnology("minimal", 1000, 1)),
            request=PhysicalDesignRequest(
                required_technology_capabilities=(
                    TechnologyCapability.VIA_DEFINITIONS,
                ),
            ),
        )
    )

    assert result.status is ResultStatus.UNSUPPORTED
    diagnostic = result.stage_reports[0].diagnostics[0]
    assert diagnostic.code == "unsupported_technology_capability"
    assert diagnostic.entities == ("via_definitions",)


def test_discontinuous_via_stack_is_structurally_invalid() -> None:
    technology = _technology()
    invalid = replace(
        technology,
        via_stacks=(ViaStack("reversed", ("via23", "via12")),),
    )

    with pytest.raises(PnrInputError, match="via stack reversed is discontinuous"):
        run(_job(invalid))


def test_track_coordinate_axis_must_match_preferred_route_direction() -> None:
    technology = _technology()
    resources = (
        replace(technology.routing_resources[0], axis=Axis.X),
        *technology.routing_resources[1:],
    )

    with pytest.raises(PnrInputError, match="track axis conflicts"):
        run(_job(replace(technology, routing_resources=resources)))
