from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sigilicon.layout.pnr import (
    AlignmentAnchor,
    AlignmentConstraint,
    ArrayConstraint,
    Axis,
    BoundingBoxAreaObjective,
    BoundingBoxCongestionObjective,
    ConstraintMode,
    ConstraintStatus,
    DensityOverflowObjective,
    EstimatedHpwlObjective,
    FenceConstraint,
    LayerKind,
    MasterPin,
    Orientation,
    OrderingConstraint,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalLayer,
    PhysicalMaster,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PlacementObjective,
    PnrInputError,
    PnrExecutionPolicy,
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    ResultStatus,
    RoutingDirection,
    SeparationAxis,
    SeparationConstraint,
    SymmetryConstraint,
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


def test_execution_policy_has_an_identity_separate_from_physical_intent() -> None:
    job = _job()
    first = run(job)
    second = run(
        replace(
            job,
            execution_policy=replace(
                job.execution_policy,
                maximum_search_states=100_001,
            ),
        )
    )

    assert first.provenance.input_sha256 == second.provenance.input_sha256
    assert first.provenance.execution_sha256 != second.provenance.execution_sha256


def _placements(job: PhysicalDesignJob) -> dict[str, Placement]:
    result = run(job)
    assert result.status is ResultStatus.SUCCEEDED
    assert all(
        outcome.status is ConstraintStatus.SATISFIED
        for outcome in result.constraint_outcomes
    )
    return {item.instance: item.placement for item in result.placements}


def test_alignment_constraint_uses_explicit_geometric_anchor() -> None:
    master = PhysicalMaster(
        name="square",
        width_dbu=10,
        height_dbu=10,
        allowed_orientations=(Orientation.R0,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "alignment",
            Rect(0, 0, 40, 30),
            (master,),
            (
                PhysicalInstance("anchor", master.name, Placement(Point(0, 10))),
                PhysicalInstance("moving", master.name),
            ),
        ),
        constraints=(
            AlignmentConstraint(
                "align-bottom",
                ("anchor", "moving"),
                Axis.Y,
                AlignmentAnchor.LOW,
            ),
        ),
    )

    placements = _placements(job)

    assert placements["moving"].origin == Point(10, 10)


def test_ordering_constraint_enforces_direction_and_gap() -> None:
    master = PhysicalMaster(
        "square",
        10,
        10,
        allowed_orientations=(Orientation.R0,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "ordered",
            Rect(0, 0, 50, 20),
            (master,),
            (
                PhysicalInstance("first", master.name, Placement(Point(0, 0))),
                PhysicalInstance("second", master.name),
            ),
        ),
        constraints=(
            OrderingConstraint(
                "first-before-second",
                "first",
                "second",
                Axis.X,
                minimum_gap_dbu=10,
            ),
        ),
    )

    placements = _placements(job)

    assert placements["second"].origin == Point(20, 0)


def test_separation_constraint_is_undirected() -> None:
    master = PhysicalMaster(
        "square",
        10,
        10,
        allowed_orientations=(Orientation.R0,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "separated",
            Rect(0, 0, 50, 20),
            (master,),
            (
                PhysicalInstance("one", master.name, Placement(Point(0, 0))),
                PhysicalInstance("two", master.name),
            ),
        ),
        constraints=(
            SeparationConstraint(
                "gap",
                "one",
                "two",
                minimum_gap_dbu=10,
                axis=SeparationAxis.ANY,
            ),
        ),
    )

    placements = _placements(job)

    assert placements["two"].origin == Point(20, 0)


def test_symmetry_constraint_reflects_bounding_boxes_about_axis() -> None:
    master = PhysicalMaster(
        "square",
        10,
        10,
        allowed_orientations=(Orientation.R0,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "symmetric",
            Rect(0, 0, 30, 20),
            (master,),
            (
                PhysicalInstance("left", master.name, Placement(Point(0, 0))),
                PhysicalInstance("right", master.name),
            ),
        ),
        constraints=(
            SymmetryConstraint(
                "mirror",
                (("left", "right"),),
                Axis.X,
                coordinate_dbu=10,
            ),
        ),
    )

    placements = _placements(job)

    assert placements["right"].origin == Point(10, 0)


def test_array_constraint_assigns_row_major_origins() -> None:
    master = PhysicalMaster(
        "tile",
        10,
        10,
        allowed_orientations=(Orientation.R0,),
    )
    instances = (
        PhysicalInstance("a", master.name, Placement(Point(0, 0))),
        PhysicalInstance("b", master.name),
        PhysicalInstance("c", master.name),
        PhysicalInstance("d", master.name),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign("array", Rect(0, 0, 30, 30), (master,), instances),
        constraints=(
            ArrayConstraint(
                "two-by-two",
                tuple(instance.name for instance in instances),
                columns=2,
                x_pitch_dbu=10,
                y_pitch_dbu=10,
            ),
        ),
    )

    placements = _placements(job)

    assert {name: placement.origin for name, placement in placements.items()} == {
        "a": Point(0, 0),
        "b": Point(10, 0),
        "c": Point(0, 10),
        "d": Point(10, 10),
    }


def test_conflicting_fixed_constraints_return_a_violation() -> None:
    master = PhysicalMaster("square", 10, 10)
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "conflict",
            Rect(0, 0, 30, 30),
            (master,),
            (
                PhysicalInstance("low", master.name, Placement(Point(0, 0))),
                PhysicalInstance("high", master.name, Placement(Point(10, 10))),
            ),
        ),
        constraints=(
            AlignmentConstraint("impossible", ("low", "high"), Axis.Y),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.FAILED
    assert result.constraint_outcomes[0].status is ConstraintStatus.VIOLATED
    assert result.stage_reports[0].diagnostics[0].code == "fixed_constraint_violation"


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


def test_search_exhaustion_is_not_reported_as_infeasibility() -> None:
    job = replace(
        _job(),
        execution_policy=PnrExecutionPolicy(maximum_search_states=1),
    )

    result = run(job)

    assert result.status is ResultStatus.EXHAUSTED
    assert result.stage_reports[0].diagnostics[0].code == "placement_search_exhausted"


def test_placement_repair_state_budget_must_be_positive() -> None:
    job = replace(
        _job(),
        execution_policy=replace(
            _job().execution_policy,
            maximum_placement_repair_states=0,
        ),
    )

    with pytest.raises(
        PnrInputError,
        match="maximum placement repair states must be positive",
    ):
        run(job)


def test_empty_netlist_routing_is_vacuously_succeeded() -> None:
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

    assert result.status is ResultStatus.SUCCEEDED
    assert result.placements
    assert result.routes == ()
    assert tuple(report.stage for report in result.stage_reports) == (
        PnrStage.PLACEMENT,
        PnrStage.ROUTING,
    )
    assert result.constraint_outcomes[0].status is ConstraintStatus.SATISFIED


def test_soft_constraint_ranks_legal_placements() -> None:
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

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.stage_reports) == 1
    assert result.stage_reports[0].stage is PnrStage.PLACEMENT
    assert result.stage_reports[0].status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[0].status is ConstraintStatus.SATISFIED
    placements = {item.instance: item.placement for item in result.placements}
    assert placements["movable"].origin.x >= 20


