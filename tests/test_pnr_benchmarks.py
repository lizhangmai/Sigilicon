from __future__ import annotations

import pytest

from sigilicon.layout.physical_design import (
    AlignmentConstraint,
    ArrayConstraint,
    Axis,
    ConstraintStatus,
    Orientation,
    OrderingConstraint,
    PhysicalDesign,
    PhysicalInstance,
    PhysicalMaster,
    PhysicalTechnology,
    Placement,
    Point,
    Rect,
    ResultStatus,
    SymmetryConstraint,
)
from sigilicon.experimental.reference_pnr import (
    ReferencePnrJob,
    run,
)


def _row_based_job() -> ReferencePnrJob:
    master = PhysicalMaster(
        "row-master",
        4,
        2,
        allowed_orientations=(Orientation.R0,),
    )
    names = "a", "b", "c"
    return ReferencePnrJob(
        PhysicalTechnology("unit-grid", 1000, 1),
        PhysicalDesign(
            "row-based",
            Rect(0, 0, 20, 4),
            (master,),
            tuple(PhysicalInstance(name, master.name) for name in names),
        ),
        constraints=(
            AlignmentConstraint("row", names, Axis.Y),
            OrderingConstraint("a-before-b", "a", "b", Axis.X),
            OrderingConstraint("b-before-c", "b", "c", Axis.X),
        ),
    )


def _symmetric_job() -> ReferencePnrJob:
    master = PhysicalMaster(
        "free-master",
        4,
        6,
        allowed_orientations=(Orientation.R0,),
    )
    return ReferencePnrJob(
        PhysicalTechnology("double-grid", 500, 2),
        PhysicalDesign(
            "custom-symmetry",
            Rect(0, 0, 24, 12),
            (master,),
            (
                PhysicalInstance("left", master.name),
                PhysicalInstance("right", master.name),
            ),
        ),
        constraints=(
            SymmetryConstraint(
                "mirror-pair",
                (("left", "right"),),
                Axis.X,
                coordinate_dbu=12,
            ),
        ),
    )


def _array_job() -> ReferencePnrJob:
    master = PhysicalMaster(
        "array-master",
        5,
        5,
        allowed_orientations=(Orientation.R0,),
    )
    names = "u0", "u1", "u2", "u3"
    return ReferencePnrJob(
        PhysicalTechnology("five-grid", 200, 5),
        PhysicalDesign(
            "regular-array",
            Rect(0, 0, 20, 20),
            (master,),
            tuple(PhysicalInstance(name, master.name) for name in names),
        ),
        constraints=(
            ArrayConstraint(
                "two-by-two",
                names,
                columns=2,
                x_pitch_dbu=5,
                y_pitch_dbu=5,
            ),
        ),
    )


@pytest.mark.parametrize(
    "job",
    (_row_based_job(), _symmetric_job(), _array_job()),
    ids=("row-based", "custom-symmetry", "regular-array"),
)
def test_reference_placer_uses_one_interface_across_design_styles(
    job: ReferencePnrJob,
) -> None:
    result = run(job)

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.placements) == len(job.design.instances)
    assert all(
        outcome.status is ConstraintStatus.SATISFIED
        for outcome in result.constraint_outcomes
    )
    assert result.provenance.deterministic is True
