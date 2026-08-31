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
    Point,
    ResultStatus,
)
from physical_design_fixtures import routed_job, typed_result


def _compile(job, *, status: ResultStatus = ResultStatus.SUCCEEDED):
    result = typed_result(job, status=status)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "layout-candidate"),
    )
    return result, plan


def test_closed_result_compiles_an_executable_database_neutral_plan() -> None:
    job = routed_job("materialization-closed")
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
    result, plan = _compile(
        routed_job("materialization-exhausted"),
        status=ResultStatus.EXHAUSTED,
    )

    assert result.status is ResultStatus.EXHAUSTED
    assert plan.acceptance.decision is MaterializationDecision.DIAGNOSTIC
    assert plan.acceptance.reason is MaterializationReason.BUDGET_EXHAUSTED
    assert not plan.executable
    assert len(plan.route_segments) == sum(
        len(route.segments) for route in result.routes
    )


def test_unsupported_and_infeasible_results_never_become_executable() -> None:
    unsupported_result, unsupported = _compile(
        routed_job("materialization-unsupported"),
        status=ResultStatus.UNSUPPORTED,
    )
    infeasible_result, infeasible = _compile(
        routed_job("materialization-infeasible"),
        status=ResultStatus.FAILED,
    )

    assert unsupported_result.status is ResultStatus.UNSUPPORTED
    assert unsupported.acceptance.decision is MaterializationDecision.REJECTED
    assert unsupported.acceptance.reason is MaterializationReason.UNSUPPORTED
    assert not unsupported.executable
    assert infeasible_result.status is ResultStatus.FAILED
    assert infeasible.acceptance.decision is MaterializationDecision.REJECTED
    assert infeasible.acceptance.reason is MaterializationReason.INFEASIBLE
    assert not infeasible.executable
    assert not infeasible.routing_blockages


def test_materialization_rejects_result_identity_mismatch() -> None:
    job = routed_job("materialization-identity")
    result = typed_result(job)
    changed_job = replace(job, design=replace(job.design, name="different-design"))

    with pytest.raises(MaterializationError, match="result_input_mismatch"):
        compile_materialization_plan(
            changed_job,
            result,
            MaterializationTarget("benchmark", "layout-candidate"),
        )


def test_plan_validation_checks_route_owner_and_geometry_independently() -> None:
    job = routed_job("materialization-validation")
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