def _net_objective_job(objective: PlacementObjective) -> PhysicalDesignJob:
    master = PhysicalMaster(
        "node",
        10,
        10,
        pins=(MasterPin("p"),),
        allowed_orientations=(Orientation.R0,),
    )
    return PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "net-objective",
            Rect(0, 0, 50, 10),
            (master,),
            (
                PhysicalInstance("blocker", master.name, Placement(Point(0, 0))),
                PhysicalInstance("moving", master.name),
                PhysicalInstance("target", master.name, Placement(Point(40, 0))),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (
                        PinReference("p", "moving"),
                        PinReference("p", "target"),
                    ),
                ),
            ),
        ),
        request=PnrRequest(objectives=(objective,)),
    )


def test_estimated_hpwl_objective_moves_connected_instances_together() -> None:
    result = run(_net_objective_job(EstimatedHpwlObjective("wirelength")))

    assert result.status is ResultStatus.SUCCEEDED
    placements = {item.instance: item.placement for item in result.placements}
    assert placements["moving"].origin == Point(30, 0)
    metrics = {metric.name: metric.value for metric in result.stage_reports[0].metrics}
    assert metrics["objective.wirelength"] == 10


def test_estimated_hpwl_uses_transformed_pin_access_geometry() -> None:
    layer = PhysicalLayer("routing", LayerKind.ROUTING, RoutingDirection.HORIZONTAL)
    master = PhysicalMaster(
        "oriented-master",
        20,
        10,
        pins=(MasterPin("p", (PinAccess("routing", Rect(0, 0, 2, 2)),)),),
        allowed_orientations=(Orientation.R90,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 1, layers=(layer,)),
        PhysicalDesign(
            "oriented-pin",
            Rect(0, 0, 20, 20),
            (master,),
            (
                PhysicalInstance(
                    "rotated",
                    master.name,
                    Placement(Point(0, 0), Orientation.R90),
                ),
            ),
            ports=(
                PhysicalPort(
                    "top",
                    (PinAccess("routing", Rect(18, 0, 20, 2)),),
                ),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (
                        PinReference("p", "rotated"),
                        PinReference("top"),
                    ),
                ),
            ),
        ),
        request=PnrRequest(objectives=(EstimatedHpwlObjective("pin-hpwl"),)),
    )

    result = run(job)

    assert result.status is ResultStatus.SUCCEEDED
    metrics = {metric.name: metric.value for metric in result.stage_reports[0].metrics}
    assert metrics["objective.pin-hpwl"] == 10


