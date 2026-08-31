"""Compile attributed closure feedback into one explicit next-job repair.

The compiler does not search placement space.  A project-owned policy maps one
typed feedback identity to one exact physical-owner placement.  This Module
only proves that the requested local change is representable and legal, then
rebuilds an immutable :class:`ReferencePnrJob` with complete parentage.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

from sigilicon.canonical import canonical_from_json, canonical_json
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.experimental.reference_pnr.model import (
    ConstraintMode,
    ReferencePnrJob,
    ReferencePnrJobLineage,
    ReferencePnrResult,
    PhysicalOwnerIdentity,
    PhysicalOwnerKind,
    Placement,
    Point,
    Rect,
    ResultStatus,
)
from sigilicon.experimental.reference_pnr._constraints import evaluate_constraint
from sigilicon.experimental.reference_pnr._legality import (
    instance_region,
    rectangles_conflict,
)
from sigilicon.layout.physical_geometry import placed_rect, placed_sized_rect
from sigilicon.layout.physical_design import PhysicalDesignJob, PhysicalDesignResult
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_id,
    physical_design_result_id,
)

if TYPE_CHECKING:
    from sigilicon.experimental.workflows.closure_campaign import ClosureIterationProvenance


class ClosureRepairError(ValueError):
    """A repair policy, plan, or application request is malformed."""


def _stable_job(job: PhysicalDesignJob | ReferencePnrJob) -> PhysicalDesignJob:
    if isinstance(job, PhysicalDesignJob) and not isinstance(job, ReferencePnrJob):
        return job
    return PhysicalDesignJob(
        technology=job.technology,
        design=job.design,
        constraints=job.constraints,
        request=job.request,
        routing_constraints=job.routing_constraints,
    )


def _stable_result(result: PhysicalDesignResult | ReferencePnrResult) -> PhysicalDesignResult:
    if isinstance(result, PhysicalDesignResult) and not isinstance(result, ReferencePnrResult):
        return result
    return PhysicalDesignResult(
        status=result.status,
        placements=result.placements,
        constraint_outcomes=result.constraint_outcomes,
        stage_reports=result.stage_reports,
        provenance=result.provenance,
        routes=result.routes,
        routing_blockage_placements=result.routing_blockage_placements,
        closed=result.closed,
        artifact_id=result.artifact_id,
    )


class ClosureRepairDecision(str, Enum):
    ACCEPTED = "accepted"
    UNSUPPORTED = "unsupported"
    UNREPAIRABLE = "unrepairable"
    INVALID_IDENTITY = "invalid_identity"


class ClosureFeedbackKind(str, Enum):
    PHYSICAL_OWNER = "physical_owner"
    FIXED_PHYSICAL_BLOCKER = "fixed_physical_blocker"
    DRC_RULE = "drc_rule"
    LVS_MISMATCH = "lvs_mismatch"
    MATERIALIZATION = "materialization"
    IDENTITY = "identity"


@dataclass(frozen=True)
class ClosureFeedbackScope:
    """Typed attribution emitted by a closure attempt for repair policy."""

    kind: ClosureFeedbackKind
    identities: tuple[str, ...]
    source_evidence: tuple[str, ...]
    involved_nets: tuple[str, ...] = ()
    involved_groups: tuple[str, ...] = ()
    repairable: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, tuple)
            for value in (
                self.identities,
                self.source_evidence,
                self.involved_nets,
                self.involved_groups,
            )
        ):
            raise ClosureRepairError("closure feedback collections must be tuples")
        if not self.identities or any(
            not isinstance(value, str) or not value for value in self.identities
        ):
            raise ClosureRepairError("closure feedback needs typed identities")
        if any(
            not isinstance(value, str) or not value
            for value in (
                *self.source_evidence,
                *self.involved_nets,
                *self.involved_groups,
            )
        ):
            raise ClosureRepairError("closure feedback scope must contain text")
        if type(self.repairable) is not bool:
            raise ClosureRepairError("closure feedback repairable flag must be bool")


@dataclass(frozen=True)
class PlacementRepairDirective:
    """One exact project-owned mapping from evidence to a local placement."""

    feedback_kind: ClosureFeedbackKind
    feedback_identity: str
    physical_owner: PhysicalOwnerIdentity
    target: Placement
    legal_region: Rect
    maximum_displacement_dbu: int

    def __post_init__(self) -> None:
        if self.feedback_kind not in {
            ClosureFeedbackKind.PHYSICAL_OWNER,
            ClosureFeedbackKind.DRC_RULE,
        }:
            raise ClosureRepairError(
                "placement repair directives support only physical-owner or "
                "explicit DRC-rule feedback"
            )
        if not self.feedback_identity:
            raise ClosureRepairError("repair directive needs a feedback identity")
        if self.physical_owner.kind not in {
            PhysicalOwnerKind.INSTANCE,
            PhysicalOwnerKind.BLOCKAGE,
        }:
            raise ClosureRepairError(
                "repair directive physical owner must be an instance or blockage"
            )
        if len(self.physical_owner.locator) != 1:
            raise ClosureRepairError(
                "repair directive physical owner must have one normalized locator"
            )
        if (
            type(self.maximum_displacement_dbu) is not int
            or self.maximum_displacement_dbu <= 0
        ):
            raise ClosureRepairError(
                "repair directive displacement budget must be positive"
            )


@dataclass(frozen=True)
class ClosureRepairPolicy:
    """Explicit owner policy; directive order is canonical, not a search space."""

    owner: str
    policy_id: str
    maximum_displacement_dbu: int
    directives: tuple[PlacementRepairDirective, ...]

    def __post_init__(self) -> None:
        try:
            owner_identity(self.owner, "repair policy owner")
            identifier(self.policy_id, "repair policy identity")
        except ValueError as exc:
            raise ClosureRepairError(str(exc)) from exc
        if (
            type(self.maximum_displacement_dbu) is not int
            or self.maximum_displacement_dbu <= 0
        ):
            raise ClosureRepairError("repair policy displacement budget must be positive")
        if not isinstance(self.directives, tuple):
            raise ClosureRepairError("repair policy directives must be an immutable tuple")
        keys = tuple(
            (item.feedback_kind.value, item.feedback_identity)
            for item in self.directives
        )
        if len(keys) != len(set(keys)):
            raise ClosureRepairError(
                "repair policy must map each feedback identity exactly once"
            )

    def canonical_json(self) -> str:
        return canonical_json(self)


def closure_repair_policy_from_json(text: str) -> ClosureRepairPolicy:
    return canonical_from_json(text, ClosureRepairPolicy)


@dataclass(frozen=True)
class PhysicalPlacementRepair:
    physical_owner: PhysicalOwnerIdentity
    source: Placement
    target: Placement
    legal_region: Rect
    displacement_dbu: int
    maximum_displacement_dbu: int

    def __post_init__(self) -> None:
        if self.source == self.target:
            raise ClosureRepairError("physical placement repair must change placement")
        if type(self.displacement_dbu) is not int or self.displacement_dbu <= 0:
            raise ClosureRepairError("physical placement repair needs displacement")
        actual_displacement = abs(self.target.origin.x - self.source.origin.x) + abs(
            self.target.origin.y - self.source.origin.y
        )
        if self.displacement_dbu != actual_displacement:
            raise ClosureRepairError(
                "physical placement repair displacement does not match its geometry"
            )
        if self.displacement_dbu > self.maximum_displacement_dbu:
            raise ClosureRepairError("physical placement repair exceeds its budget")


@dataclass(frozen=True)
class RepairPlan:
    plan_id: str
    decision: ClosureRepairDecision
    owner: str
    policy_id: str
    parent_job_identity: str
    parent_result_identity: str
    parent_job: PhysicalDesignJob
    parent_result: PhysicalDesignResult
    feedback_identity: str
    policy_identity: str
    iteration_run_id: str
    flow_plan_identity: str
    evidence_artifact_identity: tuple[str, ...]
    source_evidence: tuple[str, ...]
    action: PhysicalPlacementRepair | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ClosureRepairError("repair plan needs an explicit semantic ID")
        try:
            owner_identity(self.owner, "repair plan owner")
            identifier(self.policy_id, "repair plan policy identity")
            run_identity(self.iteration_run_id)
        except ValueError as exc:
            raise ClosureRepairError(str(exc)) from exc
        for label, value in (
            ("parent job", self.parent_job_identity),
            ("parent result", self.parent_result_identity),
            ("feedback", self.feedback_identity),
            ("policy", self.policy_identity),
            ("Flow plan", self.flow_plan_identity),
            *(("evidence artifact", value) for value in self.evidence_artifact_identity),
        ):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ClosureRepairError(f"repair plan {label} must be non-empty")
        if (
            physical_design_job_id(self.parent_job) != self.parent_job_identity
            or physical_design_result_id(self.parent_result)
            != self.parent_result_identity
            or self.parent_result.provenance.job_identity
            != physical_design_job_id(self.parent_job)
        ):
            raise ClosureRepairError("repair plan parent typed record drift")
        if not self.reason:
            raise ClosureRepairError("repair plan needs a reason")
        if not isinstance(self.evidence_artifact_identity, tuple) or not isinstance(
            self.source_evidence, tuple
        ):
            raise ClosureRepairError("repair plan evidence must use immutable tuples")
        if self.decision is ClosureRepairDecision.ACCEPTED:
            if (
                self.action is None
                or not self.source_evidence
                or not self.evidence_artifact_identity
            ):
                raise ClosureRepairError(
                    "accepted repair plan needs an action and complete evidence"
                )
        elif self.action is not None:
            raise ClosureRepairError("rejected repair plan cannot carry an action")

    @property
    def accepted(self) -> bool:
        return self.decision is ClosureRepairDecision.ACCEPTED

    def canonical_json(self) -> str:
        return canonical_json(self)


def repair_plan_from_json(text: str) -> RepairPlan:
    return canonical_from_json(text, RepairPlan)


def _rejected(
    decision: ClosureRepairDecision,
    *,
    owner: str,
    policy: ClosureRepairPolicy,
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    feedback_identity: str,
    provenance: "ClosureIterationProvenance",
    source_evidence: tuple[str, ...],
    reason: str,
) -> RepairPlan:
    return RepairPlan(
        f"{owner}:closure-repair:{provenance.run_id}",
        decision,
        owner,
        policy.policy_id,
        physical_design_job_id(job),
        physical_design_result_id(result),
        job,
        result,
        feedback_identity,
        f"{policy.owner}:closure-repair-policy:{policy.policy_id}",
        provenance.run_id,
        provenance.plan_identity,
        tuple(item.identity for item in provenance.artifacts),
        source_evidence,
        None,
        reason,
    )


def _provenance_issue(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    provenance: "ClosureIterationProvenance",
) -> str | None:
    if result.provenance.job_identity != physical_design_job_id(job):
        return "parent result typed job does not match the parent job"
    by_label = {item.label: item for item in provenance.artifacts}
    if len(by_label) != len(provenance.artifacts):
        return "iteration provenance contains duplicate artifact labels"
    if "job" not in by_label or "result" not in by_label:
        return "iteration provenance omits parent job or result evidence"
    if by_label["job"].identity != physical_design_job_id(job):
        return "iteration provenance job identity does not match the parent job"
    if by_label["result"].identity != physical_design_result_id(result):
        return "iteration provenance result identity does not match the parent result"
    return None


def _feedback_match(
    directive: PlacementRepairDirective,
    feedback: tuple[ClosureFeedbackScope, ...],
) -> ClosureFeedbackScope | None:
    for scope in feedback:
        if (
            scope.repairable
            and scope.kind is directive.feedback_kind
            and directive.feedback_identity in scope.identities
            and (
                scope.kind is not ClosureFeedbackKind.PHYSICAL_OWNER
                or directive.physical_owner.stable_name in scope.identities
            )
        ):
            return scope
    return None


def _grid_aligned(point: Point, grid: int) -> bool:
    return point.x % grid == 0 and point.y % grid == 0


def _placement_action(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    directive: PlacementRepairDirective,
) -> tuple[PhysicalPlacementRepair | None, str]:
    owner = directive.physical_owner
    owner_name = owner.locator[0]
    grid = job.technology.manufacturing_grid_dbu
    if not _grid_aligned(directive.target.origin, grid):
        return None, "project-owned target placement is off the manufacturing grid"

    placements = {item.instance: item.placement for item in result.placements}
    if len(placements) != len(result.placements):
        return None, "parent result contains duplicate instance placements"
    blockage_placements = {
        item.blockage: item.placement for item in result.routing_blockage_placements
    }
    if len(blockage_placements) != len(result.routing_blockage_placements):
        return None, "parent result contains duplicate blockage placements"

    if owner.kind is PhysicalOwnerKind.INSTANCE:
        instances = {item.name: item for item in job.design.instances}
        masters = {item.name: item for item in job.design.masters}
        instance = instances.get(owner_name)
        if instance is None or owner_name not in placements:
            return None, "attributed instance is absent from the normalized result"
        if instance.fixed_placement is not None:
            return None, "attributed instance is fixed and cannot be repaired"
        master = masters[instance.master]
        if directive.target.orientation not in master.allowed_orientations:
            return None, "project-owned target uses an unsupported orientation"
        normalized_region = instance_region(job, owner_name)
        if (
            normalized_region is None
            or not normalized_region.contains(directive.legal_region)
        ):
            return None, "project repair region exceeds normalized hard constraints"
        target_shape = placed_rect(master, directive.target)
        if not directive.legal_region.contains(target_shape):
            return None, "project-owned target lies outside the repair region"

        masters_by_instance = {
            item.name: masters[item.master] for item in job.design.instances
        }
        if set(placements) != set(masters_by_instance):
            return None, "parent result does not place every normalized instance"
        rectangles = {
            name: placed_rect(masters_by_instance[name], placement)
            for name, placement in placements.items()
        }
        if any(
            rectangles_conflict(
                target_shape,
                other,
                job.request.minimum_instance_spacing_dbu,
            )
            for name, other in rectangles.items()
            if name != owner_name
        ):
            return None, "project-owned target overlaps another placed instance"
        proposed = dict(placements)
        proposed[owner_name] = directive.target
        proposed_rectangles = dict(rectangles)
        proposed_rectangles[owner_name] = target_shape
        if not all(
            evaluate_constraint(constraint, proposed_rectangles, proposed) is True
            for constraint in job.constraints
            if constraint.mode is ConstraintMode.HARD
        ):
            return None, "project-owned target violates a normalized hard constraint"
        source = placements[owner_name]
    else:
        blockages = {item.name: item for item in job.design.routing_blockages}
        blockage = blockages.get(owner_name)
        if blockage is None:
            return None, "attributed blockage is absent from the normalized job"
        if blockage.repair_region is None:
            return None, "attributed blockage is fixed and cannot be repaired"
        source = blockage_placements.get(owner_name, blockage.placement)
        if directive.target.orientation not in blockage.allowed_orientations:
            return None, "project-owned target uses an unsupported orientation"
        if not job.design.die.contains(directive.legal_region):
            return None, "project repair region exceeds the normalized die"
        if not blockage.repair_region.contains(directive.legal_region):
            return None, "project repair region exceeds the blockage repair region"
        target_shape = placed_sized_rect(
            blockage.width_dbu,
            blockage.height_dbu,
            directive.target,
        )
        if not directive.legal_region.contains(target_shape):
            return None, "project-owned target lies outside the repair region"

    displacement = abs(directive.target.origin.x - source.origin.x) + abs(
        directive.target.origin.y - source.origin.y
    )
    maximum = directive.maximum_displacement_dbu
    if displacement == 0:
        return None, "project-owned target does not change the attributed placement"
    if displacement > maximum:
        return None, "project-owned target exceeds the displacement budget"
    return (
        PhysicalPlacementRepair(
            owner,
            source,
            directive.target,
            directive.legal_region,
            displacement,
            maximum,
        ),
        "project-owned attributed placement repair is legal",
    )


def compile_closure_repair(
    *,
    owner: str,
    job: PhysicalDesignJob | ReferencePnrJob,
    result: PhysicalDesignResult | ReferencePnrResult,
    feedback: tuple[ClosureFeedbackScope, ...],
    policy: ClosureRepairPolicy,
    provenance: "ClosureIterationProvenance",
) -> RepairPlan:
    """Compile exactly one policy-selected, locally legal next-job repair."""

    job = _stable_job(job)
    result = _stable_result(result)

    try:
        owner_identity(owner, "closure repair owner")
    except ValueError as exc:
        raise ClosureRepairError(str(exc)) from exc
    feedback_identity = f"{owner}:closure-feedback:{provenance.run_id}"
    source_evidence = tuple(
        sorted({identity for scope in feedback for identity in scope.source_evidence})
    )
    if policy.owner != owner:
        return _rejected(
            ClosureRepairDecision.INVALID_IDENTITY,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason="repair policy owner does not match the campaign owner",
        )
    issue = _provenance_issue(job, result, provenance)
    if issue is not None:
        return _rejected(
            ClosureRepairDecision.INVALID_IDENTITY,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason=issue,
        )
    if result.status not in {ResultStatus.SUCCEEDED, ResultStatus.EXHAUSTED}:
        return _rejected(
            ClosureRepairDecision.UNREPAIRABLE,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason="parent physical-design result has no repairable geometry",
        )

    matched: tuple[PlacementRepairDirective, ClosureFeedbackScope] | None = None
    for directive in policy.directives:
        scope = _feedback_match(directive, feedback)
        if scope is not None:
            matched = directive, scope
            break
    if matched is None:
        return _rejected(
            ClosureRepairDecision.UNSUPPORTED,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason=(
                "owner policy has no exact mapping for the attributed feedback; "
                "LVS, PEX, post-layout, qualification, and unmapped DRC feedback "
                "remain unsupported"
            ),
        )

    directive, scope = matched
    action, reason = _placement_action(job, result, directive)
    if action is None:
        return _rejected(
            ClosureRepairDecision.UNREPAIRABLE,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason=reason,
        )
    maximum = min(
        directive.maximum_displacement_dbu,
        policy.maximum_displacement_dbu,
    )
    if action.displacement_dbu > maximum:
        return _rejected(
            ClosureRepairDecision.UNREPAIRABLE,
            owner=owner,
            policy=policy,
            job=job,
            result=result,
            feedback_identity=feedback_identity,
            provenance=provenance,
            source_evidence=source_evidence,
            reason="project-owned target exceeds the owner policy displacement budget",
        )
    action = replace(action, maximum_displacement_dbu=maximum)
    return RepairPlan(
        f"{owner}:closure-repair:{provenance.run_id}",
        ClosureRepairDecision.ACCEPTED,
        owner,
        policy.policy_id,
        physical_design_job_id(job),
        physical_design_result_id(result),
        job,
        result,
        feedback_identity,
        f"{policy.owner}:closure-repair-policy:{policy.policy_id}",
        provenance.run_id,
        provenance.plan_identity,
        tuple(item.identity for item in provenance.artifacts),
        tuple(sorted(set(scope.source_evidence))),
        action,
        reason,
    )


def apply_repair_plan(
    job: PhysicalDesignJob | ReferencePnrJob,
    plan: RepairPlan,
) -> ReferencePnrJob:
    """Apply an accepted plan without mutating its parent normalized job."""

    if not plan.accepted or plan.action is None:
        raise ClosureRepairError("only an accepted RepairPlan can produce a next job")
    stable_job = _stable_job(job)
    if (
        physical_design_job_id(stable_job) != plan.parent_job_identity
        or stable_job != plan.parent_job
    ):
        raise ClosureRepairError("RepairPlan parent job identity or typed record does not match")
    action = plan.action
    grid = stable_job.technology.manufacturing_grid_dbu
    if not _grid_aligned(action.target.origin, grid):
        raise ClosureRepairError("RepairPlan target is off the manufacturing grid")
    owner_name = action.physical_owner.locator[0]
    if action.physical_owner.kind is PhysicalOwnerKind.INSTANCE:
        masters = {item.name: item for item in stable_job.design.masters}
        changed = False
        instances = []
        for instance in stable_job.design.instances:
            if instance.name == owner_name:
                if instance.fixed_placement is not None:
                    raise ClosureRepairError("RepairPlan instance is no longer movable")
                master = masters[instance.master]
                if action.target.orientation not in master.allowed_orientations:
                    raise ClosureRepairError(
                        "RepairPlan target uses an unsupported orientation"
                    )
                region = instance_region(stable_job, owner_name)
                if region is None or not region.contains(action.legal_region):
                    raise ClosureRepairError(
                        "RepairPlan region exceeds normalized hard constraints"
                    )
                if not action.legal_region.contains(placed_rect(master, action.target)):
                    raise ClosureRepairError(
                        "RepairPlan target lies outside its legal region"
                    )
                instances.append(replace(instance, fixed_placement=action.target))
                changed = True
            else:
                instances.append(instance)
        if not changed:
            raise ClosureRepairError("RepairPlan instance is absent from the parent job")
        design = replace(stable_job.design, instances=tuple(instances))
    else:
        changed = False
        blockages = []
        for blockage in stable_job.design.routing_blockages:
            if blockage.name == owner_name:
                if blockage.repair_region is None:
                    raise ClosureRepairError("RepairPlan blockage is no longer movable")
                if action.target.orientation not in blockage.allowed_orientations:
                    raise ClosureRepairError(
                        "RepairPlan target uses an unsupported orientation"
                    )
                if (
                    not stable_job.design.die.contains(action.legal_region)
                    or not blockage.repair_region.contains(action.legal_region)
                    or not action.legal_region.contains(
                        placed_sized_rect(
                            blockage.width_dbu,
                            blockage.height_dbu,
                            action.target,
                        )
                    )
                ):
                    raise ClosureRepairError(
                        "RepairPlan blockage target exceeds its legal region"
                    )
                blockages.append(replace(blockage, placement=action.target))
                changed = True
            else:
                blockages.append(blockage)
        if not changed:
            raise ClosureRepairError("RepairPlan blockage is absent from the parent job")
        design = replace(stable_job.design, routing_blockages=tuple(blockages))

    lineage = ReferencePnrJobLineage(
        plan.owner,
        plan.parent_job_identity,
        plan.parent_result_identity,
        plan.feedback_identity,
        plan.plan_id,
        plan.source_evidence,
    )
    reference_job = (
        job
        if isinstance(job, ReferencePnrJob)
        else ReferencePnrJob(
            technology=stable_job.technology,
            design=stable_job.design,
            constraints=stable_job.constraints,
            request=stable_job.request,
            routing_constraints=stable_job.routing_constraints,
        )
    )
    return replace(
        reference_job,
        design=design,
        repair_lineage=lineage,
    )


__all__ = [
    "ClosureFeedbackKind",
    "ClosureFeedbackScope",
    "ClosureRepairDecision",
    "ClosureRepairError",
    "ClosureRepairPolicy",
    "PhysicalPlacementRepair",
    "PlacementRepairDirective",
    "RepairPlan",
    "apply_repair_plan",
    "closure_repair_policy_from_json",
    "compile_closure_repair",
    "repair_plan_from_json",
]
