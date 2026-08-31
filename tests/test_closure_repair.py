from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.layout.physical_design import (
    FenceConstraint,
    LayerKind,
    LayerShape,
    PhysicalDesign,
    PhysicalInstance,
    PhysicalLayer,
    PhysicalMaster,
    PhysicalOwnerIdentity,
    PhysicalOwnerKind,
    PhysicalTechnology,
    Placement,
    Point,
    Rect,
    RoutingBlockage,
)
from sigilicon.experimental.reference_pnr import (
    ReferencePnrJob,
    run,
)
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_id,
    physical_design_result_id,
)
from sigilicon.experimental.reference_pnr.serialization import (
    reference_pnr_job_from_json,
)
from sigilicon.experimental.workflows.closure_campaign import (
    CampaignArtifactIdentity,
    ClosureFeedbackKind,
    ClosureFeedbackScope,
    ClosureIterationProvenance,
)
from sigilicon.experimental.workflows.closure_repair import (
    ClosureRepairDecision,
    ClosureRepairError,
    ClosureRepairPolicy,
    PlacementRepairDirective,
    apply_repair_plan,
    closure_repair_policy_from_json,
    compile_closure_repair,
    repair_plan_from_json,
)


def _job(*, fixed: bool = False) -> ReferencePnrJob:
    placement = Placement(Point(0, 0))
    return ReferencePnrJob(
        PhysicalTechnology(
            "repair-neutral",
            1000,
            2,
            layers=(PhysicalLayer("marker", LayerKind.OTHER),),
        ),
        PhysicalDesign(
            "repair-contract",
            Rect(0, 0, 20, 10),
            (PhysicalMaster("unit", 2, 2),),
            (
                PhysicalInstance(
                    "movable",
                    "unit",
                    fixed_placement=placement if fixed else None,
                ),
            ),
            routing_blockages=(
                RoutingBlockage(
                    "keepout",
                    2,
                    2,
                    (LayerShape("marker", Rect(0, 0, 2, 2)),),
                    Placement(Point(0, 6)),
                    repair_region=Rect(0, 4, 20, 10),
                ),
            ),
        ),
        constraints=(
            FenceConstraint("movable-fence", ("movable",), Rect(0, 0, 10, 4)),
        ),
    )


def _provenance(job: ReferencePnrJob):
    result = run(job)
    return result, ClosureIterationProvenance(
        "round-0",
        "0" * 32,
        "source-fixture",
        "closure",
        "repair-flow",
        (
            CampaignArtifactIdentity(
                "job",
                "physical-design.job",
                physical_design_job_id(job),
                "inputs",
                "job",
            ),
            CampaignArtifactIdentity(
                "result",
                "physical-design.result",
                physical_design_result_id(result),
                "solve",
                "result",
            ),
        ),
    )


def _feedback(
    kind: ClosureFeedbackKind = ClosureFeedbackKind.PHYSICAL_OWNER,
    identity: str = "instance:movable",
) -> tuple[ClosureFeedbackScope, ...]:
    return (
        ClosureFeedbackScope(
            kind,
            (identity,),
            ("typed-evidence:pressure-0",),
            involved_nets=("signal",),
            repairable=True,
        ),
    )


def _policy(
    *,
    target: Placement = Placement(Point(2, 0)),
    region: Rect = Rect(0, 0, 10, 4),
    budget: int = 4,
    kind: ClosureFeedbackKind = ClosureFeedbackKind.PHYSICAL_OWNER,
    identity: str = "instance:movable",
    physical_owner: PhysicalOwnerIdentity = PhysicalOwnerIdentity(
        PhysicalOwnerKind.INSTANCE,
        ("movable",),
    ),
) -> ClosureRepairPolicy:
    return ClosureRepairPolicy(
        "benchmark",
        "explicit-local-repair",
        budget,
        (
            PlacementRepairDirective(
                kind,
                identity,
                physical_owner,
                target,
                region,
                budget,
            ),
        ),
    )


def _compile(
    job: ReferencePnrJob,
    *,
    feedback: tuple[ClosureFeedbackScope, ...] | None = None,
    policy: ClosureRepairPolicy | None = None,
    provenance=None,
):
    result, valid_provenance = _provenance(job)
    return compile_closure_repair(
        owner="benchmark",
        job=job,
        result=result,
        feedback=_feedback() if feedback is None else feedback,
        policy=_policy() if policy is None else policy,
        provenance=valid_provenance if provenance is None else provenance,
    )


