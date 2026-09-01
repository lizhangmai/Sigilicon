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


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (ResultStatus.UNSUPPORTED, MaterializationReason.UNSUPPORTED),
        (ResultStatus.FAILED, MaterializationReason.INFEASIBLE),
    ),
)
def test_non_success_results_never_become_executable(
    status: ResultStatus,
    reason: MaterializationReason,
) -> None:
    result, plan = _compile(routed_job(f"materialization-{status.value}"), status=status)

    assert result.status is status
    assert plan.acceptance.decision is MaterializationDecision.REJECTED
    assert plan.acceptance.reason is reason
    assert not plan.executable


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