def test_density_overflow_objective_spreads_occupancy_across_bins() -> None:
    master = PhysicalMaster(
        "tile",
        10,
        10,
        allowed_orientations=(Orientation.R0,),
    )
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "density",
            Rect(0, 0, 40, 10),
            (master,),
            (
                PhysicalInstance("fixed", master.name, Placement(Point(0, 0))),
                PhysicalInstance("moving", master.name),
            ),
        ),
        request=PnrRequest(
            objectives=(
                DensityOverflowObjective(
                    "density-overflow",
                    bins_x=2,
                    bins_y=1,
                    target_density=0.5,
                ),
            ),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.SUCCEEDED
    placements = {item.instance: item.placement for item in result.placements}
    assert placements["moving"].origin == Point(20, 0)
    metrics = {metric.name: metric.value for metric in result.stage_reports[0].metrics}
    assert metrics["objective.density-overflow"] == 0


def test_bounding_box_area_and_congestion_proxy_are_observable_objectives() -> None:
    area_result = run(
        replace(
            _job(),
            request=PnrRequest(objectives=(BoundingBoxAreaObjective("area"),)),
        )
    )
    congestion_result = run(
        _net_objective_job(
            BoundingBoxCongestionObjective("congestion", bins_x=5, bins_y=1)
        )
    )

    assert area_result.status is ResultStatus.SUCCEEDED
    assert congestion_result.status is ResultStatus.SUCCEEDED
    area_metrics = {
        metric.name: metric.value for metric in area_result.stage_reports[0].metrics
    }
    congestion_metrics = {
        metric.name: metric.value
        for metric in congestion_result.stage_reports[0].metrics
    }
    assert area_metrics["objective.area"] == 400
    assert congestion_metrics["objective.congestion"] >= 0


def test_soft_constraint_violation_does_not_change_hard_legality() -> None:
    master = PhysicalMaster("square", 10, 10)
    job = PhysicalDesignJob(
        PhysicalTechnology("neutral", 1000, 10),
        PhysicalDesign(
            "soft-violation",
            Rect(0, 0, 30, 30),
            (master,),
            (
                PhysicalInstance("low", master.name, Placement(Point(0, 0))),
                PhysicalInstance("high", master.name, Placement(Point(10, 10))),
            ),
        ),
        constraints=(
            AlignmentConstraint(
                "preferred-alignment",
                ("low", "high"),
                Axis.Y,
                mode=ConstraintMode.SOFT,
            ),
        ),
    )

    result = run(job)

    assert result.status is ResultStatus.SUCCEEDED
    assert result.constraint_outcomes[0].status is ConstraintStatus.VIOLATED
    metrics = {metric.name: metric.value for metric in result.stage_reports[0].metrics}
    assert metrics["soft_constraint_penalty"] == 10


def test_density_objective_rejects_off_grid_bin_edges() -> None:
    job = replace(
        _job(),
        request=PnrRequest(
            objectives=(
                DensityOverflowObjective(
                    "off-grid-bins",
                    bins_x=4,
                    bins_y=1,
                    target_density=0.5,
                ),
            ),
        ),
    )

    with pytest.raises(PnrInputError, match="bin edges are off-grid"):
        run(job)


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