def test_exact_attributed_policy_compiles_and_applies_immutable_next_job() -> None:
    job = _job()
    policy = _policy()
    first = _compile(job)
    second = _compile(job)

    assert first.decision is ClosureRepairDecision.ACCEPTED
    assert first.canonical_json() == second.canonical_json()
    assert repair_plan_from_json(first.canonical_json()) == first
    assert closure_repair_policy_from_json(policy.canonical_json()) == policy

    next_job = apply_repair_plan(job, first)
    assert job.design.instances[0].fixed_placement is None
    assert next_job.design.instances[0].fixed_placement == Placement(Point(2, 0))
    assert next_job.repair_lineage is not None
    assert next_job.repair_lineage.parent_job_identity == physical_design_job_id(job)
    assert next_job.repair_lineage.parent_result_identity == first.parent_result_identity
    assert next_job.repair_lineage.feedback_identity == first.feedback_identity
    assert next_job.repair_lineage.repair_plan_identity == first.plan_id
    assert reference_pnr_job_from_json(next_job.canonical_json()) == next_job


def test_explicit_drc_rule_policy_may_map_to_one_physical_owner() -> None:
    job = _job()
    plan = _compile(
        job,
        feedback=_feedback(ClosureFeedbackKind.DRC_RULE, "M1.W.1"),
        policy=_policy(
            kind=ClosureFeedbackKind.DRC_RULE,
            identity="M1.W.1",
        ),
    )

    assert plan.accepted
    assert plan.action is not None
    assert plan.action.physical_owner.stable_name == "instance:movable"


@pytest.mark.parametrize(
    ("feedback", "policy"),
    (
        (
            _feedback(ClosureFeedbackKind.LVS_MISMATCH, "device-count"),
            ClosureRepairPolicy("benchmark", "no-lvs-model", 4, ()),
        ),
        (
            _feedback(ClosureFeedbackKind.DRC_RULE, "M2.S.2"),
            _policy(kind=ClosureFeedbackKind.DRC_RULE, identity="M1.W.1"),
        ),
    ),
)
def test_unmapped_lvs_and_drc_feedback_remain_unsupported(feedback, policy) -> None:
    plan = _compile(_job(), feedback=feedback, policy=policy)

    assert plan.decision is ClosureRepairDecision.UNSUPPORTED
    assert not plan.accepted
    assert plan.action is None


@pytest.mark.parametrize(
    "policy",
    (
        _policy(target=Placement(Point(1, 0))),
        _policy(target=Placement(Point(12, 0))),
        _policy(target=Placement(Point(6, 0)), budget=2),
        _policy(region=Rect(0, 0, 20, 4)),
    ),
)
def test_grid_region_constraint_and_displacement_violations_are_unrepairable(
    policy: ClosureRepairPolicy,
) -> None:
    plan = _compile(_job(), policy=policy)

    assert plan.decision is ClosureRepairDecision.UNREPAIRABLE
    assert plan.action is None


def test_fixed_owner_and_corrupt_parent_provenance_are_rejected() -> None:
    fixed = _compile(_job(fixed=True))
    job = _job()
    result, provenance = _provenance(job)
    corrupt = replace(
        provenance,
        artifacts=(
            replace(provenance.artifacts[0], identity="forged-identity"),
            provenance.artifacts[1],
        ),
    )
    invalid = compile_closure_repair(
        owner="benchmark",
        job=job,
        result=result,
        feedback=_feedback(),
        policy=_policy(),
        provenance=corrupt,
    )

    assert fixed.decision is ClosureRepairDecision.UNREPAIRABLE
    assert invalid.decision is ClosureRepairDecision.INVALID_IDENTITY


def test_blockage_repair_preserves_mobility_and_rejects_wrong_parent_job() -> None:
    job = _job()
    owner = PhysicalOwnerIdentity(PhysicalOwnerKind.BLOCKAGE, ("keepout",))
    feedback = _feedback(
        ClosureFeedbackKind.PHYSICAL_OWNER,
        "blockage:keepout",
    )
    policy = _policy(
        target=Placement(Point(2, 6)),
        region=Rect(0, 4, 20, 10),
        kind=ClosureFeedbackKind.PHYSICAL_OWNER,
        identity="blockage:keepout",
        physical_owner=owner,
    )
    plan = _compile(job, feedback=feedback, policy=policy)

    assert plan.accepted
    next_job = apply_repair_plan(job, plan)
    assert next_job.design.routing_blockages[0].placement == Placement(Point(2, 6))
    assert next_job.design.routing_blockages[0].repair_region == Rect(0, 4, 20, 10)
    with pytest.raises(ClosureRepairError, match="parent job identity"):
        apply_repair_plan(replace(job, constraints=()), plan)
