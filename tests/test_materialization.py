from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.layout.materialization import (
    MaterializationDecision,
    MaterializationError,
    MaterializationOwner,
    MaterializationOwnerKind,
    MaterializationReason,
    MaterializationTarget,
    compile_materialization_plan,
    materialization_plan_from_json,
    validate_materialization_plan,
)
from sigilicon.layout.physical_design import (
    Axis,
    GridlessRoutingResource,
    LayerKind,
    LayerShape,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    PhysicalLayer,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PhysicalDesignRequest,
    PhysicalDesignStage,
    Point,
    Rect,
    ResultStatus,
    RoutingBlockage,
    RoutingDirection,
    RoutingTrackPattern,
    TechnologyCapability,
)
from sigilicon.experimental.reference_pnr import (
    ReferencePnrExecutionPolicy,
    ReferencePnrJob,
    run,
)


def _gridless_job(*, maximum_route_states: int = 200_000) -> ReferencePnrJob:
    technology = PhysicalTechnology(
        "materialization-gridless",
        1000,
        1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )
    return ReferencePnrJob(
        technology,
        PhysicalDesign(
            "materialization-closed",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(PhysicalNet("signal", (PinReference("source"), PinReference("sink"))),),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
        execution_policy=ReferencePnrExecutionPolicy(maximum_route_states=maximum_route_states),
    )


def _fixed_blockage_job() -> ReferencePnrJob:
    technology = PhysicalTechnology(
        "materialization-fixed-blocker",
        1000,
        1,
        layers=(
            PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.HORIZONTAL),
        ),
        routing_resources=(RoutingTrackPattern("only-track", "route", Axis.Y, 2, 4, 1),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )
    return ReferencePnrJob(
        technology,
        PhysicalDesign(
            "materialization-infeasible",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(PhysicalNet("signal", (PinReference("source"), PinReference("sink"))),),
            routing_blockages=(
                RoutingBlockage(
                    "fixed-blocker",
                    2,
                    2,
                    (LayerShape("route", Rect(0, 0, 2, 2)),),
                    Placement(Point(7, 0)),
                ),
            ),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
    )


def _compile(job: ReferencePnrJob):
    result = run(job)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "layout-candidate"),
    )
    return result, plan


def test_closed_result_compiles_an_executable_database_neutral_plan() -> None:
    job = _gridless_job()
    result, plan = _compile(job)
    validation = validate_materialization_plan(job, result, plan)

    assert result.status is ResultStatus.SUCCEEDED
    assert plan.acceptance.decision is MaterializationDecision.EXECUTABLE
    assert plan.acceptance.reason is MaterializationReason.ACCEPTED
    assert plan.executable
    assert validation.valid
    assert plan.route_segments
    assert all(
        item.owner
        == MaterializationOwner(MaterializationOwnerKind.NET, (item.net,))
        for item in plan.route_segments
    )
    assert materialization_plan_from_json(plan.canonical_json()) == plan


def test_budget_exhaustion_retains_only_a_diagnostic_plan() -> None:
    result, plan = _compile(_gridless_job(maximum_route_states=1))

    assert result.status is ResultStatus.EXHAUSTED
    assert plan.acceptance.decision is MaterializationDecision.DIAGNOSTIC
    assert plan.acceptance.reason is MaterializationReason.BUDGET_EXHAUSTED
    assert not plan.executable
    assert len(plan.route_segments) == sum(
        len(route.segments) for route in result.routes
    )


def test_unsupported_and_infeasible_results_never_become_executable() -> None:
    unsupported_job = replace(
        _gridless_job(),
        request=replace(
            _gridless_job().request,
            required_technology_capabilities=(TechnologyCapability.VIA_DEFINITIONS,),
        ),
    )
    unsupported_result, unsupported = _compile(unsupported_job)
    infeasible_result, infeasible = _compile(_fixed_blockage_job())

    assert unsupported_result.status is ResultStatus.UNSUPPORTED
    assert unsupported.acceptance.decision is MaterializationDecision.REJECTED
    assert unsupported.acceptance.reason is MaterializationReason.UNSUPPORTED
    assert not unsupported.executable
    assert infeasible_result.status is ResultStatus.FAILED
    assert infeasible.acceptance.decision is MaterializationDecision.REJECTED
    assert infeasible.acceptance.reason is MaterializationReason.INFEASIBLE
    assert not infeasible.executable
    assert tuple(item.blockage for item in infeasible.routing_blockages) == (
        "fixed-blocker",
    )


def test_materialization_rejects_result_identity_mismatch() -> None:
    job = _gridless_job()
    result = run(job)
    changed_job = replace(job, design=replace(job.design, name="different-design"))

    with pytest.raises(MaterializationError, match="result_input_mismatch"):
        compile_materialization_plan(
            changed_job,
            result,
            MaterializationTarget("benchmark", "layout-candidate"),
        )


def test_plan_validation_checks_route_owner_and_geometry_independently() -> None:
    job = _gridless_job()
    result, plan = _compile(job)
    segment = plan.route_segments[0]
    invalid_segment = replace(
        segment,
        end=Point(job.design.die.x_max + 1, segment.end.y),
        owner=MaterializationOwner(
            MaterializationOwnerKind.NET,
            ("wrong-net",),
        ),
    )
    invalid = replace(
        plan,
        route_segments=(invalid_segment, *plan.route_segments[1:]),
    )
    validation = validate_materialization_plan(job, result, invalid)

    assert not validation.valid
    assert {item.code for item in validation.issues} >= {
        "instruction_mismatch",
        "owner_mismatch",
        "coordinate_outside_die",
    }
