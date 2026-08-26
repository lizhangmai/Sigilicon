from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sigilicon.layout.pnr import (
    ConstraintMode,
    ConstraintStatus,
    FenceConstraint,
    Orientation,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalMaster,
    PhysicalTechnology,
    Placement,
    PnrInputError,
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    run,
)


def _job(*, technology_name: str = "neutral-tech", grid: int = 10) -> PhysicalDesignJob:
    master = PhysicalMaster(
        name="rectangular-master",
        width_dbu=20,
        height_dbu=10,
        allowed_orientations=(Orientation.R0, Orientation.R90),
    )
    return PhysicalDesignJob(
        technology=PhysicalTechnology(
            name=technology_name,
            dbu_per_micron=1000,
            manufacturing_grid_dbu=grid,
        ),
        design=PhysicalDesign(
            name="neutral-design",
            die=Rect(0, 0, 100, 100),
            masters=(master,),
            instances=(
                PhysicalInstance(
                    name="fixed",
                    master=master.name,
                    fixed_placement=Placement(Point(0, 0)),
                ),
                PhysicalInstance(name="movable", master=master.name),
            ),
        ),
    )


def test_reference_engine_places_the_same_model_for_distinct_technologies() -> None:
    first = run(_job(technology_name="technology-a", grid=10))
    second = run(_job(technology_name="technology-b", grid=5))

    assert first.status is ResultStatus.SUCCEEDED
    assert second.status is ResultStatus.SUCCEEDED
    assert first.placements == second.placements
    assert first.provenance.input_sha256 != second.provenance.input_sha256
    assert first.provenance.deterministic is True


def test_hard_fence_is_solved_and_reported_through_the_public_interface() -> None:
    job = _job()
    fenced = replace(
        job,
        constraints=(
            FenceConstraint(
                name="right-hand-region",
                instances=("movable",),
                region=Rect(20, 0, 40, 20),
            ),
        ),
    )

    result = run(fenced)

    assert result.status is ResultStatus.SUCCEEDED
    placements = {item.instance: item.placement for item in result.placements}
    assert placements["movable"] == Placement(Point(20, 0), Orientation.R0)
    assert result.constraint_outcomes[0].status is ConstraintStatus.SATISFIED
    assert result.stage_reports[0].status is ResultStatus.SUCCEEDED


def test_valid_but_infeasible_job_returns_diagnostics_instead_of_raising() -> None:
    master = PhysicalMaster(name="full-die", width_dbu=20, height_dbu=20)
    job = PhysicalDesignJob(
        technology=PhysicalTechnology("technology", 1000, 1),
        design=PhysicalDesign(
            name="infeasible",
            die=Rect(0, 0, 20, 20),
            masters=(master,),
            instances=(
                PhysicalInstance("one", master.name),
                PhysicalInstance("two", master.name),
            ),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.FAILED
    assert result.stage_reports[0].diagnostics[0].code == "placement_infeasible"


def test_unimplemented_capability_is_explicitly_unsupported() -> None:
    job = replace(
        _job(),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        constraints=(
            FenceConstraint(
                name="preference",
                instances=("movable",),
                region=Rect(20, 0, 100, 100),
                mode=ConstraintMode.SOFT,
            ),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.UNSUPPORTED
    assert result.placements == ()
    assert {item.code for report in result.stage_reports for item in report.diagnostics} == {
        "unsupported_constraint_mode",
        "unsupported_stage",
    }
    assert result.constraint_outcomes[0].status is ConstraintStatus.UNSUPPORTED


def test_unsupported_constraint_has_a_placement_stage_diagnostic() -> None:
    job = replace(
        _job(),
        constraints=(
            FenceConstraint(
                name="preference",
                instances=("movable",),
                region=Rect(20, 0, 100, 100),
                mode=ConstraintMode.SOFT,
            ),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.UNSUPPORTED
    assert len(result.stage_reports) == 1
    assert result.stage_reports[0].stage is PnrStage.PLACEMENT
    assert result.stage_reports[0].status is ResultStatus.UNSUPPORTED
    assert result.stage_reports[0].diagnostics[0].code == "unsupported_constraint_mode"


def test_structurally_invalid_job_fails_before_solving() -> None:
    job = _job()
    invalid = replace(
        job,
        design=replace(
            job.design,
            instances=(PhysicalInstance("unknown", "missing-master"),),
        ),
    )

    with pytest.raises(PnrInputError, match="unknown master"):
        run(invalid)


def test_result_is_deterministic_and_canonically_serializable() -> None:
    job = _job()

    first = run(job)
    second = run(job)

    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert first.canonical_json().endswith("\n")


def test_core_source_has_no_project_pdk_or_database_dependency() -> None:
    source_root = Path(__file__).parents[1] / "src" / "sigilicon" / "layout" / "pnr"
    source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in source_root.rglob("*.py")
    )

    assert "tsmc28" not in source
    assert "cim_compute" not in source
    assert "virtuoso" not in source
    assert "laygo2" not in source
